# SPDX-License-Identifier: Apache-2.0
"""Deterministic normalizers for typed literal values.

A relation declared in ``policy/typed-relations.md`` carries a literal object
(a date, number, or ordinal). To let the deterministic engine order/compare such
values, each raw object string is parsed here into a **canonical sortable
scalar**. This module is **pure**: no engine, no I/O, no ``pyrewire`` import.

Contract for every parser:
- returns the canonical scalar, or ``None`` if the string does not parse as that
  type (the caller emits a warning and loads the fact untyped);
- never raises on bad input, and never guesses;
- reads **ASCII digits only** — a full-width/other Unicode digit does not parse
  (see the digits note below).

``amount`` (e.g. ``100억``, ``1,000원``) carries a **unit**, so it normalizes to a
declared **integer base unit** via a reviewable unit table (Korean monetary units
only in this first cut: ``원/천/만/억/조``). Amounts compare in integer base units;
a sub-base-unit fraction is rounded to the nearest int (ROUND_HALF_UP). The engine
has no float column, so the base-unit value MUST be an exact integer — see
``parse_amount``.

**Digits are ASCII-only (#388).** Every numeric group below is spelled ``[0-9]``,
never ``\\d``: Python's ``\\d`` matches the whole Unicode ``Nd`` category, so a
full-width ``date(２０２０,１)`` used to parse to the same scalar as ``date(2020,1)``
while the *stored object string* stayed different. That split one value in two
depending on context — a typed relation grouped the pair as equal (``_group_key``
keys on the scalar), an object-match query for the ASCII spelling missed the
full-width row, and without a typed declaration the two became separate entities.
The codebase normalizes to **NFC**, which preserves full-width digits (only NFKC
folds them), so nothing upstream collapses them either.

So a non-ASCII digit is **rejected**, not silently folded. The reason is the one
#388 gives, and it is a policy choice, not a claim of wider coverage: this
repository prefers an **explicit refusal over a silent fold** (``--category``
pre-validation, the silent-zero guard), and a full-width digit in a numeric field
is far more likely an **input accident** — a CJK source, a PDF conversion, an IME
left in full-width mode — than a value a human meant to write. Refusing makes the
accident *visible* at the point a human can still fix it.

Rejection is deliberately **narrow**, and does NOT make the value single-state:

- It only reaches values that go **through a parser**, i.e. objects of relations
  declared in ``policy/typed-relations.md``. A value under an **undeclared**
  relation never touches this module, so the full-width and ASCII spellings stay
  **two separate entities** — measured, unchanged from before this module was
  narrowed. That is a **residual symptom of #388 that this module does not fix**
  and cannot: nothing here is on that code path.
- The value the KB **stores** is untouched either way. Refusing to parse does not
  rewrite ``accepted.dl``; the full-width string is still what an object-match
  query has to match.

The place that *could* collapse all three symptoms at once is the **merge stage**,
where ``tools/merge_candidates.py``'s ``normalize_rows`` already rewrites the
stored object string **before** computing the dedup key (that is how
``canonical_amount`` folds ``amount(7,억)`` and ``amount(7,"억")`` into one row).
A digit fold placed there would change the stored string, so the dedup keys would
merge and the entity split would go with them. That is a **data rewrite**, a
different decision from this one, and it is not made here.

What rejection does buy is a **report**, and it now has **two visible exits for all
four types** (measured, post-#394): the value degrades to the ordinary "does not
parse" path and surfaces both at ``common.typed_projection_outcome``'s
``typed-relations`` projection warning and in ``entity_audit``'s ``malformed typed
literal`` section::

    _is_malformed_compound_term(v), no typed declaration
      date(２０２０,１)      -> True
      number(１２３)        -> True
      ordinal(３)          -> True
      amount(１００,"억")   -> True

``amount`` reached only the first exit until #394. ``_is_malformed_compound_term``
returned early for any compound ``amount`` under a relation with no ``amount``
spec, so a full-width amount was refused by this module yet invisible in the audit.
#394 narrowed that exemption to **unit resolution alone** and judges the shape
first, so a value whose ``num`` group fails is malformed with or without a
declaration — which is exactly the full-width case. Nothing in this module changed
for that to happen; the coupling is one-way, and ``entity_audit`` pins it.

Because a full-width digit is hard to see in a warning, the report sites append
``non_ascii_digits`` to name the actual offending characters. That helper now
lives in :mod:`factlog.text_norm` and is re-exported here for its callers:
**the vocabulary is there, the policy is here** (#410). What ``Nd`` *is*, and how
to fold it, is one Unicode fact with no room to diverge; whether a value carrying
one is refused (this module) or folded (the Zotero import boundary, the CSL export
boundary) is the decision that legitimately differs per call site, and it stays at
the call site.

**One known producer stopped feeding this, for new imports only.** ``zotero``'s
``extract_year`` passed a full-width year through verbatim, so an ordinary Zotero
import could write a value this module refuses; #398 normalizes it (and
``extract_pmid``) to ASCII at the import boundary instead. The split is intentional:
a hand-written literal is refused so the author sees it, while an imported one is
normalized, because there the odd digit comes from an external library the user
cannot edit from inside factlog. ``csl``'s ``_YEAR_RE`` folds one silently via
``int()`` and is the milder sibling (#399).

**Sources imported before #398 are not repaired and do not self-heal.** They keep
their full-width ``year:`` and go on producing these warnings, and re-running the
import does not fix them — the record is recognised by ``zotero_key`` and reported
``skipped``, so the stale value is never rewritten. Recovery is deliberate: edit the
``year:`` by hand, or force a re-import of that record. So if ``does not parse``
warnings appear on records nobody hand-edited, **the first thing to check is whether
the source predates #398**, not whether some writer skipped the boundary.
"""
from __future__ import annotations

