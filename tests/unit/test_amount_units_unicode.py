# SPDX-License-Identifier: Apache-2.0
"""The `amount` unit table is stored and looked up under the same NFC fold (#325).

Folding the object axis in ``check_conflicts`` (this issue) made the unit table
the last raw-byte comparison on the typed path. ``_parse_amount_units`` kept the
unit names exactly as the policy file spelled them and ``parse_amount`` looked
them up with the object's own spelling, so the two only met when both files were
written in the same normalization form.

On the KB this issue exists for — everything NFD, the macOS default for Hangul —
the folded object no longer matched the decomposed table key, every amount
degraded to untyped, and two spellings of ONE value (``5400억`` and ``0.54조``,
both 5.4e11) split into a contradiction that ``finalize`` refuses to compile.
The message named neither normalization nor the equality, so the only repair
visible to the reader was to supersede a legitimate row.

Both ends fold now: the table stores NFC keys (duplicate check included) and the
lookup key is composed before it meets the table, which also protects a
caller-supplied table. NFC only, never NFKC — fullwidth stays a different unit.
"""
from __future__ import annotations

import itertools
import unicodedata

import pytest

import check_conflicts
import common
from factlog import literal_types as lt
from factlog.common import FactlogError


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _nfd(value: str) -> str:
    return unicodedata.normalize("NFD", value)


def _fact(subject: str, relation: str, obj: str, source: str = "sources/x.md") -> dict[str, str]:
    return {
        "subject": subject,
        "relation": relation,
        "object": obj,
        "source": source,
        "status": "confirmed",
        "confidence": "0.9",
        "note": "",
    }


_UNITS_CLAUSE = "억=1e8, 조=1e12, 원=1"


class TestUnitTableStoredFolded:
    """The parsed table's keys are composed, whatever the policy file's form."""

    def test_nfd_units_clause_stores_nfc_keys(self):
        units = common._parse_amount_units(_nfd(_UNITS_CLAUSE))
        assert set(units) == {_nfc("억"), _nfc("조"), _nfc("원")}
        # Explicit about the fold: no decomposed key survives into the table.
        assert all(key == _nfc(key) for key in units)

    def test_values_survive_the_fold(self):
        units = common._parse_amount_units(_nfd(_UNITS_CLAUSE))
        assert units[_nfc("억")] == 10**8
        assert units[_nfc("조")] == 10**12

    def test_duplicate_check_runs_on_the_folded_key(self):
        # Two spellings of one unit are a duplicate declaration, not two units:
        # unfolded, the second silently shadowed nothing and both were stored.
        with pytest.raises(FactlogError, match="duplicate unit"):
            common._parse_amount_units(_nfc("억") + "=1e8, " + _nfd("억") + "=1e9")

    def test_composed_clause_is_unchanged(self):
        # CONTROL (passes before and after): an already-composed clause folds to
        # itself, so a KB that was never decomposed sees identical bytes.
        assert common._parse_amount_units(_UNITS_CLAUSE) == {
            "억": 10**8, "조": 10**12, "원": 1,
        }

    def test_ascii_clause_is_unchanged(self):
        # CONTROL (passes before and after): NFC is the identity on ASCII.
        assert common._parse_amount_units("kg=1000, g=1") == {"kg": 1000, "g": 1}


class TestLookupKeyFolded:
    """``units.get`` composes the object's unit, so either side may be decomposed."""

    def test_nfd_object_against_composed_table(self):
        # The realistic direction: DEFAULT_AMOUNT_UNITS is composed in source,
        # the object comes from an NFD-authored source file.
        assert lt.parse_amount(_nfd("5400억"), lt.DEFAULT_AMOUNT_UNITS) == 540_000_000_000

    def test_nfd_object_compound_form_against_composed_table(self):
        assert lt.parse_amount(_nfd('amount(5400,"억")'), lt.DEFAULT_AMOUNT_UNITS) == 540_000_000_000

    def test_nfd_table_and_nfc_object(self):
        units = common._parse_amount_units(_nfd(_UNITS_CLAUSE))
        assert lt.parse_amount(_nfc("5400억"), units) == 540_000_000_000

    def test_nfd_table_and_nfd_object(self):
        # CONTROL (passes before and after): matching decomposed forms met as raw
        # bytes already. It pins that folding both ends preserves that hit.
        units = common._parse_amount_units(_nfd(_UNITS_CLAUSE))
        assert lt.parse_amount(_nfd("5400억"), units) == 540_000_000_000

    def test_both_notations_reach_the_same_value_on_an_nfd_table(self):
        # CONTROL (passes before and after, same reason as above). The pair from
        # the reproduction: 5400억 == 0.54조 == 5.4e11. The parse was never the
        # broken half here — the object fold on the checker side was.
        units = common._parse_amount_units(_nfd(_UNITS_CLAUSE))
        assert lt.parse_amount(_nfd("5400억"), units) == lt.parse_amount(_nfd("0.54조"), units)

    def test_fused_currency_fallback_sees_a_composed_marker(self):
        # ``억원`` is recovered by stripping one trailing ``원``. Decomposed, the
        # tail is a trailing jamo and the strip never fires.
        assert lt.parse_amount(_nfd("100억원"), lt.DEFAULT_AMOUNT_UNITS) == 10**10

    def test_fullwidth_unit_stays_unknown(self):
        # CONTROL (passes before and after): NFC, never NFKC. The compatibility
        # variant is a different unit, not a second spelling of one — this is the
        # guard that the fold introduced here did not reach for NFKC.
        assert lt.parse_amount("100Ｗ", {"W": 1}) is None

    def test_unknown_unit_still_none(self):
        # CONTROL (passes before and after): folding widens no unit set.
        assert lt.parse_amount(_nfd("100백만"), lt.DEFAULT_AMOUNT_UNITS) is None


