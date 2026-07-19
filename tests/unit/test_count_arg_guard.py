# SPDX-License-Identifier: Apache-2.0
"""A malformed `count` query is not a verified zero (#284 sibling).

``validate_query``'s ``relation`` and ``path`` branches reject an argument that is
neither a variable nor a quoted string, because the matcher treats a bare token as a
wildcard and the report would otherwise answer a malformed query with a VERIFIED
NEGATIVE. The ``count`` branch checks arity only, so ``count("A", 'rel')?`` — single
quotes are a bare token — passed the report's validator and rendered ``count results:
0 (distinct objects)`` while ``common.classify_query`` (the ask gate) rejected the same
line as ``malformed``. Zero is documented as a verified answer, so the report asserted a
count nobody verified.
"""
from __future__ import annotations

import pytest

from common import classify_query
import run_logic_check as rlc
from conftest import vocabulary


_FACTS = [
    {"subject": "Marie Curie", "relation": "born_in", "object": "Warsaw"},
    {"subject": "Marie Curie", "relation": "born_in", "object": "Poland"},
]
_VOCAB = vocabulary({"Marie Curie", "Warsaw", "Poland", "born_in"})

# Each is a count query whose arguments are neither variables nor quoted strings.
MALFORMED = [
    """count("Marie Curie", 'born_in')?""",
    "count(Marie Curie, born_in)?",
    """count("Marie Curie", born_in)?""",
]


class TestValidateQueryRejectsMalformedCountArgs:
    @pytest.mark.parametrize("line", MALFORMED)
    def test_reports_an_error(self, line):
        errors, _ = rlc.validate_query(line, _VOCAB, set())
        assert errors, f"malformed count query accepted silently: {line}"

    @pytest.mark.parametrize("line", MALFORMED)
    def test_agrees_with_the_ask_gate(self, line):
        """The report and the gate must not disagree about the same line (#213)."""
        errors, _ = rlc.validate_query(line, _VOCAB, set())
        ok, _status, _reason = classify_query(line, _FACTS, policy_program="")
        assert bool(errors) == (not ok), (
            f"report and gate disagree on {line!r}: "
            f"report errors={errors!r}, gate ok={ok}"
        )

    def test_wellformed_count_still_passes(self):
        errors, _ = rlc.validate_query(
            'count("Marie Curie", "born_in")?', _VOCAB, set()
        )
        assert errors == []


class TestEvaluateQueriesDoesNotRenderMalformedCountAsZero:
    @pytest.mark.parametrize("line", MALFORMED)
    def test_no_verified_zero(self, monkeypatch, line):
        monkeypatch.setattr(rlc, "query_lines", lambda: [line])
        results = rlc.evaluate_queries(_FACTS, {"path": set()}, set(), hierarchy={})
        assert "count results: 0 (distinct objects)" not in results, (
            f"malformed count query rendered as a verified zero: {line}"
        )

    @pytest.mark.parametrize("line", MALFORMED)
    def test_points_at_the_errors_section(self, monkeypatch, line):
        monkeypatch.setattr(rlc, "query_lines", lambda: [line])
        results = rlc.evaluate_queries(_FACTS, {"path": set()}, set(), hierarchy={})
        assert "count query malformed — see Errors above" in results

    def test_wellformed_count_still_renders(self, monkeypatch):
        monkeypatch.setattr(
            rlc, "query_lines", lambda: ['count("Marie Curie", "born_in")?']
        )
        results = rlc.evaluate_queries(_FACTS, {"path": set()}, set(), hierarchy={})
        assert "count results: 2 (distinct objects)" in results
