#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Detect a PubMed retraction from an efetch record (spec §6.4, spike §1).

A pure function over one ``<PubmedArticle>`` element (or the raw efetch XML): no
network, no filesystem, no ledger write. It lives in its own module — separate
from ``work_parser.py`` — so that this detection can be reasoned about, tested,
and evolved on its own, and so #165 (MeSH major/minor) and this issue touch the
shared :class:`ParsedPubMedWork` in as few, as local, places as possible.

## Two independent markers, OR-ed — never AND-ed (spike §1)

An efetch record carries a retraction two ways, and factlog treats **either
one alone** as sufficient:

1. ``Article/PublicationTypeList`` contains ``Retracted Publication`` (UI
   ``D016441``).
2. ``CommentsCorrectionsList`` contains a ``CommentsCorrections`` with
   ``RefType="RetractionIn"`` — a link out to the retraction *notice*.

The spike found these two co-occur on every one of ~30 live retractions today,
so OR-ing them costs nothing now; but co-occurrence is NCBI curation behaviour,
not a contract, and if a curation lag ever lands one marker before the other the
OR catches the retraction earlier. **Requiring both would silently miss a real
retraction the day they disagree.** So: retracted iff marker 1 OR marker 2.

## The false-positive trap this OR is built to avoid (spike §1)

The retraction *notice* is a separate record shaped like a retraction in the
opposite direction: its PublicationType is ``Retraction of Publication`` (UI
``D016440``, not ``Retracted Publication``) and its ``CommentsCorrections`` RefType is
``RetractionOf`` (not ``RetractionIn``), pointing *back* at the retracted
article. Matching by exact string on ``Retracted Publication`` / ``RetractionIn``
— never a substring search for "retract" — makes the OR naturally exclude the
notice: a notice has neither marker, so it is not flagged. (A counter-example
fixture in the tests pins this.)

## The notice PMID may be missing, and that is not an error (spike §1)

Marker 2 usually carries the notice's own ``<PMID>`` — the record a human goes
to in order to read *why* a paper was retracted, and confirm the retraction. It
is captured into :attr:`RetractionStatus.retraction_notice_pmid`. But the spike
saw ``RetractionIn`` elements whose child ``<PMID>`` was empty or absent (the
retraction asserted via a ``RefSource`` citation string only, PMIDs 42235148 /
42129929). The retraction is still true; only the machine-linkable target is
missing. So an absent/empty notice PMID yields ``None`` — never a raise, never a
suppressed retraction.

## Source-scoped signal, and the human gate is not bypassed (spec §6.4)

Like arXiv's ``withdrawn_by`` and OpenAlex's ``is_retracted``, this is a
*source-scoped* signal derived by parsing. It never writes the merged/top-level
``retracted:`` claim (§7.2). :meth:`RetractionStatus.to_provenance_fields` maps
it under a **PubMed source-scoped provenance record** only; promoting it to a
top-level claim is a human's decision via ``pubmed-acknowledge-retraction`` (a
downstream issue), never an import's or refresh's silent absorption. This module
holds no writer for that reason — it returns data, and nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree as ET

__all__ = [
    "RetractionStatus",
    "RETRACTED_PUBLICATION_TYPE",
    "RETRACTION_IN_REF_TYPE",
    "detect_retraction",
]

#: The exact ``PublicationType`` text NLM assigns a retracted article (UI
#: ``D016441``). Matched exactly — not as a substring — so ``Retraction of
#: Publication`` (the notice record's own type, UI ``D016440``) is never mistaken for it.
RETRACTED_PUBLICATION_TYPE = "Retracted Publication"

#: The exact ``CommentsCorrections`` RefType that links a retracted article out
#: to its notice. Its mirror ``RetractionOf`` (the notice pointing back) is
#: deliberately *not* a retraction marker and is excluded by exact matching.
RETRACTION_IN_REF_TYPE = "RetractionIn"


@dataclass(frozen=True)
class RetractionStatus:
    """Whether a PubMed record is retracted, and how it was detected.

    ``retracted`` is the OR of the two markers. ``via_publication_type`` and
    ``via_retraction_in`` record *which* marker(s) fired, so a caller (and the
    tests) can see the union at work and audit a single-marker detection.

    ``retraction_notice_pmid`` is the notice record's PMID when marker 2 supplied
    a non-empty one, else ``None`` — an absent link target is data, not an error
    (see module docstring).
    """

    retracted: bool = False
    retraction_notice_pmid: str | None = None
    via_publication_type: bool = False
    via_retraction_in: bool = False

    def to_provenance_fields(self, *, verified_at: str | None = None) -> dict[str, object]:
        """The source-scoped provenance fields a PubMed import/refresh records.

        Returns ``{}`` when not retracted — like OpenAlex's ``is_retracted``,
        retraction is emitted only when present, so its absence from a ledger
        *means* not-retracted rather than not-checked. When retracted:

        * ``retracted: True`` — the PubMed source's own signal. It is named
          ``retracted`` under a **PubMed source-scoped** record, distinct from
          OpenAlex's ``openalex_is_retracted``; the two coexist and never
          overwrite one another.
        * ``retraction_notice_pmid`` — only when a linkable notice PMID exists.
        * ``retraction_verified_at`` — the caller's verification timestamp, only
          when supplied (the pure parser has no clock; the import/refresh that
          calls the API stamps it).

        This is the mapping *contract*; the actual ledger write belongs to the
        downstream import/refresh issue. Nothing here writes the merged
        top-level ``retracted:`` claim — that stays a human acknowledgement.
        """
        if not self.retracted:
            return {}
        fields: dict[str, object] = {"retracted": True}
        if self.retraction_notice_pmid:
            fields["retraction_notice_pmid"] = self.retraction_notice_pmid
        if verified_at:
            fields["retraction_verified_at"] = verified_at
        return fields


