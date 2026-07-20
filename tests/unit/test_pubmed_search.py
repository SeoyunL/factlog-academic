# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the PubMed search composer and silent-zero guard (#167).

Pure functions over strings and an ``esearch`` XML body — no network. The
``--year`` range check (#387) additionally reads *efetch* fixtures through the real
``work_parser``: what it compares against ``--year`` is the year that would reach
front matter, and a hand-built work object would assert that year rather than derive
it — the fixture must carry ``ArticleDate`` and ``JournalIssue/PubDate`` as the
separate fields they are, since their disagreement is the whole subject. The fixtures
are shaped like the ``eSearchResult`` envelope E-utilities returns, including the
``ErrorList``/``WarningList`` PubMed volunteers when it cannot map a phrase or
field. The tests take no network, but the zero-count fixtures are *captured* response
bodies, not bodies inferred from E-utilities' documented envelope: following the
documentation alone is precisely what produced #271's drift — the docs never say a
zero always arrives with an ``<OutputMessage>``, and every real one does.
"""
from __future__ import annotations

from datetime import date

import pytest

from factlog.integrations.pubmed.search import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    PUBMED_EPOCH_YEAR,
    PubMedSearchValidationError,
    build_year_filter,
    compose_query,
    mesh_clause,
    parse_esearch,
    parse_year_range,
    silent_zero_report,
    validate_field_tags,
    year_range_report,
)
from factlog.integrations.pubmed.work_parser import parse_efetch_response


# -- field-tag validation: reject an unknown tag before a request -----------

class TestFieldTagValidation:
    def test_known_full_name_and_abbrev_pass(self):
        assert validate_field_tags("cancer[MeSH Terms]") == "cancer[MeSH Terms]"
        assert validate_field_tags("smith[au]") == "smith[au]"
        assert validate_field_tags("brca1[tiab] AND smith[Author]").startswith("brca1")

    def test_case_and_whitespace_insensitive(self):
        # PubMed matches tags case-insensitively; a valid tag in any case passes.
        assert validate_field_tags("x[mesh terms]")
        assert validate_field_tags("x[TITLE/ABSTRACT]")

    def test_a_subqualifier_validates_only_the_field(self):
        # `[mh:noexp]` names the field `mh` with a modifier PubMed reads; the field
        # is what is validated, not the modifier.
        assert validate_field_tags("crispr[mh:noexp]")

    def test_unknown_tag_is_rejected(self):
        with pytest.raises(PubMedSearchValidationError) as exc:
            validate_field_tags("foo[NotARealTag]")
        assert "NotARealTag" in str(exc.value)

    def test_an_unfielded_query_passes_untouched(self):
        assert validate_field_tags("chain of thought") == "chain of thought"

    @pytest.mark.parametrize("tag", [
        "Date - Publication", "Date - Create", "Date - Completion",
        "Date - Entry", "Date - Entrez", "Date - MeSH", "Date - Modification",
        "crdt", "dcom", "edat", "mhda", "lr", "dp",
    ])
    def test_date_field_full_names_and_abbrevs_pass(self, tag):
        # A query copied out of PubMed's Advanced Search builder uses the "Date - X"
        # full names; each must pass alongside its abbreviation or it false-rejects.
        assert validate_field_tags(f"2020[{tag}]")

    def test_empty_query_is_rejected(self):
        with pytest.raises(PubMedSearchValidationError):
            validate_field_tags("   ")


# -- multi-word query semantics: sent verbatim, NOT auto-quoted (#89) -------

class TestMultiWordQuery:
    def test_bare_multiword_is_passed_through_verbatim(self):
        # Unlike arXiv, PubMed's Automatic Term Mapping reads the words; factlog
        # does not auto-quote (that would disable ATM and can zero a real query).
        assert compose_query("chain of thought") == "chain of thought"

    def test_a_user_quoted_phrase_is_preserved(self):
        assert compose_query('"chain of thought"') == '"chain of thought"'

    def test_filters_are_and_combined_onto_the_query(self):
        composed = compose_query("crispr", year="2020-2021", mesh=["CRISPR-Cas Systems"])
        assert composed == (
            'crispr AND CRISPR-Cas Systems[MeSH Terms] AND '
            '("2020"[Date - Publication] : "2021"[Date - Publication])'
        )


# -- --mesh composition -----------------------------------------------------

class TestMeshClause:
    def test_term_is_tagged_mesh_terms(self):
        assert mesh_clause("Neoplasms") == "Neoplasms[MeSH Terms]"

    def test_quote_in_term_is_refused(self):
        with pytest.raises(PubMedSearchValidationError):
            mesh_clause('bad"quote')

    def test_empty_term_is_refused(self):
        with pytest.raises(PubMedSearchValidationError):
            mesh_clause("  ")


# -- --year filter ----------------------------------------------------------

class TestYearFilter:
    def test_single_year(self):
        assert build_year_filter("2020") == (
            '("2020"[Date - Publication] : "2020"[Date - Publication])'
        )

    def test_range(self):
        assert build_year_filter("2010-2015") == (
            '("2010"[Date - Publication] : "2015"[Date - Publication])'
        )

    def test_reversed_range_is_rejected(self):
        with pytest.raises(PubMedSearchValidationError) as exc:
            build_year_filter("2015-2010")
        assert "backwards" in str(exc.value)

    def test_out_of_range_year_is_rejected(self):
        with pytest.raises(PubMedSearchValidationError):
            build_year_filter(str(PUBMED_EPOCH_YEAR - 1))
        with pytest.raises(PubMedSearchValidationError):
            build_year_filter(str(date.today().year + 2))

    def test_garbage_is_rejected(self):
        with pytest.raises(PubMedSearchValidationError):
            build_year_filter("last tuesday")

    def test_parse_year_range_returns_the_validated_bounds(self):
        # The clause builder and the range check must read --year through one
        # parser; the split that produced #387 is what this keeps from recurring.
        assert parse_year_range("2020") == (2020, 2020)
        assert parse_year_range("2022-2025") == (2022, 2025)


# -- --year vs. the recorded year (#387) ------------------------------------

def _efetch_record(pmid: str, *, issue_year: str, article_date: str | None = None) -> str:
    """One `<PubmedArticle>` shaped like the efetch body PMID 41620285 returns.

    `<ArticleDate DateType="Electronic">` is the field PubMed's [Date - Publication]
    filter matches; `Journal/JournalIssue/PubDate/Year` is the one `work_parser`
    writes to front matter. A `PubModel="Print-Electronic"` record carries both and
    they disagree — that disagreement is the whole subject of these tests, so the
    fixture keeps them as separate elements rather than asserting on a hand-built
    work object.
    """
    article_date_xml = ""
    if article_date is not None:
        year, month, day = article_date.split("-")
        article_date_xml = (
            f'<ArticleDate DateType="Electronic"><Year>{year}</Year>'
            f"<Month>{month}</Month><Day>{day}</Day></ArticleDate>"
        )
    return f"""
  <PubmedArticle>
    <MedlineCitation>
      <PMID>{pmid}</PMID>
      <Article PubModel="Print-Electronic">
        <Journal><Title>J Test</Title>
          <JournalIssue CitedMedium="Internet">
            <PubDate><Year>{issue_year}</Year></PubDate>
          </JournalIssue></Journal>
        <ArticleTitle>Base editing in T cells.</ArticleTitle>
        {article_date_xml}
      </Article>
    </MedlineCitation>
    <PubmedData><ArticleIdList>
      <ArticleId IdType="pubmed">{pmid}</ArticleId>
    </ArticleIdList></PubmedData>
  </PubmedArticle>"""


def _medline_record(pmid: str, *, medline_date: str) -> str:
    """A record whose issue carries `<MedlineDate>` free text instead of a `<Year>`.

    No `ArticleDate`, no `PubModel="Print-Electronic"` — the electronic-date story is
    simply false here. `work_parser` records the span's *first* year and keeps the raw
    text in `pub_date_raw`; a span like "1998 Dec-1999 Jan" therefore records 1998
    while PubMed matched the 1999 half.
    """
    return f"""
  <PubmedArticle>
    <MedlineCitation>
      <PMID>{pmid}</PMID>
      <Article>
        <Journal><Title>J Test</Title>
          <JournalIssue>
            <PubDate><MedlineDate>{medline_date}</MedlineDate></PubDate>
          </JournalIssue></Journal>
        <ArticleTitle>A winter issue.</ArticleTitle>
      </Article>
    </MedlineCitation>
    <PubmedData><ArticleIdList>
      <ArticleId IdType="pubmed">{pmid}</ArticleId>
    </ArticleIdList></PubmedData>
  </PubmedArticle>"""


def _works(*records):
    """Parse fixture records through the real parser, so `.year` is the recorded year."""
    xml = "<PubmedArticleSet>" + "".join(records) + "</PubmedArticleSet>"
    ids = tuple(r.split("<PMID>")[1].split("</PMID>")[0] for r in records)
    return parse_efetch_response(xml, ids).works


class TestYearRangeReport:
    def test_issue_year_past_the_range_is_surfaced(self):
        # The measured case from #387: matched on ArticleDate 2025-04-16, recorded
        # as the issue year 2026. Genuinely in range for PubMed, out of range for
        # the KB — so the operator is told before it lands.
        works = _works(_efetch_record("41620285", issue_year="2026",
                                      article_date="2025-04-16"))
        lines = year_range_report(works, year="2022-2025")
        assert len(lines) == 1
        assert "41620285" in lines[0]
        assert "2026" in lines[0]
        assert "2022-2025" in lines[0]

    def test_the_warning_explains_why_the_years_differ(self):
        # A bare "out of range" line reads as a factlog bug. The explanation is the
        # point of surface-and-explain: name the electronic/issue date split.
        works = _works(_efetch_record("41620285", issue_year="2026",
                                      article_date="2025-04-16"))
        line = year_range_report(works, year="2022-2025")[0]
        assert "electronic" in line
        assert "journal issue" in line

    def test_a_medline_span_is_never_blamed_on_an_electronic_date(self):
        # The counterexample that keeps the explanation honest: this record has no
        # ArticleDate and is not Print-Electronic. Its year is out of range because
        # `_pub_date` took the FIRST year of a MedlineDate span. Blaming an
        # electronic publication date would send the operator hunting for a field
        # the record does not carry — a confidently wrong explanation.
        works = _works(_medline_record("1", medline_date="1998 Dec-1999 Jan"))
        assert works[0].year == 1998
        assert works[0].pub_date_raw == "1998 Dec-1999 Jan"
        line = year_range_report(works, year="1999")[0]
        assert "electronic" not in line
        assert "MedlineDate" in line
        # The raw span is quoted, so the derived year stays auditable — the stated
        # purpose of `pub_date_raw` in work_parser.
        assert "1998 Dec-1999 Jan" in line

    def test_each_cause_gets_its_own_block(self):
        # Both causes in one result set: two blocks, each explaining only its own
        # records. Neither explanation may attach to the other's PMID.
        works = _works(
            _efetch_record("41620285", issue_year="2026", article_date="2025-04-16"),
            _medline_record("1", medline_date="1998 Dec-1999 Jan"),
        )
        lines = year_range_report(works, year="2000-2025")
        assert len(lines) == 2
        electronic = next(line for line in lines if "41620285" in line)
        medline = next(line for line in lines if "PMID 1 " in line)
        assert "electronic" in electronic and "MedlineDate" not in electronic
        assert "MedlineDate" in medline and "electronic" not in medline

    def test_a_record_inside_the_range_stays_silent(self):
        # The counterexample: an in-range issue year says nothing, whatever the
        # ArticleDate. A warning on every result would be noise, not a signal.
        works = _works(_efetch_record("40000001", issue_year="2024",
                                      article_date="2023-11-02"))
        assert year_range_report(works, year="2022-2025") == []

    def test_only_the_out_of_range_records_are_named(self):
        works = _works(
            _efetch_record("40000001", issue_year="2024", article_date="2023-11-02"),
            _efetch_record("41620285", issue_year="2026", article_date="2025-04-16"),
            _efetch_record("40000002", issue_year="2021", article_date="2022-01-09"),
        )
        lines = year_range_report(works, year="2022-2025")
        # One block per cause, not one per record: both share the electronic cause.
        assert len(lines) == 1
        assert "41620285" in lines[0] and "40000002" in lines[0]
        assert "40000001" not in lines[0]

    def test_the_explanation_is_printed_once_per_block(self):
        # At --limit 25 a per-record paragraph would fill the screen and bury the
        # other things stderr carries that run (retractions, the silent-zero guard).
        works = _works(*[
            _efetch_record(f"4000000{n}", issue_year="2026", article_date="2025-04-16")
            for n in range(1, 6)
        ])
        lines = year_range_report(works, year="2022-2025")
        assert len(lines) == 1
        assert lines[0].count("electronic publication date") == 1
        assert lines[0].startswith("⚠ 5 results")

    def test_a_single_record_is_counted_in_the_singular(self):
        works = _works(_efetch_record("41620285", issue_year="2026",
                                      article_date="2025-04-16"))
        assert year_range_report(works, year="2022-2025")[0].startswith("⚠ 1 result will")

    def test_a_single_year_spec_is_a_range_of_one(self):
        works = _works(_efetch_record("41620285", issue_year="2026",
                                      article_date="2025-04-16"))
        assert year_range_report(works, year="2025") != []
        assert year_range_report(works, year="2026") == []

    def test_no_year_filter_means_nothing_to_check(self):
        # Without --year the operator asked for no range, so no year can be outside it.
        works = _works(_efetch_record("41620285", issue_year="2026",
                                      article_date="2025-04-16"))
        assert year_range_report(works, year=None) == []
        assert year_range_report(works) == []

    def test_a_record_without_a_year_is_not_reported(self):
        # Absence is not a range mismatch; work_parser already accounts for a
        # PubDate with no parseable year.
        xml = ("<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>40000003</PMID>"
               "<Article><Journal><Title>J Test</Title><JournalIssue><PubDate/>"
               "</JournalIssue></Journal><ArticleTitle>No date.</ArticleTitle>"
               "</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>")
        works = parse_efetch_response(xml, ("40000003",)).works
        assert works[0].year is None
        assert year_range_report(works, year="2022-2025") == []

    def test_an_unparseable_year_spec_yields_no_second_complaint(self):
        # The CLI rejects a bad --year before spending a request; this must not add
        # a contradictory line on top of that rejection.
        works = _works(_efetch_record("41620285", issue_year="2026",
                                      article_date="2025-04-16"))
        assert year_range_report(works, year="last tuesday") == []

    def test_an_empty_result_set_is_silent(self):
        assert year_range_report((), year="2022-2025") == []


# -- reading esearch back ---------------------------------------------------

def _esearch(count=0, ids=(), query_translation=None, errors=(), warnings=(),
             top_error=None):
    parts = [f"<Count>{count}</Count>"]
    parts.append("<IdList>" + "".join(f"<Id>{i}</Id>" for i in ids) + "</IdList>")
    if query_translation is not None:
        parts.append(f"<QueryTranslation>{query_translation}</QueryTranslation>")
    if errors:
        parts.append("<ErrorList>" + "".join(
            f"<{tag}>{text}</{tag}>" for tag, text in errors) + "</ErrorList>")
    if warnings:
        parts.append("<WarningList>" + "".join(
            f"<{tag}>{text}</{tag}>" for tag, text in warnings) + "</WarningList>")
    body = "".join(parts)
    if top_error is not None:
        return f"<eSearchResult><ERROR>{top_error}</ERROR>{body}</eSearchResult>"
    return f"<eSearchResult>{body}</eSearchResult>"


# What NCBI actually sends for *every* zero: the count, and boilerplate. A zero-count
# body without it is a shape PubMed never returns (#271).
_NCBI_ZERO_BOILERPLATE = [("OutputMessage", "No items found.")]

# A live capture: `sepsis` + a valid MeSH descriptor `Sepsis` + year 1810 — a real
# filter that legitimately narrows to zero. Kept as the raw body NCBI returned so the
# guard is asserted against what PubMed sends, not against a synthetic shape that
# omits the boilerplate every zero carries.
_LIVE_VALID_MESH_ZERO = (
    "<eSearchResult><Count>0</Count><RetMax>0</RetMax><RetStart>0</RetStart><IdList/>"
    "<TranslationSet><Translation>     <From>sepsis</From>     "
    '<To>"sepsis"[MeSH Terms] OR "sepsis"[All Fields]</To>    </Translation>'
    "<Translation>     <From>Sepsis[MeSH Terms]</From>     "
    '<To>"sepsis"[MeSH Terms]</To>    </Translation></TranslationSet>'
    '<QueryTranslation>("sepsis"[MeSH Terms] OR "sepsis"[All Fields]) AND '
    '"sepsis"[MeSH Terms] AND 1810/01/01:1810/12/31[Date - Publication]'
    "</QueryTranslation><WarningList><OutputMessage>No items found.</OutputMessage>"
    "</WarningList></eSearchResult>"
)

# A live capture: `--query qzxwvunonsenseterm`, sent WITHOUT quotes. PubMed quotes the
# phrase in its own warning; QueryTranslation shows no quotes, so ATM was never off (#272).
_LIVE_UNQUOTED_NONSENSE_ZERO = (
    "<eSearchResult><Count>0</Count><RetMax>0</RetMax><RetStart>0</RetStart><IdList/>"
    "<TranslationSet/>"
    "<QueryTranslation>qzxwvunonsenseterm</QueryTranslation>"
    "<WarningList><QuotedPhraseNotFound>\"qzxwvunonsenseterm\"</QuotedPhraseNotFound>"
    "<OutputMessage>No items found.</OutputMessage></WarningList></eSearchResult>"
)


class TestParseEsearch:
    def test_count_and_ids(self):
        r = parse_esearch(_esearch(count=3, ids=("1", "2", "3")))
        assert r.count == 3
        assert r.ids == ("1", "2", "3")

    def test_query_translation_is_captured(self):
        r = parse_esearch(_esearch(count=1, ids=("1",), query_translation="crispr[All Fields]"))
        assert r.query_translation == "crispr[All Fields]"

    def test_error_and_warning_lists_are_captured(self):
        r = parse_esearch(_esearch(
            errors=[("PhraseNotFound", "xyzzy")],
            warnings=[("QuotedPhraseNotFound", '"not a phrase"')],
        ))
        assert r.errors == (("PhraseNotFound", "xyzzy"),)
        assert r.warnings == (("QuotedPhraseNotFound", '"not a phrase"'),)

    def test_top_level_error_is_surfaced_not_raised(self):
        r = parse_esearch(_esearch(top_error="Invalid db name"))
        assert r.top_level_error == "Invalid db name"

    def test_parse_esearch_still_captures_output_message_verbatim(self):
        # The parser stays a faithful reduction of the response: it records what NCBI
        # sent. Judging what counts as a diagnostic is the policy layer's job, not the
        # parser's (#271).
        r = parse_esearch(_LIVE_VALID_MESH_ZERO)
        assert r.warnings == (("OutputMessage", "No items found."),)
        assert r.count == 0

    def test_unparseable_body_is_reported_not_raised(self):
        r = parse_esearch("this is not xml <")
        assert r.top_level_error is not None
        assert r.count == 0


# -- the silent-zero guard --------------------------------------------------

class TestSilentZeroGuard:
    def test_nonexistent_mesh_term_surfaces_phrase_not_found(self):
        # The Done-when: a nonexistent MeSH term is a surfaced warning, not a bare 0.
        r = parse_esearch(_esearch(count=0, errors=[("PhraseNotFound", "notamesh")]))
        lines = silent_zero_report(r, mesh=["notamesh"])
        assert lines
        assert "notamesh" in lines[0]
        assert "nonexistent MeSH term" in lines[0]

    def test_quoted_phrase_not_found_is_surfaced(self):
        r = parse_esearch(_esearch(count=0, warnings=[("QuotedPhraseNotFound", '"foo bar"')]))
        lines = silent_zero_report(r)
        assert any("foo bar" in ln for ln in lines)
        # Without the query, nothing is known about the user's input — so no prescription
        # to undo something they may never have done (#272).
        assert not any("drop the quotes" in ln for ln in lines)

    def test_unquoted_query_is_never_told_to_drop_quotes(self):
        # The live body: PubMed quoted the phrase in its own warning even though the
        # query carried no quotes. Advising "drop the quotes" here is unactionable.
        r = parse_esearch(_LIVE_UNQUOTED_NONSENSE_ZERO)
        lines = silent_zero_report(r, query="qzxwvunonsenseterm")
        assert any("qzxwvunonsenseterm" in ln for ln in lines)
        assert not any("drop the quotes" in ln for ln in lines)
        assert not any("disables Automatic Term Mapping" in ln for ln in lines)

    def test_quoted_query_keeps_the_atm_advice(self):
        # The user *did* quote: quoting disables ATM, so the advice is true and useful.
        r = parse_esearch(_esearch(count=0, warnings=[("QuotedPhraseNotFound", '"gene therapy"')]))
        lines = silent_zero_report(r, query='"gene therapy"')
        assert any("disables Automatic Term Mapping" in ln for ln in lines)
        assert any("drop the quotes" in ln for ln in lines)

    def test_quoted_query_never_denies_that_the_user_quoted(self):
        # The line must not tell a user who *did* quote that the quotes are PubMed's own
        # and "do not mean the query was quoted" — that would be, in the quoted branch,
        # exactly the false assertion about the user's input #272 exists to remove.
        r = parse_esearch(_esearch(count=0, warnings=[("QuotedPhraseNotFound", '"gene therapy"')]))
        lines = silent_zero_report(r, query='"gene therapy"')
        assert lines
        assert not any("do not mean the query was quoted" in ln for ln in lines)
        assert not any("PubMed's own" in ln for ln in lines)

    def test_advice_attaches_only_to_the_phrase_the_user_actually_quoted(self):
        # A query can quote one phrase and leave another bare. The advice is owed only
        # for the phrase PubMed *named*: telling the operator to unquote `foo` — which
        # they never quoted — is the original bug wearing a different query.
        r = parse_esearch(_esearch(count=0, warnings=[("QuotedPhraseNotFound", '"foo"')]))
        lines = silent_zero_report(r, query='"gene therapy" AND foo')
        assert any("foo" in ln for ln in lines)
        assert not any("drop the quotes" in ln for ln in lines)

    def test_advice_attaches_when_the_named_phrase_is_the_quoted_one(self):
        # Same query, but now PubMed names the phrase the user *did* quote.
        r = parse_esearch(_esearch(count=0, warnings=[("QuotedPhraseNotFound", '"gene therapy"')]))
        lines = silent_zero_report(r, query='"gene therapy" AND foo')
        assert any("drop the quotes" in ln for ln in lines)

    def test_unknown_query_asserts_nothing_about_the_users_input(self):
        # No query passed: the caller told us nothing, so the line states only the fact
        # PubMed reported and asserts nothing about what the user typed.
        r = parse_esearch(_LIVE_UNQUOTED_NONSENSE_ZERO)
        lines = silent_zero_report(r)
        assert any("qzxwvunonsenseterm" in ln for ln in lines)
        assert not any("drop the quotes" in ln for ln in lines)

    def test_filtered_zero_names_the_filter_through_ncbi_boilerplate(self):
        # Every real zero carries `OutputMessage: No items found.` The filter line must
        # still fire through it, and the boilerplate itself must never be echoed —
        # it says nothing "Found 0 results." did not already say.
        r = parse_esearch(_esearch(count=0, warnings=_NCBI_ZERO_BOILERPLATE))
        lines = silent_zero_report(r, year="2020", mesh=["Foo"])
        assert any("'Foo'" in ln and "'2020'" in ln for ln in lines)
        assert not any("OutputMessage" in ln for ln in lines)

    def test_valid_mesh_filtered_zero_names_the_filter(self):
        # The live NCBI body for a *valid* MeSH term filtered to zero: no diagnostic
        # signal, only boilerplate. The filter must still be named (#271).
        r = parse_esearch(_LIVE_VALID_MESH_ZERO)
        lines = silent_zero_report(r, year="1810", mesh=["Sepsis"])
        assert any("'Sepsis'" in ln and "'1810'" in ln for ln in lines)
        assert not any("OutputMessage" in ln or "No items found" in ln for ln in lines)

    def test_nonexistent_mesh_zero_surfaces_both_the_signal_and_the_filter(self):
        # A nonexistent MeSH term gives PhraseNotFound *and* the boilerplate. Both the
        # diagnostic and the filter line are owed, diagnostic first.
        r = parse_esearch(_esearch(
            count=0,
            errors=[("PhraseNotFound", "notamesh")],
            warnings=_NCBI_ZERO_BOILERPLATE,
        ))
        lines = silent_zero_report(r, mesh=["notamesh"])
        assert "nonexistent MeSH term" in lines[0]
        assert any("a filter was applied" in ln for ln in lines)
        assert not any("OutputMessage" in ln for ln in lines)

    def test_output_message_is_never_echoed(self):
        # A nonsense query with no filter: PubMed volunteers QuotedPhraseNotFound and
        # the boilerplate. The diagnostic is surfaced; the boilerplate never is.
        r = parse_esearch(_esearch(
            count=0,
            warnings=[("QuotedPhraseNotFound", '"zzz qqq"')] + _NCBI_ZERO_BOILERPLATE,
        ))
        lines = silent_zero_report(r)
        assert any("zzz qqq" in ln for ln in lines)
        assert not any("OutputMessage" in ln for ln in lines)

    def test_output_message_at_a_nonzero_count_is_surfaced(self):
        # The counterexample. OutputMessage is boilerplate only *because* the count is
        # zero. Attached to a non-zero count it would be telling us something the count
        # does not, so it must not be swallowed — an unconditional suppression would
        # re-open the very failure mode the guard exists to close.
        r = parse_esearch(_esearch(
            count=5, ids=("1",), warnings=[("OutputMessage", "Something unexpected.")]))
        lines = silent_zero_report(r)
        assert any("Something unexpected." in ln for ln in lines)

    def test_honest_empty_set_stays_silent(self):
        # Zero results, no filter, no diagnostic signal — only the boilerplate every
        # zero carries: a plain "0 results" is honest and must stay quiet.
        r = parse_esearch(_esearch(count=0, warnings=_NCBI_ZERO_BOILERPLATE))
        assert silent_zero_report(r) == []

    def test_nonzero_count_with_a_pubmed_warning_still_surfaces_it(self):
        # PubMed dropped part of the query even though something matched — say so.
        r = parse_esearch(_esearch(count=5, ids=("1",), warnings=[("PhraseIgnored", "AND")]))
        lines = silent_zero_report(r)
        assert any("PhraseIgnored" in ln or "AND" in ln for ln in lines)

    def test_top_level_error_is_reported(self):
        r = parse_esearch(_esearch(top_error="Invalid db name"))
        lines = silent_zero_report(r)
        assert any("Invalid db name" in ln for ln in lines)


def test_limit_constants_are_sane():
    assert DEFAULT_LIMIT == 25
    assert MAX_LIMIT == 200
