# SPDX-License-Identifier: Apache-2.0
"""Single-valued membership is folded for every reader, not just the gate (#325).

``_relation_names_from`` returns the names in policy/single-valued.md verbatim,
so testing a row's relation against that set raw is a byte comparison between two
hand-written files. A KB written uniformly in NFD — the macOS default for Hangul,
and the scenario #325 exists for — then fails every membership test.

``check_conflicts`` folds both sides. The readers that display conflict counts
have to fold too, or ``factlog status`` reports 0 conflicts on a KB that
``finalize`` immediately refuses to compile. That divergence needs no mixed
spellings anywhere: one consistently decomposed KB produces it.
"""
from __future__ import annotations

import unicodedata

import check_conflicts
import common


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _nfd(value: str) -> str:
    return unicodedata.normalize("NFD", value)


def _fact(subject: str, relation: str, obj: str) -> dict[str, str]:
    return {
        "subject": subject,
        "relation": relation,
        "object": obj,
        "source": "sources/x.md",
        "status": "confirmed",
        "confidence": "0.9",
        "note": "",
    }


class TestFoldingHelpers:
    def test_folded_relation_names_collapses_spellings(self):
        folded = common.folded_relation_names({_nfc("소속"), _nfd("소속")})
        assert folded == {_nfc("소속")}

    def test_fold_relation_name_is_nfc(self):
        assert common.fold_relation_name(_nfd("소속")) == _nfc("소속")
        assert common.fold_relation_name(_nfc("소속")) == _nfc("소속")

    def test_folding_does_not_merge_distinct_relations(self):
        folded = common.folded_relation_names({"소속", "직급"})
        assert folded == {"소속", "직급"}

    def test_compatibility_variants_stay_distinct(self):
        # NFC only: fullwidth is a different relation, not a spelling.
        assert common.fold_relation_name("ＡＢＣ") != common.fold_relation_name("ABC")

    def test_composed_spelling_prefers_nfc_over_plain_min(self):
        # The three consumers that stand a representative in front of a folded
        # group must pick the same member, so the choice lives in common. Plain
        # min() returns the DECOMPOSED form — conjoining jamo (U+1100…) sort below
        # precomposed syllables (U+AC00…) — which is the spelling that will not
        # match what a reader types from an NFC editor.
        raws = {_nfc("한국대학교"), _nfd("한국대학교")}
        assert min(raws) == _nfd("한국대학교")  # the trap
        assert common.composed_spelling(raws) == _nfc("한국대학교")

    def test_composed_spelling_is_a_string_actually_written(self):
        # Uniformly decomposed group: no composed member exists, so provenance
        # wins over greppability and the returned string is still one of the raws.
        raws = {_nfd("한국대학교"), _nfd("서울대학교")}
        assert common.composed_spelling(raws) in raws

    def test_check_conflicts_uses_the_shared_chooser(self):
        raws = {_nfc("김철수"), _nfd("김철수")}
        assert check_conflicts._representative(raws) == common.composed_spelling(raws)


class TestReadersAgreeWithTheGate:
    """The KB shape that diverged: relation uniformly NFD, policy NFC."""

    _POLICY = {_nfc("소속")}
    _ROWS = [
        _fact(_nfd("김철수"), _nfd("소속"), "A사"),
        _fact(_nfd("김철수"), _nfd("소속"), "B사"),
    ]

    def test_gate_reports_the_contradiction(self):
        conflicts = check_conflicts.detect_conflicts(self._ROWS, self._POLICY, {}, {})
        assert len(conflicts) == 1

    def test_folded_membership_admits_the_same_rows(self):
        # The helper contract only. That `factlog status` and `corroboration`
        # actually CALL it is pinned end-to-end in tests/test_status_cmd.sh and
        # tests/test_corroboration.sh — reverting either call site leaves this
        # file entirely green, so it must not claim to cover them.
        folded = common.folded_relation_names(self._POLICY)
        admitted = [
            r for r in self._ROWS if common.fold_relation_name(r["relation"]) in folded
        ]
        assert len(admitted) == len(self._ROWS)

    def test_raw_membership_would_have_dropped_them(self):
        # The defect, stated as a fact about the old predicate: it admits nothing,
        # which is why status showed 0 while the gate refused to compile.
        admitted = [r for r in self._ROWS if r["relation"] in self._POLICY]
        assert admitted == []
