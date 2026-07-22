# SPDX-License-Identifier: Apache-2.0
"""entity_audit must not pair typed literals with each other (#386).

`text-to-fact.md` mandates compound-term notation for typed values — `date(2020)`,
`number(19)`. entity_audit only knew the pre-normalization prose forms, so those
values stayed in the entity set, `_tokens` split the wrapper name off, and `date`
became a token shared by every date: all C(n,2) date pairs surfaced as
fragmentation candidates and buried the real ones.

The recognizer REMOVES values from the entity listing, so a widening here hides a
real entity. These tests therefore pin three things, not one:
  - the wrapper pairs are gone;
  - a genuine fragmentation candidate still fires;
  - and every near-miss (`Date(Time)`, `date()`, `date(2020)` + newline, ...) is
    left alone. The near-miss half is what fails when the recognizer is loosened,
    so it is the half that keeps this fix honest.
"""
from __future__ import annotations

import pytest

import entity_audit


def _row(subject, relation, object_, status="accepted"):
    return {
        "subject": subject,
        "relation": relation,
        "object": object_,
        "status": status,
    }


def _shared_token_pairs(found):
    return [c for c in found["clusters"] if c[2].startswith("shared token")]


class TestCompoundTermsAreNotEntities:
    def test_dates_and_numbers_never_pair_with_each_other(self):
        facts = [
            _row("P1", "published_year", "date(1998)"),
            _row("P2", "published_year", "date(2020)"),
            _row("P3", "published_year", "date(2023)"),
            _row("P4", "published_year", "date(2025)"),
            _row("P1", "cited_by_count", "number(19)"),
            _row("P2", "cited_by_count", "number(92)"),
            _row("P3", "cited_by_count", "number(228)"),
            _row("P4", "cited_by_count", "number(348)"),
        ]
        found = entity_audit.audit(facts)

        assert _shared_token_pairs(found) == []
        for value in ("date(2020)", "number(19)"):
            assert value not in found["entities"]

    def test_every_wrapper_name_is_covered(self):
        # The names come from literal_types.TYPES; each one must be recognized.
        facts = [
            _row("P1", "attr", "date(2020,3,8)"),
            _row("P2", "attr", "number(2.5)"),
            _row("P3", "attr", "ordinal(3)"),
            _row("P4", "attr", 'amount(100,"억")'),
        ]
        found = entity_audit.audit(facts)

        assert found["entities"] == ["P1", "P2", "P3", "P4"]
        assert found["clusters"] == []

    def test_an_undeclared_relation_still_gets_the_declare_advice(self):
        # Dropping them from the entity set must not make them silent: the point
        # of the tool is to say "declare this relation".
        facts = [_row("P1", "published_year", "date(2020)")]
        found = entity_audit.audit(facts)

        assert found["literal_suspects"]["published_year"] == {"date(2020)"}


