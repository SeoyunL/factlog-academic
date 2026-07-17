# SPDX-License-Identifier: Apache-2.0
"""Catch a silently-emptied engine input: disk has facts, the engine holds none (#308).

The logic report's `engine facts` line counts DISK rows (load_accepted_facts), so it
cannot see the engine's own relation extent emptying underneath it -- the blind spot
behind #305's vacuous pass (report said `engine facts: 7` while the engine evaluated over
nothing). WIRELOG_PROGRAM now carries a WITNESS IDB, `relation_alive(S) :- relation(S,R,O)`,
which surfaces as a step() delta (relation itself is EDB and never does) and so reflects
the engine's POST-FIXPOINT relation extent -- empty whether the atoms were dropped at
parse time OR at the fixpoint. `run_logic_check.engine_relation_gap` compares it against
the disk fact count. It is the LAST NET: #305's guard rejects the known causes (relation
rule-head / .decl re-declaration) loudly at policy load; this catches an unknown cause
that slips past. Conservative: only the TOTAL-emptying (0) case fires.

`relation_alive` is a reserved engine predicate (a policy heading it would union fake
tuples into the witness), pinned by the reserved-vocabulary regression below.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from factlog.common import FactlogError, _assert_no_canonical_head
from run_logic_check import engine_relation_gap


def _facts(n):
    return [{"subject": f"s{i}", "relation": "uses", "object": f"o{i}"} for i in range(n)]


def _witness(*subjects):
    return {(s,) for s in subjects}


class TestEngineRelationGapHelper:
    """Pure function -- no engine, runs everywhere."""

    def test_disk_facts_but_empty_witness_is_an_error(self):
        msg = engine_relation_gap(_facts(7), {"relation_alive": set(), "path": {("s0", "o0")}})
        assert msg is not None
        assert "7 accepted fact(s) on disk" in msg
        assert "0 relation atoms" in msg

    def test_a_missing_witness_key_counts_as_empty(self):
        assert engine_relation_gap(_facts(3), {"path": set()}) is not None

    def test_a_live_witness_is_fine(self):
        # The witness is keyed on subject, so its size is the distinct-subject count --
        # 1 subject can back several facts. Only its EMPTINESS matters here.
        assert engine_relation_gap(_facts(7), {"relation_alive": _witness("s0")}) is None

    def test_an_empty_kb_does_not_fire(self):
        assert engine_relation_gap([], {"relation_alive": set()}) is None
        assert engine_relation_gap([], {}) is None


class TestWitnessIsReserved:
    """A policy that heads the witness would poison it (union fake tuples -> a false
    negative on the net), so it is rejected like the other engine predicates (#305 form)."""

    def test_a_rule_head_on_the_witness_is_rejected(self):
        with pytest.raises(FactlogError):
            _assert_no_canonical_head('relation_alive(S) :- relation(S, "x", "y").')

    def test_a_bare_fact_of_the_witness_is_rejected(self):
        with pytest.raises(FactlogError):
            _assert_no_canonical_head('relation_alive("s").')

    def test_a_decl_of_the_witness_is_rejected(self):
        with pytest.raises(FactlogError):
            _assert_no_canonical_head(".decl relation_alive(s: symbol)\n")

    def test_a_body_reference_to_the_witness_is_allowed(self):
        # Reading the witness in a rule body (heading a user predicate) is fine.
        _assert_no_canonical_head('flagged(S, "f") :- relation_alive(S).')


# --- Engine-backed seam + contract -------------------------------------------
try:
    import pyrewire  # noqa: F401

    _HAVE_ENGINE = True
except ImportError:  # pragma: no cover - depends on the install
    _HAVE_ENGINE = False


def _kb(tmp_path):
    kb = tmp_path / "kb"
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        capture_output=True, check=True,
    )
    (kb / "sources" / "a.md").write_text("a\n")
    rows = [("Claude Code", "developed_by", "Anthropic"), ("Anthropic", "develops", "Claude Code")]
    lines = ["subject,relation,object,source,status,confidence,note"]
    lines += [f"{s},{r},{o},sources/a.md,accepted,0.9," for s, r, o in rows]
    (kb / "facts" / "candidates.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    subprocess.run(
        [sys.executable, str(Path("tools") / "compile_facts.py")],
        capture_output=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
        check=True,
    )
    return kb


