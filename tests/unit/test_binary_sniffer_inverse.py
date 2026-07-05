# SPDX-License-Identifier: Apache-2.0
"""Regression tests: _looks_binary must be the strict boolean inverse of
is_text_source (#259).

`is_text_source` (merge/coverage) tolerates a multi-byte UTF-8 char truncated at
the sniff boundary ONLY when the file actually extends past it: for a fully-read
short file, an invalid trailing byte means binary. `_looks_binary` (ingest --scan)
read just `[:sniff]` and so lost the "extends past sniff" information, making the
two disagree for a short truncated/corrupt binary — classified as NEITHER text
nor binary. coverage then says "run ingest", ingest says "sync reads it as text",
and sync (is_text_source=False) drops it: a silently un-ingested source with
three contradictory messages. The two sniffers must be exact complements so every
source is exactly one of {text, binary}.
"""
from __future__ import annotations

import common
from factlog.cli import _looks_binary

SNIFF = 8192


class TestSnifferStrictInverse:
    def _assert_inverse(self, path):
        assert _looks_binary(path) == (not common.is_text_source(path)), (
            f"sniffers disagree on {path.name}: "
            f"_looks_binary={_looks_binary(path)}, is_text_source={common.is_text_source(path)}"
        )

    def test_short_truncated_utf8_is_binary_not_neither(self, tmp_path):
        # The bug: 14 bytes < sniff, decode error in the final 3 bytes.
        p = tmp_path / "report.pdf"
        p.write_bytes(b"hello world " + b"\xe2\x82")
        # Must be classified binary by BOTH (not text, and looks binary).
        assert common.is_text_source(p) is False
        assert _looks_binary(p) is True
        self._assert_inverse(p)

    def test_plain_short_text(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_bytes(b"hello world")
        assert _looks_binary(p) is False
        self._assert_inverse(p)

    def test_nul_byte_is_binary(self, tmp_path):
        p = tmp_path / "n.bin"
        p.write_bytes(b"abc\x00def")
        assert _looks_binary(p) is True
        self._assert_inverse(p)

    def test_empty_file(self, tmp_path):
        p = tmp_path / "e.txt"
        p.write_bytes(b"")
        self._assert_inverse(p)

    def test_long_valid_utf8(self, tmp_path):
        p = tmp_path / "big.txt"
        p.write_bytes(b"a" * (SNIFF * 2))
        assert _looks_binary(p) is False
        self._assert_inverse(p)

    def test_multibyte_truncated_at_boundary_extends_past_sniff(self, tmp_path):
        # A 3-byte char split exactly at the sniff boundary in a file that DOES
        # extend past it: tolerated as text by is_text_source, so not binary.
        p = tmp_path / "trunc.txt"
        p.write_bytes(b"a" * (SNIFF - 1) + b"\xe2\x82\xac")  # len = SNIFF + 2
        assert _looks_binary(p) is False
        self._assert_inverse(p)
