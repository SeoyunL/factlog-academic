#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Read-only PubMed (NCBI E-utilities) client (#162, Phase 4).

Wraps the three endpoints factlog needs — ``esearch``, ``efetch``, ``esummary``
— with GET requests only; NCBI records are never modified (P4).

**Layering (with #163):** this module is the HTTP/raw transport layer *only*. The
three methods return the **raw XML string** E-utilities sent; parsing that XML
into structured records — and deciding which absent id is *deleted* vs *merged*
vs *present* — belongs to the record parser (#163). This client therefore defines
no record dataclass and no deleted/merged/present enum. It exposes the failure
modes only as **raw signals** a caller can tell apart at the transport boundary:

* a **network failure** is an *exception* (:class:`PubMedConnectionError`) —
  raised before any body exists;
* a **deleted / nonexistent-but-valid PMID** is a *valid empty 200 body*
  (``<PubmedArticleSet/>``) — returned as raw XML, never raised (spike §5);
* a **malformed id** is HTTP 400 with an ``<ERROR>`` element — raised as
  :class:`PubMedRequestError` (spike §5);
* a **merged / dropped id** is an *omission* in the raw ``efetch`` body: batch
  ``efetch`` drops absent ids rather than substituting them, and never returns a
  record whose ``<PMID>`` differs from a requested id (spike §4). The caller
  recovers it by diffing its requested ids against the ``<PMID>``s in the raw XML;
* an **empty search** is a different endpoint — raw ``esearch`` XML with
  ``<Count>0</Count>``. A well-formed query (or a malformed field) can answer a
  "silent zero"; the raw zero is returned as-is and judging whether it is
  *suspicious* is the search command's job (#167), not the transport's.

Because the three empty-ish states arrive as *exception vs raw-empty-body vs
raw-zero-count*, a caller keeps them apart without this layer parsing anything.

Every behaviour above was recorded live on 2026-07-11 in
``docs/pubmed-spike-findings.md`` (#160); this client is written against those
observations, not the E-utilities spec's assumptions.

**Rate limiting is enforced, and NCBI blocks IPs that burst** (spike §3). Every
response carried ``X-RateLimit-Limit: 3`` without a key, so a serial cadence of
≥1/3 s stays under the 3/s ceiling; a key raises the ceiling to 10/s. The delay
is owned **here, once**, as a single-flight interval between *every* request — not
paced per-command — so two commands sharing a client cannot together exceed the
ceiling. A 429 is loud (real status, ``Retry-After: 2``, JSON body) and is
honoured before retrying.

*Deliberate non-reproduction (carried from spike §3):* the "12 concurrent
requests → 429 burst" measurement was **not** re-run, because forcing it is
exactly what earns an IP-level block. The 429/``Retry-After`` handling here rests
on a *single* live observation; the conservative default is to serialise and
never parallelise unkeyed traffic.

``httpx`` is imported lazily inside :meth:`_default_transport`, so importing this
module (and ``import factlog``) stays light for users without the extra. Tests
inject a fake ``transport`` to stay deterministic and network-free.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from factlog.integrations.pubmed.config import PubMedConfig

# The E-utilities base. https only; NCBI serves eutils over TLS.
API_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DB = "pubmed"

# NCBI's documented per-request ceilings (spike §3: response header
# `X-RateLimit-Limit: 3` without a key). A registered key raises it to 10/s.
NO_KEY_RATE_PER_SEC = 3
KEY_RATE_PER_SEC = 10

# Minimum serial interval between requests. The strict floor is 1/rate
# (0.333… s unkeyed, 0.1 s keyed); the unkeyed value is rounded *up* to 0.34 s so
# clock jitter cannot push the effective rate back over 3/s and trip a block.
NO_KEY_MIN_INTERVAL = 0.34
KEY_MIN_INTERVAL = 0.10

# 429 handling. Retry-After was observed as 2 s once (spike §3); it is honoured
# from the header when present, and this is the conservative default when it is
# absent. Total attempts = one try plus two retries.
MAX_ATTEMPTS = 3
DEFAULT_RETRY_AFTER_SECONDS = 2.0

# NCBI accepts at most 200 ids per efetch/esummary request; kept conservative to
# stay well inside a single GET.
MAX_ID_LIST = 200

# The one-time notice shown when no NCBI API key is configured. The last two
# lines are load-bearing: they state that the key is used only for direct eutils
# calls and is never forwarded to a model provider or any third party, so an
# operator is not deterred from configuring one.
NO_KEY_WARNING = (
    "⚠ No NCBI API key configured.\n"
    "  factlog will run at 3 req/sec (unregistered limit).\n"
    "  Get a free key at https://www.ncbi.nlm.nih.gov/account/settings/\n"
    "  (Sign in → API Key Management → Create an API Key)\n"
    "  Then set it via NCBI_API_KEY, ~/.config/factlog/pubmed.toml, or an explicit path.\n"
    "\n"
    "  Note: this key is used only for direct calls to eutils.ncbi.nlm.nih.gov.\n"
    "  factlog does not transmit it to any third-party service."
)


class PubMedError(Exception):
    """A PubMed request could not be satisfied (bad id, rejected query, ...)."""


class PubMedConnectionError(PubMedError):
    """The E-utilities API could not be reached (DNS, TLS, socket, timeout).

    Its own class so a caller can tell a transport failure from a *valid* empty
    answer (an all-deleted efetch batch, which is a raw empty 200 body) — the two
    must never be conflated (spike §5).
    """


class PubMedRequestError(PubMedError):
    """E-utilities rejected the request (HTTP 400 with an ``<ERROR>`` element).

    A malformed id (e.g. ``0``) answers HTTP 400 with
    ``<ERROR>ID list is empty!...</ERROR>`` — distinct from a valid-but-deleted
    id, which answers HTTP 200 with an empty set (spike §5).
    """


class PubMedServiceError(PubMedError):
    """NCBI is rate limiting (HTTP 429 with ``Retry-After``) after all retries."""


@dataclass(frozen=True)
class _Response:
    """The subset of an HTTP response this client depends on."""

    status_code: int
    headers: dict
    text: str = ""


def normalize_pmid(value: object) -> str:
    """Return the bare PMID string for a PMID, or raise before a request is spent.

    A PMID is a positive integer. ``0`` and any leading-zero form are rejected
    here rather than sent: ``0`` answers HTTP 400 live (spike §5), and this keeps
    obviously-malformed ids off the wire. Mirrors OpenAlex's ``normalize_pmid``.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise PubMedError(f"PMID must be a string or int, got {type(value).__name__}")
    candidate = str(value).strip()
    if candidate.lower().startswith("pmid:"):
        candidate = candidate[len("pmid:"):].strip()
    if not candidate.isdigit() or candidate.lstrip("0") != candidate:
        raise PubMedError(f"invalid PMID {value!r}; expected a positive integer.")
    return candidate


class _RateLimiter:
    """Enforce a minimum interval between requests (single-flight).

    NCBI enforces its ceiling and blocks IPs that burst past it (spike §3), so
    correctness here is not a courtesy. ``clock``/``sleep`` are injectable so the
    interval can be unit-tested without wall-clock time.
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


def _default_warn(message: str) -> None:
    print(message, file=sys.stderr)


class PubMedClient:
    """Fetch raw XML from NCBI E-utilities. GET only — NCBI is never written to (P4).

    The three methods return the raw XML string E-utilities sent; #163 parses it.
    The request interval is chosen from whether a key is configured: ~0.34 s
    without a key (3/s), ~0.1 s with one (10/s). The no-key notice
    (:data:`NO_KEY_WARNING`) is emitted **once**, before the first request, then
    the run proceeds — it informs, it never blocks (a missing key is a slower
    run, not an error).
    """

    def __init__(
        self,
        config: PubMedConfig | None = None,
        transport=None,
        sleep=time.sleep,
        clock=time.monotonic,
        warn=_default_warn,
    ):
        self._config = config or PubMedConfig()
        self._transport = transport
        self._sleep = sleep
        self._warn = warn
        interval = KEY_MIN_INTERVAL if self._config.api_key else NO_KEY_MIN_INTERVAL
        self._limiter = _RateLimiter(interval, clock=clock, sleep=sleep)
        self._warned = False

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
            raise PubMedError(
                "httpx is required for the PubMed integration: pip install 'factlog[pubmed]'"
            ) from exc

        def _send(endpoint: str, params: dict) -> _Response:
            try:
                raw = httpx.get(
                    f"{API_BASE}/{endpoint}",
                    params=params,
                    timeout=60.0,  # a large efetch page is sizeable; a short timeout truncates
                    follow_redirects=False,
                )
            except httpx.RequestError as exc:
                raise PubMedConnectionError(
                    f"cannot reach the E-utilities API at {API_BASE} "
                    f"({type(exc).__name__}): {exc}"
                ) from exc
            return _Response(raw.status_code, dict(raw.headers), raw.text)

        return _send

    # -- request plumbing --------------------------------------------------
    def _common_params(self) -> dict:
        """The identity params NCBI expects on *every* request (spike §3).

        ``tool`` and ``email`` identify the client so operators can reach a
        misbehaving caller before blocking it; ``api_key`` raises the ceiling.
        ``email`` is included only when configured — completeness is the
        import-run's job (config docstring), not this reader's.
        """
        params = {"db": DB, "retmode": "xml", "tool": self._config.tool or "factlog"}
        if self._config.email:
            params["email"] = self._config.email
        if self._config.api_key:
            params["api_key"] = self._config.api_key
        return params

    def _emit_no_key_warning_once(self) -> None:
        if self._config.api_key or self._warned:
            return
        self._warned = True
        self._warn(NO_KEY_WARNING)

    @staticmethod
    def _retry_after(response: _Response) -> float:
        raw = response.headers.get("retry-after") or response.headers.get("Retry-After")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return DEFAULT_RETRY_AFTER_SECONDS

    def _request(self, endpoint: str, params: dict) -> str:
        """Send one E-utilities request, returning the raw 200 body.

        Honours the rate limiter and 429 ``Retry-After`` (spike §3). A 400 is a
        rejected request (:class:`PubMedRequestError`, carrying NCBI's own
        ``<ERROR>`` text); any other non-200 is a generic :class:`PubMedError`.
        A network failure raises :class:`PubMedConnectionError` from the transport
        before any body exists. The 200 body is returned *unparsed* for #163.
        """
        self._emit_no_key_warning_once()
        full = {**self._common_params(), **params}
        for attempt in range(MAX_ATTEMPTS):
            # Re-armed on every attempt so a retry still honours the interval.
            self._limiter.wait()
            response = self.transport(endpoint, full)
            status = response.status_code
            if status == 200:
                return response.text
            if status == 429:
                if attempt == MAX_ATTEMPTS - 1:
                    raise PubMedServiceError(
                        "NCBI is rate limiting (HTTP 429) after "
                        f"{MAX_ATTEMPTS} attempts; back off before retrying."
                    )
                self._sleep(self._retry_after(response))
                continue
            if status == 400:
                raise PubMedRequestError(
                    f"E-utilities rejected the request (HTTP 400): "
                    f"{_error_text(response.text) or 'malformed request'}"
                )
            raise PubMedError(f"E-utilities returned HTTP {status} for {endpoint}.")
        raise PubMedError("E-utilities request failed.")  # pragma: no cover

    def _id_param(self, pmids) -> str:
        """Validate, de-duplicate and cap the requested ids, joined for the wire."""
        wanted: list[str] = []
        seen: set[str] = set()
        for value in pmids:
            pmid = normalize_pmid(value)
            if pmid not in seen:
                seen.add(pmid)
                wanted.append(pmid)
        if not wanted:
            raise PubMedError("at least one PMID is required.")
        if len(wanted) > MAX_ID_LIST:
            raise PubMedError(
                f"E-utilities accepts at most {MAX_ID_LIST} ids per request, got {len(wanted)}."
            )
        return ",".join(wanted)

    # -- queries -----------------------------------------------------------
    def esearch(
        self,
        query: str,
        *,
        retmax: int | None = None,
        retstart: int = 0,
    ) -> str:
        """Search PubMed, returning the raw ``eSearchResult`` XML.

        A zero-result answer is *not* an error here (spike §5): a well-formed
        query — or a malformed field — can legitimately match nothing, arriving as
        ``<Count>0</Count>`` in the raw body. The zero is returned as-is; judging
        whether it is *suspicious* belongs to #167, and parsing ``<IdList>`` /
        ``<Count>`` belongs to the caller, not this transport layer.
        """
        if not isinstance(query, str) or not query.strip():
            raise PubMedError("search query must be a non-empty string.")
        params: dict = {"term": query.strip(), "retstart": max(0, retstart)}
        if retmax is not None:
            if not isinstance(retmax, int) or isinstance(retmax, bool) or retmax < 0:
                raise PubMedError(f"retmax must be a non-negative integer, got {retmax!r}")
            params["retmax"] = retmax
        return self._request("esearch.fcgi", params)

    def efetch(self, pmids) -> str:
        """Fetch full records for one or more PMIDs, returning the raw XML.

        A deleted/nonexistent-but-valid id yields a *valid empty 200 body*
        (``<PubmedArticleSet/>``), never an exception (spike §5). A merged or
        dropped id surfaces as an *omission*: ``efetch`` returns records by
        omission, never by substitution (spike §4), so the caller recovers which
        ids were gone by diffing its requested list against the ``<PMID>``s in
        this raw body — that id-level parse, like all record parsing, is #163's.
        """
        return self._request("efetch.fcgi", {"id": self._id_param(pmids)})

    def esummary(self, pmids) -> str:
        """Fetch lightweight summaries for one or more PMIDs, returning raw XML.

        Unlike ``efetch``, ``esummary`` reports a deleted/nonexistent id *in band*
        as a ``<DocumentSummary>`` carrying an ``<error>`` child (spike §5);
        surfacing that per-id state is #163's parse, not this layer's.
        """
        return self._request("esummary.fcgi", {"id": self._id_param(pmids)})


def _error_text(text: str) -> str:
    """The ``<ERROR>`` element's text from a 400 body, when present.

    A transport-level nicety: it lifts NCBI's own rejection reason into the
    exception message. It does not parse records (that is #163's), only the tiny
    error envelope E-utilities returns for a malformed request.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return ""
    node = root.find(".//ERROR")
    if node is not None and node.text:
        return node.text.strip()
    return ""
