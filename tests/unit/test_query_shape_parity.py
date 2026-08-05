# SPDX-License-Identifier: Apache-2.0
"""Report / gate / router parity on a query's SIGNATURE — arity and shape (#328).

The gate (``classify_query``) applies two rules to every query: the argument
COUNT its predicate takes, then whether each argument is a Datalog variable or a
double-quoted string. Neither of the other two consumers applied both — count and
policy checked arity only, relation and path checked neither — so each answered
lines the gate rejects, and answered them WRONGLY, because each decides "is this
argument a constant" with its own predicate:

    count("Marie Curie", 'born_in')?
      report -> count over EVERY relation of the subject, printed as a verified
                aggregate (a non-double-quoted arg is a wildcard there)
      router -> count: 0, which reads as a verified negative (a non-variable arg
                is a constant there, and no fact has the relation "'born_in'")
      gate   -> (False, 'malformed', 'count arguments must be ...')

    relation("Marie Curie", "born_in", "Warsaw", X)?     # this triple IS a fact
      report -> relation results: 0 rows                 # reads as a denial
      router -> {'rows': [], 'count': 0}
      gate   -> (False, 'bad_arity', 'relation query must have ...')

The same mechanism ran through the rest: shape-malformed ``relation`` returned
rows spanning two relations with a ``'born_in'=worked_at`` binding, ``path``
returned the whole reachable set, a policy query returned the predicate's whole
extent bound to an entity it is not about (``Alice=Bob``), and an over-long
``path`` answered using the first two constants and ignoring the rest.

The fix is two shared rules — ``common.query_arity_error`` then
``common.query_shape_error`` — applied in that order by all three, so the classes
below assert the same verdict AND the same message on all three paths for the
same line. Testing only two of them is what let this survive: the count arity
guard (#257, 1bc172a) was pinned on the report AND the router precisely because
``cmd_evaluate`` calls ``evaluate`` with no ``classify`` in front of it, and a
test that imports only ``run_logic_check`` cannot see a router-only mutant.

NOT covered here, deliberately: the VOCABULARY axis, where the report and the
gate still legitimately disagree. ``count("Nobody", "born_in")?`` is well-shaped,
so the gate goes on to reject it (``entity_not_accepted``) while the report
answers ``0 (distinct objects)`` — a verified zero for a subject the KB has never
heard of. ``tests/test_count_check.sh`` pins that zero as intended behaviour, so
it is not changed here; ``validate_query`` now at least emits the same
"non-engine entity or relation" warning it emits for relation/path. Signature
parity is exact; vocabulary parity is not, and this file does not claim it.
"""
from __future__ import annotations

import pytest

import ask_router
import run_logic_check as rlc
from factlog import common
from factlog.common import QUERY_OK, classify_query


def _fact(subject, relation, object_):
    return {"subject": subject, "relation": relation, "object": object_}


FACTS = [
    _fact("Marie Curie", "born_in", "Warsaw"),
    _fact("Marie Curie", "born_in", "Poland"),
    _fact("Marie Curie", "worked_at", "Sorbonne"),
]
ENTITIES = {"Marie Curie", "Warsaw", "Poland", "Sorbonne", "born_in", "worked_at"}

POLICY_PREDICATE = "stale_entity"
POLICY_PROGRAM = f".decl {POLICY_PREDICATE}(entity: symbol, reason: symbol)\n"
POLICY_PREDICATES = {POLICY_PREDICATE}
INFERRED = {
    POLICY_PREDICATE: {("Marie Curie", "stale"), ("Pierre Curie", "no_source")},
    "path": {("Marie Curie", "Warsaw")},
}

# Lines the gate rejects as QUERY_BAD_ARITY: the wrong number of arguments.
# Every one of these is well-SHAPED, so each reaches the arity rule and only the
# arity rule — which is what makes them the pins for the arity half, and what
# makes their expected message the arity message on all three paths.
#
# The first two were answered, not merely unchecked. A dropped or duplicated
# argument is a plausible typo, and `relation results: 0 rows` about a triple
# that IS in the KB reads as an engine-verified denial.
BAD_ARITY = [
    'relation("Marie Curie", "born_in", "Warsaw", X)?',   # the triple exists
    'path("Marie Curie", "Warsaw", "Poland")?',           # answered the path
    'relation("Marie Curie", "born_in")?',
    "relation()?",                                        # 0 args, not "bad quoting"
    'count("Marie Curie", "born_in", "extra")?',
    f'{POLICY_PREDICATE}("Marie Curie")?',
]

