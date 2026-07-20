#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Turn a factlog source's front matter into a BibTeX entry.

`factlog export --bibtex` reads the provenance factlog already records in each
source's YAML front matter (written by the Zotero import) and emits BibTeX so a
researcher can cite factlog-tracked sources in LaTeX/Word. Read-only, no new
dependency — a small parser handles the simple YAML subset factlog writes.
"""
from __future__ import annotations

import re
from pathlib import Path

_LIST_ITEM_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_KV_RE = re.compile(r"^([A-Za-z0-9_]+):\s*(.*)$")

# Work type -> BibTeX entry type; anything else falls back to @misc. Keyed by
# both vocabularies `resolve_source_type` can return: Zotero's camelCase
# itemType and OpenAlex's hyphenated work type. The two never collide — where
# they share a spelling ("book", "report", "preprint") they also share a meaning.
_ENTRY_TYPES = {
    # Zotero itemType
    "journalArticle": "article",
    "conferencePaper": "inproceedings",
    "book": "book",
    "bookSection": "incollection",
    "report": "techreport",
    "thesis": "phdthesis",
    "preprint": "misc",
    # OpenAlex work type
    "article": "article",
    "review": "article",
    "conference-paper": "inproceedings",
    "book-chapter": "incollection",
    "book-section": "incollection",
    "dissertation": "phdthesis",
    "report-component": "techreport",
}

# Char-by-char LaTeX escaping (one pass, so inserted braces are not re-escaped).
_ESC = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "{": r"\{",
    "}": r"\}",
}


_SIMPLE_UNESCAPE = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _unescape(value: str) -> str:
    """Reverse the YAML scalar escaping factlog writes (\\n, \\t, \\", \\xNN)."""
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt in _SIMPLE_UNESCAPE:
                out.append(_SIMPLE_UNESCAPE[nxt])
                i += 2
                continue
            if nxt == "x" and i + 3 < len(value):
                try:
                    out.append(chr(int(value[i + 2 : i + 4], 16)))
                    i += 4
                    continue
                except ValueError:
                    pass
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_value(raw: str):
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return [_unescape(m) for m in _LIST_ITEM_RE.findall(raw)]
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        return _unescape(raw[1:-1])
    if raw in ("true", "false"):
        return raw == "true"
    return raw


def parse_front_matter(text: str) -> dict:
    """Parse the leading ``---`` fenced YAML block into a dict ({} if none)."""
    if not text.startswith("---"):
        return {}
    rest = text[3:]
    end = rest.find("\n---")
    block = rest if end == -1 else rest[:end]
    fm: dict = {}
    for line in block.splitlines():
        match = _KV_RE.match(line.strip())
        if match:
            fm[match.group(1)] = _parse_value(match.group(2))
    return fm


def read_front_matter(path: Path | str) -> dict:
    try:
        head = Path(path).read_text(encoding="utf-8")[:4096]
    except OSError:
        return {}
    return parse_front_matter(head)


def is_annotation_source(fm: dict) -> bool:
    """True for a companion ``<stem>-notes.md`` (exported separately, if at all)."""
    return fm.get("source_kind") == "annotations"


def resolve_source_type(fm: dict) -> str | None:
    """Which front-matter key carries this record's work type, and what it says.

    Each integration records the type under a different key, so reading only
    Zotero's ``item_type`` dropped every OpenAlex/arXiv/PubMed record to the
    exporter's default type (#384). Probed most-specific first:

    ==========  ====================================  ===================
    source      key                                   vocabulary
    ==========  ====================================  ===================
    Zotero      ``item_type``                         ``journalArticle``
    OpenAlex    ``type``                              ``conference-paper``
    arXiv       ``preprint: true``                    (implies a preprint)
    PubMed      *none* — inferred by the caller from ``journal``
    ==========  ====================================  ===================

    ``item_type`` stays first so a Zotero-only KB exports exactly as before.
    The arXiv flag is checked before any ``journal``-based inference because an
    arXiv deposit stays a preprint even when ``journal`` records where the work
    was later published; callers apply the ``journal`` fallback themselves,
    since what it should promote to is a per-format decision.

    Returns None when no key answers — the caller picks its own default.
    """
    for key in ("item_type", "type"):
        value = fm.get(key)
        if isinstance(value, str) and value.strip():
            return value
    if fm.get("preprint") is True:
        return "preprint"
    return None


def _entry_type(fm: dict) -> str:
    source_type = resolve_source_type(fm)
    entry = _ENTRY_TYPES.get(source_type, "misc") if source_type else "misc"
    # Standard BibTeX's @misc has no `journal` field, so biber/BibTeX drops it
    # with a warning. A record that names a journal was published in one, so
    # cite it as @article rather than emit the invalid pairing (#384). This also
    # types PubMed records, which carry no type key at all.
    if entry == "misc" and fm.get("journal"):
        return "article"
    return entry


def _esc(value: str) -> str:
    return "".join(_ESC.get(ch, ch) for ch in value)


def safe_cite_key(value: str) -> str:
    """A BibTeX-safe citation key: keep ASCII word chars and '-', collapse rest."""
    key = re.sub(r"[^A-Za-z0-9\-]+", "-", value).strip("-")
    return key or "ref"


def to_bibtex(fm: dict, cite_key: str) -> str:
    """Render one BibTeX entry from a source's front-matter dict."""
    fields: list[tuple[str, str]] = []
    authors = fm.get("authors")
    if isinstance(authors, list) and authors:
        fields.append(("author", " and ".join(str(a) for a in authors)))
    for fm_key, bib_key in (("title", "title"), ("year", "year"),
                            ("journal", "journal"), ("doi", "doi")):
        value = fm.get(fm_key)
        if value:
            fields.append((bib_key, str(value)))
    if fm.get("pmid"):
        fields.append(("note", f"PMID: {fm['pmid']}"))

    lines = [f"@{_entry_type(fm)}{{{safe_cite_key(cite_key)},"]
    for name, value in fields:
        lines.append(f"  {name} = {{{_esc(value)}}},")
    lines.append("}")
    return "\n".join(lines) + "\n"
