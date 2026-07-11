# SPDX-License-Identifier: Apache-2.0
"""Value-hierarchy subsumption in object matching (#211).

A cohort study IS an observational study. Without a declared hierarchy the two
are unrelated strings, so a query for the broader value silently misses every
row filed under a narrower one — the exact quiet omission this KB exists to
prevent. Declaring the hierarchy must fix the query WITHOUT rewriting any fact:
accepted.dl stays a 1:1 projection of the accepted candidate rows.
"""
from __future__ import annotations

import common
import pytest
import run_logic_check as rlc

HIERARCHY_MD = """\
# comment line is ignored
- 연구유형: 코호트연구 ⊂ 관찰연구
- 연구유형: 단면연구 <: 관찰연구
- 대상질환: `emphysema` < COPD
"""


@pytest.fixture
def kb(tmp_path):
    """A KB root; `value_hierarchy(root=...)` reads <root>/policy/value-hierarchy.md.

    The root argument (not a monkeypatched POLICY_DIR) is what the loader is
    designed for — and `tools/common.py` only re-exports names, so patching the
    constant there would not reach the module that actually reads it.
    """
    (tmp_path / "policy").mkdir()
    return tmp_path


def _row(subject, relation, object_):
    return {"subject": subject, "relation": relation, "object": object_}


FACTS = [
    _row("P1", "연구유형", "관찰연구"),
    _row("P2", "연구유형", "코호트연구"),
    _row("P3", "연구유형", "단면연구"),
    _row("P4", "연구유형", "RCT"),
    _row("P5", "대상질환", "emphysema"),
]


class TestParse:
    def test_absent_file_is_empty(self, kb):
        assert common.value_hierarchy(kb) == {}

    def test_parses_all_three_spellings(self, kb):
        (kb / "policy" / "value-hierarchy.md").write_text(HIERARCHY_MD, encoding="utf-8")
        h = common.value_hierarchy(kb)
        assert h["연구유형"]["코호트연구"] == {"관찰연구"}
        assert h["연구유형"]["단면연구"] == {"관찰연구"}
        assert h["대상질환"]["emphysema"] == {"COPD"}

    def test_ancestors_are_transitive(self, kb):
        (kb / "policy" / "value-hierarchy.md").write_text(
            "- r: a ⊂ b\n- r: b ⊂ c\n", encoding="utf-8"
        )
        assert common.value_hierarchy(kb)["r"]["a"] == {"b", "c"}

    def test_a_cycle_is_dropped_entirely(self, kb):
        # Not merely "does not hang": every value on the cycle must GO. Keeping a
        # cycle makes subsumption mutual — a query for the narrow value returns
        # the broad one — which silently breaks the one-way contract. (The earlier
        # version of this test asserted `"a" not in h["r"]["a"]`, which the buggy
        # implementation also satisfied: it was always true and caught nothing.)
        (kb / "policy" / "value-hierarchy.md").write_text(
            "- r: a ⊂ b\n- r: b ⊂ a\n", encoding="utf-8"
        )
        assert common.value_hierarchy(kb) == {}

    def test_a_longer_cycle_is_dropped_too(self, kb):
        (kb / "policy" / "value-hierarchy.md").write_text(
            "- r: a ⊂ b\n- r: b ⊂ c\n- r: c ⊂ a\n", encoding="utf-8"
        )
        assert common.value_hierarchy(kb) == {}

    def test_a_dropped_cycle_is_reported(self, kb):
        # Dropping it silently would be its own quiet failure: the author believes
        # the declaration is in force.
        (kb / "policy" / "value-hierarchy.md").write_text(
            "- r: a ⊂ b\n- r: b ⊂ a\n", encoding="utf-8"
        )
        warnings = common.value_hierarchy_warnings(kb)
        assert any("cycle" in w for w in warnings)

    def test_an_acyclic_branch_survives_a_cycle_elsewhere(self, kb):
        (kb / "policy" / "value-hierarchy.md").write_text(
            "- r: a ⊂ b\n- r: b ⊂ a\n- r: x ⊂ y\n", encoding="utf-8"
        )
        assert common.value_hierarchy(kb) == {"r": {"x": {"y"}}}

    def test_backticks_protect_an_operator_inside_a_value(self, kb):
        # The file format promises this; the first parser split on '<' before
        # honouring the backticks and produced silent garbage keys.
        (kb / "policy" / "value-hierarchy.md").write_text("- r: `a<b` ⊂ c\n", encoding="utf-8")
        assert common.value_hierarchy(kb) == {"r": {"a<b": {"c"}}}

    def test_backticks_protect_a_colon_in_the_relation(self, kb):
        (kb / "policy" / "value-hierarchy.md").write_text("- `r:x`: a ⊂ b\n", encoding="utf-8")
        assert common.value_hierarchy(kb) == {"r:x": {"a": {"b"}}}

    def test_an_nfd_policy_file_still_matches_nfc_facts(self, kb):
        # macOS writes NFD; accepted facts are NFC. Comparing them raw made every
        # Korean declaration a silent no-op — the exact failure #211 removes.
        import unicodedata

        (kb / "policy" / "value-hierarchy.md").write_text(
            unicodedata.normalize("NFD", "- 연구유형: 코호트연구 ⊂ 관찰연구\n"), encoding="utf-8"
        )
        h = common.value_hierarchy(kb)
        row = _row("P", "연구유형", "코호트연구")
        assert common.object_matches("관찰연구", row, h, relation="연구유형")


