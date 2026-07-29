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


class TestFullWidthDegradesToRaw:
    """The migration risk of the ASCII-only digit policy (#331), pinned.

    Full-width digits stopped parsing, so a full-width object degrades to the
    ``("raw", obj)`` key while its ASCII twin keys as ``("scalar", …)``. Under a
    single-valued relation that is **two values**, so a KB carrying both
    spellings now reports a CONFLICT and ``main`` returns 1 where it used to
    return 0. That gate flip is the one user-visible break this policy causes and
    it is intended: the resolution is to correct the source to ASCII and
    re-collect, NOT to fold full-width to ASCII in ``_group_key``. These tests
    fail if such a fold is ever added, which is the point of having them —
    ``docs/reference/typed-relations.md`` documents the same contract.
    """

    def test_full_width_amount_keys_raw_not_scalar(self):
        assert check_conflicts._group_key("100억", _AMOUNT_SPEC) == ("scalar", 10000000000)
        assert check_conflicts._group_key("１００억", _AMOUNT_SPEC) == ("raw", "１００억")

    def test_full_width_and_ascii_twin_conflict(self):
        # The same value in two spellings is now two values -> CONFLICT (exit 1).
        facts = [
            _fact("갑사", "매출", "100억"),
            _fact("갑사", "매출", "１００억"),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"매출"}, _TYPED_AMOUNT)
        # Reported values keep the original object strings (provenance), so a
        # reviewer can see which row carries the full-width digits.
        assert conflicts[("갑사", "매출")] == ["100억", "１００억"]

    def test_full_width_date_and_ascii_twin_conflict(self):
        facts = [
            _fact("기서비스", "출시", "2030.1"),
            _fact("기서비스", "출시", "２０３０.１"),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"출시"}, _TYPED_DATE)
        assert conflicts[("기서비스", "출시")] == ["2030.1", "２０３０.１"]

    def test_full_width_alone_is_not_a_conflict(self):
        # Degrading to raw must not invent a conflict out of one repeated value:
        # only a KB that carries BOTH spellings flips from green to red.
        facts = [
            _fact("갑사", "매출", "１００억"),
            _fact("갑사", "매출", "１００억"),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"매출"}, _TYPED_AMOUNT)
        assert conflicts == {}


class TestMultiValuedNotFlagged:
    def test_relation_not_single_valued_never_conflicts(self):
        # Not declared single-valued -> ignored entirely, even with typed spec.
        facts = [
            _fact("갑봇", "구성_요소", "ToolA"),
            _fact("갑봇", "구성_요소", "을서비스"),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, set(), _TYPED_AMOUNT)
        assert conflicts == {}


class TestNfdRelationName:
    """#210: a relation name written in NFD (macOS decomposed jamo) must still
    reach its NFC-keyed typed spec, so equivalent notations collapse. ``typed``
    dicts are NFC-keyed (typed_relations normalizes); facts / single-valued names
    are verbatim (NFD here). detect_conflicts must NFC the lookup."""

    # '매출' decomposed to NFD; distinct bytes from the NFC key in _TYPED_AMOUNT.
    _NFD_REL = unicodedata.normalize("NFD", "매출")

    def test_nfd_relation_equivalent_notation_not_flagged(self):
        # Regression guard: fails before the fix (typed.get(NFD) -> None -> raw
        # fallback -> two raw strings -> false CONFLICT).
        assert self._NFD_REL != "매출"  # sanity: genuinely NFD
        facts = [
            _fact("갑사", self._NFD_REL, 'amount(5400,"억")'),
            _fact("갑사", self._NFD_REL, 'amount(0.54,"조")'),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {self._NFD_REL}, _TYPED_AMOUNT)
        assert conflicts == {}

    def test_nfd_relation_real_value_difference_still_conflicts(self):
        # A genuine difference must still fire, NFD or not; and the reported
        # relation key preserves the original (NFD) form (provenance).
        facts = [
            _fact("갑사", self._NFD_REL, 'amount(5000,"억")'),
            _fact("갑사", self._NFD_REL, 'amount(5400,"억")'),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {self._NFD_REL}, _TYPED_AMOUNT)
        assert list(conflicts) == [("갑사", self._NFD_REL)]
        assert conflicts[("갑사", self._NFD_REL)] == ['amount(5000,"억")', 'amount(5400,"억")']


