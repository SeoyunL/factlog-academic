# SPDX-License-Identifier: Apache-2.0
"""policy/attribute-relations.md promises the object of an attribute relation is
kept OUT of the entity set "so they do not show up as entities, path nodes, or
count subjects".  The entity axis honoured it; **path did not** — a literal was a
node in the entity graph, both in the python renderer (`dependency_path`,
`ask_router._reachable_pairs`) and in the emitted engine program, whose `edge`
rule accepted every relation/3 unfiltered (#329).

The exclusion rule is ONE definition, not three: a value is a path node exactly
when it is in ``entity_set``.  A literal that is *also* a subject somewhere, or
the object of some non-attribute relation, IS an entity — so it stays a legal
path node.  Those two cases are pinned below because they are what separates
"mirror entity_set" from the cruder "drop every attribute edge", and the cruder
rule would put `classify_query` (which gates path endpoints on entity_set) and
`dependency_path` back into disagreement.

``TestEngineAndRendererAgree`` runs the REAL assembled program through
``common.run_wirelog`` and compares it to the python renderer on the same facts.
Do not delete it: it is the only thing that makes fixing one path and forgetting
the other fail loudly.
"""
from __future__ import annotations

import pytest

import ask_router
import common
from factlog import common as fl_common

pyrewire = pytest.importorskip("pyrewire")


def R(subject: str, relation: str, object_: str) -> dict[str, str]:
    return {
        "subject": subject,
        "relation": relation,
        "object": object_,
        "status": "accepted",
        "source": "sources/a.md",
    }


# 갑봇 -통합-> 을서비스 -정식_운영-> 2030.1 (a literal), plus a second entity that
# carries the very same literal — the "meaningless connection" the promise rules out.
FACTS = [
    R("갑봇", "통합", "을서비스"),
    R("을서비스", "정식_운영", "2030.1"),
    R("병서비스", "정식_운영", "2030.1"),
]
ATTRS = {"정식_운영"}

# The literal is ALSO a subject -> it is a real entity -> a legal path node.
FACTS_LITERAL_IS_SUBJECT = FACTS + [R("2030.1", "후속", "정서비스")]
# The literal is ALSO the object of a NON-attribute relation -> likewise an entity.
FACTS_LITERAL_IS_ENTITY_OBJECT = FACTS + [R("무서비스", "참조", "2030.1")]
# An incomplete row (compile_facts reports it as an error but exits 0, so it DOES
# reach the engine). `entity_node(S) :- relation(S, R, O).` admits every subject
# unconditionally, empty string included, so the empty value is an engine path
# node and 갑봇 -> 을서비스 is an engine-derived path. The renderer must agree —
# see TestEmptyValuesAgreeWithTheEngine.  No attribute relation is involved.
FACTS_WITH_EMPTY_VALUES = [
    R("갑봇", "통합", ""),
    R("", "통합", "을서비스"),
]


@pytest.fixture
def attrs(monkeypatch):
    """Bind the ambient attribute-relation policy without touching the filesystem."""

    def _bind(names: set[str]) -> set[str]:
        monkeypatch.setattr(fl_common, "attribute_relations", lambda: set(names))
        return set(names)

    return _bind


def engine_path_pairs(facts: list[dict[str, str]], monkeypatch, tmp_path) -> set[tuple[str, str]]:
    """path/2 as the REAL emitted program computes it, via ``common.run_wirelog``.

    Everything run_wirelog reads is redirected at the module boundary — accepted
    atoms, policy text, typed relations, aliases — so the assembly under test is
    the production one and only its *inputs* are synthetic.
    """
    accepted_dl = tmp_accepted_dl(facts, tmp_path)
    monkeypatch.setattr(fl_common, "ACCEPTED_DL", accepted_dl)
    monkeypatch.setattr(fl_common, "load_accepted_facts", lambda: list(facts))
    monkeypatch.setattr(fl_common, "load_logic_policy", lambda: "")
    monkeypatch.setattr(fl_common, "typed_relations", lambda: {})
    monkeypatch.setattr(fl_common, "relation_aliases", lambda: {})
    inferred = fl_common.run_wirelog()
    return {tuple(row) for row in inferred["path"]}


def tmp_accepted_dl(facts, tmp_path):
    path = tmp_path / "accepted.dl"
    path.write_text("\n".join(common.dl_atom(row) for row in facts) + "\n", encoding="utf-8")
    return path


