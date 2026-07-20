# SPDX-License-Identifier: Apache-2.0
"""Unit tests for literal_types.humanize() (#188 display-only humanizer)."""
from __future__ import annotations

import literal_types as lt
import pytest


class TestHumanizeDate:
    def test_date_1arg(self):
        # Year precision renders only the year it carries — padding it to
        # "2030-01" would display a month the source never had (#385).
        assert lt.humanize("date(2030)") == "2030"

    def test_date_1arg_out_of_range_year_verbatim(self):
        assert lt.humanize("date(0000)") == "date(0000)"

    def test_date_2arg(self):
        assert lt.humanize("date(2030,1)") == "2030-01"

    def test_date_3arg(self):
        assert lt.humanize("date(2030,1,15)") == "2030-01-15"

    def test_date_zero_pad_day(self):
        assert lt.humanize("date(2030,1,5)") == "2030-01-05"

    def test_date_zero_pad_month(self):
        assert lt.humanize("date(2030,3)") == "2030-03"


class TestHumanizeAmount:
    def test_amount_integer(self):
        assert lt.humanize('amount(7,"억")') == "7억"

    def test_amount_decimal(self):
        assert lt.humanize('amount(2.675,"억")') == "2.675억"


class TestHumanizeNumber:
    def test_number(self):
        assert lt.humanize("number(2.5)") == "2.5"


class TestHumanizePassthrough:
    def test_plain_string(self):
        assert lt.humanize("Chest") == "Chest"

    def test_bare_year(self):
        assert lt.humanize("2005") == "2005"

    def test_malformed_date(self):
        assert lt.humanize("date(20xx,1)") == "date(20xx,1)"

    def test_empty_string(self):
        assert lt.humanize("") == ""

    def test_passthrough_identity(self):
        # Strings are immutable; passthrough returns the same object.
        s = "Chest"
        result = lt.humanize(s)
        assert result == s
        assert result is s  # passthrough must be byte-identical object


class TestHumanizeDateOutOfRange:
    """Out-of-range date compounds must be returned verbatim (not fabricated ISO)."""

    def test_month_too_high(self):
        assert lt.humanize("date(2030,13)") == "date(2030,13)"

    def test_month_zero(self):
        assert lt.humanize("date(2030,0)") == "date(2030,0)"

    def test_day_too_high(self):
        assert lt.humanize("date(2030,1,45)") == "date(2030,1,45)"

    def test_valid_3arg_still_humanizes(self):
        assert lt.humanize("date(2030,12,31)") == "2030-12-31"

    def test_valid_2arg_still_humanizes(self):
        assert lt.humanize("date(2030,1)") == "2030-01"


class TestHumanizeCalendarImpossible:
    """A day <= 31 that is impossible for its month must not be fabricated as ISO."""

    @pytest.mark.parametrize("value", [
        "date(2024,2,30)",   # February never has 30 days
        "date(2024,4,31)",   # April has 30 days
        "date(2023,2,29)",   # non-leap Feb 29
    ])
    def test_impossible_returns_verbatim(self, value):
        assert lt.humanize(value) == value

    def test_leap_feb_29_humanizes(self):
        assert lt.humanize("date(2024,2,29)") == "2024-02-29"

    def test_extreme_future_humanizes(self):
        assert lt.humanize("date(9999,12,31)") == "9999-12-31"


class TestHumanizeNeverRaises:
    @pytest.mark.parametrize("value", [
        "",
        "Chest",
        "2005",
        "date(20xx,1)",
        "date(2030)",
        "date(2030,)",
        "date(2030,1)",
        "date(2030,1,15)",
        "date(2030,1,5)",
        'amount(7,"억")',
        'amount(2.675,"억")',
        "number(2.5)",
        "ordinal(3)",
        "amount()",
        "date()",
        "number(abc)",
    ])
    def test_never_raises(self, value):
        # humanize must be total — no exception for any input
        result = lt.humanize(value)
        assert isinstance(result, str)
