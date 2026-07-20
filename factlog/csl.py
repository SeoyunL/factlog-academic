#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Turn a factlog source's front matter into a CSL-JSON item.

CSL-JSON is consumed by Pandoc, Zotero, and Word citation tools, so
`factlog export --csl` complements the BibTeX export for a wider set of writing
workflows. Reuses the front-matter reader from :mod:`factlog.bibtex`; read-only.
"""
from __future__ import annotations

import re

from factlog.bibtex import resolve_source_type

# Work type -> CSL type; anything else falls back to "document". Keyed by both
# vocabularies `resolve_source_type` can return (Zotero itemType and OpenAlex
# work type), mirroring `bibtex._ENTRY_TYPES` entry for entry so the two
# exporters never disagree about what a record is.
_CSL_TYPES = {
    # Zotero itemType
    "journalArticle": "article-journal",
    "conferencePaper": "paper-conference",
    "book": "book",
    "bookSection": "chapter",
    "report": "report",
    "thesis": "thesis",
    "preprint": "article",
    # OpenAlex work type
    "article": "article-journal",
    "review": "article-journal",
    "conference-paper": "paper-conference",
    "book-chapter": "chapter",
    "book-section": "chapter",
    "dissertation": "thesis",
    "report-component": "report",
}

_YEAR_RE = re.compile(r"\d{4}")


def _csl_type(fm: dict) -> str:
    source_type = resolve_source_type(fm)
    csl_type = _CSL_TYPES.get(source_type, "document") if source_type else "document"
    # A record naming a journal is a journal article, even when no key says so —
    # this is the only signal PubMed front matter gives (#384). Unlike BibTeX
    # this promotes "document" only: CSL's "article" (what a preprint maps to)
    # is already a valid pairing with `container-title`.
    if csl_type == "document" and fm.get("journal"):
        return "article-journal"
    return csl_type


def _author(name: str) -> dict:
    """Split a display name into CSL family/given, or a literal for one token.

    factlog writes authors as "Family, Given" (Zotero's two-field creators), which
    splits unambiguously even for a compound surname. A legacy "Family Given"
    (no comma) falls back to a first-space split, and a single token (an
    institution, "et al.") becomes a literal name.
    """
    if ", " in name:
        family, given = name.split(", ", 1)
        if family.strip() and given.strip():
            return {"family": family.strip(), "given": given.strip()}
    parts = name.split(" ", 1)
    if len(parts) == 2 and parts[1].strip():
        return {"family": parts[0], "given": parts[1].strip()}
    return {"literal": name}


def to_csl(fm: dict, item_id: str) -> dict:
    """Render one CSL-JSON item dict from a source's front-matter dict."""
    item: dict = {"id": item_id, "type": _csl_type(fm)}

    title = fm.get("title")
    if title:
        item["title"] = str(title)

    authors = fm.get("authors")
    if isinstance(authors, list) and authors:
        item["author"] = [_author(str(a)) for a in authors]

    year = fm.get("year")
    if year:
        match = _YEAR_RE.search(str(year))
        if match:
            item["issued"] = {"date-parts": [[int(match.group(0))]]}

    journal = fm.get("journal")
    if journal:
        item["container-title"] = str(journal)

    if fm.get("doi"):
        item["DOI"] = str(fm["doi"])
    if fm.get("pmid"):
        item["PMID"] = str(fm["pmid"])
    return item
