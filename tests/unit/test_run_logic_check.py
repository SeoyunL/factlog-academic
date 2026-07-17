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


class TestQueryLines:
    """What counts as a query in facts/query.dl.

    This filter decides both which lines get validated and — since the report now
    derives its "empty vs. no result" message from it (#220) — which of those two
    messages the user reads. A comment-only file must read as EMPTY: telling the
    user the file "has 2 line(s) but none produced a result" sends them auditing
    their own comments.
    """

    def _query_dl(self, tmp_path, monkeypatch, text):
        facts_dir = tmp_path / "facts"
        facts_dir.mkdir()
        (facts_dir / "query.dl").write_text(text, encoding="utf-8")
        monkeypatch.setattr(rlc, "FACTS_DIR", facts_dir)

    def test_comments_and_blanks_are_not_queries(self, tmp_path, monkeypatch):
        self._query_dl(
            tmp_path, monkeypatch, '// a comment\n\n   \nrelation("A", "uses", "B")?\n'
        )
        assert rlc.query_lines() == ['relation("A", "uses", "B")?']

    def test_a_comment_only_file_holds_no_queries(self, tmp_path, monkeypatch):
        self._query_dl(tmp_path, monkeypatch, "// only comments\n// still nothing\n")
        assert rlc.query_lines() == []

    def test_an_absent_file_holds_no_queries(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rlc, "FACTS_DIR", tmp_path / "no-such-dir")
        assert rlc.query_lines() == []


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

    def test_a_first_argument_that_is_not_a_valid_json_string_does_not_crash(self):
        """`"\\q"` starts and ends with a quote but is not a decodable JSON string.

        The inline `startswith('"') and endswith('"')` test called it a quoted
        constant and handed it to `arg_value`, which `json.loads`ed it and died with
        a `JSONDecodeError` — a hard crash of the whole report over one draft line
        (#342). It no longer reaches `arg_value`: `is_quoted_string` returns False for
        a non-decodable token, so the shape guard (#321) rejects it as malformed —
        a clean error, not a crash — exactly as the ask gate does.
        """
        errors, warnings = rlc.validate_query(
            'retracted("\\q", R)?', self.ENTITIES, self.POLICY
        )
        assert errors == [
            'policy query arguments must be variables or quoted strings: retracted("\\q", R)?'
        ]
        assert warnings == []

    def test_an_nfd_query_constant_meets_an_nfc_accepted_entity(self):
        """The `entities` set is `known_constants`, which folds every value through
        `canonical_value` (NFC). A raw comparison of the query constant against it
        called an NFD-typed query of an accepted entity a "non-engine entity" — a
        claim about the KB that is false, since the entity IS in the engine (#341).
        The generic constant loop (L187) already folds; the policy branch now folds
        the same way, so the two axes of one function no longer diverge.
        """
        import unicodedata

        nfc = unicodedata.normalize("NFC", "한글")
        nfd = unicodedata.normalize("NFD", nfc)
        assert nfc != nfd
        errors, warnings = rlc.validate_query(
            f'retracted("{nfd}", R)?', {nfc}, self.POLICY
        )
        assert (errors, warnings) == ([], [])


