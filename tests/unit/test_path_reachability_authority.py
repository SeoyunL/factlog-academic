# SPDX-License-Identifier: Apache-2.0
"""Reachability for a path query is the ENGINE's call alone; the gate stops guessing (#303).

Two truth sources decided whether a path exists:

* the GATE (``classify_query``) re-derived it with ``dependency_path`` — a python mirror
  of the STANDARD edge/path rules over the accepted facts;
* the MATCHER (``path_query_rows``) reads ``inferred["path"]`` — the engine's fixpoint.

On a pair reachable only through a fact a ``logic-policy.extra.dl`` rule/fact adds, the
python mirror cannot see it, so the gate answered FACT_ABSENT (a *verified negative*)
while the matcher answered "reachable" — the two contradicted on one query. And because
``cmd_render`` skips the engine when the classification is negative, that false negative
reached the USER's answer. The fix removes the gate's reachability guess: vocabulary is
still validated (entities accepted), and whether a path EXISTS is left to the engine.
A true negative is then the engine's own empty result, rendered verified-empty.

This divergence is REAL and reproducible on a live KB, not merely structural. The
reserved-head guard (#226, ``_assert_no_canonical_head``) forbids a policy from heading
``edge``/``path``/``canonical``/``attr_rel`` — but NOT ``relation``. So a bare fact
``relation("C", "uses", "A").`` in ``logic-policy.extra.dl`` (between two already-accepted
entities) compiles cleanly, the engine derives the path pair (C, A), yet
``dependency_path`` — built from ``accepted.dl`` alone, which never carries the policy
fact — returns ``[]``. Before this change the gate called that a verified negative and
``cmd_render`` shipped it to the user without running the engine. ``TestEngineEndToEnd``
below reproduces exactly this over a real engine; the pure-python classes pin the same
behaviour at the function boundary with an explicit engine pair.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

import ask_router
import pytest
from factlog.common import (
    QUERY_ENTITY_NOT_ACCEPTED,
    QUERY_OK,
    classify_query,
    dependency_path,
    path_query_rows,
    query_args,
    validate_candidate_query,
)

nfc = lambda s: unicodedata.normalize("NFC", s)  # noqa: E731
nfd = lambda s: unicodedata.normalize("NFD", s)  # noqa: E731


def _linear():
    # A -> B -> C (no cycle; C is a leaf, nothing leaves C)
    return [
        {"subject": "A", "relation": "uses", "object": "B"},
        {"subject": "B", "relation": "uses", "object": "C"},
    ]


@pytest.fixture(autouse=True)
def _empty_policy(tmp_path, monkeypatch):
    """No aliases/policy on disk, so classify reads a clean (empty) vocabulary."""
    import factlog.common as fc

    (tmp_path / "policy").mkdir()
    monkeypatch.setattr(fc, "POLICY_DIR", tmp_path / "policy")


class TestGateDelegatesReachability:
    """The gate validates vocabulary but no longer asserts a path is ABSENT."""

    def test_accepted_but_unreachable_pair_passes_the_gate(self):
        # C and A are accepted; there is no route C -> A. The gate used to answer
        # FACT_ABSENT here; now it passes as QUERY_OK and leaves the verdict to the engine.
        assert dependency_path(_linear(), "C", "A") == []  # the python mirror sees no route
        ok, code, _ = classify_query('path("C", "A")?', _linear())
        assert (ok, code) == (True, QUERY_OK)

    def test_reflexive_no_cycle_passes_the_gate(self):
        assert dependency_path(_linear(), "A", "A") == []
        ok, code, _ = classify_query('path("A", "A")?', _linear())
        assert (ok, code) == (True, QUERY_OK)

    def test_a_reachable_pair_still_passes(self):
        ok, code, _ = classify_query('path("A", "C")?', _linear())
        assert (ok, code) == (True, QUERY_OK)

    def test_vocabulary_is_still_gated(self):
        # Delegation is only about reachability; an UNACCEPTED entity is still rejected.
        ok, code, _ = classify_query('path("A", "없는노드")?', _linear())
        assert (ok, code) == (False, QUERY_ENTITY_NOT_ACCEPTED)


class TestAskNoLongerFakesAVerifiedNegative:
    """ask_router.classify: an unreachable-but-accepted pair is no longer marked a
    verified negative (negative=True) by the gate — the engine decides."""

    def test_unreachable_pair_is_not_marked_negative(self):
        d = ask_router.classify('path("C", "A")?', _linear())
        assert d["route"] == "engine"
        assert d["negative"] is False  # was True: the false verified-negative this fixes

    def test_reflexive_is_not_marked_negative(self):
        d = ask_router.classify('path("A", "A")?', _linear())
        assert d["route"] == "engine"
        assert d["negative"] is False


class TestGateAndMatcherAgreeOnEngineOnlyPair:
    """The issue's scenario at the seam: an engine pair the python mirror lacks (see the
    module docstring for the live-KB form — a bare ``relation`` fact in extra.dl). The
    gate now says QUERY_OK (no reachability guess) and the matcher says reachable — they
    agree. Before the fix the gate said FACT_ABSENT while the matcher said reachable."""

    def test_no_contradiction_on_a_policy_only_pair(self):
        facts = _linear()
        engine_only = {("C", "A")}  # reachable per the engine, absent from the python graph
        assert dependency_path(facts, "C", "A") == []
        # gate: accepts (vocabulary ok, no reachability assertion)
        ok, code, _ = classify_query('path("C", "A")?', facts)
        assert (ok, code) == (True, QUERY_OK)
        # matcher: the engine proved the pair, so it is reachable -- and rendered as the pair
        assert path_query_rows(query_args('path("C", "A")?'), facts, engine_only) == [["C", "A"]]


class TestValidateCandidateQuery:
    def test_a_well_formed_negative_path_validates(self):
        # The self-correction loop must not try to repair a query that is simply a
        # (to-be) verified negative; it is well formed, so validation passes.
        ok, reason = validate_candidate_query('path("C", "A")?', _linear())
        assert ok is True


class TestNfcFoldStillHolds:
    """#299 fold is untouched: an NFD-stored entity still meets an NFC path constant at
    the gate, which now passes as QUERY_OK."""

    def test_nfd_fact_nfc_query_passes_the_gate(self):
        A = "관찰"
        facts = [{"subject": nfd(A), "relation": "uses", "object": "B"},
                 {"subject": "B", "relation": "uses", "object": "C"}]
        ok, code, _ = classify_query(f'path("{nfc(A)}", "C")?', facts)
        assert (ok, code) == (True, QUERY_OK)


# --- Engine-backed end-to-end (delegation renders true negatives correctly) ----
try:
    import pyrewire  # noqa: F401

    _HAVE_ENGINE = True
except ImportError:  # pragma: no cover - depends on the install
    _HAVE_ENGINE = False


@pytest.mark.skipif(not _HAVE_ENGINE, reason="pyrewire not installed")
class TestEngineEndToEnd:
    """Over a REAL KB and engine: a standard-KB true-negative path still renders as a
    VERIFIED — engine empty result (never a wiki fallback), and a positive path renders
    its route. This is the delegation path -- the verdict now comes from running the
    engine (evaluate), not from the gate's guess."""

    def _render(self, tmp_path, query, extra_dl=None):
        kb = tmp_path / "kb"
        subprocess.run(
            [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
            capture_output=True, check=True,
        )
        (kb / "sources" / "a.md").write_text("a\n")
        rows = [("A", "uses", "B"), ("B", "uses", "C")]
        lines = ["subject,relation,object,source,status,confidence,note"]
        lines += [f"{s},{r},{o},sources/a.md,accepted,0.9," for s, r, o in rows]
        (kb / "facts" / "candidates.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if extra_dl is not None:
            (kb / "policy" / "logic-policy.extra.dl").write_text(extra_dl, encoding="utf-8")
        subprocess.run(
            [sys.executable, str(Path("tools") / "compile_facts.py")],
            capture_output=True,
            env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
            check=True,
        )
        # Replicate cmd_render's engine branch so the assertion is on the real render:
        # classify -> (negative ? render([]) : render(evaluate.rows)). Also report the
        # gate/matcher pieces so a test can pin the full 4-way (gate, matcher, ask, render).
        script = r"""
import os, sys, json
sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.path.join(os.getcwd(), "tools"))
import factlog.common as c
import ask_router
facts = c.load_accepted_facts()
q = %r
d = ask_router.classify(q, facts)
if d["route"] == "engine" and d["negative"]:
    out = ask_router.render_engine_answer(q, [])
else:
    res = ask_router.evaluate(q, facts)
    out = ask_router.render_engine_answer(q, res["rows"])
gate = c.classify_query(q, facts, policy_program=c.load_logic_policy())[1]
matcher = c.path_query_rows(c.query_args(q), facts, c.run_wirelog()["path"])
dep = c.dependency_path(facts, "C", "A")
print(json.dumps({"decision": d, "render": out, "gate": gate, "matcher": matcher, "dep_path": dep}))
""" % (query,)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True,
            env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
            check=True,
        )
        return json.loads(proc.stdout)

    def test_true_negative_path_renders_verified_empty(self, tmp_path):
        data = self._render(tmp_path, 'path("C", "A")?')
        # Delegated: the gate did not mark it negative; the engine ran and found nothing.
        assert data["decision"]["negative"] is False
        assert data["decision"]["route"] == "engine"
        assert "VERIFIED — engine" in data["render"]
        assert "rows: 0" in data["render"]  # a verified-empty result, not a wiki fallback

    def test_positive_path_renders_the_route(self, tmp_path):
        data = self._render(tmp_path, 'path("A", "C")?')
        assert "VERIFIED — engine" in data["render"]
        assert "rows: 1" in data["render"]
        assert "A, B, C" in data["render"]

    def test_policy_only_pair_is_no_longer_a_false_negative(self, tmp_path):
        """The reviewer's live-KB reproduction, end to end. A bare ``relation`` fact in
        logic-policy.extra.dl makes (C, A) reachable in the ENGINE but not in the python
        graph (accepted.dl has no such fact). Before #303 the gate answered FACT_ABSENT
        and cmd_render shipped a verified negative to the user; now the four views agree:
        the gate passes (QUERY_OK), the engine proves the pair, and ask renders it
        positive. This is the false verified-negative the fix removes -- and it is real."""
        data = self._render(tmp_path, 'path("C", "A")?', extra_dl='relation("C", "uses", "A").\n')
        # The seam: engine reaches (C, A), python graph does not.
        assert data["dep_path"] == []          # python mirror sees no route
        assert data["matcher"] == [["C", "A"]]  # engine proved the pair
        # The gate no longer contradicts the matcher: it passes instead of FACT_ABSENT.
        assert data["gate"] == QUERY_OK
        assert data["decision"]["route"] == "engine"
        assert data["decision"]["negative"] is False  # was True -> the false verified-negative
        # And the user sees a POSITIVE engine answer naming the pair, not a fake negative.
        assert "VERIFIED — engine" in data["render"]
        assert "rows: 1" in data["render"]
        assert "C, A" in data["render"]
