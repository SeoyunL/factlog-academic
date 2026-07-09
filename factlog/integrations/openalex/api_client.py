#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Read-only OpenAlex client (#51, spec §5.5 Step 2).

Wraps the two endpoints factlog needs — ``/works`` and ``/works/{id}`` — with
GET requests only; OpenAlex records are never modified (P4).

Three behaviours of the live API shape this module (measured in #51):

* **Credit-based rate limiting.** The daily budget is ~1000 credits, not
  100,000 requests. A ``search`` costs 10 credits, a filter-only query costs 1,
  and fetching a single work by id or DOI costs 0. Every response carries the
  remaining budget, which :attr:`OpenAlexClient.rate_limit` exposes so callers
  can warn before the budget runs out. Cost is flat per request regardless of
  ``per_page``, so a wide page is strictly cheaper than paging.
* **Zero-padded work ids are silently coerced.** ``/works/W000000000000``
  answers ``200`` with an unrelated work (``W0``) rather than ``404``. Ids are
  therefore validated here, before the request, instead of trusting the status.
* **Error bodies are HTML.** A ``404`` returns ``text/html``, so responses are
  classified by status *before* anything tries to parse JSON.

``httpx`` is imported lazily inside :meth:`_default_transport`, so importing this
module (and ``import factlog``) stays light for users without the extra. Tests
inject a fake ``transport`` to stay deterministic and network-free.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from factlog.integrations.openalex.config import API_MAX_PER_PAGE, OpenAlexConfig

API_BASE = "https://api.openalex.org"

# OpenAlex work ids are "W" + a positive integer. Leading zeros must be rejected
# rather than sent: the API coerces "W000000000000" to the unrelated work "W0".
_WORK_ID_RE = re.compile(r"^W[1-9][0-9]*$")

# A DOI is a "10.<registrant>/<suffix>" name; the resolver prefix is optional.
_DOI_RE = re.compile(r"^10\.[0-9]{4,9}/\S+$")

_OPENALEX_URL_PREFIX = "https://openalex.org/"
_DOI_URL_PREFIXES = ("https://doi.org/", "http://doi.org/", "doi:")
_PMID_URL_PREFIX = "https://pubmed.ncbi.nlm.nih.gov/"

# A "YYYY" year, or an inclusive "YYYY-YYYY" range (OpenAlex filter syntax).
_YEAR_RE = re.compile(r"^(?P<from>[12][0-9]{3})(?:-(?P<to>[12][0-9]{3}))?$")

# Credits charged per request class, measured against the live API. Used to
# estimate a command's cost before it runs.
CREDITS_SEARCH = 10
CREDITS_FILTER = 1
CREDITS_LOOKUP = 0

# Remaining-credit level below which callers should warn the operator. Two
# searches' worth: enough runway to finish an in-flight command and react.
LOW_CREDIT_THRESHOLD = 2 * CREDITS_SEARCH


class OpenAlexError(Exception):
    """An OpenAlex request could not be satisfied (bad id, rejected query, ...)."""


class OpenAlexConnectionError(OpenAlexError):
    """The OpenAlex API could not be reached (DNS, TLS, socket, timeout)."""


class OpenAlexNotFoundError(OpenAlexError):
    """The requested work does not exist."""


class OpenAlexRateLimitError(OpenAlexError):
    """The daily credit budget is exhausted (HTTP 429)."""


@dataclass(frozen=True)
class RateLimit:
    """The credit budget reported on a response.

    ``cost`` is what *this* request was charged (``x-ratelimit-credits-used``),
    not a running total. ``remaining`` and ``limit`` describe the daily budget,
    and ``reset_seconds`` is how long until it refills (~23h).
    """

    limit: int | None = None
    remaining: int | None = None
    cost: int | None = None
    reset_seconds: int | None = None

    @property
    def is_low(self) -> bool:
        """True when too little budget remains for another search."""
        return self.remaining is not None and self.remaining < LOW_CREDIT_THRESHOLD

    @property
    def searches_remaining(self) -> int | None:
        """How many more ``search`` requests the remaining budget affords."""
        if self.remaining is None:
            return None
        return self.remaining // CREDITS_SEARCH


@dataclass(frozen=True)
class SearchPage:
    """One page of ``/works`` results plus the total match count."""

    results: list[dict]
    count: int
    next_cursor: str | None = None


def normalize_work_id(value: str) -> str:
    """Return the bare ``W...`` id for a work id or an ``openalex.org`` URL.

    Raises :class:`OpenAlexError` on anything that is not a well-formed id.
    Validation happens here because the API answers ``200`` with the wrong work
    for zero-padded ids (see module docstring).
    """
    if not isinstance(value, str) or not value.strip():
        raise OpenAlexError("work id must be a non-empty string.")
    candidate = value.strip()
    if candidate.lower().startswith(_OPENALEX_URL_PREFIX):
        candidate = candidate[len(_OPENALEX_URL_PREFIX):]
    candidate = candidate.strip("/")
    # Accept a lowercase "w123" but normalize it; reject W0 and zero-padding.
    if candidate[:1] in ("w", "W"):
        candidate = "W" + candidate[1:]
    if not _WORK_ID_RE.match(candidate):
        raise OpenAlexError(
            f"invalid OpenAlex work id {value!r}; expected the form 'W2741809807' "
            "(no leading zeros)."
        )
    return candidate


def normalize_doi(value: str) -> str:
    """Return the bare, lowercased ``10.x/y`` DOI for a DOI or a resolver URL."""
    if not isinstance(value, str) or not value.strip():
        raise OpenAlexError("DOI must be a non-empty string.")
    candidate = value.strip().lower()
    for prefix in _DOI_URL_PREFIXES:
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix):]
            break
    if not _DOI_RE.match(candidate):
        raise OpenAlexError(f"invalid DOI {value!r}; expected the form '10.1234/abcd'.")
    return candidate


