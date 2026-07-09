# SPDX-License-Identifier: Apache-2.0
"""Unit tests for arXiv settings and the query vocabularies (#57).

`validate_category` exists because arXiv answers a bogus `cat:` value with HTTP
200 and zero results — the same silent lie `WORK_TYPES` guards against on the
OpenAlex side, except arXiv does not even reject an unknown *field* name.
"""
from __future__ import annotations

import pytest

from factlog.integrations.arxiv.config import (
    CATEGORIES,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    OLD_STYLE_ARCHIVES,
    REQUEST_DELAY_SECONDS,
    ArxivConfig,
    ArxivConfigError,
    ArxivValidationError,
    from_mapping,
    load_config,
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