import datetime
import decimal
import re
from decimal import Decimal

# Re-exported, not redefined: ``non_ascii_digits`` describes the ``Nd`` vocabulary,
# which the fold sites need too, so it lives in ``text_norm`` (#410). It stays
# importable as ``literal_types.non_ascii_digits`` because that is where its
# callers (``common``, ``entity_audit``) report from — beside this module's
# refusal, which is what they are explaining. The redundant ``as`` alias is the
# PEP 484 explicit-re-export spelling and is load-bearing, not a typo: without it
# this is an unused import (ruff F401, measured). It is preferred over ``__all__``
# because it re-exports this one name without also declaring a public surface for
# the other eleven, which this module never had.
from factlog.text_norm import non_ascii_digits as non_ascii_digits

# The literal types this module can normalize. The declaration parser validates
# a type tag against this set; the engine projection maps each to a column type.
TYPES: frozenset[str] = frozenset({"date", "number", "ordinal", "amount"})

# Built-in default unit table for `amount`, used when no inline table is declared.
# Multipliers are Python **ints** (never floats like 1e8) so that
# ``Decimal(num) * unit`` is exact — an int64 column has no float to round into.
# Korean monetary units only (first cut): 원/천/만/억/조.
DEFAULT_AMOUNT_UNITS: dict[str, int] = {
    "원": 1,
    "천": 10**3,
    "만": 10**4,
    "억": 10**8,
    "조": 10**12,
}

_DATE_RE = re.compile(r"^([0-9]{4})[.\-/]([0-9]{1,2})(?:[.\-/]([0-9]{1,2}))?$")
# The compound form is year-precision friendly: month AND day are optional, so
# ``date(2020)`` parses (a bibliographic record normally knows only the year —
# see #385). This mirrors the prose path, where a missing day already defaults
# to ``01`` (``2030.1`` -> ``20300101``); a missing month now defaults the same
# way. The bare prose ``2020`` stays UNPARSEABLE on purpose: without the ``date(…)``
# wrapper it is indistinguishable from a plain number, so only the explicitly
# typed compound term opts into year precision.
_DATE_COMPOUND_RE = re.compile(
    r"^date\(\s*([0-9]{4})(?:\s*,\s*([0-9]{1,2})(?:\s*,\s*([0-9]{1,2}))?)?\s*\)$",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"^-?[0-9][0-9,]*(?:\.[0-9]+)?$")
