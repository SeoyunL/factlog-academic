# SPDX-License-Identifier: Apache-2.0
"""Regression tests for ``normalize_confidence`` non-finite handling (#287)."""
from __future__ import annotations

import factlog.common as fcommon
import pytest


@pytest.mark.parametrize("value", ["nan", "NaN", " nan ", "inf", "Inf", "-inf", "infinity", "-infinity"])
def test_non_finite_falls_back_to_default(value: str) -> None:
    # Core regression: float() parses these into NaN/±inf, which must not leak
    # through the clamp as "nan"/"inf" but fall back to the neutral default.
    assert fcommon.normalize_confidence(value) == "0.50"


def test_large_finite_value_is_clamped_not_defaulted() -> None:
    # A finite but out-of-range value is a real number, so it clamps to the
    # bound rather than falling back to the 0.50 default.
    assert fcommon.normalize_confidence("1e9") == "1.00"


@pytest.mark.parametrize("value", ["abc", ""])
def test_unparseable_falls_back_to_default(value: str) -> None:
    assert fcommon.normalize_confidence(value) == "0.50"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.7", "0.70"),
        ("-0.5", "0.00"),
        ("1.5", "1.00"),
    ],
)
def test_finite_values_pass_through(value: str, expected: str) -> None:
    assert fcommon.normalize_confidence(value) == expected
