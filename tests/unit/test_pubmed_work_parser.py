# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the PubMed efetch parser (#163, spec §6.4, §6.6).

The fixtures are inline efetch XML shaped like the #160 spike's recorded
responses (`docs/pubmed-spike-findings.md`): a present record with a structured
abstract and DOI in both ArticleId and ELocationID form, a bare record with no
authors/abstract/DOI, the three PubDate shapes (Year / MedlineDate / neither),
the empty `<PubmedArticleSet/>` a deleted PMID returns, and the `<ERROR>` body a
malformed id earns. No network, no recorded-response library.
"""
from __future__ import annotations

import pytest

from factlog.integrations.pubmed.work_parser import (
    MergedRecord,
    ParsedPubMedWork,
    PresentRecord,
    PubMedFetchOutcome,
    PubMedParseError,
    parse_efetch_response,
)

# A trimmed present record in the shape of spike §1's PMID 16354850: structured
# abstract, journal Title + ISOAbbreviation, DOI in BOTH ArticleId and
# ELocationID, a personal author and a CollectiveName group author.
PRESENT_XML = """<?xml version="1.0" ?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">16354850</PMID>
      <Article>
        <Journal>
          <Title>Chest</Title>
          <ISOAbbreviation>Chest</ISOAbbreviation>
          <JournalIssue>
            <PubDate><Year>2005</Year><Month>Dec</Month></PubDate>
          </JournalIssue>
        </Journal>
        <ArticleTitle>Omega-3 fatty acids in COPD.</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Chronic obstructive
            pulmonary disease involves inflammation.</AbstractText>
          <AbstractText Label="METHODS">A randomized trial.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author>
            <LastName>Matsuyama</LastName>
            <ForeName>Wataru</ForeName>
            <Initials>W</Initials>
          </Author>
          <Author>
            <CollectiveName>COPD Study Group</CollectiveName>
          </Author>
        </AuthorList>
        <ELocationID EIdType="doi" ValidYN="Y">10.1378/chest.128.6.3817</ELocationID>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">16354850</ArticleId>
        <ArticleId IdType="doi">10.1378/chest.128.6.3817</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""

# Spike §5: a gone/nonexistent PMID returns HTTP 200 with this exact empty set.
DELETED_XML = "<?xml version=\"1.0\" ?>\n<PubmedArticleSet></PubmedArticleSet>\n"

# Spike §5: a malformed id (e.g. "0") returns HTTP 400 with this shape.
ERROR_XML = (
    "<?xml version=\"1.0\" ?>\n"
    "<eFetchResult><ERROR>ID list is empty! Possibly it has no correct IDs.</ERROR>"
    "</eFetchResult>\n"
)


def _record(pmid, *, title="A title", authors="", abstract="", doi_article="",
            doi_eloc="", pubdate="<Year>2020</Year>", journal="Some Journal"):
    """Build one <PubmedArticle> with the parts a case needs; omit the rest."""
    author_xml = ""
    if authors:
        author_xml = "<AuthorList>" + "".join(
            f"<Author><LastName>{a}</LastName><ForeName>Jo</ForeName></Author>"
            for a in authors.split(",")
        ) + "</AuthorList>"
    abstract_xml = f"<Abstract><AbstractText>{abstract}</AbstractText></Abstract>" if abstract else ""
    eloc_xml = f'<ELocationID EIdType="doi">{doi_eloc}</ELocationID>' if doi_eloc else ""
    article_id = f'<ArticleId IdType="doi">{doi_article}</ArticleId>' if doi_article else ""
    return f"""
  <PubmedArticle>
    <MedlineCitation>
      <PMID>{pmid}</PMID>
      <Article>
        <Journal><Title>{journal}</Title>
          <JournalIssue><PubDate>{pubdate}</PubDate></JournalIssue></Journal>
        <ArticleTitle>{title}</ArticleTitle>
        {abstract_xml}{author_xml}{eloc_xml}
      </Article>
    </MedlineCitation>
    <PubmedData><ArticleIdList>
      <ArticleId IdType="pubmed">{pmid}</ArticleId>{article_id}
    </ArticleIdList></PubmedData>
  </PubmedArticle>"""


