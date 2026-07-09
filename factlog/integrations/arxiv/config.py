#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""arXiv integration connection/import settings and the query vocabularies.

Mirrors :mod:`factlog.integrations.openalex.config`: a small TOML reader with the
precedence

    explicit path arg
        >  <kb>/policy/arxiv-config.toml
        >  ${XDG_CONFIG_HOME:-~/.config}/factlog/arxiv.toml
        >  built-in defaults

arXiv has no credentials — no key, no registration, no polite pool. ``email`` is
an identification courtesy carried in the User-Agent, so a KB-scoped policy file
is safe here, as it is for OpenAlex.

**Why the two frozensets below exist.** arXiv answers a *bogus query value* with
``200`` and zero results (#57): ``cat:cs.NOTAREALCAT`` and even the unknown field
``bogusfield:x`` both report "0 results". The operator reads that as "no such
literature exists". This is the same silent lie that
:data:`factlog.integrations.openalex.api_client.WORK_TYPES` exists to prevent —
except arXiv is worse, because OpenAlex at least answers ``400`` for an unknown
*field*. Values are therefore validated here, before a request is spent.

``tomllib`` is stdlib on 3.11+, so this module has no third-party imports.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# factlog policy, not an API constraint: the API serves max_results=5000 (a
# 10 MB body) and only fails at 30000, with HTTP 500. Do not "fix" this upward
# without also fixing the truncation risk documented in `client.py`.
MAX_LIMIT = 200
DEFAULT_LIMIT = 25

# The API's own default page size. Measured: an `id_list` of 15 ids sent without
# `max_results` returns *ten* entries and `totalResults=15` — the remaining five
# vanish with no error (#57). Every id_list request must therefore set
# max_results >= len(id_list).
API_DEFAULT_MAX_RESULTS = 10

# arXiv recommends one request per three seconds. It is a courtesy, not an
# enforced limit: twelve zero-delay requests all answered 200 (#57). Nothing on
# the wire will catch a regression here, so the delay is enforced by unit test.
REQUEST_DELAY_SECONDS = 3.0

# Pre-2007 identifiers are `archive/YYMMNNN`. The old scheme was retired in
# April 2007, so this set is *frozen* — a complete list stays correct forever,
# unlike WORK_TYPES which OpenAlex can extend. It must be exhaustive or absent:
# an incomplete list false-rejects valid historical ids, which is the very bug
# the spec's `^[a-z-]+\.[A-Z]{2}/[0-9]{7}$` regex had (it rejects `hep-th/...`).
OLD_STYLE_ARCHIVES = frozenset({
    # active-era archives that survived into the new scheme
    "astro-ph", "cond-mat", "cs", "gr-qc", "hep-ex", "hep-lat", "hep-ph",
    "hep-th", "math", "math-ph", "nlin", "nucl-ex", "nucl-th", "physics",
    "q-bio", "quant-ph",
    # archives retired or folded into others before/at the 2007 cutover
    "acc-phys", "adap-org", "alg-geom", "ao-sci", "atom-ph", "bayes-an",
    "chao-dyn", "chem-ph", "cmp-lg", "comp-gas", "dg-ga", "funct-an",
    "mtrl-th", "patt-sol", "plasm-ph", "q-alg", "solv-int", "supr-con",
})

# Every category `cat:` accepts, from https://arxiv.org/category_taxonomy.
# Nine archives (`hep-th`, `quant-ph`, ...) are themselves categories and carry
# no subject class; the rest are `archive.SUBJECT`.
#
# Unlike OLD_STYLE_ARCHIVES this vocabulary *grows* — `econ.*` and `eess.*` post-
# date the original taxonomy — so it needs periodic regeneration. Regenerate
# with: tools/refresh_arxiv_categories.py
CATEGORIES = frozenset({
    "astro-ph.CO", "astro-ph.EP", "astro-ph.GA", "astro-ph.HE", "astro-ph.IM",
    "astro-ph.SR", "cond-mat.dis-nn", "cond-mat.mes-hall", "cond-mat.mtrl-sci",
    "cond-mat.other", "cond-mat.quant-gas", "cond-mat.soft",
    "cond-mat.stat-mech", "cond-mat.str-el", "cond-mat.supr-con",
    "cs.AI", "cs.AR", "cs.CC", "cs.CE", "cs.CG", "cs.CL", "cs.CR", "cs.CV",
    "cs.CY", "cs.DB", "cs.DC", "cs.DL", "cs.DM", "cs.DS", "cs.ET", "cs.FL",
    "cs.GL", "cs.GR", "cs.GT", "cs.HC", "cs.IR", "cs.IT", "cs.LG", "cs.LO",
    "cs.MA", "cs.MM", "cs.MS", "cs.NA", "cs.NE", "cs.NI", "cs.OH", "cs.OS",
    "cs.PF", "cs.PL", "cs.RO", "cs.SC", "cs.SD", "cs.SE", "cs.SI", "cs.SY",
    "econ.EM", "econ.GN", "econ.TH",
    "eess.AS", "eess.IV", "eess.SP", "eess.SY",
    "gr-qc", "hep-ex", "hep-lat", "hep-ph", "hep-th",
    "math-ph",
    "math.AC", "math.AG", "math.AP", "math.AT", "math.CA", "math.CO",
    "math.CT", "math.CV", "math.DG", "math.DS", "math.FA", "math.GM",
    "math.GN", "math.GR", "math.GT", "math.HO", "math.IT", "math.KT",
    "math.LO", "math.MG", "math.MP", "math.NA", "math.NT", "math.OA",
    "math.OC", "math.PR", "math.QA", "math.RA", "math.RT", "math.SG",
    "math.SP", "math.ST",
    "nlin.AO", "nlin.CD", "nlin.CG", "nlin.PS", "nlin.SI",
    "nucl-ex", "nucl-th",
    "physics.acc-ph", "physics.ao-ph", "physics.app-ph", "physics.atm-clus",
    "physics.atom-ph", "physics.bio-ph", "physics.chem-ph", "physics.class-ph",
    "physics.comp-ph", "physics.data-an", "physics.ed-ph", "physics.flu-dyn",
    "physics.gen-ph", "physics.geo-ph", "physics.hist-ph", "physics.ins-det",
    "physics.med-ph", "physics.optics", "physics.plasm-ph", "physics.pop-ph",
    "physics.soc-ph", "physics.space-ph",
    "q-bio.BM", "q-bio.CB", "q-bio.GN", "q-bio.MN", "q-bio.NC", "q-bio.OT",
    "q-bio.PE", "q-bio.QM", "q-bio.SC", "q-bio.TO",
    "q-fin.CP", "q-fin.EC", "q-fin.GN", "q-fin.MF", "q-fin.PM", "q-fin.PR",
    "q-fin.RM", "q-fin.ST", "q-fin.TR",
    "quant-ph",
    "stat.AP", "stat.CO", "stat.ME", "stat.ML", "stat.OT", "stat.TH",
})

# Field prefixes `search_query` understands. An unknown prefix is not rejected by
# the API — `bogusfield:x` answers 200 with zero results — so a query the client
# builds must only ever use these.
SEARCH_FIELDS = frozenset({"ti", "au", "abs", "co", "jr", "cat", "rn", "id", "all"})

# A `field:value` token in a Lucene-style query. The value stops at whitespace
# unless it is quoted, so `ti:"chain of thought"` is one token. A bare colon
# inside a quoted phrase (`ti:"a: b"`) is not treated as a field.
_FIELD_TOKEN_RE = re.compile(r'(?:^|[\s(])([A-Za-z_]+):("[^"]*"|\S+)')

# `sortBy` values. Unlike the above, a bogus one *is* rejected (HTTP 400).
SORT_FIELDS = {"submitted": "submittedDate", "updated": "lastUpdatedDate",
               "relevance": "relevance"}

# arXiv's first submission was August 1991; nothing predates it. A --year below
# this, or above next year, is a typo, not a query — and a typo must not read as
# "no such literature exists".
ARXIV_EPOCH_YEAR = 1991

# --year is `YYYY` or `YYYY-YYYY`. Anything else is rejected before a request.
_YEAR_RE = re.compile(r"^\s*([0-9]{4})(?:\s*-\s*([0-9]{4}))?\s*$")


class ArxivConfigError(Exception):
    """An arXiv settings file was named but could not be read/parsed/validated."""


class ArxivValidationError(Exception):
    """A query value was rejected before a request was spent (unknown category, ...)."""


@dataclass(frozen=True)
class ArxivConfig:
    """Resolved arXiv client + import settings.

    ``email`` is optional and unauthenticated; it travels in the User-Agent so
    arXiv's operators can contact heavy users. ``skip_duplicates`` is what makes
    re-import idempotent (P3), matching the Zotero and OpenAlex importers.
    """

    email: str = ""
    default_limit: int = DEFAULT_LIMIT
    max_limit: int = MAX_LIMIT
    request_delay: float = REQUEST_DELAY_SECONDS
    default_target: str = ""
    skip_duplicates: bool = True
    include_abstract: bool = True


def validate_category(value: str) -> str:
    """Return a known arXiv category, or raise before a request is spent.

    An unknown category is rejected here rather than sent, because the API
    answers a bogus ``cat:`` value with ``200`` and zero results — a silent lie
    that reads as "no such literature exists" (#57).
    """
    if not isinstance(value, str) or not value.strip():
        raise ArxivValidationError("category must be a non-empty string.")
    candidate = value.strip()
    if candidate not in CATEGORIES:
        raise ArxivValidationError(
            f"unknown arXiv category {value!r}. See https://arxiv.org/category_taxonomy "
            f"for the {len(CATEGORIES)} valid categories (e.g. 'cs.CL', 'stat.ML')."
        )
    return candidate


def validate_search_query(query: str) -> str:
    """Return the query unchanged, or raise on a field prefix arXiv will ignore.

    ``bogusfield:anything`` answers ``200`` with zero results — arXiv validates
    neither the field name nor, for ``cat:``, the value (#57). Both silences read
    as "no such literature exists", so both are caught here. Any ``cat:`` value
    found in the query is checked against :func:`validate_category`.
    """
    if not isinstance(query, str) or not query.strip():
        raise ArxivValidationError("search query must be a non-empty string.")

    for field, value in _FIELD_TOKEN_RE.findall(query):
        if field.lower() not in SEARCH_FIELDS:
            known = ", ".join(sorted(SEARCH_FIELDS))
            raise ArxivValidationError(
                f"unknown arXiv search field {field!r}; expected one of: {known}. "
                "arXiv answers an unknown field with zero results rather than an error."
            )
        if field.lower() == "cat" and value:
            validate_category(value.strip('"'))
    return query.strip()


def validate_sort(value: str) -> str:
    """Translate factlog's ``--sort`` into arXiv's ``sortBy`` value."""
    if not isinstance(value, str) or value.strip() not in SORT_FIELDS:
        known = ", ".join(sorted(SORT_FIELDS))
        raise ArxivValidationError(f"invalid sort {value!r}; expected one of: {known}")
    return SORT_FIELDS[value.strip()]


def compose_search_query(
    query: str, categories=(), year: str | None = None, *, today: date | None = None
) -> str:
    """The exact ``search_query`` a search will send. Pure: it spends no request.

    Shared by :meth:`ArxivClient.search` and ``arxiv-search --dry-run`` so the
    string an operator is shown is the string that would be sent — not a
    reconstruction of it that can drift.
    """
    clauses = [validate_search_query(query)]
    for category in categories:
        clauses.append(f"cat:{validate_category(category)}")
    if year:
        clauses.append(build_submitted_date(year, today=today))
    return " AND ".join(clauses)


def build_submitted_date(year_spec: str, *, today: date | None = None) -> str:
    """Turn ``--year`` (``YYYY`` or ``YYYY-YYYY``) into a ``submittedDate:`` clause.

    Measured against the live API (#80), two silent traps make this a
    validate-and-expand step rather than a passthrough:

    * **A reversed or out-of-range span answers 200 with zero results**, not an
      error — ``[...2359 TO ...0000]`` and a year like 2099 both read as "no such
      literature exists". So the start must not exceed the end, and each year
      must fall within arXiv's lifetime (``1991`` .. next year).
    * **Only syntactic garbage 500s** (``[abc TO def]``), which is too late and
      too coarse to guide the operator. Everything catchable is caught here,
      before a request is spent.

    The bounds are emitted in arXiv's documented ``YYYYMMDDTTTT`` form rather than
    as bare years. Note that a bare year is *not* reinterpreted: measured on the
    same span, ``[2020 TO 2021]`` and ``[202001010000 TO 202112312359]`` return an
    identical count (15208), as do the one-year forms (7125). An earlier draft
    claimed otherwise, having compared a two-year bare span against a one-year full
    span. The full form is still what we send — it is the documented one, and it
    states the intended bounds without relying on how arXiv widens a bare year —
    but it buys correctness of *expression*, not of *result*.

    ``today`` may be injected, and the tests do. It is *not* the injected-clock
    rule ``BaseSourceWriter`` and ``provenance`` follow: that rule exists because
    those values are written into a user's KB and must be reproducible. This one
    only bounds a search filter, is never persisted, and defaults to the clock so a
    library caller need not supply a date to search by year.

    Returns e.g. ``submittedDate:[202001010000 TO 202012312359]``.
    """
    if not isinstance(year_spec, str) or not year_spec.strip():
        raise ArxivValidationError("--year must be a year or range, e.g. 2023 or 2020-2025.")
    match = _YEAR_RE.match(year_spec)
    if match is None:
        raise ArxivValidationError(
            f"invalid --year {year_spec!r}; expected a year or range, e.g. 2023 or 2020-2025."
        )

    start_year = int(match.group(1))
    end_year = int(match.group(2)) if match.group(2) else start_year

    ceiling = (today or date.today()).year + 1
    for year in (start_year, end_year):
        if not ARXIV_EPOCH_YEAR <= year <= ceiling:
            raise ArxivValidationError(
                f"year {year} is outside arXiv's range ({ARXIV_EPOCH_YEAR}-{ceiling}); "
                "arXiv answers an out-of-range year with zero results rather than an error."
            )
    if start_year > end_year:
        raise ArxivValidationError(
            f"--year range {start_year}-{end_year} runs backwards; arXiv answers a "
            "reversed range with zero results rather than an error."
        )

    return f"submittedDate:[{start_year}01010000 TO {end_year}12312359]"


def xdg_config_path() -> Path:
    """The user-level arXiv settings path, next to factlog's own config."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "factlog" / "arxiv.toml"


def default_config_paths(kb_root: Path | str | None = None) -> list[Path]:
    """Auto-discovery search order: KB-scoped policy file, then user-level file."""
    paths: list[Path] = []
    if kb_root is not None:
        paths.append(Path(kb_root) / "policy" / "arxiv-config.toml")
    paths.append(xdg_config_path())
    return paths


def _as_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _as_bool(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_limit(value: object, default: int) -> int:
    # bool is an int subclass; a stray `true` must not read as limit 1.
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_LIMIT:
        return value
    return default


def _as_delay(value: object, default: float) -> float:
    # A delay below the recommendation is accepted but never silently: arXiv will
    # not push back on it (#57), so an operator lowering it takes the risk
    # knowingly. Non-numeric or negative values fall back.
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return default


def from_mapping(data: dict) -> ArxivConfig:
    """Build an :class:`ArxivConfig` from a parsed TOML mapping.

    Wrong-typed value fields fall back to defaults (graceful). A non-string
    ``client.email`` fails loud: it is echoed into the User-Agent of every
    request, so a typo there should not be silently dropped.
    """
    client = data.get("client", {})
    imp = data.get("import", {})
    if not isinstance(client, dict):
        client = {}
    if not isinstance(imp, dict):
        imp = {}

    raw_email = client.get("email", "")
    if not isinstance(raw_email, str):
        raise ArxivConfigError(f"client email must be a string, got {type(raw_email).__name__}")

    max_limit = _as_limit(imp.get("max_limit"), MAX_LIMIT)
    default_limit = _as_limit(imp.get("default_limit"), DEFAULT_LIMIT)
    if default_limit > max_limit:
        default_limit = max_limit

    return ArxivConfig(
        email=raw_email.strip(),
        default_limit=default_limit,
        max_limit=max_limit,
        request_delay=_as_delay(client.get("request_delay"), REQUEST_DELAY_SECONDS),
        default_target=_as_str(imp.get("default_target")),
        skip_duplicates=_as_bool(imp.get("skip_duplicates"), True),
        include_abstract=_as_bool(imp.get("include_abstract"), True),
    )


def _load_file(path: Path) -> ArxivConfig:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ArxivConfigError(f"invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ArxivConfigError(f"cannot read {path}: {exc}") from exc
    return from_mapping(data)


def load_config(
    path: Path | str | None = None,
    kb_root: Path | str | None = None,
) -> ArxivConfig:
    """Resolve arXiv settings following the module precedence.

    ``path`` names an explicit settings file (a missing one is an error, since
    the caller pointed at it). With no ``path``, auto-discovery walks
    :func:`default_config_paths`; if none exist, built-in defaults are returned.
    """
    if path is not None:
        explicit = Path(path).expanduser()
        if not explicit.is_file():
            raise ArxivConfigError(f"arxiv config not found: {explicit}")
        return _load_file(explicit)

    for candidate in default_config_paths(kb_root):
        if candidate.is_file():
            return _load_file(candidate)
    return ArxivConfig()
