# SPDX-License-Identifier: Apache-2.0
"""Unit tests for arXiv settings and the query vocabularies (#57).

`validate_category` exists because arXiv answers a bogus `cat:` value with HTTP
200 and zero results — the same silent lie `WORK_TYPES` guards against on the
OpenAlex side, except arXiv does not even reject an unknown *field* name.
"""
from __future__ import annotations

from datetime import date

import pytest

from factlog.integrations.arxiv.config import (
    ARXIV_EPOCH_YEAR,
    CATEGORIES,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    OLD_STYLE_ARCHIVES,
    REQUEST_DELAY_SECONDS,
    ArxivConfig,
    ArxivConfigError,
    ArxivValidationError,
    build_submitted_date,
    from_mapping,
    load_config,
    as_phrase,
    compose_search_query,
    validate_category,
    validate_search_query,
    validate_sort,
)


def test_defaults_match_the_spec():
    config = ArxivConfig()
    assert config.default_limit == DEFAULT_LIMIT == 25
    assert config.max_limit == MAX_LIMIT == 200
    assert config.request_delay == REQUEST_DELAY_SECONDS == 3.0
    assert config.email == ""


@pytest.mark.parametrize("category", ["cs.CL", "stat.ML", "cs.LG", "eess.AS", "econ.TH"])
def test_known_categories_pass(category):
    assert validate_category(category) == category


def test_bare_archive_categories_pass():
    # Nine archives are themselves categories and carry no subject class.
    for category in ("hep-th", "quant-ph", "gr-qc", "math-ph", "nucl-ex"):
        assert validate_category(category) == category


@pytest.mark.parametrize("category", ["cs.NOTAREALCAT", "zz.ZZ", "cs.cl", "", "  ", "cs"])
def test_unknown_categories_are_rejected_before_a_request(category):
    with pytest.raises(ArxivValidationError, match="category"):
        validate_category(category)


def test_category_error_points_at_the_taxonomy():
    with pytest.raises(ArxivValidationError, match="category_taxonomy"):
        validate_category("cs.ZZZ")


def test_category_vocabulary_is_the_published_size():
    # 146 archive.SUBJECT categories + 9 bare archives (arxiv.org/category_taxonomy).
    assert len(CATEGORIES) == 155


def test_old_style_archives_include_the_hyphenated_and_retired_ones():
    # The set is frozen: the old scheme was retired in 2007-04. An incomplete
    # list false-rejects valid historical ids.
    for archive in ("hep-th", "cond-mat", "math", "cs", "physics", "nlin"):
        assert archive in OLD_STYLE_ARCHIVES
    for retired in ("cmp-lg", "alg-geom", "funct-an", "q-alg", "supr-con"):
        assert retired in OLD_STYLE_ARCHIVES


def test_old_style_archives_never_contain_a_dot():
    # The normalizer splits on the first '.' to drop the subject class; an
    # archive containing one would break that.
    assert not any("." in archive for archive in OLD_STYLE_ARCHIVES)


@pytest.mark.parametrize(
    "query",
    ['ti:"chain of thought"', "au:LeCun", "abs:transformers", "all:x",
     "cat:cs.CL", "cat:cs.CL AND ti:bert", 'ti:"a: colon inside quotes"',
     "chain of thought"],  # a bare phrase carries no field prefix at all
)
def test_valid_search_queries_pass(query):
    assert validate_search_query(query) == query.strip()


@pytest.mark.parametrize(
    "query",
    ["bogusfield:anything", "title:transformers", "author:LeCun",
     "ti:bert AND bogus:1"],
)
def test_unknown_search_fields_are_rejected(query):
    # arXiv answers an unknown field with 200 and zero results, never an error.
    with pytest.raises(ArxivValidationError, match="unknown arXiv search field"):
        validate_search_query(query)


def test_search_query_validates_embedded_category_values():
    with pytest.raises(ArxivValidationError, match="unknown arXiv category"):
        validate_search_query("cat:cs.NOTAREALCAT")
    assert validate_search_query('cat:"cs.CL"') == 'cat:"cs.CL"'


def test_empty_search_query_is_rejected():
    with pytest.raises(ArxivValidationError, match="non-empty"):
        validate_search_query("  ")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("submitted", "submittedDate"), ("updated", "lastUpdatedDate"),
     ("relevance", "relevance")],
)
def test_sort_translates_to_the_api_spelling(value, expected):
    assert validate_sort(value) == expected