def _run(kb, script):
    return subprocess.run(
        [sys.executable, "-c", "import os, sys; sys.path.insert(0, os.getcwd()); "
         "sys.path.insert(0, os.path.join(os.getcwd(), 'tools'))\n" + script],
        capture_output=True, text=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
    )


@pytest.mark.skipif(not _HAVE_ENGINE, reason="pyrewire not installed")
class TestRunLogicCheckSeam:
    """The seam #305's guard bypasses: with the witness forced empty (standing in for any
    unknown cause), run_logic_check must error and exit non-zero -- not report a vacuous
    'no contradictions' over an emptied engine."""

    def test_emptied_witness_makes_check_exit_nonzero(self, tmp_path):
        kb = _kb(tmp_path)
        script = (
            "import run_logic_check as rlc\n"
            "_orig = rlc.run_wirelog\n"
            "def _fake():\n"
            "    inf = _orig()\n"
            "    inf['relation_alive'] = set()\n"
            "    return inf\n"
            "rlc.run_wirelog = _fake\n"
            "print('RC', rlc.main())\n"
        )
        out = _run(kb, script)
        assert "RC 1" in out.stdout, out.stdout + out.stderr
        report = (kb / "facts" / "logic_report.txt").read_text(encoding="utf-8")
        assert "engine input gap" in report
        assert "errors: 1" in report

    def test_healthy_kb_does_not_trip_the_gap(self, tmp_path):
        kb = _kb(tmp_path)
        out = _run(kb, "import run_logic_check as rlc\nprint('RC', rlc.main())")
        assert "RC None" in out.stdout, out.stdout + out.stderr
        report = (kb / "facts" / "logic_report.txt").read_text(encoding="utf-8")
        assert "engine input gap" not in report
        assert "errors: 0" in report


@pytest.mark.skipif(not _HAVE_ENGINE, reason="pyrewire not installed")
class TestWitnessContract:
    """The signal only works if the witness genuinely surfaces. Pin that contract: a
    healthy KB's inferred["relation_alive"] is non-empty (the == 0 test is meaningful only
    if a live engine produces a non-empty witness)."""

    def test_run_wirelog_surfaces_the_witness(self, tmp_path):
        kb = _kb(tmp_path)
        out = _run(kb, "import factlog.common as c\n"
                       "inf = c.run_wirelog()\n"
                       "print(len(inf.get('relation_alive', set())))")
        assert int(out.stdout.strip()) > 0, out.stdout + out.stderr


@pytest.mark.skipif(not _HAVE_ENGINE, reason="pyrewire not installed")
class TestWitnessCatchesFixpointDrop:
    """The witness's whole reason for existing over a parse-time count: it reflects the
    POST-FIXPOINT extent. A relation rule-head makes pyrewire flip relation to IDB and drop
    every accepted fact AT THE FIXPOINT (compile rc=0) -- a parse-time reader
    (preview_inline_facts) still counts the inline facts and misses it, but the witness,
    projected from relation after the fixpoint, goes EMPTY. #305's guard rejects this exact
    program at policy load; here we feed the poisoned program STRAIGHT to the engine (the
    seam that guard closes) to pin, on a real engine, that the witness would catch a
    fixpoint drop from any unknown cause that reproduces it."""

    def test_a_rule_head_fixpoint_drop_empties_the_witness(self, tmp_path):
        kb = _kb(tmp_path)
        script = (
            "import factlog.common as c\n"
            "from pyrewire import EasySession\n"
            "from collections import defaultdict\n"
            "acc = c.ACCEPTED_DL.read_text()\n"
            "rows = c.load_accepted_facts()\n"
            "def witness(prog):\n"
            "    s = EasySession(prog)\n"
            "    for r in rows:\n"
            "        s.intern(r['subject']); s.intern(r['relation']); s.intern(r['object'])\n"
            "    inf = defaultdict(set)\n"
            "    for name, row, diff in s.step():\n"
            "        if diff > 0:\n"
            "            inf[name].add(tuple(str(c.decode_wirelog_value(s, v)) for v in row))\n"
            "    s.close()\n"
            "    return len(inf.get('relation_alive', set()))\n"
            "healthy = witness(c.WIRELOG_PROGRAM + chr(10) + acc)\n"
            "poison = c.WIRELOG_PROGRAM + chr(10) + 'relation(X,\"d\",Y) :- relation(Y,\"e\",X).' + chr(10) + acc\n"
            "print(healthy, witness(poison))\n"
        )
        out = _run(kb, script)
        healthy, poisoned = out.stdout.split()
        assert int(healthy) > 0, out.stdout + out.stderr   # a live engine populates the witness
        assert int(poisoned) == 0, out.stdout + out.stderr  # the fixpoint drop empties it