class TestGateOnAnAllNfdTypedKb:
    """End to end: the reproduction KB stops reporting a contradiction."""

    def _spec(self, clause: str, relation: str = "") -> dict[str, common.TypedRelSpec]:
        return {
            relation or _nfd("매출"): common.TypedRelSpec(
                "amount", "revenue", common._parse_amount_units(clause)
            )
        }

    def test_nfd_units_clause_no_longer_splits_one_value(self):
        facts = [
            _fact(_nfd("삼성"), _nfd("매출"), _nfd("5400억"), "sources/a.md"),
            _fact(_nfd("삼성"), _nfd("매출"), _nfd("0.54조"), "sources/b.md"),
        ]
        conflicts = check_conflicts.detect_conflicts(
            facts, {_nfd("매출")}, self._spec(_nfd(_UNITS_CLAUSE))
        )
        assert conflicts == {}

    def test_mixed_kb_base_unit_matches_the_scaled_notation(self):
        # The mixed variant of the same regression: an NFD 억 row against a plain
        # 원 row naming the identical value.
        facts = [
            _fact(_nfd("삼성"), _nfd("매출"), _nfd("5400억"), "sources/a.md"),
            _fact(_nfd("삼성"), _nfd("매출"), "540000000000원", "sources/b.md"),
        ]
        conflicts = check_conflicts.detect_conflicts(
            facts, {_nfd("매출")}, self._spec(_nfd(_UNITS_CLAUSE))
        )
        assert conflicts == {}

    def test_a_real_disagreement_on_an_nfd_table_still_conflicts(self):
        # CONTROL (passes before and after, by different routes: unparsed raw
        # strings before, distinct scalars after). Folding must not swallow a
        # genuine contradiction: 5400억 != 1조.
        facts = [
            _fact(_nfd("삼성"), _nfd("매출"), _nfd("5400억"), "sources/a.md"),
            _fact(_nfd("삼성"), _nfd("매출"), _nfd("1조"), "sources/b.md"),
        ]
        conflicts = check_conflicts.detect_conflicts(
            facts, {_nfd("매출")}, self._spec(_nfd(_UNITS_CLAUSE))
        )
        assert list(conflicts) == [(_nfd("삼성"), _nfd("매출"))]

    def test_composed_kb_is_unaffected(self):
        # CONTROL (passes before and after): the composed KB always worked.
        facts = [
            _fact("삼성", "매출", "5400억", "sources/a.md"),
            _fact("삼성", "매출", "0.54조", "sources/b.md"),
        ]
        conflicts = check_conflicts.detect_conflicts(
            facts, {"매출"}, self._spec(_UNITS_CLAUSE, "매출")
        )
        assert conflicts == {}


# Objects spanning every typed parser and both notations, plus strings that
# parse under none of them.
_PROBE_OBJECTS = sorted({
    form(obj)
    for obj in (
        "5400억", "0.54조", "540000000000원", "1조", "7억", "2.675억", "100억원",
        "1,000원", 'amount(5400,"억")', 'amount(0.54,"조")', "amount(100,억)",
        "제3호", "3위", "5위", "제5번", "3rd", "3등", "ordinal(3)",
        "2030.1", "date(2030,1)", "number(2.5)", "2.5", "백만", "100백만원",
        "무순위", "abc",
    )
    for form in (_nfc, _nfd)
})

_PROBE_SPECS = [
    None,
    common.TypedRelSpec("amount", "v", None),  # DEFAULT_AMOUNT_UNITS
    common.TypedRelSpec("ordinal", "v"),
    common.TypedRelSpec("date", "v"),
    common.TypedRelSpec("number", "v"),
] + [
    common.TypedRelSpec("amount", "v", common._parse_amount_units(form(clause)))
    for clause in (_UNITS_CLAUSE, "억=1e8, 원=1", "조=1e12", "kg=1000, g=1", "원=1")
    for form in (_nfc, _nfd)
]


class TestFoldingIsMergeOnly:
    """The property the whole object-fold rests on, checked over every pair.

    ``_group_key`` folds, ``_group_key_unfolded`` does not. Folding is allowed to
    *join* two objects that keyed apart — that is #325's gain. It must never
    *split* two that keyed together: those were already one value, so a split
    invents a contradiction out of a normalization form, which is precisely how
    the raw unit table turned ``5400억``/``0.54조`` into a CONFLICT that never
    existed before this branch. Nothing else in the module can catch that
    direction — the disclosure paths only describe merges.
    """

    def test_no_pair_that_grouped_together_is_split_by_the_fold(self):
        split = [
            (spec, a, b)
            for spec in _PROBE_SPECS
            for a, b in itertools.combinations(_PROBE_OBJECTS, 2)
            if check_conflicts._group_key_unfolded(a, spec)
            == check_conflicts._group_key_unfolded(b, spec)
            and check_conflicts._group_key(a, spec) != check_conflicts._group_key(b, spec)
        ]
        assert split == [], [
            (spec, ascii(a), ascii(b)) for spec, a, b in split[:10]
        ]

    def test_fold_is_the_identity_on_composed_objects(self):
        # CONTROL (passes before and after): the byte-invariance the issue asks
        # for. A KB with no decomposed code point keys identically either way,
        # under every spec — including the decomposed unit tables, which a
        # composed object must still resolve against.
        composed = [obj for obj in _PROBE_OBJECTS if obj == _nfc(obj)]
        assert composed  # the probe set is not accidentally empty
        for spec in _PROBE_SPECS:
            for obj in composed:
                assert check_conflicts._group_key(obj, spec) == (
                    check_conflicts._group_key_unfolded(obj, spec)
                ), (spec, ascii(obj))
