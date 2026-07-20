# SPDX-License-Identifier: Apache-2.0
"""The logic report must not answer a malformed query with a verified negative (#284).

`evaluate_queries` used to hand a malformed relation/path query straight to the
matcher, which treats a bare token as a wildcard and a wrong-arity query as no
match, so the report printed "relation results: 0 rows" / "path results: 0 rows".
That reads as a VERIFIED NEGATIVE — the engine looked and found nothing — for a
query `validate_query` (and the `ask` gate) reject as malformed. The report must
instead say the query is broken and point the reader at the Errors section.

The malformed criterion is pinned to `validate_query`'s relation (arity 3, every
arg a variable or quoted constant) and path (arity 2, same arg rule) branches so
the two cannot drift.
"""
from __future__ import annotations

import run_logic_check as rlc


def _fact(subject, relation, object_):
    return {"subject": subject, "relation": relation, "object": object_}


def _evaluate(monkeypatch, queries, facts=None):
    monkeypatch.setattr(rlc, "query_lines", lambda: queries)
    return rlc.evaluate_queries(
        facts if facts is not None else [],
        {"path": set()},
        set(),
        hierarchy={},
    )


class TestMalformedRelationQuery:
    def test_two_arg_relation_is_not_a_verified_negative(self, monkeypatch):
        results = _evaluate(monkeypatch, ['relation("A", "uses")?'])
        assert not any("0 rows" in line for line in results)
        assert "relation query malformed — see Errors above" in results

    def test_bare_token_relation_is_not_a_verified_negative(self, monkeypatch):
        # `uses` is a bare lower-case token: not a variable (variables match
        # [A-Z_]...) and not a quoted constant. The matcher would treat it as a
        # wildcard and report a verified "0 rows" for a malformed query.
        results = _evaluate(monkeypatch, ["relation(A, uses, B)?"])
        assert not any("0 rows" in line for line in results)
        assert "relation query malformed — see Errors above" in results


class TestMalformedPathQuery:
    def test_one_arg_path_is_not_a_verified_negative(self, monkeypatch):
        results = _evaluate(monkeypatch, ['path("A")?'])
        assert not any("path results: 0 rows" in line for line in results)
        assert "path query malformed — see Errors above" in results

    def test_bare_token_path_is_not_a_verified_negative(self, monkeypatch):
        # Lower-case bare tokens: correct arity (2) but neither variable nor quoted,
        # so still malformed by validate_query's path rule.
        results = _evaluate(monkeypatch, ["path(a, b)?"])
        assert not any("path results: 0 rows" in line for line in results)
        assert "path query malformed — see Errors above" in results


class TestWellFormedUnsatisfiedIsStillAnswered:
    """No regression: a well-formed query that simply has no result keeps printing
    its verified negative. The guard must fire on malformed input only."""

    def test_relation_with_no_match_keeps_zero_rows(self, monkeypatch):
        # All three constants are accepted vocabulary — "A"/"uses" from the first
        # fact, "B" as an object of the second — only this exact triple absent: a
        # verified negative, "0 rows". An unaccepted constant (subject, relation OR,
        # since #350, object) renders "unverified" instead — a different axis than the
        # malformed guard here.
        results = _evaluate(
            monkeypatch,
            ['relation("A", "uses", "B")?'],
            facts=[_fact("A", "uses", "C"), _fact("D", "made", "B")],
        )
        assert "relation results: 0 rows" in results
        assert "relation query malformed — see Errors above" not in results

    def test_relation_variable_object_still_binds(self, monkeypatch):
        facts = [_fact("A", "uses", "B")]
        results = _evaluate(monkeypatch, ['relation("A", "uses", O)?'], facts=facts)
        assert any(line.startswith("relation results: 1 rows") for line in results)

    def test_path_not_found_is_preserved(self, monkeypatch):
        # BOTH nodes are accepted entities and no path joins them: the verified
        # negative "(not found)" is for. The facts matter since #366 — over an empty
        # KB "A" is not accepted vocabulary at all, and the empty extent is then
        # unverified rather than a checked "no", the same axis as the relation test
        # above.
        results = _evaluate(
            monkeypatch,
            ['path("A", "C")?'],
            facts=[_fact("A", "uses", "B"), _fact("C", "uses", "D")],
        )
        assert "path A -> C: (not found)" in results
        assert "path query malformed — see Errors above" not in results
