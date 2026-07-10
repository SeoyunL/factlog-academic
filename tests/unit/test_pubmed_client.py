# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the read-only PubMed E-utilities client (#162).

A fake transport stands in for httpx so the tests are deterministic and need no
network. The XML fixtures are shaped like the live E-utilities responses recorded
in ``docs/pubmed-spike-findings.md`` (#160).

Per the #162/#163 split, this client is the raw HTTP layer: the three methods
return the raw XML string, and the traps the spike found surface only as *raw
signals* a caller can tell apart at the boundary — not as parsed records (that is
#163). So these tests assert on the returned raw XML and on which requests were
sent, and drive the failure modes exactly as the spike observed them:

* a deleted/nonexistent-but-valid PMID answers 200 with an empty
  ``<PubmedArticleSet/>`` — returned raw, distinct from a network failure (an
  exception) and an empty *search* (raw ``<Count>0</Count>``) (spike §5);
* a malformed id answers HTTP 400 with an ``<ERROR>`` element (spike §5);
* batch ``efetch`` drops an absent id by omission, never by substitution, so a
  merged/deleted PMID surfaces only as an absence in the raw XML (spike §4);
* ``esummary`` reports a deleted id in band via a ``<DocumentSummary>`` with an
  ``<error>`` child (spike §5).

The rate limiter is tested here rather than against the API on purpose: NCBI
blocks IPs that burst (spike §3), so the burst is deliberately never reproduced,
and the serial cadence is enforced entirely by this client.
"""
from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from factlog.integrations.pubmed.client import (
    API_BASE,
    KEY_MIN_INTERVAL,
    NO_KEY_MIN_INTERVAL,
    NO_KEY_WARNING,
    PubMedClient,
    PubMedConnectionError,
    PubMedError,
    PubMedRequestError,
    PubMedServiceError,
    _RateLimiter,
    _Response,
    normalize_pmid,
)
from factlog.integrations.pubmed.config import PubMedConfig


# -- fixtures shaped like live E-utilities XML -----------------------------

def esearch_xml(*pmids: str, count: int | None = None) -> str:
    total = len(pmids) if count is None else count
    ids = "".join(f"<Id>{p}</Id>" for p in pmids)
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        f"<eSearchResult><Count>{total}</Count><RetMax>{len(pmids)}</RetMax>"
        f"<RetStart>0</RetStart><IdList>{ids}</IdList></eSearchResult>"
    )


def article(pmid: str) -> str:
    # A minimal PubmedArticle carrying a nested <PMID> inside a RetractionIn link,
    # so a caller's record-id diff (#163) can be shown to take only the record's
    # own MedlineCitation/PMID, not the nested one.
    return (
        "<PubmedArticle><MedlineCitation>"
        f"<PMID Version='1'>{pmid}</PMID>"
        "<Article><ArticleTitle>T</ArticleTitle></Article>"
        "<CommentsCorrectionsList>"
        "<CommentsCorrections RefType='RetractionIn'><PMID Version='1'>99999999</PMID>"
        "</CommentsCorrections></CommentsCorrectionsList>"
        "</MedlineCitation></PubmedArticle>"
    )


def efetch_xml(*pmids: str) -> str:
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        f"<PubmedArticleSet>{''.join(article(p) for p in pmids)}</PubmedArticleSet>"
    )


# The live deleted/nonexistent shape: HTTP 200, empty set (~205 bytes) (spike §5).
EFETCH_EMPTY = "<?xml version='1.0' encoding='UTF-8'?><PubmedArticleSet></PubmedArticleSet>"

# The live malformed-id shape: HTTP 400 with an <ERROR> element (spike §5).
EFETCH_ERROR = (
    "<?xml version='1.0' encoding='UTF-8'?><eFetchResult>"
    "<ERROR>ID list is empty! Possibly it has no correct IDs.</ERROR></eFetchResult>"
)


def summary_doc(uid: str, *, error: bool = False) -> str:
    if error:
        return f"<DocumentSummary uid='{uid}'><error>cannot get document summary</error></DocumentSummary>"
    return f"<DocumentSummary uid='{uid}'><Title>A title</Title></DocumentSummary>"


def esummary_xml(*docs: str) -> str:
    return (
        "<?xml version='1.0' encoding='UTF-8'?><eSummaryResult>"
        f"<DocumentSummarySet status='OK'>{''.join(docs)}</DocumentSummarySet></eSummaryResult>"
    )


# -- test-local raw-signal readers (these live in #163, not the client) ----

def record_pmids(xml: str) -> list[str]:
    """Top-level record ids in an efetch body — a caller's diff basis (spike §4).

    Deliberately kept in the test, not the client: extracting record ids is
    #163's parse. It takes only each record's own MedlineCitation/PMID, never a
    nested RetractionIn <PMID>.
    """
    root = ET.fromstring(xml)
    return [
        el.text.strip()
        for el in root.findall("./PubmedArticle/MedlineCitation/PMID")
        if el.text and el.text.strip()
    ]


def search_count(xml: str) -> int:
    return int(ET.fromstring(xml).findtext("Count"))


def search_ids(xml: str) -> list[str]:
    root = ET.fromstring(xml)
    return [el.text for el in root.findall("./IdList/Id")]


# -- transport harness ------------------------------------------------------

def client(responses, warnings=None, **config_kw):
    """A PubMedClient whose transport replays `responses` and never sleeps."""
    queue = list(responses)
    calls = []
    sink = warnings if warnings is not None else []

    def transport(endpoint, params):
        calls.append((endpoint, params))
        return queue.pop(0)

    config = PubMedConfig(email="dev@example.edu", **config_kw)
    instance = PubMedClient(
        config=config,
        transport=transport,
        sleep=lambda _s: None,
        warn=sink.append,
    )
    return instance, calls, sink


def ok(body: str) -> _Response:
    return _Response(200, {"content-type": "text/xml"}, body)


# -- esearch: raw XML, silent zero exposed (spike §5) ----------------------
def test_esearch_returns_raw_xml_with_pmids_and_count():
    body = esearch_xml("16354850", "33301246", count=2)
    api, calls, _ = client([ok(body)])
    result = api.esearch("copd omega-3")
    assert result == body
    assert search_ids(result) == ["16354850", "33301246"]
    assert search_count(result) == 2
    endpoint, params = calls[0]
    assert endpoint == "esearch.fcgi"
    assert params["term"] == "copd omega-3"
    assert params["db"] == "pubmed"
    assert params["retmode"] == "xml"


def test_esearch_zero_result_is_a_valid_answer_not_an_error():
    # A well-formed query — or a malformed field — can answer <Count>0</Count>.
    # The client returns the raw zero as-is; suspicion is #167's call (spike §5).
    api, _, _ = client([ok(esearch_xml(count=0))])
    result = api.esearch("MeSH:notarealterm[Majr]")
    assert search_count(result) == 0
    assert search_ids(result) == []


def test_esearch_passes_retmax_and_retstart_to_the_wire():
    api, calls, _ = client([ok(esearch_xml(count=4217))])
    api.esearch("cancer", retmax=3, retstart=10)
    _, params = calls[0]
    assert params["retmax"] == 3
    assert params["retstart"] == 10


def test_esearch_rejects_an_empty_query_before_a_request():
    api, calls, _ = client([])
    with pytest.raises(PubMedError, match="non-empty string"):
        api.esearch("   ")
    assert calls == []


# -- efetch: the three empty-ish states are distinguishable (spike §5) -----
def test_efetch_returns_the_raw_record_xml():
    body = efetch_xml("16354850", "33301246")
    api, calls, _ = client([ok(body)])
    result = api.efetch(["16354850", "33301246"])
    assert result == body  # handed through untouched for #163's parser
    assert record_pmids(result) == ["16354850", "33301246"]
    assert calls[0][0] == "efetch.fcgi"
    assert calls[0][1]["id"] == "16354850,33301246"


def test_efetch_deleted_pmid_is_a_raw_empty_body_not_a_failure():
    # HTTP 200 + empty <PubmedArticleSet/> means "this PMID is gone" (spike §5).
    # It is returned as raw XML, never raised — the caller sees an absence.
    api, _, _ = client([ok(EFETCH_EMPTY)])
    result = api.efetch(["999999999"])
    assert result == EFETCH_EMPTY
    assert record_pmids(result) == []  # requested id absent -> deleted/gone


def test_efetch_network_failure_is_an_exception_not_an_empty_body():
    # A transport failure raises before any body exists — distinct from the
    # empty-but-valid deleted answer above. Conflating them is the spike §5 trap.
    def transport(endpoint, params):
        raise PubMedConnectionError("boom")

    api = PubMedClient(
        config=PubMedConfig(email="d@e.edu"),
        transport=transport,
        sleep=lambda _s: None,
        warn=lambda _m: None,
    )
    with pytest.raises(PubMedConnectionError):
        api.efetch(["16354850"])


def test_efetch_malformed_id_answers_http_400_with_an_error_element():
    # A malformed id answers HTTP 400 with <ERROR>, distinct from the valid empty
    # answer a deleted-but-well-formed id gives (spike §5). The client lifts
    # NCBI's own reason into the exception message.
    api, _, _ = client([_Response(400, {}, EFETCH_ERROR)])
    with pytest.raises(PubMedRequestError, match="ID list is empty"):
        api.efetch(["16354850"])


# -- efetch: merged/absent ids surface by omission (spike §4) --------------
def test_efetch_drops_an_absent_id_by_omission_not_substitution():
    # Requesting A,B,C where B is gone returns exactly [A, C]. efetch never
    # substitutes a different-PMID record, so a caller learns B is gone only by
    # diffing requested ids against the raw body's PMIDs (spike §4).
    requested = ["16354850", "999999999", "33301246"]
    api, _, _ = client([ok(efetch_xml("16354850", "33301246"))])
    body = api.efetch(requested)
    returned = record_pmids(body)
    assert returned == ["16354850", "33301246"]
    missing = [p for p in requested if p not in returned]
    assert missing == ["999999999"]


def test_efetch_body_never_carries_a_pmid_that_was_not_requested():
    # The substitution guard: a returned record id is always one that was asked
    # for; a nested RetractionIn <PMID> must not read as a returned record.
    requested = ["16354850"]
    api, _, _ = client([ok(efetch_xml("16354850"))])
    returned = record_pmids(api.efetch(requested))
    assert returned == ["16354850"]
    assert set(returned) <= set(requested)


def test_efetch_deduplicates_and_caps_the_id_list():
    api, calls, _ = client([ok(efetch_xml("16354850"))])
    api.efetch(["16354850", "16354850", "pmid:16354850"])
    assert calls[0][1]["id"] == "16354850"

    api, calls, _ = client([])
    with pytest.raises(PubMedError, match="at most 200"):
        api.efetch([str(n) for n in range(1, 202)])
    assert calls == []


def test_efetch_requires_at_least_one_id():
    api, _, _ = client([])
    with pytest.raises(PubMedError, match="at least one PMID"):
        api.efetch([])


# -- esummary: raw body carries per-id deleted signal (spike §5) -----------
def test_esummary_returns_raw_xml_with_the_per_id_error_signal():
    body = esummary_xml(summary_doc("33301246"), summary_doc("999999999", error=True))
    api, calls, _ = client([ok(body)])
    result = api.esummary(["33301246", "999999999"])
    assert result == body
    assert calls[0][0] == "esummary.fcgi"
    # The deleted-id <error> signal is present in the raw body for #163 to read.
    root = ET.fromstring(result)
    errored = [d.get("uid") for d in root.findall(".//DocumentSummary") if d.find("./error") is not None]
    assert errored == ["999999999"]


# -- rate limiting (spike §3): serial cadence, no burst -------------------
def test_interval_is_the_unregistered_cadence_without_a_key():
    api, _, _ = client([ok(esearch_xml(count=0))])
    assert api._limiter._interval == NO_KEY_MIN_INTERVAL
    assert NO_KEY_MIN_INTERVAL >= 1 / 3  # stays under the 3/s ceiling


def test_interval_tightens_to_the_registered_cadence_with_a_key():
    api, _, _ = client([ok(esearch_xml(count=0))], api_key="secret")
    assert api._limiter._interval == KEY_MIN_INTERVAL
    assert KEY_MIN_INTERVAL >= 1 / 10


def test_rate_limiter_waits_the_interval_between_requests():
    now, slept = [0.0], []

    def clock():
        return now[0]

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    limiter = _RateLimiter(0.34, clock=clock, sleep=sleep)
    limiter.wait()  # first call: no wait
    assert slept == []
    limiter.wait()  # immediately after: full interval
    assert slept == [0.34]

    now[0] += 0.1  # some work elapses
    limiter.wait()
    assert slept == [0.34, pytest.approx(0.24)]

    now[0] += 10.0  # more than the interval elapses: no wait
    limiter.wait()
    assert slept == [0.34, pytest.approx(0.24)]


def test_client_paces_across_commands_not_per_command():
    # Two commands on one client share one limiter, so together they cannot
    # exceed the ceiling — the delay is owned by the client, once (spike §3).
    now, slept = [0.0], []

    def clock():
        return now[0]

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    def transport(endpoint, params):
        return ok(esearch_xml(count=0)) if endpoint == "esearch.fcgi" else ok(EFETCH_EMPTY)

    api = PubMedClient(
        config=PubMedConfig(email="d@e.edu"),
        transport=transport,
        sleep=sleep,
        clock=clock,
        warn=lambda _m: None,
    )
    api.esearch("q")          # first request: no wait
    api.efetch(["16354850"])  # second request: paced by the shared limiter
    assert slept == [NO_KEY_MIN_INTERVAL]


# -- 429 handling: honour Retry-After (spike §3) ---------------------------
def test_429_is_retried_after_retry_after_then_recovers():
    slept = []
    api = PubMedClient(
        config=PubMedConfig(email="d@e.edu"),
        transport=_replay([
            _Response(429, {"retry-after": "2"}, ""),
            ok(esearch_xml("16354850", count=1)),
        ]),
        sleep=slept.append,
        warn=lambda _m: None,
    )
    result = api.esearch("q")
    assert search_ids(result) == ["16354850"]
    assert 2.0 in slept  # honoured the Retry-After header


def test_429_that_never_clears_raises_a_service_error():
    api, _, _ = client([_Response(429, {"retry-after": "2"}, "")] * 3)
    with pytest.raises(PubMedServiceError, match="429"):
        api.esearch("q")


def test_429_without_a_retry_after_header_uses_a_conservative_default():
    slept = []
    api = PubMedClient(
        config=PubMedConfig(email="d@e.edu"),
        transport=_replay([
            _Response(429, {}, ""),
            ok(esearch_xml(count=0)),
        ]),
        sleep=slept.append,
        warn=lambda _m: None,
    )
    api.esearch("q")
    assert 2.0 in slept  # DEFAULT_RETRY_AFTER_SECONDS


# -- no-key first-run warning (once, then proceed) -------------------------
def test_no_key_warning_is_emitted_once_across_multiple_requests():
    warnings = []
    api, _, sink = client(
        [ok(esearch_xml(count=0)), ok(EFETCH_EMPTY)],
        warnings=warnings,
    )
    api.esearch("q")
    api.efetch(["16354850"])
    assert sink == [NO_KEY_WARNING]  # exactly once, not per request


def test_no_key_warning_text_names_the_no_third_party_boundary():
    # The last two lines are load-bearing: the key never leaves for a model
    # provider. Assert the exact required text, verbatim.
    assert NO_KEY_WARNING == (
        "⚠ No NCBI API key configured.\n"
        "  factlog will run at 3 req/sec (unregistered limit).\n"
        "  Get a free key at https://www.ncbi.nlm.nih.gov/account/settings/\n"
        "  (Sign in → API Key Management → Create an API Key)\n"
        "  Then set it via NCBI_API_KEY, ~/.config/factlog/pubmed.toml, or an explicit path.\n"
        "\n"
        "  Note: this key is used only for direct calls to eutils.ncbi.nlm.nih.gov.\n"
        "  factlog does not transmit it to any third-party service."
    )


def test_no_warning_when_a_key_is_configured():
    warnings = []
    api, _, sink = client([ok(esearch_xml(count=0))], warnings=warnings, api_key="secret")
    api.esearch("q")
    assert sink == []


def test_request_carries_tool_and_email_and_key_when_present():
    api, calls, _ = client([ok(esearch_xml(count=0))], api_key="secret")
    api.esearch("q")
    _, params = calls[0]
    assert params["tool"] == "factlog"
    assert params["email"] == "dev@example.edu"
    assert params["api_key"] == "secret"


def test_request_omits_the_key_param_when_no_key_is_configured():
    api, calls, _ = client([ok(esearch_xml(count=0))])
    api.esearch("q")
    assert "api_key" not in calls[0][1]


# -- id validation ----------------------------------------------------------
@pytest.mark.parametrize("bad", ["0", "007", "abc", "", "-5", "12x"])
def test_malformed_pmids_never_reach_the_transport(bad):
    api, calls, _ = client([])
    with pytest.raises(PubMedError, match="invalid PMID"):
        api.efetch([bad])
    assert calls == []


def test_normalize_pmid_strips_a_pmid_prefix():
    assert normalize_pmid("pmid:16354850") == "16354850"
    assert normalize_pmid(16354850) == "16354850"


def test_api_base_is_https():
    assert API_BASE.startswith("https://")


def _replay(responses):
    queue = list(responses)

    def transport(endpoint, params):
        return queue.pop(0)

    return transport
