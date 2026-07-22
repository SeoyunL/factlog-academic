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

from factlog.export_types import (
    COLLECTION,
    INFORMAL,
    ISSUER,
    PERIODICAL,
    SCHOOL,
    SERIES,
    resolve_source_type,
    should_promote_to_journal_type,
    venue_role,
)

_LIST_ITEM_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_KV_RE = re.compile(r"^([A-Za-z0-9_]+):\s*(.*)$")

# Work type -> BibTeX entry type; anything else falls back to @misc. Keyed by
# both vocabularies `resolve_source_type` can return: Zotero's camelCase
# itemType and OpenAlex's hyphenated work type. The two never collide — where
# they share a spelling ("book", "report", "preprint") they also share a meaning.
_ENTRY_TYPES = {
    # Zotero itemType
    "journalArticle": "article",
    "magazineArticle": "article",
    "newspaperArticle": "article",
    "conferencePaper": "inproceedings",
    "book": "book",
    "bookSection": "incollection",
    "encyclopediaArticle": "incollection",
    "dictionaryEntry": "incollection",
    "report": "techreport",
    "thesis": "phdthesis",
    "preprint": "misc",
    # OpenAlex work type (a subset of api_client.WORK_TYPES; see
    # tests/unit/test_export_entry_types.py, which pins that containment)
    "article": "article",
    "review": "article",
    "book-review": "article",
    "letter": "article",
    "editorial": "article",
    "erratum": "article",
    "retraction": "article",
    "data-paper": "article",
    "conference-paper": "inproceedings",
    "book-chapter": "incollection",
    "book-section": "incollection",
    "reference-entry": "incollection",
    "dissertation": "phdthesis",
    "report-component": "techreport",
    # Standard BibTeX has no @dataset/@software (those are biblatex), so these
    # stay @misc — but CSL does have them, hence no matching _CSL_TYPES value.
    "dataset": "misc",
    "software": "misc",
}

# Venue role -> the standard-BibTeX field that holds it. Standard BibTeX scopes
# venue fields tightly: `journal` is defined for @article ALONE, `booktitle` for
# @inproceedings/@incollection, `institution` for @techreport, `school` for
# @phdthesis, `howpublished` for @misc. Emitting `journal` on any other entry
# type is the same defect as the @misc+journal pairing this fixes — the field is
# dropped, and @inproceedings/@incollection additionally warn on the now-empty
# `booktitle` they require. `SERIES` goes to `series`, which @book defines: a
# whole book has no containing venue, but a venue value on one names the series
# it belongs to, and every role here must *move* the value rather than discard
# it — a misfiled venue can be recovered by hand, a dropped one cannot.
_VENUE_FIELDS = {
    PERIODICAL: "journal",
    COLLECTION: "booktitle",
    ISSUER: "institution",
    SCHOOL: "school",
    INFORMAL: "howpublished",
    SERIES: "series",
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


# How much to pull per read while looking for the closing fence, and the point at
# which an *unclosed* front matter stops being read. The cap bounds only the
# pathological file (an opening ``---`` whose fence is never closed); a well-formed
# block stops at its own fence, however long it is.
_FRONT_MATTER_CHUNK_CHARS = 8192
_FRONT_MATTER_MAX_CHARS = 1 << 20


def read_front_matter(path: Path | str) -> dict:
    """The source's YAML front matter as a dict, or ``{}``.

    Reads to the block's **closing fence**, not to a fixed byte count. A fixed
    4096-byte window truncated the block mid-way and silently dropped every key
    past it: the arXiv writer emits one long ``authors:`` line ahead of ``year``/
    ``journal``/``preprint``, so a large collaboration (200 authors, 7903-byte
    block) kept only ``arxiv_id``/``arxiv_version``/``authors``/``title`` and
    exported as a bare ``@misc`` with a title and nothing else — no author, year,
    venue, DOI, nor the type key that makes it a preprint (#395).

    The window was never a read budget: the old code called ``read_text()`` on the
    whole file and only *then* sliced, so it paid for every byte of the body and
    still lost the tail of the front matter. Stopping at the fence — and returning
    early when there is no opening fence — reads strictly less than that.

    ``OSError`` yields ``{}`` so an unreadable file is reported as "no front
    matter" (``cmd_export`` skips it) rather than aborting the export.
    """
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            head = fh.read(_FRONT_MATTER_CHUNK_CHARS)
            if not head.startswith("---"):
                # No opening fence: nothing to find, and no reason to read the body.
                return {}
            # Re-scan the accumulated text each pass, so a fence straddling a chunk
            # boundary is still found.
            while "\n---" not in head[3:] and len(head) < _FRONT_MATTER_MAX_CHARS:
                chunk = fh.read(_FRONT_MATTER_CHUNK_CHARS)
                if not chunk:
                    break
                head += chunk
    except OSError:
        return {}
    return parse_front_matter(head)


def is_annotation_source(fm: dict) -> bool:
    """True for a companion ``<stem>-notes.md`` (exported separately, if at all)."""
    return fm.get("source_kind") == "annotations"


def _entry_type(fm: dict) -> str:
    source_type = resolve_source_type(fm)
    if should_promote_to_journal_type(fm, source_type):
        # PubMed declares no type at all; naming a journal is the only evidence
        # its front matter gives that the record is a journal article (#384).
        return "article"
    return _ENTRY_TYPES.get(source_type, "misc") if source_type else "misc"


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
    entry_type = _entry_type(fm)
    venue_key = _VENUE_FIELDS[venue_role(fm)]
    for fm_key, bib_key in (("title", "title"), ("year", "year"),
                            ("journal", venue_key), ("doi", "doi")):
        value = fm.get(fm_key)
        if value and bib_key:
            fields.append((bib_key, str(value)))
    if fm.get("pmid"):
        fields.append(("note", f"PMID: {fm['pmid']}"))

    lines = [f"@{entry_type}{{{safe_cite_key(cite_key)},"]
    for name, value in fields:
        lines.append(f"  {name} = {{{_esc(value)}}},")
    lines.append("}")
    return "\n".join(lines) + "\n"