_NUMBER_COMPOUND_RE = re.compile(
    r"^number\(\s*\"?(-?[0-9][0-9,]*(?:\.[0-9]+)?)\"?\s*\)$",
    re.IGNORECASE,
)
_ORDINAL_KO_RE = re.compile(r"^제?([0-9]+)\s*(?:호|위|번|차|등|째)$")
_ORDINAL_EN_RE = re.compile(r"^([0-9]+)\s*(?:st|nd|rd|th)$", re.IGNORECASE)
_ORDINAL_COMPOUND_RE = re.compile(r"^ordinal\(\s*([0-9]+)\s*\)$", re.IGNORECASE)
# <number><unit>, contiguous OR a single space between them. The number part is a
# plain/comma/decimal magnitude with an OPTIONAL leading sign (a loss/credit may be
# negative); the unit is validated against the table by the caller. A leading `제`
# (ordinal marker) can't match because the `num` group is anchored to an optional
# sign + leading digit (`^-?[0-9]…`), so `제3호`-style ordinals never match (the
# first char `제` is neither `-` nor a digit → no match).
# The unit tail stays `\D+` (NOT `[^0-9]+`) on purpose: `\D` excludes the whole
# Unicode `Nd` category, so a full-width digit can never be absorbed into a unit
# either. Both spellings end at `None` anyway — the unit is looked up in a table
# whose keys hold no digits — but `\D+` fails at the match, one step earlier.
_AMOUNT_RE = re.compile(r"^(?P<num>-?[0-9][0-9,]*(?:\.[0-9]+)?) ?(?P<unit>\D+)$")
# Compound amount: the unit may be quoted ("...", allowing spaces and commas) or
# bare (no comma/paren/quote). The number is optionally quoted. Canonicalisation
# always emits the quoted unit form (see ``canonical_amount``).
_AMOUNT_COMPOUND_RE = re.compile(
    r'^amount\(\s*"?(?P<num>-?[0-9][0-9,]*(?:\.[0-9]+)?)"?\s*,\s*'
    r'(?:"(?P<qunit>[^"]*)"|(?P<unit>[^,)"]+))\s*\)$',
    re.IGNORECASE,
)


def _amount_unit(match: re.Match[str]) -> str:
    """The unit from an ``_AMOUNT_COMPOUND_RE`` match (quoted group wins), stripped
    of surrounding whitespace. The prose ``_AMOUNT_RE`` has only a bare ``unit``
    group, so this helper is compound-only."""
    qunit = match.groupdict().get("qunit")
    unit = qunit if qunit is not None else match.group("unit")
    return unit.strip()


def parse_date(raw: str) -> int | None:
    """A date string -> a sortable ``yyyymmdd`` int. Missing month/day default to
    ``01`` (e.g. ``2030.1`` -> ``20300101``, ``2030.01.15`` -> ``20300115``,
    ``date(2030)`` -> ``20300101``). Accepts ``.``/``-``/``/`` separators and the
    compound form ``date(year[, month[, day]])``. A bare ``2030`` (no ``date(…)``
    wrapper, no separator) does NOT parse — it is not distinguishable from a
    number. Returns ``None`` if out of range."""
    text = raw.strip()
    m = _DATE_COMPOUND_RE.match(text) or _DATE_RE.match(text)
    if not m:
        return None
    year = int(m.group(1))
    # Year precision (``date(2030)``) reaches here only via the compound path;
    # ``_DATE_RE`` always captures a month. Default it to ``01``, the same way a
    # missing day defaults, so year/month/day precision all sort consistently.
    month = int(m.group(2)) if m.group(2) is not None else 1
    day = int(m.group(3)) if m.group(3) is not None else 1
    # A month-precision date (no day in the source) defaults day to 01, which is
    # always a valid day, so this preserves ``2030.1`` -> ``20300101``. When a day
    # IS given, ``datetime.date`` rejects calendar-impossible dates (2/30, 4/31,
    # a non-leap 2/29): the ``day <= 31`` range check alone is not enough.
    try:
        datetime.date(year, month, day)
    except ValueError:
        return None
    return year * 10000 + month * 100 + day


def parse_number(raw: str) -> float | None:
    """A plain/comma/decimal number -> ``float`` (``1,000`` -> ``1000.0``).
    Also accepts ``number(value)``."""
    s = raw.strip()
    compound = _NUMBER_COMPOUND_RE.match(s)
    if compound:
        s = compound.group(1)
    if not _NUMBER_RE.match(s):
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:  # pragma: no cover - guarded by the regex
        return None


NUMBER_SCALE = 1000  # fixed-point factor for `number` -> int64 (3 decimal places)


def parse_number_scaled(raw: str) -> int | None:
    """A number -> exact int scaled by NUMBER_SCALE (2.5 -> 2500), or None.
    _NUMBER_RE validates; Decimal scales exactly (a float path mis-rounds:
    1.0005 -> 1000 vs 1001). Also accepts ``number(value)``. Sub-factor
    fraction rounds ROUND_HALF_UP."""
    s = raw.strip()
    compound = _NUMBER_COMPOUND_RE.match(s)
    if compound:
        s = compound.group(1)
    if not _NUMBER_RE.match(s):
        return None
    try:
        product = Decimal(s.replace(",", "")) * NUMBER_SCALE
    except decimal.InvalidOperation:  # pragma: no cover - guarded by the regex
        return None
    if product == product.to_integral_value():
        return int(product)
    return int(product.to_integral_value(rounding=decimal.ROUND_HALF_UP))


