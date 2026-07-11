# SPDX-License-Identifier: Apache-2.0
"""Unit tests for PubMed retraction detection (#164, spec §6.4, spike §1).

The fixtures are trimmed to the elements that matter for retraction, but every
*shape* is the one the #160 live spike recorded on 2026-07-11
(``docs/pubmed-spike-findings.md``), not one invented from the spec:

* PMID 16354850 carries **both** markers (``Retracted Publication`` pub-type and
  a ``RetractionIn`` comment → notice PMID 18842931). Spike §1.
* PMID 33301246 (control) carries **neither** — a ``Retracted Publication``-free
  pub-type list and 11 ``CommentIn`` comments with zero ``RetractionIn``. Spike §2.
* A retraction *notice* carries ``Retraction Notice`` + ``RetractionOf`` (notice
  42328254 → 42245901) — retraction-shaped, opposite direction; must NOT flag.
* ``RetractionIn`` with an empty / absent child ``<PMID>`` (PMIDs 42235148 /
  42129929) — retracted is still true, only the link target is missing. Spike §1.

The single-marker fixtures (``ONLY_PUBTYPE`` / ``ONLY_RETRACTION_IN``) are the OR
proof: today the markers co-occur, but the union rule must detect each alone.
"""
from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from factlog.integrations.pubmed import retraction as retraction_module
from factlog.integrations.pubmed.retraction import (
    RetractionStatus,
    detect_retraction,
)
from factlog.integrations.pubmed.work_parser import (
    parse_article,
    parse_efetch_response,
)


def _wrap(citation_body: str, pubmed_data: str = "") -> str:
    """A whole ``<PubmedArticle>`` around a MedlineCitation body."""
    return (
        "<PubmedArticle>"
        "<MedlineCitation Status=\"MEDLINE\" Owner=\"NLM\">"
        f"{citation_body}"
        "</MedlineCitation>"
        f"{pubmed_data}"
        "</PubmedArticle>"
    )


# --- Both markers present: the spec §6.4 example, as the spike observed it. -----
RETRACTED_BOTH = _wrap(
    "<PMID Version=\"1\">16354850</PMID>"
    "<Article PubModel=\"Print\">"
    "<Journal><Title>Chest</Title></Journal>"
    "<ArticleTitle>Omega-3 fatty acids in COPD.</ArticleTitle>"
    "<PublicationTypeList>"
    "<PublicationType UI=\"D016428\">Journal Article</PublicationType>"
    "<PublicationType UI=\"D016449\">Randomized Controlled Trial</PublicationType>"
    "<PublicationType UI=\"D016441\">Retracted Publication</PublicationType>"
    "</PublicationTypeList>"
    "</Article>"
    "<CommentsCorrectionsList>"
    "<CommentsCorrections RefType=\"RetractionIn\">"
    "<RefSource>Chest. 2008 Oct;134(4):893.</RefSource>"
    "<PMID Version=\"1\">18842931</PMID>"
    "</CommentsCorrections>"
    "</CommentsCorrectionsList>"
)

# --- Control: neither marker; plenty of CommentIn but no RetractionIn. ----------
CONTROL_NOT_RETRACTED = _wrap(
    "<PMID Version=\"1\">33301246</PMID>"
    "<Article PubModel=\"Print-Electronic\">"
    "<Journal><Title>N Engl J Med</Title></Journal>"
    "<ArticleTitle>A phase II/III randomized trial.</ArticleTitle>"
    "<PublicationTypeList>"
    "<PublicationType UI=\"D016428\">Journal Article</PublicationType>"
    "<PublicationType UI=\"D016449\">Randomized Controlled Trial</PublicationType>"
    "</PublicationTypeList>"
    "</Article>"
    "<CommentsCorrectionsList>"
    "<CommentsCorrections RefType=\"CommentIn\">"
    "<RefSource>N Engl J Med. 2021;384(1):1.</RefSource>"
    "<PMID Version=\"1\">33301111</PMID>"
    "</CommentsCorrections>"
    "<CommentsCorrections RefType=\"CommentIn\">"
    "<RefSource>N Engl J Med. 2021;384(2):2.</RefSource>"
    "<PMID Version=\"1\">33301222</PMID>"
    "</CommentsCorrections>"
    "</CommentsCorrectionsList>"
)

