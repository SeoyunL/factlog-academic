# SPDX-License-Identifier: Apache-2.0
"""Regression tests for run_logic_check query evaluation (#99).

A comma inside a quoted object literal must not be split into extra args.
With the old naive ``split(",")`` parser these queries produced 0 rows even
though the fact exists; after delegating to common's string-aware parser they
resolve correctly.
"""
from __future__ import annotations

import run_logic_check as rlc


def _fact(subject, relation, object_):
    return {"subject": subject, "relation": relation, "object": object_}


class TestRelationResultsCommaLiteral:
    def test_object_with_comma_matches(self):
        facts = [_fact("A", "born_in", "Paris, France")]
        rows = rlc.relation_results('relation("A", "born_in", "Paris, France")?', facts)
        assert rows == [("A", "born_in", "Paris, France")]

    def test_object_with_comma_does_not_match_different_value(self):
        facts = [_fact("A", "born_in", "Paris, France")]
        rows = rlc.relation_results('relation("A", "born_in", "Lyon, France")?', facts)
        assert rows == []

    def test_variable_object_binds_comma_value(self):
        facts = [_fact("A", "born_in", "Paris, France")]
        rows = rlc.relation_results('relation("A", "born_in", O)?', facts)
        assert rows == [("A", "born_in", "Paris, France")]

    def test_plain_three_arg_still_works(self):
        facts = [_fact("A", "knows", "B")]
        rows = rlc.relation_results('relation("A", "knows", "B")?', facts)
        assert rows == [("A", "knows", "B")]


class TestPredicateExactDispatch:
    """evaluate_queries must dispatch on the exact predicate, not a prefix.

    The branches used to select an evaluator with ``line.startswith("relation")``
    and friends. When a query predicate is only a *prefix* of the line's actual
    predicate (``relationship`` vs ``relation``, ``pathway`` vs ``path``), the
    startswith test drew the query into the wrong branch and printed a bogus
    VERIFIED-looking answer for a predicate validate_query already rejects as
    ``query unknown predicate``. Matching the predicate token exactly keeps those
    lines out of every evaluation branch, leaving the Errors section to speak.
    """

    def _run(self, monkeypatch, line, facts=None, inferred=None):
        monkeypatch.setattr(rlc, "query_lines", lambda: [line])
        return rlc.evaluate_queries(facts or [], inferred or {}, set())

    def test_relationship_does_not_enter_relation_branch(self, monkeypatch):
        facts = [_fact("A", "knows", "B")]
        assert self._run(monkeypatch, 'relationship("A", "knows", "B")?', facts) == []

    def test_pathway_does_not_enter_path_branch(self, monkeypatch):
        assert self._run(monkeypatch, 'pathway("A", "B")?', inferred={"path": {("A", "B")}}) == []

    def test_counter_does_not_enter_count_branch(self, monkeypatch):
        facts = [_fact("A", "knows", "B")]
        assert self._run(monkeypatch, 'counter("A", "knows")?', facts) == []

    def test_review_required_prefix_does_not_enter_review_branch(self, monkeypatch):
        assert self._run(monkeypatch, 'review_required_extra("q")?') == []

    def test_exact_predicates_still_evaluate(self, monkeypatch):
        facts = [_fact("A", "knows", "B")]
        inferred = {"path": {("A", "B")}}
        assert self._run(monkeypatch, 'relation("A", "knows", "B")?', facts) == [
            "relation results: 1 rows; A, knows, B"
        ]
        assert self._run(monkeypatch, 'path("A", "B")?', facts, inferred) == ["path A -> B: A -> B"]
        assert self._run(monkeypatch, 'count("A", "knows")?', facts) == [
            "count results: 1 (distinct objects)"
        ]
        assert self._run(monkeypatch, 'review_required("q")?') == ["review_required: q"]