def normalize_pmid(value: str) -> str:
    """Return the bare PMID for a PMID or a ``pubmed.ncbi.nlm.nih.gov`` URL.

    OpenAlex reports ``ids.pmid`` as a full URL; §7.1 duplicate detection and the
    PubMed integration both need the bare number.
    """
    if not isinstance(value, str) or not value.strip():
        raise OpenAlexError("PMID must be a non-empty string.")
    candidate = value.strip()
    if candidate.lower().startswith(_PMID_URL_PREFIX):
        candidate = candidate[len(_PMID_URL_PREFIX):]
    candidate = candidate.strip("/")
    if not candidate.isdigit() or candidate.lstrip("0") != candidate:
        raise OpenAlexError(f"invalid PMID {value!r}; expected a positive integer.")
    return candidate


def year_filter(value: str) -> str:
    """Translate ``2023`` or ``2020-2025`` into an OpenAlex year filter value."""
    if not isinstance(value, str) or not _YEAR_RE.match(value.strip()):
        raise OpenAlexError(f"invalid year {value!r}; expected 'YYYY' or 'YYYY-YYYY'.")
    match = _YEAR_RE.match(value.strip())
    start, end = match.group("from"), match.group("to")
    if end is not None and int(end) < int(start):
        raise OpenAlexError(f"invalid year range {value!r}; {end} precedes {start}.")
    return value.strip()


@dataclass(frozen=True)
class _Response:
    """The subset of an HTTP response this client depends on."""

    status_code: int
    headers: dict
    json_body: object = None
    text: str = ""


def _as_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _parse_rate_limit(headers: dict) -> RateLimit:
    lower = {str(k).lower(): v for k, v in (headers or {}).items()}
    return RateLimit(
        limit=_as_int(lower.get("x-ratelimit-limit")),
        remaining=_as_int(lower.get("x-ratelimit-remaining")),
        cost=_as_int(lower.get("x-ratelimit-credits-used")),
        reset_seconds=_as_int(lower.get("x-ratelimit-reset")),
    )


def _api_message(response: _Response) -> str:
    """The API's own error text, when it sent JSON; otherwise the status line.

    Error bodies are HTML for 404s, so the JSON shape cannot be assumed.
    """
    body = response.json_body
    if isinstance(body, dict):
        message = body.get("message") or body.get("error")
        if isinstance(message, str) and message:
            return message
    return f"HTTP {response.status_code}"


