# SPDX-License-Identifier: Apache-2.0
"""The two places #328 and #329 meet, pinned so a future resolution cannot drop
one side.

Both landed in the same functions, and in both the answer is "keep both, in this
order" rather than "pick one". Nothing in either PR's own suite fails if the
other half is removed here, which is why these pins exist:

* ``validate_query`` / ``evaluate_queries`` — #328's SIGNATURE verdict
  (``query_error``: arity, then shape) must run BEFORE #329's VOCABULARY check
  (are the endpoints accepted entities). Wrong order and a path query the gate
  refuses reaches ``path_endpoints`` anyway, where a single-quoted argument is
  not a quoted string, so it is silently dropped and the line is reported as an
  error AND warned about, or answered from whatever endpoints survived.
* ``policy_predicates`` — #348 excluded ``count``/``review_required`` because
  classify_query gives them a branch; #329 excluded ``attr_rel``/``entity_node``
  because the engine owns them. The exclusions are independent and the set is
  their union.
"""
from __future__ import annotations

import re

import pytest

import run_logic_check as rlc
from factlog import common as fl_common
from factlog.common import WIRELOG_PROGRAM as WIRELOG


def R(subject: str, relation: str, object_: str) -> dict[str, str]:
    return {
        "subject": subject,
        "relation": relation,
        "object": object_,
        "status": "accepted",
        "source": "sources/a.md",
    }


FACTS = [
    R("갑봇", "통합", "을서비스"),
    R("을서비스", "정식_운영", "2030.1"),
]
VALUES = {"갑봇", "을서비스", "2030.1"}   # value_set
NODES = {"갑봇", "을서비스"}              # entity_set: 정식_운영 is an attribute relation


@pytest.fixture
def attrs(monkeypatch):
    monkeypatch.setattr(fl_common, "attribute_relations", lambda: {"정식_운영"})
    monkeypatch.setattr(rlc, "attribute_relations", lambda: {"정식_운영"}, raising=False)


@pytest.fixture
def queries(monkeypatch):
    def _bind(*lines: str) -> None:
        monkeypatch.setattr(rlc, "query_lines", lambda: list(lines))

    return _bind


# A path query the gate refuses, one per rule, with the verdict kind it gets.
REFUSED = [
    ('path()?', "bad_arity"),
    ('path("갑봇")?', "bad_arity"),
    ('path("갑봇", "을서비스", "X")?', "bad_arity"),
    ("path('갑봇', '2030.1')?", "malformed"),
    ("path(갑봇, 을서비스)?", "malformed"),
]


class TestSignatureIsDecidedBeforeVocabulary:
    """#328's guard runs first; #329's endpoint check only sees lines it passed."""

    @pytest.mark.parametrize("line,kind", REFUSED)
    def test_the_gate_refuses_these(self, line, kind):
        # CONTROL — these are exactly the lines classify_query rejects on signature,
        # so the report owes the same verdict and no answer.
        ok, verdict, _ = fl_common.classify_query(line, FACTS)
        assert (ok, verdict) == (False, kind)

    @pytest.mark.parametrize("line,kind", REFUSED)
    def test_a_refused_line_is_an_error_and_nothing_else(self, line, kind, attrs):
        errors, warnings = rlc.validate_query(line, VALUES, set(), NODES)
        assert len(errors) == 1
        assert line in errors[0]
        # Never also a warning: one line, one verdict.
        assert warnings == []

    @pytest.mark.parametrize("line,kind", REFUSED)
    def test_a_refused_line_is_never_answered(self, line, kind, attrs, queries):
        queries(line)
        assert rlc.evaluate_queries(FACTS, {"path": set()}, set(), NODES) == []

    @pytest.mark.parametrize("line,kind", REFUSED)
    def test_endpoints_are_never_read_for_a_refused_line(self, line, kind, monkeypatch):
        # The ordering itself, not just its outcome: `path('갑봇','2030.1')?` has two
        # arguments that are not quoted strings, so path_endpoints returns [] and the
        # endpoint check would silently pass a line the gate rejects.
        seen: list[str] = []
        original = rlc.path_endpoints
        monkeypatch.setattr(
            rlc, "path_endpoints", lambda text: seen.append(text) or original(text)
        )
        rlc.validate_query(line, VALUES, set(), NODES)
        assert seen == []

    def test_a_well_formed_line_still_reaches_the_endpoint_check(self, attrs, queries):
        # The other direction: #329's check must not be shadowed by the new guard.
        line = 'path("갑봇", "2030.1")?'
        errors, warnings = rlc.validate_query(line, VALUES, set(), NODES)
        assert errors == []
        assert warnings == ["query path argument is not an accepted entity: 2030.1"]
        queries(line)
        assert rlc.evaluate_queries(FACTS, {"path": set()}, set(), NODES) == [
            "path 갑봇 -> 2030.1: (not evaluated — not an accepted entity: 2030.1)"
        ]

    def test_a_reachable_pair_is_still_answered(self, attrs, queries):
        # CONTROL — neither guard touches an ordinary answerable query.
        queries('path("갑봇", "을서비스")?')
        assert rlc.evaluate_queries(
            FACTS, {"path": {("갑봇", "을서비스")}}, set(), NODES
        ) == ["path 갑봇 -> 을서비스: 갑봇 -> 을서비스"]


