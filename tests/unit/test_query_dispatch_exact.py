# SPDX-License-Identifier: Apache-2.0
"""evaluate_queries dispatches on the exact predicate, like validate_query (#294).

The old dispatch chose a branch by ``line.startswith("relation")`` etc., while
``validate_query`` keys off ``line.split("(", 1)[0]``. So ``relationship(...)?``
entered the ``relation`` branch — a line the gate calls ``query unknown
predicate`` — and (post-#284) drew "relation query malformed", a Query
evaluation line pointing at the wrong Errors diagnosis. This pins the two on the
same predicate test and pins the one deliberate silence: ``conflict`` is a known
query predicate with no evaluation branch, so it must stay unlabelled.
"""
from __future__ import annotations

import run_logic_check as rlc


def _fact(subject, relation, object_):
    return {"subject": subject, "relation": relation, "object": object_}


def _evaluate(monkeypatch, queries, facts=None, inferred=None, policy=None):
    monkeypatch.setattr(rlc, "query_lines", lambda: queries)
    return rlc.evaluate_queries(
        facts if facts is not None else [],
        inferred if inferred is not None else {"path": set()},
        policy if policy is not None else set(),
        hierarchy={},
    )


class TestUnknownPredicateIsNotMisdispatched:
    def test_relationship_does_not_enter_relation_branch(self, monkeypatch):
        results = _evaluate(monkeypatch, ['relationship("A", "B", "C")?'])
        assert not any("relation query malformed" in line for line in results)
        assert not any("relation results" in line for line in results)
        assert "unknown query predicate — see Errors above" in results

    def test_counts_does_not_enter_count_branch(self, monkeypatch):
        results = _evaluate(monkeypatch, ['counts("A", "B")?'])
        assert not any("count results" in line for line in results)
        assert "unknown query predicate — see Errors above" in results

    def test_a_prefix_of_path_is_unknown(self, monkeypatch):
        # `pathological` starts with `path` — the exact reason startswith was wrong.
        results = _evaluate(monkeypatch, ['pathological("A", "B")?'])
        assert not any("path" in line and "->" in line for line in results)
        assert not any("path results" in line for line in results)
        assert "unknown query predicate — see Errors above" in results


class TestConflictIsUnknownPredicate:
    """#306 removed `conflict` from QUERY_PREDICATES: it is a policy-derived
    predicate, not a static one, and with no evaluation branch it used to slip
    through validate_query (errors: 0) while producing no result line — a lone
    `conflict(...)?` then drew the report's "none produced a result — see Errors
    above" fallback pointing at an empty Errors section. Now an undeclared
    `conflict` is an unknown predicate on both pipelines, so evaluate_queries
    renders the same unknown line it does for any other unknown predicate. (This
    supersedes #294's TestConflictStaysSilent, which pinned the buggy silence.)"""

    def test_conflict_is_flagged_unknown(self, monkeypatch):
        results = _evaluate(monkeypatch, ['conflict("A", "B")?'])
        assert "unknown query predicate — see Errors above" in results


class TestWellFormedRenderUnchanged:
    def test_relation_render(self, monkeypatch):
        facts = [_fact("A", "uses", "B")]
        results = _evaluate(monkeypatch, ['relation("A", "uses", "B")?'], facts=facts)
        assert "relation results: 1 rows; A, uses, B" in results

    def test_relation_empty_is_verified_zero(self, monkeypatch):
        # ALL THREE constants are accepted vocabulary — "A"/"uses" from the first
        # fact, "B" as an object of the second — so only this exact triple is absent:
        # a verified negative, "0 rows". The gate agrees (fact_absent). #350 extends
        # the unverified render to the object axis too, so "B" must itself appear or
        # the empty result is unverified (a subject/relation/object outside the
        # accepted vocabulary), a different axis than this verified zero.
        results = _evaluate(
            monkeypatch,
            ['relation("A", "uses", "B")?'],
            facts=[_fact("A", "uses", "C"), _fact("D", "made", "B")],
        )
        assert "relation results: 0 rows" in results

    def test_path_render(self, monkeypatch):
        # Both nodes accepted, no path between them — the verified negative. Since
        # #366 a path node is vocabulary-checked like a subject, so an empty KB would
        # render "unverified" here for the same reason the relation test above needs
        # its constants to appear in the facts.
        results = _evaluate(
            monkeypatch,
            ['path("A", "C")?'],
            facts=[_fact("A", "uses", "B"), _fact("C", "uses", "D")],
        )
        assert "path A -> C: (not found)" in results

    def test_count_render(self, monkeypatch):
        facts = [_fact("A", "uses", "B")]
        results = _evaluate(monkeypatch, ['count("A", "uses")?'], facts=facts)
        assert "count results: 1 (distinct objects)" in results

    def test_review_required_render(self, monkeypatch):
        results = _evaluate(monkeypatch, ['review_required("원 질문")?'])
        assert "review_required: 원 질문" in results

    def test_policy_predicate_render(self, monkeypatch):
        results = _evaluate(
            monkeypatch,
            ['retracted("논문A", "reason")?'],
            inferred={"path": set(), "retracted": {("논문A", "reason")}},
            policy={"retracted"},
        )
        assert any(line.startswith("retracted results: 1 rows") for line in results)
