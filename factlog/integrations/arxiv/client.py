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
``Retry-After``, not ``429``; both are handled, and the header is *obeyed* rather
than merely reported: a usable ``Retry-After`` sets the wait before the next
attempt — never below :data:`BACKOFF_BASE_SECONDS`, which stays as a floor no
configuration can lower — an unusable one (absent, unparseable, non-finite, zero
or negative) falls back to the exponential backoff, and a wait longer than
:data:`MAX_RETRY_AFTER_SECONDS` is not retried at all. Retrying inside a window
the server named — even a clamped fraction of it — is knocking on a door that was
just closed while telling the operator to wait. What the message says about the
attempts is measured, not assumed: the ceiling is judged per response, so a wait
past it can arrive on any attempt, and the count comes from the retry loop.

``httpx`` and ``feedparser`` are imported lazily inside :meth:`_default_transport`
and :meth:`_parse_feed`, so importing this module (and ``import factlog``) stays
light for users without the extra. Tests inject a fake ``transport`` to stay
deterministic and network-free.
"""
from __future__ import annotations

import math
import sys
import time
from datetime import date
from dataclasses import dataclass, field

from factlog import __version__
from factlog.integrations.arxiv.config import (
    API_DEFAULT_MAX_RESULTS,
    ArxivConfig,
    compose_search_query,
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

# Total attempts (one try plus two retries) for arXiv's push-back statuses
# (spec §8.3). Only 429 and 503 are retried: a 500 here is deterministic — it is
# what `start` past the end of the result set returns — so retrying it would just
# spend the delay three times over.
#
# How long an attempt waits is the server's call when it made one: a usable
# `Retry-After` sets the wait. `BACKOFF_BASE_SECONDS` serves twice over — as the
# fallback when the server named nothing readable, doubling per attempt (2s, 4s),
# and as the floor under a wait it did name. See `_backoff`: the floor is what
# keeps a second, config-independent guard under every retry.
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 2.0

# The longest server-requested wait this client will sit through. Past it the
# request stops rather than retries: the wait is reported instead of slept. The
# ceiling is judged per response and judging it costs no attempt of its own —
# though attempts already made are of course already spent, since a wait past the
# ceiling can arrive on any of them. Clamping a long `Retry-After` down to this
# value instead would keep knocking inside the window the server named — the same
# mistake in miniature — while the message still quotes the server's number.
MAX_RETRY_AFTER_SECONDS = 60.0

# Decimals a parsed `Retry-After` is rounded to. One rounding, applied before
# the ceiling comparison, keeps the number compared, slept and printed identical.
_WAIT_PRECISION = 3

# A server-directed wait at or above this many seconds is announced on stderr
# before the client sits through it (#484). Below it the wait is indistinguishable
# in feel from the exponential backoff (worst case 8s), so a notice would be noise;
# at or above it the client can go quiet for up to MAX_RETRY_AFTER_SECONDS, and a
# silent minute is exactly the window in which an interactive `arxiv-search` user
# reaches for Ctrl-C and throws away the retry #478 obeyed the server to earn.
WAIT_NOTICE_THRESHOLD_SECONDS = 30.0


class ArxivError(Exception):
    """An arXiv request could not be satisfied (bad id, rejected query, ...)."""


class ArxivConnectionError(ArxivError):
    """The arXiv API could not be reached (DNS, TLS, socket, timeout)."""


class ArxivNotFoundError(ArxivError):
    """Every requested id is well-formed but unknown to arXiv (200 with no entries)."""


class ArxivServiceError(ArxivError):
    """arXiv is pushing back (503 with Retry-After, or 429).

    Carries what the retry loop needs to decide, so the decision does not have to
    be re-derived from the message text: ``retry_after`` is the usable wait the
    server asked for in seconds (``None`` when it sent none this client can use),
    and ``retriable`` is ``False`` when that wait exceeds
    :data:`MAX_RETRY_AFTER_SECONDS`. Both are attributes on the existing class
    rather than a new subclass, so callers catching ``ArxivServiceError`` keep
    working unchanged.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        retriable: bool = True,
    ):
        super().__init__(message)
        self.retry_after = retry_after
        self.retriable = retriable


