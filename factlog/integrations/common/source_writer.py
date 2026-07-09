#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The source-agnostic half of writing a factlog ``sources/<slug>.md`` original.

Every integration (Zotero, OpenAlex, ...) turns some upstream record into one
markdown file with YAML front matter. What differs between them is small — the
identity field's name, the front matter, the body — and what is shared is not:
atomic writes, slug construction, global-unique filenames, the batch index, and
duplicate detection. That shared half lives here; a subclass supplies the rest.

Four invariants hold for every integration:

* **P4 (original immutability).** An existing file is never overwritten or
  deleted. A fresh, globally-unique filename is chosen instead, and the write is
  atomic (temp file + ``os.replace``).
* **P3 (idempotent re-import).** A record whose identity already appears in
  ``sources/`` is skipped (when ``skip_duplicates``), so re-running an import
  leaves the filesystem unchanged.
* **Global-unique slugs (spec §12).** When a base slug is already claimed by a
  *different* record, a ``-2``/``-3`` suffix is appended.
* **Cross-source duplicate detection (spec §7.1).** DOI, PMID and a normalized
  arXiv id are read from *every* source file, whatever imported it, so the same
  paper arriving from a second database is reported rather than written twice.
  Detection lives here; the *merge* itself (§7.3) is a side effect deferred to
  :meth:`BaseSourceWriter._merge`, a no-op unless a writer opts in via
  :attr:`BaseSourceWriter.merges_cross_source` (only ``ArxivSourceWriter`` does).
  So classification stays pure and ``plan``/``write`` agree, while the sidecar
  write happens only in ``write``.

``imported_at`` is injected by the caller rather than read from a clock here, so
writers stay pure and unit-testable and the CLI controls the (single, batch)
timestamp. Because suffix assignment depends on the directory's current state,
the caller must feed records in a deterministic order for reproducible suffixes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from factlog.common import slugify
from factlog.integrations.arxiv.id_normalizer import ArxivIdError, normalize_arxiv_id
from factlog.integrations.common._textio import atomic_write_text
from factlog.integrations.common.front_matter import read_scalars

# Byte budgets for the filename (most filesystems cap a name at 255 bytes).
# Author and title are individually bounded, then the whole stem is capped with
# headroom left for a "-NN" uniqueness suffix and the ".md" extension.
AUTHOR_SLUG_MAX_BYTES = 64
TITLE_SLUG_MAX_BYTES = 80
STEM_MAX_BYTES = 190

# Identifiers that identify the same *paper* across databases, in §7.1's
# priority order: DOI is the most trustworthy, PMID next, then the normalized
# arXiv base id (the only exact join key for preprints, since a preprint rarely
# carries a DOI). Mapped to the label used when reporting a duplicate.
CROSS_SOURCE_IDS = (("doi", "DOI"), ("pmid", "PMID"), ("arxiv_id", "arXiv id"))

# Front-matter field recording which integration wrote a source file. Read
# alongside the identity/cross-id keys so identity registration can be scoped by
# provenance (see :meth:`BaseSourceWriter._index`).
IMPORTED_FROM_KEY = "imported_from"


def _same_source(imported_from: str, source_name: str) -> bool:
    """Did *source_name*'s integration write a file whose provenance is *imported_from*?

    An absent value means a legacy or hand-written file and counts as this
    writer's own, so re-import stays idempotent (P3). The comparison ignores case
    and surrounding space: writers always emit lowercase, and a human editing the
    front matter must not be able to reclassify the file by typing ``OpenAlex``.
    """
    return imported_from.strip().lower() in ("", source_name.lower())

