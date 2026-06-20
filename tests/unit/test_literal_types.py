# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the deterministic literal normalizers (#117)."""
from __future__ import annotations

import literal_types as lt
import pytest


class TestParseDate:
    @pytest.mark.parametrize("raw,expected", [
        ("2030.1", 20300101),
        ("2030-01", 20300101),
        ("2030.01.15", 20300115),
        ("2024/07/01", 20240701),
        ("2030.12.31", 20301231),
    ])
    def test_accepts(self, raw, expected):
        assert lt.parse_date(raw) == expected

    @pytest.mark.parametrize("raw", ["2026", "not a date", "2030.13.01", "2030.1.32", ""])
    def test_rejects(self, raw):
        assert lt.parse_date(raw) is None


class TestParseNumber:
    @pytest.mark.parametrize("raw,expected", [
        ("2026", 2026.0),
        ("3.14", 3.14),
        ("1,000", 1000.0),
        ("1,000,000", 1000000.0),
    ])
    def test_accepts(self, raw, expected):
        assert lt.parse_number(raw) == expected

    @pytest.mark.parametrize("raw", ["abc", "", "3호", "1.2.3"])
    def test_rejects(self, raw):
        assert lt.parse_number(raw) is None


class TestParseNumberScaled:
    @pytest.mark.parametrize("raw,expected", [
        ("2.5", 2500),
        ("2026", 2026000),
        ("1,000", 1000000),
        ("0", 0),
        # IEEE-754 divergence proofs: a float path mis-rounds these; Decimal is
        # exact. 1.0005 * 1000 == 1000.4999999... as a float -> 1000, but the
        # exact scaled value is 1000.5 -> ROUND_HALF_UP -> 1001.
        ("1.0005", 1001),
        ("0.0005", 1),
    ])
    def test_accepts(self, raw, expected):
        assert lt.parse_number_scaled(raw) == expected

    @pytest.mark.parametrize("raw", ["abc", "", "3호", "1.2.3"])
    def test_rejects(self, raw):
        assert lt.parse_number_scaled(raw) is None

    def test_returns_int_never_float(self):
        assert type(lt.parse_number_scaled("2.5")) is int


class TestParseOrdinal:
    @pytest.mark.parametrize("raw,expected", [
        ("제3호", 3), ("3위", 3), ("3rd", 3), ("1st", 1), ("12th", 12), ("제5번", 5),
    ])
    def test_accepts(self, raw, expected):
        assert lt.parse_ordinal(raw) == expected

    @pytest.mark.parametrize("raw", ["3", "100억", "2026년", "", "third"])
    def test_rejects(self, raw):
        # bare numbers, amount/date units, and words are not ordinals
        assert lt.parse_ordinal(raw) is None


class TestParseAmount:
    @pytest.mark.parametrize("raw,expected", [
        ("100억", 10000000000),
        ("1,000원", 1000),
        ("50억", 5000000000),
        ("1조", 1000000000000),
        ("100 억", 10000000000),  # single space allowed
    ])
    def test_accepts(self, raw, expected):
        assert lt.parse_amount(raw, lt.DEFAULT_AMOUNT_UNITS) == expected

    def test_decimal_is_exact(self):
        # int(2.675 * 1e8) == 267499999 (IEEE-754 error); Decimal is exact.
        assert lt.parse_amount("2.675억", lt.DEFAULT_AMOUNT_UNITS) == 267500000

    @pytest.mark.parametrize("raw", ["3GB", "제3호", "50%", "2026년", "3 GB", "", "억"])
    def test_rejects(self, raw):
        # unknown/ASCII units, ordinal marker, percent, date unit -> None
        assert lt.parse_amount(raw, lt.DEFAULT_AMOUNT_UNITS) is None

    def test_returns_int_never_float(self):
        result = lt.parse_amount("2.675억", lt.DEFAULT_AMOUNT_UNITS)
        assert type(result) is int


class TestNormalizeDispatcher:
    def test_dispatches_by_tag(self):
        assert lt.normalize("date", "2030.1") == 20300101
        # number now projects as a scaled int64 (×1000), not a float (#125).
        assert lt.normalize("number", "3.14") == 3140
        assert lt.normalize("ordinal", "3위") == 3

    def test_amount_uses_default_table(self):
        # amount is no longer an unknown tag: with no table it uses the default.
        assert lt.normalize("amount", "100억") == 10000000000

    def test_amount_uses_passed_table(self):
        assert lt.normalize("amount", "3.3억", {"억": 10**8}) == 330000000

    def test_unknown_tag_is_none(self):
        assert lt.normalize("nonsense", "x") is None

    def test_non_parsing_is_none(self):
        assert lt.normalize("date", "not a date") is None

    def test_types_constant(self):
        assert lt.TYPES == {"date", "number", "ordinal", "amount"}

    def test_deterministic(self):
        assert lt.normalize("date", "2030.1") == lt.normalize("date", "2030.1")

    def test_module_is_pure(self):
        # the module must not import the engine into its namespace
        assert not hasattr(lt, "pyrewire")
        assert not hasattr(lt, "EasySession")


class TestLiteralReConsistency:
    """Pinning test (#117 option b): the entity_audit detector and these
    normalizers must not drift. Every canonical literal example that entity_audit
    flags as a literal is parseable by its intended-type normalizer."""

    # (raw, intended type, expected scalar)
    # NB: only amount canonicals that _LITERAL_RE ALREADY detects belong here.
    # entity_audit's amount detection is partial/advisory (e.g. it does not flag
    # `1,000원` or `3.3억`); parse_amount is intentionally more permissive. We do
    # not widen the advisory detector to match — a known minor gap.
    CANONICAL = [
        ("2030.1", "date", 20300101),
        ("2024-07-01", "date", 20240701),
        ("2026", "number", 2026000),
        ("1,000", "number", 1000000),
        ("3.14", "number", 3140),
        ("제3호", "ordinal", 3),
        ("3위", "ordinal", 3),
        ("100억", "amount", 10000000000),
    ]

    @pytest.mark.parametrize("raw,type_tag,expected", CANONICAL)
    def test_detected_and_parsed(self, raw, type_tag, expected):
        from entity_audit import _LITERAL_RE
        assert _LITERAL_RE.match(raw), f"entity_audit no longer detects {raw!r}"
        assert lt.normalize(type_tag, raw) == expected