def _clean(value: str | None) -> str | None:
    """A whitespace-collapsed, non-empty string, or None."""
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


def _as_record(source: ET.Element | str) -> ET.Element:
    """Normalize the input to a *single* record element to search.

    Accepts a single-record element (``<PubmedArticle>`` / ``<MedlineCitation>`` /
    ``<Article>``) or the raw XML of one. A ``<PubmedArticleSet>`` wrapping exactly
    one record is unwrapped to that record; an **empty** set is returned as-is (no
    record, so no markers, so not retracted).

    A set holding *more than one* record is refused with ``ValueError``. The marker
    search below uses descendant axes (``.//``), which would otherwise OR markers
    across sibling records — one retracted paper in a batch would flag the whole
    set. Retraction is a per-record fact, so multi-record classification belongs to
    ``parse_efetch_response`` (which parses each record on its own), never here. A
    non-str, non-Element input is a caller bug and raises ``TypeError`` rather than
    silently reporting not-retracted.
    """
    if isinstance(source, str):
        element = ET.fromstring(source)
    elif isinstance(source, ET.Element):
        element = source
    else:
        raise TypeError(
            f"detect_retraction expects an XML string or Element, got {type(source).__name__}"
        )
    if element.tag == "PubmedArticleSet":
        articles = element.findall("PubmedArticle")
        if len(articles) > 1:
            raise ValueError(
                "detect_retraction takes a single PubMed record, not a "
                f"multi-record PubmedArticleSet ({len(articles)} records); the "
                "descendant-axis marker search would OR retractions across records. "
                "Classify each record separately (see parse_efetch_response)."
            )
        if len(articles) == 1:
            return articles[0]
    return element


def _has_retracted_publication_type(element: ET.Element) -> bool:
    """True iff a ``PublicationType`` reads exactly ``Retracted Publication``.

    Exact, case-sensitive text match against NLM's controlled term (never a
    substring), so ``Retraction of Publication`` and ``Published Erratum`` cannot
    trip it. Case-sensitivity is deliberate: the value is a controlled MeSH term
    NLM emits verbatim, so a case difference would signal a feed change worth
    noticing rather than something to paper over.
    """
    for pub_type in element.iterfind(".//PublicationType"):
        if _clean(pub_type.text) == RETRACTED_PUBLICATION_TYPE:
            return True
    return False


def _retraction_in_notice_pmid(element: ET.Element) -> tuple[bool, str | None]:
    """Whether a ``RetractionIn`` comment exists, and its notice PMID if linkable.

    Returns ``(found, notice_pmid)``. ``found`` is True as soon as any
    ``CommentsCorrections`` carries ``RefType="RetractionIn"`` — the retraction
    holds even when the notice PMID is empty/absent. ``notice_pmid`` is the first
    non-empty child ``<PMID>`` across all such comments, or ``None`` (spike §1:
    the link target may be missing while the retraction is real).
    """
    found = False
    notice_pmid: str | None = None
    for comment in element.iterfind(".//CommentsCorrections"):
        if comment.get("RefType") != RETRACTION_IN_REF_TYPE:
            continue
        found = True
        if notice_pmid is None:
            # Scoped to this comment element — never the citation's own <PMID>.
            pmid = _clean(comment.findtext("PMID"))
            if pmid:
                notice_pmid = pmid
    return found, notice_pmid


def detect_retraction(source: ET.Element | str) -> RetractionStatus:
    """Detect a retraction on one efetch record (or raw efetch XML).

    ``source`` is a *single* record: a ``<PubmedArticle>`` element (as
    ``work_parser`` holds it), a ``<MedlineCitation>``/``<Article>`` element, the
    raw efetch XML of one record, or a ``<PubmedArticleSet>`` wrapping exactly one.
    A multi-record set is refused with ``ValueError`` (see :func:`_as_record`):
    classifying a batch is ``parse_efetch_response``'s job, not this per-record
    function's. The record is retracted iff it carries the ``Retracted
    Publication`` publication type **or** a ``RetractionIn`` comment — the OR the
    module docstring justifies. Never raises on a normal record; a non-retracted
    record (and a retraction *notice*, which carries neither marker) returns
    ``retracted=False``.
    """
    element = _as_record(source)
    via_pub_type = _has_retracted_publication_type(element)
    via_retraction_in, notice_pmid = _retraction_in_notice_pmid(element)
    return RetractionStatus(
        retracted=via_pub_type or via_retraction_in,
        retraction_notice_pmid=notice_pmid,
        via_publication_type=via_pub_type,
        via_retraction_in=via_retraction_in,
    )
