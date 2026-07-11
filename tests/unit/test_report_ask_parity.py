# SPDX-License-Identifier: Apache-2.0
"""The report and ask must resolve a relation query identically (#213).

`run_logic_check.relation_results` (the verifiable report) and
`ask_router.evaluate_relation` (`/factlog ask`) are two paths to the same answer.
They used to canonicalise differently — the report compared all three positions as
raw strings — so the same question got two answers:

* a relation ALIAS: rows store the surface variant, the query names the canonical.
  ask returned the rows; the report returned nothing. Declaring an alias made
  facts VANISH from the verification report.
* an `amount` literal: `amount(100,억)` against the stored `amount(100,"억")`.
  Same split.

Two verification paths disagreeing is worse than either being wrong: nothing tells
you which to believe. These tests drive both functions over one fact set so the
canonicalisation cannot drift apart again.
"""
from __future__ import annotations

import ask_router
import pytest
import run_logic_check as rlc


@pytest.fixture
def kb(tmp_path, monkeypatch):
    """A KB whose policy both modules will read (they resolve POLICY_DIR lazily)."""
    import factlog.common as fc

    (tmp_path / "policy").mkdir()
    monkeypatch.setattr(fc, "POLICY_DIR", tmp_path / "policy")
    return tmp_path


def _row(subject, relation, object_):
    return {"subject": subject, "relation": relation, "object": object_}


def _both(query, facts):
    """(report rows, ask rows) as comparable subject/relation/object triples."""
    report = [tuple(r) for r in rlc.relation_results(query, facts)]
    ask = [tuple(r) for r in ask_router.evaluate_relation(query, facts)]
    return report, ask


class TestRelationAlias:
    def test_a_canonical_query_matches_surface_variant_rows_in_both(self, kb):
        (kb / "policy" / "relation-aliases.md").write_text(
            "- `연구 유형` -> `연구유형`\n", encoding="utf-8"
        )
        facts = [_row("P1", "연구 유형", "관찰연구")]
        report, ask = _both('relation(P, "연구유형", "관찰연구")?', facts)
        assert report == ask
        assert len(report) == 1  # and it is not the empty answer

    def test_a_surface_variant_query_still_works_in_both(self, kb):
        (kb / "policy" / "relation-aliases.md").write_text(
            "- `연구 유형` -> `연구유형`\n", encoding="utf-8"
        )
        facts = [_row("P1", "연구 유형", "관찰연구")]
        report, ask = _both('relation(P, "연구 유형", "관찰연구")?', facts)
        assert report == ask == [("P1", "연구 유형", "관찰연구")]

    def test_an_unrelated_relation_still_does_not_match(self, kb):
        (kb / "policy" / "relation-aliases.md").write_text(
            "- `연구 유형` -> `연구유형`\n", encoding="utf-8"
        )
        facts = [_row("P1", "연구 유형", "관찰연구")]
        report, ask = _both('relation(P, "혈액형", "관찰연구")?', facts)
        assert report == ask == []


class TestAmountLiteral:
    def test_an_unquoted_unit_matches_the_stored_quoted_form_in_both(self, kb):
        facts = [_row("갑사", "누적_투자액", 'amount(100,"억")')]
        report, ask = _both('relation("갑사", "누적_투자액", "amount(100,억)")?', facts)
        assert report == ask
        assert len(report) == 1

    def test_a_different_amount_matches_neither(self, kb):
        facts = [_row("갑사", "누적_투자액", 'amount(100,"억")')]
        report, ask = _both('relation("갑사", "누적_투자액", "amount(200,억)")?', facts)
        assert report == ask == []


class TestHierarchyParity:
    def test_subsumption_agrees_across_both_paths(self, kb):
        (kb / "policy" / "value-hierarchy.md").write_text(
            "- 연구유형: 코호트연구 ⊂ 관찰연구\n", encoding="utf-8"
        )
        facts = [_row("P1", "연구유형", "코호트연구")]
        report, ask = _both('relation(P, "연구유형", "관찰연구")?', facts)
        assert report == ask == [("P1", "연구유형", "코호트연구")]

    def test_subsumption_stays_one_way_in_both(self, kb):
        (kb / "policy" / "value-hierarchy.md").write_text(
            "- 연구유형: 코호트연구 ⊂ 관찰연구\n", encoding="utf-8"
        )
        facts = [_row("P1", "연구유형", "관찰연구")]
        report, ask = _both('relation(P, "연구유형", "코호트연구")?', facts)
        assert report == ask == []


class TestPlainQueriesUnchanged:
    """A KB with no aliases and no hierarchy must behave exactly as before."""

    def test_exact_match(self, kb):
        facts = [_row("A", "knows", "B"), _row("A", "knows", "C")]
        report, ask = _both('relation("A", "knows", "B")?', facts)
        assert report == ask == [("A", "knows", "B")]

    def test_variable_object_binds_every_row(self, kb):
        facts = [_row("A", "knows", "B"), _row("A", "knows", "C")]
        report, ask = _both('relation("A", "knows", O)?', facts)
        assert report == ask
        assert len(report) == 2

    def test_object_with_a_comma_is_not_split(self, kb):
        facts = [_row("A", "born_in", "Paris, France")]
        report, ask = _both('relation("A", "born_in", "Paris, France")?', facts)
        assert report == ask == [("A", "born_in", "Paris, France")]


class TestCountParity:
    """The count branch sat right beside the one that was fixed, and diverged too.

    In an aliased KB the report answered "0 distinct objects" to a question ask
    answered "2" — on the same facts. A count is a relation query with a free
    object, so it now goes through the same shared predicate.
    """

    @staticmethod
    def _count(query, facts):
        # The report's count branch and ask's both reduce to the shared predicate
        # with a free object; drive it the same way and compare.
        import common

        aliases, hierarchy = common.relation_aliases(), common.value_hierarchy()
        args = common._query_args(query)
        report_objects = {
            row["object"]
            for row in facts
            if common.relation_row_matches([args[0], args[1], "O"], row, aliases, hierarchy)
        }
        ask = ask_router.evaluate(query, facts)
        return len(report_objects), ask["count"]

    def test_count_agrees_in_an_aliased_kb(self, kb):
        (kb / "policy" / "relation-aliases.md").write_text(
            "- `연구 유형` -> `연구유형`\n", encoding="utf-8"
        )
        facts = [
            _row("P1", "연구 유형", "관찰연구"),
            _row("P1", "연구 유형", "코호트연구"),
        ]
        report, ask = self._count('count("P1", "연구유형")?', facts)
        assert report == ask == 2

    def test_count_agrees_without_aliases(self, kb):
        facts = [_row("P1", "knows", "B"), _row("P1", "knows", "C")]
        report, ask = self._count('count("P1", "knows")?', facts)
        assert report == ask == 2


class TestVariableRelationWithAlias:
    def test_a_variable_relation_keeps_subsumption_in_an_aliased_kb(self, kb):
        # The alias fallback for a variable-relation query was uncovered: reverting
        # it left every test green.
        (kb / "policy" / "relation-aliases.md").write_text(
            "- `연구 유형` -> `연구유형`\n", encoding="utf-8"
        )
        (kb / "policy" / "value-hierarchy.md").write_text(
            "- 연구유형: 코호트연구 ⊂ 관찰연구\n", encoding="utf-8"
        )
        facts = [_row("P2", "연구 유형", "코호트연구")]
        report, ask = _both('relation(P, R, "관찰연구")?', facts)
        assert report == ask == [("P2", "연구 유형", "코호트연구")]
