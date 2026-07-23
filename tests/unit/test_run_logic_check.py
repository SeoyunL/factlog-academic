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


def _row(status):
    return {"subject": "PMID_1", "relation": "개입_영양소", "object": "EPA", "status": status}


class TestStatusWarnings:
    """`superseded` is a legitimate non-engine status and must not warn;
    only a genuinely unrecognised status does."""

    def test_superseded_produces_no_warning(self):
        assert rlc.status_warnings([_row("superseded")]) == []

    def test_every_known_status_is_silent(self):
        from common import KNOWN_STATUSES

        assert rlc.status_warnings([_row(s) for s in KNOWN_STATUSES]) == []

    def test_unrecognised_status_still_warns(self):
        warnings = rlc.status_warnings([_row("bogus")])
        assert warnings == ["unknown status treated as non-engine input: bogus"]

    def test_superseded_does_not_mask_a_typo_in_the_same_batch(self):
        warnings = rlc.status_warnings([_row("superseded"), _row("bogus")])
        assert warnings == ["unknown status treated as non-engine input: bogus"]
