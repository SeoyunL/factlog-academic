# SPDX-License-Identifier: Apache-2.0
"""What a reserved bare FACT does to the engine once it reaches it (#358).

The guard is what keeps such a fact out of the program; these tests say why that
matters, by handing the engine the policy text the guard now refuses and reading
the answer back out of the REAL assembled program.

``attr_rel/1`` is pure EDB — the engine has no rules for it — so an in-program
bare fact is honoured and joins the atoms injected from
policy/attribute-relations.md. One line therefore reclassifies a relation as
"attribute", drops its object out of ``entity_node``, and removes the edge:
``path("갑봇","병문서")`` goes from a derived path to nothing, at rc=0.

That is not a general property of bare facts. A relation with rules (IDB) ignores
them, which is why probing this with an inert predicate reads as "no consequence"
when there is one. Both directions are pinned below so the distinction is not
re-derived from a single probe next time.
"""
from __future__ import annotations

import pytest

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


FACTS = [
    R("갑봇", "통합", "을서비스"),
    R("을서비스", "정식_운영", "2030.1"),
    R("갑봇", "참조", "병문서"),
]
# The policy text the guard refuses. Written as it was measured: one space before
# the terminating dot is the entire difference from the standalone form.
FUSED = 'p(X) :- relation(X,_,_) .attr_rel("참조").\n'


def engine_paths(policy_text, monkeypatch, tmp_path):
    """path/2 from the real emitted program, with *policy_text* as the policy."""
    accepted = tmp_path / "accepted.dl"
    accepted.write_text(
        "\n".join(common.dl_atom(row) for row in FACTS) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(fl_common, "ACCEPTED_DL", accepted)
    monkeypatch.setattr(fl_common, "load_accepted_facts", lambda: list(FACTS))
    # Bypasses the guard on purpose: load_logic_policy is where it runs, and the
    # point here is what happens downstream of it.
    monkeypatch.setattr(fl_common, "load_logic_policy", lambda: policy_text)
    monkeypatch.setattr(fl_common, "typed_relations", lambda: {})
    monkeypatch.setattr(fl_common, "relation_aliases", lambda: {})
    monkeypatch.setattr(fl_common, "attribute_relations", lambda: {"정식_운영"})
    return {tuple(row) for row in fl_common.run_wirelog()["path"]}


class TestAFusedAttrRelFactChangesTheAnswer:
    def test_clean_policy_derives_the_path(self, monkeypatch, tmp_path):
        assert ("갑봇", "병문서") in engine_paths("", monkeypatch, tmp_path)

    def test_the_fused_fact_removes_it(self, monkeypatch, tmp_path):
        # The report renders this as `- path 갑봇 -> 병문서: (not found)` — which
        # reads as "the facts do not connect them" — with errors: 0.
        assert ("갑봇", "병문서") not in engine_paths(FUSED, monkeypatch, tmp_path)

    def test_the_standalone_fact_does_the_same_thing(self, monkeypatch, tmp_path):
        # Same fact, no fusion: identical effect on the engine. The guard has
        # always refused this form, which is what made the fused one worth fixing
        # rather than tolerating.
        assert ("갑봇", "병문서") not in engine_paths(
            'attr_rel("참조").\n', monkeypatch, tmp_path
        )

    def test_the_guard_refuses_the_text_these_tests_feed_the_engine(self):
        # Ties this file back to the guard: production cannot reach the states
        # above, because load_logic_policy raises first.
        with pytest.raises(fl_common.FactlogError, match="attr_rel is a reserved engine"):
            fl_common._assert_no_reserved_head(FUSED)

    def test_an_unrelated_bare_fact_leaves_paths_alone(self, monkeypatch, tmp_path):
        # CONTROL — it is the reserved predicate that matters, not "a bare fact".
        assert ("갑봇", "병문서") in engine_paths(
            '.decl note(a: symbol)\nnote("x").\n', monkeypatch, tmp_path
        )


class TestEdbAndIdbBareFactsDiffer:
    """Why the earlier "inert" reading was wrong: it depends on the predicate."""

    def test_a_bare_fact_for_a_pure_edb_relation_is_honoured(self, monkeypatch, tmp_path):
        # attr_rel has no rules in WIRELOG_PROGRAM.
        assert "attr_rel" not in _rule_heads(fl_common.WIRELOG_PROGRAM)
        assert ("갑봇", "병문서") not in engine_paths(FUSED, monkeypatch, tmp_path)

    def test_a_bare_fact_for_a_derived_relation_is_ignored(self, monkeypatch, tmp_path):
        # entity_node HAS rules, so an in-program fact for it does not add a node
        # — the same probe on this predicate shows nothing and would suggest,
        # wrongly, that the whole class is inert.
        assert "entity_node" in _rule_heads(fl_common.WIRELOG_PROGRAM)
        before = engine_paths("", monkeypatch, tmp_path)
        after = engine_paths('entity_node("유령").\n', monkeypatch, tmp_path)
        assert before == after


def _rule_heads(program: str) -> set[str]:
    """Predicate names that appear as a rule head (left of ':-') in *program*."""
    import re

    return {
        m.group(1)
        for m in re.finditer(
            r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)\s*:-", program
        )
    }