class TestBuiltInExclusionsAreTheUnion:
    """`policy_predicates` excludes both PRs' names, for their two reasons."""

    @pytest.mark.parametrize(
        "predicate",
        [
            "relation",
            "edge",
            "path",
            "count",
            "review_required",
            # WIRELOG_PROGRAM declares all three; `canonical` was the one the set
            # had been missing while the comment already described it. Adding it
            # left the sample-kb's policy_predicates, all five gate verdicts, and
            # the whole logic_report.txt byte-identical.
            "canonical",
            "attr_rel",
            "entity_node",
        ],
    )
    def test_a_built_in_never_becomes_a_policy_predicate(self, predicate):
        program = f".decl {predicate}(entity: symbol, reason: symbol)\n"
        assert fl_common.policy_predicates(program) == set()

    def test_every_wirelog_declared_name_is_excluded(self):
        """The set must not drift from the engine program it mirrors: every name
        WIRELOG_PROGRAM `.decl`s is engine-owned and cannot be a policy predicate."""
        declared = set(
            re.findall(r"^\.decl\s+([A-Za-z_][A-Za-z0-9_]*)\(", WIRELOG, flags=re.M)
        )
        assert declared  # the program really does declare things
        program = "".join(
            f".decl {name}(entity: symbol, reason: symbol)\n" for name in sorted(declared)
        )
        assert fl_common.policy_predicates(program) == set()

    @pytest.mark.parametrize(
        "predicate",
        [
            "conflict",
            "stale_entity",
            # The repo's OWN generated policy predicate. Its name contains both
            # words of the excluded `review_required` in the other order, so a
            # substring or fuzzy exclusion would silently swallow it.
            "requires_review",
            # Predicates the reserved-head guard actively suggests as renames.
            "my_attr_rel",
            "my_entity_node",
            "not_canonical",
            # Names that merely contain an excluded one.
            "attr_rel_audit",
            "entity_node_audit",
            "counted",
            "pathway",
        ],
    )
    def test_a_legitimate_policy_predicate_survives(self, predicate):
        program = f".decl {predicate}(entity: symbol, reason: symbol)\n"
        assert fl_common.policy_predicates(program) == {predicate}

    def test_a_mixed_program_keeps_only_the_policy_predicates(self):
        program = (
            ".decl conflict(entity: symbol, reason: symbol)\n"
            ".decl stale_entity(entity: symbol, reason: symbol)\n"
            ".decl attr_rel(rel: symbol)\n"
            ".decl entity_node(name: symbol)\n"
            ".decl count(subject: symbol, rel: symbol)\n"
        )
        assert fl_common.policy_predicates(program) == {"conflict", "stale_entity"}
