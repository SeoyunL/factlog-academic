# SPDX-License-Identifier: Apache-2.0
"""Attribute relations do not become path nodes (#226).

The policy file `factlog init` hands the user says, in its own words:

    Objects of these relations are kept OUT of the entity set (so they do not show
    up as entities, path nodes, or count subjects) but remain valid, verifiable
    relation-query objects.

The entity list honoured that. The engine did not: `edge(S, O) :- relation(S, R, O)`
had no filter, so a dependency path hopped straight through a date —
`갑봇 -> 을서비스 -> 2030.1` — treating a literal as a waypoint. That is a false
guarantee in the file a user reads and trusts, about an artifact of the
deterministic engine.

The Python tracer (`dependency_graph`) must agree with the engine rule, because the
report asks the ENGINE whether a path exists and then asks the tracer to render it:
a divergence would print a route the engine says does not exist.
"""
from __future__ import annotations

import common
import factlog.common as fc
import pytest
import json
import os
import subprocess
import sys
from pathlib import Path



@pytest.fixture
def kb(tmp_path, monkeypatch):
    """A KB whose policy dir the loaders actually read.

    Patch `factlog.common`, not `common`: the latter is a re-export shim, so
    rebinding POLICY_DIR there never reaches the module that reads it.
    """
    (tmp_path / "policy").mkdir()
    monkeypatch.setattr(fc, "POLICY_DIR", tmp_path / "policy")
    return tmp_path


def _row(subject, relation, object_):
    return {"subject": subject, "relation": relation, "object": object_, "status": "accepted"}


FACTS = [
    _row("갑봇", "통합", "을서비스"),
    _row("을서비스", "정식_운영", "2030.1"),   # attribute: the object is a literal
    _row("을서비스", "의존", "병모듈"),
]


class TestDependencyGraph:
    def test_a_literal_is_not_a_path_node(self, kb):
        (kb / "policy" / "attribute-relations.md").write_text("정식_운영\n", encoding="utf-8")
        assert common.dependency_path(FACTS, "갑봇", "2030.1") == []

    def test_entity_paths_still_resolve(self, kb):
        (kb / "policy" / "attribute-relations.md").write_text("정식_운영\n", encoding="utf-8")
        assert common.dependency_path(FACTS, "갑봇", "병모듈") == ["갑봇", "을서비스", "병모듈"]

    def test_without_the_declaration_the_literal_is_still_a_node(self, kb):
        # Undeclared = a first-class entity, by design. This pins that the fix keys
        # on the DECLARATION and does not start guessing what a literal looks like.
        assert common.dependency_path(FACTS, "갑봇", "2030.1") == ["갑봇", "을서비스", "2030.1"]


class TestEngineProgram:
    def test_the_edge_rule_excludes_attribute_relations(self):
        assert "!literal_node(O)" in common.WIRELOG_PROGRAM

    def test_no_declarations_emit_no_attr_facts(self, kb):
        # A KB that declares nothing must produce a byte-identical program.
        assert common._attr_rel_facts() == ""

    def test_declared_relations_become_attr_rel_facts(self, kb):
        (kb / "policy" / "attribute-relations.md").write_text("정식_운영\n발행연도\n", encoding="utf-8")
        emitted = common._attr_rel_facts()
        assert 'attr_rel("정식_운영").' in emitted
        assert 'attr_rel("발행연도").' in emitted


# --- behavioural pins: run the ENGINE, not a substring check on the program ---
#
# The string assertions above pass even when the `.decl attr_rel` line is deleted:
# the engine then fails to load the relation, the filter dies silently, and the
# literal comes back as a path node. Only running the engine catches that.