def test_unknown_sort_is_rejected():
    with pytest.raises(ArxivValidationError, match="invalid sort"):
        validate_sort("bogus")


# -- build_submitted_date (#80) --------------------------------------------
# The bounds are always the full YYYYMMDDTTTT form the live API needs: a bare
# four-digit year (`[2020 TO 2021]`) is silently reinterpreted to a different,
# larger result set than the equivalent full-form span.


def test_single_year_expands_to_a_full_year_span():
    assert build_submitted_date("2020") == "submittedDate:[202001010000 TO 202012312359]"


def test_year_range_expands_to_the_full_span():
    assert build_submitted_date("2020-2023") == (
        "submittedDate:[202001010000 TO 202312312359]"
    )


def test_whitespace_around_the_range_is_tolerated():
    assert build_submitted_date(" 2020 - 2023 ") == (
        "submittedDate:[202001010000 TO 202312312359]"
    )


def test_bounds_are_never_emitted_as_bare_years():
    # The bare-year form silently means something else on the live API, so the
    # clause must always carry the eight trailing digits.
    clause = build_submitted_date("2020")
    assert "[2020 TO" not in clause and "TO 2020]" not in clause


def test_reversed_range_is_rejected_not_sent():
    # A reversed span answers 200/0 on the live API — a silent lie, so it is
    # caught here rather than passed through.
    with pytest.raises(ArxivValidationError, match="runs backwards"):
        build_submitted_date("2023-2020")


def test_year_below_the_arxiv_epoch_is_rejected():
    with pytest.raises(ArxivValidationError, match="outside arXiv's range"):
        build_submitted_date(str(ARXIV_EPOCH_YEAR - 1))


def test_far_future_year_is_rejected():
    # A future year answers 200/0 rather than erroring, so a typo like 2099 would
    # read as "no such literature exists".
    with pytest.raises(ArxivValidationError, match="outside arXiv's range"):
        build_submitted_date("2099", today=date(2026, 7, 9))


def test_next_year_is_accepted_as_the_ceiling():
    # A submission dated early next year (timezones, embargoes) must not be
    # false-rejected.
    assert build_submitted_date("2027", today=date(2026, 7, 9)).startswith(
        "submittedDate:[2027"
    )


@pytest.mark.parametrize("bad", ["", "  ", "abc", "20xy", "202", "20200", "2020-",
                                 "-2020", "2020-2021-2022", "2020/2021"])
def test_malformed_year_is_rejected(bad):
    with pytest.raises(ArxivValidationError):
        build_submitted_date(bad)


def test_from_mapping_reads_client_and_import_sections():
    config = from_mapping({
        "client": {"email": " a@b.example ", "request_delay": 5},
        "import": {"default_limit": 10, "max_limit": 50, "skip_duplicates": False},
    })
    assert config.email == "a@b.example"
    assert config.request_delay == 5.0
    assert config.default_limit == 10
    assert config.max_limit == 50
    assert config.skip_duplicates is False


def test_non_string_email_fails_loud():
    # It is echoed into the User-Agent of every request; a typo must not be
    # silently dropped into an unidentified client.
    with pytest.raises(ArxivConfigError, match="email must be a string"):
        from_mapping({"client": {"email": 42}})


def test_default_limit_is_clamped_to_the_operators_ceiling():
    config = from_mapping({"import": {"default_limit": 100, "max_limit": 50}})
    assert config.default_limit == 50


@pytest.mark.parametrize("value", [True, -1, "3", None])
def test_bad_request_delay_falls_back_to_the_recommendation(value):
    # bool is an int subclass; `true` must not read as a one-second delay.
    assert from_mapping({"client": {"request_delay": value}}).request_delay == 3.0


def test_missing_config_file_named_explicitly_is_an_error(tmp_path):
    with pytest.raises(ArxivConfigError, match="not found"):
        load_config(tmp_path / "absent.toml")


def test_no_config_anywhere_yields_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert load_config(kb_root=tmp_path) == ArxivConfig()


def test_kb_policy_file_wins_over_the_user_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    user = tmp_path / "xdg" / "factlog"
    user.mkdir(parents=True)
    (user / "arxiv.toml").write_text('[client]\nemail = "user@example.com"\n')
    policy = tmp_path / "kb" / "policy"
    policy.mkdir(parents=True)
    (policy / "arxiv-config.toml").write_text('[client]\nemail = "kb@example.com"\n')

    assert load_config(kb_root=tmp_path / "kb").email == "kb@example.com"


