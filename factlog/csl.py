#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Turn a factlog source's front matter into a CSL-JSON item.

CSL-JSON is consumed by Pandoc, Zotero, and Word citation tools, so
`factlog export --csl` complements the BibTeX export for a wider set of writing
workflows. Reuses the front-matter reader from :mod:`factlog.bibtex`; read-only.
"""
from __future__ import annotations

import re

# Zotero itemType -> CSL type; anything else falls back to "document".
_CSL_TYPES = {
    "journalArticle": "article-journal",
    "conferencePaper": "paper-conference",
    "book": "book",
    "bookSection": "chapter",
    "report": "report",
    "thesis": "thesis",
    "preprint": "article",
}

_YEAR_RE = re.compile(r"\d{4}")


def _csl_type(item_type: object) -> str:
    return _CSL_TYPES.get(item_type, "document") if isinstance(item_type, str) else "document"


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
    item: dict = {"id": item_id, "type": _csl_type(fm.get("item_type"))}

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
