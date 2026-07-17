# SPDX-License-Identifier: Apache-2.0
"""A policy rule that HEADS `relation` is rejected before it can empty the engine (#305).

`relation` is the engine's accepted-fact EDB (facts/accepted.dl). A rule such as

    relation(X, "developed_by", Y) :- relation(Y, "develops", X).

is natural authoring (derive the inverse), but pyrewire then treats `relation` as IDB and
SILENTLY drops every accepted fact: compile stays rc=0, and relation/path/every policy
predicate evaluate over an EMPTY relation. The failure is a *vacuous pass* -- check reports
`errors: 0` exit 0, add reports "no contradictions", and ask renders "VERIFIED — engine
(verified negative)" -- all three agree, all three wrong.

The guard (`_assert_no_canonical_head`) already fails loud on canonical/attr_rel/edge/path
heads; #305 adds `relation` for the RULE-head case only. A bare `relation(...)` FACT stays
allowed (#303 relies on it), and a standard rule that merely READS relation in its body is
untouched. Tested at the text layer (no engine) and, where pyrewire is present, end to end.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from factlog.common import FactlogError, _assert_no_canonical_head

# The issue's exact reproduction rule.
REPRO_RULE = 'relation(X, "developed_by", Y) :- relation(Y, "develops", X).\n'


def _rejects(text):
    try:
        _assert_no_canonical_head(text)
        return None
    except FactlogError as exc:
        return str(exc)


class TestRelationRuleHeadRejected:
    def test_the_issue_reproduction_rule_is_rejected(self):
        msg = _rejects(REPRO_RULE)
        assert msg is not None
        assert "relation is the engine's accepted-fact EDB" in msg
        assert "DIFFERENT predicate name" in msg  # the actionable fix is named

    def test_a_relation_rule_head_on_one_physical_line_is_rejected(self):
        assert _rejects('relation(A, "r", B) :- relation(B, "r", A).') is not None

    def test_a_relation_rule_head_after_another_statement_is_rejected(self):
        # A second statement on the same line still heads relation with a neck.
        text = 'requires_review(S, "r") :- relation(S, "uses", "F"). ' + REPRO_RULE
        assert _rejects(text) is not None


class TestDeclRelationRejected:
    """A policy `.decl relation(...)` re-declares the engine's EDB and pyrewire then
    silently drops/corrupts accepted facts (#305). Every arity is rejected."""

    def test_arity_mismatched_decl_is_rejected(self):
        # The reviewer's scenario: `.decl relation(a, b)` (arity 2 vs the engine's 3)
        # compiles rc=0 but silently loses facts (path pairs 3 -> 2). Reject it.
        msg = _rejects(".decl relation(a: symbol, b: symbol)\n")
        assert msg is not None
        assert "already declared by the engine" in msg

    def test_arity_matched_decl_is_also_rejected(self):
        # Even an arity-matching re-decl is a meaningless duplicate that only invites the
        # mismatch failure later, so it is rejected too.
        assert _rejects(".decl relation(subject: symbol, rel: symbol, object: symbol)\n") is not None

    def test_a_decl_of_a_predicate_named_like_relation_is_allowed(self):
        # Token match, not substring: `.decl my_relation(...)` is a user predicate.
        assert _rejects(".decl my_relation(a: symbol, b: symbol)\n") is None


class TestAllowedFormsUnchanged:
    def test_a_bare_relation_fact_is_allowed(self):
        # #303 depends on this: a bare fact keeps relation an EDB, no IDB flip.
        assert _rejects('relation("C", "uses", "A").') is None

    def test_a_standard_rule_reading_relation_in_its_body_is_allowed(self):
        # relation appears only in the BODY (right of :-); the head is a user predicate.
        assert _rejects('requires_review(S, "r") :- relation(S, "uses", "FastAPI").') is None

    def test_a_user_predicate_named_like_relation_is_allowed(self):
        # Substring, not token: `my_relation` heads a rule and must NOT be rejected.
        assert _rejects('my_relation(A, B) :- relation(A, "uses", B).') is None

    def test_an_empty_policy_is_allowed(self):
        assert _rejects("") is None


class TestReservedFourStillRejected:
    """No regression: the pre-#305 reserved set still rejects both facts and rules."""

    @pytest.mark.parametrize("name", ["canonical", "attr_rel", "edge", "path"])
    def test_reserved_rule_head_rejected(self, name):
        assert _rejects(f'{name}(S, T) :- relation(T, "uses", S).') is not None

    @pytest.mark.parametrize("name", ["canonical", "attr_rel", "edge", "path"])
    def test_reserved_bare_fact_rejected(self, name):
        assert _rejects(f'{name}("a", "b").') is not None

    @pytest.mark.parametrize("name", ["canonical", "attr_rel", "edge", "path"])
    def test_reserved_decl_rejected(self, name):
        # The .decl loop's rejection of the reserved four is unchanged by the new
        # relation .decl branch, which sits after it.
        assert _rejects(f".decl {name}(a: symbol, b: symbol)\n") is not None


class TestLexEdgeCases:
    def test_a_neck_inside_a_reason_string_does_not_trip_the_guard(self):
        # The `:-` lives in a quoted literal; the skeleton strips strings, so the head
        # is requires_review, not relation.
        assert _rejects('requires_review(E, "a :- b") :- relation(E, "uses", "X").') is None

    def test_a_relation_rule_head_inside_a_comment_is_ignored(self):
        text = "// " + REPRO_RULE + 'requires_review(S, "r") :- relation(S, "uses", "F").\n'
        assert _rejects(text) is None

    def test_a_hash_commented_relation_rule_head_is_ignored(self):
        text = "# " + REPRO_RULE + 'requires_review(S, "r") :- relation(S, "uses", "F").\n'
        assert _rejects(text) is None

    def test_a_relation_bare_fact_then_a_relation_rule_head_still_rejects(self):
        # The fact is allowed but the following rule is not: the loop must catch the rule.
        assert _rejects('relation("C", "uses", "A"). ' + REPRO_RULE) is not None


# --- Engine-backed entry points (fail loud / degrade) -------------------------
try:
    import pyrewire  # noqa: F401

    _HAVE_ENGINE = True
except ImportError:  # pragma: no cover - depends on the install
    _HAVE_ENGINE = False


def _kb(tmp_path, extra_dl):
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
    (kb / "policy" / "logic-policy.extra.dl").write_text(extra_dl, encoding="utf-8")
    subprocess.run(
        [sys.executable, str(Path("tools") / "compile_facts.py")],
        capture_output=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
        check=True,
    )
    return kb


def _probe(kb, script):
    return subprocess.run(
        [sys.executable, "-c", "import os, sys; sys.path.insert(0, os.getcwd()); "
         "sys.path.insert(0, os.path.join(os.getcwd(), 'tools'))\n" + script],
        capture_output=True, text=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
    )


@pytest.mark.skipif(not _HAVE_ENGINE, reason="pyrewire not installed")
class TestEngineEntryPointsFailLoud:
    """With the reproduction rule installed, the deterministic paths (check) fail loud
    instead of vacuously passing; ask degrades (never renders a false engine answer)."""

    def test_load_logic_policy_raises(self, tmp_path):
        kb = _kb(tmp_path, REPRO_RULE)
        out = _probe(kb, "import factlog.common as c\n"
                         "try:\n c.load_logic_policy(); print('NO_RAISE')\n"
                         "except c.FactlogError:\n print('FACTLOG_ERROR')")
        assert out.stdout.strip() == "FACTLOG_ERROR", out.stdout + out.stderr

    def test_run_wirelog_raises(self, tmp_path):
        kb = _kb(tmp_path, REPRO_RULE)
        out = _probe(kb, "import factlog.common as c\n"
                         "try:\n c.run_wirelog(); print('NO_RAISE')\n"
                         "except c.FactlogError:\n print('FACTLOG_ERROR')")
        assert out.stdout.strip() == "FACTLOG_ERROR", out.stdout + out.stderr

    def test_decl_relation_arity_mismatch_fails_loud(self, tmp_path):
        # The reviewer's silent-data-loss scenario: `.decl relation(a, b)` compiled rc=0
        # and dropped a KB's path pairs 3->2 with no signal. The guard now fails loud at
        # policy load instead of letting the engine evaluate over corrupted facts.
        kb = _kb(tmp_path, ".decl relation(a: symbol, b: symbol)\n")
        out = _probe(kb, "import factlog.common as c\n"
                         "try:\n c.run_wirelog(); print('NO_RAISE')\n"
                         "except c.FactlogError:\n print('FACTLOG_ERROR')")
        assert out.stdout.strip() == "FACTLOG_ERROR", out.stdout + out.stderr

    def test_run_logic_check_exits_nonzero(self, tmp_path):
        kb = _kb(tmp_path, REPRO_RULE)
        proc = subprocess.run(
            [sys.executable, str(Path("tools") / "run_logic_check.py")],
            capture_output=True, text=True,
            env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
        )
        # Before #305 this exited 0 with "errors: 0" over a silently-emptied engine.
        assert proc.returncode != 0
        assert "relation is the engine's accepted-fact EDB" in (proc.stdout + proc.stderr)


@pytest.mark.skipif(not _HAVE_ENGINE, reason="pyrewire not installed")
class TestAskDegradesNotFalseNegative:
    """ask is exploratory and must never hard-fail on a broken extra.dl (#193): the
    guard's FactlogError is caught and the query DEGRADES -- crucially, ask no longer
    renders a false 'VERIFIED — engine (verified negative)'."""

    def test_policy_predicate_query_routes_wiki(self, tmp_path):
        # A policy predicate declared ALONGSIDE the poisoned relation rule-head. Without
        # the guard the policy loads and `flagged` classifies engine (then evaluates
        # vacuously); with the guard load_logic_policy raises, _policy_program_optional
        # catches it and returns "", so the predicate is unknown -> route=wiki. No false
        # engine answer reaches the user.
        extra = ('.decl flagged(entity: symbol, reason: symbol)\n'
                 'flagged(S, "flagged") :- relation(S, "developed_by", "Anthropic").\n'
                 + REPRO_RULE)
        kb = _kb(tmp_path, extra)
        out = _probe(kb, "import factlog.common as c, ask_router\n"
                         "facts = c.load_accepted_facts()\n"
                         "print(ask_router.classify('flagged(E, R)?', facts)['route'])")
        assert out.stdout.strip() == "wiki", out.stdout + out.stderr

    def test_path_query_signals_policy_unevaluable(self, tmp_path):
        kb = _kb(tmp_path, REPRO_RULE)
        out = _probe(kb, "import factlog.common as c, ask_router, json\n"
                         "facts = c.load_accepted_facts()\n"
                         "r = ask_router.evaluate('path(\"Claude Code\", \"Anthropic\")?', facts)\n"
                         "print(json.dumps({'count': r['count'], 'unevaluable': bool(r.get('policy_unevaluable'))}))")
        assert '"unevaluable": true' in out.stdout, out.stdout + out.stderr


@pytest.mark.skipif(not _HAVE_ENGINE, reason="pyrewire not installed")
class TestLegitimateAuthoringStillWorks:
    """The guard must not break a standard derived-predicate policy: a rule that READS
    relation in its body (heading a NEW predicate) compiles and evaluates normally."""

    def test_a_body_relation_rule_evaluates(self, tmp_path):
        extra = ('.decl uses_anthropic(entity: symbol, reason: symbol)\n'
                 'uses_anthropic(S, "uses_anthropic") :- relation(S, "developed_by", "Anthropic").\n')
        kb = _kb(tmp_path, extra)
        out = _probe(kb, "import factlog.common as c\n"
                         "inf = c.run_wirelog()\n"
                         "print(len(inf.get('uses_anthropic', [])))")
        assert out.returncode == 0, out.stdout + out.stderr
        assert int(out.stdout.strip()) >= 1  # the derived predicate fired over live facts