class TestPolicyQueryArgumentFormGuard:
    """The policy branch of `validate_query` must reject an argument that is neither
    a variable nor a quoted string — the same shape guard `classify_query` applies.

    Before #321 the policy branch checked arity only. A bare/single-quoted token
    like `'Alice'` is a wildcard to the matcher, so the report passed it with no
    error while the ask gate rejected it as malformed — the two verdicts on one
    line diverged (#319 is the same omission in the count branch).
    """

    ENTITIES = {"Alice", "P1"}
    POLICY = {"needs_review"}

    def test_a_single_quoted_bare_token_is_an_error(self):
        errors, warnings = rlc.validate_query(
            "needs_review('Alice', R)?", self.ENTITIES, self.POLICY
        )
        assert errors == [
            "policy query arguments must be variables or quoted strings: "
            "needs_review('Alice', R)?"
        ]
        assert warnings == []

    def test_a_well_formed_quoted_entity_still_passes(self):
        errors, warnings = rlc.validate_query(
            'needs_review("Alice", R)?', self.ENTITIES, self.POLICY
        )
        assert (errors, warnings) == ([], [])

    def test_a_well_formed_variable_entity_still_passes(self):
        errors, warnings = rlc.validate_query(
            "needs_review(X, R)?", self.ENTITIES, self.POLICY
        )
        assert (errors, warnings) == ([], [])

    def test_report_and_gate_agree_on_the_malformed_policy_query(self):
        """The parity #321 restores: report error <-> gate malformed, on one line."""
        from common import classify_query

        facts = [{"subject": "Alice", "relation": "authored", "object": "P1"}]
        policy = (
            ".decl needs_review(e: symbol, r: symbol)\n"
            'needs_review(E, "x") :- relation(E, "authored", "P1").'
        )
        query = "needs_review('Alice', R)?"
        report_errors, _ = rlc.validate_query(query, self.ENTITIES, self.POLICY)
        ok, reason, _ = classify_query(query, facts, policy_program=policy)
        assert report_errors, "report must flag the malformed policy query"
        assert ok is False and reason == "malformed", (reason, report_errors)


class TestUnverifiedVocabularyRender:
    """A relation/count query naming a subject or relation-name outside the
    accepted vocabulary is rendered "unverified", not a verified "0 rows" (#347).

    The gate rejects such a query (entity_not_accepted/relation_not_accepted) and
    never renders a result; the report used to answer it with "0 rows" — a VERIFIED
    NEGATIVE — while warning, on the same page, that the term is not an engine
    entity or relation. Zero rows over a vocabulary the KB never accepted is not a
    verified "no such fact". The discriminator: a fully-accepted vocabulary with an
    absent triple keeps the honest "0 rows".
    """

    def _evaluate(self, monkeypatch, queries, facts):
        monkeypatch.setattr(rlc, "query_lines", lambda: queries)
        return rlc.evaluate_queries(facts, {"path": set()}, set(), hierarchy={})

    def test_unaccepted_relation_name_is_unverified_not_zero(self, monkeypatch):
        # "Anthropic" is an accepted subject, "Claude Code" an accepted object, but
        # "develops" is not an accepted relation (needs_review) — sample-kb q5.
        facts = [_fact("Anthropic", "founded_by", "someone"), _fact("x", "y", "Claude Code")]
        results = self._evaluate(
            monkeypatch, ['relation("Anthropic", "develops", "Claude Code")?'], facts
        )
        assert any(
            "relation results: unverified" in line and "develops" in line for line in results
        ), results
        assert not any("relation results: 0 rows" in line for line in results), results

    def test_accepted_vocabulary_absent_triple_stays_zero(self, monkeypatch):
        # sample-kb q4: subject, relation-name and object all appear in accepted
        # facts, but this exact triple does not — a verified negative, still "0 rows".
        facts = [_fact("factlog", "is_a", "plugin"), _fact("Claude Code", "developed_by", "Anthropic")]
        results = self._evaluate(
            monkeypatch, ['relation("factlog", "developed_by", "Anthropic")?'], facts
        )
        assert "relation results: 0 rows" in results, results

    def test_unaccepted_count_relation_is_unverified(self, monkeypatch):
        facts = [_fact("Marie Curie", "won", "Nobel Prize")]
        results = self._evaluate(
            monkeypatch, ['count("Marie Curie", "no_such_rel")?'], facts
        )
        assert any(
            "count results: unverified" in line and "no_such_rel" in line for line in results
        ), results

    def test_accepted_count_with_no_objects_stays_zero(self, monkeypatch):
        # "Marie Curie" and "won" are both accepted vocabulary; the (subject, relation)
        # pair simply has no objects here — a verified zero, not unverified.
        facts = [_fact("Marie Curie", "born_in", "Warsaw"), _fact("Einstein", "won", "Nobel Prize")]
        results = self._evaluate(
            monkeypatch, ['count("Marie Curie", "won")?'], facts
        )
        assert "count results: 0 (distinct objects)" in results, results
