#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Work-type resolution shared by the BibTeX and CSL exporters.

Each integration records a work's type under a different front-matter key, so
an exporter that reads only one key silently defaults every other source (#384).
Deciding *which key answers* is format-neutral — the answer is the same whether
the caller is about to emit `@inproceedings` or `"paper-conference"` — so it
lives here rather than in either exporter, alongside the one other judgement
both exporters must make identically (`should_promote_to_journal_type`).

Mapping the resolved type onto a citation vocabulary is *not* neutral and stays
in each exporter (`bibtex._ENTRY_TYPES`, `csl._CSL_TYPES`).
"""
from __future__ import annotations

# The OpenAlex work type lives under the bare `type` key in front matter, but
# `type` is also the provenance ledger's RESERVED key for the *source* name
# ("openalex"), which is why the ledger keys the work type `work_type` instead
# (#73, openalex/source_writer.py). No producer writes a conflicting front-matter
# `type` today, but the ambiguity is real, so `type` is trusted only on a record
# the OpenAlex writer actually produced.
_OPENALEX_SOURCE = "openalex"


def resolve_source_type(fm: dict) -> str | None:
    """Return this record's declared work type, or None if no key declares one.

    Probed most-specific first:

    ==========  ==========================  ==============================
    source      key                         vocabulary
    ==========  ==========================  ==============================
    Zotero      ``item_type``               Zotero itemType (camelCase)
    OpenAlex    ``type``                    OpenAlex work type (hyphenated)
    arXiv       ``preprint: true``          implies the type ``preprint``
    PubMed      *(none)*                    returns None
    ==========  ==========================  ==============================

    ``item_type`` is probed first so a record carrying both keys keeps the type
    Zotero assigned it. The arXiv flag is probed *last among the keys but still
    before any ``journal`` inference*: an arXiv deposit stays a preprint even
    when ``journal`` records where the work was later published (#60), and
    :func:`should_promote_to_journal_type` upholds that by refusing to infer a
    type for any record that already declared one.

    Returns None for PubMed, whose front matter carries no type key at all.
    Callers supply their own default for None, optionally after consulting
    :func:`should_promote_to_journal_type`.
    """
    item_type = fm.get("item_type")
    if isinstance(item_type, str) and item_type.strip():
        return item_type
    work_type = fm.get("type")
    if (isinstance(work_type, str) and work_type.strip()
            and fm.get("imported_from") == _OPENALEX_SOURCE):
        return work_type
    if fm.get("preprint") is True:
        return "preprint"
    return None


def should_promote_to_journal_type(fm: dict, resolved_type: str | None) -> bool:
    """True when a record's type must be *inferred* from its ``journal`` field.

    PubMed front matter declares no type, so naming a journal is the only
    evidence it gives that the record is a journal article. Both exporters must
    agree on when that inference fires, or the same record gets typed one way in
    BibTeX and another in CSL — which is exactly how the two drifted apart
    before this predicate was hoisted here.

    Deliberately narrow: it fires only when *no key answered at all*. A record
    whose declared type has no entry in an exporter's map is a gap in that map,
    to be closed by adding the mapping — not by overriding a stated type with a
    guess. Without that restriction, `journal` (which Zotero fills from
    `publicationTitle` for *any* item type) silently re-typed magazine and
    newspaper articles as journal articles, and overrode `preprint` on the
    deposits #60 says must stay preprints.
    """
    return resolved_type is None and bool(fm.get("journal"))


# What the `journal` front-matter field *is* for a given work type. Both
# exporters read this one table, then translate the role into their own field
# name — the role is the shared judgement, the field name is not.
#
# Selecting the venue field per entry type (rather than special-casing one type)
# is what keeps the emitted field valid: in standard BibTeX `journal` belongs to
# `@article` alone, so putting it on `@book`/`@incollection`/`@inproceedings`/
# `@techreport`/`@phdthesis` is exactly as wrong as putting it on `@misc` (#384).
PERIODICAL = "periodical"     # a serial: journal, magazine, newspaper
COLLECTION = "collection"     # a larger work containing this one: book, proceedings
ISSUER = "issuer"             # the institution that issued a report
SCHOOL = "school"             # the degree-granting institution of a thesis
INFORMAL = "informal"         # self-deposited or unclassified: no formal container
NO_VENUE = "no-venue"         # the work IS the container (a whole book)

_VENUE_ROLES = {
    # Zotero itemType
    "journalArticle": PERIODICAL,
    "magazineArticle": PERIODICAL,
    "newspaperArticle": PERIODICAL,
    "conferencePaper": COLLECTION,
    "bookSection": COLLECTION,
    "encyclopediaArticle": COLLECTION,
    "dictionaryEntry": COLLECTION,
    "book": NO_VENUE,
    "report": ISSUER,
    "thesis": SCHOOL,
    "preprint": INFORMAL,
    # OpenAlex work type
    "article": PERIODICAL,
    "review": PERIODICAL,
    "book-review": PERIODICAL,
    "letter": PERIODICAL,
    "editorial": PERIODICAL,
    "erratum": PERIODICAL,
    "retraction": PERIODICAL,
    "data-paper": PERIODICAL,
    "conference-paper": COLLECTION,
    "book-chapter": COLLECTION,
    "book-section": COLLECTION,
    "reference-entry": COLLECTION,
    "report-component": ISSUER,
    "dissertation": SCHOOL,
    "dataset": INFORMAL,
    "software": INFORMAL,
}


def venue_role(fm: dict) -> str:
    """What kind of venue this record's ``journal`` field names.

    Shared for the same reason as :func:`should_promote_to_journal_type`: when
    each exporter picked the venue field on its own condition, the same record
    was published in a ``howpublished`` in BibTeX and a ``container-title`` in
    CSL, and a BibTeX->CSL round trip (pandoc reads ``howpublished`` as
    ``publisher``) made the two outputs contradict each other outright.

    An unknown type gets :data:`INFORMAL`, whose field is valid on every entry
    type, so a type this table has never heard of cannot produce an invalid
    pairing.
    """
    source_type = resolve_source_type(fm)
    if should_promote_to_journal_type(fm, source_type):
        return PERIODICAL
    if source_type is None:
        return INFORMAL
    return _VENUE_ROLES.get(source_type, INFORMAL)
