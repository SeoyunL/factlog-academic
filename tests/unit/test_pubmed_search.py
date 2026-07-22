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


def _no_year_record(pmid: str, *, pub_date: str = "<PubDate/>") -> str:
    """A record whose `<PubDate>` yields no year at all — `_pub_date` returns `None`.

    Three shapes reach this state and all three are real PubMed data: an empty
    `<PubDate/>`, a `<PubDate>` holding only a `<Season>` (which `_pub_date` does not
    read), and a `<MedlineDate>` whose free text carries no four-digit run ("Winter").
    The last one still sets `pub_date_raw`, so the no-year warning can quote it.
    """
    return f"""
  <PubmedArticle>
    <MedlineCitation>
      <PMID>{pmid}</PMID>
      <Article>
        <Journal><Title>J Test</Title>
          <JournalIssue>{pub_date}</JournalIssue></Journal>
        <ArticleTitle>A dateless record.</ArticleTitle>
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

    def test_a_record_without_a_year_never_joins_a_range_mismatch_block(self):
        # Absence is not a range mismatch. The record IS reported (#389) but only in
        # its own block: claiming a year "outside 2022-2025" for a record that has no
        # year would be a fact the data does not carry.
        works = _works(_no_year_record("40000003"))
        assert works[0].year is None
        lines = year_range_report(works, year="2022-2025")
        assert len(lines) == 1
        assert "outside --year" not in lines[0]
        assert "no year at all" in lines[0]

    def test_an_unparseable_year_spec_yields_no_second_complaint(self):
        # The CLI rejects a bad --year before spending a request; this must not add
        # a contradictory line on top of that rejection.
        works = _works(_efetch_record("41620285", issue_year="2026",
                                      article_date="2025-04-16"))
        assert year_range_report(works, year="last tuesday") == []

    def test_an_empty_result_set_is_silent(self):
        assert year_range_report((), year="2022-2025") == []


class TestNoYearAtAllReport:
    """#389: a --year search must not silently accept records with no year.

    Distinct from the two blocks above in *claim*, not just wording: those say a year
    was recorded outside the range, this says no year was recorded at all. Asking for
    --year is asking to filter by year, so a year-less source landing without a word
    makes the run indistinguishable from one that passed no --year.
    """

    def test_an_empty_pub_date_is_surfaced(self):
        works = _works(_no_year_record("40000003"))
        assert works[0].year is None
        lines = year_range_report(works, year="2022-2025")
        assert len(lines) == 1
        assert "40000003" in lines[0]
        assert "no year at all" in lines[0]
        # The range is named, so the operator sees which request went unchecked.
        assert "2022-2025" in lines[0]

    def test_a_season_only_pub_date_is_surfaced(self):
        # `_pub_date` reads <Year> and <MedlineDate>; a <Season> sibling is not a year
        # and must not be mistaken for one.
        works = _works(_no_year_record("40000004", pub_date="<PubDate><Season>Winter</Season></PubDate>"))
        assert works[0].year is None
        lines = year_range_report(works, year="2022-2025")
        assert len(lines) == 1
        assert "40000004" in lines[0]
        assert "no year at all" in lines[0]

    def test_a_medline_date_with_no_four_digit_year_is_surfaced_and_quoted(self):
        # "Winter" sets pub_date_raw but yields no year. work_parser keeps that field
        # to make a "derived-or-absent" year auditable; this is the absent half, so
        # the text is quoted rather than dropped.
        works = _works(_no_year_record(
            "40000005", pub_date="<PubDate><MedlineDate>Winter</MedlineDate></PubDate>"))
        assert works[0].year is None
        assert works[0].pub_date_raw == "Winter"
        line = year_range_report(works, year="2022-2025")[0]
        assert "40000005" in line
        assert "no year at all" in line
        assert '"Winter"' in line

    def test_a_record_with_a_year_in_range_stays_silent(self):
        # The counterexample: a parseable, in-range year says nothing at all.
        works = _works(_efetch_record("40000001", issue_year="2024", article_date="2023-11-02"))
        assert year_range_report(works, year="2022-2025") == []

    def test_only_the_year_less_records_are_named(self):
        works = _works(
            _efetch_record("40000001", issue_year="2024", article_date="2023-11-02"),
            _no_year_record("40000003"),
        )
        lines = year_range_report(works, year="2022-2025")
        assert len(lines) == 1
        assert "40000003" in lines[0]
        assert "40000001" not in lines[0]

    def test_no_year_filter_means_nothing_to_check(self):
        # Without --year there is no range, so nothing about a record's year — present
        # or missing — was requested. Silence is correct here, not a miss.
        works = _works(_no_year_record("40000003"))
        assert year_range_report(works, year=None) == []
        assert year_range_report(works) == []

    def test_the_explanation_is_printed_once_per_block(self):
        works = _works(*[_no_year_record(f"4000000{n}") for n in range(1, 4)])
        lines = year_range_report(works, year="2022-2025")
        assert len(lines) == 1
        assert lines[0].startswith("⚠ 3 results")
        assert lines[0].count("is recorded with") == 1

    def test_a_single_record_is_counted_in_the_singular(self):
        works = _works(_no_year_record("40000003"))
        line = year_range_report(works, year="2022-2025")[0]
        assert line.startswith("⚠ 1 result will be recorded with no year at all")
        assert "against it)" in line

    def test_all_three_causes_get_their_own_block(self):
        # All three causes in one result set. The blocks must split three ways and no
        # explanation may attach to another cause's PMID: the electronic-date story is
        # false for a record with no ArticleDate, and the MedlineDate-span story is
        # false for a record whose PubDate is empty.
        works = _works(
            _efetch_record("41620285", issue_year="2026", article_date="2025-04-16"),
            _medline_record("1", medline_date="1998 Dec-1999 Jan"),
            _no_year_record("40000003"),
        )
        lines = year_range_report(works, year="2000-2025")
        assert len(lines) == 3
        electronic = next(line for line in lines if "41620285" in line)
        medline = next(line for line in lines if "PMID 1 " in line)
        unknown = next(line for line in lines if "40000003" in line)
        assert "electronic" in electronic and "MedlineDate" not in electronic
        assert "MedlineDate" in medline and "electronic" not in medline
        assert "no year at all" in unknown
        assert "electronic" not in unknown and "MedlineDate" not in unknown
        # The two range-mismatch blocks keep their claim; the third does not borrow it.
        assert "outside --year" in electronic and "outside --year" in medline
        assert "outside --year" not in unknown
        # No PMID appears in a block that is not about it.
        assert "40000003" not in electronic and "40000003" not in medline
        assert "41620285" not in unknown and "PMID 1 " not in unknown


class _Work:
    """A duck-typed stand-in for one parsed work — the contract `year_range_report` states.

    Deliberately NOT built through `parse_efetch_response`, and that is the subject of the
    tests below rather than a shortcut. `work_parser._text` whitespace-collapses every
    element it reads, so a newline inside a real `<MedlineDate>` is already gone by the
    time a parsed work exists (measured: `&#10;` in a fixture comes back as a space).
    Routing these through the fixtures would therefore assert *`_text`'s* behaviour and
    stay green with the gate deleted — a vacuous test.

    `year_range_report` documents itself as duck-typed over `.pmid`/`.year`/
    `.pub_date_raw` and imports no parser, so this IS an input it accepts. The collapse
    upstream is a display decision ("a non-empty, whitespace-collapsed string"), not a
    defence of this warning's block shape: whoever loosens it to preserve formatting has
    no reason to think about stderr, which is why the gate sits at emission (#396, and
    common.py's GATE PLACEMENT clause on why a gate nothing reaches today may still stay).
    """

    def __init__(self, pmid, year=None, pub_date_raw=None):
        self.pmid = pmid
        self.year = year
        self.pub_date_raw = pub_date_raw


# The forgery from #396: a MedlineDate carrying a newline plus a line that looks exactly
# like one of factlog's own stderr warnings. Read by a human, the second line is
# indistinguishable from something factlog said — it is the record's data.
_FORGED = "1998 Dec\n⚠ 99 results were silently dropped"


class TestQuotedTextCannotForgeAWarningLine:
    """#396: a block is one claim line plus one indented reason; data may not add lines.

    Each block interpolates caller-influenced text — the PMID in all three, the
    MedlineDate in two — and a CR/LF anywhere in it splits the block, so record bytes
    land where a factlog line belongs and the reader cannot tell them apart.
    """

    @staticmethod
    def _assert_block_shape(line):
        """Exactly two lines: the "⚠" claim, then the indented reason. Nothing else.

        Split with `splitlines()`, never `split("\\n")`. The first cut of this helper
        used `split("\\n")` and was *structurally blind* to U+0085/U+2028/U+2029 — the
        exact characters that cut's gate let through — so it reported a forged
        three-line block as two lines and passed. `splitlines()` recognizes every
        character a Python consumer would break a line on, which is the claim being
        made; matching the gate's own list here would only re-assert the gate.

        The claim is about *line structure*, not about the "⚠" character — a warning
        marker inside the quoted text forges nothing while it stays mid-line, and
        asserting it away would be asserting the gate deletes text (it does not).
        """
        rows = line.splitlines()
        assert len(rows) == 2
        assert rows[0].startswith("⚠")
        assert rows[1].startswith("  ") and not rows[1].lstrip().startswith("⚠")
        assert "\r" not in line

    def test_the_medline_block_stays_one_claim_line(self):
        line = year_range_report([_Work("40000003", 1998, _FORGED)], year="2022-2025")[0]
        self._assert_block_shape(line)
        # Sanitized, not dropped: the audit trail `pub_date_raw` exists for survives.
        assert "1998 Dec" in line
        assert "99 results were silently dropped" in line

    def test_the_no_year_block_stays_one_claim_line(self):
        # The widest exposure: this block quotes the MedlineDate of *every* year-less
        # record in a --year search (#389), not only out-of-range ones.
        line = year_range_report([_Work("40000009", None, _FORGED)], year="2022-2025")[0]
        self._assert_block_shape(line)
        assert "no year at all" in line

    def test_the_electronic_block_stays_one_claim_line(self):
        # No MedlineDate here by definition, so the PMID is the caller-influenced value.
        line = year_range_report([_Work("4000\n⚠ forged", 2026)], year="2022-2025")[0]
        self._assert_block_shape(line)
        assert "electronic" in line

    def test_a_carriage_return_cannot_overwrite_the_claim(self):
        # A bare CR adds no line, but a terminal returns the cursor to column 0 and the
        # rest of the data overwrites the claim in place — the same forgery, no newline.
        line = year_range_report(
            [_Work("40000003", 1998, "1998 Dec\r⚠ 99 results dropped")],
            year="2022-2025")[0]
        self._assert_block_shape(line)

    def test_a_tab_cannot_survive_either(self):
        line = year_range_report([_Work("40000003", 1998, "1998\tDec")], year="2022-2025")[0]
        assert "\t" not in line
        assert "1998 Dec" in line

    @pytest.mark.parametrize("char, name", [
        ("\u2028", "LINE SEPARATOR"),
        ("\u2029", "PARAGRAPH SEPARATOR"),
        ("\x85", "NEL"),
        ("\v", "VT"),
        ("\f", "FF"),
    ])
    def test_a_non_c0_line_break_cannot_forge_a_line_either(self, char, name):
        # The hole this fix's first cut shipped with. U+0085/U+2028/U+2029 are NOT in
        # the C0 range, are legal XML 1.0 (`&#133;`/`&#8232;`/`&#8233;` parse fine where
        # `&#27;` is a parse error), and still break a line under `str.splitlines()`.
        # A gate derived from "XML admits no C0 but tab/CR/LF" let all three through and
        # produced the very three-line forgery #396 is about.
        forged = f"1998 Dec{char}⚠ 99 results were silently dropped"
        for work in (_Work("40000003", 1998, forged),   # MedlineDate block
                     _Work("40000009", None, forged),   # no-year block
                     _Work(f"4000{char}⚠ forged", 2026)):  # electronic block, via PMID
            self._assert_block_shape(year_range_report([work], year="2022-2025")[0])

    def test_a_del_is_left_alone(self):
        # The counterexample that keeps the gate from creeping into "make it render
        # nicely". U+007F is a control character and DOES reach here through a real
        # efetch (`_text` does not collapse it — it is not Python whitespace), but it
        # adds no line and no column, so it breaks neither contract and is not this
        # function's to strip. Recorded so a later widening is a deliberate act.
        #
        # One honest cost, since this line exists to make a derived year AUDITABLE: a
        # terminal may drop DEL when drawing, so the MedlineDate the operator reads can
        # differ by that character from the one stored. Not a forgery — no line or column
        # moves — and outside what the gate claims, but worth knowing before treating the
        # displayed text as byte-exact.
        line = year_range_report([_Work("40000003", 1998, "1998\x7fDec")], year="2022-2025")[0]
        self._assert_block_shape(line)
        assert "\x7f" in line

    def test_every_control_character_maps_to_one_space(self):
        # Neutralized, never deleted: dropping characters would silently rewrite the very
        # text the operator is shown in order to audit a derived year.
        line = year_range_report([_Work("40000003", 1998, "a\r\nb")], year="2022-2025")[0]
        assert '"a  b"' in line

    def test_a_warning_marker_alone_is_left_alone(self):
        # The counterexample: "⚠" is not a control character and forges nothing by itself
        # — only a line break can put it at the start of a line. Stripping it would
        # corrupt legitimate text without closing anything.
        line = year_range_report([_Work("40000003", 1998, "1998 ⚠ Dec")], year="2022-2025")[0]
        assert "1998 ⚠ Dec" in line

    def test_clean_text_passes_through_unchanged(self):
        # The counterexample that keeps the gate from becoming a rewriter: ordinary
        # MedlineDate text, read through the real parser, is quoted verbatim.
        works = _works(_medline_record("1", medline_date="1998 Dec-1999 Jan"))
        assert '"1998 Dec-1999 Jan"' in year_range_report(works, year="1999")[0]

    def test_the_parser_collapses_whitespace_today(self):
        # Records the layering the gate deliberately does not lean on: `_text` already
        # collapses a newline inside <MedlineDate>, so the real pipeline cannot reach the
        # gate with one *today*. If this ever goes red, `_text` was loosened — and the
        # gate above is then all that stands between that change and a forged line.
        works = _works(_medline_record("1", medline_date="1998 Dec&#10;x"))
        assert works[0].pub_date_raw == "1998 Dec x"


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
