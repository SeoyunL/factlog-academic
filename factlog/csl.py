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
    PERIODICAL,
    SCHOOL,
    SERIES,
    is_preprint,
    resolve_source_type,
    should_promote_to_journal_type,
    venue_role,
)
from factlog.text_norm import fold_decimal_digits

# Venue role -> CSL variable, resolved from the same `venue_role` judgement the
# BibTeX exporter uses. CSL constrains nothing structurally (any variable may
# sit on any type), so the choice is settled on semantics, with rendering
# measured first to check whether it forces the hand (#384). It does not.
#
# INFORMAL is the contested one. Such a record is typed `article` (a preprint —
# CSL 1.0.2 has no `preprint` type, and #60 forbids retyping a deposit that
# names where it later appeared). Rendered with pandoc 3.10 --citeproc, one
# preprint carrying `Nature 585, 357 (2020)`, venue present (Y) or lost (N):
#
#   variable          chicago  apa  ieee  nature  ama
#   container-title      Y      Y    N      Y      Y     4/5
#   publisher            Y      Y    Y      N      Y     4/5
#
# An exact tie: `container-title` is dropped by IEEE, `publisher` by Nature
# (whose `type="article"` branch never references it). Preprint status ties too
# at 4/5 either way once `genre` is emitted. So rendering does not decide it,
# and the tiebreak is what the value *is*: an arXiv `journal_ref` or a Zotero
# `publicationTitle` is a periodical's name, not a publisher's. `publisher`
# would be a false statement that happens to print; `container-title` is a true
# one that IEEE happens to ignore. This is also what `main` already emitted, so
# it holds CSL output steady while the BibTeX side is corrected.
#
# The BibTeX side keeps `howpublished` (the only venue field `@misc` defines),
# so the two formats deliberately diverge here, exactly as they do for
# dataset/software types. Note this means a pandoc BibTeX->CSL round trip
# yields `publisher`, disagreeing with the CSL we emit directly; the export we
# emit is the accurate one, and a lossy third-party conversion is not a reason
# to make it wrong.
_VENUE_FIELDS = {
    PERIODICAL: "container-title",
    COLLECTION: "container-title",
    ISSUER: "publisher",
    SCHOOL: "publisher",
    INFORMAL: "container-title",
    SERIES: "collection-title",
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

# ASCII-only on purpose. The pattern was `\d{4}`, i.e. the whole Unicode `Nd`
# category, so a full-width `２０２０` matched — and then came out correct anyway,
# because `int()` accepts any `Nd` digit. The value was right by accident: the
# regex admitted a character it never meant to and the parse step happened to
# rescue it. `fold_decimal_digits` now performs that conversion where it can be
# read, and this pattern states what it actually accepts (#399).
#
# Folding rather than rejecting because this is an **export** path: the value is
# already in the KB by the time we see it, and a reader of the exported CSL can do
# nothing about a bad one, so refusing would only drop a year we can read perfectly
# well. `literal_types` refuses these same characters and the two are not in
# conflict — that module guards values *entering* the store as typed literals,
# where the front matter is still editable and a warning is actionable. The fold
# mechanism itself is one Unicode fact shared with the import boundary, so it lives
# in `text_norm` (#410); this comment is the part that is a policy and stays here.
_YEAR_RE = re.compile(r"[0-9]{4}")


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
        match = _YEAR_RE.search(fold_decimal_digits(str(year)))
        if match:
            item["issued"] = {"date-parts": [[int(match.group(0))]]}

    journal = fm.get("journal")
    venue_key = _VENUE_FIELDS[venue_role(fm)]
    if journal and venue_key:
        item[venue_key] = str(journal)

    # CSL 1.0.2 has no `preprint` type, so the status rides in `genre`. Styles
    # that check it render it (APA prints `[Preprint]`, which it otherwise
    # omits); styles that infer it from the type alone are unchanged.
    if is_preprint(fm):
        item["genre"] = "Preprint"

    if fm.get("doi"):
        item["DOI"] = str(fm["doi"])
    if fm.get("pmid"):
        item["PMID"] = str(fm["pmid"])
    return item
