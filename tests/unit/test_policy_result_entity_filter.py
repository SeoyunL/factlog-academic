# SPDX-License-Identifier: Apache-2.0
"""A pinned entity constrains a policy query's rows (#213 report/router parity).

``ask_router.evaluate`` filters ``inferred[predicate]`` on ``row[0]`` when the query
pins a quoted entity, so ``needs_review("Alice", R)?`` answers with Alice's rows.
``policy_result_line`` prints every row of the predicate's extent regardless, so the
report answered the same question with every subject's rows — and for an entity with no
rows at all it reported the full extent as that entity's, a fabricated positive about a
named subject.

The binding loop makes it unreadable as well as wrong: quoted args are skipped when
rendering, so the entity column is dropped and the reader sees a list of reasons with
nothing to attribute them to.
"""
from __future__ import annotations

import run_logic_check as rlc


_INFERRED = {
    "needs_review": {
        ("Alice", "low_conf"),
        ("Carol", "stale"),
        ("Dave", "no_source"),
    }
}


class TestPinnedEntityFiltersRows:
    def test_only_the_pinned_entitys_rows_are_counted(self):
        line = rlc.policy_result_line(
            "needs_review", 'needs_review("Alice", R)?', _INFERRED
        )
        assert "1 rows" in line or "1 row" in line, line

    def test_other_entities_reasons_are_not_attributed_to_the_pinned_one(self):
        line = rlc.policy_result_line(
            "needs_review", 'needs_review("Alice", R)?', _INFERRED
        )
        assert "stale" not in line, f"Carol's reason attributed to Alice: {line}"
        assert "no_source" not in line, f"Dave's reason attributed to Alice: {line}"
        assert "low_conf" in line, line

    def test_entity_with_no_rows_is_a_verified_zero(self):
        """The sharpest form: Bob has no rows, so the report must not claim three."""
        line = rlc.policy_result_line(
            "needs_review", 'needs_review("Bob", R)?', _INFERRED
        )
        assert "0 rows" in line, f"fabricated rows for an entity with none: {line}"

    def test_free_entity_still_sees_the_whole_extent(self):
        line = rlc.policy_result_line(
            "needs_review", "needs_review(X, R)?", _INFERRED
        )
        assert "3 rows" in line, line


class TestReportAgreesWithRouter:
    """The oracle is `ask_router.evaluate` ITSELF, run over the same extent, not a
    hand-copy of the filter it applies. `run_wirelog` and `_policy_program_optional`
    are module-level names (tools/ask_router.py), so monkeypatching them lets the
    real `evaluate` run the real `policy_row_matches` on our fixture extent. The old
    copy pinned a snapshot of the pre-#320 raw semantics, so it could not catch the
    router drifting away from the report — a parity test that watched nothing (#346).
    """

    def _router_rows(self, monkeypatch, predicate, line):
        import ask_router

        # A .decl is all `policy_predicates` needs to route the draft to the policy
        # branch; the extent comes from the patched engine, not a compiled program.
        program = f".decl {predicate}(e: symbol, r: symbol)\n"
        monkeypatch.setattr(ask_router, "_policy_program_optional", lambda: program)
        monkeypatch.setattr(
            ask_router, "run_wirelog", lambda: {predicate: set(_INFERRED.get(predicate, set()))}
        )
        return ask_router.evaluate(line, [])["rows"]

    def test_row_counts_match_for_a_pinned_entity(self, monkeypatch):
        line = 'needs_review("Alice", R)?'
        expected = len(self._router_rows(monkeypatch, "needs_review", line))
        rendered = rlc.policy_result_line("needs_review", line, _INFERRED)
        assert f"{expected} rows" in rendered, (
            f"report/router divergence: router={expected}, report={rendered!r}"
        )

    def test_row_counts_match_for_an_absent_entity(self, monkeypatch):
        line = 'needs_review("Bob", R)?'
        expected = len(self._router_rows(monkeypatch, "needs_review", line))
        rendered = rlc.policy_result_line("needs_review", line, _INFERRED)
        assert f"{expected} rows" in rendered, (
            f"report/router divergence: router={expected}, report={rendered!r}"
        )