def _set(*records):
    return "<PubmedArticleSet>" + "".join(records) + "</PubmedArticleSet>"


# --- present record: every field the writer needs -------------------------


def test_present_record_reads_every_field():
    outcome = parse_efetch_response(PRESENT_XML, "16354850")
    assert outcome.deleted == ()
    assert outcome.merged == ()
    assert len(outcome.present) == 1
    record = outcome.present[0]
    assert isinstance(record, PresentRecord)
    assert record.requested_pmid == "16354850"
    work = record.work
    assert work.pmid == "16354850"
    assert work.title == "Omega-3 fatty acids in COPD."
    assert work.journal == "Chest"
    assert work.year == 2005
    assert work.pub_date_raw is None
    assert work.doi == "10.1378/chest.128.6.3817"
    # Structured abstract segments concatenate; whitespace is collapsed.
    assert work.abstract == (
        "Chronic obstructive pulmonary disease involves inflammation. "
        "A randomized trial."
    )
    assert work.has_abstract
    # "Given Family" order, plus the CollectiveName group author.
    assert work.authors == ("Wataru Matsuyama", "COPD Study Group")


def test_journal_falls_back_to_iso_abbreviation():
    xml = _set(_record("1", journal="").replace(
        "<Journal><Title></Title>",
        "<Journal><ISOAbbreviation>J Clin</ISOAbbreviation>",
    ))
    outcome = parse_efetch_response(xml, "1")
    assert outcome.present[0].work.journal == "J Clin"


def test_doi_read_from_elocationid_when_articleid_absent():
    # Spike §6: DOI can live in ELocationID only.
    xml = _set(_record("7", doi_eloc="10.1000/xyz", doi_article=""))
    assert parse_efetch_response(xml, "7").present[0].work.doi == "10.1000/xyz"


def test_doi_read_from_articleid_when_elocation_absent():
    xml = _set(_record("7", doi_article="10.1000/abc", doi_eloc=""))
    assert parse_efetch_response(xml, "7").present[0].work.doi == "10.1000/abc"


# --- absence is data, not error (spike §6) --------------------------------


def test_record_with_no_authors_abstract_or_doi_parses():
    # The Done-when case: none of the three optional fields, no exception.
    xml = _set(_record("42", authors="", abstract="", doi_article="", doi_eloc=""))
    work = parse_efetch_response(xml, "42").present[0].work
    assert work.pmid == "42"
    assert work.authors == ()
    assert work.abstract == ""
    assert not work.has_abstract
    assert work.doi is None
    assert work.title == "A title"


# --- PubDate: Year / MedlineDate / neither (three shapes) -----------------


def test_pubdate_year():
    work = parse_efetch_response(_set(_record("1", pubdate="<Year>1999</Year>")), "1").present[0].work
    assert work.year == 1999
    assert work.pub_date_raw is None


def test_pubdate_medline_date_range_extracts_first_year():
    # Free-text range: the year is the first four-digit run; the raw text is kept
    # so a derived year is auditable rather than silent.
    work = parse_efetch_response(
        _set(_record("2", pubdate="<MedlineDate>1998 Dec-1999 Jan</MedlineDate>")), "2"
    ).present[0].work
    assert work.year == 1998
    assert work.pub_date_raw == "1998 Dec-1999 Jan"


def test_pubdate_absent_yields_none_year_not_exception():
    work = parse_efetch_response(_set(_record("3", pubdate="")), "3").present[0].work
    assert work.year is None
    assert work.pub_date_raw is None


def test_pubdate_medline_season_extracts_year():
    work = parse_efetch_response(
        _set(_record("4", pubdate="<MedlineDate>Winter 1985</MedlineDate>")), "4"
    ).present[0].work
    assert work.year == 1985
    assert work.pub_date_raw == "Winter 1985"


# --- deleted vs merged vs present, kept distinct --------------------------


def test_deleted_pmid_is_a_value_not_an_exception():
    # Spike §5: gone PMID -> empty set. It is `deleted`, and no record is present.
    outcome = parse_efetch_response(DELETED_XML, "999999999")
    assert outcome == PubMedFetchOutcome(deleted=("999999999",))
    assert outcome.present == ()
    assert outcome.merged == ()
    assert outcome.works == ()


