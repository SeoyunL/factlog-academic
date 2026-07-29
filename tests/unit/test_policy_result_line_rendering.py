# SPDX-License-Identifier: Apache-2.0
"""Regression tests: what a policy query result line may claim (#326).

Two rendering defects that survive the quoted-constant filter:

1. An UNPARSEABLE query (no trailing '?', say) makes ``query_args`` return [].
   With no args, no constant is pinned, so the filter passes every row and the
   line answers with the predicate's whole extent — for a query the report is
   simultaneously rejecting in its own Errors section ('query must end with ?').
   An error plus a fabricated full-extent answer is worse than the error alone.

2. The filtered result line and the "Policy evaluation:" extent line sit a few
   lines apart in the same report and now legitimately disagree (3 rows there,
   0 rows here). Echoing the query is what makes that pair readable as scope
   rather than contradiction; the extent line is deliberately left alone (it is
   pinned by tests/golden/logic_report.txt).
"""
from __future__ import annotations

import run_logic_check as rlc

PREDICATE = "needs_review"
INFERRED = {
    PREDICATE: {
        ("Alice", "low_conf"),
        ("Carol", "stale"),
        ("Dave", "no_source"),
    }
}


class TestUnparseableQueryEmitsNoResultLine:
    def _evaluate(self, monkeypatch, line):
        monkeypatch.setattr(rlc, "query_lines", lambda: [line])
        return rlc.evaluate_queries([], INFERRED, {PREDICATE})

    def test_missing_question_mark_produces_no_result(self, monkeypatch):
        assert self._evaluate(monkeypatch, f'{PREDICATE}("Bob", R)') == []

    def test_bare_predicate_produces_no_result(self, monkeypatch):
        assert self._evaluate(monkeypatch, PREDICATE) == []

    def test_unparseable_line_returns_none_not_a_full_extent_string(self, monkeypatch):
        assert rlc.policy_result_line(PREDICATE, f'{PREDICATE}("Bob", R)', INFERRED) is None

    def test_wellformed_query_still_produces_a_result(self, monkeypatch):
        results = self._evaluate(monkeypatch, f'{PREDICATE}("Alice", R)?')
        assert len(results) == 1
        assert "1 rows" in results[0]


class TestResultLineNamesTheQueryItAnswers:
    def test_variable_only_query_text_is_byte_identical_to_before_the_fix(self):
        # The fix promised not to change the output of a variable-only query.
        # This is the literal upstream/main (c6d359d) rendering of this input,
        # so the echo must not reach it: a variable-only query reports the extent,
        # which is exactly what the 'Policy evaluation:' line says, so it cannot
        # produce the mismatch the echo exists to explain.
        line = rlc.policy_result_line(PREDICATE, f"{PREDICATE}(E, R)?", INFERRED)
        assert line == (
            "needs_review results: 3 rows; "
            "E=Alice, R=low_conf; E=Carol, R=stale; E=Dave, R=no_source"
        )
        assert "query:" not in line

    def test_result_line_echoes_the_query(self):
        draft = f'{PREDICATE}("Bob", R)?'
        line = rlc.policy_result_line(PREDICATE, draft, INFERRED)
        # Without the echo, '0 rows' here reads as a contradiction of the
        # 'needs_review: 3 rows' extent line printed just above it.
        assert draft in line
        assert "0 rows" in line

    def test_echo_distinguishes_two_queries_on_the_same_predicate(self, monkeypatch):
        monkeypatch.setattr(
            rlc,
            "query_lines",
            lambda: [f'{PREDICATE}("Alice", R)?', f'{PREDICATE}("Bob", R)?'],
        )
        first, second = rlc.evaluate_queries([], INFERRED, {PREDICATE})
        assert "Alice" in first and "1 rows" in first
        assert "Bob" in second and "0 rows" in second
        assert first != second