# Lines the gate rejects as QUERY_MALFORMED: right argument count, but an
# argument that is neither a Datalog variable nor a double-quoted string. Single
# quotes are not a string literal in this dialect, and a bare token is a variable
# only if it is capitalised — a lowercase identifier is the case a hand-written
# copy of the rule gets wrong, so every predicate carries one.
MALFORMED_SHAPE = [
    "relation(\"Marie Curie\", 'born_in', O)?",
    "relation(Marie Curie, born_in, O)?",
    'relation("Marie Curie", "born_in", warsaw)?',
    "path(Marie Curie, Warsaw)?",
    "path('Marie Curie', \"Warsaw\")?",
    'path("Marie Curie", warsaw)?',
    # Two DOUBLE-quoted constants, so `len(constants) >= 2` in the path renderer
    # is satisfied and the answer is blocked by the shape guard alone. Every
    # other malformed path here has fewer than two, so the renderer's own
    # condition would block it and the guard could be deleted unnoticed. Arity is
    # 2, so the arity guard does not cover this line either.
    'path("Marie Curie", "Warsaw"junk)?',
    "count(\"Marie Curie\", 'born_in')?",
    "count(Marie Curie, born_in)?",
    "count(x, y)?",
    f"{POLICY_PREDICATE}(Marie Curie, stale)?",
    f"{POLICY_PREDICATE}(\"Marie Curie\", 'stale')?",
    f"{POLICY_PREDICATE}(x, R)?",
]

REFUSED = BAD_ARITY + MALFORMED_SHAPE

WELL_FORMED = [
    'relation("Marie Curie", "born_in", O)?',
    "relation(S, R, O)?",
    'path("Marie Curie", "Warsaw")?',
    'count("Marie Curie", "born_in")?',
    "count(S, R)?",
    'count("Marie Curie", R)?',
    f'{POLICY_PREDICATE}("Marie Curie", R)?',
    f"{POLICY_PREDICATE}(E, R)?",
]


def _report_errors(line):
    errors, _warnings = rlc.validate_query(line, ENTITIES, POLICY_PREDICATES)
    return errors


def _report_answer(monkeypatch, line):
    monkeypatch.setattr(rlc, "query_lines", lambda: [line])
    return rlc.evaluate_queries(FACTS, INFERRED, POLICY_PREDICATES)


def _router_answer(monkeypatch, line):
    monkeypatch.setattr(ask_router, "policy_predicates", lambda program: POLICY_PREDICATES)
    monkeypatch.setattr(ask_router, "run_wirelog", lambda: INFERRED)
    return ask_router.evaluate(line, FACTS)


def _gate(line):
    return classify_query(line, FACTS, POLICY_PROGRAM)


class TestBadSignatureIsRefusedByEveryPath:
    """The three consumers reject the same lines, with the same wording.

    Comparing against the gate's own `reason` rather than a literal is what pins
    the ORDER of the two rules as well as their outcome: a path that checked
    shape before arity would still refuse `relation()?`, but would call it bad
    quoting instead of a zero-argument query, and these assertions fail.
    """

    @pytest.mark.parametrize("line", REFUSED)
    def test_report_reports_the_gates_message(self, line):
        _ok, _code, reason = _gate(line)
        assert _report_errors(line) == [f"{reason}: {line}"]

    @pytest.mark.parametrize("line", REFUSED)
    def test_report_renders_no_answer(self, monkeypatch, line):
        # The report must not answer a line it is calling an error: the wrong
        # number is what a reader takes away, not the Errors section.
        assert _report_answer(monkeypatch, line) == []

    @pytest.mark.parametrize("line", REFUSED)
    def test_router_raises_the_gates_message(self, monkeypatch, line):
        # `ask_router.py evaluate` calls evaluate() with no classify in front of
        # it, so the gate's verdict does not protect this path.
        _ok, _code, reason = _gate(line)
        with pytest.raises(NotImplementedError) as excinfo:
            _router_answer(monkeypatch, line)
        assert str(excinfo.value) == reason


class TestVerdictsAgreeOnAllThreePaths:
    @pytest.mark.parametrize("line", REFUSED + WELL_FORMED)
    def test_answerable_by_all_or_by_none(self, monkeypatch, line):
        gate_ok, code, reason = _gate(line)
        report_ok = not _report_errors(line)
        try:
            _router_answer(monkeypatch, line)
            router_ok = True
        except NotImplementedError:
            router_ok = False
        assert report_ok == router_ok == (gate_ok and code == QUERY_OK), (line, code, reason)


class TestWellFormedUnchanged:
    """Controls — these pass before AND after the fix (they are not pins)."""

    @pytest.mark.parametrize("line", WELL_FORMED)
    def test_well_formed_query_is_not_an_error(self, line):
        assert _report_errors(line) == []

    def test_relation_answer_unchanged(self, monkeypatch):
        assert _report_answer(monkeypatch, 'relation("Marie Curie", "born_in", O)?') == [
            "relation results: 2 rows; O=Warsaw; O=Poland"
        ]

    def test_path_answer_unchanged(self, monkeypatch):
        assert _report_answer(monkeypatch, 'path("Marie Curie", "Warsaw")?') == [
            "path Marie Curie -> Warsaw: Marie Curie -> Warsaw"
        ]

    def test_policy_answer_unchanged(self, monkeypatch):
        line = f'{POLICY_PREDICATE}("Marie Curie", R)?'
        assert _report_answer(monkeypatch, line) == [
            f"{POLICY_PREDICATE} results (query: {line}): 1 rows; R=stale"
        ]

    def test_arity_message_unchanged(self):
        assert _report_errors('count("Marie Curie")?') == [
            'count query must have subject and relation arguments: count("Marie Curie")?'
        ]