class TestDeclarationWarnings:
    def test_a_typo_in_a_value_is_reported(self, kb):
        # A mistyped declaration does nothing, and the author has no way to know:
        # they declared the hierarchy and believe the broad query now catches the
        # narrow rows. Silence here recreates the omission the feature removes.
        (kb / "policy" / "value-hierarchy.md").write_text(
            "- 연구유형: 코호트연국 ⊂ 관찰연구\n", encoding="utf-8"
        )
        facts = [_row("P", "연구유형", "코호트연구")]
        warnings = common.value_hierarchy_warnings(kb, facts=facts)
        assert any("코호트연국" in w for w in warnings)

    def test_an_unknown_relation_is_reported(self, kb):
        (kb / "policy" / "value-hierarchy.md").write_text("- nosuch: x ⊂ y\n", encoding="utf-8")
        facts = [_row("P", "연구유형", "코호트연구")]
        assert any("nosuch" in w for w in common.value_hierarchy_warnings(kb, facts=facts))

    def test_a_correct_declaration_is_quiet(self, kb):
        (kb / "policy" / "value-hierarchy.md").write_text(
            "- 연구유형: 코호트연구 ⊂ 관찰연구\n", encoding="utf-8"
        )
        facts = [_row("P", "연구유형", "코호트연구")]
        assert common.value_hierarchy_warnings(kb, facts=facts) == []


