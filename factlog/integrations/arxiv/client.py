#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Read-only arXiv client (#57, spec §11 Step 2).

Wraps the single endpoint factlog needs — ``GET /api/query`` — with GET requests
only; arXiv records are never modified (P4).

Five measured behaviours of the live API shape this module (#57). Every one of
them is a way to get a *wrong answer without an error*, which is why so much of
this file is validation rather than transport:

* **Well-formed-but-wrong input answers 200 with zero entries.** A nonexistent
  id (``9999.99999``), a nonexistent *version* (``1706.03762v99``), a bogus
  category (``cat:cs.NOTAREALCAT``), an unknown search field
  (``bogusfield:x``), and even ``arXiv:1706.03762`` — arXiv's own citation form —
  all report "0 results". Only syntactic garbage yields a 400. Ids and query
  values are therefore validated before the request, and a zero-entry
  ``id_list`` response is an error condition rather than an empty result.
* **Error bodies are Atom, and contain an ``<entry>``.** A 400 returns a feed
  whose single entry has ``<title>Error</title>``. Counting entries to judge
  success reads an error document as a result, so responses are classified by
  status *before* anything looks at the body.
* **``max_results`` defaults to 10.** An ``id_list`` of 15 ids sent without it
  returns ten entries and ``totalResults=15``; the other five vanish silently.
* **The response does not preserve ``id_list`` order.** Requesting
  ``1706.03762,1810.04805,...`` returns the same works in a different order, so
  pairing requests to entries positionally attaches every paper's provenance to
  the wrong record — worse than a miss, because nothing errors. Results are
  matched by normalized base id.
* **feedparser returns partial entries on a truncated body**, setting only
  ``bozo``. A 10 MB response cut mid-document parsed to two entries with no
  exception. ``bozo`` is therefore treated as a hard failure.

Rate limiting is a courtesy, not an enforcement: twelve zero-delay requests all
answered 200. Nothing on the wire will catch a regression in the delay, so it is
enforced by unit test. arXiv's documented push-back is ``503`` with
``Retry-After``, not ``429``; both are handled.

``httpx`` and ``feedparser`` are imported lazily inside :meth:`_default_transport`
and :meth:`_parse_feed`, so importing this module (and ``import factlog``) stays
light for users without the extra. Tests inject a fake ``transport`` to stay
deterministic and network-free.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from factlog import __version__
from factlog.integrations.arxiv.config import (
    API_DEFAULT_MAX_RESULTS,
    ArxivConfig,
    validate_category,
    validate_sort,
)
from factlog.integrations.arxiv.id_normalizer import ArxivId, normalize_arxiv_id

# Hardcoded https. The documented `http://export.arxiv.org` answers 301, and a
# redirect to an unexpected host is something to see rather than absorb — so
# redirects are not followed (contrast OpenAlexClient's follow_redirects=True).
API_BASE = "https://export.arxiv.org"
API_PATH = "/api/query"

# arXiv's id_list ceiling per request (spec §2.2).
MAX_ID_LIST = 100

# Retries for 429/503/5xx, with exponential backoff (spec §8.3).
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 2.0


class ArxivError(Exception):
    """An arXiv request could not be satisfied (bad id, rejected query, ...)."""


class ArxivConnectionError(ArxivError):
    """The arXiv API could not be reached (DNS, TLS, socket, timeout)."""


class ArxivNotFoundError(ArxivError):
    """Every requested id is well-formed but unknown to arXiv (200 with no entries)."""


class ArxivServiceError(ArxivError):
    """arXiv is pushing back (503 with Retry-After, or 429)."""


class ArxivResponseError(ArxivError):
    """The response body could not be trusted (truncated, malformed, miscounted)."""


@dataclass(frozen=True)
class _Response:
    """The subset of an HTTP response this client depends on."""

    status_code: int
    headers: dict
    text: str = ""


@dataclass(frozen=True)
class Feed:
    """One parsed ``/api/query`` response.

    ``total`` is ``<opensearch:totalResults>`` — the count of record, and the
    only way to tell truncation (``total != len(entries)``) from a genuinely
    missing id.
    """

    entries: list = field(default_factory=list)
    total: int = 0
    items_per_page: int = 0


@dataclass(frozen=True)
class BatchResult:
    """Works that came back, and the requested ids that silently did not."""

    works: list
    missing: list[ArxivId]


def _user_agent(config: ArxivConfig) -> str:
    """arXiv operators may throttle unidentified clients (spec §2)."""
    if config.email:
        return f"factlog/{__version__} (contact: {config.email})"
    return f"factlog/{__version__}"


class _RateLimiter:
    """Enforce a minimum interval between requests.

    arXiv will not enforce this for us (#57): twelve zero-delay requests all
    answered 200. Correctness here is entirely on the client, hence the unit test.
    """

    def __init__(self, interval: float, clock=time.monotonic, sleep=time.sleep):
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None:
            remaining = self._interval - (now - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


class ArxivClient:
    """Fetch works from the arXiv API. GET only — arXiv is never written to (P4)."""

    def __init__(self, config: ArxivConfig | None = None, transport=None, sleep=time.sleep):
        self._config = config or ArxivConfig()
        self._transport = transport
        self._sleep = sleep
        self._limiter = _RateLimiter(self._config.request_delay, sleep=sleep)

    # -- transport ---------------------------------------------------------
    @property
    def transport(self):
        if self._transport is None:
            self._transport = self._default_transport()
        return self._transport

    def _default_transport(self):
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment without the extra
            raise ArxivError(
                "httpx is required for the arXiv integration: pip install 'factlog[arxiv]'"
            ) from exc

        user_agent = _user_agent(self._config)

        def _send(params: dict) -> _Response:
            try:
                raw = httpx.get(
                    f"{API_BASE}{API_PATH}",
                    params=params,
                    timeout=120.0,  # a 200-entry page is megabytes; a short timeout truncates
                    headers={"User-Agent": user_agent},
                    follow_redirects=False,
                )
            except httpx.RequestError as exc:
                raise ArxivConnectionError(
                    f"cannot reach the arXiv API at {API_BASE} ({type(exc).__name__}): {exc}"
                ) from exc
            return _Response(raw.status_code, dict(raw.headers), raw.text)

        return _send

    # -- requests ----------------------------------------------------------
    def _classify(self, response: _Response) -> None:
        """Raise on any non-200. Called *before* the body is parsed.

        A 400 body is a well-formed Atom feed carrying an error ``<entry>``, so
        a parser that runs first would report the error as a result.
        """
        status = response.status_code
        if status == 200:
            return
        if status in (429, 503):
            retry_after = response.headers.get("retry-after") or response.headers.get("Retry-After")
            wait = f" retry after {retry_after}s" if retry_after else ""
            raise ArxivServiceError(f"arXiv is rate limiting or unavailable (HTTP {status}).{wait}")
        if 300 <= status < 400:
            location = response.headers.get("location", "")
            raise ArxivError(f"arXiv redirected the request (HTTP {status}) to {location!r}.")
        raise ArxivError(f"arXiv rejected the request: HTTP {status}")

    def _parse_feed(self, text: str) -> Feed:
        try:
            import feedparser
        except ImportError as exc:  # pragma: no cover - environment without the extra
            raise ArxivError(
                "feedparser is required for the arXiv integration: "
                "pip install 'factlog[arxiv]'"
            ) from exc

        parsed = feedparser.parse(text)
        # feedparser does not raise on a truncated document: it sets `bozo` and
        # returns whatever entries it managed to read. Trusting that would turn a
        # cut-short download into a silently short result set.
        if parsed.bozo:
            reason = type(parsed.get("bozo_exception")).__name__
            raise ArxivResponseError(
                f"arXiv returned a malformed or truncated Atom feed ({reason}). "
                "The response was not parsed; no results are reported."
            )
        feed = parsed.feed
        return Feed(
            entries=list(parsed.entries),
            total=_as_int(feed.get("opensearch_totalresults")),
            items_per_page=_as_int(feed.get("opensearch_itemsperpage")),
        )

    def _request(self, params: dict) -> Feed:
        last_error: ArxivError | None = None
        for attempt in range(MAX_RETRIES):
            self._limiter.wait()
            response = self.transport(params)
            try:
                self._classify(response)
            except ArxivServiceError as exc:
                last_error = exc
                if attempt == MAX_RETRIES - 1:
                    raise
                self._sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                continue
            return self._parse_feed(response.text)
        raise last_error or ArxivError("arXiv request failed.")  # pragma: no cover

    # -- queries -----------------------------------------------------------
    def fetch_works(self, ids) -> BatchResult:
        """Fetch works by id, reporting the ids arXiv silently declined to return.

        A well-formed id that does not exist — and a pinned version that does not
        exist — come back as *absence*, not as an error. So the requested ids are
        diffed against the returned ones, and the diff keys on the normalized
        base id because **the response is not in request order**.
        """
        from factlog.integrations.arxiv.work_parser import parse_entry

        wanted = [normalize_arxiv_id(value) for value in ids]
        if not wanted:
            raise ArxivError("fetch_works requires at least one id.")
        if len(wanted) > MAX_ID_LIST:
            raise ArxivError(
                f"arXiv accepts at most {MAX_ID_LIST} ids per request, got {len(wanted)}."
            )

        feed = self._request({
            "id_list": ",".join(identifier.query_value for identifier in wanted),
            # Without this the API returns only its default page of ten and drops
            # the rest with no error.
            "max_results": max(len(wanted), API_DEFAULT_MAX_RESULTS),
        })

        if feed.total != len(feed.entries):
            raise ArxivResponseError(
                f"arXiv reported {feed.total} results but returned {len(feed.entries)} "
                "entries; the response is truncated or paged. No results are reported."
            )

        works = [parse_entry(entry) for entry in feed.entries]
        by_base = {work.arxiv_id: work for work in works}

        found, missing = [], []
        for identifier in wanted:
            work = by_base.get(identifier.base)
            # A pinned version must come back as that version; arXiv answers an
            # unknown version with absence, and a bare id with the latest.
            if work is None or (identifier.version is not None
                                and work.version != identifier.version):
                missing.append(identifier)
            else:
                found.append(work)

        if not found:
            raise ArxivNotFoundError(
                "arXiv returned no entries for: "
                + ", ".join(str(identifier) for identifier in missing)
            )
        return BatchResult(found, missing)

    def fetch_work(self, value: str):
        """Fetch exactly one work. Raises :class:`ArxivNotFoundError` when absent."""
        result = self.fetch_works([value])
        if result.missing:
            raise ArxivNotFoundError(f"arXiv has no record for {result.missing[0]}")
        return result.works[0]

    def search(
        self,
        query: str,
        *,
        categories=(),
        limit: int | None = None,
        sort: str | None = None,
        start: int = 0,
    ) -> tuple[list, int]:
        """Search works, returning ``(works, total)``.

        Unlike :meth:`fetch_works`, **zero results is a legitimate answer here** —
        a search that matches nothing is not an error. That asymmetry is why the
        missing-id diff does not generalize to search.
        """
        from factlog.integrations.arxiv.work_parser import parse_entry

        if not isinstance(query, str) or not query.strip():
            raise ArxivError("search query must be a non-empty string.")

        # Validated before the request: an unknown category answers 200 with zero
        # results, which reads as "no such literature exists".
        clauses = [query.strip()]
        for category in categories:
            clauses.append(f"cat:{validate_category(category)}")

        params: dict = {
            "search_query": " AND ".join(clauses),
            "start": max(0, start),
            "max_results": self._limit(limit),
        }
        if sort:
            params["sortBy"] = validate_sort(sort)
            params["sortOrder"] = "descending"

        feed = self._request(params)
        return [parse_entry(entry) for entry in feed.entries], feed.total

    def _limit(self, limit: int | None) -> int:
        if limit is None:
            return self._config.default_limit
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ArxivError(f"limit must be a positive integer, got {limit!r}")
        if limit > self._config.max_limit:
            raise ArxivError(
                f"limit {limit} exceeds the maximum of {self._config.max_limit}."
            )
        return limit


def _as_int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0