def test_malformed_id_error_body_raises_distinct_from_deleted():
    # Spike §5: malformed id -> <ERROR> body. That is a caller bug (raise), NOT a
    # gone PMID (a `deleted` value). The two are categorically different.
    with pytest.raises(PubMedParseError, match="ID list is empty"):
        parse_efetch_response(ERROR_XML, "0")


def test_network_failure_shape_is_distinct_from_deleted():
    # A network failure never reaches the parser (the client raises), but if a
    # non-PubMed body is handed in it raises rather than returning a `deleted`
    # value — so deleted (a value) and failure (an exception) never collide.
    with pytest.raises(PubMedParseError):
        parse_efetch_response("<html>503 Service Unavailable</html>", "16354850")
    with pytest.raises(PubMedParseError):
        parse_efetch_response("not xml at all", "16354850")


def test_merged_single_request_exposes_both_pmids():
    # Request X, receive a record under Y: an unambiguous merge. Both PMIDs are
    # exposed; the requested PMID is merged, NOT deleted.
    xml = _set(_record("20493475"))  # returned PMID is 20493475
    outcome = parse_efetch_response(xml, "11111111")  # but we asked for 11111111
    assert outcome.deleted == ()
    assert outcome.present == ()
    assert len(outcome.merged) == 1
    merged = outcome.merged[0]
    assert isinstance(merged, MergedRecord)
    assert merged.requested_pmid == "11111111"
    assert merged.returned_pmid == "20493475"
    assert merged.work.pmid == "20493475"


def test_batch_present_and_deleted_are_separated():
    # Spike §4: efetch drops absent ids by omission. Requesting two, receiving
    # one, the missing id is `deleted`, the returned one `present`.
    xml = _set(_record("16354850"), _record("33301246"))
    outcome = parse_efetch_response(xml, ["16354850", "999999999", "33301246"])
    assert {r.requested_pmid for r in outcome.present} == {"16354850", "33301246"}
    assert outcome.deleted == ("999999999",)
    assert outcome.merged == ()


def test_batch_ambiguous_unexpected_record_is_merged_without_a_requester():
    # Two requests unanswered AND an unexpected record: the pairing is unknowable,
    # so the unexpected record is merged with requested_pmid=None and both
    # unanswered requests stay deleted rather than being falsely paired.
    xml = _set(_record("77777777"))  # not among requested
    outcome = parse_efetch_response(xml, ["11111111", "22222222"])
    assert set(outcome.deleted) == {"11111111", "22222222"}
    assert len(outcome.merged) == 1
    assert outcome.merged[0].requested_pmid is None
    assert outcome.merged[0].returned_pmid == "77777777"


# --- defensive parsing edges ----------------------------------------------


def test_unexpected_root_raises():
    with pytest.raises(PubMedParseError, match="unexpected efetch root"):
        parse_efetch_response("<esearchResult><Count>0</Count></esearchResult>", "1")


def test_record_without_pmid_raises():
    xml = "<PubmedArticleSet><PubmedArticle><MedlineCitation></MedlineCitation></PubmedArticle></PubmedArticleSet>"
    with pytest.raises(PubMedParseError, match="no PMID"):
        parse_efetch_response(xml, "1")


def test_non_string_body_raises():
    with pytest.raises(PubMedParseError, match="must be a string"):
        parse_efetch_response(None, "1")


def test_doi_normalized_and_junk_dropped():
    # A DOI wrapped as a URL is reduced to bare form; a non-DOI ELocationID (pii)
    # is dropped rather than recorded.
    xml = _set(_record("9", doi_article="https://doi.org/10.1/AB", doi_eloc=""))
    assert parse_efetch_response(xml, "9").present[0].work.doi == "10.1/ab"
    xml2 = _set(_record("9", doi_article="", doi_eloc="S0140-6736(20)30183-5"))
    assert parse_efetch_response(xml2, "9").present[0].work.doi is None


def test_parsed_work_is_hashable_and_frozen():
    work = ParsedPubMedWork(pmid="1")
    assert hash(work) is not None
    with pytest.raises(Exception):
        work.pmid = "2"  # type: ignore[misc]
