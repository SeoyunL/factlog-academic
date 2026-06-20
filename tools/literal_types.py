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
"""
from __future__ import annotations

import decimal
import re
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

_DATE_RE = re.compile(r"^(\d{4})[.\-/](\d{1,2})(?:[.\-/](\d{1,2}))?$")
_NUMBER_RE = re.compile(r"^\d[\d,]*(?:\.\d+)?$")
_ORDINAL_KO_RE = re.compile(r"^제?(\d+)\s*(?:호|위|번|차|등|째)$")
_ORDINAL_EN_RE = re.compile(r"^(\d+)\s*(?:st|nd|rd|th)$", re.IGNORECASE)
# <number><unit>, contiguous OR a single space between them. The number part is a
# plain/comma/decimal magnitude; the unit is validated against the table by the
# caller. A leading `제` (ordinal marker) can't match because the `num` group is
# anchored to a leading digit (`^\d…`), so `제3호`-style ordinals never match (the
# first char `제` is a non-digit → no match).
_AMOUNT_RE = re.compile(r"^(?P<num>\d[\d,]*(?:\.\d+)?) ?(?P<unit>\D+)$")


def parse_date(raw: str) -> int | None:
    """A date string -> a sortable ``yyyymmdd`` int. Missing month/day default to
    ``01`` (e.g. ``2030.1`` -> ``20300101``, ``2030.01.15`` -> ``20300115``).
    Accepts ``.``/``-``/``/`` separators. Returns ``None`` if out of range."""
    m = _DATE_RE.match(raw.strip())
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(2))
    day = int(m.group(3)) if m.group(3) is not None else 1
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return year * 10000 + month * 100 + day


def parse_number(raw: str) -> float | None:
    """A plain/comma/decimal number -> ``float`` (``1,000`` -> ``1000.0``)."""
    s = raw.strip()
    if not _NUMBER_RE.match(s):
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:  # pragma: no cover - guarded by the regex
        return None


def parse_ordinal(raw: str) -> int | None:
    """An ordinal -> its int rank (``제3호``/``3위``/``3rd`` -> ``3``).

    Only ordinal-class units (호/위/번/차/등/째 and English st/nd/rd/th) qualify;
    amount units (억/만/원) and date units (년/월/일) are NOT ordinals -> ``None``.
    """
    s = raw.strip()
    m = _ORDINAL_KO_RE.match(s) or _ORDINAL_EN_RE.match(s)
    return int(m.group(1)) if m else None


def parse_amount(raw: str, units: dict[str, int]) -> int | None:
    """A ``<number><unit>`` amount -> its value in the **integer base unit**, or
    ``None`` if it does not parse / the unit is unknown. Never raises.

    Conversion is **exact**: the numeric part is parsed with ``decimal.Decimal``
    (commas stripped) and multiplied by the unit's **int** multiplier, so e.g.
    ``2.675억`` -> ``267500000`` exactly (a float ``2.675 * 1e8`` would give
    ``267499999``). An integral product is returned as-is; a sub-base-unit
    fraction is rounded to the nearest int (ROUND_HALF_UP) and documented as such.

    Scope (first cut): Korean monetary units only (the table's keys). A leading
    ``제`` (ordinal marker), a ``%``, or any unit not in *units* -> ``None``.
    ``3 GB`` / ASCII-space units are out of scope.
    """
    m = _AMOUNT_RE.match(raw.strip())
    if not m:
        return None
    unit = m.group("unit").strip()
    multiplier = units.get(unit)
    if multiplier is None:
        return None
    try:
        num = Decimal(m.group("num").replace(",", ""))
    except decimal.InvalidOperation:  # pragma: no cover - guarded by the regex
        return None
    product = num * multiplier
    if product == product.to_integral_value():
        return int(product)
    return int(product.to_integral_value(rounding=decimal.ROUND_HALF_UP))


_PARSERS = {"date": parse_date, "number": parse_number, "ordinal": parse_ordinal}


def normalize(type_tag: str, raw: str, units: dict[str, int] | None = None) -> int | float | None:
    """Parse *raw* under *type_tag* into its canonical scalar, or ``None`` if it
    does not parse (or the tag is unknown). Total: never raises.

    ``amount`` is special-cased: it uses *units* (or ``DEFAULT_AMOUNT_UNITS`` when
    a declaration carries no inline table). date/number/ordinal ignore *units*."""
    if type_tag == "amount":
        return parse_amount(raw, units or DEFAULT_AMOUNT_UNITS)
    parser = _PARSERS.get(type_tag)
    return parser(raw) if parser is not None else None
