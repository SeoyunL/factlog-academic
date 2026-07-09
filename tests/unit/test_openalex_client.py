# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the read-only OpenAlex client (#51, spec §5.5 Step 2).

A fake transport stands in for httpx so the tests are deterministic and need no
network or the extra dependency. Header and payload fixtures are copied from
live ``api.openalex.org`` responses recorded during the #51 spike, so the traps
the spike found (credit headers, zero-padded ids, HTML 404 bodies) are exercised
against realistic shapes.
"""
from __future__ import annotations

import pytest

from factlog.integrations.openalex.api_client import (
    CREDITS_SEARCH,
    OpenAlexClient,
    OpenAlexConnectionError,
    OpenAlexError,
    OpenAlexNotFoundError,
    OpenAlexRateLimitError,
    _Response,
    normalize_doi,
    normalize_pmid,
    normalize_work_id,
    year_filter,
)
from factlog.integrations.openalex.config import OpenAlexConfig

# Headers as the live API sends them after a `search` request.
SEARCH_HEADERS = {
    "content-type": "application/json",
    "x-ratelimit-limit": "1000",
    "x-ratelimit-remaining": "990",
    "x-ratelimit-credits-used": "10",
    "x-ratelimit-reset": "82924",
}

# A trimmed real work: identifiers arrive as full URLs, not bare ids.
WORK = {
    "id": "https://openalex.org/W3113149630",
    "doi": "https://doi.org/10.1007/s10462-023-10448-w",
    "title": "Neurosymbolic AI: the 3rd wave",
    "publication_year": 2023,
    "is_retracted": False,
    "ids": {
        "openalex": "https://openalex.org/W3113149630",
        "doi": "https://doi.org/10.1007/s10462-023-10448-w",
        "pmid": "https://pubmed.ncbi.nlm.nih.gov/32738937",
    },
}


class FakeTransport:
    """Records the (path, params) it is called with and replays canned responses."""

    def __init__(self, *responses: _Response):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, path: str, params: dict) -> _Response:
        self.calls.append((path, dict(params)))
        if not self._responses:
            raise AssertionError(f"unexpected request: {path} {params}")
        return self._responses.pop(0)

    @property
    def last_params(self) -> dict:
        return self.calls[-1][1]

    @property
    def last_path(self) -> str:
        return self.calls[-1][0]


def ok(body: object, headers: dict | None = None) -> _Response:
    return _Response(200, dict(headers or SEARCH_HEADERS), body, "")


def page(results: list[dict], count: int | None = None, next_cursor: str | None = None) -> _Response:
    meta: dict = {"count": len(results) if count is None else count}
    if next_cursor:
        meta["next_cursor"] = next_cursor
    return ok({"meta": meta, "results": results})


def client(*responses: _Response, config: OpenAlexConfig | None = None):
    transport = FakeTransport(*responses)
    return OpenAlexClient(config or OpenAlexConfig(), transport=transport), transport


# -- identifier normalization ---------------------------------------------
class TestNormalizeWorkId:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("W2741809807", "W2741809807"),
            ("  W2741809807  ", "W2741809807"),
            ("https://openalex.org/W2741809807", "W2741809807"),
            ("https://openalex.org/W2741809807/", "W2741809807"),
            ("w2741809807", "W2741809807"),
        ],
    )
    def test_accepts_ids_and_urls(self, raw, expected):
        assert normalize_work_id(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "W000000000000",  # the API would answer 200 with the unrelated work W0
            "W0",
            "W0123",
            "Wabc",
            "notanid",
            "2741809807",
            "",
            "   ",
        ],
    )
    def test_rejects_malformed_ids_before_the_request(self, raw):
        with pytest.raises(OpenAlexError, match="invalid OpenAlex work id|non-empty"):
            normalize_work_id(raw)

    def test_rejects_non_string(self):
        with pytest.raises(OpenAlexError, match="non-empty string"):
            normalize_work_id(None)


class TestNormalizeDoi:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("10.1007/s10462-023-10448-w", "10.1007/s10462-023-10448-w"),
            ("https://doi.org/10.1007/s10462-023-10448-w", "10.1007/s10462-023-10448-w"),
            ("http://doi.org/10.1007/ABC", "10.1007/abc"),
            ("doi:10.1007/abc", "10.1007/abc"),
            ("  10.1007/ABC  ", "10.1007/abc"),
        ],
    )
    def test_strips_resolver_prefix_and_lowercases(self, raw, expected):
        assert normalize_doi(raw) == expected

    @pytest.mark.parametrize("raw", ["not-a-doi", "10.1/x", "10.1007", "", "10.1007/"])
    def test_rejects_malformed(self, raw):
        with pytest.raises(OpenAlexError, match="invalid DOI|non-empty"):
            normalize_doi(raw)


class TestNormalizePmid:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("32738937", "32738937"),
            ("https://pubmed.ncbi.nlm.nih.gov/32738937", "32738937"),
            ("https://pubmed.ncbi.nlm.nih.gov/32738937/", "32738937"),
        ],
    )
    def test_strips_url(self, raw, expected):
        assert normalize_pmid(raw) == expected

    @pytest.mark.parametrize("raw", ["0", "007", "abc", "", "-5"])
    def test_rejects_malformed(self, raw):
        with pytest.raises(OpenAlexError, match="invalid PMID|non-empty"):
            normalize_pmid(raw)

    def test_extracts_pmid_from_a_real_work_payload(self):
        assert normalize_pmid(WORK["ids"]["pmid"]) == "32738937"


class TestYearFilter:
    @pytest.mark.parametrize("raw", ["2023", "2020-2025", " 2020-2025 "])
    def test_accepts_year_and_range(self, raw):
        assert year_filter(raw) == raw.strip()

    @pytest.mark.parametrize("raw", ["20233", "2020-", "-2025", "2025-2020", "abcd", ""])
    def test_rejects_malformed(self, raw):
        with pytest.raises(OpenAlexError, match="invalid year"):
            year_filter(raw)


# -- request construction --------------------------------------------------
class TestRequestConstruction:
    def test_get_work_uses_the_zero_credit_lookup_path(self):
        cl, transport = client(ok(WORK))
        assert cl.get_work("https://openalex.org/W3113149630")["id"] == WORK["id"]
        assert transport.last_path == "/works/W3113149630"

    def test_get_work_by_doi_uses_the_doi_lookup_path(self):
        cl, transport = client(ok(WORK))
        cl.get_work_by_doi("https://doi.org/10.1007/S10462-023-10448-W")
        assert transport.last_path == "/works/doi:10.1007/s10462-023-10448-w"

    def test_invalid_id_never_reaches_the_transport(self):
        cl, transport = client()
        with pytest.raises(OpenAlexError):
            cl.get_work("W000000000000")
        assert transport.calls == []

    def test_search_sends_query_filters_and_per_page(self):
        cl, transport = client(page([WORK]))
        cl.search_works("neurosymbolic AI", year="2020-2025", work_type="article", limit=10)
        params = transport.last_params
        assert params["search"] == "neurosymbolic AI"
        assert params["per_page"] == 10
        assert params["filter"] == "publication_year:2020-2025,type:article"

    def test_search_omits_filter_when_unfiltered(self):
        cl, transport = client(page([]))
        cl.search_works("copd")
        assert "filter" not in transport.last_params

    def test_search_defaults_per_page_to_configured_default_limit(self):
        cl, transport = client(page([]), config=OpenAlexConfig(default_limit=7))
        cl.search_works("copd")
        assert transport.last_params["per_page"] == 7

    def test_email_is_sent_as_mailto_when_configured(self):
        cl, transport = client(page([]), config=OpenAlexConfig(email="a@b.c"))
        cl.search_works("copd")
        assert transport.last_params["mailto"] == "a@b.c"

    def test_mailto_is_omitted_when_no_email_configured(self):
        cl, transport = client(page([]))
        cl.search_works("copd")
        assert "mailto" not in transport.last_params

    def test_citing_and_cited_use_filters_not_search(self):
        cl, transport = client(page([WORK]), page([WORK]))
        cl.citing_works("W3113149630", limit=5)
        assert transport.last_params == {"filter": "cites:W3113149630", "per_page": 5}
        assert "search" not in transport.last_params

        cl.cited_works("https://openalex.org/W3113149630", limit=5)
        assert transport.last_params == {"filter": "cited_by:W3113149630", "per_page": 5}

    @pytest.mark.parametrize("bad", ["", "   ", None])
    def test_empty_query_rejected(self, bad):
        cl, transport = client()
        with pytest.raises(OpenAlexError, match="non-empty string"):
            cl.search_works(bad)
        assert transport.calls == []

    def test_limit_above_max_is_rejected_before_the_request(self):
        cl, transport = client()
        with pytest.raises(OpenAlexError, match="exceeds the maximum of 200"):
            cl.search_works("copd", limit=201)
        assert transport.calls == []

    @pytest.mark.parametrize("bad", [0, -1, True, 2.5])
    def test_non_positive_limit_rejected(self, bad):
        cl, _ = client()
        with pytest.raises(OpenAlexError, match="positive integer"):
            cl.search_works("copd", limit=bad)

    def test_config_max_limit_narrows_the_ceiling(self):
        cl, _ = client(config=OpenAlexConfig(max_limit=50))
        with pytest.raises(OpenAlexError, match="exceeds the maximum of 50"):
            cl.search_works("copd", limit=51)


# -- response handling -----------------------------------------------------
class TestResponseHandling:
    def test_search_page_carries_results_count_and_cursor(self):
        cl, _ = client(page([WORK], count=4116, next_cursor="abc"))
        result = cl.search_works("neurosymbolic AI")
        assert result.count == 4116
        assert result.next_cursor == "abc"
        assert result.results == [WORK]

    def test_non_dict_results_are_dropped(self):
        cl, _ = client(ok({"meta": {"count": 2}, "results": [WORK, "junk", None]}))
        assert cl.search_works("x").results == [WORK]

    def test_malformed_envelope_is_an_error(self):
        cl, _ = client(ok({"results": [WORK]}))  # no meta
        with pytest.raises(OpenAlexError, match="malformed /works response"):
            cl.search_works("x")

    def test_404_html_body_raises_not_found_without_json_parsing(self):
        html = _Response(404, {"content-type": "text/html; charset=utf-8"}, None, "<!doctype html>")
        cl, _ = client(html)
        with pytest.raises(OpenAlexNotFoundError, match="no record at /works/doi:10.9999/nope"):
            cl.get_work_by_doi("10.9999/nope")

    def test_429_raises_rate_limit_error_with_reset_hint(self):
        cl, _ = client(_Response(429, {"x-ratelimit-reset": "82924"}, None, ""))
        with pytest.raises(OpenAlexRateLimitError, match=r"credit budget is exhausted\. retry in ~82924s"):
            cl.search_works("copd")

    def test_400_surfaces_the_api_message(self):
        body = {"error": "Invalid query parameters error.", "message": "bogus is not a valid field."}
        cl, _ = client(_Response(400, {"content-type": "application/json"}, body, ""))
        with pytest.raises(OpenAlexError, match="bogus is not a valid field"):
            cl.search_works("copd")

    def test_500_without_json_falls_back_to_the_status_line(self):
        cl, _ = client(_Response(500, {"content-type": "text/html"}, None, "boom"))
        with pytest.raises(OpenAlexError, match="HTTP 500"):
            cl.search_works("copd")

    def test_200_with_non_dict_body_is_an_error(self):
        cl, _ = client(_Response(200, {"content-type": "application/json"}, [1, 2], ""))
        with pytest.raises(OpenAlexError, match="non-JSON body"):
            cl.get_work("W1")

    def test_not_found_is_an_openalex_error_subclass(self):
        assert issubclass(OpenAlexNotFoundError, OpenAlexError)
        assert issubclass(OpenAlexRateLimitError, OpenAlexError)
        assert issubclass(OpenAlexConnectionError, OpenAlexError)


# -- credit budget ---------------------------------------------------------
class TestRateLimit:
    def test_headers_are_parsed_from_the_last_response(self):
        cl, _ = client(page([WORK]))
        cl.search_works("x")
        rate = cl.rate_limit
        assert (rate.limit, rate.remaining, rate.cost, rate.reset_seconds) == (1000, 990, 10, 82924)

    def test_cost_is_per_request_not_cumulative(self):
        first = page([WORK])
        second = _Response(200, {**SEARCH_HEADERS, "x-ratelimit-remaining": "980"},
                           {"meta": {"count": 0}, "results": []}, "")
        cl, _ = client(first, second)
        cl.search_works("x")
        assert cl.rate_limit.remaining == 990
        cl.search_works("y")
        assert cl.rate_limit.remaining == 980
        assert cl.rate_limit.cost == CREDITS_SEARCH

    def test_searches_remaining_divides_by_search_cost(self):
        cl, _ = client(page([WORK]))
        cl.search_works("x")
        assert cl.rate_limit.searches_remaining == 99

    def test_is_low_when_under_two_searches_remain(self):
        headers = {**SEARCH_HEADERS, "x-ratelimit-remaining": "19"}
        cl, _ = client(ok({"meta": {"count": 0}, "results": []}, headers))
        cl.search_works("x")
        assert cl.rate_limit.is_low is True
        assert cl.rate_limit.searches_remaining == 1

    def test_is_not_low_at_the_threshold(self):
        headers = {**SEARCH_HEADERS, "x-ratelimit-remaining": "20"}
        cl, _ = client(ok({"meta": {"count": 0}, "results": []}, headers))
        cl.search_works("x")
        assert cl.rate_limit.is_low is False

    def test_missing_headers_leave_the_budget_unknown_not_low(self):
        cl, _ = client(_Response(200, {"content-type": "application/json"},
                                 {"meta": {"count": 0}, "results": []}, ""))
        cl.search_works("x")
        assert cl.rate_limit.remaining is None
        assert cl.rate_limit.is_low is False
        assert cl.rate_limit.searches_remaining is None

    def test_header_lookup_is_case_insensitive(self):
        cl, _ = client(_Response(200, {"X-RateLimit-Remaining": "500", "content-type": "application/json"},
                                 {"meta": {"count": 0}, "results": []}, ""))
        cl.search_works("x")
        assert cl.rate_limit.remaining == 500

    def test_garbage_header_values_are_ignored(self):
        cl, _ = client(_Response(200, {"x-ratelimit-remaining": "lots", "content-type": "application/json"},
                                 {"meta": {"count": 0}, "results": []}, ""))
        cl.search_works("x")
        assert cl.rate_limit.remaining is None

    def test_rate_limit_is_recorded_even_when_the_request_fails(self):
        cl, _ = client(_Response(400, {"x-ratelimit-remaining": "42", "content-type": "application/json"},
                                 {"message": "bad"}, ""))
        with pytest.raises(OpenAlexError):
            cl.search_works("x")
        assert cl.rate_limit.remaining == 42
