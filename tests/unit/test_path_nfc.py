# SPDX-License-Identifier: Apache-2.0
"""A path query meets an NFD-stored fact whatever normal form the constant is in (#299).

#296 folded the value-comparison chokepoint (``_canonical_value``) for relation/count/
object, but ``path`` never reaches it: ``path_query_rows`` compares the query constant
against the ENGINE's interned path/2 pairs, and ``dependency_path`` against the raw
python graph nodes. The engine interns accepted.dl VERBATIM, so an NFD-authored entity
stays NFD in both, and an NFC query constant missed it — the report and ask agreed, but
both were wrong (a route that exists went unfound).

The fix folds BOTH sides at the comparison (gate AND matcher together, the #296 rework
lesson): the engine pair stays the truth of reachability, but the query constant and the
pair are compared on their canonical form, and the STORED (verbatim) spelling is rendered
for provenance. NFC-only data folds to itself, so nothing about an NFC KB moves.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

import ask_router
import factlog.common as c
import pytest
from factlog.common import dependency_path, path_query_rows, query_args

nfc = lambda s: unicodedata.normalize("NFC", s)  # noqa: E731
nfd = lambda s: unicodedata.normalize("NFD", s)  # noqa: E731

# Hangul syllables whose NFC and NFD forms differ byte-for-byte.
A, B, C = "관찰", "코호트", "전향"


def _facts(*triples):
    return [
        {"subject": s, "relation": r, "object": o, "status": "accepted"} for s, r, o in triples
    ]


# Facts stored NFD (macOS-authored text is routinely NFD); the engine interns these
# verbatim, so its path/2 pairs carry the NFD spelling too.
NFD_FACTS = _facts((nfd(A), "uses", nfd(B)), (nfd(B), "uses", nfd(C)))
NFD_PAIRS = {(nfd(A), nfd(B)), (nfd(B), nfd(C)), (nfd(A), nfd(C))}


class TestMatcherFoldsForms:
    """path_query_rows: an NFC query constant reaches the NFD engine pair, both branches."""

    def test_two_constants_find_the_stored_route(self):
        rows = path_query_rows(query_args(f'path("{nfc(A)}", "{nfc(C)}")?'), NFD_FACTS, NFD_PAIRS)
        assert rows == [[nfd(A), nfd(B), nfd(C)]]

    def test_variable_target_binds_stored_pairs(self):
        # Order follows the stored (NFD) collation; the SET of stored pairs is what matters.
        rows = path_query_rows(query_args(f'path("{nfc(A)}", X)?'), NFD_FACTS, NFD_PAIRS)
        assert {tuple(r) for r in rows} == {(nfd(A), nfd(B)), (nfd(A), nfd(C))}

    def test_variable_start_binds_stored_pairs(self):
        rows = path_query_rows(query_args(f'path(X, "{nfc(C)}")?'), NFD_FACTS, NFD_PAIRS)
        assert {tuple(r) for r in rows} == {(nfd(A), nfd(C)), (nfd(B), nfd(C))}

    def test_reverse_direction_nfd_query_against_nfc_facts(self):
        nfc_facts = _facts((nfc(A), "uses", nfc(B)), (nfc(B), "uses", nfc(C)))
        nfc_pairs = {(nfc(A), nfc(B)), (nfc(B), nfc(C)), (nfc(A), nfc(C))}
        rows = path_query_rows(query_args(f'path("{nfd(A)}", "{nfd(C)}")?'), nfc_facts, nfc_pairs)
        assert rows == [[nfc(A), nfc(B), nfc(C)]]


class TestRenderedPathIsStoredVerbatim:
    """Provenance: the rendered path carries the STORED (NFD) spelling, never the query's
    NFC form — the engine proved the pair on the stored symbol, so that is what we show."""

    def test_route_nodes_are_the_nfd_stored_form(self):
        [route] = path_query_rows(query_args(f'path("{nfc(A)}", "{nfc(C)}")?'), NFD_FACTS, NFD_PAIRS)
        assert route == [nfd(A), nfd(B), nfd(C)]
        for node in route:
            assert node == nfd(node) and node != nfc(node)  # genuinely NFD, not the query's NFC

    def test_variable_rows_are_the_nfd_stored_form(self):
        rows = path_query_rows(query_args(f'path("{nfc(A)}", X)?'), NFD_FACTS, NFD_PAIRS)
        for _start, target in rows:
            assert target == nfd(target) and target != nfc(target)


class TestDependencyPathFoldsForms:
    def test_nfc_endpoints_resolve_to_the_stored_route(self):
        assert dependency_path(NFD_FACTS, nfc(A), nfc(C)) == [nfd(A), nfd(B), nfd(C)]

    def test_nfc_only_is_unchanged(self):
        nfc_facts = _facts((nfc(A), "uses", nfc(B)), (nfc(B), "uses", nfc(C)))
        assert dependency_path(nfc_facts, nfc(A), nfc(C)) == [nfc(A), nfc(B), nfc(C)]


class TestReachableButNoRouteContract:
    """#220/#226: a pair the engine proved (an edge from a logic-policy.extra.dl rule,
    no raw fact) is reported as the pair — never a false empty — and now under NFC/NFD
    the STORED pair is what surfaces."""

    def test_engine_only_pair_reports_the_stored_pair(self):
        engine_only = {(nfd(C), nfd(A))}  # reachable per engine, no dependency edge
        rows = path_query_rows(query_args(f'path("{nfc(C)}", "{nfc(A)}")?'), NFD_FACTS, engine_only)
        assert rows == [[nfd(C), nfd(A)]]

    def test_an_unreachable_pair_is_still_an_honest_empty(self):
        rows = path_query_rows(query_args(f'path("{nfc(C)}", "{nfc(A)}")?'), NFD_FACTS, NFD_PAIRS)
        assert rows == []


class TestNfcOnlyMatcherUnchanged:
    def test_nfc_query_and_nfc_facts_are_byte_identical_answer(self):
        nfc_facts = _facts((nfc(A), "uses", nfc(B)), (nfc(B), "uses", nfc(C)))
        nfc_pairs = {(nfc(A), nfc(B)), (nfc(B), nfc(C)), (nfc(A), nfc(C))}
        assert path_query_rows(query_args(f'path("{nfc(A)}", "{nfc(C)}")?'), nfc_facts, nfc_pairs) == [
            [nfc(A), nfc(B), nfc(C)]
        ]


class TestGateRoutesEngine:
    """The 4-way parity: with the gate entity membership folded, an NFD fact + NFC path
    query routes to ENGINE (before the fold the raw entity gate rejected the NFC constant
    and routed to wiki). ask_router.classify's path branch uses dependency_path over the
    passed facts, so this needs no live engine."""

    @pytest.fixture
    def kb(self, tmp_path, monkeypatch):
        (tmp_path / "policy").mkdir()
        monkeypatch.setattr(c, "POLICY_DIR", tmp_path / "policy")
        return tmp_path

    def test_nfd_fact_nfc_query_routes_engine(self, kb):
        q = f'path("{nfc(A)}", "{nfc(C)}")?'
        assert ask_router.classify(q, NFD_FACTS)["route"] == "engine"

    def test_raw_membership_would_miss_but_folded_gate_accepts(self, kb):
        # Load-bearing: the raw check the gate used before this change misses the NFC
        # constant (absent from the NFD entity set), yet the folded gate routes engine.
        assert nfc(A) not in c.entity_set(NFD_FACTS)
        assert ask_router.classify(f'path("{nfc(A)}", "{nfc(C)}")?', NFD_FACTS)["route"] == "engine"

    def test_nfc_only_still_routes_engine(self, kb):
        nfc_facts = _facts((nfc(A), "uses", nfc(B)), (nfc(B), "uses", nfc(C)))
        assert ask_router.classify(f'path("{nfc(A)}", "{nfc(C)}")?', nfc_facts)["route"] == "engine"


# --- Engine-backed end-to-end (report == ask over a real KB) ------------------
try:
    import pyrewire  # noqa: F401

    _HAVE_ENGINE = True
except ImportError:  # pragma: no cover - depends on the install
    _HAVE_ENGINE = False


@pytest.mark.skipif(not _HAVE_ENGINE, reason="pyrewire not installed")
class TestEngineEndToEnd:
    """With a REAL engine over an NFD-authored KB: the engine's path/2 pairs are NFD
    verbatim, the NFC query reaches them through the shared matcher, and the report and
    ask return the identical stored route (#213/#220 parity, now under NFC/NFD)."""

    def _run(self, tmp_path):
        kb = tmp_path / "kb"
        subprocess.run(
            [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
            capture_output=True, check=True,
        )
        (kb / "sources" / "a.md").write_text("a\n")
        rows = [(nfd(A), "uses", nfd(B)), (nfd(B), "uses", nfd(C))]
        lines = ["subject,relation,object,source,status,confidence,note"]
        lines += [f"{s},{r},{o},sources/a.md,accepted,0.9," for s, r, o in rows]
        (kb / "facts" / "candidates.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        subprocess.run(
            [sys.executable, str(Path("tools") / "compile_facts.py")],
            capture_output=True,
            env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
            check=True,
        )
        # A fresh interpreter with FACTLOG_ROOT set, so the module path globals resolve
        # to the KB. Compute the engine pairs and drive BOTH callers' shared matcher.
        script = r"""
import os, sys, json
sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.path.join(os.getcwd(), "tools"))
import factlog.common as c
import ask_router
facts = c.load_accepted_facts()
inferred = c.run_wirelog()
pairs = sorted(tuple(t) for t in inferred["path"])
query = 'path("%s", "%s")?'  # NFC constants
report_rows = c.path_query_rows(c.query_args(query), facts, inferred["path"])
ask_rows = ask_router.evaluate(query, facts)["rows"]
route = ask_router.classify(query, facts)["route"]
print(json.dumps({"pairs": pairs, "report": report_rows, "ask": ask_rows, "route": route}))
""" % (nfc(A), nfc(C))
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True,
            env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
            check=True,
        )
        return json.loads(out.stdout)

    def test_engine_pairs_are_nfd_and_report_equals_ask(self, tmp_path):
        data = self._run(tmp_path)
        # The engine interned the facts VERBATIM: its pairs carry the NFD spelling,
        # never the NFC form the query used.
        pair_lists = [list(p) for p in data["pairs"]]
        assert [nfd(A), nfd(C)] in pair_lists
        assert [nfc(A), nfc(C)] not in pair_lists
        # The NFC query reaches the NFD pairs, and both callers render the same route.
        assert data["report"] == data["ask"] == [[nfd(A), nfd(B), nfd(C)]]
        assert data["route"] == "engine"
