# SPDX-License-Identifier: Apache-2.0
"""Deterministic normalizers for typed literal values.

A relation declared in ``policy/typed-relations.md`` carries a literal object
(a date, number, or ordinal). To let the deterministic engine order/compare such
values, each raw object string is parsed here into a **canonical sortable
scalar**. This module is **pure**: no engine, no I/O, no ``pyrewire`` import.

Contract for every parser:
- returns the canonical scalar, or ``None`` if the string does not parse as that
  type (the caller emits a warning and loads the fact untyped);
- never raises on bad input, and never guesses.

``amount`` (e.g. ``100억``, ``1,000원``) carries a **unit**, so it normalizes to a
declared **integer base unit** via a reviewable unit table (Korean monetary units
only in this first cut: ``원/천/만/억/조``). Amounts compare in integer base units;
a sub-base-unit fraction is rounded to the nearest int (ROUND_HALF_UP). The engine
has no float column, so the base-unit value MUST be an exact integer — see
``parse_amount``.

**Digits are ASCII-only.** Python's ``\\d`` matches the whole Unicode ``Nd``
category, so a full-width ``１００억`` used to parse to the *same scalar* as
``100억`` while ``relation/3`` kept the differing object string — one value with
three different answers to "is this the same?" depending on the code path. The
policy is **reject, not fold**: every numeric group below is spelled ``[0-9]``,
so a full-width value takes the ordinary "does not parse -> untyped" path and
surfaces as a typed-projection warning instead of merging silently. Rewriting the
stored string would have been the silent fold this repo consistently refuses.

This is not only about full-width U+FF10–FF19. ``\\d`` is exactly the Unicode
``Nd`` category, so Arabic-Indic ``١٠٠``, Devanagari ``१२३`` and Thai ``๑๒๓`` were
accepted the same way — and ``int``/``Decimal`` still accept all of them
(``int('١٠٠') == 100``), which is why the regex has to be the gate.

Five call sites outside this module change behaviour as a consequence. All five
are intended, and none of them rewrites data already stored:

- ``tools/merge_candidates.py`` dedup key — ``canonical_amount`` returns ``None``
  for a full-width term, so ``amount(１００,억)`` and ``amount(１００,"억")`` stay
  two rows instead of collapsing into one. Collapsing them is exactly the fold
  this policy rejects. A newly merged full-width row also keeps its **source
  string verbatim** instead of being rewritten to the quoted canonical form;
  rows already in the KB are untouched either way.
- ``tools/check_conflicts.py`` ``_group_key`` — a full-width object no longer
  normalizes to a scalar, so it keys as ``("raw", obj)`` while its ASCII twin
  keys as ``("scalar", …)``. Under a single-valued relation those are **two
  values**, so a KB that used to pass can now report a conflict and the command
  exits **1**. This is the one consequence that flips a green gate red — see the
  migration note in ``docs/reference/typed-relations.md``.
- ``common._canonical_value`` — a fact already stored as ``amount(１００,"억")``
  in an existing KB is no longer canonicalised, so a query written as
  ``amount(１００,억)`` misses it. Intended: under this policy ``amount(１００,억)``
  is not a valid amount term at all, so there is no canonical form to map it to.
  The fix is to correct the source to ASCII and re-collect, not to fold here.
- ``tools/ask_router.py`` answer annotation — ``humanize`` returns a full-width
  compound term verbatim rather than rendering it as ``１００억``, so the display
  suffix (``… (= 100억)``) is simply omitted for such a row.
- ``factlog/cli.py`` ``status`` conflict count — this path never folded typed
  scalars; it counts distinct **raw object strings**, so it already reported the
  ASCII/full-width pair as 2 values while ``check_conflicts`` reported 0. The two
  commands contradicted each other; narrowing here converges ``check_conflicts``
  onto the count ``status`` was already giving. It is the strongest code-level
  argument for rejecting rather than folding.

A rejected value surfaces on one automatic warning, one gate, and one manual
tool — and falls through one hole:

- **automatic** — under a relation declared **typed**,
  ``common._project_typed_relations`` warns on stderr and loads the fact untyped.
  The warning names the offending codepoints (``mark_non_ascii_digits``): its
  ``repr``-rendered value alone cannot tell ``1２3억`` from ``123억``, and the
  remedy it points at needs the reader to know which character is wrong.
- **gate** — under a relation declared **single-valued**,
  ``tools/check_conflicts.py`` sees the ASCII/full-width pair as two values and
  exits 1, naming the offending value (``non_ascii_digit_note``).
- **manual** — ``tools/entity_audit.py`` reports a value under a relation not
  declared an attribute as a *literal suspect*. Nothing in the pipeline runs it
  (``skills/factlog/SKILL.md`` documents it as a manual command) and its message
  says nothing about digit width. Its ``_LITERAL_RE`` is deliberately looser than
  this module and still matches full-width digits, so narrowing that detector
  would close this path (a pinning test guards the divergence). It only sees bare
  prose forms: ``１００억``/``２０２６``/``제３호``/``３위`` match, while ``３rd``
  and every compound term (``date(２０２０,１)``, ``amount(１００,"억")``,
  ``number(１２３)``, ``ordinal(３)``) do not — the same blind spots it has for the
  ASCII spellings, so this is not a regression.
- **hole** — a relation declared as an **attribute but not typed** has no spec, so
  the projection loop skips it without warning and its objects land in
  ``entity_audit``'s ``declared_literals``: an unflagged sorted list in which
  ``100억`` and ``１００억`` sort far apart.
"""
from __future__ import annotations

import datetime
import decimal
import re
import unicodedata
from decimal import Decimal

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


