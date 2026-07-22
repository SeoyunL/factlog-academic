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
from factlog.front_matter_scan import front_matter_block

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


def _parse_block(block: str) -> dict:
    """Parse the *inside* of a front-matter block — fences already stripped."""
    fm: dict = {}
    for line in block.splitlines():
        match = _KV_RE.match(line.strip())
        if match:
            fm[match.group(1)] = _parse_value(match.group(2))
    return fm


def parse_front_matter(text: str) -> dict:
    """Parse the leading ``---`` fenced YAML block into a dict ({} if none).

    Fails closed on a block whose closing fence is missing, the same rule
    :func:`factlog.front_matter_scan.front_matter_block` applies to a file on disk
    — an unclosed block has no knowable extent, so its ``key:`` lines cannot be
    told from body lines. This once differed: the block ran to the end of whatever
    it was given, which is how a reading note quoting a paper came to export as a
    citation of that paper (#419). Keeping one rule is the point; a caller reaching
    for ``parse_front_matter(path.read_text())`` must not get the lenient answer
    that :func:`read_front_matter` no longer gives.
    """
    if not text.startswith("---"):
        return {}
    rest = text[3:]
    end = rest.find("\n---")
    return {} if end == -1 else _parse_block(rest[:end])


def read_front_matter(path: Path | str) -> dict:
    """The source's YAML front matter as a dict, or ``{}``.

    Locating the block is :func:`factlog.front_matter_scan.front_matter_block`'s
    job, shared with the de-duplication reader since #419; this adds only the
    parse. That module's docstring carries the evidence for the read extent and for
    failing closed on an unclosed block — including the export this one used to
    emit from a reading note's body.

    ``{}`` here means "no front matter", and ``cmd_export`` reports the file as
    skipped rather than citing it. Every way the scan can come up empty — an
    unreadable file, undecodable bytes, no opening fence, no closing fence — lands
    on that same, visible outcome instead of aborting the run.

    **A genuinely closed block longer than the cap reads as ``{}`` too**, and that
    is a change on this path: the exporter used to cite such a record from whatever
    truncated keys it had managed to read, and now drops it and reports it skipped.
    This is the cap behaving as ``front_matter_scan`` documents — past it a real
    fence and a missing one are indistinguishable — but that was written there as a
    standing property of the de-duplication reader, and it is new here. Reaching it
    takes a block over ``FRONT_MATTER_MAX_CHARS`` — **characters, not bytes**: the
    read is a text handle, so ``fh.read`` yields characters and ``len(head)`` counts
    them, which is what the ``_CHARS`` in the name says. The equivalent author count
    is therefore not a constant but ``cap / chars-per-author``, measured at 22,795
    authors for 46-character names, 29,959 for 35-character, 65,535 for
    16-character. Those three are ASCII, where characters and bytes coincide; a
    35-character CJK name costs 81 bytes and still thresholds at 31,775 authors,
    where a byte reading would predict 12,945. A block can exceed the cap threefold
    in bytes and still be cited. ``front_matter_scan``'s cap comment illustrates the
    same curve at ~40 characters per author; every point on it is far past the few
    thousand the largest real collaborations run to. Dropping the record whole is
    the better failure than citing a fraction of it silently; the cap is the lever
    if a real corpus ever gets there.
    """
    block = front_matter_block(path)
    return {} if block is None else _parse_block(block)


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
