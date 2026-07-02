# SPDX-License-Identifier: Apache-2.0
"""Unit tests for typed-scalar contradiction detection (#204).

``check_conflicts.detect_conflicts`` must judge single-valued contradictions on
the *canonical scalar* of a typed literal, not its raw string, so that equivalent
notations (억 ↔ 조) of the same amount collapse to one value instead of firing a
false CONFLICT — while untyped / unparseable objects degrade to the original raw
string comparison (backward compatible), and the reported values preserve the
original object strings (provenance).
"""
from __future__ import annotations

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


# A typed single-valued amount relation with the default unit table.
_AMOUNT_SPEC = common.TypedRelSpec("amount", "revenue")
_TYPED_AMOUNT = {"매출": _AMOUNT_SPEC}

# A typed single-valued date relation (no unit table needed).
_TYPED_DATE = {"출시": common.TypedRelSpec("date", "launch_date")}


class TestEquivalentNotationNotFlagged:
    def test_amount_equivalent_units_collapse_to_one_value(self):
        # amount(5400,"억") == amount(0.54,"조") == 5.4e11 -> a single value.
        facts = [
            _fact("갑사", "매출", 'amount(5400,"억")'),
            _fact("갑사", "매출", 'amount(0.54,"조")'),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"매출"}, _TYPED_AMOUNT)
        assert conflicts == {}

    def test_identical_scalar_dedups_regardless_of_source_rows(self):
        facts = [
            _fact("갑사", "매출", 'amount(5400,"억")'),
            _fact("갑사", "매출", 'amount(540000000000,"원")'),
            _fact("갑사", "매출", 'amount(0.54,"조")'),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"매출"}, _TYPED_AMOUNT)
        assert conflicts == {}

    def test_date_equivalent_precision_collapses_to_one_value(self):
        # 2030.1 (month precision, day->01) == 2030.01.01 == 20300101 -> one value.
        facts = [
            _fact("기서비스", "출시", "2030.1"),
            _fact("기서비스", "출시", "2030.01.01"),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"출시"}, _TYPED_DATE)
        assert conflicts == {}


class TestRealValueDifferenceDetected:
    def test_distinct_amounts_conflict(self):
        # 5000억 != 5400억 -> a real contradiction.
        facts = [
            _fact("갑사", "매출", 'amount(5000,"억")'),
            _fact("갑사", "매출", 'amount(5400,"억")'),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"매출"}, _TYPED_AMOUNT)
        assert list(conflicts) == [("갑사", "매출")]
        # Reported values preserve the ORIGINAL notation (provenance), not the
        # normalized integer, one representative per distinct scalar.
        assert conflicts[("갑사", "매출")] == ['amount(5000,"억")', 'amount(5400,"억")']

    def test_equivalent_pair_plus_a_real_difference(self):
        # {5000억} vs {5400억, 0.54조} -> 2 distinct scalars -> conflict, and the
        # message must say two (not three) values.
        facts = [
            _fact("갑사", "매출", 'amount(5000,"억")'),
            _fact("갑사", "매출", 'amount(5400,"억")'),
            _fact("갑사", "매출", 'amount(0.54,"조")'),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"매출"}, _TYPED_AMOUNT)
        reps = conflicts[("갑사", "매출")]
        assert len(reps) == 2  # two distinct values, three source rows
        # deterministic representatives: sort-min raw per scalar group.
        assert reps == ['amount(0.54,"조")', 'amount(5000,"억")']


class TestUntypedUnchanged:
    def test_untyped_single_valued_string_diff_conflicts(self):
        # No typed declaration -> raw string comparison, behaviour unchanged.
        facts = [
            _fact("을서비스", "주_속성", "값가"),
            _fact("을서비스", "주_속성", "값나"),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"주_속성"}, {})
        assert conflicts == {("을서비스", "주_속성"): ["값가", "값나"]}

    def test_untyped_omitting_typed_arg_defaults_to_raw(self):
        facts = [
            _fact("을서비스", "주_속성", "값가"),
            _fact("을서비스", "주_속성", "값나"),
        ]
        # Legacy call signature (no typed arg) must still work identically.
        conflicts = check_conflicts.detect_conflicts(facts, {"주_속성"})
        assert conflicts == {("을서비스", "주_속성"): ["값가", "값나"]}


class TestUnparseableDegrades:
    def test_unparseable_typed_object_uses_raw_key(self):
        # A typed relation but an object that does not parse -> raw-string key.
        # 'amount(5400,"억")' parses (5.4e11); 'not-a-number' does not -> two
        # distinct keys (one scalar, one raw) -> conflict.
        facts = [
            _fact("갑사", "매출", 'amount(5400,"억")'),
            _fact("갑사", "매출", "not-a-number"),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"매출"}, _TYPED_AMOUNT)
        assert conflicts[("갑사", "매출")] == ['amount(5400,"억")', "not-a-number"]

    def test_two_unparseable_distinct_strings_conflict(self):
        facts = [
            _fact("갑사", "매출", "n/a"),
            _fact("갑사", "매출", "unknown"),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"매출"}, _TYPED_AMOUNT)
        assert conflicts[("갑사", "매출")] == ["n/a", "unknown"]

    def test_two_unparseable_same_string_no_conflict(self):
        facts = [
            _fact("갑사", "매출", "n/a"),
            _fact("갑사", "매출", "n/a"),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"매출"}, _TYPED_AMOUNT)
        assert conflicts == {}

    def test_raw_integer_string_does_not_collide_with_scalar_key(self):
        # Key-namespace safety: amount(5400,"억") -> ("scalar", 540000000000);
        # the bare digit string "540000000000" has no unit -> unparseable as amount
        # -> ("raw", "540000000000"). The (kind, value) tuple keeps the int scalar
        # and the look-alike raw string distinct, so this is a real 2-value conflict,
        # never a false merge.
        facts = [
            _fact("갑사", "매출", 'amount(5400,"억")'),
            _fact("갑사", "매출", "540000000000"),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"매출"}, _TYPED_AMOUNT)
        assert conflicts[("갑사", "매출")] == ["540000000000", 'amount(5400,"억")']


class TestMultiValuedNotFlagged:
    def test_relation_not_single_valued_never_conflicts(self):
        # Not declared single-valued -> ignored entirely, even with typed spec.
        facts = [
            _fact("갑봇", "구성_요소", "ToolA"),
            _fact("갑봇", "구성_요소", "을서비스"),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, set(), _TYPED_AMOUNT)
        assert conflicts == {}


class TestSupersededIgnored:
    def test_superseded_row_excluded_from_conflict(self):
        # engine_facts drops non-engine statuses -> a superseded row can't fire.
        facts = [
            _fact("갑사", "매출", 'amount(5000,"억")', status="superseded"),
            _fact("갑사", "매출", 'amount(5400,"억")'),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"매출"}, _TYPED_AMOUNT)
        assert conflicts == {}
