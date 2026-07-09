# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the read-only arXiv client (#57, spec §11 Step 2).

A fake transport stands in for httpx so the tests are deterministic and need no
network. The Atom fixtures are shaped like live `export.arxiv.org` responses
recorded during the #57 spike, so the traps the spike found are exercised
against realistic bodies:

* a well-formed-but-nonexistent id answers 200 with zero entries
* a 400 body is a valid Atom feed carrying an error `<entry>`
* `max_results` defaults to 10, silently dropping the rest of an `id_list`
* the response is **not** in `id_list` order
* feedparser reports a truncated body only via `bozo`

The rate limiter is tested here rather than against the API on purpose: twelve
zero-delay requests all answered 200 (#57), so nothing on the wire will ever
catch a regression in the delay.
"""
from __future__ import annotations

import pytest

from factlog.integrations.arxiv.client import (
    API_BASE,
    ArxivClient,
    ArxivConnectionError,
    ArxivError,
    ArxivNotFoundError,
    ArxivResponseError,
    ArxivServiceError,
    _RateLimiter,
    _Response,
    _user_agent,
)
from factlog.integrations.arxiv.config import ArxivConfig

pytest.importorskip("feedparser", reason="the arXiv extra provides feedparser")

FEED_HEAD = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    '<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" '
    'xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns="http://www.w3.org/2005/Atom">'
)


def entry(arxiv_id: str, version: int, title: str = "T", summary: str = "S") -> str:
    return (
        "<entry>"
        f"<id>http://arxiv.org/abs/{arxiv_id}v{version}</id>"
        f"<updated>2023-11-15T18:54:01Z</updated>"
        f"<published>2023-11-15T18:54:01Z</published>"
        f"<title>{title}</title><summary>{summary}</summary>"
        "<author><name>A Name</name></author>"
        '<arxiv:primary_category xmlns:arxiv="http://arxiv.org/schemas/atom" term="cs.CL"/>'
        '<category term="cs.CL"/>'
        "</entry>"
    )


def feed(*entries: str, total: int | None = None, per_page: int | None = None) -> str:
    count = len(entries) if total is None else total
    shown = len(entries) if per_page is None else per_page
    return (
        f"{FEED_HEAD}"
        f"<opensearch:totalResults>{count}</opensearch:totalResults>"
        f"<opensearch:startIndex>0</opensearch:startIndex>"
        f"<opensearch:itemsPerPage>{shown}</opensearch:itemsPerPage>"
        f"{''.join(entries)}</feed>"
    )


# A real 400 body: a well-formed feed whose only entry *is* the error.
ERROR_FEED = feed(
    "<entry><id>https://arxiv.org/api/errors#incorrect_id_format_for_notanid</id>"
    "<title>Error</title><summary>incorrect id format</summary></entry>"
)


def client(responses, **config_kw):
    """An ArxivClient whose transport replays `responses` and never sleeps."""
    queue = list(responses)
    calls = []

    def transport(params):
        calls.append(params)
        return queue.pop(0)

    config = ArxivConfig(request_delay=0.0, **config_kw)
    instance = ArxivClient(config=config, transport=transport, sleep=lambda _s: None)
    return instance, calls


def ok(body: str) -> _Response:
    return _Response(200, {"content-type": "application/atom+xml"}, body)


# -- happy path ------------------------------------------------------------
def test_fetch_work_parses_a_single_entry():
    api, calls = client([ok(feed(entry("2311.09277", 1)))])
    work = api.fetch_work("arXiv:2311.09277")
    assert work.arxiv_id == "2311.09277"
    assert work.version == 1
    # The normalizer ran before the request: the `arXiv:` prefix is gone.
    assert calls[0]["id_list"] == "2311.09277"


def test_search_accepts_zero_results_as_a_normal_answer():
    # Unlike id_list, an empty search is not an error: the literature really can
    # be empty for a query. This asymmetry is why the missing-id diff does not
    # generalize to search.
    api, _ = client([ok(feed(total=0))])
    works, total = api.search("cat:cs.CL AND ti:nonsense")
    assert works == []
    assert total == 0


def test_search_validates_the_category_before_spending_a_request():
    from factlog.integrations.arxiv.config import ArxivValidationError

    api, calls = client([])
    # A bogus category answers 200 with zero results, which reads to the operator
    # as "no such literature exists". It must never reach the API.
    with pytest.raises(ArxivValidationError, match="unknown arXiv category"):
        api.search("chain of thought", categories=["cs.NOTAREALCAT"])
    assert calls == []


def test_search_builds_a_conjunctive_query_from_valid_categories():
    api, calls = client([ok(feed(entry("2311.09277", 1)))])
    api.search("chain of thought", categories=["cs.CL"], sort="submitted")
    # The bare phrase is quoted so arXiv searches it as one (#89).
    assert calls[0]["search_query"] == 'all:"chain of thought" AND cat:cs.CL' 
    assert calls[0]["sortBy"] == "submittedDate"


def test_search_appends_a_submitted_date_clause_for_year():
    # --year expands to the full YYYYMMDDTTTT span. Not because a bare year is
    # reinterpreted — measured, it is not — but because the full form states the
    # intended bounds without depending on how arXiv widens a bare year (#80).
    api, calls = client([ok(feed(entry("2311.09277", 1)))])
    api.search("chain of thought", categories=["cs.CL"], year="2023")
    assert calls[0]["search_query"] == (
        'all:"chain of thought" AND cat:cs.CL AND '
        "submittedDate:[202301010000 TO 202312312359]"
    )


def test_search_rejects_a_reversed_year_range_before_a_request():
    from factlog.integrations.arxiv.config import ArxivValidationError

    api, calls = client([])
    with pytest.raises(ArxivValidationError, match="runs backwards"):
        api.search("chain of thought", year="2025-2020")
    assert calls == []


# -- the silent-miss traps -------------------------------------------------
def test_nonexistent_id_returns_zero_entries_and_becomes_an_error():
    # `9999.99999` answers HTTP 200 with an empty feed. Without this the caller
    # sees "success, no works".
    api, _ = client([ok(feed(total=0))])
    with pytest.raises(ArxivNotFoundError, match="9999.99999"):
        api.fetch_work("9999.99999")


def test_pinned_version_that_does_not_exist_is_reported_missing():
    # `1706.03762v99` answers 200 with zero entries; a *bare* id would answer
    # with v7. So a pinned version must be checked against what came back.
    api, _ = client([ok(feed(total=0))])
    with pytest.raises(ArxivNotFoundError):
        api.fetch_work("1706.03762v99")


def test_bare_id_accepts_whatever_version_arxiv_calls_latest():
    api, _ = client([ok(feed(entry("1706.03762", 7)))])
    assert api.fetch_work("1706.03762").version == 7


def test_pinned_version_mismatch_is_missing_even_when_an_entry_returns():
    # Defence in depth: if arXiv ever answered a pinned v3 with v7, silently
    # accepting it would record the wrong version in provenance.
    api, _ = client([ok(feed(entry("1706.03762", 7)))])
    with pytest.raises(ArxivNotFoundError):
        api.fetch_work("1706.03762v3")


def test_batch_reports_missing_ids_without_failing_the_whole_request():
    # `id_list=1706.03762,9999.99999` returns 200 with one entry and no hint
    # which id was dropped. The importer needs per-id outcomes, not a hard fail.
    api, _ = client([ok(feed(entry("1706.03762", 7), total=1))])
    result = api.fetch_works(["1706.03762", "9999.99999"])
    assert [w.arxiv_id for w in result.works] == ["1706.03762"]
    assert [str(i) for i in result.missing] == ["9999.99999"]


def test_batch_sets_max_results_so_the_api_does_not_drop_ids():
    # The API's default max_results is 10: fifteen ids return ten entries and
    # totalResults=15, with the other five gone silently.
    ids = [f"2311.0{n:04d}" for n in range(1, 16)]
    api, calls = client([ok(feed(*[entry(i, 1) for i in ids]))])
    api.fetch_works(ids)
    assert calls[0]["max_results"] == 15


def test_two_versions_of_one_paper_both_resolve():
    # `1706.03762v1,1706.03762v3` is exactly what a version comparison requests.
    # Both entries share a base id, so a base-keyed index loses one of them and
    # reports a version arXiv *did* return as missing.
    api, _ = client([ok(feed(entry("1706.03762", 1), entry("1706.03762", 3)))])
    result = api.fetch_works(["1706.03762v1", "1706.03762v3"])
    assert [w.version for w in result.works] == [1, 3]
    assert result.missing == []


def test_bare_id_takes_the_latest_when_several_versions_return():
    api, _ = client([ok(feed(entry("1706.03762", 1), entry("1706.03762", 3)))])
    result = api.fetch_works(["1706.03762"])
    assert [w.version for w in result.works] == [3]


def test_duplicate_ids_collapse_to_one_request_and_one_work():
    api, calls = client([ok(feed(entry("1706.03762", 7)))])
    result = api.fetch_works(["1706.03762", "arXiv:1706.03762", "1706.03762"])
    assert [w.versioned_id for w in result.works] == ["1706.03762v7"]
    assert calls[0]["id_list"] == "1706.03762"


def test_a_pinned_and_a_bare_request_for_one_paper_are_distinct_ids():
    api, calls = client([ok(feed(entry("1706.03762", 7)))])
    result = api.fetch_works(["1706.03762", "1706.03762v7"])
    assert calls[0]["id_list"] == "1706.03762,1706.03762v7"
    assert [w.versioned_id for w in result.works] == ["1706.03762v7"] * 2


def test_all_missing_batch_still_returns_a_result_not_an_exception():
    # The importer emits a per-id outcome for each miss. An exception here would
    # lose the list, and it would be inconsistent with the partial-miss path.
    api, _ = client([ok(feed(total=0))])
    result = api.fetch_works(["9999.99999", "9999.99998"])
    assert result.works == []
    assert [str(i) for i in result.missing] == ["9999.99999", "9999.99998"]


def test_results_are_matched_by_id_not_by_position():
    # The response is not in id_list order. Positional pairing would attach each
    # paper's provenance to the wrong record — correct title, wrong id.
    api, _ = client([ok(feed(entry("2005.14165", 4), entry("1706.03762", 7)))])
    result = api.fetch_works(["1706.03762", "2005.14165"])
    assert [w.arxiv_id for w in result.works] == ["1706.03762", "2005.14165"]
    assert result.works[0].version == 7


def test_truncated_or_paged_response_is_refused_rather_than_under_reported():
    # totalResults=15 with 10 entries means the body is short. Reporting the ten
    # would look like five withdrawn papers to arxiv-check-versions.
    api, _ = client([ok(feed(entry("1706.03762", 7), total=15))])
    with pytest.raises(ArxivResponseError, match="truncated or paged"):
        api.fetch_works(["1706.03762"])


def test_malformed_body_is_refused_rather_than_partially_parsed():
    # feedparser does not raise on a truncated document: it sets `bozo` and
    # returns the entries it managed to read.
    truncated = feed(entry("1706.03762", 7))[: len(feed(entry("1706.03762", 7))) - 40]
    api, _ = client([ok(truncated)])
    with pytest.raises(ArxivResponseError, match="malformed or truncated"):
        api.fetch_works(["1706.03762"])


# -- status classification -------------------------------------------------
def test_http_400_error_entry_is_never_read_as_a_result():
    # The 400 body is a valid Atom feed with one <entry> whose title is "Error".
    # Classifying on status before parsing is what keeps it from being a work.
    api, _ = client([_Response(400, {}, ERROR_FEED)])
    with pytest.raises(ArxivError, match="HTTP 400"):
        api.fetch_works(["1706.03762"])


def test_paging_past_the_end_explains_itself_rather_than_reading_as_an_outage():
    # `start` past the end of the result set answers 500, not an empty page.
    api, _ = client([_Response(500, {}, "")])
    with pytest.raises(ArxivError, match="beyond the end of arXiv's result set"):
        api.search("cat:cs.CL", start=99999999)


def test_http_500_on_the_first_page_is_reported_as_a_server_error():
    api, _ = client([_Response(500, {}, "")])
    with pytest.raises(ArxivError, match="HTTP 500"):
        api.search("cat:cs.CL")


def test_unknown_search_field_is_rejected_before_a_request():
    from factlog.integrations.arxiv.config import ArxivValidationError

    # arXiv answers `bogusfield:x` with 200 and zero results, exactly as it does
    # a bogus category. It does not even validate the field name.
    api, calls = client([])
    with pytest.raises(ArxivValidationError, match="unknown arXiv search field"):
        api.search("bogusfield:anything")
    assert calls == []


def test_bogus_category_inside_the_query_string_is_rejected_too():
    from factlog.integrations.arxiv.config import ArxivValidationError

    api, calls = client([])
    with pytest.raises(ArxivValidationError, match="unknown arXiv category"):
        api.search("cat:cs.NOTAREALCAT AND ti:transformers")
    assert calls == []


@pytest.mark.parametrize(
    "query",
    ['ti:"chain of thought"', "au:LeCun AND cat:cs.LG", "all:transformers",
     'abs:"in-context learning" ANDNOT cat:stat.ML'],
)
def test_valid_field_prefixes_pass_through(query):
    api, _ = client([ok(feed(total=0))])
    api.search(query)


def test_redirects_are_surfaced_not_absorbed():
    # http:// answers 301. A redirect to an unexpected host is worth seeing.
    api, _ = client([_Response(301, {"location": "https://elsewhere.example/"}, "")])
    with pytest.raises(ArxivError, match="redirected"):
        api.fetch_works(["1706.03762"])


@pytest.mark.parametrize("status", [429, 503])
def test_service_push_back_retries_then_raises(status):
    # arXiv's documented push-back is 503 + Retry-After, not 429. Handle both.
    api, calls = client([_Response(status, {"retry-after": "12"}, "")] * 3)
    with pytest.raises(ArxivServiceError, match="retry after 12s"):
        api.fetch_works(["1706.03762"])
    assert len(calls) == 3  # MAX_RETRIES


def test_service_push_back_recovers_when_a_retry_succeeds():
    api, calls = client([
        _Response(503, {}, ""),
        ok(feed(entry("1706.03762", 7))),
    ])
    assert api.fetch_work("1706.03762").version == 7
    assert len(calls) == 2


def test_connection_failure_is_its_own_error_class():
    def transport(params):
        raise ArxivConnectionError("boom")

    api = ArxivClient(config=ArxivConfig(request_delay=0.0), transport=transport)
    with pytest.raises(ArxivConnectionError):
        api.fetch_works(["1706.03762"])


# -- guards ----------------------------------------------------------------
def test_malformed_id_never_reaches_the_transport():
    from factlog.integrations.arxiv.id_normalizer import ArxivIdError

    api, calls = client([])
    with pytest.raises(ArxivIdError):
        api.fetch_works(["notanid"])
    assert calls == []


def test_id_list_over_the_api_ceiling_is_refused():
    api, calls = client([])
    with pytest.raises(ArxivError, match="at most 100 ids"):
        api.fetch_works([f"2311.{n:05d}" for n in range(101)])
    assert calls == []


def test_empty_id_list_is_refused():
    api, _ = client([])
    with pytest.raises(ArxivError, match="at least one id"):
        api.fetch_works([])


def test_limit_above_the_configured_maximum_is_refused():
    api, _ = client([])
    with pytest.raises(ArxivError, match="exceeds the maximum"):
        api.search("cat:cs.CL", limit=201)


# -- rate limiter ----------------------------------------------------------
def test_rate_limiter_waits_the_configured_interval_between_requests():
    now, slept = [0.0], []

    def clock():
        return now[0]

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    limiter = _RateLimiter(3.0, clock=clock, sleep=sleep)
    limiter.wait()          # first call: no wait
    assert slept == []
    limiter.wait()          # immediately after: full interval
    assert slept == [3.0]

    now[0] += 1.0           # one second of work elapses
    limiter.wait()
    assert slept == [3.0, 2.0]

    now[0] += 10.0          # more than the interval elapses: no wait
    limiter.wait()
    assert slept == [3.0, 2.0]


def test_user_agent_identifies_factlog_and_carries_the_contact():
    assert _user_agent(ArxivConfig(email="a@b.example")).startswith("factlog/")
    assert "contact: a@b.example" in _user_agent(ArxivConfig(email="a@b.example"))
    assert "contact" not in _user_agent(ArxivConfig())


def test_api_base_is_https_because_http_redirects():
    assert API_BASE.startswith("https://")


def test_a_bare_multi_word_query_is_sent_as_a_phrase():
    """The wire form, not the composer's return value. A bare `chain of thought`
    reaches arXiv as loose tokens and matches 87,029 papers; the phrase matches
    5,669 (#89). Nothing errors — only the count is wrong."""
    api, calls = client([ok(feed(total=0))])
    api.search("chain of thought")
    assert calls[0]["search_query"] == 'all:"chain of thought"'


def test_a_query_the_user_structured_is_sent_untouched():
    api, calls = client([ok(feed(total=0))], )
    api.search("chain AND thought")
    assert calls[0]["search_query"] == "chain AND thought"

    api, calls = client([ok(feed(total=0))])
    api.search('ti:"chain of thought"', categories=["cs.CL"])
    assert calls[0]["search_query"] == 'ti:"chain of thought" AND cat:cs.CL'