class TestSubsumption:
    def test_broad_query_catches_narrow_rows(self, kb):
        (kb / "policy" / "value-hierarchy.md").write_text(HIERARCHY_MD, encoding="utf-8")
        h = common.value_hierarchy(kb)
        rows = rlc.relation_results('relation(P, "연구유형", "관찰연구")?', FACTS, h)
        assert {r[0] for r in rows} == {"P1", "P2", "P3"}

    def test_without_the_declaration_the_query_leaks(self, kb):
        # This is the #211 bug: the same query, no hierarchy → the two subtype
        # rows vanish. Pinned so the fix cannot be quietly reverted.
        rows = rlc.relation_results('relation(P, "연구유형", "관찰연구")?', FACTS, None)
        assert {r[0] for r in rows} == {"P1"}

    def test_subsumption_is_one_way(self, kb):
        (kb / "policy" / "value-hierarchy.md").write_text(HIERARCHY_MD, encoding="utf-8")
        h = common.value_hierarchy(kb)
        rows = rlc.relation_results('relation(P, "연구유형", "코호트연구")?', FACTS, h)
        assert {r[0] for r in rows} == {"P2"}  # NOT P1 (the broader row)

    def test_unrelated_value_is_untouched(self, kb):
        (kb / "policy" / "value-hierarchy.md").write_text(HIERARCHY_MD, encoding="utf-8")
        h = common.value_hierarchy(kb)
        rows = rlc.relation_results('relation(P, "연구유형", "RCT")?', FACTS, h)
        assert {r[0] for r in rows} == {"P4"}

    def test_hierarchy_is_scoped_to_its_relation(self, kb):
        # `emphysema ⊂ COPD` is declared for 대상질환 only; it must not leak into
        # another relation that happens to use the same value.
        (kb / "policy" / "value-hierarchy.md").write_text(HIERARCHY_MD, encoding="utf-8")
        h = common.value_hierarchy(kb)
        facts = [_row("P9", "언급질환", "emphysema")]
        assert rlc.relation_results('relation(P, "언급질환", "COPD")?', facts, h) == []

    def test_returned_rows_report_their_own_value(self, kb):
        # The row keeps its real object; subsumption widens the match, it does
        # not rewrite the fact.
        (kb / "policy" / "value-hierarchy.md").write_text(HIERARCHY_MD, encoding="utf-8")
        h = common.value_hierarchy(kb)
        rows = rlc.relation_results('relation("P2", "연구유형", "관찰연구")?', FACTS, h)
        assert rows == [("P2", "연구유형", "코호트연구")]


class TestObjectMatches:
    def test_normalizer_folds_surface_spelling(self, kb):
        h = {"r": {"child": {"PARENT"}}}
        row = _row("S", "r", "child")
        assert not common.object_matches("parent", row, h)
        assert common.object_matches("parent", row, h, str.lower)

    def test_no_hierarchy_is_exact_match(self):
        row = _row("S", "r", "child")
        assert common.object_matches("child", row, None)
        assert not common.object_matches("parent", row, None)


class TestGateScope:
    """The declaration licences a value under ITS relation, not everywhere.

    Pooling every relation's ancestors into one vocabulary made a query naming a
    declared value under an UNRELATED relation stop being "not our vocabulary"
    (route=wiki) and become a *verified negative* — the engine asserting "no such
    fact" about a term the KB never adopted there. A wrong assertion is worse than
    an honest "cannot express".
    """

    def test_ancestors_are_scoped_to_their_relation(self, kb):
        (kb / "policy" / "value-hierarchy.md").write_text(HIERARCHY_MD, encoding="utf-8")
        h = common.value_hierarchy(kb)
        assert "관찰연구" in common.declared_ancestors(h, "연구유형")
        assert "관찰연구" not in common.declared_ancestors(h, "혈액형")

    def test_a_variable_relation_sees_every_declaration(self, kb):
        # A variable relation really can range over all of them, so the wide
        # vocabulary is honest there.
        (kb / "policy" / "value-hierarchy.md").write_text(HIERARCHY_MD, encoding="utf-8")
        h = common.value_hierarchy(kb)
        assert {"관찰연구", "COPD"} <= common.declared_ancestors(h, None)

    def test_no_hierarchy_declares_nothing(self, kb):
        assert common.declared_ancestors(common.value_hierarchy(kb), "연구유형") == set()


class TestWarningsAreAliasAware:
    def test_a_declaration_on_a_canonical_name_is_not_called_ineffective(self, kb):
        # Rows store the surface variant; the declaration names the canonical. It
        # DOES take effect, so warning "no effect" would push the user to delete a
        # working declaration and bring the omission back.
        (kb / "policy" / "value-hierarchy.md").write_text(
            "- 연구유형: 코호트연구 ⊂ 관찰연구\n", encoding="utf-8"
        )
        (kb / "policy" / "relation-aliases.md").write_text(
            "- `연구 유형` -> `연구유형`\n", encoding="utf-8"
        )
        facts = [_row("P2", "연구 유형", "코호트연구")]
        assert common.value_hierarchy_warnings(kb, facts=facts) == []
