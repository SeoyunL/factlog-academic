# SPDX-License-Identifier: Apache-2.0
"""Regression tests: path("X","X")? must be a verified negative unless a real
cycle exists (#256).

The wirelog engine defines path/2 only over edges:
    path(S,O) :- edge(S,O).
    path(S,O) :- edge(S,M), path(M,O).
so a path requires >= 1 edge — a reflexive path("X","X") is true ONLY when a
genuine cycle leads back to X, never trivially. `dependency_path` returned the
zero-edge `[start]` whenever start == target, so `classify_query` routed
path("X","X")? as an engine POSITIVE and ask_router rendered it under
"VERIFIED — engine" — presenting as engine-verified a result the engine would
never emit (and one the variable-branch `_reachable_pairs` correctly omits).
"""
from __future__ import annotations

import common


def _linear():
    # claude -> mcp -> server  (no cycle; 'server' is a leaf)
    return [
        {"subject": "claude", "relation": "uses", "object": "mcp"},
        {"subject": "mcp", "relation": "uses", "object": "server"},
    ]


class TestReflexivePathIsNegative:
    def test_dependency_path_no_trivial_self_path_on_leaf(self):
        assert common.dependency_path(_linear(), "server", "server") == []

    def test_dependency_path_no_trivial_self_path_on_interior(self):
        assert common.dependency_path(_linear(), "claude", "claude") == []

    def test_classify_reflexive_passes_the_vocabulary_gate(self):
        # #303: the gate no longer re-derives reachability (it has no engine pairs and
        # its python mirror cannot see policy edges). A reflexive query over an accepted
        # entity now passes the gate as QUERY_OK -- the verified NEGATIVE is the engine's
        # own empty result, asserted by the sibling below via ask_router.evaluate.
        ok, code, _ = common.classify_query('path("server","server")?', _linear())
        assert code == common.QUERY_OK
        assert ok is True

    def test_ask_router_reflexive_count_zero(self):
        # This is where #256's guarantee now lives: the ENGINE emits no reflexive path
        # without a real cycle, so evaluate() returns 0 rows -- a verified negative.
        import ask_router

        assert ask_router.evaluate('path("server","server")?', _linear())["count"] == 0


class TestGenuineCycleStillPositive:
    def test_real_cycle_returns_a_path(self):
        # a -> b -> a : path(a,a) IS derivable by the engine, must stay positive.
        facts = [
            {"subject": "a", "relation": "r", "object": "b"},
            {"subject": "b", "relation": "r", "object": "a"},
        ]
        p = common.dependency_path(facts, "a", "a")
        assert p and p[0] == "a" and p[-1] == "a" and len(p) >= 3

    def test_self_edge_returns_a_path(self):
        # a -> a : a genuine self-edge is a real 1-edge path.
        facts = [{"subject": "a", "relation": "r", "object": "a"}]
        assert common.dependency_path(facts, "a", "a") != []

    def test_normal_forward_path_unaffected(self):
        assert common.dependency_path(_linear(), "claude", "server") == ["claude", "mcp", "server"]