def parse_ordinal(raw: str) -> int | None:
    """An ordinal -> its int rank (``제3호``/``3위``/``3rd`` -> ``3``).

    Only ordinal-class units (호/위/번/차/등/째 and English st/nd/rd/th) qualify;
    amount units (억/만/원) and date units (년/월/일) are NOT ordinals -> ``None``.
    Also accepts ``ordinal(n)``.
    """
    s = raw.strip()
    m = _ORDINAL_COMPOUND_RE.match(s) or _ORDINAL_KO_RE.match(s) or _ORDINAL_EN_RE.match(s)
    return int(m.group(1)) if m else None


# The engine projects an amount into a signed 64-bit integer column, so any value
# outside this range would overflow silently. ``parse_amount`` returns ``None``
# (untyped) rather than emit an out-of-range int — same "does not parse -> untyped"
# contract as the other parsers.
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

# A trailing monetary marker fused onto a scale unit (``억원`` = ``억`` + ``원``).
# The prose ``_AMOUNT_RE`` captures the whole non-digit tail as one unit, so a
# fused form like ``억원`` never matches the table directly. On a first-pass miss
# we strip ONE trailing ``원`` and retry, which recovers ``억원``/``조원``/``백만원``
# only when the stripped stem is itself a known unit. The base ``원`` unit is
# matched on the first pass (``1,000원`` -> unit ``원``), so this retry never
# shadows it.
_CURRENCY_MARKER = "원"


def parse_amount(raw: str, units: dict[str, int]) -> int | None:
    """A ``<number><unit>`` amount -> its value in the **integer base unit**, or
    ``None`` if it does not parse / the unit is unknown / it overflows int64.
    Never raises.

    Conversion is **exact**: the numeric part is parsed with ``decimal.Decimal``
    (commas stripped) and multiplied by the unit's **int** multiplier, so e.g.
    ``2.675억`` -> ``267500000`` exactly (a float ``2.675 * 1e8`` would give
    ``267499999``). An integral product is returned as-is; a sub-base-unit
    fraction is rounded to the nearest int (ROUND_HALF_UP) and documented as such.

    Prose fallback: a fused currency suffix (``100억원``) is recovered by stripping
    one trailing currency marker (``원``) and re-looking-up the stem (``억``) in
    *units*. This applies to the caller-supplied table too, and only succeeds when
    the stripped stem is a known unit — an unknown stem (``백만원``) stays ``None``.

    Scope (first cut): Korean monetary units only (the table's keys). A leading
    ``제`` (ordinal marker), a ``%``, or any unit not in *units* -> ``None``.
    ``3 GB`` / ASCII-space units are out of scope.
    """
    text = raw.strip()
    m = _AMOUNT_COMPOUND_RE.match(text)
    if m:
        unit = _amount_unit(m)
    else:
        m = _AMOUNT_RE.match(text)
        if not m:
            return None
        unit = m.group("unit").strip()
    multiplier = units.get(unit)
    if multiplier is None:
        # Prose fallback: a fused currency suffix (``억원``). Strip one trailing
        # marker and retry against *units*. Recovery succeeds ONLY when the
        # stripped stem is itself a known unit, so an unknown stem (``백만``,
        # foreign currency) stays ``None``. A redundant marker (``원원`` -> ``원``)
        # or a fused compound unit also resolves — harmless, since the stem must
        # still be a real table unit (never a guess).
        if unit.endswith(_CURRENCY_MARKER) and len(unit) > len(_CURRENCY_MARKER):
            multiplier = units.get(unit[: -len(_CURRENCY_MARKER)])
        if multiplier is None:
            return None
    try:
        num = Decimal(m.group("num").replace(",", ""))
    except decimal.InvalidOperation:  # pragma: no cover - guarded by the regex
        return None
    product = num * multiplier
    if product == product.to_integral_value():
        value = int(product)
    else:
        value = int(product.to_integral_value(rounding=decimal.ROUND_HALF_UP))
    if value < _INT64_MIN or value > _INT64_MAX:
        return None
    return value


