# SPDX-License-Identifier: Apache-2.0
"""`conflict` is a policy-derived predicate, not a static one (#306).

Two defects, one root cause. `conflict` sat in the static ``QUERY_PREDICATES``
but had no evaluation branch and was absent from ask's allowed set, so:

  * the report (validate_query) called `conflict(...)?` a valid query (errors: 0)
    that produced no result line, and a lone one drew the fallback
    "... none produced a result — see Errors above" pointing at an Errors
    section that was not there — a dangling pointer of the #284/#220 family;
  * ask (classify_query) called the same line an unknown predicate (route=wiki).

#306 removes `conflict` from ``QUERY_PREDICATES`` (undeclared, it is unknown on
BOTH pipelines) and derives ask's allowed set from ``QUERY_PREDICATES`` so the
two static vocabularies cannot drift again. When a KB's policy DOES declare
`conflict`, it is a policy predicate and both pipelines evaluate it.

The fallback pointer is now guarded on ``errors`` so no report ever points at an
absent Errors section.

Red/green: put "conflict" back into ``QUERY_PREDICATES`` and
``test_undeclared_conflict_is_unknown_on_all_three`` fails on every clause.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import run_logic_check as rlc
from factlog.common import QUERY_UNKNOWN_PREDICATE, classify_query


def _fact(subject, relation, object_):
    return {"subject": subject, "relation": relation, "object": object_}


def _evaluate(monkeypatch, queries, facts=None, inferred=None, policy=None):
    monkeypatch.setattr(rlc, "query_lines", lambda: queries)
    return rlc.evaluate_queries(
        facts if facts is not None else [],
        inferred if inferred is not None else {"path": set()},
        policy if policy is not None else set(),
        hierarchy={},
    )


class TestUndeclaredConflictParity:
    """(a) With no policy declaring it, `conflict` is unknown on all three judges:
    validate_query (report errors), evaluate_queries (report Query evaluation), and
    classify_query (ask). Before #306 the first two disagreed with the third."""

    QUERY = 'conflict("A", "B")?'

    def test_undeclared_conflict_is_unknown_on_all_three(self, monkeypatch):
        # validate_query: an Errors-section entry.
        errors, _ = rlc.validate_query(self.QUERY, set(), set())
        assert errors == [f"query unknown predicate: {self.QUERY}"]

        # evaluate_queries: the Query-evaluation line, same as any unknown predicate.
        results = _evaluate(monkeypatch, [self.QUERY])
        assert results == ["unknown query predicate — see Errors above"]

        # classify_query (ask): the machine-readable unknown code.
        ok, code, _ = classify_query(self.QUERY, [], policy_program="")
        assert (ok, code) == (False, QUERY_UNKNOWN_PREDICATE)


class TestDeclaredConflictEvaluates:
    """(b) When policy declares `conflict`, it is a policy predicate: the report
    evaluates it and ask recognises it. This behaviour is byte-unchanged by #306
    (ask's allowed set is `QUERY_PREDICATES | policy_query_predicates`, and the
    static half is identical to the old literal) — a no-regression pin."""

    QUERY = 'conflict("A", "reason")?'
    POLICY_DECL = ".decl conflict(a: symbol, b: symbol)"

    def test_report_validates_and_renders_policy_result(self, monkeypatch):
        errors, _ = rlc.validate_query(self.QUERY, {"A"}, {"conflict"})
        assert errors == []
        results = _evaluate(
            monkeypatch,
            [self.QUERY],
            inferred={"path": set(), "conflict": {("A", "reason")}},
            policy={"conflict"},
        )
        assert any(line.startswith("conflict results: 1 rows") for line in results)

    def test_ask_recognises_declared_conflict(self):
        _, code, _ = classify_query(self.QUERY, [], policy_program=self.POLICY_DECL)
        # Not unknown: it clears the predicate gate and proceeds to arg checks.
        assert code != QUERY_UNKNOWN_PREDICATE