def has_non_ascii_digits(value: str) -> bool:
    """True when *value* carries a Unicode decimal digit (category ``Nd``) outside
    ASCII ``0-9`` — exactly the characters the ``[0-9]`` narrowing rejects and the
    old ``\\d`` accepted (``\\d`` and ``Nd`` coincide). Diagnostic only: callers use
    it to EXPLAIN a rejection, never to decide one — the regexes stay the single
    gate. ``str.isdigit`` is deliberately not used: it also matches ``No`` (``²``),
    which ``\\d`` never did. Total; never raises."""
    return any(unicodedata.category(ch) == "Nd" and not ch.isascii() for ch in value)


def _escape_codepoint(ch: str) -> str:
    """``ch`` as the Python escape that decodes back to it. ``\\uXXXX`` holds four
    hex digits, so a codepoint above the BMP needs the eight-digit ``\\UXXXXXXXX``
    form — ``\\u1d7cf`` is not an overlong ``𝟏`` but ``\\u1d7c`` followed by ``f``,
    i.e. a different character. This is not a corner case: 390 of the 760 ``Nd``
    codepoints are astral (mathematical bold/sans/mono digits U+1D7CE–U+1D7FF,
    Osmanya, …), and mathematical bold digits arrive by copy-pasting styled text."""
    return f"\\U{ord(ch):08x}" if ord(ch) > 0xFFFF else f"\\u{ord(ch):04x}"


def mark_non_ascii_digits(value: str) -> str:
    """*value* with every non-ASCII decimal digit replaced by its ``\\uXXXX`` /
    ``\\UXXXXXXXX`` escape, so a message can name the offending characters.
    ``repr`` cannot: ``repr('１００억')`` is ``'１００억'``, indistinguishable from
    ``'100억'`` in most fonts. Everything else is left verbatim so the value stays
    readable."""
    return "".join(
        _escape_codepoint(ch) if unicodedata.category(ch) == "Nd" and not ch.isascii() else ch
        for ch in value
    )


# Every numeric group below is written ``[0-9]``, never ``\d`` — see the ASCII-only
# paragraph in the module docstring. Do NOT reach for ``re.ASCII`` to get the same
# effect: the flag also narrows ``\s``, and U+3000 (ideographic space) inside
# ``date(2020,<U+3000>1)`` would silently stop parsing. The ``\D+`` unit group in
# ``_AMOUNT_RE`` needs no change: ``\D`` is the complement of ``Nd``, so it already
# excludes full-width digits (``1２3억`` therefore does not match either half).
# The asymmetry is deliberate, not an arbitrary line: whitespace is not part of the
# value (an ideographic space in ``date(2020,<U+3000>1)`` is layout, and dropping it
# changes nothing about what the value means), whereas a digit IS the value —
# accepting ``１`` as ``1`` decides that two different stored strings mean the same
# thing, which is exactly the fold this policy refuses. So narrow digits explicitly
# and leave ``\s``/``\D`` alone.
_DATE_RE = re.compile(r"^([0-9]{4})[.\-/]([0-9]{1,2})(?:[.\-/]([0-9]{1,2}))?$")
# The compound form is year-precision friendly: month AND day are optional, so
# ``date(2020)`` parses (a bibliographic record normally knows only the year).
# This mirrors the prose path, where a missing day already defaults to ``01``
# (``2030.1`` -> ``20300101``); a missing month now defaults the same way. The
# bare prose ``2020`` stays UNPARSEABLE on purpose: without the ``date(…)`` wrapper
# it is indistinguishable from a plain number, so only the explicitly typed
# compound term opts into year precision.
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
_AMOUNT_RE = re.compile(r"^(?P<num>-?[0-9][0-9,]*(?:\.[0-9]+)?) ?(?P<unit>\D+)$")
# Compound amount: the unit may be quoted ("...", allowing spaces and commas) or
# bare (no comma/paren/quote). The number is optionally quoted. Canonicalisation
# always emits the quoted unit form (see ``canonical_amount``).
# The unit group is deliberately OUTSIDE the digit policy: a unit is an opaque
# label validated against the unit table, not a number. ``amount(100,１００)`` still
# canonicalises to ``amount(100,"１００")`` and humanizes to ``100１００`` —
# byte-identical to before this narrowing, because ``１００`` is in no unit table and
# so never normalizes to a scalar. Only the ``num`` groups are ``[0-9]``.
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

    The lookup key is **NFC-folded** before it meets *units* (the same NFC as
    ``common.fold_relation_name``; this module stays pure, so it normalizes
    directly rather than importing ``common``). An object written in NFD — the
    macOS default for Hangul — is a different byte string from the composed unit
    name it means, so an unfolded lookup misses and the amount silently loads
    untyped; two spellings of one value then split into a contradiction. Folding
    here also protects a caller-supplied table, and lets the ``억원`` currency
    fallback below see a composed ``원`` to strip. NFC only, never NFKC:
    fullwidth ``ＡＢＣ`` and ``ABC`` stay different units. An already-composed or
    ASCII unit folds to itself, so composed KBs are byte-identical.

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
    unit = unicodedata.normalize("NFC", unit)
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
    dedup key collapses the two.

    A full-width number (``amount(１００,억)``) is not an amount compound term, so
    it returns ``None`` and the two spellings stay separate rows in the
    ``tools/merge_candidates.py`` dedup key — the intended consequence of the
    ASCII-only digit policy (see the module docstring)."""
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
    objects is byte-identical. A full-width term (``amount(１００,"억")``) is not
    recognized under the ASCII-only digit policy, so it too comes back verbatim
    rather than rendered."""
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
