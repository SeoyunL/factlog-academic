# SPDX-License-Identifier: Apache-2.0
"""The gate and the report must judge a query constant by its POSITION, alike (#362).

`classify_query` (the ask gate) never judged "the vocabulary": it judges a subject
against `entity_set`, a relation name against the accepted relations and their
declared aliases, a relation object against `value_set` plus the ancestors declared
UNDER THE QUERIED RELATION, and a policy query's pinned entity against `entity_set`
again. The report pooled all four into one set of constants, which is a superset of
every one of them. So on a KB that declares attribute relations or a value hierarchy
the two disagreed, and always in the dangerous direction: the gate answered
`entity_not_accepted` while the report rendered the empty extent as `0 rows` — a
VERIFIED NEGATIVE, the engine asserting "no such fact" about a term the KB never
adopted in that position (#284's failure mode, reached through #350/#351's rendering).

Both sides now route every one of those decisions through `common.QueryVocabulary`,
so the four positions are tested here as one property: for each query, the gate's
accept/reject and the report's `0 rows`/`unverified` must agree.
"""
from __future__ import annotations

import pytest
import run_logic_check as rlc

from factlog.common import (
    QUERY_ENTITY_NOT_ACCEPTED,
    QUERY_OK,
    classify_query,
)

# `published_year` is an attribute relation, so its object `2020` is a literal VALUE
# and not an entity: entity_set excludes it while value_set keeps it. `anyone` is
# declared an ancestor under `founded_by` ONLY, so it is a legitimate object there
# and nowhere else. Between them these two declarations separate all four positions.
ATTRIBUTE_RELATIONS_MD = "published_year\n"
VALUE_HIERARCHY_MD = "- founded_by: Bob ⊂ anyone\n"
POLICY_PROGRAM = (
    ".decl needs_review(e: symbol, r: symbol)\n"
    'needs_review(E, "low_conf") :- relation(E, "founded_by", "Bob").'
)

FACTS = [
    {"subject": "Alice", "relation": "founded_by", "object": "Bob"},
    {"subject": "Paper1", "relation": "published_year", "object": "2020"},
]


@pytest.fixture
def kb(tmp_path, monkeypatch):
    """A KB whose declarations both the gate and the report read from disk."""
    import factlog.common as fc

    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "attribute-relations.md").write_text(ATTRIBUTE_RELATIONS_MD, encoding="utf-8")
    (policy / "value-hierarchy.md").write_text(VALUE_HIERARCHY_MD, encoding="utf-8")
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    monkeypatch.setattr(fc, "POLICY_DIR", policy)
    monkeypatch.setattr(rlc, "FACTS_DIR", facts_dir)
    return tmp_path


def _gate_accepts(query: str) -> bool:
    """Does the ask gate admit the query's VOCABULARY?

    `fact_absent` is an accepted vocabulary with an absent triple — the verified
    negative the report renders as `0 rows` — so it counts as accepted here. Only an
    unaccepted constant is a rejection.
    """
    ok, code, reason = classify_query(query, FACTS, policy_program=POLICY_PROGRAM)
    assert code != "malformed", reason
    if not ok:
        assert code in {QUERY_ENTITY_NOT_ACCEPTED, "relation_not_accepted", "fact_absent"}, code
    return code != QUERY_ENTITY_NOT_ACCEPTED and code != "relation_not_accepted"


def _report_line(kb, query: str) -> str:
    (kb / "facts" / "query.dl").write_text(query + "\n", encoding="utf-8")
    results = rlc.evaluate_queries(
        FACTS,
        {"path": set(), "needs_review": {("Alice", "low_conf")}},
        {"needs_review"},
    )
    assert len(results) == 1, results
    return results[0]


def _report_accepts(kb, query: str) -> bool:
    return "unverified" not in _report_line(kb, query)


def _warns(query: str) -> list[str]:
    from common import QueryVocabulary, relation_aliases, value_hierarchy

    vocab = QueryVocabulary.from_facts(FACTS, value_hierarchy(), relation_aliases())
    return rlc.validate_query(query, vocab, {"needs_review"})[1]