# --- OR proof, marker 1 alone: Retracted Publication, no RetractionIn. ----------
ONLY_PUBTYPE = _wrap(
    "<PMID Version=\"1\">40000001</PMID>"
    "<Article PubModel=\"Print\">"
    "<ArticleTitle>Only the pub-type marker.</ArticleTitle>"
    "<PublicationTypeList>"
    "<PublicationType UI=\"D016428\">Journal Article</PublicationType>"
    "<PublicationType UI=\"D016441\">Retracted Publication</PublicationType>"
    "</PublicationTypeList>"
    "</Article>"
)

# --- OR proof, marker 2 alone: RetractionIn, no Retracted Publication pub-type. -
ONLY_RETRACTION_IN = _wrap(
    "<PMID Version=\"1\">40000002</PMID>"
    "<Article PubModel=\"Print\">"
    "<ArticleTitle>Only the RetractionIn marker.</ArticleTitle>"
    "<PublicationTypeList>"
    "<PublicationType UI=\"D016428\">Journal Article</PublicationType>"
    "</PublicationTypeList>"
    "</Article>"
    "<CommentsCorrectionsList>"
    "<CommentsCorrections RefType=\"RetractionIn\">"
    "<RefSource>J Example. 2024;1(1):1.</RefSource>"
    "<PMID Version=\"1\">40009999</PMID>"
    "</CommentsCorrections>"
    "</CommentsCorrectionsList>"
)

# --- The false-positive trap: a retraction NOTICE record (opposite direction). --
RETRACTION_NOTICE = _wrap(
    "<PMID Version=\"1\">42328254</PMID>"
    "<Article PubModel=\"Electronic\">"
    "<ArticleTitle>Retraction: A study that was retracted.</ArticleTitle>"
    "<PublicationTypeList>"
    "<PublicationType UI=\"D016428\">Journal Article</PublicationType>"
    "<PublicationType UI=\"D016440\">Retraction of Publication</PublicationType>"
    "</PublicationTypeList>"
    "</Article>"
    "<CommentsCorrectionsList>"
    "<CommentsCorrections RefType=\"RetractionOf\">"
    "<RefSource>J Example. 2023;9(9):9.</RefSource>"
    "<PMID Version=\"1\">42245901</PMID>"
    "</CommentsCorrections>"
    "</CommentsCorrectionsList>"
)

# --- RetractionIn with an EMPTY child PMID (RefSource string only). -------------
RETRACTION_IN_EMPTY_PMID = _wrap(
    "<PMID Version=\"1\">42235148</PMID>"
    "<Article PubModel=\"Electronic\">"
    "<ArticleTitle>Retracted, but the notice PMID is empty.</ArticleTitle>"
    "<PublicationTypeList>"
    "<PublicationType UI=\"D016428\">Journal Article</PublicationType>"
    "</PublicationTypeList>"
    "</Article>"
    "<CommentsCorrectionsList>"
    "<CommentsCorrections RefType=\"RetractionIn\">"
    "<RefSource>J Example. 2026;2(2):2.</RefSource>"
    "<PMID Version=\"1\"></PMID>"
    "</CommentsCorrections>"
    "</CommentsCorrectionsList>"
)

# --- RetractionIn with NO child PMID element at all. ----------------------------
RETRACTION_IN_NO_PMID = _wrap(
    "<PMID Version=\"1\">42129929</PMID>"
    "<Article PubModel=\"Electronic\">"
    "<ArticleTitle>Retracted via a citation string only.</ArticleTitle>"
    "<PublicationTypeList>"
    "<PublicationType UI=\"D016428\">Journal Article</PublicationType>"
    "</PublicationTypeList>"
    "</Article>"
    "<CommentsCorrectionsList>"
    "<CommentsCorrections RefType=\"RetractionIn\">"
    "<RefSource>J Example. 2026;3(3):3.</RefSource>"
    "</CommentsCorrections>"
    "</CommentsCorrectionsList>"
)

