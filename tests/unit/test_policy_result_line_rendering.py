# SPDX-License-Identifier: Apache-2.0
"""Regression tests: what a policy query result line may claim (#326).

Two rendering defects that survive the quoted-constant filter:

1. A MALFORMED query still got an answer. No trailing '?' makes ``query_args``
   return [] and ``pred()?`` returns one empty arg — either way no constant is
   pinned, the filter passes every row, and the line reports the predicate's
   whole extent. Wrong arity (``pred("Alice")?``, ``pred(E, R, "zzz")?``) filters
   on whatever constants happen to line up with a column and reports a plausible
   but meaningless count. All four shapes are rejected in the report's own Errors
   section at the same time; an error plus a fabricated answer is worse than the
   error alone. The guard here is the arity test ``validate_query`` already uses,
   and ``ask_router.evaluate`` raises on the same shapes so neither path answers.

2. The filtered result line and the "Policy evaluation:" extent line sit a few
   lines apart in the same report and now legitimately disagree (3 rows there,
   0 rows here). Echoing the query is what makes that pair readable as scope
   rather than contradiction. The echo is limited to constant-pinned queries so a
   variable-only query keeps its original text; the extent line is deliberately
   left alone (it is pinned by tests/golden/logic_report.txt).
"""
from __future__ import annotations

import pytest

import ask_router
import run_logic_check as rlc

PREDICATE = "needs_review"
INFERRED = {
    PREDICATE: {
        ("Alice", "low_conf"),
        ("Carol", "stale"),
        ("Dave", "no_source"),
    }
}


MALFORMED = [
    f'{PREDICATE}("Bob", R)',  # no trailing '?' — query_args returns []
    PREDICATE,  # bare predicate, not an atom at all
    f"{PREDICATE}()?",  # one empty arg, not zero args
    f'{PREDICATE}("Alice")?',  # too few
    f'{PREDICATE}("Alice", R, "zzz")?',  # too many
]


class TestMalformedQueryEmitsNoResultLine:
    def _evaluate(self, monkeypatch, line):
        monkeypatch.setattr(rlc, "query_lines", lambda: [line])
        return rlc.evaluate_queries([], INFERRED, {PREDICATE})

    @pytest.mark.parametrize("line", MALFORMED)
    def test_malformed_query_produces_no_result_line(self, monkeypatch, line):
        assert self._evaluate(monkeypatch, line) == []

    @pytest.mark.parametrize("line", MALFORMED)
    def test_malformed_query_returns_none_not_a_row_count_string(self, line):
        assert rlc.policy_result_line(PREDICATE, line, INFERRED) is None

    def test_wellformed_query_still_produces_a_result(self, monkeypatch):
        results = self._evaluate(monkeypatch, f'{PREDICATE}("Alice", R)?')
        assert len(results) == 1
        assert "1 rows" in results[0]


class TestMalformedQueryParity:
    """A malformed policy query must get NO answer from either path.

    Before the guard the two paths disagreed outright on the same input: for
    ``pred("Bob", R)`` (no '?') the router returned count=3 while the report
    returned nothing, and for ``pred(E, R, "zzz")?`` both rendered '0 rows',
    which reads as a verified negative for a query classify_query rejects as
    BAD_ARITY. The router refuses by raising (cmd_evaluate turns that into error
    JSON); the report refuses by emitting no line. Neither invents a count.
    """

    @pytest.mark.parametrize("draft", MALFORMED)
    def test_router_refuses_and_report_stays_silent(self, monkeypatch, draft):
        monkeypatch.setattr(ask_router, "policy_predicates", lambda program: {PREDICATE})
        monkeypatch.setattr(ask_router, "run_wirelog", lambda: INFERRED)
        with pytest.raises(NotImplementedError):
            ask_router.evaluate(draft, [])
        assert rlc.policy_result_line(PREDICATE, draft, INFERRED) is None

    def test_wellformed_query_is_still_answered_by_both(self, monkeypatch):
        monkeypatch.setattr(ask_router, "policy_predicates", lambda program: {PREDICATE})
        monkeypatch.setattr(ask_router, "run_wirelog", lambda: INFERRED)
        draft = f'{PREDICATE}("Alice", R)?'
        assert ask_router.evaluate(draft, [])["count"] == 1
        assert "1 rows" in rlc.policy_result_line(PREDICATE, draft, INFERRED)


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
