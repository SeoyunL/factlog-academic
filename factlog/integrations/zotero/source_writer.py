#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Write a parsed Zotero item into a factlog ``sources/<slug>.md`` original.

Consumes the standard dict from :mod:`factlog.integrations.zotero.item_parser`
and produces one markdown source file carrying a YAML provenance front matter
plus a readable body (abstract + the original Zotero/DOI/PMID pointers).

Three invariants matter:

* **P4 (original immutability).** An existing file is never overwritten or
  deleted. A fresh, globally-unique filename is chosen instead, and the write is
  atomic (temp file + ``os.replace``).
* **P3 (idempotent re-import).** If a source already carries the same
  ``zotero_key``, re-import skips it (when ``skip_duplicates``), so re-running an
  import leaves the filesystem unchanged.
* **Global-unique slugs (spec §12).** When a base slug is already claimed by a
  *different* item, a ``-2``/``-3`` suffix is appended.

``imported_at`` is injected by the caller rather than read from a clock here, so
the writer is pure and unit-testable and the CLI controls the (single, batch)
timestamp. Because suffix assignment depends on the directory's current state,
the caller (CLI) must feed items in a deterministic order (e.g. sorted by
``zotero_key``) for reproducible suffixes.

factlog never machine-parses this front matter — sources are read as text by the
extraction step — so a minimal hand-rolled YAML emitter (no PyYAML dependency)
is sufficient; it double-quotes every string with the two escapes YAML needs.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from factlog.common import slugify

# Byte budgets for the filename (most filesystems cap a name at 255 bytes).
# Author and title are individually bounded, then the whole stem is capped with
# headroom left for a "-NN" uniqueness suffix and the ".md" extension.
_AUTHOR_SLUG_MAX_BYTES = 64
_TITLE_SLUG_MAX_BYTES = 80
_STEM_MAX_BYTES = 190

# How many leading bytes of a source file to scan for its front-matter key.
_FRONT_MATTER_SCAN_BYTES = 2048

_FRONT_MATTER_KEY_RE = re.compile(r'^zotero_key:\s*"?([^"\n]+?)"?\s*$', re.MULTILINE)