# --- Two RetractionIn comments: the first has no linkable PMID, the second does.
# The notice PMID is the first *non-empty* one across all comments, not the first
# element — so an empty lead comment must not shadow a later linkable one.
MULTI_RETRACTION_IN = _wrap(
    "<PMID Version=\"1\">42500000</PMID>"
    "<Article PubModel=\"Electronic\">"
    "<ArticleTitle>Two RetractionIn comments, first unlinkable.</ArticleTitle>"
    "<PublicationTypeList>"
    "<PublicationType UI=\"D016428\">Journal Article</PublicationType>"
    "</PublicationTypeList>"
    "</Article>"
    "<CommentsCorrectionsList>"
    "<CommentsCorrections RefType=\"RetractionIn\">"
    "<RefSource>J Example. 2026;4(4):4.</RefSource>"
    "<PMID Version=\"1\"></PMID>"
    "</CommentsCorrections>"
    "<CommentsCorrections RefType=\"RetractionIn\">"
    "<RefSource>J Example. 2026;5(5):5.</RefSource>"
    "<PMID Version=\"1\">42599999</PMID>"
    "</CommentsCorrections>"
    "</CommentsCorrectionsList>"
)


# ----------------------------------------------------------------------------
# detect_retraction — the pure function
# ----------------------------------------------------------------------------

def test_real_retraction_16354850_detected_with_notice_pmid():
    status = detect_retraction(RETRACTED_BOTH)
    assert status.retracted is True
    assert status.via_publication_type is True
    assert status.via_retraction_in is True
    assert status.retraction_notice_pmid == "18842931"


def test_control_33301246_not_retracted_despite_many_comments():
    # Guards against a parser that over-triggers on any CommentsCorrections.
    status = detect_retraction(CONTROL_NOT_RETRACTED)
    assert status.retracted is False
    assert status.via_publication_type is False
    assert status.via_retraction_in is False
    assert status.retraction_notice_pmid is None


def test_publication_type_marker_alone_is_sufficient():
    # OR proof #1: Retracted Publication with no RetractionIn comment.
    status = detect_retraction(ONLY_PUBTYPE)
    assert status.retracted is True
    assert status.via_publication_type is True
    assert status.via_retraction_in is False
    assert status.retraction_notice_pmid is None


def test_retraction_in_marker_alone_is_sufficient():
    # OR proof #2: RetractionIn comment with no Retracted Publication pub-type.
    status = detect_retraction(ONLY_RETRACTION_IN)
    assert status.retracted is True
    assert status.via_publication_type is False
    assert status.via_retraction_in is True
    assert status.retraction_notice_pmid == "40009999"


def test_retraction_notice_record_is_not_flagged_as_retracted():
    # The false-positive trap: Retraction Notice + RetractionOf point the other
    # way. The exact-match OR excludes it — a substring "retract" search would not.
    status = detect_retraction(RETRACTION_NOTICE)
    assert status.retracted is False
    assert status.via_publication_type is False
    assert status.via_retraction_in is False
    assert status.retraction_notice_pmid is None


def test_retraction_in_with_empty_pmid_is_retracted_without_notice_pmid():
    status = detect_retraction(RETRACTION_IN_EMPTY_PMID)
    assert status.retracted is True
    assert status.via_retraction_in is True
    assert status.retraction_notice_pmid is None


def test_retraction_in_with_no_pmid_element_is_none_safe():
    status = detect_retraction(RETRACTION_IN_NO_PMID)
    assert status.retracted is True
    assert status.via_retraction_in is True
    assert status.retraction_notice_pmid is None


def test_multiple_retraction_in_takes_first_non_empty_notice_pmid():
    # NIT: the first RetractionIn comment carries no linkable PMID; the notice
    # PMID must be the second comment's, not an empty first-element shadow.
    status = detect_retraction(MULTI_RETRACTION_IN)
    assert status.retracted is True
    assert status.via_retraction_in is True
    assert status.retraction_notice_pmid == "42599999"


def test_detect_accepts_a_parsed_element_too():
    element = ET.fromstring(RETRACTED_BOTH)
    assert detect_retraction(element) == detect_retraction(RETRACTED_BOTH)


def test_single_record_wrapped_in_a_set_is_unwrapped():
    # A PubmedArticleSet holding exactly one record is accepted (unwrapped).
    one = f"<PubmedArticleSet>{RETRACTED_BOTH}</PubmedArticleSet>"
    assert detect_retraction(one) == detect_retraction(RETRACTED_BOTH)


