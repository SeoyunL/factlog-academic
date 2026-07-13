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
    return {"subject": "A", "relation": "r", "object": "B", "status": status}


class TestStatusWarnings:
    """Status vocabulary of the logic report (#208).

    `factlog reject`/`amend` retires a row as `superseded`. That is a known
    status, so the report must stay silent about it — warning per retired row
    made the report noisier the more review had been done. A typo must still
    warn.
    """

    def test_superseded_is_silent(self):
        assert rlc.status_warnings([_row("superseded")]) == []

    def test_engine_and_review_statuses_are_silent(self):
        rows = [_row(s) for s in ("confirmed", "accepted", "needs_review", "candidate")]
        assert rlc.status_warnings(rows) == []

    def test_unrecognised_status_still_warns(self):
        warnings = rlc.status_warnings([_row("bogus")])
        assert warnings == ["unknown status treated as non-engine input: bogus"]

    def test_warns_once_per_offending_row_only(self):
        rows = [_row("superseded"), _row("bogus"), _row("accepted")]
        assert len(rlc.status_warnings(rows)) == 1

    def test_vocabulary_follows_common(self):
        # Pins the derive-don't-restate rule: extending common's vocabulary must
        # extend this consumer, which is exactly what #208 broke.
        import common

        for status in common.KNOWN_STATUSES:
            assert rlc.status_warnings([_row(status)]) == [], status

    def test_known_statuses_covers_every_declared_status_set(self):
        # The above only pins that consumers derive from KNOWN_STATUSES — not
        # that KNOWN_STATUSES is complete. A new `*_STATUSES` set left out of the
        # union reintroduces #208 with every test still green. Introspect the
        # module so adding one forces the union to be updated.
        import common

        declared = set().union(
            *[
                value
                for name, value in vars(common).items()
                if name.endswith("_STATUSES")
                and name != "KNOWN_STATUSES"
                and isinstance(value, (set, frozenset))
            ]
        )
        assert declared <= set(common.KNOWN_STATUSES)

    def test_every_status_the_cli_writes_is_known(self):
        # accept/reject/amend write these. Restated here on purpose: cli.py sets
        # the strings inline rather than via constants, so nothing else pins the
        # CLI's write surface against the vocabulary. Add to this list if cli.py
        # starts writing a new status.
        import common

        assert {"accepted", "superseded"} <= set(common.KNOWN_STATUSES)


class TestPolicyQueryEntityWarning:
    """A policy query warns about its first argument only when that argument NAMES
    an entity the engine does not have.

    The guard is a conjunction — quoted AND quoted AND unknown. Relaxing it to a
    disjunction left the whole suite green, yet it warns on every VARIABLE first
    argument (a variable is never in `entities`), so `retracted(P, R)?` — the
    ordinary way to ask the question — would have reported the variable's own name
    as a "non-engine entity".
    """

    POLICY = {"retracted"}
    ENTITIES = {"논문A"}

    def test_a_quoted_unknown_entity_warns(self):
        errors, warnings = rlc.validate_query(
            'retracted("논문B", "reason")?', self.ENTITIES, self.POLICY
        )
        assert errors == []
        assert warnings == ["query references non-engine entity: 논문B"]

    def test_a_quoted_known_entity_is_silent(self):
        errors, warnings = rlc.validate_query(
            'retracted("논문A", "reason")?', self.ENTITIES, self.POLICY
        )
        assert (errors, warnings) == ([], [])

    def test_a_variable_first_argument_claims_no_entity(self):
        errors, warnings = rlc.validate_query("retracted(P, R)?", self.ENTITIES, self.POLICY)
        assert (errors, warnings) == ([], [])

    def test_a_policy_query_of_the_wrong_arity_is_an_error_not_a_warning(self):
        errors, warnings = rlc.validate_query('retracted("논문A")?', self.ENTITIES, self.POLICY)
        assert errors == [
            'policy query must have entity and reason arguments: retracted("논문A")?'
        ]
        assert warnings == []