def test_invalid_toml_is_an_error(tmp_path):
    bad = tmp_path / "arxiv.toml"
    bad.write_text("this is not = = toml")
    with pytest.raises(ArxivConfigError, match="invalid TOML"):
        load_config(bad)


class TestComposeSearchQuery:
    """The composer is shared by `ArxivClient.search` and `arxiv-search --dry-run`,
    so the query an operator is shown is the query that would be sent."""

    def test_a_bare_query_passes_through(self):
        assert compose_search_query("transformers") == "transformers"

    def test_categories_and_year_are_conjoined(self):
        from datetime import date

        composed = compose_search_query(
            "transformers", ["cs.CL", "stat.ML"], "2023", today=date(2026, 1, 1))
        assert composed == (
            "transformers AND cat:cs.CL AND cat:stat.ML "
            "AND submittedDate:[202301010000 TO 202312312359]"
        )

    def test_a_typo_is_refused_before_anything_is_composed(self):
        with pytest.raises(ArxivValidationError, match="unknown arXiv category"):
            compose_search_query("x", ["cs.NOPE"])

    def test_an_unknown_field_prefix_is_refused(self):
        with pytest.raises(ArxivValidationError, match="unknown arXiv search field"):
            compose_search_query("bogusfield:x")


class TestYearBoundsAreEmittedInFullForm:
    """A bare year is *not* silently reinterpreted — measured live, `[2020 TO 2021]`
    and `[202001010000 TO 202112312359]` return an identical count. An earlier
    docstring claimed otherwise, having compared a two-year bare span against a
    one-year full one. The full form is sent because it states the intended bounds
    without depending on how arXiv widens a bare year, not because a bare year is
    wrong."""

    def test_bounds_carry_a_time_component(self):
        from datetime import date

        clause = build_submitted_date("2020", today=date(2026, 1, 1))
        assert clause == "submittedDate:[202001010000 TO 202012312359]"
        assert "[2020 TO" not in clause

    def test_a_range_covers_all_of_the_final_year(self):
        from datetime import date

        assert build_submitted_date("2020-2021", today=date(2026, 1, 1)).endswith(
            "202112312359]")

    def test_a_reversed_span_is_refused(self):
        from datetime import date

        # Live: a reversed span answers 200 with zero results, never an error.
        with pytest.raises(ArxivValidationError):
            build_submitted_date("2021-2020", today=date(2026, 1, 1))

    def test_an_out_of_range_year_is_refused(self):
        from datetime import date

        # Live: 2099 answers 200 with zero results, which reads as "no such
        # literature exists".
        with pytest.raises(ArxivValidationError, match="outside arXiv's range"):
            build_submitted_date("2099", today=date(2026, 1, 1))


class TestABareMultiWordQueryIsSearchedAsAPhrase:
    """`--query "chain of thought"` loses its quotes to the shell, and arXiv reads
    the bare words loosely. Measured live: 87,029 results unquoted vs 5,669 quoted,
    and `chain` alone matches 71,394. Nothing errors — the operator simply never
    learns their phrase was not searched as one (#89)."""

    def test_a_bare_phrase_is_quoted(self):
        assert as_phrase("chain of thought") == 'all:"chain of thought"'

    def test_a_single_word_is_left_alone(self):
        # Live: `transformer` and `all:"transformer"` both match 172,792.
        assert as_phrase("transformer") == "transformer"

    @pytest.mark.parametrize(
        "query",
        ['ti:"chain of thought"', "au:LeCun AND cat:cs.LG", "cat:cs.CL", "all:x"],
    )
    def test_a_field_prefix_means_the_user_speaks_arxiv(self, query):
        assert as_phrase(query) == query

    @pytest.mark.parametrize("query", ["chain AND thought", "a OR b", "x ANDNOT y"])
    def test_a_boolean_query_is_never_wrapped(self, query):
        # Wrapping would silently change the meaning: live, `chain AND thought`
        # matches 6,015 while `all:"chain AND thought"` matches 5,669.
        assert as_phrase(query) == query

    def test_an_already_quoted_query_is_left_alone(self):
        assert as_phrase('"deliberately quoted"') == '"deliberately quoted"'

    def test_the_word_and_inside_a_phrase_is_not_a_boolean(self):
        # `AND` is an operator; `and` is a word.
        assert as_phrase("cats and dogs") == 'all:"cats and dogs"'

    def test_composition_uses_the_phrase_form(self):
        assert compose_search_query("chain of thought", ["cs.CL"]) == (
            'all:"chain of thought" AND cat:cs.CL'
        )