def canonical_amount(raw: str) -> str | None:
    """Rewrite an amount compound term to the always-quoted canonical form
    ``amount(N,"unit")`` (commas stripped from ``N``, the unit always quoted), or
    ``None`` if *raw* is not an amount compound term.

    Quoting the unit unconditionally makes it unambiguous regardless of its
    contents — a unit may carry spaces (``"kilometer per hour"``) or commas
    (``"달러,센트"``) without colliding with the compound's own ``,``/``)`` syntax.
    The flat ``relation/3`` fact stores this object string verbatim; the engine
    ``.dl`` text parser supports ``\\"`` escapes (wirelog#924), so a quoted unit
    reaches ``facts/accepted.dl`` as ``"amount(7,\\"억\\")"`` and loads cleanly.
    Both the bare (``amount(7,억)``) and quoted (``amount(7,"억")``) input forms
    canonicalise to the same quoted output, so a re-merge is idempotent and the
    dedup key collapses the two."""
    m = _AMOUNT_COMPOUND_RE.match(raw.strip())
    if not m:
        return None
    return f'amount({m.group("num").replace(",", "")},"{_amount_unit(m)}")'


# `number` dispatches to parse_number_scaled (exact int64 fixed-point, ×1000):
# the engine .dl text parser has no float column, so a number projects as a
# sortable scaled int (see #125). parse_number (float) stays exported as the
# public parser / validity gate (AC3).
_PARSERS = {"date": parse_date, "number": parse_number_scaled, "ordinal": parse_ordinal}


def normalize(type_tag: str, raw: str, units: dict[str, int] | None = None) -> int | None:
    """Parse *raw* under *type_tag* into its canonical scalar, or ``None`` if it
    does not parse (or the tag is unknown). Total: never raises.

    ``amount`` is special-cased: it uses *units* (or ``DEFAULT_AMOUNT_UNITS`` when
    a declaration carries no inline table). date/number/ordinal ignore *units*.

    Return type is ``int | None``: every projected type (date/ordinal/amount and
    ``number`` via ``parse_number_scaled``'s ×1000 fixed-point) yields an **int**,
    so a caller keying on the scalar never needs to handle a float. The public
    float parser ``parse_number`` stays a separate ``float`` API (validity gate)."""
    if type_tag == "amount":
        return parse_amount(raw, units or DEFAULT_AMOUNT_UNITS)
    parser = _PARSERS.get(type_tag)
    return parser(raw) if parser is not None else None


def humanize(value: str) -> str:
    """A compound-term object string -> a human-friendly display form, or *value*
    unchanged if it is not a recognized compound term. Total: never raises.

    DISPLAY-ONLY. The stored/canonical string stays the source of truth — dedup
    (``merge_candidates``), engine input (``accepted.dl``) and query matching all
    key on the stored form — so a caller must render this *beside* the stored
    object, never in place of it (else a humanized value copied into a query
    would miss). Recognizes the unambiguous compound terms:

      ``date(2030)`` -> ``2030``        ``date(2030,1)`` -> ``2030-01``
      ``date(2030,1,15)`` -> ``2030-01-15``
      ``amount(7,"억")`` -> ``7억``       ``number(2.5)`` -> ``2.5``

    ``ordinal(N)`` is intentionally NOT humanized: the source unit (호/위/번) is
    lost at normalization, so a bare rank would be ambiguous. Any non-compound or
    unrecognized string is returned verbatim, so a KB that emits no compound
    objects is byte-identical."""
    text = value.strip()
    m = _DATE_COMPOUND_RE.match(text)
    if m:
        year = int(m.group(1))
        month = int(m.group(2)) if m.group(2) is not None else None
        day = int(m.group(3)) if m.group(3) is not None else None
        # Reject calendar-impossible dates so we never fabricate a misleading ISO
        # display (e.g. ``date(2024,2,30)`` stays verbatim, not ``2024-02-30``).
        # A month-precision term (no day) only needs a valid month; a
        # year-precision term (``date(2030)``) needs neither. Probe the missing
        # parts with 01, always valid, to reuse the same calendar check.
        try:
            datetime.date(year, month if month is not None else 1, day if day is not None else 1)
        except ValueError:
            return value
        # Render only the precision the term actually carries: padding a
        # year-precision value to ``2030-01`` would invent a month it never had.
        iso = f"{year:04d}"
        if month is not None:
            iso += f"-{month:02d}"
        if day is not None:
            iso += f"-{day:02d}"
        return iso
    m = _AMOUNT_COMPOUND_RE.match(text)
    if m:
        return f"{m.group('num')}{_amount_unit(m)}"
    m = _NUMBER_COMPOUND_RE.match(text)
    if m:
        return m.group(1)
    return value
