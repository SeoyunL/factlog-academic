#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Turn one PubMed ``efetch`` XML record into a :class:`ParsedPubMedWork` (spec §6.4).

In the shape of ``arxiv/work_parser.py`` and ``openalex/work_parser.py``: a pure
function over the raw efetch body — no network, no filesystem, no optional
library. Every decision below is anchored to the #160 live spike
(``docs/pubmed-spike-findings.md``), not to the spec's assumptions.

**Absence is data, not error (spike §6).** Author, abstract, and DOI are all
*normally* absent — abstract on 4/12 old records, DOI on 4/8 old news items,
``AuthorList`` entirely missing on 4/15 recent errata. A record with none of the
three parses without raising and without warning; every optional path is guarded.

**``PubDate`` is not one shape (spike §6, spec §6.6 Risk 2).** A record carries a
``<Year>``, or a free-text ``<MedlineDate>`` range, or neither. The year feeds
the title+author+year matcher (``common/matcher.py``, ``YEAR_TOLERANCE=1``), so a
silent ``None`` degrades duplicate detection unseen. We read ``<Year>`` first and
fall back to the first four-digit run inside ``<MedlineDate>``; :attr:`pub_date_raw`
carries the ``MedlineDate`` verbatim so a caller can see *why* a year was derived
(or is ``None``) rather than guessing.

**DOI lives in two places (spike §6).** ``ArticleIdList/ArticleId[@IdType="doi"]``
*and* ``ELocationID[@EIdType="doi"]``. Both are checked before concluding "no DOI".

**Structured abstracts (spike §6).** Multiple ``<AbstractText>`` segments (with
``@Label`` like BACKGROUND/METHODS) concatenate into the full abstract, and the
DTD permits inline child markup, so each segment is read with ``itertext()``,
not ``.text``.

**Three outcomes, named — never collapsed (spec §6.4, spike §4–5).** An efetch
response classifies each requested PMID as one of:

