# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the shared Unicode digit vocabulary (#410).

`fold_decimal_digits` replaced two near-identical private helpers — the Zotero
import boundary's `_ascii_digits` (#398) and the CSL export boundary's
`_fold_decimal_digits` (#399). The defensive signature was adopted because the
demanding one is its special case: on an all-`Nd` run the two agree, and the
defensive one additionally survives everything else. These tests pin that
equivalence, the totality that makes the merge safe, and the fact that the fold
and the `non_ascii_digits` diagnostic name the SAME set — the property that
justifies one module rather than two.

The per-boundary behaviour stays with its boundary (`test_csl.py`,
`test_zotero_item_parser.py`), because the policy (fold vs refuse) is what
legitimately differs there.
"""
from __future__ import annotations

import sys
import unicodedata

from factlog.text_norm import fold_decimal_digits, non_ascii_digits

# One representative per script that `\d` matches, all spelling 2020. NFKC folds
# only the first, which is why NFKC is not the mechanism (see the module docstring).
_YEAR_SPELLINGS = {
    "2020": "ASCII",
    "２０２０": "full-width",
    "٢٠٢٠": "Arabic-Indic",
    "۲۰۲۰": "Extended Arabic-Indic",
    "२०२०": "Devanagari",
    "２0٢0": "mixed within one run",
}


class TestFoldDecimalDigits:
    def test_every_decimal_script_folds_to_ascii(self):
        for spelling in _YEAR_SPELLINGS:
            assert fold_decimal_digits(spelling) == "2020"

    def test_non_digits_pass_through_untouched(self):
        # The defensive half of the signature: #398's helper raised ValueError
        # here, which is why it could not be the shared one.
        assert fold_decimal_digits("n.d.") == "n.d."
        assert fold_decimal_digits("") == ""
        assert fold_decimal_digits("출판 ２０２０년") == "출판 2020년"
        assert fold_decimal_digits("10.１２３４/abc-x") == "10.1234/abc-x"

    def test_non_decimal_digit_lookalikes_are_not_folded(self):
        # Category `No`, not `Nd`: `\d` never matched them, so folding one would
        # invent a value the source never stated. NFKC would have.
        assert fold_decimal_digits("20²0") == "20²0"
        assert fold_decimal_digits("①②③④") == "①②③④"

    def test_length_and_position_preserved(self):
        # What makes "fold, then match `[0-9]{4}`" equivalent to the old `\d{4}`:
        # a fold that changed length could create or destroy a 4-digit run.
        for value in (*_YEAR_SPELLINGS, "출판 ２０２０년", "PMID: １23４567"):
            folded = fold_decimal_digits(value)
            assert len(folded) == len(value)
            for original, out in zip(value, folded):
                assert original.isdecimal() == out.isdecimal()

    def test_idempotent(self):
        for value in (*_YEAR_SPELLINGS, "출판 ２０２０년", "20²0"):
            once = fold_decimal_digits(value)
            assert fold_decimal_digits(once) == once

    def test_folds_substrings_and_groups_independently(self):
        # #405 folds a DOI *prefix* only, leaving the opaque suffix alone, so
        # the fold must be usable on a slice or a regex group. It is, because it
        # is per-character: folding a part equals the same part of the folded
        # whole. This asserts the property, not #405's behaviour.
        value = "10.１２３４/АBC１２３"
        for cut in range(len(value) + 1):
            assert (
                fold_decimal_digits(value[:cut]) + fold_decimal_digits(value[cut:])
                == fold_decimal_digits(value)
            )

    def test_total_over_the_whole_nd_category(self):
        # The old #398 helper argued totality from a precondition ("only ever
        # called on a `\d` capture"). The shared one needs no precondition, but
        # `unicodedata.decimal` must still be defined for every `Nd` character —
        # `Nd` grows with each Unicode revision, so this sweeps the running build
        # rather than trusting a spot check.
        for code in range(sys.maxunicode + 1):
            char = chr(code)
            if not char.isdecimal():
                continue
            folded = fold_decimal_digits(char)
            assert len(folded) == 1
            assert folded.isascii() and folded.isdigit()
            assert int(folded) == unicodedata.decimal(char)


class TestSharedVocabulary:
    """The fold and the diagnostic must name the same set, or a warning would
    blame a character the parsers never rejected. That shared claim is the reason
    both live in one module."""

    def test_folding_removes_exactly_what_the_diagnostic_names(self):
        for value in (*_YEAR_SPELLINGS, "date(２０２０,１)", "출판 ２０２０년"):
            assert non_ascii_digits(fold_decimal_digits(value)) == ""

    def test_diagnostic_ignores_what_the_fold_ignores(self):
        for value in ("20²0", "①②③④", "n.d."):
            assert non_ascii_digits(value) == ""
            assert fold_decimal_digits(value) == value

    def test_literal_types_re_exports_the_same_object(self):
        # `literal_types.non_ascii_digits` stays importable for its report callers
        # (`common`, `entity_audit`); it must be the same function, not a copy.
        from factlog import literal_types

        assert literal_types.non_ascii_digits is non_ascii_digits
