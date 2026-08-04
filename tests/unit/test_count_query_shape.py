# SPDX-License-Identifier: Apache-2.0
"""Report/gate parity for a count query's ARGUMENT SHAPE (#328).

``validate_query`` used to judge a count query on ARITY alone while the gate's
``classify_query`` also required each argument to be a variable or a quoted
string. A line the gate rejects as ``malformed`` therefore reached the report,
which rendered it as a verified aggregate:

    count("Marie Curie", 'born_in')?
      report -> ([], [])                          # no error, answer printed
      gate   -> (False, 'malformed', 'count arguments must be ...')

The number was also wrong, not merely unvetted: ``evaluate_queries`` treats a
non-double-quoted argument as a WILDCARD, so ``'born_in'`` filtered nothing and
the count spanned every relation of that subject.

These tests pin both halves of the fix — the report errors on the shape, and it
prints no count answer for a line it is simultaneously calling an error — and
pin that the two paths agree line by line.
"""
from __future__ import annotations

import pytest

import run_logic_check as rlc
from factlog.common import QUERY_OK, classify_query


def _fact(subject, relation, object_):
    return {"subject": subject, "relation": relation, "object": object_}


FACTS = [
    _fact("Marie Curie", "born_in", "Warsaw"),
    _fact("Marie Curie", "born_in", "Poland"),
    _fact("Marie Curie", "worked_at", "Sorbonne"),
]
ENTITIES = {"Marie Curie", "Warsaw", "Poland", "Sorbonne", "born_in", "worked_at"}

# Shapes the gate rejects as malformed: an argument that is neither a Datalog
# variable nor a double-quoted string.
MALFORMED = [
    "count(\"Marie Curie\", 'born_in')?",   # single quotes are not a string literal
    "count(Marie Curie, born_in)?",         # bare tokens
    "count('Marie Curie', \"born_in\")?",   # malformed subject, well-formed relation
]

WELL_FORMED = [
    'count("Marie Curie", "born_in")?',
    'count(S, R)?',
    'count("Marie Curie", R)?',
]


def _report_errors(line):
    errors, _warnings = rlc.validate_query(line, ENTITIES, set())
    return errors


def _evaluate(monkeypatch, line):
    monkeypatch.setattr(rlc, "query_lines", lambda: [line])
    return rlc.evaluate_queries(FACTS, {}, set())


class TestReportRejectsMalformedCountArgs:
    @pytest.mark.parametrize("line", MALFORMED)
    def test_shape_violation_is_an_error(self, line):
        errors = _report_errors(line)
        assert any("variables or quoted strings" in item for item in errors), errors

    @pytest.mark.parametrize("line", MALFORMED)
    def test_no_count_answer_is_rendered(self, monkeypatch, line):
        # The report must not answer a line it is calling an error: the wrong
        # number is what a reader would take away, not the Errors section.
        assert _evaluate(monkeypatch, line) == []


class TestReportGateParityOnCount:
    @pytest.mark.parametrize("line", MALFORMED + WELL_FORMED)
    def test_verdicts_agree(self, line):
        report_ok = not _report_errors(line)
        gate_ok, _code, _reason = classify_query(line, FACTS, "")
        assert report_ok == (gate_ok and _code == QUERY_OK), (line, _code, _reason)


class TestWellFormedCountUnchanged:
    """Regression anchors — these pass before AND after the fix (not pins)."""

    @pytest.mark.parametrize("line", WELL_FORMED)
    def test_well_formed_count_still_passes(self, line):
        assert _report_errors(line) == []

    def test_well_formed_count_still_evaluates(self, monkeypatch):
        assert _evaluate(monkeypatch, 'count("Marie Curie", "born_in")?') == [
            "count results: 2 (distinct objects)"
        ]

    def test_arity_message_unchanged(self):
        assert _report_errors('count("Marie Curie")?') == [
            'count query must have subject and relation arguments: count("Marie Curie")?'
        ]
