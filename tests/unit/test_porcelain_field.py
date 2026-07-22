# SPDX-License-Identifier: Apache-2.0
"""The shared neutralization rule (#141), and the set it covers (#396).

`porcelain_field` backs two contracts: a positional `--porcelain` row read by column
offset, and a human stderr block whose line shape carries meaning. Both reduce to one
guarantee — no tab and no line break survives — so the tests here are about the *set*
that guarantee ranges over, which is where #396's first cut went wrong: it was derived
from "the C0 range" and missed U+0085/U+2028/U+2029, which are not C0, are legal in
both XML and JSON, and split a line under `str.splitlines()`.
"""
from __future__ import annotations

import pytest

from factlog.integrations.common.porcelain import _LINE_BREAKS, porcelain_field


class TestTheNeutralizedSet:
    def test_the_hardcoded_set_is_exactly_pythons_line_break_set(self):
        """The list is hardcoded; this derives it and asserts equality.

        `_LINE_BREAKS` stays a literal because deriving it at import time costs a scan of
        all 1.1M code points on every `import factlog`. That leaves a stated risk with no
        teeth — "if Python widens what `splitlines()` breaks on, the gate silently narrows"
        — so the scan lives here instead, where it runs once per suite. Python widening the
        set turns this red rather than turning a gate quiet.
        """
        derived = {c for c in map(chr, range(0x110000)) if len(f"a{c}b".splitlines()) > 1}
        assert set(_LINE_BREAKS) == derived

    @pytest.mark.parametrize("char", sorted({*_LINE_BREAKS, "\t"}))
    def test_every_covered_character_becomes_one_space(self, char):
        assert porcelain_field(f"a{char}b") == "a b"

    def test_each_character_maps_to_exactly_one_space(self):
        # The guarantee is "no tab or line break survives", never "length is preserved" —
        # but it *is* one-for-one, so "\r\n" is two spaces and column positions in a
        # fixed-width field do not shift. A regex collapsing runs would break that.
        assert porcelain_field("a\r\nb") == "a  b"
        assert len(porcelain_field("a\r\nb")) == len("a\r\nb")

    def test_a_gated_field_keeps_its_row_one_line_and_five_columns(self):
        # The function gates a *field*, never an assembled row — a row's own tabs are
        # structure, and passing the whole row through would flatten them into text.
        # This is how every caller uses it, and the shape the contract is stated in.
        row = "\t".join(["result", "ok", porcelain_field("a\u2028b"), "", ""])
        assert len(row.splitlines()) == 1
        assert len(row.split("\t")) == 5

class TestWhatIsDeliberatelyLeftAlone:
    """Counterexamples. The gate is not "strip control characters" — it is two contracts.

    A character that adds neither a column nor a row breaks neither contract, and removing
    it would make this function an arbiter of what renders nicely.
    """

    @pytest.mark.parametrize("char, name", [
        ("\x7f", "DEL — reaches the #396 gate through a real PubMed efetch"),
        ("\x1b", "ESC — not reachable via XML, but JSON and POSIX paths admit it"),
    ])
    def test_a_non_line_breaking_control_character_survives(self, char, name):
        assert porcelain_field(f"a{char}b") == f"a{char}b", name

    def test_an_ansi_erase_line_sequence_still_leaves_one_row_of_three_fields(self):
        # Measured during review: a POSIX filename may contain ESC outright, so a `ledger`
        # path can carry `\x1b[2K` (ANSI erase-line) through this gate. A terminal may
        # erase what is already drawn, but neither contract claims anything about that —
        # the row's field count and line count, which they do claim, are intact.
        row = "\t".join(["result", "ok", porcelain_field("a\x1b[2Kforged.md")])
        assert len(row.splitlines()) == 1
        assert len(row.split("\t")) == 3

    def test_ordinary_text_is_returned_unchanged(self):
        assert porcelain_field("1998 Dec-1999 Jan") == "1998 Dec-1999 Jan"
        # Not whitespace-collapsing: a plain space run is not a contract problem.
        assert porcelain_field("a  b") == "a  b"

    @pytest.mark.parametrize("text", [
        "\U0001f600",              # astral plane
        "\U0001d11e",              # astral plane, musical symbol
        "\U0001f1f0\U0001f1f7",    # regional indicator pair (flag)
        "e\u0301",                 # combining acute
        "\xa0",                    # NBSP: whitespace, but no tab and no line break
    ])
    def test_non_bmp_and_combining_text_is_untouched(self, text):
        # `translate` maps code points one-for-one, so nothing here can be split or
        # mangled. NBSP is the pointed case: `str.isspace()` is true for it, so an
        # isspace()-based rule would have rewritten legitimate text.
        assert porcelain_field(text) == text
