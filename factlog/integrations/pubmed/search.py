#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compose and read back a PubMed ``esearch`` query — the silent-zero guard (#167).

Pure functions over strings and an ``esearch`` XML body: no network, no
filesystem, no optional library, in the shape of ``mesh.py``/``work_parser.py``.
Kept in its own module so this lands additively beside #163/#164's
``work_parser.py`` and #166's import path.

**The silent-zero trap, and why this module exists.** ``esearch`` answers a
*malformed field tag* or a *nonexistent MeSH term* not with an error but with
HTTP 200 and ``<Count>0</Count>`` — indistinguishable, on the count alone, from
an honest empty set. The operator reads "no papers matched" and believes it. This
is the exact family of bug :mod:`factlog.integrations.arxiv.config` answers for
arXiv (an unknown ``cat:`` / field answers 200 + zero results, #57) and that #89
answered for a bare multi-word query. PubMed's own reply, however, carries more
than the count: a query it could not map surfaces an ``<ErrorList>`` /
``<WarningList>`` — ``<PhraseNotFound>``, ``<FieldNotFound>``,
``<QuotedPhraseNotFound>`` — beside the zero. The transport (#162) returns that
body raw and leaves judging whether a zero is *suspicious* to this layer (its
docstring says so explicitly); :func:`parse_esearch` and
:func:`silent_zero_report` are that judgement.

**Three guards, layered.** (1) A ``[field tag]`` the query names is validated
against a closed set *before* a request is spent (:func:`validate_field_tags`),
because an unknown tag is one of the silences above. (2) PubMed's own
``ErrorList``/``WarningList`` is surfaced verbatim — it is the authoritative
signal that a phrase or field was not found. (3) A **filtered** zero (a
``--year`` or ``--mesh`` was applied) is surfaced with the filters named, even
when PubMed volunteered no warning, because a nonexistent MeSH term is precisely
what produces that quiet, warning-less zero.

**The MeSH-validation decision (recorded here, per the issue).** Validating a
``--mesh`` term against the real MeSH vocabulary needs the MeSH tree — a
downloadable dataset, not a per-query fetch. The issue offered three options:
bundle a subset, lazily ``esearch`` the term alone, or surface-and-explain.
**This module takes surface-and-explain — the issue's stated floor.** A ``--mesh``
term is composed into a ``TERM[MeSH Terms]`` clause and sent as-is; if PubMed
cannot map it, guard (2) surfaces PubMed's ``PhraseNotFound`` and guard (3)
surfaces the filtered zero and names the term. No local MeSH list is shipped, so
none can drift out of date or false-reject a term newer than the bundle — the
authority stays PubMed's, and factlog's job is to stop its silence from reading
as absence.

**Multi-word ``--query`` (the #89 question, answered for PubMed).** Unlike arXiv,
factlog does **not** auto-quote a bare multi-word PubMed query. PubMed runs
Automatic Term Mapping (ATM): it expands the words against MeSH and journal
indexes and searches them together, which is usually the intended broad recall —
whereas quoting *disables* ATM and can collapse a real query to a
``QuotedPhraseNotFound`` zero. So a bare query is passed through verbatim and its
interpretation is made *visible* instead of *rewritten*: ``--show-query`` prints
the composed term, and a real search surfaces PubMed's own ``<QueryTranslation>``
so the operator sees exactly how ATM read their words. A user who wants a literal
phrase quotes it themselves, and guard (2) then surfaces any
``QuotedPhraseNotFound``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

__all__ = [
    "SEARCH_FIELD_TAGS",
    "MESH_FIELD",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "PUBMED_EPOCH_YEAR",
    "PubMedSearchValidationError",
    "validate_field_tags",
    "build_year_filter",
    "mesh_clause",
    "compose_query",
    "EsearchResult",
    "parse_esearch",
    "silent_zero_report",
]

# factlog policy, not an API constraint. The default result count is the issue's
# 25; the ceiling is the client's per-request id cap (MAX_ID_LIST=200), since the
# listing efetches the returned PMIDs and cannot page past one request here.
DEFAULT_LIMIT = 25
MAX_LIMIT = 200

# The field tag `--mesh` composes into. PubMed spells it `[MeSH Terms]` (abbrev
# `[mh]`); a term that maps to no descriptor answers `<PhraseNotFound>` + a zero.
MESH_FIELD = "MeSH Terms"

# Every search field tag PubMed's query parser understands, from NLM's "Search
# Field Descriptions and Tags". Both the full name and the abbreviation are
# accepted; matching is case-insensitive and whitespace-collapsed (see
# `_normalize_tag`). An unknown tag is NOT rejected by PubMed — it is folded into
# the query and answered with a zero or a `<FieldNotFound>` — so a tag the query
# names must be one of these or be refused before a request is spent.
#
# Like arXiv's CATEGORIES this vocabulary is stable but not frozen (NLM has added
# tags over the years). It must stay a superset of what PubMed accepts: an
# *incomplete* set false-rejects a valid query, the very failure arXiv's
# OLD_STYLE_ARCHIVES note warns against. When PubMed adds a tag, add it here.
_FIELD_TAG_NAMES = frozenset({
    "affiliation", "all fields", "article identifier", "author",
    "author identifier", "book", "completion date",
    "conflict of interest statement", "corporate author", "create date",
    "ec/rn number", "editor", "entry date", "filter", "first author name",
    "full author name", "full investigator name", "grants and funding",
    "investigator", "isbn", "issue", "journal", "language", "last author name",
    "location id", "mesh date", "mesh major topic", "mesh subheadings",
    "mesh subheading", "mesh terms", "modification date", "nlm unique id",
    "other term", "pagination", "personal name as subject",
    "pharmacological action", "place of publication", "pmid",
    "publication date", "date - publication", "publication type", "publisher",
    "secondary source id", "subset", "supplementary concept", "text word",
    "title", "title/abstract", "transliterated title", "volume",
})
_FIELD_TAG_ABBREVS = frozenset({
    "ad", "all", "aid", "au", "auid", "book", "dcom", "cois", "cn", "crdt",
    "rn", "ed", "edat", "sb", "1au", "fau", "fir", "gr", "ir", "isbn", "ip",
    "ta", "jour", "la", "lastau", "lid", "mhda", "majr", "sh", "mh", "lr",
    "jid", "ot", "pg", "ps", "pa", "pl", "pmid", "dp", "pt", "pubn", "si",
    "nm", "tw", "ti", "tiab", "tt", "vi",
})
SEARCH_FIELD_TAGS = _FIELD_TAG_NAMES | _FIELD_TAG_ABBREVS

# A `[...]` field tag in a query term, e.g. `crispr[MeSH Terms]` or `smith[au]`.
# PubMed tags are always bracketed and always trail their value.
_TAG_TOKEN_RE = re.compile(r"\[([^\[\]]+)\]")

# PubMed's advanced-search date picker bottoms out at 1781; MEDLINE/OLDMEDLINE
# reaches back that far, so the floor is set there rather than at MEDLINE's 1946
# to avoid false-rejecting a genuinely old record. A --year below it, or above
# next year, is a typo — and a typo must not read as "no such literature exists".
PUBMED_EPOCH_YEAR = 1781

# --year is `YYYY` or `YYYY-YYYY`. Anything else is rejected before a request.
_YEAR_RE = re.compile(r"^\s*([0-9]{4})(?:\s*-\s*([0-9]{4}))?\s*$")


class PubMedSearchValidationError(Exception):
    """A query value was rejected before a request was spent (unknown field tag, ...)."""


def _normalize_tag(tag: str) -> str:
    """Lowercase and whitespace-collapse a field tag for closed-set comparison.

    A PubMed sub-qualifier (`[mh:noexp]`, `[tiab:~2]`) carries the field before a
    colon; only the field part is validated, the modifier is PubMed's to read.
    """
    head = tag.split(":", 1)[0]
    return " ".join(head.split()).lower()


def validate_field_tags(query: str) -> str:
    """Return the query unchanged, or raise on a ``[field tag]`` PubMed will ignore.

    An unknown tag is not rejected by ``esearch`` — it is folded into the term and
    answered with a zero (or a ``<FieldNotFound>``), a silence that reads as "no
    such literature exists". Every bracketed tag the query names is therefore
    checked against :data:`SEARCH_FIELD_TAGS` here, before a request is spent, the
    same guard :func:`factlog.integrations.arxiv.config.validate_search_query`
    applies for arXiv.
    """
    if not isinstance(query, str) or not query.strip():
        raise PubMedSearchValidationError("search query must be a non-empty string.")
    for raw_tag in _TAG_TOKEN_RE.findall(query):
        normalized = _normalize_tag(raw_tag)
        if normalized not in SEARCH_FIELD_TAGS:
            raise PubMedSearchValidationError(
                f"unknown PubMed field tag [{raw_tag}]. PubMed answers an unknown tag "
                "with zero results rather than an error, so it is refused here. See "
                "NLM's Search Field Descriptions for valid tags (e.g. [Title], "
                "[Author], [MeSH Terms], [tiab])."
            )
    return query.strip()


def build_year_filter(year_spec: str) -> str:
    """Turn ``--year`` (``YYYY`` or ``YYYY-YYYY``) into a ``[Date - Publication]`` clause.

    A reversed or out-of-range span is a typo, and PubMed answers a typo'd date
    with a zero rather than an error — the same silent lie the field-tag and MeSH
    guards exist for — so the start must not exceed the end and each year must
    fall within PubMed's range (:data:`PUBMED_EPOCH_YEAR` .. next year). The
    bounds are emitted in PubMed's documented quoted ``[Date - Publication]`` form.

    Returns e.g. ``("2020"[Date - Publication] : "2025"[Date - Publication])``.
    """
    if not isinstance(year_spec, str) or not year_spec.strip():
        raise PubMedSearchValidationError("--year must be a year or range, e.g. 2023 or 2020-2025.")
    match = _YEAR_RE.match(year_spec)
    if match is None:
        raise PubMedSearchValidationError(
            f"invalid --year {year_spec!r}; expected a year or range, e.g. 2023 or 2020-2025."
        )

    start_year = int(match.group(1))
    end_year = int(match.group(2)) if match.group(2) else start_year

    from datetime import date

    ceiling = date.today().year + 1
    for year in (start_year, end_year):
        if not PUBMED_EPOCH_YEAR <= year <= ceiling:
            raise PubMedSearchValidationError(
                f"year {year} is outside PubMed's range ({PUBMED_EPOCH_YEAR}-{ceiling}); "
                "PubMed answers an out-of-range year with zero results rather than an error."
            )
    if start_year > end_year:
        raise PubMedSearchValidationError(
            f"--year range {start_year}-{end_year} runs backwards; PubMed answers a "
            "reversed range with zero results rather than an error."
        )
    return (
        f'("{start_year}"[Date - Publication] : "{end_year}"[Date - Publication])'
    )


def mesh_clause(term: str) -> str:
    """Compose one ``--mesh`` term into a ``TERM[MeSH Terms]`` clause.

    The term is *not* validated against a local MeSH list (see module docstring's
    recorded decision): it is sent as-is, and a term PubMed cannot map is surfaced
    by :func:`silent_zero_report`, never swallowed. A quote inside the term would
    unbalance the clause, so it is refused rather than sent to match loosely.
    """
    if not isinstance(term, str) or not term.strip():
        raise PubMedSearchValidationError("--mesh term must be a non-empty string.")
    cleaned = term.strip()
    if '"' in cleaned:
        raise PubMedSearchValidationError(
            f"--mesh term {term!r} contains a quote; supply the descriptor without quotes "
            "(factlog tags it [MeSH Terms] for you)."
        )
    return f"{cleaned}[{MESH_FIELD}]"


def compose_query(query: str, *, year: str | None = None, mesh=()) -> str:
    """The exact ``term`` an ``esearch`` will send. Pure: it spends no request.

    Shared by the search command and ``--show-query`` so the string an operator is
    shown is the string that would be sent. The bare ``query`` is passed through
    verbatim after field-tag validation — PubMed's Automatic Term Mapping reads a
    multi-word query, and factlog makes that reading visible (``--show-query`` and
    ``<QueryTranslation>``) rather than rewriting it (see module docstring). The
    ``--mesh`` and ``--year`` filters are AND-combined onto it.
    """
    clauses = [validate_field_tags(query)]
    for term in mesh:
        clauses.append(mesh_clause(term))
    if year:
        clauses.append(build_year_filter(year))
    return " AND ".join(clauses)


# -- reading esearch back ---------------------------------------------------

@dataclass(frozen=True)
class EsearchResult:
    """An ``eSearchResult`` body reduced to what the silent-zero guard needs.

    ``count`` is PubMed's total match count (the number the operator would read),
    ``ids`` the returned PMIDs (capped by ``retmax``). ``query_translation`` is
    PubMed's own rendering of the query after Automatic Term Mapping — the honest
    answer to "how did PubMed read my words". ``errors`` and ``warnings`` are the
    ``<ErrorList>``/``<WarningList>`` children verbatim as ``(tag, text)`` pairs:
    a ``PhraseNotFound`` / ``FieldNotFound`` / ``QuotedPhraseNotFound`` is exactly
    how PubMed says a phrase or field could not be mapped, and is the
    authoritative signal that a zero is not honest.
    """

    count: int = 0
    ids: tuple[str, ...] = ()
    query_translation: str | None = None
    errors: tuple[tuple[str, str], ...] = ()
    warnings: tuple[tuple[str, str], ...] = ()
    top_level_error: str | None = None


def _child_signals(parent: ET.Element | None) -> tuple[tuple[str, str], ...]:
    """The ``(tag, text)`` of each child of an ``ErrorList``/``WarningList``."""
    if parent is None:
        return ()
    signals: list[tuple[str, str]] = []
    for child in parent:
        text = (child.text or "").strip()
        signals.append((child.tag, text))
    return tuple(signals)


def parse_esearch(xml_text: str) -> EsearchResult:
    """Reduce a raw ``eSearchResult`` body to an :class:`EsearchResult`.

    Belongs to the search command, not the transport (#162's client returns the
    body raw and defers judging a suspicious zero to #167). A body that is not
    parseable XML, or whose root carries a top-level ``<ERROR>`` (e.g. a bad
    ``db``), is reported via ``top_level_error`` rather than raising, so a caller
    can surface it beside the count instead of crashing.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return EsearchResult(top_level_error=f"unparseable esearch response: {exc}")

    # A whole-request rejection can arrive as a bare <ERROR> root or child.
    top_error_node = root if root.tag == "ERROR" else root.find("ERROR")
    top_level_error = None
    if top_error_node is not None and (top_error_node.text or "").strip():
        top_level_error = top_error_node.text.strip()

    count_node = root.find("Count")
    try:
        count = int((count_node.text or "0").strip()) if count_node is not None else 0
    except (TypeError, ValueError):
        count = 0

    ids = tuple(
        (node.text or "").strip()
        for node in root.findall("./IdList/Id")
        if (node.text or "").strip()
    )

    qt_node = root.find("QueryTranslation")
    query_translation = None
    if qt_node is not None and (qt_node.text or "").strip():
        query_translation = qt_node.text.strip()

    return EsearchResult(
        count=count,
        ids=ids,
        query_translation=query_translation,
        errors=_child_signals(root.find("ErrorList")),
        warnings=_child_signals(root.find("WarningList")),
        top_level_error=top_level_error,
    )


# How PubMed names the not-found signals inside ErrorList/WarningList, mapped to a
# human line. An unlisted tag still surfaces (see `_signal_line`) — the map only
# adds guidance where the tag name alone is opaque.
_SIGNAL_EXPLANATIONS = {
    "PhraseNotFound": (
        "PubMed could not map {value!r} to any indexed term. A nonexistent MeSH "
        "term produces exactly this — verify the descriptor exists."
    ),
    "QuotedPhraseNotFound": (
        "PubMed found no match for the exact phrase {value!r} (quoting disables "
        "Automatic Term Mapping; drop the quotes to search the words loosely)."
    ),
    "FieldNotFound": (
        "PubMed did not recognize the field {value!r} in the query."
    ),
    "PhraseIgnored": (
        "PubMed ignored {value!r} while running the query."
    ),
}


def _signal_line(tag: str, text: str) -> str:
    template = _SIGNAL_EXPLANATIONS.get(tag)
    if template is not None and text:
        return template.format(value=text)
    if text:
        return f"PubMed reported {tag}: {text}"
    return f"PubMed reported {tag}."


def silent_zero_report(result: EsearchResult, *, year=None, mesh=()) -> list[str]:
    """The lines to surface so a suspicious zero never reads as an honest empty set.

    Returns, in order:

    * every ``<ErrorList>``/``<WarningList>`` signal PubMed volunteered — the
      authoritative "a phrase/field was not found" (surfaced at *any* count, since
      PubMed reporting it means it dropped part of the query);
    * a top-level ``<ERROR>`` if the whole request was rejected;
    * when the count is zero **and a filter was applied** but PubMed volunteered no
      signal, a line naming the ``--year``/``--mesh`` filters and stating that a
      nonexistent MeSH term produces precisely this quiet zero.

    An honest empty set — zero results, no filter, no PubMed signal — yields ``[]``,
    so a plain "0 results" is left to stand. Pure: the caller writes these to
    stderr.
    """
    lines: list[str] = []
    if result.top_level_error:
        lines.append(f"PubMed rejected the request: {result.top_level_error}")
    for tag, text in result.errors:
        lines.append(_signal_line(tag, text))
    for tag, text in result.warnings:
        lines.append(_signal_line(tag, text))

    mesh = tuple(mesh or ())
    if result.count == 0 and not lines and (year or mesh):
        applied: list[str] = []
        if mesh:
            applied.append("MeSH term(s) " + ", ".join(repr(m) for m in mesh))
        if year:
            applied.append(f"year {year!r}")
        lines.append(
            "zero results, but a filter was applied: " + "; ".join(applied) + ". "
            "PubMed answers a nonexistent MeSH term (or an out-of-range year) with a "
            "silent zero, not an error — confirm each filter matches something before "
            "reading this as 'no such literature exists'."
        )
    return lines