@pytest.mark.skipif(not _HAVE_ENGINE, reason="pyrewire not installed")
class TestPoisonedPolicyTripsGapOnRealEngine:
    """The reviewer's measurement path, made self-standing. Poison ``load_logic_policy``
    (a relation rule-head, which the #305 guard rejects -- but that guard LIVES inside
    load_logic_policy, so monkeypatching the function bypasses it) so the REAL
    ``run_wirelog`` assembles and runs the poisoned program on the engine. The witness
    then genuinely empties at the fixpoint and ``engine_relation_gap`` fires -- the whole
    reason the witness beats a parse-time count, pinned end to end through the production
    run_wirelog path rather than a hand-built program."""

    def test_poisoned_policy_empties_witness_and_trips_gap(self, tmp_path):
        kb = _kb(tmp_path)
        script = (
            "import factlog.common as c, run_logic_check as rlc\n"
            "poison = lambda: 'relation(X, \"d\", Y) :- relation(Y, \"e\", X).'\n"
            "c.load_logic_policy = poison\n"       # run_wirelog reads the module global
            "rlc.load_logic_policy = poison\n"     # run_logic_check imported the name
            "inf = c.run_wirelog()\n"
            "facts = c.load_accepted_facts()\n"
            "alive = len(inf.get('relation_alive', set()))\n"
            "gap = rlc.engine_relation_gap(facts, inf)\n"
            "print(alive, gap is not None)\n"
        )
        out = _run(kb, script)
        alive, tripped = out.stdout.split()
        assert alive == "0", out.stdout + out.stderr       # a real fixpoint drop empties the witness
        assert tripped == "True", out.stdout + out.stderr  # and the gap fires over the emptied engine


@pytest.mark.skipif(not _HAVE_ENGINE, reason="pyrewire not installed")
class TestNoRegressionOnDuplicates:
    """A KB whose candidates carry duplicate rows (deduped at compile) must not trip the
    gap -- the engine still holds the deduped relation atoms, so the witness is live."""

    def test_dedup_kb_reports_no_gap(self, tmp_path):
        kb = tmp_path / "kb"
        subprocess.run(
            [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
            capture_output=True, check=True,
        )
        (kb / "sources" / "a.md").write_text("a\n")
        lines = ["subject,relation,object,source,status,confidence,note",
                 "A,uses,B,sources/a.md,accepted,0.9,",
                 "A,uses,B,sources/a.md,accepted,0.9,"]
        (kb / "facts" / "candidates.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        subprocess.run(
            [sys.executable, str(Path("tools") / "compile_facts.py")],
            capture_output=True,
            env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
            check=True,
        )
        out = _run(kb, "import run_logic_check as rlc\nprint('RC', rlc.main())")
        assert "RC None" in out.stdout, out.stdout + out.stderr
        assert "engine input gap" not in (kb / "facts" / "logic_report.txt").read_text(encoding="utf-8")