class TestPathAxis:
    def test_literal_is_not_a_path_node_in_the_python_renderer(self, attrs):
        # The reported reproduction: entity_set excluded the literal, path did not.
        declared = attrs(ATTRS)
        assert "2030.1" not in common.entity_set(FACTS, declared)
        assert common.dependency_path(FACTS, "갑봇", "2030.1") == []

    def test_literal_is_not_a_path_node_in_the_emitted_engine_program(self, attrs, monkeypatch, tmp_path):
        attrs(ATTRS)
        pairs = engine_path_pairs(FACTS, monkeypatch, tmp_path)
        assert ("을서비스", "2030.1") not in pairs
        assert ("갑봇", "2030.1") not in pairs
        assert ("갑봇", "을서비스") in pairs  # non-vacuous: the entity edge survives

    def test_entities_sharing_a_literal_no_longer_share_a_reachable_node(self, attrs):
        # 을서비스 and 병서비스 both carry the date 2030.1 and nothing else. While
        # the date is an entity-graph node their reachable sets overlap on it, and
        # `path(X, "2030.1")?` reports both as "connected to" the same node — a
        # relationship neither fact states.
        attrs(ATTRS)
        pairs = ask_router._reachable_pairs(FACTS)
        reach = lambda e: {t for (s, t) in pairs if s == e}  # noqa: E731
        assert not (reach("을서비스") & reach("병서비스"))

    def test_variable_path_query_does_not_offer_the_literal_as_a_target(self, attrs):
        # ask_router._reachable_pairs backs `path("갑봇", X)?`. classify_query's
        # endpoint guard never sees a variable, so this branch handed the literal
        # back inside an engine-routed POSITIVE answer.
        attrs(ATTRS)
        pairs = ask_router._reachable_pairs(FACTS)
        assert ("갑봇", "2030.1") not in pairs
        assert ("갑봇", "을서비스") in pairs


class TestEmptyValuesAgreeWithTheEngine:
    """REGRESSION PIN for the graph-membership test itself. #329 first routed the
    renderer's membership through ``entity_set``, whose truthiness guard exists to
    keep the empty string out of a VOCABULARY listing. Graph membership is a
    different question: the engine's ``entity_node(S) :- relation(S, R, O).`` has
    no such guard, so an empty value IS an engine path node. Reusing entity_set
    made the renderer drop edges the engine keeps, and `ask` then answered an
    engine-derived path with "no such fact (verified negative)".

    An incomplete row is reported by run_logic_check as an error but does not stop
    the pipeline (exit 0), so this input reaches users.
    """

    def test_empty_value_is_a_path_node_in_the_python_renderer(self, attrs):
        attrs(set())
        assert common.dependency_path(FACTS_WITH_EMPTY_VALUES, "갑봇", "을서비스") == [
            "갑봇", "", "을서비스",
        ]

    def test_empty_value_is_not_offered_as_vocabulary(self, attrs):
        # entity_set keeps its guard — this is the property the reuse conflated.
        attrs(set())
        assert "" not in common.entity_set(FACTS_WITH_EMPTY_VALUES, set())

    def test_engine_derives_the_transitive_pair(self, attrs, monkeypatch, tmp_path):
        # Non-vacuous half of the parity case: the engine really does derive it,
        # so a renderer that drops it turns a positive into a verified negative.
        attrs(set())
        assert ("갑봇", "을서비스") in engine_path_pairs(FACTS_WITH_EMPTY_VALUES, monkeypatch, tmp_path)


class TestLiteralThatIsAlsoAnEntity:
    """NEGATIVE CONTROLS — these pass before AND after the fix, by construction:
    nothing was excluded before. They exist to kill the cruder fix (drop every
    attribute edge outright). entity_set keeps a value that is a subject, or the
    object of a non-attribute relation, anywhere; path must keep it too —
    otherwise classify_query (which gates a path endpoint on entity_set) would
    admit the endpoint while dependency_path answered 'verified negative' for a
    pair the facts directly connect, which is the same divergence in a new place."""

    def test_literal_used_as_a_subject_stays_a_path_node(self, attrs):
        attrs(ATTRS)
        assert "2030.1" in common.entity_set(FACTS_LITERAL_IS_SUBJECT, ATTRS)
        assert common.dependency_path(FACTS_LITERAL_IS_SUBJECT, "을서비스", "정서비스") == [
            "을서비스", "2030.1", "정서비스",
        ]

    def test_literal_reached_by_a_non_attribute_relation_stays_a_path_node(self, attrs):
        attrs(ATTRS)
        assert "2030.1" in common.entity_set(FACTS_LITERAL_IS_ENTITY_OBJECT, ATTRS)
        assert common.dependency_path(FACTS_LITERAL_IS_ENTITY_OBJECT, "을서비스", "2030.1") == [
            "을서비스", "2030.1",
        ]