_CROSS_SOURCE_KINDS = frozenset(kind for kind, _ in CROSS_SOURCE_IDS)


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a single :meth:`BaseSourceWriter.write` call.

    ``status`` is ``"imported"`` | ``"skipped"`` | ``"error"`` | ``"merged"``.
    ``"merged"`` is produced only by a writer that opts in via
    :attr:`BaseSourceWriter.merges_cross_source`: the incoming record is a *second
    database's* view of a paper already in ``sources/``, so instead of writing a
    new original it is appended to the existing file's provenance sidecar (§7.3).
    ``path`` then points at the **existing** original, which is never rewritten.
    """

    path: Path | None
    status: str  # "imported" | "skipped" | "error" | "merged"
    reason: str = ""


@dataclass
class _DirIndex:
    """One scan of a ``sources/`` directory, kept current as names are reserved."""

    claimed: set[str] = field(default_factory=set)
    # identity value -> (path, that file's ``imported_from``; "" when it has none).
    # EVERY file carrying this writer's identity key registers here, whichever
    # integration wrote it. Provenance rides along so :meth:`_duplicate` can tell
    # "this record was re-imported" from "this paper is already here via another
    # database" — it must never decide whether the file is *found*.
    by_identity: dict[str, tuple[Path, str]] = field(default_factory=dict)
    # ("doi", "10.1234/x") -> path. Populated from every source file regardless
    # of which integration wrote it, which is what makes §7.1 detection work.
    # ("doi", "10.1234/x") -> (path, that file's `imported_from`). Populated from
    # every source file regardless of which integration wrote it, which is what
    # makes §7.1 detection work. Provenance rides along so a duplicate found
    # inside this writer's OWN database is not mistaken for another database's
    # view of the paper: two arXiv deposits that share a DOI are a plain
    # duplicate, not a cross-source merge.
    by_cross_id: dict[tuple[str, str], tuple[Path, str]] = field(default_factory=dict)


def byte_trunc(slug: str, max_bytes: int) -> str:
    """Trim a slug to <= max_bytes UTF-8 bytes, multibyte-safe, on a '-' edge."""
    encoded = slug.encode("utf-8")
    if len(encoded) <= max_bytes:
        return slug
    cut = encoded[:max_bytes].decode("utf-8", "ignore")
    if "-" in cut:
        cut = cut.rsplit("-", 1)[0]
    return cut.strip("-") or cut


def slug_or(raw: str, fallback: str, max_bytes: int) -> str:
    """slugify a raw field, byte-capped; use fallback only when raw is blank.

    Branching on the *raw* value (not on slugify's "item" empty-fallback) avoids
    forcing a legitimate title like "Item Response Theory" to the fallback.
    """
    if not raw.strip():
        return fallback
    return byte_trunc(slugify(raw), max_bytes)


def build_slug(author: str, year: str, title: str) -> str:
    """Base filename ``{author}-{year}-{title}.md`` (no uniqueness suffix).

    Missing pieces degrade gracefully: no author -> ``anonymous``, no year ->
    ``n-d`` in the year slot, no title -> ``untitled``. Each component and the
    whole stem are byte-capped so a long/non-ASCII field cannot overflow the
    filesystem name limit.
    """
    author_slug = slug_or(author, "anonymous", AUTHOR_SLUG_MAX_BYTES)
    year_slug = slugify(year) if year.strip() else "n-d"
    title_slug = slug_or(title, "untitled", TITLE_SLUG_MAX_BYTES)
    return f"{byte_trunc(f'{author_slug}-{year_slug}-{title_slug}', STEM_MAX_BYTES)}.md"


def normalize_cross_id(kind: str, value: str) -> str:
    """Canonical comparison form for a cross-source identifier.

    DOIs are case-insensitive, so a Zotero record's ``10.1378/CHEST...`` must
    match OpenAlex's lowercased form; otherwise the same paper imports twice.

    An ``arxiv_id`` is canonicalised the way :func:`normalize_arxiv_id` does —
    version stripped, subject class dropped, archive lowercased — so
    ``2311.09277v2`` and ``2311.09277``, or ``math.GT/0309136`` and
    ``math/0309136``, collide as the same paper. That normalizer is reused rather
    than reimplemented as a regex, which would miss old-style ids and URL forms;
    it imports only stdlib and arXiv config, so ``common`` depending on it is no
    cycle.

    **Tolerant of junk on purpose.** ``normalize_cross_id`` runs over
    hand-editable source files (via :meth:`BaseSourceWriter._index`), where one
    malformed ``arxiv_id:`` would otherwise abort *every* import in the KB.
    ``normalize_arxiv_id`` *raises* ``ArxivIdError`` on a bad value; here we catch
    it and fall back to ``value.strip()`` so the bad file simply does not match
    anything (arXiv ids are case-significant, so this is deliberately not
    lowercased). The CLI stays strict — a mistyped ``--arxiv-id`` is validated at
    input — but junk already sitting in a file is tolerated, mirroring the
    parser's optional-field handling.
    """
    normalized = value.strip()
    if kind == "doi":
        return normalized.lower()
    if kind == "arxiv_id":
        try:
            return normalize_arxiv_id(normalized).base
        except ArxivIdError:
            return normalized
    return normalized


class BaseSourceWriter:
    """Render parsed records into ``sources/`` markdown originals.

    Subclasses declare :attr:`identity_key` — the front-matter field that makes
    re-import idempotent — and implement :meth:`identity_of`, :meth:`slug_fields`,
    :meth:`cross_ids` and :meth:`render`.
    """

    #: Front-matter field carrying the record's identity (e.g. ``zotero_key``).
    identity_key: str = ""
    #: The value this writer emits as ``imported_from``. Identity registration in
    #: :meth:`_index` is scoped to files bearing this provenance (or none), so a
    #: foreign file that merely *carries* this writer's identity key as a
    #: cross-source id is not mistaken for a prior import by this writer.
    source_name: str = ""
    #: Front-matter pattern marking a companion file to exclude from the index.
    ignore_re: re.Pattern[str] | None = None
    #: Opt-in: does this writer merge a cross-source duplicate into the existing
    #: file's provenance sidecar (§7.3) instead of reporting a bare ``skipped``?
    #: **False by default**, which is what keeps Zotero and OpenAlex from ever
    #: touching a sidecar. Only :class:`ArxivSourceWriter` sets it, and only it
    #: overrides :meth:`_merge`. When False, a cross-source duplicate stays
    #: ``skipped`` exactly as before, so those integrations are unchanged.
    merges_cross_source: bool = False

    def __init__(self, skip_duplicates: bool = True, include_abstract: bool = True):
        self.skip_duplicates = skip_duplicates
        self.include_abstract = include_abstract
        # Per-(directory, mode) index, scanned once then kept current as we
        # reserve, so a batch is O(files + N) rather than O(N x files). A dry-run
        # plan reserves in its OWN index so it can still predict collision
        # suffixes across a batch WITHOUT polluting the write path (a plan() must
        # never make a later write() on the same instance skip).
        self._dir_index: dict[tuple[str, str], _DirIndex] = {}

    # -- subclass contract -------------------------------------------------
    def identity_of(self, parsed) -> str:
        """The record's stable identity, or "" when it has none."""
        raise NotImplementedError

    def slug_fields(self, parsed) -> tuple[str, str, str]:
        """``(first-author name, year, title)`` as raw text for the slug."""
        raise NotImplementedError

    def cross_ids(self, parsed) -> dict[str, str]:
        """Cross-database identifiers (``doi``, ``pmid``) this record carries."""
        return {}

    def _cross_id_values(self, parsed) -> dict[str, str]:
        """Cross-source ids for the incoming record, in :data:`CROSS_SOURCE_IDS` form.

        A writer whose ``identity_key`` is *itself* a cross-source id (the arXiv
        writer, whose ``arxiv_id`` is both its identity and a §7.1 join key)
        contributes that identity as the cross-id automatically. This is why the
        arXiv writer needs no ``cross_ids()`` override: the same paper reached
        through another database is detected without every such writer having to
        re-declare its identity key.
        """
        values = dict(self.cross_ids(parsed))
        if self.identity_key in _CROSS_SOURCE_KINDS:
            identity = self.identity_of(parsed)
            if identity:
                values.setdefault(self.identity_key, identity)
        return values

    def render(self, parsed, imported_at: str = "") -> str:
        """The full markdown text (front matter + body)."""
        raise NotImplementedError

    # -- shared machinery --------------------------------------------------
    def generate_slug(self, parsed) -> str:
        return build_slug(*self.slug_fields(parsed))

    def _scan_keys(self) -> tuple[str, ...]:
        return (self.identity_key, IMPORTED_FROM_KEY, *(kind for kind, _ in CROSS_SOURCE_IDS))

    def _index(self, sources_dir: Path, mode: str) -> _DirIndex:
        key = (str(sources_dir.resolve()), mode)
        cached = self._dir_index.get(key)
        if cached is None:
            cached = _DirIndex()
            if sources_dir.is_dir():
                for path in sorted(sources_dir.glob("*.md")):
                    cached.claimed.add(path.name)
                    scalars = read_scalars(path, self._scan_keys(), self.ignore_re)
                    identity = scalars.get(self.identity_key, "")
                    # Registration is unconditional, and provenance rides along.
                    # Gating registration on ``imported_from`` is tempting —
                    # ``arxiv_id`` is both the arXiv writer's identity key and a
                    # cross-source id, so an OpenAlex file carrying ``arxiv_id:``
                    # lands here too — but then a file whose ``imported_from`` a
                    # human capitalised or misspelled would not register at all,
                    # and re-importing it would write a *second* file.
                    # ``openalex_id`` and ``zotero_key`` are not cross-source ids,
                    # so nothing catches the miss: P3 breaks in silence.
                    # Provenance decides the *report*, never the lookup.
                    if identity:
                        cached.by_identity.setdefault(
                            identity, (path, scalars.get(IMPORTED_FROM_KEY, "")))
                    for kind, _ in CROSS_SOURCE_IDS:
                        value = scalars.get(kind, "")
                        if value:
                            cached.by_cross_id.setdefault(
                                (kind, normalize_cross_id(kind, value)),
                                (path, scalars.get(IMPORTED_FROM_KEY, "")))
            self._dir_index[key] = cached
        return cached

    def _unique_path(self, sources_dir: Path, base_slug: str, claimed: set[str]) -> Path:
        """A path whose name no existing/just-written file claims (-2, -3, ...)."""
        if base_slug not in claimed:
            return sources_dir / base_slug
        stem = base_slug[:-3]  # strip '.md'
        index = 2
        while f"{stem}-{index}.md" in claimed:
            index += 1
        return sources_dir / f"{stem}-{index}.md"

    def _duplicate(self, parsed, index: _DirIndex) -> WriteResult | None:
        """A skip for a record already in ``sources/``, or None to import it.

        Identity is checked first (the same record re-imported), then the
        cross-database identifiers in §7.1's priority order (the same *paper*
        reached through another database, or another record of the same paper —
        a preprint and its journal version share a DOI).

        Two outcomes are distinguished. A file this writer produced (or a legacy
        one with no ``imported_from``) is the **same record re-imported** and is
        always ``skipped`` — P3 never depends on a provenance string. A file
        written by *another* database, or matched only through a shared
        cross-source id, is the **same paper via another database**: for a writer
        that opts in (:attr:`merges_cross_source`) that is a ``merged``, otherwise
        it stays ``skipped``. The classification is pure — no file is touched here
        — so :meth:`plan` and :meth:`write` agree and ``--dry-run`` writes nothing.
        """
        identity = self.identity_of(parsed)
        found = index.by_identity.get(identity)
        if found is not None:
            existing, imported_from = found
            if _same_source(imported_from, self.source_name):
                return WriteResult(existing, "skipped",
                                   f"already imported ({self.identity_key} match)")
            label = dict(CROSS_SOURCE_IDS).get(self.identity_key, self.identity_key)
            return self._cross_source(
                existing, f"duplicate {label} {identity} (already in {existing.name})")

        cross_ids = self._cross_id_values(parsed)
        for kind, label in CROSS_SOURCE_IDS:
            value = cross_ids.get(kind, "")
            if not value:
                continue
            found = index.by_cross_id.get((kind, normalize_cross_id(kind, value)))
            if found is not None:
                existing, imported_from = found
                reason = f"duplicate {label} {value} (already in {existing.name})"
                # A shared identifier inside this writer's OWN database is a plain
                # duplicate, not another database's view of the paper: two arXiv
                # deposits sharing a DOI must not fold one into the other's ledger.
                # Merging describes a record this writer did not write.
                if _same_source(imported_from, self.source_name):
                    return WriteResult(existing, "skipped", reason)
                return self._cross_source(existing, reason)
        return None

    def _cross_source(self, existing: Path, reason: str) -> WriteResult:
        """Outcome for the same paper reached through another database.

        ``merged`` when this writer opts into §7.3 merging, else ``skipped`` — the
        historic behaviour, and what leaves Zotero/OpenAlex untouched. This is the
        only decision point; the sidecar write itself is deferred to
        :meth:`write` via :meth:`_merge`, so classifying (in ``plan`` too) has no
        side effect.
        """
        status = "merged" if self.merges_cross_source else "skipped"
        return WriteResult(existing, status, reason)

    def _reserve(self, parsed, sources_dir: Path, index: _DirIndex) -> WriteResult:
        path = self._unique_path(sources_dir, self.generate_slug(parsed), index.claimed)
        index.claimed.add(path.name)
        # A file this writer is about to create carries its own ``imported_from``.
        index.by_identity.setdefault(self.identity_of(parsed), (path, self.source_name))
        cross_ids = self._cross_id_values(parsed)
        for kind, _ in CROSS_SOURCE_IDS:
            value = cross_ids.get(kind, "")
            if value:
                index.by_cross_id.setdefault(
                    (kind, normalize_cross_id(kind, value)), (path, self.source_name))
        return WriteResult(path, "imported")

    def _resolve(self, parsed, target: Path | str, mode: str) -> WriteResult:
        """Decide the outcome (imported/skipped/error) and reserve the target name.

        Shared by :meth:`write` (mode "write") and :meth:`plan` (mode "plan") so a
        dry run predicts exactly what a real run would do, including collision
        suffixes. The two modes hold separate indexes. No file is touched here.

        A record with no identity is an error rather than a write: without one
        there is no way to keep re-import idempotent, so a new file would
        proliferate on every run.
        """
        if not self.identity_of(parsed):
            return WriteResult(None, "error", f"missing {self.identity_key}")

        sources_dir = Path(target) / "sources"
        index = self._index(sources_dir, mode)

        if self.skip_duplicates:
            duplicate = self._duplicate(parsed, index)
            if duplicate is not None:
                return duplicate

        return self._reserve(parsed, sources_dir, index)

    def plan(self, parsed, target: Path | str) -> WriteResult:
        """Predict :meth:`write`'s outcome without creating any file (dry run).

        Safe to interleave with :meth:`write` on the same instance — plan uses a
        separate reservation index, so it never makes a later write() skip.
        """
        return self._resolve(parsed, target, "plan")

    def _merge(self, parsed, decision: WriteResult, imported_at: str) -> WriteResult:
        """Fold *parsed* into the existing original's provenance sidecar.

        The one place a merge's side effect lives, so it is reached only from
        :meth:`write` and never from :meth:`plan` — that is what keeps
        ``--dry-run`` from touching the filesystem. **No-op on the base class**:
        it returns the ``merged`` decision unchanged without writing anything. A
        base writer never even reaches here (it cannot produce ``merged`` while
        :attr:`merges_cross_source` is False); the no-op exists so the hook is
        safe by construction and only a writer that opts in overrides it. The
        override owns building a source-specific record and read-modify-writing
        the sidecar at ``sidecar_path(decision.path)`` — ``decision.path`` is the
        **existing** original, never a would-be new file.
        """
        return decision

    def _record(self, parsed, decision: WriteResult, imported_at: str) -> WriteResult:
        """Write the source's *own* provenance record for a new-file import.

        The sibling of :meth:`_merge`. Where ``_merge`` folds a *second*
        database's view into an existing original's sidecar, ``_record`` writes
        the record for the file this writer is *itself* creating, so an ordinary
        import — a new paper, no duplicate — still leaves a ledger. Without it the
        sidecar would exist only where a collision happened, an artifact of import
        order rather than a record of every source (#72).

        Same opt-in posture as :meth:`_merge` and **no-op on the base class**: it
        returns the ``imported`` decision unchanged without touching the
        filesystem, so Zotero and OpenAlex — which never override it — stay
        byte-identical. Only :class:`ArxivSourceWriter` overrides it, behind the
        same ``merges_cross_source`` opt-in. Reached only from :meth:`write` and
        only on an ``imported`` outcome, never from :meth:`plan`, so ``--dry-run``
        writes nothing. The override read-modify-writes ``sidecar_path(decision.path)``
        for the **new** original's final (suffix-resolved) name.

        On success it returns the ``imported`` decision; on failure it returns an
        ``error`` so :meth:`write` can refuse to create the ``.md`` (see there).
        """
        return decision

    def write(self, parsed, target: Path | str, imported_at: str = "") -> WriteResult:
        """Write one source file under ``<target>/sources/`` and report the outcome."""
        decision = self._resolve(parsed, target, "write")
        if decision.status == "merged":
            # The existing original stays byte- and mtime-immutable (P4); the only
            # write is to its sidecar, and only writers that opt in do it.
            return self._merge(parsed, decision, imported_at)
        if decision.status != "imported":
            return decision
        # Sidecar FIRST, original LAST. The ``.md`` is the P3 skip key: once it
        # exists a re-import is ``skipped`` before any write (see :meth:`_duplicate`),
        # so the file must not appear until its ledger already does. If the sidecar
        # write fails, ``_record`` returns an ``error`` and we create no ``.md`` —
        # nothing is orphaned, and a retry re-runs cleanly. The reverse order would
        # leave an orphaned ``.md`` whose existence permanently suppresses the
        # ledger, since the original is never rewritten (P4) and the re-import skips
        # (#72, risk 2). ``_record`` is a no-op unless a writer opts in, so a
        # base/Zotero/OpenAlex import writes the ``.md`` exactly as before.
        recorded = self._record(parsed, decision, imported_at)
        if recorded.status != "imported":
            return recorded
        sources_dir = Path(target) / "sources"
        sources_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(decision.path, self.render(parsed, imported_at))
        return decision
