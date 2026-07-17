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
    def _router_rows(self, predicate, line):
        """The filter ask_router.evaluate applies (tools/ask_router.py:423)."""
        from common import arg_value, is_quoted_string
        from common import _query_args as query_args

        args = query_args(line)
        rows = []
        for row in sorted(_INFERRED.get(predicate, set())):
            if args and is_quoted_string(args[0]) and (not row or arg_value(args[0]) != row[0]):
                continue
            rows.append(list(row))
        return rows

    def test_row_counts_match_for_a_pinned_entity(self):
        line = 'needs_review("Alice", R)?'
        expected = len(self._router_rows("needs_review", line))
        rendered = rlc.policy_result_line("needs_review", line, _INFERRED)
        assert f"{expected} rows" in rendered, (
            f"report/router divergence: router={expected}, report={rendered!r}"
        )

    def test_row_counts_match_for_an_absent_entity(self):
        line = 'needs_review("Bob", R)?'
        expected = len(self._router_rows("needs_review", line))
        rendered = rlc.policy_result_line("needs_review", line, _INFERRED)
        assert f"{expected} rows" in rendered, (
            f"report/router divergence: router={expected}, report={rendered!r}"
        )