class TestOrdinalRankOnly:
    """#218 / #224 A: ordinal is a *rank-only* contract. ``parse_ordinal`` keeps
    only the integer rank and drops the ordinal-class unit (호/위/번/차/등/째), so
    cross-unit notations of the same rank collapse onto one value — consistent with
    the engine, which also compares ordinals on rank alone. This pins that
    by-design collapse so a future 'unit-aware grouping' change (which would
    diverge from the engine) fails loudly here."""

    _TYPED_ORDINAL = {"순위": common.TypedRelSpec("ordinal", "rank")}

    def test_cross_unit_same_rank_collapses_to_one_value(self):
        # 제3호 and 3위 both normalize to rank 3 -> a single value, NOT a conflict.
        facts = [
            _fact("갑", "순위", "제3호"),
            _fact("갑", "순위", "3위"),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"순위"}, self._TYPED_ORDINAL)
        assert conflicts == {}

    def test_distinct_ranks_still_conflict(self):
        # A genuine rank difference (3 vs 5) must still fire — the rank-only
        # contract collapses units, not values.
        facts = [
            _fact("갑", "순위", "제3호"),
            _fact("갑", "순위", "5위"),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"순위"}, self._TYPED_ORDINAL)
        assert list(conflicts) == [("갑", "순위")]
        # provenance: original notations preserved, one representative per rank,
        # sorted (ASCII '5' < Hangul '제').
        assert conflicts[("갑", "순위")] == ["5위", "제3호"]


class TestYearOnlyDateDegrade:
    """#224 B2: a bare year ``2030`` has no month, so ``parse_date`` returns None
    and the object degrades to the raw-string key ``("raw","2030")``. A scalar
    date (``2030.1`` -> 20300101) keys as ``("scalar", 20300101)``. The two keys
    are distinct, so the pair fires a real CONFLICT — pinning that a bare year is
    NOT silently treated as a parseable date."""

    def test_bare_year_degrades_and_conflicts_with_scalar_date(self):
        facts = [
            _fact("기서비스", "출시", "2030"),
            _fact("기서비스", "출시", "2030.1"),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"출시"}, _TYPED_DATE)
        assert list(conflicts) == [("기서비스", "출시")]
        # provenance: both original strings preserved as representatives.
        assert conflicts[("기서비스", "출시")] == ["2030", "2030.1"]


class TestCustomUnitTableParsed:
    """#224 B1: the custom unit table declared in ``policy/typed-relations.md``
    must flow through the REAL parser (``common._parse_typed_relations``) into
    ``spec.units`` and then into ``_group_key``, so equivalent amounts under a
    custom table collapse. Prior tests only used hand-built specs; this pins the
    parse -> units -> grouping path. (The shell test covers the full KB path.)"""

    def test_custom_units_equivalent_amounts_collapse(self):
        # 달러=1300, 센트=13: amount(2,"달러") == amount(200,"센트") == 2600.
        text = '- `보상` : amount as reward (달러=1300, 센트=13)\n'
        specs = common._parse_typed_relations(text)
        assert specs["보상"].units == {"달러": 1300, "센트": 13}
        facts = [
            _fact("갑", "보상", 'amount(2,"달러")'),
            _fact("갑", "보상", 'amount(200,"센트")'),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"보상"}, specs)
        assert conflicts == {}

    def test_custom_units_distinct_amounts_conflict(self):
        # 2달러 (2600) vs 3달러 (3900) -> a real difference under the custom table.
        text = '- `보상` : amount as reward (달러=1300, 센트=13)\n'
        specs = common._parse_typed_relations(text)
        facts = [
            _fact("갑", "보상", 'amount(2,"달러")'),
            _fact("갑", "보상", 'amount(3,"달러")'),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"보상"}, specs)
        assert conflicts[("갑", "보상")] == ['amount(2,"달러")', 'amount(3,"달러")']


class TestSupersededIgnored:
    def test_superseded_row_excluded_from_conflict(self):
        # engine_facts drops non-engine statuses -> a superseded row can't fire.
        facts = [
            _fact("갑사", "매출", 'amount(5000,"억")', status="superseded"),
            _fact("갑사", "매출", 'amount(5400,"억")'),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"매출"}, _TYPED_AMOUNT)
        assert conflicts == {}