class TestCountResultLineNamesItsQuery:
    """A count result line echoes its query, as a policy result line does.

    Malformed lines drop out of the result list, so three query lines can leave
    two result lines, and a reader matching them up by position reads the third
    query's answer as the second's.
    """

    def test_result_line_carries_the_query(self, monkeypatch):
        assert _report_answer(monkeypatch, 'count("Marie Curie", "born_in")?') == [
            'count results (query: count("Marie Curie", "born_in")?): 2 (distinct objects)'
        ]

    def test_dropped_line_leaves_the_others_identifiable(self, monkeypatch):
        lines = [
            'count("Marie Curie", "born_in")?',
            "count(\"Marie Curie\", 'worked_at')?",  # malformed: no answer
            'count("Marie Curie", "worked_at")?',
        ]
        monkeypatch.setattr(rlc, "query_lines", lambda: lines)
        assert rlc.evaluate_queries(FACTS, INFERRED, POLICY_PREDICATES) == [
            f"count results (query: {lines[0]}): 2 (distinct objects)",
            f"count results (query: {lines[2]}): 1 (distinct objects)",
        ]


class TestPolicyDeclCannotHijackABuiltInPredicate:
    """A `.decl` in logic-policy.extra.dl must not re-route a built-in predicate.

    The report tests `predicate in policy_query_predicates` BEFORE `== "count"`,
    while classify_query tests count first, so a hand-authored `.decl count(...)`
    used to send the same line down the policy branch on one path and the count
    branch on the other — #328's divergence, reachable without touching either
    branch's code.
    """

    @pytest.mark.parametrize("predicate", ["count", "review_required", "relation", "path"])
    def test_declaring_a_built_in_does_not_make_it_a_policy_predicate(self, predicate):
        program = f".decl {predicate}(entity: symbol, reason: symbol)\n"
        assert common.policy_predicates(program) == set()

    def test_conflict_is_still_policy_declarable(self):
        # Control: `conflict` has no branch of its own and IS meant to be
        # declared by the policy program.
        assert common.policy_predicates(".decl conflict(entity: symbol, reason: symbol)\n") == {
            "conflict"
        }


class TestQueryEvaluationSectionDistinguishesEmptyFromRefused:
    """"no facts/query.dl found" must mean the file is absent, nothing else.

    The shape guard widened the set of lines that produce no result line, and
    SKILL.md tells the reader that placeholder means the `/factlog query` step
    was skipped — which would send them to re-run a step they already ran.
    """

    def _report_text(self, monkeypatch, tmp_path, lines):
        monkeypatch.setattr(rlc, "FACTS_DIR", tmp_path)
        monkeypatch.setattr(rlc, "ensure_dirs", lambda: None)
        monkeypatch.setattr(rlc, "load_accepted_facts", list)
        monkeypatch.setattr(rlc, "load_facts", list)
        monkeypatch.setattr(rlc, "load_logic_policy", str)
        monkeypatch.setattr(rlc, "policy_predicates", lambda program: set())
        monkeypatch.setattr(rlc, "run_wirelog", dict)
        monkeypatch.setattr(rlc, "query_lines", lambda: lines)
        rlc.main()
        return (tmp_path / "logic_report.txt").read_text(encoding="utf-8")

    def test_all_lines_refused_is_not_reported_as_a_missing_file(self, monkeypatch, tmp_path):
        text = self._report_text(monkeypatch, tmp_path, ["count(x, y)?"])
        assert "- no answerable queries in facts/query.dl (see Errors)" in text
        assert "no facts/query.dl found" not in text

    def test_absent_file_keeps_its_placeholder(self, monkeypatch, tmp_path):
        assert "- no facts/query.dl found" in self._report_text(monkeypatch, tmp_path, [])


class TestUnacceptedVocabularyStillWarns:
    """count now reaches the shared warning loop, as relation/path already did.

    The verified-zero answer is unchanged (pinned by tests/test_count_check.sh);
    what was missing was any signal at all that the query named something the
    engine does not carry.
    """

    def test_unknown_subject_warns_like_relation(self):
        _errors, warnings = rlc.validate_query(
            'count("Nobody", "born_in")?', ENTITIES, POLICY_PREDICATES
        )
        assert warnings == ["query references non-engine entity or relation: Nobody"]

    def test_known_vocabulary_does_not_warn(self):
        _errors, warnings = rlc.validate_query(
            'count("Marie Curie", "born_in")?', ENTITIES, POLICY_PREDICATES
        )
        assert warnings == []
