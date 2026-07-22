#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Turn a factlog source's front matter into a CSL-JSON item.

CSL-JSON is consumed by Pandoc, Zotero, and Word citation tools, so
`factlog export --csl` complements the BibTeX export for a wider set of writing
workflows. Read-only. The caller (`factlog export`) supplies the parsed front
matter, read by :mod:`factlog.bibtex`; the work-type judgements this module
shares with the BibTeX exporter live in :mod:`factlog.export_types`.
"""
from __future__ import annotations

import re

from factlog.export_types import (
    COLLECTION,
    INFORMAL,
    ISSUER,
    NO_VENUE,
    PERIODICAL,
    SCHOOL,
    resolve_source_type,
    should_promote_to_journal_type,
    venue_role,
)

# Venue role -> CSL variable, resolved from the same `venue_role` judgement the
# BibTeX exporter uses. CSL constrains nothing structurally (any variable may sit
# on any type), so the choice is settled by what styles actually render (#384).
#
# INFORMAL is the one that looks wrong on paper: the venue is a periodical name,
# and `container-title` is where a periodical name belongs. But an INFORMAL
# record is typed `article` (a preprint — #60 forbids retyping a deposit that
# names where it later appeared), and for a standalone `article` the styles
# disagree about `container-title` while agreeing about `publisher`. Rendered
# with pandoc --citeproc, one preprint carrying `Nature 585, 357 (2020)`:
#
#   style     container-title                     publisher
#   ieee      (venue dropped entirely)            "2020, Nature 585, 357 (2020)"
#   apa       "In Nature 585, 357 (2020)."        "Nature 585, 357 (2020)."
#   chicago   "In Nature 585...  Preprint."       "Preprint, Nature 585, 357..."
#   ama/nature  renders                           renders
#
# So `container-title` reintroduces the very defect #384 fixes (IEEE silently
# loses the venue) and reads as a containment claim the record does not make;
# `publisher` renders everywhere, and the styles phrase it as "Preprint at" /
# "Preprint posted online", which is what the record means. It also agrees with
# the BibTeX side after a round trip, since pandoc reads `howpublished` back as
# `publisher` — a corroboration, not the reason.
_VENUE_FIELDS = {
    PERIODICAL: "container-title",
    COLLECTION: "container-title",
    ISSUER: "publisher",
    SCHOOL: "publisher",
    INFORMAL: "publisher",
    NO_VENUE: "",
}

# Work type -> CSL type; anything else falls back to "document". Keyed by the
# same vocabularies as `bibtex._ENTRY_TYPES` (Zotero itemType and OpenAlex work
# type) and kept key-for-key in step with it, so the two exporters never
# disagree about what a record is. CSL draws finer distinctions than standard
# BibTeX in places (magazine/newspaper, dataset/software), so the values are a
# refinement of the BibTeX ones, never a contradiction.
_CSL_TYPES = {
    # Zotero itemType
    "journalArticle": "article-journal",
    "magazineArticle": "article-magazine",
    "newspaperArticle": "article-newspaper",
    "conferencePaper": "paper-conference",
    "book": "book",
    "bookSection": "chapter",
    "encyclopediaArticle": "entry-encyclopedia",
    "dictionaryEntry": "entry-dictionary",
    "report": "report",
    "thesis": "thesis",
    "preprint": "article",
    # OpenAlex work type
    "article": "article-journal",
    "review": "article-journal",
    "book-review": "article-journal",
    "letter": "article-journal",
    "editorial": "article-journal",
    "erratum": "article-journal",
    "retraction": "article-journal",
    "data-paper": "article-journal",
    "conference-paper": "paper-conference",
    "book-chapter": "chapter",
    "book-section": "chapter",
    "reference-entry": "entry-encyclopedia",
    "dissertation": "thesis",
    "report-component": "report",
    "dataset": "dataset",
    "software": "software",
}

_YEAR_RE = re.compile(r"\d{4}")


def _csl_type(fm: dict) -> str:
    source_type = resolve_source_type(fm)
    if should_promote_to_journal_type(fm, source_type):
        # Same inference as `bibtex._entry_type`, on the same condition, which is
        # why that condition lives in one place (#384).
        return "article-journal"
    return _CSL_TYPES.get(source_type, "document") if source_type else "document"


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
    venue_key = _VENUE_FIELDS[venue_role(fm)]
    if journal and venue_key:
        item[venue_key] = str(journal)

    if fm.get("doi"):
        item["DOI"] = str(fm["doi"])
    if fm.get("pmid"):
        item["PMID"] = str(fm["pmid"])
    return item