* **present** — a returned record whose ``<PMID>`` matches the request.
* **deleted** — requested but *absent* from the response. Spike §5: a gone PMID
  is HTTP 200 with an empty ``<PubmedArticleSet/>`` — not an error, not a network
  failure. It is a *value* here (an entry in :attr:`PubMedFetchOutcome.deleted`),
  categorically distinct from a network failure, which is an *exception* the
  client (#162) raises before this parser ever runs.
* **merged** — a returned record whose ``<PMID>`` differs from the requested one.
  Spike §4 found efetch returns by omission, never substitution, so in a batch a
  merged PMID reads as an absence; but a single-request merge (request X, receive
  Y) is unambiguous, and both PMIDs are exposed so a downstream ``pubmed-refresh``
  (#170) can follow the pointer. Flattening these three here would leave nothing
  downstream to recover the distinction from.

MeSH major/minor (#165) is *deliberately not here*. Retraction detection (#164)
lives in the sibling ``retraction.py`` module (a pure function); this parser only
carries its two additive result fields (:attr:`ParsedPubMedWork.retracted`,
:attr:`ParsedPubMedWork.retraction_notice_pmid`) and calls it once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from factlog.integrations.pubmed.mesh import MeshHeading, parse_mesh_headings
from factlog.integrations.pubmed.retraction import detect_retraction

__all__ = [
    "ParsedPubMedWork",
    "PresentRecord",
    "MergedRecord",
    "PubMedFetchOutcome",
    "PubMedParseError",
    "parse_article",
    "parse_efetch_response",
]

# The first plausible four-digit year inside a free-text MedlineDate. NLM writes
# ranges ("1998 Dec-1999 Jan"), seasons ("Winter 1985"), and bare years; the
# publication year is the first four-digit run in every observed shape. The
# leading-digit classes keep it from latching onto a three-digit volume or a
# five-digit page number that happens to sit beside the date.
_MEDLINE_YEAR_RE = re.compile(r"\b(1\d{3}|20\d{2}|21\d{2})\b")


class PubMedParseError(Exception):
    """The efetch body is not parseable PubMed XML.

    Raised for a body that is not XML at all, whose root is not a
    ``PubmedArticleSet``, or that is an ``<eFetchResult><ERROR>`` (spike §5: a
    *malformed* id yields HTTP 400 with that shape — a caller bug, distinct from a
    gone PMID's empty-but-well-formed set). A gone PMID is **not** an error: it is
    reported as a ``deleted`` value, never by raising.
    """


@dataclass(frozen=True)
class ParsedPubMedWork:
    """One PubMed article, reduced to the fields a factlog source file records.

    ``pmid`` is the PMID *as returned* (the key identifier). For a merged record
    that differs from the requested id — :class:`MergedRecord` keeps both.

    ``pub_date_raw`` is the ``MedlineDate`` free text when the year came from
    there (else ``None``); it makes a derived-or-absent year auditable instead of
    silent. ``year`` is the resolved integer year, or ``None`` when the record
    carries neither a ``<Year>`` nor a parseable ``<MedlineDate>``.
    """

    pmid: str
    title: str | None = None
    authors: tuple[str, ...] = ()
    journal: str | None = None
    year: int | None = None
    doi: str | None = None
    abstract: str = ""
    pub_date_raw: str | None = None
    # #165: MeSH descriptors with descriptor- AND qualifier-level majorness
    # preserved (the level OpenAlex drops; spike §7). Empty on an unindexed
    # record. Source-scoped — coexists with OpenAlex's flat ``mesh_terms``.
    mesh_headings: tuple[MeshHeading, ...] = ()
    # Additive retraction signal (#164). This is a *source-scoped* signal (like
    # arXiv `withdrawn_by` / OpenAlex `openalex_is_retracted`), derived by
    # `retraction.detect_retraction`; it never sets the merged top-level
    # `retracted:` claim, which stays a human acknowledgement (§6.4).
    retracted: bool = False
    retraction_notice_pmid: str | None = None

    @property
    def has_abstract(self) -> bool:
        return bool(self.abstract)


@dataclass(frozen=True)
class PresentRecord:
    """A requested PMID that returned a record under the same PMID."""

    requested_pmid: str
    work: ParsedPubMedWork


@dataclass(frozen=True)
class MergedRecord:
    """A record returned under a PMID that differs from the one requested.

    ``requested_pmid`` is known only when the pairing is unambiguous (a single
    requested PMID that came back under a different one); in an ambiguous batch it
    is ``None`` while ``returned_pmid`` is always the PMID that actually arrived.
    Both are exposed so ``pubmed-refresh`` can record the forward pointer.
    """

    requested_pmid: str | None
    returned_pmid: str
    work: ParsedPubMedWork


@dataclass(frozen=True)
class PubMedFetchOutcome:
    """One efetch response classified against the PMIDs that were requested.

    The three buckets are kept separate on purpose (see module docstring): a
    downstream refresh acts differently on a deleted vs a merged PMID, and cannot
    recover a distinction this parser throws away.
    """

    present: tuple[PresentRecord, ...] = ()
    deleted: tuple[str, ...] = ()
    merged: tuple[MergedRecord, ...] = ()

    @property
    def works(self) -> tuple[ParsedPubMedWork, ...]:
        """Every parsed record in the response, present and merged alike."""
        return tuple(r.work for r in self.present) + tuple(m.work for m in self.merged)


def _text(value: object) -> str | None:
    """A non-empty, whitespace-collapsed string, or None."""
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


def _node_text(node: ET.Element | None) -> str | None:
    """Full text of an element including inline child markup, collapsed, or None.

    ``itertext()`` (not ``.text``) because the DTD permits inline markup inside
    ``AbstractText``/``ArticleTitle`` (spike §6), and ``.text`` would silently
    truncate at the first child element.
    """
    if node is None:
        return None
    return _text("".join(node.itertext()))


def _author_name(author: ET.Element) -> str | None:
    """One author as "Given Family" (or a CollectiveName), or None.

    "Given Family" order — not citation "Family Initials" — so the last token is
    the surname, which is exactly what ``matcher.surname`` folds on for the
    duplicate gate. A ``CollectiveName`` (group authorship, spike §6) stands in
    when there is no personal name.
    """
    collective = _node_text(author.find("CollectiveName"))
    if collective:
        return collective
    last = _node_text(author.find("LastName"))
    given = _node_text(author.find("ForeName")) or _node_text(author.find("Initials"))
    parts = [p for p in (given, last) if p]
    return " ".join(parts) or None


def _authors(article: ET.Element) -> tuple[str, ...]:
    """Author display names in list order; empty when ``AuthorList`` is absent.

    Spike §6: ``AuthorList`` is *sometimes entirely absent* (4/15 recent errata) —
    a plain record, not a corrupt one, so this returns ``()`` without warning.
    """
    author_list = article.find("AuthorList")
    if author_list is None:
        return ()
    names = []
    for author in author_list.findall("Author"):
        name = _author_name(author)
        if name:
            names.append(name)
    return tuple(names)


def _journal(article: ET.Element) -> str | None:
    """Journal ``<Title>``, falling back to ``<ISOAbbreviation>`` (spec §6.4)."""
    journal = article.find("Journal")
    if journal is None:
        return None
    return _node_text(journal.find("Title")) or _node_text(journal.find("ISOAbbreviation"))


def _pub_date(article: ET.Element) -> tuple[int | None, str | None]:
    """Resolve (year, medline_date_raw) from ``Journal/JournalIssue/PubDate``.

    ``<Year>`` wins. Otherwise ``<MedlineDate>``'s first four-digit run is the
    year and its verbatim text is returned so a derived year is auditable. Neither
    present → ``(None, None)``. See module docstring for why the year matters.
    """
    journal = article.find("Journal")
    if journal is None:
        return None, None
    pub_date = journal.find("JournalIssue/PubDate")
    if pub_date is None:
        return None, None

    year_text = _node_text(pub_date.find("Year"))
    if year_text and year_text.isdigit():
        return int(year_text), None

    medline = _node_text(pub_date.find("MedlineDate"))
    if medline:
        match = _MEDLINE_YEAR_RE.search(medline)
        return (int(match.group(1)) if match else None), medline

    return None, None


def _normalize_doi(value: str | None) -> str | None:
    """A bare, lowercased DOI, stripping any ``doi:`` or ``doi.org`` prefix.

    DOIs are case-insensitive; §7.1 duplicate detection matches on bare DOIs, so
    they are reduced to that shape here. A value that does not look like a DOI
    (no ``10.`` registrant) is dropped rather than recorded as junk.
    """
    text = _text(value)
    if text is None:
        return None
    lowered = text.lower()
    lowered = re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", lowered)
    lowered = re.sub(r"^doi:\s*", "", lowered)
    lowered = lowered.strip()
    return lowered if lowered.startswith("10.") else None


def _doi(article: ET.Element, pubmed_data: ET.Element | None) -> str | None:
    """DOI from either home NLM gives it (spike §6): ``ArticleId`` then ``ELocationID``.

    ``ArticleIdList/ArticleId[@IdType="doi"]`` (in ``PubmedData``) is tried first,
    then ``ELocationID[@EIdType="doi"]`` (in ``Article``). Both are checked before
    concluding a record has no DOI.
    """
    if pubmed_data is not None:
        for article_id in pubmed_data.findall("ArticleIdList/ArticleId"):
            if article_id.get("IdType") == "doi":
                doi = _normalize_doi(_node_text(article_id))
                if doi:
                    return doi
    for eloc in article.findall("ELocationID"):
        if eloc.get("EIdType") == "doi":
            doi = _normalize_doi(_node_text(eloc))
            if doi:
                return doi
    return None


def parse_article(record: ET.Element) -> ParsedPubMedWork:
    """Reduce one ``<PubmedArticle>`` element to a :class:`ParsedPubMedWork`.

    Raises :class:`PubMedParseError` only when the record carries no ``<PMID>`` —
    without an id nothing downstream can address the record. Every other field
    degrades to ``None``/empty rather than failing (spike §6: absence is data).
    """
    citation = record.find("MedlineCitation")
    if citation is None:
        raise PubMedParseError("PubmedArticle has no MedlineCitation")

    pmid = _node_text(citation.find("PMID"))
    if not pmid:
        raise PubMedParseError("PubmedArticle record has no PMID")

    # MeSH lives under MedlineCitation, not Article (#165, spike §7): read it here
    # so it survives even a degenerate Article-less citation.
    mesh_headings = parse_mesh_headings(citation)
    # Detected off the whole record: PublicationTypeList sits under Article, but
    # the RetractionIn comment sits under MedlineCitation, so a record can be
    # retracted even when Article is degenerate/absent. One call, both markers.
    retraction = detect_retraction(record)

    article = citation.find("Article")
    if article is None:
        # A citation with no Article is degenerate but still addressable; expose
        # the PMID (so deleted/merged classification still works) with empty body.
        return ParsedPubMedWork(
            pmid=pmid,
            mesh_headings=mesh_headings,
            retracted=retraction.retracted,
            retraction_notice_pmid=retraction.retraction_notice_pmid,
        )

    pubmed_data = record.find("PubmedData")
    year, pub_date_raw = _pub_date(article)

    abstract_parts = [
        text
        for node in article.findall("Abstract/AbstractText")
        if (text := _node_text(node))
    ]

    return ParsedPubMedWork(
        pmid=pmid,
        title=_node_text(article.find("ArticleTitle")),
        authors=_authors(article),
        journal=_journal(article),
        year=year,
        doi=_doi(article, pubmed_data),
        abstract=" ".join(abstract_parts),
        pub_date_raw=pub_date_raw,
        mesh_headings=mesh_headings,
        retracted=retraction.retracted,
        retraction_notice_pmid=retraction.retraction_notice_pmid,
    )


def _require_str(value: object) -> str:
    if not isinstance(value, str):
        raise PubMedParseError(f"efetch body must be a string, got {type(value).__name__}")
    return value


def _normalize_requested(requested_pmids: object) -> list[str]:
    """A de-duplicated, order-preserving list of requested PMID strings."""
    if isinstance(requested_pmids, str):
        items: list[object] = [requested_pmids]
    else:
        items = list(requested_pmids)
    seen: dict[str, None] = {}
    for item in items:
        pmid = str(item).strip()
        if pmid:
            seen.setdefault(pmid, None)
    return list(seen)


def parse_efetch_response(xml_text: str, requested_pmids) -> PubMedFetchOutcome:
    """Parse a full efetch response and classify it against the requested PMIDs.

    ``requested_pmids`` may be a single PMID string or an iterable of them.

    Returns a :class:`PubMedFetchOutcome` with present/deleted/merged buckets
    (see module docstring). Raises :class:`PubMedParseError` for a body that is
    not parseable ``PubmedArticleSet`` XML — including the ``<ERROR>`` body a
    malformed id earns (spike §5). A gone PMID is **not** an error: it lands in
    ``deleted``. A network failure never reaches here — the client (#162) raises
    it — so a value in ``deleted`` and a raised exception are never confusable.
    """
    text = _require_str(xml_text)
    requested = _normalize_requested(requested_pmids)

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise PubMedParseError(f"efetch body is not well-formed XML: {exc}") from exc

    # A malformed id returns <eFetchResult><ERROR>...</ERROR></eFetchResult> with
    # HTTP 400 (spike §5). That is a caller/id bug, not a gone PMID — raise.
    if root.tag == "eFetchResult":
        error = _node_text(root.find("ERROR"))
        raise PubMedParseError(f"efetch returned an error: {error or 'unspecified'}")

    if root.tag != "PubmedArticleSet":
        raise PubMedParseError(
            f"unexpected efetch root <{root.tag}>, expected <PubmedArticleSet>"
        )

    # An empty set is the deleted/gone signal (spike §5): well-formed, not an
    # error. Records fall out below as `deleted` with no work attached.
    works = [parse_article(record) for record in root.findall("PubmedArticle")]

    requested_set = set(requested)
    present: list[PresentRecord] = []
    unexpected: list[ParsedPubMedWork] = []  # returned under a non-requested PMID
    matched: set[str] = set()
    for work in works:
        if work.pmid in requested_set:
            present.append(PresentRecord(requested_pmid=work.pmid, work=work))
            matched.add(work.pmid)
        else:
            unexpected.append(work)

    unmatched_requested = [p for p in requested if p not in matched]

    # A single-request merge is unambiguous: exactly one PMID went unanswered and
    # exactly one record came back under a different id — pair them, and the
    # requested PMID is merged, not deleted. Any other arrangement leaves the
    # pairing unknowable (batch omission, spike §4), so unexpected records surface
    # with requested_pmid=None and unmatched requests stay deleted.
    if len(unmatched_requested) == 1 and len(unexpected) == 1:
        merged = (
            MergedRecord(
                requested_pmid=unmatched_requested[0],
                returned_pmid=unexpected[0].pmid,
                work=unexpected[0],
            ),
        )
        deleted: tuple[str, ...] = ()
    else:
        merged = tuple(
            MergedRecord(requested_pmid=None, returned_pmid=w.pmid, work=w)
            for w in unexpected
        )
        deleted = tuple(unmatched_requested)

    return PubMedFetchOutcome(present=tuple(present), deleted=deleted, merged=merged)