def test_multi_record_set_is_refused_not_ored_across_records():
    # MINOR 3 footgun: a set with a retracted and a non-retracted record must not
    # OR their markers into one True. Classifying a batch is not this function's job.
    both = (
        "<PubmedArticleSet>"
        f"{RETRACTED_BOTH}{CONTROL_NOT_RETRACTED}"
        "</PubmedArticleSet>"
    )
    with pytest.raises(ValueError):
        detect_retraction(both)


def test_empty_set_is_not_retracted():
    assert detect_retraction("<PubmedArticleSet></PubmedArticleSet>").retracted is False


def test_detect_rejects_a_non_xml_non_element_input():
    with pytest.raises(TypeError):
        detect_retraction(1234)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------
# parse_article / parse_efetch_response integration — additive fields
# ----------------------------------------------------------------------------

def test_parse_article_carries_the_retraction_signal():
    work = parse_article(ET.fromstring(RETRACTED_BOTH))
    assert work.pmid == "16354850"
    assert work.retracted is True
    assert work.retraction_notice_pmid == "18842931"


def test_parse_article_leaves_control_unflagged():
    work = parse_article(ET.fromstring(CONTROL_NOT_RETRACTED))
    assert work.retracted is False
    assert work.retraction_notice_pmid is None


def test_parse_efetch_response_flags_only_the_retracted_record():
    body = (
        "<PubmedArticleSet>"
        f"{RETRACTED_BOTH}{CONTROL_NOT_RETRACTED}"
        "</PubmedArticleSet>"
    )
    outcome = parse_efetch_response(body, ["16354850", "33301246"])
    by_pmid = {w.pmid: w for w in outcome.works}
    assert by_pmid["16354850"].retracted is True
    assert by_pmid["16354850"].retraction_notice_pmid == "18842931"
    assert by_pmid["33301246"].retracted is False


# ----------------------------------------------------------------------------
# Provenance mapping contract + the human-gate guard (§6.4, P1/P3)
# ----------------------------------------------------------------------------

def test_provenance_mapping_is_source_scoped_and_omits_absent_fields():
    status = detect_retraction(RETRACTED_BOTH)
    fields = status.to_provenance_fields(verified_at="2026-07-11T00:00:00Z")
    # Source-scoped record fields: `retracted` (this source's own signal, coexists
    # with OpenAlex `openalex_is_retracted`), plus the notice PMID and a stamp.
    assert fields == {
        "retracted": True,
        "retraction_notice_pmid": "18842931",
        "retraction_verified_at": "2026-07-11T00:00:00Z",
    }


def test_provenance_mapping_omits_notice_pmid_when_absent():
    fields = detect_retraction(RETRACTION_IN_NO_PMID).to_provenance_fields()
    assert fields == {"retracted": True}
    assert "retraction_notice_pmid" not in fields
    assert "retraction_verified_at" not in fields


def test_provenance_mapping_is_empty_when_not_retracted():
    # Like OpenAlex is_retracted: emitted only when present, so absence means
    # not-retracted rather than not-checked.
    assert detect_retraction(CONTROL_NOT_RETRACTED).to_provenance_fields() == {}
    assert detect_retraction(RETRACTION_NOTICE).to_provenance_fields() == {}


def test_detection_never_writes_a_top_level_retracted_claim(tmp_path, monkeypatch):
    # P1/P3: no code path in this change writes a ledger or the merged top-level
    # `retracted:` claim — that is a human's `pubmed-acknowledge-retraction`.
    # Detection is pure data; run it in an empty cwd and assert nothing is written.
    monkeypatch.chdir(tmp_path)
    status = detect_retraction(RETRACTED_BOTH)
    work = parse_article(ET.fromstring(RETRACTED_BOTH))
    assert status.retracted is True
    assert work.retracted is True
    assert list(Path(tmp_path).iterdir()) == []
    # The retraction module holds no writer and does not reach for the
    # acknowledgement primitive (the only authorized writer of the human gate) or
    # any provenance writer. Checked on imports, not a bare substring — the
    # docstring legitimately *names* the downstream pubmed-acknowledge-retraction
    # command without importing it.
    src = Path(retraction_module.__file__).read_text(encoding="utf-8")
    assert "import" in src  # sanity: we actually read the module source
    assert "common.acknowledge" not in src
    assert "import acknowledge" not in src
    assert "common.provenance" not in src
    assert not any(
        name.lower().startswith(("write", "update", "save", "acknowledge"))
        for name in dir(retraction_module)
    )
