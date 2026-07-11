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


class TestHeadIsMatchedAsAToken:
    """The guard tokenizes the head. Substring-searching it was wrong BOTH ways: a
    single space slipped a reserved head past it (and #226 came back with rc=0), while
    a user predicate that merely CONTAINS a reserved name was rejected, so a KB that
    ran fine before could no longer run `factlog check`."""

    @pytest.mark.parametrize(
        "policy",
        [
            'attr_rel (R) :- relation(S, R, O).',  # one space
            'canonical (S, R, O) :- relation(S, R, O).',
            'attr_rel("정식_운영").',  # a bare fact
            ".decl attr_rel(rel: symbol)",  # a redeclaration
        ],
    )
    def test_a_reserved_head_is_rejected_however_it_is_spaced(self, policy):
        with pytest.raises(fcommon.FactlogError):
            fcommon._assert_no_canonical_head(policy)

    @pytest.mark.parametrize(
        "policy",
        [
            'not_canonical(X, "unaliased") :- relation(X, "depends_on", Y).',
            "no_attr_rel(R) :- relation(S, R, O).",
            "attr_rel_ok(R) :- relation(S, R, O).",
            'ok(X, "r") :- canonical(X, R, O).',  # a body reference is the point of it
            '.decl ok(entity: symbol, reason: symbol)\nok(X, "r") :- attr_rel(R).',
        ],
    )
    def test_a_predicate_that_merely_contains_a_reserved_name_is_allowed(self, policy):
        fcommon._assert_no_canonical_head(policy)


class TestInlineCommentsDoNotHideAHead:
    """A trailing `// TODO` after a clause's '.' left the comment at the head of the
    NEXT statement, so the head anchor never matched and a reserved head walked through
    -- #226 back with rc=0, and the canonical guard weaker than before the change."""

    @pytest.mark.parametrize("reserved", ["attr_rel(R) :- relation(S, R, O).", "canonical(S, R, O) :- relation(S, R, O)."])
    def test_a_head_behind_an_inline_comment_is_rejected(self, reserved):
        policy = 'foo(X, "r") :- relation(X, "a", Y).  // TODO\n' + reserved
        with pytest.raises(fcommon.FactlogError):
            fcommon._assert_no_canonical_head(policy)

    def test_a_decl_that_is_not_at_the_start_of_a_line_is_rejected(self):
        with pytest.raises(fcommon.FactlogError):
            fcommon._assert_no_canonical_head("foo(X) :- bar(X). .decl attr_rel(rel: symbol)")

    def test_a_slash_inside_a_string_is_not_a_comment(self):
        fcommon._assert_no_canonical_head('ok(X, "a // b") :- attr_rel(R).')


class TestOneLexerNotTwoRegexPasses:
    """Two regex passes were wrong in BOTH orders, and each order was a live bypass.

    Comments-first: a `//` inside a reason string looked like a comment.
    Quotes-first: an ODD `"` inside a comment paired with a quote on a LATER line and
    deleted everything between -- including a reserved head. The guard saw nothing, the
    engine ran the rule, attr_rel became IDB, every emitted atom was dropped, and #226
    came back with rc=0.
    """

    @pytest.mark.parametrize(
        "policy",
        [
            '// prefer the "canonical\nattr_rel(R) :- relation(S, R, O).',
            '# beware of " here\ncanonical("a","b","c").',
            '// a stray "\nedge(S, O) :- relation(S, R, O).',
        ],
    )
    def test_an_odd_quote_in_a_comment_cannot_hide_a_head(self, policy):
        with pytest.raises(fcommon.FactlogError):
            fcommon._assert_no_canonical_head(policy)

    @pytest.mark.parametrize(
        "policy",
        [
            'ok(X, "a // b") :- canonical(X, R, O).',
            'ok(X, "a # b") :- attr_rel(R).',
            '.decl ok(entity: symbol, reason: symbol)\nok(X, "r") :- edge(X, Y).',
        ],
    )
    def test_a_comment_marker_inside_a_string_is_not_a_comment(self, policy):
        fcommon._assert_no_canonical_head(policy)

    def test_an_unterminated_string_fails_loudly(self):
        """The engine cannot parse it either; swallowing it would mean guessing."""
        with pytest.raises(fcommon.FactlogError, match="unterminated string"):
            fcommon._assert_no_canonical_head('ok(X, "never closed) :- relation(X, R, O).')


class TestEdgeAndPathAreReservedToo:
    """The scaffold promises, unconditionally, that no edge is drawn along an attribute
    relation. A policy heading `edge` re-draws every link the filter removed -- a
    guarantee with an unguarded escape hatch is the false promise #226 is about."""

    @pytest.mark.parametrize(
        "policy",
        ["edge(S, O) :- relation(S, R, O).", "path(S, O) :- edge(S, O)."],
    )
    def test_heading_an_engine_derivation_is_rejected(self, policy):
        with pytest.raises(fcommon.FactlogError):
            fcommon._assert_no_canonical_head(policy)

    def test_referencing_them_in_a_body_is_still_fine(self):
        fcommon._assert_no_canonical_head('reach(X, "r") :- path(X, Y).')



class TestBackslashEscapesMatchTheEngine:
    """pyrewire 1.0.3+ supports `\\"` escapes (MIN_PYREWIRE_VERSION), and
    generate_logic_policy emits them via dl_string. Treating an escaped quote as a
    string terminator rejected policies factlog itself generates and the engine parses.
    """

    @pytest.mark.parametrize(
        "policy",
        [
            # a reason with an odd embedded quote, as dl_string would serialize it
            r'requires_review(X, "size 5\" bolt") :- relation(X, "status", _).',
            r'after(X, "flagged \"provisional\"") :- relation(X, "s", _).',
            r'ok(X, "a\" b") :- attr_rel(R).',  # escaped quote + a body reference
        ],
    )
    def test_an_escaped_quote_is_not_a_terminator(self, policy):
        fcommon._assert_no_canonical_head(policy)  # engine parses it; the guard must too

    def test_a_head_hidden_behind_an_escaped_quote_is_still_caught(self):
        # the escape must not become a new way to smuggle a reserved head
        with pytest.raises(fcommon.FactlogError):
            fcommon._assert_no_canonical_head(r'ok(X, "z\") :- relation(X,R,O).' + "\nattr_rel(R) :- relation(S,R,O).")

    def test_a_genuinely_unterminated_string_still_fails_loudly(self):
        with pytest.raises(fcommon.FactlogError, match="unterminated string"):
            fcommon._assert_no_canonical_head('ok(X, "never closed) :- relation(X, R, O).')