class ArxivResponseError(ArxivError):
    """The response body could not be trusted (truncated, malformed, miscounted)."""


@dataclass(frozen=True)
class _Response:
    """The subset of an HTTP response this client depends on.

    Header names are case-insensitive on the wire, and httpx, a fake transport
    and arXiv itself may each pick a different casing. They are folded to
    lower-case once, here at construction, so no read site has to try both
    spellings — a defence that only works where someone remembered to write it.
    """

    status_code: int
    headers: dict
    text: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "headers", {str(key).lower(): value for key, value in self.headers.items()}
        )


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


def _retry_after(response: _Response) -> float | None:
    """The ``Retry-After`` wait in seconds, or ``None`` when it cannot be used.

    ``None`` — meaning "fall back to the exponential backoff, and say nothing
    about a wait" — covers every form that would otherwise turn into a bad
    instruction: no header at all, a value ``float()`` rejects, a non-finite one,
    and zero or negative. ``inf`` and ``nan`` need the explicit
    :func:`math.isfinite` guard because ``float()`` *accepts* them; a try/except
    alone would let ``Retry-After: inf`` through as a wait to sleep for.

    Only the delta-seconds form is read. RFC 9110 also allows an HTTP-date, and
    that is deliberately unsupported: arXiv sends delta-seconds, and resolving a
    date would put the wall clock into a retry decision — the same request would
    retry or not depending on when it ran, and on how far the local clock has
    drifted from the server's. A header this client cannot read deterministically
    is treated as a header it did not get.

    The value is rounded to :data:`_WAIT_PRECISION` decimals so that the number
    compared against the ceiling, the number slept, and the number printed are
    one number. Unrounded, ``Retry-After: 60.0000001`` compares as over a 60s
    ceiling while printing as ``60s`` — a message that contradicts itself.
    """
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds):
        return None
    seconds = round(seconds, _WAIT_PRECISION)
    if seconds <= 0:
        return None
    return seconds


def _seconds(value: float) -> str:
    """Render a wait exactly as :func:`_retry_after` rounded it.

    Not ``%g``: it renders 1000000 as ``1e+06``, and "retry after 1e+06s" is not
    an instruction an operator can act on — the same "nothing the reader can do
    with this" failure the message rework is meant to remove. Whole seconds
    print whole (``30``), a fractional wait keeps its fraction (``1.5``), and a
    large one stays in plain integer seconds.
    """
    if value == int(value):
        return str(int(value))
    return f"{value:.{_WAIT_PRECISION}f}".rstrip("0").rstrip(".")


def _backoff(retry_after: float | None, attempt: int) -> float:
    """How long to wait before the next attempt.

    A usable ``Retry-After`` decides it, but never below
    :data:`BACKOFF_BASE_SECONDS`. RFC 9110 makes ``Retry-After`` a *minimum* —
    "how long the user agent ought to wait **before** making a follow-up
    request" — so waiting longer is full compliance, and a server that answered
    503 is not asking to be called back in a millisecond.

    That floor is deliberately independent of configuration. Before
    ``Retry-After`` was honoured there were two unrelated floors under a retry:
    this exponential backoff, a code constant, and :class:`_RateLimiter`, a
    configured interval. Letting the header replace the backoff outright leaves
    the limiter as the only floor — and ``request_delay = 0`` is a setting an
    operator can reach, which would turn three retries into three requests in
    three milliseconds. Two guards that fail independently is the point; one
    that a config file can switch off is not a guard.

    With the default 3s ``request_delay`` the limiter already waits longer than
    this floor, so the floor changes nothing there. It only bites where the
    limiter has been turned off.
    """
    if retry_after is None:
        return BACKOFF_BASE_SECONDS * (2 ** attempt)
    return max(retry_after, BACKOFF_BASE_SECONDS)


