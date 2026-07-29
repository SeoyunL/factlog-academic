# SPDX-License-Identifier: Apache-2.0
"""Regression tests: a policy query's quoted constants must FILTER rows (#326).

``policy_result_line`` printed the whole extent of the policy predicate no
matter which entity the query pinned, so ``needs_review("Bob", R)?`` reported
three rows for a Bob the engine inferred NOTHING about — other subjects' reasons
rendered as Bob's. The report is the artifact SKILL.md tells the reader to show
verbatim before stating a conclusion, so a fabricated positive there is not a
cosmetic defect.

The filter must apply at EVERY argument position, not just the first: fixing
only ``args[0]`` would leave ``pred(E, "stale")?`` mis-attributing rows while
making the first-argument form accurate — which removes the only signal a reader
had that the second line is untrustworthy.

``ask_router.evaluate`` (the ``ask`` path) had the same defect at positions
other than the first. The parity class below pins the two paths together.

FACTLOG_ROOT is bound to a throwaway dir by ``tests/unit/conftest.py`` BEFORE
any tool module is imported, which matters here: ``ask_router`` re-exports
``FACTLOG_ROOT`` at import time from argv/env, so without that pin the parity
test would read the developer's real knowledge base.
"""
from __future__ import annotations

import re

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


def _row_count(report_line: str) -> int:
    """Extract N from '<pred> results ...: N rows; ...'."""
    match = re.search(r"(\d+) rows", report_line)
    assert match, f"unparseable result line: {report_line!r}"
    return int(match.group(1))


def _report_rows(draft: str, inferred=None) -> int:
    return _row_count(rlc.policy_result_line(PREDICATE, draft, inferred or INFERRED))


def _router_rows(monkeypatch, draft: str, inferred=None) -> int:
    monkeypatch.setattr(ask_router, "policy_predicates", lambda program: {PREDICATE})
    monkeypatch.setattr(ask_router, "run_wirelog", lambda: inferred or INFERRED)
    return ask_router.evaluate(draft, [])["count"]


class TestPolicyResultLineFiltersFixedEntity:
    def test_named_entity_reports_only_its_own_rows(self):
        line = rlc.policy_result_line(PREDICATE, f'{PREDICATE}("Alice", R)?', INFERRED)
        assert _row_count(line) == 1
        assert "low_conf" in line
        assert "stale" not in line and "no_source" not in line

    def test_entity_with_no_inferred_rows_reports_zero(self):
        line = rlc.policy_result_line(PREDICATE, f'{PREDICATE}("Bob", R)?', INFERRED)
        assert _row_count(line) == 0
        assert "low_conf" not in line and "stale" not in line and "no_source" not in line

    def test_second_argument_constant_also_filters(self):
        # The defect is not first-argument-specific: a reason-pinned query must
        # not report the other reasons' rows either.
        line = rlc.policy_result_line(PREDICATE, f'{PREDICATE}(E, "stale")?', INFERRED)
        assert _row_count(line) == 1
        assert "Carol" in line
        assert "Alice" not in line and "Dave" not in line

    def test_second_argument_constant_with_no_match_reports_zero(self):
        line = rlc.policy_result_line(PREDICATE, f'{PREDICATE}(E, "missing_reason")?', INFERRED)
        assert _row_count(line) == 0

    def test_both_arguments_constant_matching_row(self):
        line = rlc.policy_result_line(PREDICATE, f'{PREDICATE}("Carol", "stale")?', INFERRED)
        assert _row_count(line) == 1

    def test_both_arguments_constant_crossed_pair_reports_zero(self):
        # Carol and low_conf each exist, but not together — a per-column filter
        # that ORed the positions would wrongly report a row here.
        line = rlc.policy_result_line(PREDICATE, f'{PREDICATE}("Carol", "low_conf")?', INFERRED)
        assert _row_count(line) == 0

    def test_all_variable_query_still_reports_full_extent(self):
        line = rlc.policy_result_line(PREDICATE, f"{PREDICATE}(E, R)?", INFERRED)
        assert _row_count(line) == 3
        assert "E=Alice, R=low_conf" in line

    def test_empty_row_dropped_when_a_constant_is_pinned(self):
        # A 0-arity row cannot satisfy a pinned constant, so it is filtered out;
        # an all-variable query keeps reporting it, as before.
        inferred = {PREDICATE: {(), ("Alice", "low_conf")}}
        assert _report_rows(f'{PREDICATE}("Alice", R)?', inferred) == 1
        assert _report_rows(f"{PREDICATE}(E, R)?", inferred) == 2


class TestReportRouterParity:
    """The report and the ``ask`` router must answer a policy query identically.

    Parity is NOT correctness: two paths can agree and both be wrong, which is
    exactly the state before this fix at non-first argument positions. So every
    case asserts the ABSOLUTE row count as well as the agreement, and the router
    side calls ``ask_router.evaluate`` directly rather than re-implementing the
    filter in the test (a re-implementation would prove nothing about either
    production path).
    """

    @pytest.mark.parametrize(
        ("draft", "expected"),
        [
            (f'{PREDICATE}("Alice", R)?', 1),
            (f'{PREDICATE}("Bob", R)?', 0),
            (f'{PREDICATE}(E, "stale")?', 1),
            (f'{PREDICATE}(E, "missing_reason")?', 0),
            (f'{PREDICATE}("Carol", "stale")?', 1),
            (f'{PREDICATE}("Carol", "low_conf")?', 0),
            (f"{PREDICATE}(E, R)?", 3),
        ],
    )
    def test_report_and_router_agree_on_the_verified_row_count(self, monkeypatch, draft, expected):
        report_rows = _report_rows(draft)
        router_rows = _router_rows(monkeypatch, draft)
        assert report_rows == expected
        assert router_rows == expected
        assert report_rows == router_rows