class OpenAlexClient:
    """Fetch works from the OpenAlex API. GET only — OpenAlex is never written to (P4)."""

    def __init__(self, config: OpenAlexConfig | None = None, transport=None):
        self._config = config or OpenAlexConfig()
        self._transport = transport
        self._rate_limit = RateLimit()

    # -- transport ---------------------------------------------------------
    @property
    def transport(self):
        if self._transport is None:
            self._transport = self._default_transport()
        return self._transport

    @property
    def rate_limit(self) -> RateLimit:
        """The credit budget reported by the most recent response."""
        return self._rate_limit

    def _default_transport(self):
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment without the extra
            raise OpenAlexError(
                "httpx is required for the OpenAlex integration: "
                "pip install 'factlog[openalex]'"
            ) from exc

        user_agent = "factlog"
        if self._config.email:
            user_agent = f"factlog (mailto:{self._config.email})"

        def _send(path: str, params: dict) -> _Response:
            try:
                raw = httpx.get(
                    f"{API_BASE}{path}",
                    params=params,
                    timeout=30.0,
                    headers={"User-Agent": user_agent},
                    follow_redirects=True,
                )
            except httpx.RequestError as exc:
                raise OpenAlexConnectionError(
                    f"cannot reach the OpenAlex API at {API_BASE} ({type(exc).__name__}): {exc}"
                ) from exc
            # Only 2xx bodies are guaranteed JSON; a 404 arrives as HTML.
            body = None
            if raw.headers.get("content-type", "").startswith("application/json"):
                try:
                    body = raw.json()
                except ValueError:
                    body = None
            return _Response(raw.status_code, dict(raw.headers), body, raw.text)

        return _send

    # -- requests ----------------------------------------------------------
    def _request(self, path: str, params: dict | None = None) -> dict:
        query = dict(params or {})
        # OpenAlex asks identified tools to send a contact address. It no longer
        # grants a separate rate-limit pool (#51), but it stays a courtesy.
        if self._config.email:
            query.setdefault("mailto", self._config.email)

        response = self.transport(path, query)
        self._rate_limit = _parse_rate_limit(response.headers)

        if response.status_code == 404:
            raise OpenAlexNotFoundError(f"OpenAlex has no record at {path}")
        if response.status_code == 429:
            remaining = self._rate_limit.reset_seconds
            wait = f" retry in ~{remaining}s" if remaining else ""
            raise OpenAlexRateLimitError(
                f"OpenAlex daily credit budget is exhausted.{wait}"
            )
        if response.status_code >= 400:
            raise OpenAlexError(f"OpenAlex rejected the request: {_api_message(response)}")
        if not isinstance(response.json_body, dict):
            raise OpenAlexError(
                f"OpenAlex returned a non-JSON body for {path} "
                f"(HTTP {response.status_code})"
            )
        return response.json_body

    def _page(self, params: dict) -> SearchPage:
        payload = self._request("/works", params)
        results = payload.get("results")
        meta = payload.get("meta")
        if not isinstance(results, list) or not isinstance(meta, dict):
            raise OpenAlexError("OpenAlex returned a malformed /works response.")
        return SearchPage(
            results=[r for r in results if isinstance(r, dict)],
            count=_as_int(meta.get("count")) or 0,
            next_cursor=meta.get("next_cursor") if isinstance(meta.get("next_cursor"), str) else None,
        )

    def _limit(self, limit: int | None) -> int:
        if limit is None:
            return self._config.default_limit
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise OpenAlexError(f"limit must be a positive integer, got {limit!r}")
        if limit > self._config.max_limit:
            raise OpenAlexError(
                f"limit {limit} exceeds the maximum of {self._config.max_limit} "
                f"(the OpenAlex API caps per_page at {API_MAX_PER_PAGE})."
            )
        return limit

    # -- queries -----------------------------------------------------------
    def get_work(self, work_id: str) -> dict:
        """Fetch one work by its OpenAlex id. Costs 0 credits."""
        return self._request(f"/works/{normalize_work_id(work_id)}")

    def get_work_by_doi(self, doi: str) -> dict:
        """Fetch one work by DOI. Costs 0 credits."""
        return self._request(f"/works/doi:{normalize_doi(doi)}")

    def search_works(
        self,
        query: str,
        *,
        year: str | None = None,
        work_type: str | None = None,
        limit: int | None = None,
        sort: str | None = None,
        cursor: str | None = None,
    ) -> SearchPage:
        """Search works by free text. **Costs 10 credits per request.**

        ``limit`` maps to ``per_page``; because cost is flat per request, asking
        for the full page the caller wants is cheaper than paging to it.
        """
        if not isinstance(query, str) or not query.strip():
            raise OpenAlexError("search query must be a non-empty string.")

        filters = []
        if year is not None:
            filters.append(f"publication_year:{year_filter(year)}")
        if work_type is not None:
            if not isinstance(work_type, str) or not work_type.strip():
                raise OpenAlexError("type must be a non-empty string.")
            filters.append(f"type:{work_type.strip()}")

        params: dict = {"search": query.strip(), "per_page": self._limit(limit)}
        if filters:
            params["filter"] = ",".join(filters)
        if sort:
            params["sort"] = sort
        if cursor:
            params["cursor"] = cursor
        return self._page(params)

    def citing_works(self, work_id: str, *, limit: int | None = None) -> SearchPage:
        """Works that cite ``work_id`` (spec's ``--direction citing``). Costs 1 credit."""
        wid = normalize_work_id(work_id)
        return self._page({"filter": f"cites:{wid}", "per_page": self._limit(limit)})

    def cited_works(self, work_id: str, *, limit: int | None = None) -> SearchPage:
        """Works that ``work_id`` cites (spec's ``--direction cited``). Costs 1 credit."""
        wid = normalize_work_id(work_id)
        return self._page({"filter": f"cited_by:{wid}", "per_page": self._limit(limit)})