# C0 control characters (except the whitespace we escape explicitly) that must
# be rendered as \xNN inside a double-quoted YAML scalar.
_YAML_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a single :meth:`SourceWriter.write` call."""

    path: Path | None
    status: str  # "imported" | "skipped" | "error"
    reason: str = ""


def _yaml_str(value: str) -> str:
    """Double-quote a scalar with the escapes a double-quoted YAML string needs.

    Backslash and quote are escaped, whitespace controls become \\n/\\r/\\t, and
    any remaining C0 control char becomes \\xNN — so an embedded newline/tab in a
    Zotero title or journal name cannot break the front matter onto a stray line.
    """
    out = []
    for ch in value:
        if ch in _YAML_ESCAPES:
            out.append(_YAML_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _yaml_list(items: list[str]) -> str:
    return "[" + ", ".join(_yaml_str(item) for item in items) + "]"


def _byte_trunc(slug: str, max_bytes: int) -> str:
    """Trim a slug to <= max_bytes UTF-8 bytes, multibyte-safe, on a '-' edge."""
    encoded = slug.encode("utf-8")
    if len(encoded) <= max_bytes:
        return slug
    cut = encoded[:max_bytes].decode("utf-8", "ignore")
    if "-" in cut:
        cut = cut.rsplit("-", 1)[0]
    return cut.strip("-") or cut


def _slug_or(raw: str, fallback: str, max_bytes: int) -> str:
    """slugify a raw field, byte-capped; use fallback only when raw is blank.

    Branching on the *raw* value (not on slugify's "item" empty-fallback) avoids
    forcing a legitimate title like "Item Response Theory" to the fallback.
    """
    if not raw.strip():
        return fallback
    return _byte_trunc(slugify(raw), max_bytes)


def _first_author_token(parsed: dict) -> str:
    authors = parsed.get("authors") or []
    if authors:
        first = authors[0]
        name = first.get("last") or first.get("name") or ""
        if name.strip():
            return _byte_trunc(slugify(name), _AUTHOR_SLUG_MAX_BYTES)
    return "anonymous"


def read_zotero_key(path: Path) -> str:
    """Return the ``zotero_key`` recorded in a source file's front matter, or "".

    Only the leading front-matter block (between the opening ``---`` and its
    closing ``---``) is consulted, and only the first bytes are read — so a plain
    user source, or the literal text ``zotero_key:`` in a body, is not mistaken
    for a prior Zotero import.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            head = fh.read(_FRONT_MATTER_SCAN_BYTES)
    except OSError:
        return ""
    if not head.startswith("---"):
        return ""
    # Bound the search to the front-matter block: from after the opening fence to
    # the closing fence (or the scanned head if the fence is beyond it).
    rest = head[3:]
    end = rest.find("\n---")
    block = rest if end == -1 else rest[:end]
    match = _FRONT_MATTER_KEY_RE.search(block)
    return match.group(1).strip() if match else ""


class SourceWriter:
    """Render parsed Zotero items into ``sources/`` markdown originals."""

    def __init__(self, skip_duplicates: bool = True, include_abstract: bool = True):
        self.skip_duplicates = skip_duplicates
        self.include_abstract = include_abstract
        # Per-(directory, mode) index, scanned once then kept current as we
        # reserve, so a batch is O(files + N) rather than O(N x files). Keyed by
        # (resolved sources dir, mode) where mode is "write" or "plan": a dry-run
        # plan reserves in its OWN index so it can still predict collision
        # suffixes across a batch WITHOUT polluting the write path (a plan() must
        # never make a later write() on the same instance skip). Value is
        # ({claimed filenames}, {zotero_key: path}).
        self._dir_index: dict[tuple[str, str], tuple[set[str], dict[str, Path]]] = {}

    def generate_slug(self, parsed: dict) -> str:
        """Base filename ``{author}-{year}-{title}.md`` (no uniqueness suffix).

        Missing pieces degrade gracefully: no author -> ``anonymous``, no year ->
        ``n-d`` in the year slot, no title -> ``untitled``. Each component and the
        whole stem are byte-capped so a long/non-ASCII field cannot overflow the
        filesystem name limit.
        """
        author = _first_author_token(parsed)
        raw_year = parsed.get("year") or ""
        year = slugify(raw_year) if raw_year.strip() else "n-d"
        title = _slug_or(parsed.get("title") or "", "untitled", _TITLE_SLUG_MAX_BYTES)
        stem = _byte_trunc(f"{author}-{year}-{title}", _STEM_MAX_BYTES)
        return f"{stem}.md"

    def _index(self, sources_dir: Path, mode: str) -> tuple[set[str], dict[str, Path]]:
        key = (str(sources_dir.resolve()), mode)
        cached = self._dir_index.get(key)
        if cached is None:
            claimed: set[str] = set()
            by_key: dict[str, Path] = {}
            if sources_dir.is_dir():
                for path in sorted(sources_dir.glob("*.md")):
                    claimed.add(path.name)
                    zkey = read_zotero_key(path)
                    if zkey and zkey not in by_key:
                        by_key[zkey] = path
            cached = (claimed, by_key)
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

    def render(self, parsed: dict, imported_at: str = "") -> str:
        """The full markdown text (front matter + body) for a parsed item."""
        return self._front_matter(parsed, imported_at) + self._body(parsed)

    def _front_matter(self, parsed: dict, imported_at: str) -> str:
        lines = ["---"]
        lines.append(f"zotero_key: {_yaml_str(parsed.get('zotero_key', ''))}")
        if parsed.get("item_type"):
            lines.append(f"item_type: {_yaml_str(parsed['item_type'])}")
        lines.append(f"title: {_yaml_str(parsed.get('title', ''))}")
        authors = [a.get("name", "") for a in (parsed.get("authors") or []) if a.get("name")]
        if authors:
            lines.append(f"authors: {_yaml_list(authors)}")
        if parsed.get("year"):
            lines.append(f"year: {_yaml_str(parsed['year'])}")
        if parsed.get("journal"):
            lines.append(f"journal: {_yaml_str(parsed['journal'])}")
        if parsed.get("doi"):
            lines.append(f"doi: {_yaml_str(parsed['doi'])}")
        if parsed.get("pmid"):
            lines.append(f"pmid: {_yaml_str(parsed['pmid'])}")
        if parsed.get("tags"):
            lines.append(f"tags: {_yaml_list(parsed['tags'])}")
        lines.append("imported_from: zotero")
        if imported_at:
            lines.append(f"imported_at: {_yaml_str(imported_at)}")
        if parsed.get("retracted"):
            lines.append("retracted: true")
        lines.append("---")
        return "\n".join(lines) + "\n"

    def _body(self, parsed: dict) -> str:
        title = parsed.get("title") or "Untitled"
        parts = [f"\n# {title}\n"]
        if self.include_abstract:
            abstract = (parsed.get("abstract") or "").strip()
            parts.append("\n## Abstract\n")
            parts.append(f"\n{abstract or '_No abstract available._'}\n")
        parts.append("\n## Original source\n")
        key = parsed.get("zotero_key", "")
        if key:
            parts.append(f"\n- Zotero item: `zotero://select/library/items/{key}`")
        if parsed.get("doi"):
            parts.append(f"\n- DOI: {parsed['doi']}")
        if parsed.get("pmid"):
            parts.append(f"\n- PMID: {parsed['pmid']}")
        return "".join(parts) + "\n"

    def _resolve(self, parsed: dict, target: Path | str, mode: str) -> WriteResult:
        """Decide the outcome (imported/skipped/error) and reserve the target name.

        Shared by :meth:`write` (mode "write") and :meth:`plan` (mode "plan") so a
        dry run predicts exactly what a real run would do, including collision
        suffixes: an "imported" decision reserves its filename in the *mode's* index
        so the next item in the same batch sees it. The two modes hold separate
        indexes, so a plan() never causes a later write() to skip. No file is
        touched here.

        A missing ``zotero_key`` is an error rather than a write: without an
        identity there is no way to keep re-import idempotent, so a new file would
        proliferate on every run. Every real Zotero item has a key.
        """
        zotero_key = parsed.get("zotero_key", "")
        if not zotero_key:
            return WriteResult(None, "error", "missing zotero_key")

        sources_dir = Path(target) / "sources"
        claimed, by_key = self._index(sources_dir, mode)

        existing = by_key.get(zotero_key)
        if existing is not None and self.skip_duplicates:
            return WriteResult(existing, "skipped", "already imported (zotero_key match)")

        path = self._unique_path(sources_dir, self.generate_slug(parsed), claimed)
        claimed.add(path.name)
        by_key.setdefault(zotero_key, path)
        return WriteResult(path, "imported")

    def plan(self, parsed: dict, target: Path | str) -> WriteResult:
        """Predict :meth:`write`'s outcome without creating any file (dry run).

        Safe to interleave with :meth:`write` on the same instance — plan uses a
        separate reservation index, so it never makes a later write() skip.
        """
        return self._resolve(parsed, target, "plan")

    def write(self, parsed: dict, target: Path | str, imported_at: str = "") -> WriteResult:
        """Write one source file under ``<target>/sources/`` and report the outcome."""
        decision = self._resolve(parsed, target, "write")
        if decision.status != "imported":
            return decision
        sources_dir = Path(target) / "sources"
        sources_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(decision.path, self.render(parsed, imported_at))
        return decision


def _atomic_write(path: Path, text: str) -> None:
    """Write text via a temp file + atomic replace so a crash cannot leave a
    half-written source. The temp file sits in the same dir to keep replace
    atomic; a failed replace unlinks the temp so no stray file lingers."""
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