class TestFallbackErrorPointer:
    """The report's "none produced a result" fallback points at Errors only when an
    Errors section exists — the #284/#220 dangling-pointer rule."""

    def test_empty_errors_fallback_omits_the_pointer(self, tmp_path, monkeypatch):
        # (c) Force the fallback (a non-empty query.dl whose lines yield no result)
        # while errors stay empty. Post-#306 this is unreachable through query.dl —
        # every validate-accepted predicate has an evaluation branch — so the seam
        # is stubbed to prove the guard, not a reachable input.
        facts_dir = tmp_path / "facts"
        facts_dir.mkdir()
        (facts_dir / "query.dl").write_text('relation("A", "uses", "B")?\n', encoding="utf-8")
        monkeypatch.setattr(rlc, "FACTS_DIR", facts_dir)
        monkeypatch.setattr(rlc, "ensure_dirs", lambda: None)
        monkeypatch.setattr(rlc, "load_accepted_facts", lambda: [])
        monkeypatch.setattr(rlc, "load_facts", lambda: [])
        monkeypatch.setattr(rlc, "run_wirelog", lambda: {"path": set()})
        monkeypatch.setattr(rlc, "load_logic_policy", lambda: "")
        monkeypatch.setattr(rlc, "policy_predicates", lambda program: set())
        monkeypatch.setattr(rlc, "value_hierarchy", lambda: {})
        monkeypatch.setattr(rlc, "relation_aliases", lambda: {})
        monkeypatch.setattr(rlc, "known_constants", lambda *a, **k: set())
        monkeypatch.setattr(rlc, "review_facts", lambda candidates: [])
        monkeypatch.setattr(rlc, "status_warnings", lambda candidates: [])
        monkeypatch.setattr(rlc, "value_hierarchy_warnings", lambda **k: [])
        monkeypatch.setattr(rlc, "typed_projection_warnings", lambda *a, **k: [])
        monkeypatch.setattr(rlc, "typed_policy_warnings", lambda: [])
        # A valid relation query keeps errors empty; force no result line.
        monkeypatch.setattr(rlc, "validate_query", lambda *a, **k: ([], []))
        monkeypatch.setattr(rlc, "evaluate_queries", lambda *a, **k: [])

        rc = rlc.main()
        report = (facts_dir / "logic_report.txt").read_text(encoding="utf-8")
        assert "errors: 0" in report
        assert "none produced a result" in report
        assert "see Errors above" not in report
        assert rc is None


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPILE = REPO_ROOT / "tools" / "compile_facts.py"
CHECK = REPO_ROOT / "tools" / "run_logic_check.py"
HEADER = "subject,relation,object,source,status,confidence,note"


def _env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    env["FACTLOG_ROOT"] = str(root)
    return env


class TestCountArityFallbackKeepsPointer:
    """(d) The complementary case: a lone count query of the wrong arity DOES
    record an error, so the report keeps its "see Errors above" pointer.
    End-to-end through the real CLI (needs the engine).

    Post-#319 the pointer comes from the count branch itself, which guards arity and
    argument shape like `relation` and `path`, instead of main's "none produced a
    result" fallback — a fallback the old branch reached only by silently appending
    nothing.

    This pins WHICH seam printed the pointer, on purpose. Both messages contain
    "see Errors above", so asserting that substring alone would still pass if the
    count branch regressed to appending nothing: the generic fallback would print
    its own pointer and cover for the silence #319 removed. Asserting the branch's
    own message keeps the #306 invariant (the pointer refers to an Errors section
    that exists) AND fails on that regression.
    """

    def test_count_wrong_arity_alone_keeps_pointer(self, tmp_path):
        pytest.importorskip("pyrewire", reason="run_logic_check needs the engine")
        kb = tmp_path / "wiki"
        subprocess.run(
            [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
            check=True,
            capture_output=True,
            env=_env(tmp_path),
        )
        (kb / "sources" / "a.md").write_text("a\n", encoding="utf-8")
        (kb / "facts" / "candidates.csv").write_text(
            f"{HEADER}\nA,uses,B,sources/a.md,accepted,0.90,\n", encoding="utf-8"
        )
        (kb / "facts" / "query.dl").write_text('count("A")?\n', encoding="utf-8")
        subprocess.run([sys.executable, str(COMPILE)], cwd=kb, check=True, capture_output=True, env=_env(kb))
        subprocess.run([sys.executable, str(CHECK)], cwd=kb, capture_output=True, text=True, env=_env(kb))
        report = (kb / "facts" / "logic_report.txt").read_text(encoding="utf-8")
        assert "count query malformed — see Errors above" in report
        assert "Errors:" in report  # the section the pointer refers to exists
