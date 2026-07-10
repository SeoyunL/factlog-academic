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
  :attr:`BaseSourceWriter.merges_cross_source` (``ArxivSourceWriter`` and
  ``OpenAlexSourceWriter`` do; Zotero does not). So classification stays pure and
  ``plan``/``write`` agree, while the sidecar write happens only in ``write``.

``imported_at`` is injected by the caller rather than read from a clock here, so
writers stay pure and unit-testable and the CLI controls the (single, batch)
timestamp. Because suffix assignment depends on the directory's current state,
the caller must feed records in a deterministic order for reproducible suffixes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from factlog.common import slugify
from factlog.integrations.arxiv.id_normalizer import ArxivIdError, normalize_arxiv_id
from factlog.integrations.common._textio import atomic_write_text
from factlog.integrations.common.front_matter import read_first_author, read_scalars
from factlog.integrations.common.matcher import (
    MatchInput,
    TITLE_SIMILARITY_THRESHOLD,
    score_pair,
    surname,
)
from factlog.integrations.common.merge_candidates import (
    STATE_PENDING,
    CandidatePair,
    MergeCandidatesError,
    add_candidate,
    candidates_path,
    read_candidates,
    write_candidates,
)
from factlog.integrations.common.provenance import (
    Provenance,
    ProvenanceConflict,
    ProvenanceError,
    SourceRecord,
    add_source,
    read_provenance,
    sidecar_path,
    write_provenance,
)

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