class TestTheFourPositionsAgree:
    """One property, four positions. Each query names a constant that the pooled set
    admitted and the position does not."""

    @pytest.mark.parametrize(
        "query",
        [
            # OBJECT: `anyone` is declared only under `founded_by`, so under
            # `published_year` it is not vocabulary — the issue's headline case.
            'relation("Alice", "published_year", "anyone")?',
            # SUBJECT: `2020` is an attribute literal, in value_set but not an entity.
            'relation("2020", R, O)?',
            # RELATION NAME: never accepted, never declared as an alias.
            'relation("Alice", "invented_by", "Bob")?',
            # POLICY ENTITY: the same attribute literal, pinned in a policy query.
            'needs_review("2020", R)?',
        ],
    )
    def test_a_constant_its_position_rejects_is_rejected_on_both_sides(self, kb, query):
        assert _gate_accepts(query) is False, "gate must reject the constant"
        assert _report_accepts(kb, query) is False, (
            f"report rendered a verified negative for a query the gate rejects: "
            f"{_report_line(kb, query)!r}"
        )

    @pytest.mark.parametrize(
        "query",
        [
            # The declaration IS in scope here, and it matches the narrower row.
            'relation("Alice", "founded_by", "anyone")?',
            # A VARIABLE relation really can range over every relation, so the gate
            # widens the object licence to the whole file — and so must the report.
            'relation("Alice", R, "anyone")?',
            # An accepted literal in the position that admits literals.
            'relation("Paper1", "published_year", "2020")?',
            # An accepted entity pinned in a policy query.
            'needs_review("Alice", R)?',
        ],
    )
    def test_a_constant_its_position_accepts_is_accepted_on_both_sides(self, kb, query):
        assert _gate_accepts(query) is True, "gate must accept the constant"
        assert _report_accepts(kb, query) is True, _report_line(kb, query)

    @pytest.mark.parametrize(
        "query",
        [
            'relation("Alice", "published_year", "anyone")?',
            'relation("2020", R, O)?',
            'relation("Alice", "invented_by", "Bob")?',
            'needs_review("2020", R)?',
            'relation("Alice", "founded_by", "anyone")?',
            'relation("Alice", R, "anyone")?',
            'relation("Paper1", "published_year", "2020")?',
            'needs_review("Alice", R)?',
            # The discriminator belongs in the parity set too: accepted vocabulary
            # with an absent triple must be accepted by BOTH, not just by the report.
            'relation("Alice", "founded_by", "Paper1")?',
        ],
    )
    def test_the_two_verdicts_are_equal(self, kb, query):
        """PARITY is the assertion, not either side's expected string.

        The tests above pin what each side should say, which is what a reader needs;
        this one pins that they say THE SAME THING. Both are needed: agreeing on a
        wrong answer is still a bug, and the pinned expectations catch that — but a
        pair of expectations can be updated in lockstep by someone who reads only one
        of them, and this assertion cannot be satisfied that way.
        """
        gate, report = _gate_accepts(query), _report_accepts(kb, query)
        assert gate == report, (
            f"gate accepts={gate} but report accepts={report}: {_report_line(kb, query)!r}"
        )


class TestTheDiscriminatorSurvives:
    """Narrowing the sets must not turn an honest verified negative into `unverified`
    — that would trade one false answer for a report that can no longer say `no`."""

    def test_accepted_vocabulary_with_an_absent_triple_is_still_zero_rows(self, kb):
        line = _report_line(kb, 'relation("Alice", "founded_by", "Paper1")?')
        assert line == "relation results: 0 rows", line
        _, code, _ = classify_query(
            'relation("Alice", "founded_by", "Paper1")?', FACTS, policy_program=POLICY_PROGRAM
        )
        assert code == "fact_absent", code

    def test_an_accepted_policy_entity_with_no_rows_is_still_zero_rows(self, kb):
        line = _report_line(kb, 'needs_review("Bob", R)?')
        assert line == "needs_review results: 0 rows", line
        _, code, _ = classify_query(
            'needs_review("Bob", R)?', FACTS, policy_program=POLICY_PROGRAM
        )
        assert code == QUERY_OK, code


class TestTheWarningPointerIsExact:
    """`unverified — '...' (see Warnings above)` must point at a warning that is
    actually there: the result line and the Warnings section read one predicate."""

    @pytest.mark.parametrize(
        ("query", "constant"),
        [
            ('relation("Alice", "published_year", "anyone")?', "anyone"),
            ('relation("2020", R, O)?', "2020"),
            ('needs_review("2020", R)?', "2020"),
        ],
    )
    def test_every_unverified_result_has_its_warning(self, kb, query, constant):
        line = _report_line(kb, query)
        assert f"'{constant}' is not accepted vocabulary (see Warnings above)" in line, line
        assert any(constant in warning for warning in _warns(query)), _warns(query)

    def test_a_constant_its_position_accepts_draws_no_warning(self, kb):
        assert _warns('relation("Alice", "founded_by", "anyone")?') == []
        assert _warns('needs_review("Alice", R)?') == []

    def test_one_unaccepted_constant_is_warned_about_once(self, kb):
        """The position checks return instead of falling through to the generic
        constant loop, which would report the same constant a second time."""
        assert _warns('relation("2020", R, O)?') == [
            "query references non-engine entity or relation: 2020"
        ]
