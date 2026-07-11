# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the PubMed search composer and silent-zero guard (#167).

Pure functions over strings and an ``esearch`` XML body — no network. The fixtures
are shaped like the ``eSearchResult`` envelope E-utilities returns, including the
``ErrorList``/``WarningList`` PubMed volunteers when it cannot map a phrase or
field. No live capture was run (network-free by design, like the client tests);
the fixtures follow E-utilities' documented esearch envelope.
"""
from __future__ import annotations

from datetime import date

import pytest

from factlog.integrations.pubmed.search import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    PUBMED_EPOCH_YEAR,
    EsearchResult,
    PubMedSearchValidationError,
    build_year_filter,
    compose_query,
    mesh_clause,
    parse_esearch,
    silent_zero_report,
    validate_field_tags,
)


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

    def test_filtered_zero_without_a_pubmed_signal_is_still_surfaced(self):
        # A nonexistent MeSH term can produce a warning-less zero; naming the filter
        # is the floor guard so it never reads as an honest empty set.
        r = parse_esearch(_esearch(count=0))
        lines = silent_zero_report(r, year="2020", mesh=["Foo"])
        assert lines
        assert "'Foo'" in lines[0] and "'2020'" in lines[0]

    def test_honest_empty_set_stays_silent(self):
        # Zero results, no filter, no PubMed signal: a plain "0 results" is honest.
        r = parse_esearch(_esearch(count=0))
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
