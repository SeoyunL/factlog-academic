# SPDX-License-Identifier: Apache-2.0
"""Unit tests for Unicode-normalization folding in conflict detection (#325).

``check_conflicts`` folded only the *relation* axis. The object axis used the raw
string, so two objects that render identically but are spelled NFC on one row and
NFD on another were reported as a contradiction — a false positive the reader
cannot act on, because the two values look the same on screen.

The fold has to happen *before* ``literal_types.normalize``, not only on the
untyped fallback: an NFD-authored typed literal does not parse, degrades to the
``"raw"`` tag, and never meets its NFC twin under ``"scalar"``.

Folding is NFC only — compatibility variants (fullwidth) and case stay distinct.
"""
from __future__ import annotations

import unicodedata

import check_conflicts
import common


def _fact(subject: str, relation: str, obj: str, status: str = "confirmed") -> dict[str, str]:
    return {
        "subject": subject,
        "relation": relation,
        "object": obj,
        "source": "sources/x.md",
        "status": status,
        "confidence": "0.9",
        "note": "",
    }


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _nfd(value: str) -> str:
    return unicodedata.normalize("NFD", value)


_AMOUNT_SPEC = common.TypedRelSpec("amount", "revenue")
_TYPED_AMOUNT = {"매출": _AMOUNT_SPEC}
_TYPED_ORDINAL = {"순위": common.TypedRelSpec("ordinal", "rank")}


class TestTypedObjectFoldedBeforeParse:
    """An NFD-authored typed literal must reach its scalar, not degrade to raw."""

    def test_amount_nfc_vs_nfd_same_value_no_conflict(self):
        # Same amount, one row authored NFD: the 억 unit decomposes, parse_amount
        # fails, and the row would key as ("raw", …) against its twin's scalar.
        obj = 'amount(5400,"억")'
        facts = [
            _fact("갑사", "매출", _nfc(obj)),
            _fact("갑사", "매출", _nfd(obj)),
        ]
        assert check_conflicts.detect_conflicts(facts, {"매출"}, _TYPED_AMOUNT) == {}

    def test_ordinal_nfc_vs_nfd_same_rank_no_conflict(self):
        facts = [
            _fact("갑", "순위", _nfc("제3호")),
            _fact("갑", "순위", _nfd("제3호")),
        ]
        assert check_conflicts.detect_conflicts(facts, {"순위"}, _TYPED_ORDINAL) == {}

    def test_all_nfd_kb_cross_notation_amounts_collapse(self):
        # 5400억 == 0.54조 == 5.4e11. In an all-NFD KB neither side parsed before,
        # so #116's cross-notation equivalence never fired there; now it does.
        facts = [
            _fact("갑사", "매출", _nfd('amount(5400,"억")')),
            _fact("갑사", "매출", _nfd('amount(0.54,"조")')),
        ]
        assert check_conflicts.detect_conflicts(facts, {"매출"}, _TYPED_AMOUNT) == {}

    def test_nfd_typed_literals_with_different_values_still_conflict(self):
        # Folding must not swallow a genuine typed contradiction.
        facts = [
            _fact("갑사", "매출", _nfd('amount(5400,"억")')),
            _fact("갑사", "매출", _nfd('amount(1,"조")')),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"매출"}, _TYPED_AMOUNT)
        assert list(conflicts) == [("갑사", "매출")]
        assert len(conflicts[("갑사", "매출")]) == 2


class TestUntypedObjectAxisFolded:
    """Issue case (b): the unactionable false positive."""

    def test_mixed_untyped_object_same_value_no_conflict(self):
        facts = [
            _fact("연구소", "소속", _nfc("한국대학교")),
            _fact("연구소", "소속", _nfd("한국대학교")),
        ]
        assert check_conflicts.detect_conflicts(facts, {"소속"}, {}) == {}

    def test_reported_object_is_a_raw_string_actually_present(self):
        raws = [_nfc("한국대학교"), _nfd("한국대학교")]
        facts = [
            _fact("연구소", "소속", raws[0]),
            _fact("연구소", "소속", raws[1]),
            _fact("연구소", "소속", "서울대학교"),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"소속"}, {})
        values = conflicts[("연구소", "소속")]
        # Two equivalence classes, and the merged one reports its composed form.
        assert values == ["서울대학교", _nfc("한국대학교")]


class TestRepresentativeChoice:
    """The reported string is one that was written, and the one likeliest to grep."""

    def test_representative_prefers_the_composed_spelling(self):
        # Plain min() would always return the NFD form: conjoining jamo (U+1100…)
        # sort below precomposed syllables (U+AC00…). That is the spelling that
        # will NOT match what a reader types from an NFC editor.
        raws = {_nfc("한국대학교"), _nfd("한국대학교")}
        assert min(raws) == _nfd("한국대학교")  # the trap this avoids
        assert check_conflicts._representative(raws) == _nfc("한국대학교")

    def test_representative_is_deterministic_when_no_form_is_composed(self):
        # Two distinct NFD spellings that fold together cannot both be NFC; the
        # choice must still be stable, so it falls back to lexicographic order.
        raws = {_nfd("김철수"), _nfd("김철수") + "x"}
        assert check_conflicts._representative(raws) == min(raws)


class TestNonEquivalentNotationsStayDistinct:
    """NFC only: no NFKC, no casefold."""

    def test_fullwidth_stays_a_separate_value(self):
        facts = [_fact("갑", "속성", "ABC"), _fact("갑", "속성", "ＡＢＣ")]
        conflicts = check_conflicts.detect_conflicts(facts, {"속성"}, {})
        assert conflicts[("갑", "속성")] == sorted(["ABC", "ＡＢＣ"])

    def test_case_stays_a_separate_value(self):
        facts = [_fact("갑", "속성", "abc"), _fact("갑", "속성", "ABC")]
        conflicts = check_conflicts.detect_conflicts(facts, {"속성"}, {})
        assert conflicts[("갑", "속성")] == ["ABC", "abc"]
