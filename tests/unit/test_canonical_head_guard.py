# SPDX-License-Identifier: Apache-2.0
"""Unit tests for #227 COMMIT 3: reserved-predicate guard for canonical head rules.

- _assert_no_canonical_head raises FactlogError on a canonical rule head.
- _assert_no_canonical_head raises on a bare canonical fact line.
- _assert_no_canonical_head is SILENT when canonical appears only in a rule body.
- _load_logic_policy_from raises when extra.dl contains a canonical head.
- _load_logic_policy_from is silent when extra.dl uses canonical only in body.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

import factlog.common as fcommon


# ---------------------------------------------------------------------------
# _assert_no_canonical_head — direct unit tests
# ---------------------------------------------------------------------------

class TestAssertNoCanonicalHead:
    """Guard function: canonical in head → FactlogError; body → allowed."""

    def test_rejects_canonical_rule_head(self):
        """A rule whose head is canonical(...) must raise FactlogError."""
        policy = textwrap.dedent("""\
            .decl conflict(entity: symbol, reason: symbol)
            canonical(X, "결론", O) :- relation(X, "concludes", O).
        """)
        with pytest.raises(fcommon.FactlogError, match="reserved engine EDB predicate"):
            fcommon._assert_no_canonical_head(policy)

    def test_rejects_bare_canonical_fact(self):
        """A bare canonical fact line (no neck) must raise FactlogError."""
        policy = 'canonical("doc1", "결론", "true").\n'
        with pytest.raises(fcommon.FactlogError, match="reserved engine EDB predicate"):
            fcommon._assert_no_canonical_head(policy)

    def test_allows_canonical_in_rule_body(self):
        """canonical appearing only in the rule body (after :-) must NOT raise."""
        policy = textwrap.dedent("""\
            .decl conflict(entity: symbol, reason: symbol)
            conflict(X, "retracted_conclusion") :-
              canonical(X, "결론", _),
              canonical(X, "철회상태", _).
        """)
        # Must not raise
        fcommon._assert_no_canonical_head(policy)

    def test_allows_canonical_body_single_line(self):
        """Single-line rule with canonical only after :- must NOT raise."""
        policy = '.decl c(x: symbol, r: symbol)\nc(X, "r") :- canonical(X, "rel", _).\n'
        fcommon._assert_no_canonical_head(policy)

    def test_empty_policy_is_allowed(self):
        """Empty policy text must not raise."""
        fcommon._assert_no_canonical_head("")

    def test_rejects_bare_canonical_fact_after_rule_end_same_line(self):
        """A bare canonical fact sharing a physical line with a preceding rule's
        terminating '.' must still be caught — the per-line state machine let it
        through as an in-body reference (#261)."""
        policy = 'foo(X, "r") :-\n  relation(X, "a", _). canonical(X, "b", "z").\n'
        with pytest.raises(fcommon.FactlogError, match="reserved engine EDB predicate"):
            fcommon._assert_no_canonical_head(policy)

    def test_rejects_canonical_head_after_rule_end_no_space(self):
        """Same evasion with no whitespace after the terminator."""
        policy = 'foo(X, "r") :- relation(X, "a", _).canonical(Y, "b", Z) :- bar(Y, Z).\n'
        with pytest.raises(fcommon.FactlogError, match="reserved engine EDB predicate"):
            fcommon._assert_no_canonical_head(policy)

    def test_allows_two_statements_one_line_both_legal(self):
        """Two statements on one physical line, neither heading canonical, must
        NOT raise (no false positive from the finer splitting)."""
        policy = 'foo(X, "r") :- canonical(X, "a", _). bar("y", "z").\n'
        fcommon._assert_no_canonical_head(policy)

    def test_comment_only_is_allowed(self):
        """Comment-only lines must not raise."""
        policy = "// canonical(X, Y, Z) :- something(X).\n# also a comment\n"
        fcommon._assert_no_canonical_head(policy)

    def test_rejects_canonical_head_before_neck_on_same_line(self):
        """canonical(...) appearing before :- on the same line is a head."""
        policy = 'canonical(X, "r", O) :- relation(X, "r", O).\n'
        with pytest.raises(fcommon.FactlogError, match="reserved engine EDB predicate"):
            fcommon._assert_no_canonical_head(policy)

    def test_error_message_mentions_relation_aliases(self):
        """Error message should mention relation-aliases.md to guide the author."""
        policy = 'canonical("A", "b", "C").\n'
        with pytest.raises(fcommon.FactlogError, match="relation-aliases.md"):
            fcommon._assert_no_canonical_head(policy)

    def test_error_message_mentions_rule_bodies(self):
        """Error message should tell the author canonical may appear only in bodies."""
        policy = 'canonical(X, "r", O) :- relation(X, "r", O).\n'
        with pytest.raises(fcommon.FactlogError, match="rule bodies"):
            fcommon._assert_no_canonical_head(policy)

    def test_rejects_canonical_head_with_space_before_paren(self):
        """`canonical (X, ...) :- ...` (space before the paren) is still a head —
        a substring `find("canonical(")` missed it, letting the head evade the guard
        with rc=0. Head tokenization tolerates the whitespace."""
        policy = 'canonical (X, "r", O) :- relation(X, "r", O).\n'
        with pytest.raises(fcommon.FactlogError, match="reserved engine EDB predicate"):
            fcommon._assert_no_canonical_head(policy)

    def test_rejects_bare_canonical_fact_with_space_before_paren(self):
        """A bare `canonical (...)` fact with a space before the paren is a head."""
        policy = 'canonical ("doc1", "결론", "true").\n'
        with pytest.raises(fcommon.FactlogError, match="reserved engine EDB predicate"):
            fcommon._assert_no_canonical_head(policy)

    def test_allows_not_canonical_head(self):
        """A user predicate that merely CONTAINS the reserved name — `not_canonical`
        — must NOT be rejected as a canonical head. A substring match flagged it,
        so a legitimate policy could no longer run `factlog check`."""
        policy = 'not_canonical(X, "r") :- relation(X, "r", _).\n'
        fcommon._assert_no_canonical_head(policy)

    def test_allows_not_canonical_bare_fact(self):
        """A bare `not_canonical(...)` fact must not be mistaken for a canonical head."""
        policy = 'not_canonical("A", "b").\n'
        fcommon._assert_no_canonical_head(policy)

    def test_allows_not_canonical_in_body(self):
        """`not_canonical` used only in a rule body must be allowed."""
        policy = 'conflict(X, "r") :- not_canonical(X, "r", _).\n'
        fcommon._assert_no_canonical_head(policy)

    def test_canonical_in_string_literal_not_flagged(self):
        """A string literal containing 'canonical(' must not trigger the guard."""
        # The word "canonical" inside a quoted string is not a predicate call.
        policy = '.decl conflict(entity: symbol, reason: symbol)\nconflict(X, "canonical(X)") :- relation(X, "rel", _).\n'
        # "canonical(" appears only inside a quoted string after :-; guard must pass.
        fcommon._assert_no_canonical_head(policy)


# ---------------------------------------------------------------------------
# _load_logic_policy_from integration: guard fires through the loader
# ---------------------------------------------------------------------------

def _make_kb(tmp_path: Path, *, dl_text: str = "", extra_text: str | None = None) -> Path:
    """Scaffold a minimal policy dir with logic-policy.dl and optional extra.dl."""
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir(parents=True, exist_ok=True)
    dl = policy_dir / "logic-policy.dl"
    if dl_text is not None:
        dl.write_text(dl_text, encoding="utf-8")
    if extra_text is not None:
        (policy_dir / "logic-policy.extra.dl").write_text(extra_text, encoding="utf-8")
    return dl


class TestLoadLogicPolicyCanonicalHeadGuard:
    """_load_logic_policy_from must raise when either .dl or extra.dl has a canonical head."""

    def test_raises_when_logic_policy_dl_has_canonical_head(self, tmp_path):
        """A canonical head in logic-policy.dl (base file) triggers the guard."""
        dl_text = textwrap.dedent("""\
            // generated from policy/logic-policy.md
            .decl conflict(entity: symbol, reason: symbol)
            canonical(X, "결론", O) :- relation(X, "r", O).
        """)
        dl = _make_kb(tmp_path, dl_text=dl_text)
        with pytest.raises(fcommon.FactlogError, match="reserved engine EDB predicate"):
            fcommon._load_logic_policy_from(dl)

    def test_raises_when_extra_dl_has_canonical_head(self, tmp_path):
        """A canonical head in logic-policy.extra.dl triggers the guard."""
        dl_text = textwrap.dedent("""\
            // generated
            .decl conflict(entity: symbol, reason: symbol)
        """)
        extra_text = textwrap.dedent("""\
            .decl bad(entity: symbol, reason: symbol)
            canonical(X, "결론", O) :- relation(X, "r", O).
        """)
        dl = _make_kb(tmp_path, dl_text=dl_text, extra_text=extra_text)
        with pytest.raises(fcommon.FactlogError, match="reserved engine EDB predicate"):
            fcommon._load_logic_policy_from(dl)

    def test_ok_when_canonical_only_in_body(self, tmp_path):
        """canonical only in rule bodies (no head) must load without raising."""
        dl_text = textwrap.dedent("""\
            // generated
            .decl conflict(entity: symbol, reason: symbol)
            conflict(X, "retracted") :- canonical(X, "결론", _), canonical(X, "철회상태", _).
        """)
        dl = _make_kb(tmp_path, dl_text=dl_text)
        result = fcommon._load_logic_policy_from(dl)
        assert "conflict" in result
        assert "canonical" in result

    def test_ok_when_canonical_in_extra_body_only(self, tmp_path):
        """canonical in extra.dl rule body only must load without raising."""
        dl_text = "// generated\n.decl conflict(entity: symbol, reason: symbol)\n"
        extra_text = 'conflict(X, "r") :- canonical(X, "rel", _).\n'
        dl = _make_kb(tmp_path, dl_text=dl_text, extra_text=extra_text)
        result = fcommon._load_logic_policy_from(dl)
        assert "canonical" in result