class TestNearMissesStayEntities:
    """The recognizer must not eat names that merely resemble the notation.

    Every value here was accepted by the first cut (`re.IGNORECASE` + `.*` + `$`),
    which would have deleted a legitimate entity from the report.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "Date(Time)",                # a dataset column label
            "Amount(USD)",               # a spreadsheet header
            "AMOUNT(Adjusted)",          # ditto, shouting
            "date()",                    # names no value at all
            "number(19) vs number(20)",  # a comparison: one entity, not a literal
            "date(2020)\n",              # trailing control char: stays visible (#373)
            "date(2020) ",               # padding is not the mandated notation either
            "predate(2020)",             # the wrapper name must start the string
            "date((2020))",              # nested parens are not the notation
            "기타(IL-10)",               # an ordinary parenthetical value
        ],
    )
    def test_near_miss_is_not_a_compound_term(self, value):
        assert entity_audit._is_compound_term(value) is False

    @pytest.mark.parametrize("value", ["Date(Time)", "Amount(USD)", "date()", "date(2020)\n"])
    def test_near_miss_survives_in_the_entity_listing(self, value):
        found = entity_audit.audit([_row(value, "topic", "AI")])

        assert value in found["entities"]
        assert found["literal_subjects"] == []
        assert found["malformed_literals"] == []


class TestRealCandidatesSurvive:
    def test_substring_contained_pair_is_still_reported(self):
        facts = [
            _row("Neurosymbolic Value-Inspired AI (Why What and How)", "topic", "AI"),
            _row("Value-Inspired AI", "topic", "AI"),
            _row("P1", "published_year", "date(2020)"),
            _row("P2", "published_year", "date(2023)"),
        ]
        found = entity_audit.audit(facts)

        assert _shared_token_pairs(found) == []
        reasons = {c[2] for c in found["clusters"]}
        assert "substring-contained" in reasons

    def test_a_shared_token_between_real_entities_is_still_reported(self):
        facts = [
            _row("Samplebot Research Lab", "topic", "AI"),
            _row("Samplebot Institute", "topic", "AI"),
        ]
        found = entity_audit.audit(facts)

        assert [c[2] for c in _shared_token_pairs(found)] == ["shared token ['Samplebot']"]


class TestLiteralInSubjectPosition:
    def test_a_literal_subject_is_surfaced_not_swallowed(self):
        # declared_literals and literal_suspects only look at objects, so removing
        # a subject-position compound term from `entities` without this section
        # would erase it from the whole report.
        found = entity_audit.audit([_row("date(2020)", "author", "Kim")])

        assert found["entities"] == ["Kim"]
        assert found["literal_subjects"] == ["date(2020)"]

    def test_an_object_only_literal_is_not_called_a_subject(self):
        found = entity_audit.audit([_row("P1", "published_year", "date(2020)")])

        assert found["literal_subjects"] == []


class TestMalformedCompoundTerms:
    def test_unparsable_body_is_reported_not_hidden(self):
        # Wrapper-shaped but not a value. Silently filing it as "a literal" would
        # hide exactly the row a human has to fix.
        found = entity_audit.audit([_row("P1", "when", "date(abc)")])

        assert "date(abc)" not in found["entities"]
        assert found["malformed_literals"] == ["date(abc)"]

    def test_impossible_date_is_reported(self):
        found = entity_audit.audit([_row("P1", "when", "date(2020,2,30)")])

        assert found["malformed_literals"] == ["date(2020,2,30)"]

    def test_a_parsable_compound_term_is_not_malformed(self):
        facts = [
            _row("P1", "when", "date(2020,3,8)"),
            _row("P2", "count", "number(19)"),
            _row("P3", "rank", "ordinal(3)"),
            _row("P4", "budget", 'amount(100,"억")'),
        ]
        found = entity_audit.audit(facts)

        assert found["malformed_literals"] == []

    def test_amount_with_a_kb_declared_unit_is_not_malformed(self, monkeypatch):
        # `파운드` is not in literal_types' built-in table, but the KB's
        # typed-relations line declares it, and the engine parses the value to
        # 8500. Judging it against the built-in table alone told a human to fix
        # correct data — the worst failure mode for an advisory tool.
        from common import TypedRelSpec

        monkeypatch.setattr(
            entity_audit,
            "typed_relations",
            lambda: {"예산": TypedRelSpec(type="amount", alias="budget", units={"파운드": 1700, "원": 1})},
        )
        found = entity_audit.audit([_row("P1", "예산", 'amount(5,"파운드")')])

        assert found["malformed_literals"] == []

    def test_amount_with_no_declaration_is_not_judged(self):
        # No typed-relations line means no unit table to judge against, and the
        # engine never parses the value either. Silence beats a false accusation.
        found = entity_audit.audit([_row("P1", "예산", 'amount(5,"파운드")')])

        assert found["malformed_literals"] == []
        assert "amount(5,\"파운드\")" not in found["entities"]

    def test_amount_outside_a_declared_unit_table_is_still_reported(self, monkeypatch):
        # The table IS readable here, and `달러` is not in it — so the engine
        # genuinely cannot parse this one. Skipping unjudgeable amounts must not
        # turn into skipping every amount.
        from common import TypedRelSpec

        monkeypatch.setattr(
            entity_audit,
            "typed_relations",
            lambda: {"예산": TypedRelSpec(type="amount", alias="budget", units={"파운드": 1700})},
        )
        found = entity_audit.audit([_row("P1", "예산", 'amount(5,"달러")')])

        assert found["malformed_literals"] == ['amount(5,"달러")']

    def test_year_only_date_tracks_literal_types(self):
        # COUPLING (#385): `date(2020)` does not parse today, so it reports as
        # malformed. When #385 lands year-only dates this assertion flips with no
        # change to entity_audit — the parse question lives in literal_types, and
        # this test asks it out loud rather than freezing a copy of the answer.
        from factlog import literal_types

        parses = literal_types.normalize("date", "date(2020)") is not None
        found = entity_audit.audit([_row("P1", "published_year", "date(2020)")])

        assert found["malformed_literals"] == ([] if parses else ["date(2020)"])
        # Either way it is never an entity and never loses the declare advice.
        assert found["entities"] == ["P1"]
        assert found["literal_suspects"]["published_year"] == {"date(2020)"}


class TestAmountShapeIsJudgedWithoutADeclaration:
    """#394 — the unit exemption covers unit RESOLUTION, not the whole type.

    A KB that writes compound terms before declaring them is the population this
    section exists for; exempting `amount` wholesale left exactly that population
    with no signal. These values fail on literal_types' `num` group, before any
    unit lookup, so no declaration could ever make them parse.
    """

    @pytest.mark.parametrize("value", ['amount(abc,"억")', 'amount(,"억")', "amount(5)"])
    def test_shape_failure_is_malformed_with_no_typed_declaration(self, value):
        found = entity_audit.audit([_row("P1", "예산", value)])

        assert found["malformed_literals"] == [value]

    def test_unit_resolution_failure_stays_unjudged_with_no_declaration(self):
        # The other half of the split, and the half that keeps the fix honest: the
        # unit table really is per-KB, so an unresolved unit must stay silent.
        found = entity_audit.audit([_row("P1", "예산", 'amount(5,"달러")')])

        assert found["malformed_literals"] == []

    def test_shape_failure_is_malformed_under_a_non_amount_relation(self, monkeypatch):
        # INFO in #394: the wrapper name decides the type for every type. A relation
        # declared `number` supplies no unit table, so it is the no-spec case — the
        # shape is still judged.
        from common import TypedRelSpec

        monkeypatch.setattr(
            entity_audit,
            "typed_relations",
            lambda: {"예산": TypedRelSpec(type="number", alias="budget")},
        )
        found = entity_audit.audit([_row("P1", "예산", 'amount(abc,"억")')])

        assert found["malformed_literals"] == ['amount(abc,"억")']

    def test_a_declared_amount_of_good_shape_is_still_not_malformed(self, monkeypatch):
        from common import TypedRelSpec

        monkeypatch.setattr(
            entity_audit,
            "typed_relations",
            lambda: {"예산": TypedRelSpec(type="amount", alias="budget", units={"파운드": 1700})},
        )
        found = entity_audit.audit([_row("P1", "예산", 'amount(5,"파운드")')])

        assert found["malformed_literals"] == []


class TestConflictingTypedDeclarations:
    """#393 — two declarations claiming one surface form must not silently win.

    The canonical and its alias each carrying their OWN unit table made both lines
    expand onto both forms; the last written overwrote the first, so a value written
    under the CANONICAL was judged against the ALIAS's table and falsely reported
    malformed. The parser accepts the pair (exit 0), so nothing else told the author.
    """

    @staticmethod
    def _two_tables(monkeypatch):
        from common import TypedRelSpec

        monkeypatch.setattr(
            entity_audit,
            "typed_relations",
            lambda: {
                "published_year": TypedRelSpec(type="amount", alias="a1", units={"파운드": 1700}),
                "게재연도": TypedRelSpec(type="amount", alias="a2", units={"달러": 1300}),
            },
        )
        monkeypatch.setattr(entity_audit, "relation_aliases", lambda: {"게재연도": "published_year"})

    def test_the_issues_reproduction_is_no_longer_a_false_accusation(self, monkeypatch):
        self._two_tables(monkeypatch)
        found = entity_audit.audit([
            _row("P1", "published_year", 'amount(9,"파운드")'),
            _row("P2", "게재연도", 'amount(5,"파운드")'),
        ])

        assert found["malformed_literals"] == []

    def test_the_conflict_is_reported_not_just_swallowed(self, monkeypatch):
        # Dropping the form alone would fix the false accusation while leaving the
        # author with no way to learn why the table stopped applying.
        self._two_tables(monkeypatch)
        found = entity_audit.audit([_row("P1", "published_year", 'amount(9,"파운드")')])

        assert found["typed_form_conflicts"] == {
            "published_year": ["published_year", "게재연도"],
            "게재연도": ["published_year", "게재연도"],
        }

    def test_shape_failures_survive_a_conflicted_form(self, monkeypatch):
        # Skipping the contested form must skip UNIT resolution only — it must not
        # become a way to switch the whole judgement off.
        self._two_tables(monkeypatch)
        found = entity_audit.audit([_row("P1", "published_year", 'amount(abc,"파운드")')])

        assert found["malformed_literals"] == ['amount(abc,"파운드")']

    def test_agreeing_declarations_are_not_a_conflict(self, monkeypatch):
        # Two DISTINCT objects carrying an equal table. Passing one object twice
        # would only prove `previous is spec` and would pass even if the comparison
        # were identity-based, so the carve-out has to be shown on separate objects.
        from common import TypedRelSpec

        monkeypatch.setattr(
            entity_audit,
            "typed_relations",
            lambda: {
                "published_year": TypedRelSpec(type="amount", alias="a1", units={"파운드": 1700}),
                "게재연도": TypedRelSpec(type="amount", alias="a1", units={"파운드": 1700}),
            },
        )
        monkeypatch.setattr(entity_audit, "relation_aliases", lambda: {"게재연도": "published_year"})
        found = entity_audit.audit([
            _row("P1", "published_year", 'amount(9,"파운드")'),
            _row("P2", "published_year", 'amount(5,"달러")'),
        ])

        assert found["typed_form_conflicts"] == {}
        assert found["malformed_literals"] == ['amount(5,"달러")']

    def test_declarations_differing_only_in_alias_are_not_a_conflict(self, monkeypatch):
        """The carve-out must survive the one difference real KBs ALWAYS have.

        common.py raises on a duplicate alias, so two lines can never share one —
        which means whole-spec equality made the carve-out unreachable and reported
        a self-contradiction for agreeing declarations (a NEW false positive, worse
        than main's last-writer-wins, which at least picked an identical table).
        `alias` names the engine side-relation and never reaches a verdict.
        """
        from common import TypedRelSpec

        monkeypatch.setattr(
            entity_audit,
            "typed_relations",
            lambda: {
                "published_year": TypedRelSpec(type="amount", alias="a1", units={"파운드": 1700}),
                "게재연도": TypedRelSpec(type="amount", alias="a2", units={"파운드": 1700}),
            },
        )
        monkeypatch.setattr(entity_audit, "relation_aliases", lambda: {"게재연도": "published_year"})
        found = entity_audit.audit([
            _row("P1", "published_year", 'amount(9,"파운드")'),
            _row("P2", "게재연도", 'amount(5,"파운드")'),
            _row("P3", "published_year", 'amount(5,"달러")'),
        ])

        # No conflict, the shared table still applies, and coverage does not regress:
        # the genuinely unresolvable unit is still reported.
        assert found["typed_form_conflicts"] == {}
        assert found["malformed_literals"] == ['amount(5,"달러")']

    @staticmethod
    def _three_lines(monkeypatch, third_units):
        """Three lines on one canonical, DECLARED in an order that is not
        alphabetical — so declaration order and sorted order are distinguishable."""
        from common import TypedRelSpec

        monkeypatch.setattr(
            entity_audit,
            "typed_relations",
            lambda: {
                "출판연도": TypedRelSpec(type="amount", alias="a3", units={"파운드": 1700}),
                "published_year": TypedRelSpec(type="amount", alias="a1", units={"파운드": 1700}),
                "게재연도": TypedRelSpec(type="amount", alias="a2", units=third_units),
            },
        )
        monkeypatch.setattr(
            entity_audit,
            "relation_aliases",
            lambda: {"게재연도": "published_year", "출판연도": "published_year"},
        )

    def test_every_claimant_is_named_even_when_some_agree(self, monkeypatch):
        """The first two lines AGREE; only the third differs.

        Recording just the disagreeing pair dropped whichever agreeing line was
        overwritten as "the" declarer, so the report named 2 of 3 lines — and sent
        the author to edit the wrong ones. All three claim the form; all three are
        named.
        """
        self._three_lines(monkeypatch, {"달러": 1300})
        found = entity_audit.audit([_row("P1", "published_year", 'amount(9,"파운드")')])

        assert found["typed_form_conflicts"]["published_year"] == [
            "출판연도", "published_year", "게재연도",
        ]

    def test_report_order_is_fixed_not_incidental(self, monkeypatch):
        """Ordering must be asserted as a LIST — dict `==` ignores key order.

        Three contested forms and three claimants, so both the outer (form) and
        inner (claimant) orderings have something to get wrong. Without this,
        reversing either ordering in `_typed_spec_by_form` left every test passing.
        Forms come out sorted; claimants keep declaration order, which is why the
        fixture declares them non-alphabetically.
        """
        self._three_lines(monkeypatch, {"달러": 1300})
        found = entity_audit.audit([_row("P1", "published_year", 'amount(9,"파운드")')])
        claimants = ["출판연도", "published_year", "게재연도"]

        assert list(found["typed_form_conflicts"].items()) == [
            ("published_year", claimants),
            ("게재연도", claimants),
            ("출판연도", claimants),
        ]

    def test_three_agreeing_lines_are_still_not_a_conflict(self, monkeypatch):
        # The completeness fix must not turn "seen more than once" into "contested".
        self._three_lines(monkeypatch, {"파운드": 1700})
        found = entity_audit.audit([
            _row("P1", "published_year", 'amount(9,"파운드")'),
            _row("P2", "published_year", 'amount(5,"달러")'),
        ])

        assert found["typed_form_conflicts"] == {}
        assert found["malformed_literals"] == ['amount(5,"달러")']

    def test_an_unrelated_declaration_keeps_its_table(self, monkeypatch):
        # The conflict must be scoped to the contested form, not disable typing.
        from common import TypedRelSpec

        monkeypatch.setattr(
            entity_audit,
            "typed_relations",
            lambda: {
                "published_year": TypedRelSpec(type="amount", alias="a1", units={"파운드": 1700}),
                "게재연도": TypedRelSpec(type="amount", alias="a2", units={"달러": 1300}),
                "예산": TypedRelSpec(type="amount", alias="a3", units={"파운드": 1700}),
            },
        )
        monkeypatch.setattr(entity_audit, "relation_aliases", lambda: {"게재연도": "published_year"})
        found = entity_audit.audit([
            _row("P1", "예산", 'amount(5,"달러")'),
            _row("P2", "예산", 'amount(5,"파운드")'),
        ])

        assert found["malformed_literals"] == ['amount(5,"달러")']
        assert "예산" not in found["typed_form_conflicts"]