def _kb(tmp_path, rows, policy, aliases=None):
    kb = tmp_path / "kb"
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        capture_output=True,
        check=True,
    )
    (kb / "sources" / "a.md").write_text("a\n")
    lines = ["subject,relation,object,source,status,confidence,note"]
    lines += [f"{s},{r},{o},sources/a.md,accepted,0.9," for s, r, o in rows]
    (kb / "facts" / "candidates.csv").write_text("\n".join(lines) + "\n")
    (kb / "policy" / "attribute-relations.md").write_text(policy)
    if aliases:
        (kb / "policy" / "relation-aliases.md").write_text(aliases)
    subprocess.run(
        [sys.executable, str(Path("tools") / "compile_facts.py")],
        capture_output=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
        check=True,
    )
    return kb


def _engine_and_tracer(kb):
    """(engine path set, tracer reachable set) for a KB, in a fresh interpreter."""
    script = r"""
import os, sys
from collections import deque
sys.path.insert(0, os.getcwd())
import factlog.common as c
facts = c.load_accepted_facts()
eng = sorted(tuple(t) for t in c.run_wirelog().get("path", set()))
g = c.dependency_graph(facts)
py = set()
for s0 in list(g):
    q, seen = deque(g[s0]), set()
    while q:
        n = q.popleft()
        if n in seen:
            continue
        seen.add(n); py.add((s0, n)); q.extend(g.get(n, []))
import json
print(json.dumps({"engine": eng, "tracer": sorted(py), "entities": sorted(c.entity_set(facts))}))
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
    )
    if proc.returncode != 0:
        # Skip ONLY when the engine is genuinely absent. An engine that IS present
        # and rejected OUR program is a failure, not a skip: deleting the
        # `.decl attr_rel` line makes the filter die silently, and a blanket skip
        # here reported that as green.
        missing = "No module named" in proc.stderr or "pyrewire" in proc.stderr.lower() and "version" in proc.stderr.lower()
        if missing:
            pytest.skip(f"pyrewire unavailable: {proc.stderr[-200:]}")
        raise AssertionError(f"engine rejected the program:\n{proc.stderr[-800:]}")
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    return (
        {tuple(x) for x in got["engine"]},
        {tuple(x) for x in got["tracer"]},
        set(got["entities"]),
    )


ROWS = [("갑", "통합", "을"), ("을", "정식_운영", "2030.1")]


def test_engine_drops_pure_literal_from_paths(tmp_path):
    eng, tracer, ents = _engine_and_tracer(_kb(tmp_path, ROWS, "정식_운영\n"))
    assert ("갑", "2030.1") not in eng
    assert "2030.1" not in ents
    assert eng == tracer  # the report asks the engine, then renders with the tracer


def test_literal_that_is_also_a_subject_stays_reachable(tmp_path):
    """entity_set admits it as an entity, so the engine must NOT deny the path.

    Otherwise the vocabulary gate lets the query through and the engine answers a
    false `verified negative` about a chain that genuinely exists.
    """
    rows = [*ROWS, ("2030.1", "비고", "메모")]
    eng, tracer, ents = _engine_and_tracer(_kb(tmp_path, rows, "정식_운영\n"))
    assert "2030.1" in ents
    assert ("갑", "2030.1") in eng
    assert eng == tracer


def test_filter_survives_a_relation_alias(tmp_path):
    """Declaring the canonical must filter facts stored under a surface alias."""
    rows = [("갑", "통합", "을"), ("을", "게재연도", "2020")]
    kb = _kb(
        tmp_path, rows, "published_year\n", aliases="- `게재연도` -> `published_year`\n"
    )
    eng, tracer, ents = _engine_and_tracer(kb)
    assert "2020" not in ents
    assert ("갑", "2020") not in eng
    assert eng == tracer


def test_quoted_relation_name_does_not_crash_the_engine(tmp_path):
    """A quote in a declared name emitted `attr_rel(""x"")` and killed the program."""
    eng, tracer, _ = _engine_and_tracer(_kb(tmp_path, ROWS, '- "정식_운영"\n'))
    assert eng == tracer  # reaching here at all means the engine parsed the program