def _gave_up_after(exc: ArxivServiceError, attempts: int) -> ArxivServiceError:
    """Restate a push-back with the attempt count only the retry loop knows.

    A push-back can end the request on any attempt: the ceiling is judged per
    response, so a wait past it may arrive on the first try, after a short wait
    was already honoured, or on the last. "Not retried" would be false in two of
    those three. The count is therefore reported as measured — how many requests
    were actually sent — rather than asserted from the classification.
    """
    plural = "attempt" if attempts == 1 else "attempts"
    return ArxivServiceError(
        f"{exc} Gave up after {attempts} {plural}.",
        retry_after=exc.retry_after,
        retriable=exc.retriable,
    )


def _default_warn(message: str) -> None:
    """Where a wait notice goes when the caller injects nothing: stderr.

    stderr, never stdout, so the ``--porcelain`` contract stays byte-clean — the
    same rule every other operator-facing line in this integration follows. Tests
    inject their own ``warn`` to observe the notice without a real stream.
    """
    print(message, file=sys.stderr)


def _user_agent(config: ArxivConfig) -> str:
    """arXiv operators may throttle unidentified clients (spec §2)."""
    if config.email:
        return f"factlog-academic/{__version__} (contact: {config.email})"
    return f"factlog-academic/{__version__}"


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

    def __init__(
        self,
        config: ArxivConfig | None = None,
        transport=None,
        sleep=time.sleep,
        warn=_default_warn,
    ):
        self._config = config or ArxivConfig()
        self._transport = transport
        self._sleep = sleep
        self._warn = warn
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
                "httpx is required for the arXiv integration: "
                "pip install 'factlog-academic[arxiv] @ git+https://github.com/SeoyunL/factlog-academic'"
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
            wait = _retry_after(response)
            # Nothing here says how many attempts were made: `_classify` sees one
            # response and does not know which attempt it is. A count asserted
            # from here is only true on the first attempt, and a message that
            # misstates what the client did is the very defect this retry
            # handling exists to fix. `_request` appends that sentence.
            if wait is not None and wait > MAX_RETRY_AFTER_SECONDS:
                raise ArxivServiceError(
                    f"arXiv is rate limiting or unavailable (HTTP {status}) and asked to "
                    f"wait {_seconds(wait)}s, longer than the "
                    f"{_seconds(MAX_RETRY_AFTER_SECONDS)}s factlog will wait; "
                    f"retry after {_seconds(wait)}s.",
                    retry_after=wait,
                    retriable=False,
                )
            # `retry after Ns` is advice to the operator for their *next* run,
            # quoting what arXiv asked for — not a report of how long this client
            # slept, which `_backoff` may have floored to a longer value. The two
            # are never conflated in the wording: nothing here claims "we waited".
            detail = f"; retry after {_seconds(wait)}s" if wait is not None else ""
            raise ArxivServiceError(
                f"arXiv is rate limiting or unavailable (HTTP {status}){detail}.",
                retry_after=wait,
            )
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
                "pip install 'factlog-academic[arxiv] @ git+https://github.com/SeoyunL/factlog-academic'"
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
        for attempt in range(MAX_ATTEMPTS):
            # Re-armed on every attempt, so a retry still honours the interval.
            self._limiter.wait()
            response = self.transport(params)
            try:
                self._classify(response)
            except ArxivServiceError as exc:
                # `retriable` is False when the server named a wait past
                # MAX_RETRY_AFTER_SECONDS: that ends the request here, with the
                # remaining attempts unspent. Either way the count of attempts
                # actually made is known only here, so it is stated only here.
                if attempt == MAX_ATTEMPTS - 1 or not exc.retriable:
                    raise _gave_up_after(exc, attempt + 1) from exc
                self._announce_wait(exc.retry_after, attempt)
                self._sleep(_backoff(exc.retry_after, attempt))
                continue
            return self._parse_feed(response.text)
        raise ArxivError("arXiv request failed.")  # pragma: no cover

    def _announce_wait(self, retry_after: float | None, attempt: int) -> None:
        """Say, once per honoured wait, that arXiv asked for a long pause (#484).

        Only a server-directed ``Retry-After`` at or above
        :data:`WAIT_NOTICE_THRESHOLD_SECONDS` is announced. The exponential
        backoff tops out at 8s, so it never reaches the threshold and never
        speaks; a ``retry_after`` of ``None`` (no usable header) means the wait
        *is* that backoff, so it stays silent too. The quoted number is the
        server's own ``Retry-After``, matching the ceiling message's wording, not
        the possibly-floored value actually slept.

        ``attempt`` is 0-based and names the try that just failed; the wait
        precedes the *next* one, so the notice counts ``attempt + 2`` — for the
        first failure, "attempt 2/3".
        """
        if retry_after is None or retry_after < WAIT_NOTICE_THRESHOLD_SECONDS:
            return
        self._warn(
            f"arXiv asked to wait {_seconds(retry_after)}s; "
            f"waiting (attempt {attempt + 2}/{MAX_ATTEMPTS})..."
        )

    # -- queries -----------------------------------------------------------
    def fetch_works(self, ids) -> BatchResult:
        """Fetch works by id, reporting the ids arXiv silently declined to return.

        A well-formed id that does not exist — and a pinned version that does not
        exist — come back as *absence*, not as an error. So the requested ids are
        diffed against the returned ones, and the diff cannot key on position:
        **the response is not in request order.**

        Nor can it key on the base id alone. ``1706.03762v1,1706.03762v3`` is a
        legitimate request — it is what a version comparison looks like — and
        both entries come back sharing one base. Matching is therefore
        version-aware: a pinned request wants that exact version, and a bare
        request takes whichever version arXiv calls latest.

        Always returns a :class:`BatchResult`, even when every id is missing, so
        the importer can emit a per-id outcome for each rather than losing the
        list to an exception. Duplicate ids collapse to one request and one work.
        """
        from factlog.integrations.arxiv.work_parser import parse_entry

        wanted, seen = [], set()
        for value in ids:
            identifier = normalize_arxiv_id(value)
            if str(identifier) not in seen:
                seen.add(str(identifier))
                wanted.append(identifier)
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
        # One base id can map to several returned versions, so index every
        # version and keep the highest for bare requests.
        by_version: dict[tuple[str, int], object] = {}
        latest: dict[str, object] = {}
        for work in works:
            by_version[(work.arxiv_id, work.version)] = work
            known = latest.get(work.arxiv_id)
            if known is None or work.version > known.version:
                latest[work.arxiv_id] = work

        found, missing = [], []
        for identifier in wanted:
            if identifier.version is None:
                work = latest.get(identifier.base)
            else:
                work = by_version.get((identifier.base, identifier.version))
            if work is None:
                missing.append(identifier)
            else:
                found.append(work)
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
        year: str | None = None,
        today: date | None = None,
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

        # Validated before the request: an unknown category, an unknown field
        # prefix, a reversed or out-of-range year — each answers 200 with zero
        # results, which reads as "no such literature exists" (#57, #80).
        # `compose_search_query` is shared with `--dry-run`, so what an operator
        # is shown is what would be sent, not a reconstruction that can drift.
        params: dict = {
            "search_query": compose_search_query(query, categories, year, today=today),
            "start": max(0, start),
            "max_results": self._limit(limit),
        }
        if sort:
            params["sortBy"] = validate_sort(sort)
            # arXiv orders relevance ascending-by-default too, so the flag is
            # sent for every sort; for `relevance` descending means best-first.
            params["sortOrder"] = "descending"

        try:
            feed = self._request(params)
        except ArxivError as exc:
            # `start` past the end of the result set answers HTTP 500, not an
            # empty page. Reported as-is it reads as an arXiv outage.
            if start > 0 and "HTTP 500" in str(exc):
                raise ArxivError(
                    f"start={start} is beyond the end of arXiv's result set; the API "
                    "answers HTTP 500 rather than an empty page. Stop paging once "
                    "start >= the total reported by the first page."
                ) from exc
            raise
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
