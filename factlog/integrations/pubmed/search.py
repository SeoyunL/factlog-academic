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

One measured caveat, which the advice in guard (2) is built around: PubMed raises
``QuotedPhraseNotFound`` for **unquoted** queries too, quoting the phrase itself in
the warning text (a live ``qzxwvunonsenseterm`` sent without quotes comes back as
``<QuotedPhraseNotFound>"qzxwvunonsenseterm"</QuotedPhraseNotFound>`` while
``<QueryTranslation>qzxwvunonsenseterm</QueryTranslation>`` shows ATM was never
disabled). So the tag alone does not mean the user quoted anything, and factlog only
advises dropping quotes when *the query we sent* actually quoted **the very phrase
PubMed named** — a query may quote one phrase and leave another bare, and telling the
operator to unquote the bare one would be the same unactionable advice in a new dress
(#272).
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
    "parse_year_range",
    "build_year_filter",
    "mesh_clause",
    "compose_query",
    "EsearchResult",
    "parse_esearch",
    "silent_zero_report",
    "year_range_report",
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
    "publication date", "publication type", "publisher",
    "secondary source id", "subset", "supplementary concept", "text word",
    "title", "title/abstract", "transliterated title", "volume",
    # PubMed's Advanced Search builder shows the date fields as "Date - X"; the
    # bare "X date" forms above are the older spellings. Both must pass or a query
    # copied out of the builder false-rejects. Each pairs with an abbreviation
    # below: dp/crdt/dcom/edat/mhda/lr.
    "date - publication", "date - create", "date - completion",
    "date - entry", "date - entrez", "date - mesh", "date - modification",
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


def parse_year_range(year_spec: str) -> tuple[int, int]:
    """Read ``--year`` (``YYYY`` or ``YYYY-YYYY``) into validated ``(start, end)``.

    A reversed or out-of-range span is a typo, and PubMed answers a typo'd date
    with a zero rather than an error — the same silent lie the field-tag and MeSH
    guards exist for — so the start must not exceed the end and each year must
    fall within PubMed's range (:data:`PUBMED_EPOCH_YEAR` .. next year).

    Split out of :func:`build_year_filter` so the composed clause and the
    after-the-fact range check (:func:`year_range_report`) read the operator's
    ``--year`` through *one* parser. Two readings of the same spec is exactly the
    two-sources-of-truth split #387 is about; one is enough.
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
    return start_year, end_year


def build_year_filter(year_spec: str) -> str:
    """Turn ``--year`` into a ``[Date - Publication]`` clause.

    The bounds :func:`parse_year_range` validated are emitted in PubMed's
    documented quoted form, e.g.
    ``("2020"[Date - Publication] : "2025"[Date - Publication])``.

    Note what this tag matches, because it is *not* the year factlog records:
    ``[Date - Publication]`` matches a record on its **electronic** publication
    date too (``ArticleDate``), while the front matter ``year`` comes from the
    journal issue's ``PubDate`` (``work_parser._pub_date``). For a
    ``PubModel="Print-Electronic"`` paper — online one year, in print the next —
    those disagree. factlog keeps the idiomatic PubMed filter and surfaces the
    disagreement instead of silently narrowing the search or rewriting the
    recorded year: see :func:`year_range_report` (#387).
    """
    start_year, end_year = parse_year_range(year_spec)
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
    authoritative signal that a zero is not honest. They also carry non-diagnostic
    boilerplate — a zero always brings an ``OutputMessage`` along — because this is
    a faithful reduction of the response, not a judgement about it: which signals
    are diagnostic is decided by :func:`silent_zero_report`, not here.
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
    # `QuotedPhraseNotFound` is deliberately absent: its line depends on a fact the
    # response cannot carry — did *our request* quote the phrase PubMed named? — so it
    # is built by `_quoted_phrase_line` from `_QUOTED_PHRASE_LINES` instead (#272).
    "FieldNotFound": (
        "PubMed did not recognize the field {value!r} in the query."
    ),
    "PhraseIgnored": (
        "PubMed ignored {value!r} while running the query."
    ),
}


# NCBI puts <OutputMessage>No items found.</OutputMessage> on *every* zero-count
# esearch response — the valid-MeSH filtered zero and the nonsense query alike.
# It is the boilerplate companion of a zero, not a diagnostic PubMed volunteered,
# and echoing it says nothing "Found 0 results." did not already say (#271).
# Matched by tag name only: matching the text would be a gate NCBI could silently
# break by rewording its own boilerplate.
_BOILERPLATE_SIGNALS = frozenset({"OutputMessage"})


# One sentence per state, never one template plus a bolt-on clause: the "those quotes
# are PubMed's own" fact is *false* when the user really did quote, and appending the
# advice to it produced a line that denied and then asserted the same thing (#272).
# Keyed by `_user_quoted_phrase`'s three answers.
_QUOTED_PHRASE_LINES = {
    True: (
        "PubMed found no match for the quoted phrase {value!r}. Quoting disables "
        "Automatic Term Mapping — drop the quotes to search the words loosely."
    ),
    False: (
        "PubMed found no match for the phrase {value!r} in its phrase index. The query "
        "did not quote that phrase: the quotes are PubMed's own, added in its warning, "
        "so Automatic Term Mapping was never disabled."
    ),
    # Unknown: state only what PubMed reported, and assert nothing about the input.
    # No pointer to a printed line — `--porcelain` prints no QueryTranslation row.
    None: (
        "PubMed found no match for the phrase {value!r} in its phrase index. The quotes "
        "in that warning are PubMed's own and do not mean the query was quoted; PubMed's "
        "QueryTranslation shows how it actually read the query."
    ),
}


def _user_quoted_phrase(text: str, query: str | None) -> bool | None:
    """Did *the request we sent* wrap the phrase PubMed named in quotes?

    A deterministic check against the request string, never a match on PubMed's prose:
    PubMed quotes the phrase inside its own warning whether or not the query had any, so
    reading the response would answer "yes" every time. Scoped to the *named* phrase, not
    to the query as a whole — ``'"gene therapy" AND foo'`` quotes one phrase and leaves
    the other bare, and advising the operator to unquote ``foo`` would be the original
    bug in a new query. ``None`` (no query given) means unknown; nothing is then asserted.
    """
    if query is None:
        return None
    phrase = text.strip('"')
    return f'"{phrase}"' in query


def _quoted_phrase_line(text: str, *, query: str | None) -> str:
    return _QUOTED_PHRASE_LINES[_user_quoted_phrase(text, query)].format(value=text)


def _signal_line(tag: str, text: str, *, query: str | None = None) -> str:
    if tag == "QuotedPhraseNotFound" and text:
        return _quoted_phrase_line(text, query=query)
    template = _SIGNAL_EXPLANATIONS.get(tag)
    if template is not None and text:
        return template.format(value=text)
    if text:
        return f"PubMed reported {tag}: {text}"
    return f"PubMed reported {tag}."


def silent_zero_report(
    result: EsearchResult, *, year=None, mesh=(), query: str | None = None
) -> list[str]:
    """The lines to surface so a suspicious zero never reads as an honest empty set.

    Returns, in order:

    * a top-level ``<ERROR>`` if the whole request was rejected;
    * every *diagnostic* ``<ErrorList>``/``<WarningList>`` signal PubMed volunteered —
      the authoritative "a phrase/field was not found" (surfaced at *any* count, since
      PubMed reporting it means it dropped part of the query). ``OutputMessage`` is
      excluded **at a zero count**: NCBI attaches it to every zero, so there it is the
      boilerplate companion of the count rather than a diagnostic, and repeating it
      adds nothing the count did not already say. At a non-zero count it is surfaced
      like any other signal (see ``_BOILERPLATE_SIGNALS``);
    * whenever the count is zero **and a filter was applied**, a line naming the
      ``--year``/``--mesh`` filters and stating that a nonexistent MeSH term produces
      precisely this quiet zero — appended *after* any diagnostic line, since a
      nonexistent term yields both. The only exception is a top-level ``<ERROR>``,
      where the count itself cannot be trusted.

    An honest empty set — zero results, no filter, no diagnostic signal — yields ``[]``,
    so a plain "0 results" is left to stand. Pure: the caller writes these to
    stderr.

    ``query`` is the raw ``--query`` the user typed, and it exists for one question a
    ``QuotedPhraseNotFound`` cannot answer from the response alone: *did the user quote
    the phrase PubMed named?* PubMed wraps that phrase in quotes inside its own warning
    even when the query carried none, so reading the warning text would answer "yes"
    every time (#272). The check is therefore a deterministic fact about **the request we
    sent** (:func:`_user_quoted_phrase`), not a match against PubMed's prose, and it is
    made per signal — the phrase PubMed named, not the query as a whole, is what the
    advice would tell the operator to unquote. Three states: quoted (the ATM advice is
    true), not quoted (the fact, and that the quotes are PubMed's own), and ``None`` —
    the default, unknown, asserting nothing about the user's input.
    """
    lines: list[str] = []
    if result.top_level_error:
        lines.append(f"PubMed rejected the request: {result.top_level_error}")
    for tag, text in (*result.errors, *result.warnings):
        # Boilerplate only *because* the count is zero. Should NCBI ever attach an
        # OutputMessage to a non-zero response it is telling us something the count
        # does not, so it is surfaced — suppressing it unconditionally would re-open
        # the swallowed-diagnostic failure mode #167 exists to close.
        if tag in _BOILERPLATE_SIGNALS and result.count == 0:
            continue
        lines.append(_signal_line(tag, text, query=query))

    mesh = tuple(mesh or ())
    if result.count == 0 and (year or mesh) and not result.top_level_error:
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


# Why a --year search can record a year the operator did not ask for, one paragraph
# per *cause*. Never one paragraph for several: the electronic-date story is plainly
# false for a record that has no ArticleDate at all, and a warning whose explanation
# is wrong sends the operator hunting for a field that is not there — worse than no
# explanation, because it is confidently wrong. `pub_date_raw` is what tells the
# causes apart, and `work_parser` states that is the field's purpose: it "makes a
# derived-or-absent year auditable instead of silent". Reporting a derived year
# without it would discard the audit trail kept for exactly this moment — and the
# *absent* half of that sentence is why the no-year cause quotes it too (#389).
_YEAR_CAUSE_ELECTRONIC = (
    "PubMed's date filter also matches a record's electronic publication date, while "
    "factlog records the journal issue's year — the two differ when a paper appears "
    "online in one year and in print the next (PubModel=\"Print-Electronic\"). Such a "
    "record is a genuine match, not a bug: it is imported as usual and the exit code "
    "stays 0 — decide whether you want it in the KB."
)
_YEAR_CAUSE_MEDLINE = (
    "A record whose issue carries no plain <Year> is recorded with the first year of "
    "its free-text MedlineDate span, quoted above; a span can straddle two years, and "
    "the half PubMed matched need not be the half that gets recorded. Such a record is "
    "a genuine match, not a bug: it is imported as usual and the exit code stays 0 — "
    "decide whether you want it in the KB."
)
# Not a range mismatch at all, and never merged into the two above: these records
# have no year to compare. Reported anyway (#389) because asking for --year is
# asking to filter by year, and a source that lands with the year field missing is
# exactly what the operator did not ask for — silence here is indistinguishable
# from a search with no --year at all.
_YEAR_CAUSE_UNKNOWN = (
    "A record carrying no four-digit year anywhere in its <PubDate> — the element is "
    "empty, or holds free text such as a season with no year in it — is recorded with "
    "no year field at all, so the requested range can neither include nor exclude it. "
    "Such a record is a genuine match, not a bug: it is imported as usual and the exit "
    "code stays 0 — decide whether you want a source with no year in the KB."
)


def year_range_report(works, *, year: str | None = None) -> list[str]:
    """Name every result a requested ``--year`` will not hold for once recorded.

    **The mismatch this surfaces (#387).** ``--year`` composes a
    ``[Date - Publication]`` clause, and PubMed matches that range against dates the
    front matter ``year`` is not taken from. The year factlog writes is
    ``Journal/JournalIssue/PubDate``'s alone (``work_parser._pub_date``), so a record
    PubMed rightly matched can still land in the KB with a year outside the range.

    **Two causes, never conflated.** A ``PubModel="Print-Electronic"`` paper — posted
    online in one year, carried in a print issue the next — matches on its
    ``ArticleDate`` and records the later issue year (PMID 41620285 is the measured
    case: ``ArticleDate`` 2025-04-16, ``JournalIssue/PubDate/Year`` 2026). A record
    whose issue carries free text instead of a ``<Year>`` records the *first* year of
    a ``MedlineDate`` span, and a span like ``"1998 Dec-1999 Jan"`` straddles two.
    These are different facts, and ``pub_date_raw`` — non-``None`` exactly when the
    year was derived from ``MedlineDate`` — is what separates them. Each cause gets
    its own explanation, and the derived span is quoted, because an explanation
    attached to the wrong record is misinformation, not help: it would send an
    operator looking for an ``ArticleDate`` the record does not have.

    **Grouped, not one block per record.** The explanation is long and the same for
    every record sharing a cause; at ``--limit 25`` repeating it would bury the other
    things stderr is carrying that run (a retraction warning, the silent-zero guard).
    So each cause yields **one** entry: a header naming every affected record on one
    line, then the reason once, indented on a continuation line.

    **Why a warning and not a fix.** No field here is wrong: the date PubMed matched
    is what makes the record a hit, the recorded year is what a citation prints.
    Dropping the record would discard a real result, and rewriting the year would put
    a date on a citation its journal never carried. So this states the fact, explains
    it, and leaves the decision with the operator — the same surface-and-explain floor
    the MeSH guard and arXiv's ``--category`` pre-flight take. Nothing here filters or
    blocks, and the exit code is unaffected.

    **A third block: no year at all (#389).** A record whose ``PubDate`` carries no
    parseable year is recorded with no ``year`` field, and absence is *not* evidence
    of a range mismatch — so it never joins either block above, whose whole claim is
    that a year was recorded outside the range. It is still reported, in its own
    block with its own wording, because a search that was given ``--year`` and
    silently accepts year-less records is indistinguishable from one given no
    ``--year`` at all. The ``MedlineDate`` text, when there was one, is quoted here
    too: ``pub_date_raw`` exists to keep a "derived-**or-absent**" year auditable,
    and a season with no year in it is the absent half.

    Pure, duck-typed over ``.pmid``/``.year``/``.pub_date_raw`` (no import of
    ``work_parser``), and silent — ``[]`` — when no ``--year`` was given (there is no
    range to check anything against, missing year included) or every result lands
    inside the range with a year of its own.
    """
    if not year:
        return []
    try:
        start_year, end_year = parse_year_range(year)
    except PubMedSearchValidationError:
        # An unparseable --year is the caller's error to report before spending a
        # request (the CLI validates it up front). Never a second, contradictory
        # complaint from here.
        return []

    electronic: list[str] = []
    medline: list[str] = []
    unknown: list[str] = []
    for work in works:
        work_year = getattr(work, "year", None)
        pmid = getattr(work, "pmid", "?")
        # Non-None exactly when `_pub_date` read MedlineDate free text — whether or
        # not a year could be derived from it.
        raw = getattr(work, "pub_date_raw", None)
        if work_year is None:
            unknown.append(f'PMID {pmid} (MedlineDate "{raw}")' if raw else f"PMID {pmid}")
            continue
        if start_year <= work_year <= end_year:
            continue
        if raw:
            medline.append(f'PMID {pmid} ({work_year}, from MedlineDate "{raw}")')
        else:
            electronic.append(f"PMID {pmid} ({work_year})")

    # One claim per block, in the count-agnostic shape the header needs. `{pronoun}`
    # stays a literal placeholder (not an f-string field) until the block's size is
    # known, so "1 result ... against it" reads as English rather than as a template.
    outside = f"will be recorded with a year outside --year {year}"
    absent = (
        f"will be recorded with no year at all (--year {year} cannot be checked "
        "against {pronoun})"
    )
    lines: list[str] = []
    for named, claim, reason in ((electronic, outside, _YEAR_CAUSE_ELECTRONIC),
                                 (medline, outside, _YEAR_CAUSE_MEDLINE),
                                 (unknown, absent, _YEAR_CAUSE_UNKNOWN)):
        if not named:
            continue
        single = len(named) == 1
        noun = "result" if single else "results"
        claim = claim.replace("{pronoun}", "it" if single else "them")
        lines.append(
            f"⚠ {len(named)} {noun} {claim}: " + ", ".join(named) + f".\n  {reason}"
        )
    return lines
