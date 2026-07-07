# SPDX-License-Identifier: Apache-2.0
"""Unit tests for `_normalize_lang` control-character rejection (#274).

`factlog lang` (no arg) is a one-line porcelain contract that SKILL.md parses,
and the stored value is fed back as a narration-language instruction. `.strip()`
trims only leading/trailing whitespace, so an interior newline/tab/CR would
survive — emitting a multi-line value that breaks the porcelain contract and acts
as a self-config prose-injection vector. `_normalize_lang` must reject interior
control characters with `(None, error)` while still accepting legitimate codes
and treating a whitespace-only value as a clear.
"""
from __future__ import annotations

import factlog.cli as cli


class TestNormalizeLangControlChars:
    def test_interior_newline_rejected(self):
        normalized, error = cli._normalize_lang("ko\nHACKED")
        assert normalized is None
        assert error is not None
        assert "control character" in error

    def test_interior_tab_rejected(self):
        normalized, error = cli._normalize_lang("ko\tx")
        assert normalized is None and error is not None

    def test_interior_carriage_return_rejected(self):
        normalized, error = cli._normalize_lang("ko\rx")
        assert normalized is None and error is not None

    def test_del_char_rejected(self):
        normalized, error = cli._normalize_lang("ko\x7fx")
        assert normalized is None and error is not None

    def test_trailing_whitespace_still_trimmed_to_valid(self):
        # A trailing newline is stripped away, leaving a clean code — accepted.
        normalized, error = cli._normalize_lang("ko\n")
        assert normalized == "ko" and error is None

    def test_whitespace_only_is_clear_not_error(self):
        # Purely-whitespace collapses to "" (clear/unset), which is a legitimate
        # action, not a control-character rejection.
        normalized, error = cli._normalize_lang("\n\t ")
        assert normalized == "" and error is None

    def test_valid_codes_still_accepted(self):
        for code in ("ko", "en", "ko-KR", "Korean", "브라질 포르투갈어"):
            normalized, error = cli._normalize_lang(code)
            assert error is None, f"{code!r} should be accepted"
            assert normalized == code.strip()

    def test_over_length_still_rejected(self):
        # Regression: the length contract survives alongside the new control check.
        normalized, error = cli._normalize_lang("x" * 100)
        assert normalized is None
        assert error is not None and "too long" in error