class TestEngineAndRendererAgree:
    """MUTATION GUARD, not a bug pin — measured to pass before the #329 fix too,
    because both paths agreed on the same WRONG answer. Its job is the future: it
    fails the moment someone changes WIRELOG_PROGRAM's entity_node/edge rules or
    common.dependency_graph without changing the other."""

    @pytest.mark.parametrize(
        "facts,declared",
        [
            (FACTS, ATTRS),
            (FACTS, set()),                              # nothing declared -> unchanged
            (FACTS_LITERAL_IS_SUBJECT, ATTRS),
            (FACTS_LITERAL_IS_ENTITY_OBJECT, ATTRS),
            (FACTS_WITH_EMPTY_VALUES, set()),
            (FACTS_WITH_EMPTY_VALUES, ATTRS),
        ],
    )
    def test_reachable_pairs_match_engine_path(self, facts, declared, attrs, monkeypatch, tmp_path):
        attrs(declared)
        assert ask_router._reachable_pairs(facts) == engine_path_pairs(facts, monkeypatch, tmp_path)


class TestNoAttributeRelationsIsUnchanged:
    def test_absent_policy_leaves_every_path(self, attrs):
        # Backward compatibility: with nothing declared entity_set == value_set,
        # so every relation is still an edge and the old answer stands.
        attrs(set())
        assert common.dependency_path(FACTS, "갑봇", "2030.1") == ["갑봇", "을서비스", "2030.1"]
        assert ("을서비스", "2030.1") in ask_router._reachable_pairs(FACTS)


class TestCountSubjectAxis:
    """CHARACTERIZATION PIN — NOT a regression pin. This third axis of the promise
    already held before #329 (classify_query gates a count subject on entity_set),
    so this test passes both before and after the fix. Kept so the axis cannot be
    dropped unnoticed while the other two are being changed."""

    def test_literal_is_not_an_accepted_count_subject(self, attrs):
        attrs(ATTRS)
        ok, code, reason = common.classify_query(
            'count("2030.1", "정식_운영")?', FACTS, policy_program=""
        )
        assert (ok, code) == (False, common.QUERY_ENTITY_NOT_ACCEPTED)
        assert "count subject is not an accepted entity" in reason


class TestAttributeObjectsStayQueryable:
    """CHARACTERIZATION PIN — NOT a regression pin. The "remain valid, verifiable
    relation-query objects" half of the same promise; passes before and after.
    Guards against a fix that pushes the literal out of relation queries too."""

    def test_relation_query_on_the_literal_object_still_resolves(self, attrs):
        attrs(ATTRS)
        ok, code, _ = common.classify_query(
            'relation("을서비스", "정식_운영", "2030.1")?', FACTS, policy_program=""
        )
        assert (ok, code) == (True, common.QUERY_OK)


class TestAttributeRelationProgramText:
    """The emitted ``attr_rel/1`` EDB block. Its docstring promises three things
    and none was exercised — the ``names`` argument had no caller and no test."""

    def test_no_declarations_emit_nothing(self):
        # This is what makes a KB without attribute-relations.md byte-identical
        # to WIRELOG_PROGRAM + policy + accepted.
        assert fl_common.attribute_relation_program(set()) == ""

    def test_names_are_emitted_sorted(self):
        # Sorted so the assembled program text is reproducible run to run.
        assert fl_common.attribute_relation_program({"나", "가", "다"}) == (
            '\nattr_rel("가").\nattr_rel("나").\nattr_rel("다").\n'
        )

    def test_a_name_carrying_a_quote_stays_a_legal_atom(self):
        # dl_string, not an f-string: an unescaped quote would be a ParseError
        # that rejects the WHOLE program (relation/3 included: a dead KB).
        emitted = fl_common.attribute_relation_program({'q"uote'})
        assert emitted == '\nattr_rel("q\\"uote").\n'