# Each integration's own identity front-matter key, by the ``imported_from`` value
# it emits. Used only by the candidate matcher (#75) to read an *existing* source's
# own identity so a surfaced pair keys on the two sources' identities, not their
# filenames. Distinct from ``identity_key`` (this writer's key): the matcher must
# name whichever integration wrote the file it matched against.
IDENTITY_KEYS_BY_SOURCE = {
    "openalex": "openalex_id",
    "arxiv": "arxiv_id",
    "zotero": "zotero_key",
}


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
class CandidateMatch:
    """A title+author+year match a human should look at, surfaced but never merged (#75).

    Produced by :meth:`BaseSourceWriter._find_candidate` when the incoming paper is
    imported as a *new* file yet resembles an existing source that shares no exact
    identifier. It is a *report*, not an outcome: the paper still imports, the status
    stays ``"imported"``, and nothing is merged (the spike proved the harmful cases
    are unreachable by these three fields — see :mod:`factlog.integrations.common.matcher`).

    ``incoming`` and ``existing`` are the two sources' own ``(type, id)`` identities,
    the key the ledger records so the pair is never re-proposed. ``existing_path``
    names the file already in ``sources/`` (unchanged), and ``score`` is the title
    Jaccard that cleared :data:`~factlog.integrations.common.matcher.TITLE_SIMILARITY_THRESHOLD`.
    """

    incoming: tuple[str, str]
    existing: tuple[str, str]
    existing_path: Path
    score: float


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a single :meth:`BaseSourceWriter.write` call.

    ``status`` is ``"imported"`` | ``"skipped"`` | ``"error"`` | ``"merged"``.
    ``"merged"`` is produced only by a writer that opts in via
    :attr:`BaseSourceWriter.merges_cross_source`: the incoming record is a *second
    database's* view of a paper already in ``sources/``, so instead of writing a
    new original it is appended to the existing file's provenance sidecar (§7.3).
    ``path`` then points at the **existing** original, which is never rewritten.

    ``candidate`` is a **field, never a status** (#75). A title+author+year match
    that could not rule out a merge onto a genuinely different source record is
    surfaced here while ``status`` stays ``"imported"`` — the counters and exit codes
    are untouched, and the CLI must keep it out of its status ternary (the #65 trap).
    It is ``None`` unless the incoming paper both imported as a new file and matched.
    """

    path: Path | None
    status: str  # "imported" | "skipped" | "error" | "merged"
    reason: str = ""
    candidate: CandidateMatch | None = None


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
    # Per-file title+author+year data for the candidate matcher (#75), populated
    # ONLY when a writer opts in via :attr:`BaseSourceWriter.surfaces_candidates`, so
    # a non-surfacing writer's scan is byte-identical to before. Each row is the
    # file's own identity and the three fields the matcher compares.
    match_rows: list[_MatchRow] = field(default_factory=list)


@dataclass(frozen=True)
class _MatchRow:
    """One existing source's identity and matchable fields, for the candidate scan."""

    path: Path
    identity: tuple[str, str]  # (imported_from, that source's own id)
    match: MatchInput


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
    #: **False by default**, which is what keeps Zotero from ever touching a
    #: sidecar. :class:`ArxivSourceWriter` and :class:`OpenAlexSourceWriter` set it
    #: True; the shared :meth:`_merge`/:meth:`_record` machinery below reads it.
    #: When False, a cross-source duplicate stays ``skipped`` exactly as before, so
    #: Zotero is unchanged.
    merges_cross_source: bool = False
    #: Opt-in: does this writer surface a title+author+year *candidate* (#75) when an
    #: imported paper resembles an existing source that shares no exact identifier?
    #: **False by default**, which is what keeps Zotero from ever surfacing one (#75
    #: H2 — a Zotero item is the same paper as seen by the *user*, not by a database,
    #: so cross-source candidates against it are noise) and keeps a non-surfacing
    #: writer's ``sources/`` scan byte-identical (no extra reads). ``ArxivSourceWriter``
    #: and ``OpenAlexSourceWriter`` set it True. Surfacing NEVER merges and never
    #: changes ``status`` — the paper still imports as a new file.
    surfaces_candidates: bool = False
    #: Fields in a provenance record whose change an *import* has no authority to
    #: absorb; a drift in one is a per-id ``error`` only a refresh command may
    #: clear. **Empty by default, and that is load-bearing.** A writer with no
    #: refresh command must leave it empty, or a drift becomes a *permanently*
    #: unclearable error — nothing exists to call :func:`update_source`. OpenAlex
    #: has no refresh command (nothing calls ``update_source`` for it), so it keeps
    #: the empty default: every field drifts silently, first-import-wins, and
    #: :meth:`_divergence` is never reached. Only :class:`ArxivSourceWriter` sets a
    #: non-empty tuple, because only arXiv ships ``arxiv-check-versions`` to clear
    #: the divergence it raises.
    _IDENTIFYING_FIELDS: tuple[str, ...] = ()

    #: Set when the merge-candidate ledger could not be read or written. The
    #: import still succeeds — a paper the user asked for should arrive (P1) — but
    #: the fallback is disabled for that run, and a silently disabled check is the
    #: failure this whole detection layer exists to prevent. The importer copies it
    #: into the report so the CLI can say so once.
    candidate_ledger_error: str | None = None

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
        keys = (self.identity_key, IMPORTED_FROM_KEY, *(kind for kind, _ in CROSS_SOURCE_IDS))
        if self.surfaces_candidates:
            # The matcher also needs each file's title, year and own identity key.
            # Only a surfacing writer reads them, so a non-surfacing writer's scan is
            # unchanged. ``dict.fromkeys`` dedups while preserving order (``arxiv_id``
            # is both a cross-source id and arXiv's own identity key).
            keys = tuple(dict.fromkeys(
                (*keys, "title", "year", *IDENTITY_KEYS_BY_SOURCE.values())))
        return keys

    def _match_row(self, path: Path, scalars: dict[str, str]) -> _MatchRow | None:
        """Build a matcher row for an existing source, or None if it has no usable
        identity or no readable first author.

        Identity is ``(imported_from, that source's own id)``. A tool-written file
        always carries ``imported_from``; a legacy/hand-written file without it falls
        back to whichever recognised identity key is present. A file whose identity
        cannot be pinned, or whose first author cannot be read, yields no row — the
        matcher then simply never proposes a pair against it (fails closed).
        """
        provenance = scalars.get(IMPORTED_FROM_KEY, "").strip().lower()
        source_type, identity = "", ""
        if provenance in IDENTITY_KEYS_BY_SOURCE:
            source_type = provenance
            identity = scalars.get(IDENTITY_KEYS_BY_SOURCE[provenance], "").strip()
        else:
            # No (or unrecognised) provenance: infer the type from the first
            # recognised identity key the file actually carries.
            for stype, ikey in IDENTITY_KEYS_BY_SOURCE.items():
                value = scalars.get(ikey, "").strip()
                if value:
                    source_type, identity = stype, value
                    break
        if not source_type or not identity:
            return None
        first_author = read_first_author(path, self.ignore_re)
        if not surname(first_author):
            return None
        year_raw = scalars.get("year", "").strip()
        year = int(year_raw) if year_raw.isdigit() else None
        match = MatchInput(first_author, year, scalars.get("title", ""))
        return _MatchRow(path, (source_type, identity), match)

    def _index(self, sources_dir: Path, mode: str) -> _DirIndex:
        key = (str(sources_dir.resolve()), mode)
        cached = self._dir_index.get(key)
        if cached is None:
            cached = _DirIndex()
            if sources_dir.is_dir():
                # `rglob`, because `ingest` mirrors an original's subtree and a nested
                # original is a real source (#112). A flat walk left `sources/sub/x.md`
                # out of `by_identity`, so re-importing that paper wrote a *second* `.md`
                # for it and broke P3 idempotence in silence.
                #
                # Hidden paths are NOT filtered here, and this index is the one sources/
                # walk that must not filter them. `provenance_sources` excludes them
                # because they are not *sources* (#67) — nothing syncs them, nothing gives
                # them a ledger. This index answers a different question: does a file on
                # disk already claim this identity, or this name? A `.md` that exists can
                # be duplicated whether or not `sync` counts it. Skipping `sources/.h/x.md`
                # here makes a re-import of that paper write a *second* `.md` — measured,
                # and the same silent P3 break #112 fixes for nested files. Indexing it
                # instead yields a `skipped`, which the report names. A skip the operator
                # can see beats a duplicate they cannot.
                for path in sorted(sources_dir.rglob("*.md")):
                    # `claimed` stays flat on purpose: it exists to keep `_unique_path`
                    # from overwriting a file, and this writer only ever creates files
                    # directly under `sources/`. Adding a nested file's bare name would
                    # push a new import to `x-2.md` because an unrelated `sub/x.md` exists.
                    if path.parent == sources_dir:
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
                    if self.surfaces_candidates:
                        row = self._match_row(path, scalars)
                        if row is not None:
                            cached.match_rows.append(row)
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
            # An identity match on a key that is ALSO a cross-source id (arXiv's
            # ``arxiv_id``) can land in a file another database wrote — OpenAlex
            # echoes ``arxiv_id`` into its front matter — so a foreign
            # ``imported_from`` there means "same paper via another database" and
            # is a merge for a writer that opts in. But a key no other database
            # emits (``openalex_id``, ``zotero_key``) can only be *this* writer's
            # own record; a mistyped ``imported_from`` beside it is corruption, not
            # another database, so it stays a same-source skip. P3 idempotency must
            # never turn a plain re-import into a merge because a human fat-fingered
            # a provenance string — and, now that OpenAlex records on import, a
            # spurious merge would write a sidecar on re-import, breaking P3's
            # "re-running leaves the filesystem unchanged".
            if _same_source(imported_from, self.source_name) or \
                    self.identity_key not in _CROSS_SOURCE_KINDS:
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

    def _find_candidate(self, parsed, index: _DirIndex, kb_root: Path) -> CandidateMatch | None:
        """A title+author+year candidate for an imported paper, or None (#75).

        Pure: reads the ``sources/`` index and the candidate ledger, writes nothing.
        Runs in both ``plan`` and ``write`` (so ``--dry-run`` previews a candidate);
        only ``write`` records it, in :meth:`_record_candidate`.

        Fails closed at every step. A writer that does not opt in
        (:attr:`surfaces_candidates`) never proposes one; an incoming paper with no
        first-author surname or no year cannot clear the gate; a pair already in the
        ledger in any state is suppressed; and a corrupt/unreadable ledger surfaces
        nothing rather than acting on an unknown state or crashing the import.

        The gate (surname + year agreement) is applied first so title Jaccard is
        computed only on the surname/year-agreeing subset — the scan is bounded by
        that subset, not the whole directory. On a tie the lowest existing identity
        wins, so a batch's ``--porcelain`` output is reproducible.
        """
        if not self.surfaces_candidates or not index.match_rows:
            return None
        first_author, year_raw, title = self.slug_fields(parsed)
        if not surname(first_author):
            return None
        year = int(year_raw) if year_raw.strip().isdigit() else None
        if year is None:
            return None
        incoming_match = MatchInput(first_author, year, title)
        incoming_identity = (self.source_name, self.identity_of(parsed))

        best: tuple[float, _MatchRow] | None = None
        for row in index.match_rows:
            if row.identity == incoming_identity:
                continue  # never propose a paper against itself
            score = score_pair(incoming_match, row.match)
            if score is None or score < TITLE_SIMILARITY_THRESHOLD:
                continue
            if best is None or score > best[0] or (
                    score == best[0] and row.identity < best[1].identity):
                best = (score, row)
        if best is None:
            return None
        score, row = best

        try:
            ledger = read_candidates(candidates_path(kb_root))
        except (MergeCandidatesError, OSError) as exc:
            # A corrupt ledger is human-fixable. Do not crash the batch, and do not
            # act on an unknown state — a pair a human already rejected must not be
            # re-proposed. But do not swallow it either: the operator would import a
            # paper with the fallback silently disabled and never learn it.
            self.candidate_ledger_error = str(exc)
            return None
        if ledger.has_pair(incoming_identity, row.identity):
            return None
        return CandidateMatch(
            incoming=incoming_identity, existing=row.identity,
            existing_path=row.path, score=score)

    def _record_candidate(self, kb_root: Path, candidate: CandidateMatch,
                          recorded_at: str) -> None:
        """Record a surfaced pair as ``pending`` in the KB ledger. Write mode only.

        Called from :meth:`write` **after** the ``.md`` is written — the ledger is
        not a skip key, so a pair recorded before a failed ``.md`` write would point
        at a file that never appeared. Idempotent: a pair already present in any
        state is a no-op (a human's ``rejected`` is never overwritten). A
        corrupt/unreadable ledger is left untouched — never erased, never a crash;
        the import already succeeded and the pair simply is not recorded.
        """
        path = candidates_path(kb_root)
        try:
            ledger = read_candidates(path)
        except (MergeCandidatesError, OSError) as exc:
            self.candidate_ledger_error = str(exc)
            return
        if ledger.has_pair(candidate.incoming, candidate.existing):
            return
        add_candidate(ledger, CandidatePair.create(
            candidate.incoming, candidate.existing,
            state=STATE_PENDING, score=candidate.score, recorded_at=recorded_at))
        try:
            write_candidates(path, ledger)
        except (MergeCandidatesError, OSError) as exc:
            self.candidate_ledger_error = str(exc)
            return

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

        reserved = self._reserve(parsed, sources_dir, index)
        # A candidate is only meaningful for a NEW file (an imported outcome): a
        # skip/merge already matched an exact identifier. The matcher runs before the
        # reservation pollutes ``match_rows`` (it never adds to it), so it compares
        # only against files already on disk.
        candidate = self._find_candidate(parsed, index, Path(target))
        if candidate is not None:
            reserved = replace(reserved, candidate=candidate)
        return reserved

    def plan(self, parsed, target: Path | str) -> WriteResult:
        """Predict :meth:`write`'s outcome without creating any file (dry run).

        Safe to interleave with :meth:`write` on the same instance — plan uses a
        separate reservation index, so it never makes a later write() skip.
        """
        return self._resolve(parsed, target, "plan")

    # -- §7.3 provenance sidecar (shared; only opt-in writers reach the disk) --
    def _identity_fields(self, record: SourceRecord) -> dict:
        """The subset of *record* an import may not revise (see :attr:`_IDENTIFYING_FIELDS`).

        ``imported_at`` is deliberately absent: it records when factlog first saw
        the provenance, not a fact about the paper, and the CLI stamps a fresh one
        every run — comparing it would make a plain re-import look like a conflict.
        With the default empty tuple this is ``{}`` for every record, so no drift
        is ever a divergence and :meth:`_divergence` is never reached.
        """
        return {name: record.fields.get(name) for name in self._IDENTIFYING_FIELDS}

    def _provenance_record(self, parsed, imported_at: str) -> SourceRecord:
        """This source's contribution to a paper's provenance ledger.

        Overridden by every writer that opts into §7.3 merging
        (:attr:`merges_cross_source`). The base raises, because a non-merger never
        records and so never needs to build one.
        """
        raise NotImplementedError

    def _divergence(self, existing: SourceRecord, incoming: SourceRecord) -> str:
        """Why an import refuses to revise the ledger. Generic, refresh-agnostic.

        Reached only when :attr:`_IDENTIFYING_FIELDS` is non-empty and one of those
        fields moved, so a writer keeping the empty default never uses it. A writer
        with a refresh command overrides this to name it (arXiv points at
        ``arxiv-check-versions``); the default must **not** name any command, so it
        never sends a user to one that does not apply to their integration.
        """
        return (
            f"provenance ledger already records a different {existing.type} entry "
            f"for id {existing.id!r}; an import may not revise it"
        )

    def _upsert_sidecar(
        self, parsed, decision: WriteResult, imported_at: str, target: Path
    ) -> WriteResult:
        """Read-modify-write this source's record into ``sidecar_path(decision.path, target)``.

        The shared mechanism behind both :meth:`_merge` (fold into an *existing*
        original's ledger) and :meth:`_record` (write a *new* original's own
        ledger). Both derive the sidecar from ``decision.path`` and add the very
        same :meth:`_provenance_record`, so the disk operation is identical; only
        which file ``decision.path`` names differs, and that is the caller's
        concern. The ``.md`` is never opened here — only its sidecar — so the
        original stays byte- and mtime-immutable (P4). The disk ledger is re-read
        every call (never cached), so a prior write is respected.

        Idempotence (P3) is judged on :meth:`_identity_fields`, never the import
        clock: a record already present with the same identifying fields is a
        no-op that keeps the *first* import's timestamp, so the ledger stays
        byte-identical across re-imports that carry a fresh ``imported_at``. With
        an empty :attr:`_IDENTIFYING_FIELDS` that comparison is always equal, so
        the incumbent record wins and all drift is absorbed silently.

        A divergence in an identifying field is a change an import has no authority
        to make: it becomes a per-id ``error`` pointing at the refresh path via
        :meth:`_divergence`. Every failure — a corrupt/unreadable sidecar, a write
        fault — is a per-id ``error``, never a batch crash: one paper's problem
        does not stop the imports queued behind it.
        """
        sidecar = sidecar_path(decision.path, target)
        record = self._provenance_record(parsed, imported_at)
        try:
            provenance = read_provenance(sidecar)
        except (ProvenanceError, OSError) as exc:
            # ProvenanceError: the ledger is malformed. OSError: the path cannot be
            # read at all (a permission fault, or ``source-provenance`` occupied by
            # a plain file so the sidecar cannot exist under it). Either way it is
            # this one paper's problem — a per-id error, never a batch crash.
            return WriteResult(
                decision.path, "error",
                f"provenance sidecar is unreadable ({exc}); repair or delete "
                f"{sidecar.name} and re-import",
            )

        existing = next((r for r in provenance.records if r.key == record.key), None)
        if existing is not None:
            if self._identity_fields(existing) == self._identity_fields(record):
                return decision  # already recorded; keep the first timestamp
            return WriteResult(decision.path, "error", self._divergence(existing, record))

        try:
            add_source(provenance, record)
            write_provenance(sidecar, provenance)
        except ProvenanceConflict:  # pragma: no cover - guarded by the check above
            return WriteResult(
                decision.path, "error",
                f"provenance ledger diverged for {record.type} id {record.id!r}",
            )
        except (ProvenanceError, OSError) as exc:
            return WriteResult(
                decision.path, "error", f"cannot write {sidecar.name}: {exc}",
            )
        return decision

    def _merge(
        self, parsed, decision: WriteResult, imported_at: str, target: Path
    ) -> WriteResult:
        """Fold *parsed* into the **existing** original's provenance sidecar (§7.3).

        Gated by :attr:`merges_cross_source`. A writer that does not opt in returns
        the ``merged`` decision unchanged and touches nothing — and in fact never
        even produces a ``merged`` status (see :meth:`_cross_source`), so the guard
        is belt-and-braces. Reached only from :meth:`write`, never :meth:`plan`, so
        ``--dry-run`` stays side-effect-free. ``decision.path`` is the original
        another database already wrote; this appends this source's view of the same
        paper to its ledger via :meth:`_upsert_sidecar`, never a second ``.md``.
        """
        if not self.merges_cross_source:
            return decision
        return self._upsert_sidecar(parsed, decision, imported_at, target)

    def _record(
        self, parsed, decision: WriteResult, imported_at: str, target: Path
    ) -> WriteResult:
        """Write this source's OWN record for a **new** original (#72).

        Gated by the SAME :attr:`merges_cross_source` flag as :meth:`_merge`, and
        that coupling is deliberate, not incidental. Splitting the two would allow
        an order-dependent ledger: ``_record`` on with ``_merge`` off makes an
        arXiv-first import of a paper leave ``{arxiv}`` while an OpenAlex-first
        import of the *same* paper leaves ``{openalex, arxiv}``. Keeping one flag
        for both is what guarantees the two import orders converge on the same
        record set (#73). A non-merger (Zotero) returns the ``imported`` decision
        unchanged and writes no sidecar, so it stays byte-identical.

        ``decision.path`` is the file this writer is about to create — its final,
        suffix-resolved name (a ``-2`` collision lands the sidecar beside the right
        ``.md`` because :func:`sidecar_path` derives from that name). Called from
        :meth:`write` *before* the ``.md`` is written, so a sidecar failure aborts
        the import with no orphaned original.

        **A sidecar already at that path is stale and is replaced, never appended
        to** (#72 risk 3). The ``.md`` does not exist yet — that is precisely why
        this outcome is ``imported`` and not ``skipped`` — so no ledger for *this*
        original can legitimately be there. What can be there is a deleted source's
        ledger whose slug this paper now reuses; appending it would make the new
        original's ledger assert it came from a source it never had, an audit
        ledger that lies. Replacement also makes a retry after a failed ``.md``
        write converge byte-for-byte when it carries the same batch ``imported_at``.
        """
        if not self.merges_cross_source:
            return decision
        record = self._provenance_record(parsed, imported_at)
        sidecar = sidecar_path(decision.path, target)
        fresh = Provenance()
        add_source(fresh, record)
        try:
            write_provenance(sidecar, fresh)
        except (ProvenanceError, OSError) as exc:
            return WriteResult(decision.path, "error", f"cannot write {sidecar.name}: {exc}")
        return decision

    def write(self, parsed, target: Path | str, imported_at: str = "") -> WriteResult:
        """Write one source file under ``<target>/sources/`` and report the outcome."""
        decision = self._resolve(parsed, target, "write")
        if decision.status == "merged":
            # The existing original stays byte- and mtime-immutable (P4); the only
            # write is to its sidecar, and only writers that opt in do it.
            return self._merge(parsed, decision, imported_at, Path(target))
        if decision.status != "imported":
            return decision
        # Sidecar FIRST, original LAST. The ``.md`` is the P3 skip key: once it
        # exists a re-import is ``skipped`` before any write (see :meth:`_duplicate`),
        # so the file must not appear until its ledger already does. If the sidecar
        # write fails, ``_record`` returns an ``error`` and we create no ``.md`` —
        # nothing is orphaned, and a retry re-runs cleanly. The reverse order would
        # leave an orphaned ``.md`` whose existence permanently suppresses the
        # ledger, since the original is never rewritten (P4) and the re-import skips
        # (#72, risk 2). ``_record`` is a no-op unless a writer opts in via
        # :attr:`merges_cross_source`, so a Zotero import writes the ``.md``
        # exactly as before; arXiv and OpenAlex write their ledger first (#73).
        recorded = self._record(parsed, decision, imported_at, Path(target))
        if recorded.status != "imported":
            return recorded
        sources_dir = Path(target) / "sources"
        sources_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(decision.path, self.render(parsed, imported_at))
        # The candidate ledger is written LAST — after the ``.md`` — because it is
        # not a skip key: a pair recorded before a failed ``.md`` write would
        # reference a file that never appeared. It never touches the ``.md`` (P4).
        if decision.candidate is not None:
            self._record_candidate(Path(target), decision.candidate, imported_at)
        return decision
