# SPDX-License-Identifier: Apache-2.0
"""Unit tests for #227 COMMIT 3: reserved-predicate guard for canonical head rules.

- _assert_no_reserved_head raises FactlogError on a canonical rule head.
- _assert_no_reserved_head raises on a bare canonical fact line.
- _assert_no_reserved_head is SILENT when canonical appears only in a rule body.
- _load_logic_policy_from raises when extra.dl contains a canonical head.
- _load_logic_policy_from is silent when extra.dl uses canonical only in body.

#329 round 2 extends the same guard to attr_rel and entity_node — see
TestReservedAttributePredicates for what each one does when it is NOT caught.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

import factlog.common as fcommon


# ---------------------------------------------------------------------------
# _assert_no_reserved_head — direct unit tests
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
            fcommon._assert_no_reserved_head(policy)

    def test_rejects_bare_canonical_fact(self):
        """A bare canonical fact line (no neck) must raise FactlogError."""
        policy = 'canonical("doc1", "결론", "true").\n'
        with pytest.raises(fcommon.FactlogError, match="reserved engine EDB predicate"):
            fcommon._assert_no_reserved_head(policy)

    def test_allows_canonical_in_rule_body(self):
        """canonical appearing only in the rule body (after :-) must NOT raise."""
        policy = textwrap.dedent("""\
            .decl conflict(entity: symbol, reason: symbol)
            conflict(X, "retracted_conclusion") :-
              canonical(X, "결론", _),
              canonical(X, "철회상태", _).
        """)
        # Must not raise
        fcommon._assert_no_reserved_head(policy)

    def test_allows_canonical_body_single_line(self):
        """Single-line rule with canonical only after :- must NOT raise."""
        policy = '.decl c(x: symbol, r: symbol)\nc(X, "r") :- canonical(X, "rel", _).\n'
        fcommon._assert_no_reserved_head(policy)

    def test_empty_policy_is_allowed(self):
        """Empty policy text must not raise."""
        fcommon._assert_no_reserved_head("")

    def test_rejects_bare_canonical_fact_after_rule_end_same_line(self):
        """A bare canonical fact sharing a physical line with a preceding rule's
        terminating '.' must still be caught — the per-line state machine let it
        through as an in-body reference (#261)."""
        policy = 'foo(X, "r") :-\n  relation(X, "a", _). canonical(X, "b", "z").\n'
        with pytest.raises(fcommon.FactlogError, match="reserved engine EDB predicate"):
            fcommon._assert_no_reserved_head(policy)

    def test_rejects_canonical_head_after_rule_end_no_space(self):
        """Same evasion with no whitespace after the terminator."""
        policy = 'foo(X, "r") :- relation(X, "a", _).canonical(Y, "b", Z) :- bar(Y, Z).\n'
        with pytest.raises(fcommon.FactlogError, match="reserved engine EDB predicate"):
            fcommon._assert_no_reserved_head(policy)

    def test_allows_two_statements_one_line_both_legal(self):
        """Two statements on one physical line, neither heading canonical, must
        NOT raise (no false positive from the finer splitting)."""
        policy = 'foo(X, "r") :- canonical(X, "a", _). bar("y", "z").\n'
        fcommon._assert_no_reserved_head(policy)

    def test_comment_only_is_allowed(self):
        """Comment-only lines must not raise."""
        policy = "// canonical(X, Y, Z) :- something(X).\n# also a comment\n"
        fcommon._assert_no_reserved_head(policy)

    def test_rejects_canonical_head_before_neck_on_same_line(self):
        """canonical(...) appearing before :- on the same line is a head."""
        policy = 'canonical(X, "r", O) :- relation(X, "r", O).\n'
        with pytest.raises(fcommon.FactlogError, match="reserved engine EDB predicate"):
            fcommon._assert_no_reserved_head(policy)

    def test_error_message_mentions_relation_aliases(self):
        """Error message should mention relation-aliases.md to guide the author."""
        policy = 'canonical("A", "b", "C").\n'
        with pytest.raises(fcommon.FactlogError, match="relation-aliases.md"):
            fcommon._assert_no_reserved_head(policy)

    def test_error_message_mentions_rule_bodies(self):
        """Error message should tell the author canonical may appear only in bodies."""
        policy = 'canonical(X, "r", O) :- relation(X, "r", O).\n'
        with pytest.raises(fcommon.FactlogError, match="rule bodies"):
            fcommon._assert_no_reserved_head(policy)

    def test_rejects_canonical_head_with_space_before_paren(self):
        """`canonical (X, ...) :- ...` (space before the paren) is still a head —
        a substring `find("canonical(")` missed it, letting the head evade the guard
        with rc=0. Head tokenization tolerates the whitespace."""
        policy = 'canonical (X, "r", O) :- relation(X, "r", O).\n'
        with pytest.raises(fcommon.FactlogError, match="reserved engine EDB predicate"):
            fcommon._assert_no_reserved_head(policy)

    def test_rejects_bare_canonical_fact_with_space_before_paren(self):
        """A bare `canonical (...)` fact with a space before the paren is a head."""
        policy = 'canonical ("doc1", "결론", "true").\n'
        with pytest.raises(fcommon.FactlogError, match="reserved engine EDB predicate"):
            fcommon._assert_no_reserved_head(policy)

    def test_allows_not_canonical_head(self):
        """A user predicate that merely CONTAINS the reserved name — `not_canonical`
        — must NOT be rejected as a canonical head. A substring match flagged it,
        so a legitimate policy could no longer run `factlog check`."""
        policy = 'not_canonical(X, "r") :- relation(X, "r", _).\n'
        fcommon._assert_no_reserved_head(policy)

    def test_allows_not_canonical_bare_fact(self):
        """A bare `not_canonical(...)` fact must not be mistaken for a canonical head."""
        policy = 'not_canonical("A", "b").\n'
        fcommon._assert_no_reserved_head(policy)

    def test_allows_not_canonical_in_body(self):
        """`not_canonical` used only in a rule body must be allowed."""
        policy = 'conflict(X, "r") :- not_canonical(X, "r", _).\n'
        fcommon._assert_no_reserved_head(policy)

    def test_canonical_in_string_literal_not_flagged(self):
        """A string literal containing 'canonical(' must not trigger the guard."""
        # The word "canonical" inside a quoted string is not a predicate call.
        policy = '.decl conflict(entity: symbol, reason: symbol)\nconflict(X, "canonical(X)") :- relation(X, "rel", _).\n'
        # "canonical(" appears only inside a quoted string after :-; guard must pass.
        fcommon._assert_no_reserved_head(policy)


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


# ---------------------------------------------------------------------------
# #329 round 2 — the same guard for attr_rel / entity_node
# ---------------------------------------------------------------------------

class TestReservedAttributePredicates:
    """attr_rel and entity_node are engine-owned exactly as canonical is, and the
    guard covered only canonical. Measured on the PR head, in a real KB with
    `정식_운영` declared an attribute relation:

    * one line in logic-policy.extra.dl —
      ``attr_rel(R) :- relation(S, R, O), R = "존재하지않음".`` — restored
      ``engine path/2 : [('갑봇','2030.1'), ('갑봇','을서비스'), ('을서비스','2030.1')]``
      against ``renderer pairs: [('갑봇','을서비스')]`` with rc=0 and no error:
      the engine/renderer divergence #329 exists to remove, silently back.
    * ``.decl entity_node(entity: symbol, reason: symbol)`` plus a rule — the
      standard shape of a policy predicate in this repo, so an existing KB can
      already be named that — died with a bare
      ``pyrewire._core.errors.ExecError: execution error`` traceback (SIGSEGV in
      an isolated probe with a matching fact). It works on main.

    The control for both is the canonical case above, which has always failed loudly.
    """

    def test_rejects_attr_rel_head_in_extra_dl(self, tmp_path):
        # The exact line measured to silently revert the #329 filter.
        dl = _make_kb(
            tmp_path,
            dl_text="// generated\n.decl conflict(entity: symbol, reason: symbol)\n",
            extra_text='attr_rel(R) :- relation(S, R, O), R = "존재하지않음".\n',
        )
        with pytest.raises(fcommon.FactlogError, match="attr_rel is a reserved engine"):
            fcommon._load_logic_policy_from(dl)

    def test_rejects_bare_attr_rel_fact(self):
        with pytest.raises(fcommon.FactlogError, match="attr_rel is a reserved engine"):
            fcommon._assert_no_reserved_head('attr_rel("정식_운영").\n')

    def test_rejects_entity_node_head(self):
        # Adds rows to the derived predicate -> literals return to the graph.
        with pytest.raises(fcommon.FactlogError, match="entity_node is a reserved engine"):
            fcommon._assert_no_reserved_head("entity_node(O) :- relation(S, R, O).\n")

    def test_rejects_entity_node_redeclaration_without_any_rule(self, tmp_path):
        # A .decl alone already changes the arity the program compiles against;
        # this is the shape that produced the raw ExecError traceback.
        dl = _make_kb(
            tmp_path,
            dl_text="// generated\n.decl entity_node(entity: symbol, reason: symbol)\n",
        )
        with pytest.raises(fcommon.FactlogError, match="entity_node is a reserved engine"):
            fcommon._load_logic_policy_from(dl)

    def test_rejects_attr_rel_redeclaration(self):
        with pytest.raises(fcommon.FactlogError, match="attr_rel is a reserved engine"):
            fcommon._assert_no_reserved_head(".decl attr_rel(rel: symbol)\n")

    def test_allows_both_in_rule_bodies(self):
        # CONTROL — passes before and after. Reading them is the point of #227's
        # body allowance; only a head or a re-.decl corrupts the program.
        policy = textwrap.dedent("""\
            .decl literal_rel(entity: symbol, reason: symbol)
            literal_rel(R, "attribute") :- attr_rel(R), entity_node(R).
        """)
        fcommon._assert_no_reserved_head(policy)

    def test_allows_a_user_predicate_that_merely_contains_the_name(self):
        # CONTROL — passes before and after. Pins the both-directions requirement
        # the canonical guard already carries, now for the widened name set.
        fcommon._assert_no_reserved_head("my_entity_node(X) :- relation(X, R, O).\n")
        fcommon._assert_no_reserved_head("attr_rel_audit(R) :- relation(S, R, O).\n")

    def test_message_names_the_identifier_and_suggests_a_rename(self):
        with pytest.raises(fcommon.FactlogError) as excinfo:
            fcommon._assert_no_reserved_head("entity_node(O) :- relation(S, R, O).\n")
        message = str(excinfo.value)
        assert "entity_node" in message
        assert "my_entity_node" in message  # actionable alternative


# ---------------------------------------------------------------------------
# #329 round 3 — comments are cut to end of line, not dropped whole-line
# ---------------------------------------------------------------------------

_RESERVED = ["canonical", "attr_rel", "entity_node"]
_HEADS = {
    "canonical": 'canonical(X, "결론", O) :- relation(X, "r", O).',
    "attr_rel": 'attr_rel(R) :- relation(S, R, O), R = "정식_운영".',
    "entity_node": "entity_node(O) :- relation(S, R, O).",
}


class TestInlineCommentsDoNotDisableTheGuard:
    """A comment at the END of a line used to survive comment stripping, because
    the filter dropped only lines that START with `//`/`#`. `_split_policy_statements`
    then pushed the surviving comment onto the FRONT of the next statement, and the
    head tokenizer (`\\s*([A-Za-z_]\\w*)\\s*\\(`) failed on the `/` — `m is None`, so
    the statement passed UNCHECKED. Measured on the previous head, in a KB with
    `정식_운영` declared an attribute relation:

        attr_rel head, no comment                -> FactlogError, rc=1
        same, with `// note` ending the line before ->
            engine   [('갑봇','2030.1'), ('갑봇','을서비스'), ('을서비스','2030.1')]
            renderer [('갑봇','을서비스')]                          rc=0, silent

    i.e. one end-of-line comment — the most ordinary formatting there is in a
    Datalog file — silently restored the engine/renderer divergence #329 removes.
    The flaw predates #329: `canonical` was bypassed the same way on main.
    """

    @pytest.mark.parametrize("name", _RESERVED)
    @pytest.mark.parametrize("comment", ["//", "#"])
    def test_reserved_head_after_an_inline_comment_is_still_rejected(self, name, comment):
        policy = f'u(X, "n") :- relation(X, R, O).  {comment} note\n{_HEADS[name]}\n'
        with pytest.raises(fcommon.FactlogError, match=f"{name} is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)

    @pytest.mark.parametrize("name", _RESERVED)
    def test_reserved_head_after_a_tab_indented_comment_is_still_rejected(self, name):
        policy = f'u(X, "n") :- relation(X, R, O).\t// note\n{_HEADS[name]}\n'
        with pytest.raises(fcommon.FactlogError, match=f"{name} is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)

    @pytest.mark.parametrize("name", _RESERVED)
    def test_reserved_head_after_a_comment_containing_a_dot_is_rejected(self, name):
        # The dot inside the comment is a second evasion route: it terminates a
        # statement early, so the comment tail is all that reaches the tokenizer.
        policy = f'u(X, "n") :- relation(X, R, O). // v1.0 note\n{_HEADS[name]}\n'
        with pytest.raises(fcommon.FactlogError, match=f"{name} is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)

    @pytest.mark.parametrize("name", _RESERVED)
    def test_reserved_head_on_the_same_line_after_a_comment_start_is_ignored(self, name):
        # The converse direction: text after `//` is NOT policy, so a reserved head
        # written inside a comment must NOT be rejected.
        policy = f'u(X, "n") :- relation(X, R, O).  // {_HEADS[name]}\n'
        fcommon._assert_no_reserved_head(policy)

    @pytest.mark.parametrize("name", _RESERVED)
    @pytest.mark.parametrize("comment", ["//", "#"])
    def test_decl_of_a_reserved_name_inside_an_inline_comment_is_allowed(self, name, comment):
        # The `.decl` scan runs on the whole text, so an inline comment explaining
        # WHY a name was avoided used to be rejected as a real re-declaration —
        # something a careful policy author actually writes. The whole-line form
        # was already accepted; the two must agree.
        policy = (
            f'u(X, "n") :- relation(X, R, O).  {comment} .decl {name}(a: symbol) 은 금지\n'
        )
        fcommon._assert_no_reserved_head(policy)

    def test_double_slash_inside_a_string_literal_is_not_a_comment(self):
        # CONTROL for the fix's ordering: quoted strings must be removed BEFORE
        # comments are cut, or a URL in a reason literal would swallow the rest of
        # the file and hide every head after it.
        policy = (
            'u(X, "http://example.com/a") :- relation(X, R, O).\n'
            'attr_rel(R) :- relation(S, R, O).\n'
        )
        with pytest.raises(fcommon.FactlogError, match="attr_rel is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)

    def test_a_hash_inside_a_string_literal_is_not_a_comment(self):
        policy = (
            'u(X, "tag#1") :- relation(X, R, O).\n'
            'entity_node(O) :- relation(S, R, O).\n'
        )
        with pytest.raises(fcommon.FactlogError, match="entity_node is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)

    def test_an_unpaired_quote_inside_a_comment_does_not_hide_a_later_head(self):
        # The other ordering hazard: a lone `"` in a comment must not pair with a
        # quote further down and delete the policy in between.
        policy = (
            'u(X, "n") :- relation(X, R, O).  // don"t use the reserved names\n'
            'attr_rel(R) :- relation(S, R, O), R = "정식_운영".\n'
        )
        with pytest.raises(fcommon.FactlogError, match="attr_rel is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)

    @pytest.mark.parametrize("name", _RESERVED)
    @pytest.mark.parametrize(
        "opener",
        [
            "foo(X, a#b).",
            "foo(X, a//b).",
            "foo(X, # y).",
            "foo(X, a",
            "foo(X, a) :- bar(X",
        ],
        ids=[
            "mid-hash",
            "mid-slash",
            "hash-swallows-terminator",
            "unterminated",
            "unterminated-past-the-neck",
        ],
    )
    def test_an_unterminated_statement_cannot_hide_the_next_head(self, name, opener):
        """`re.match` reads only the FIRST atom of a head, so a clause that never
        terminated absorbs the next one and that head goes unexamined.

        Cutting comments to end of line is what makes it reachable: `foo(X, a#b).`
        is not valid Datalog, but removing `#b).` leaves `foo(X, a` unterminated.
        The bare `foo(X, a` case shows the hole is not specific to comments — it
        passed on the previous head too, with no comment anywhere in the text.
        `foo(X, a) :- bar(X` is the same absorption PAST a neck, which lands the
        swallowed head in the first clause's body; it passed on main as well."""
        policy = f"{opener}\n{_HEADS[name]}\n"
        with pytest.raises(fcommon.FactlogError, match=f"{name} is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)

    def test_a_reserved_name_used_as_a_symbol_in_a_head_is_allowed(self):
        # CONTROL — the head-position search requires a following '(', so a
        # reserved name appearing as a plain term is not a head.
        fcommon._assert_no_reserved_head('foo(X, attr_rel) :- relation(X, R, O).\n')
        fcommon._assert_no_reserved_head('foo(X, "entity_node").\n')

    @pytest.mark.parametrize("name", _RESERVED)
    @pytest.mark.parametrize(
        "gap", [" ", "  ", "\t", "\n"], ids=["space", "spaces", "tab", "newline"]
    )
    def test_whitespace_before_a_terminator_does_not_fuse_two_clauses(self, name, gap):
        """`p(X) :- canonical(X,_,_) .q(Y) :- relation(Y,_,_).` is TWO clauses and
        pyrewire compiles it (measured, alongside the plain two-line form).

        `is_directive = prev.isspace() and nxt.isalpha()` read that ` .q` as a
        directive, so the terminator did not terminate, the next clause was glued
        on, and the absorption scan then reported the FIRST clause's body
        reference as a head:

            FactlogError: canonical is a reserved engine EDB predicate ...
                          it may appear only in rule bodies

        — about a `canonical` that is in a rule body. #227 allows exactly that.
        """
        policy = f'p2(X) :- {name}(X,_,_){gap}.q2(Y) :- relation(Y,_,_).\n'
        fcommon._assert_no_reserved_head(policy)

    @pytest.mark.parametrize(
        "directive",
        [".decl", ".type", ".output", ".input", ".printsize", ".pragma"],
    )
    def test_a_real_directive_still_does_not_terminate_a_clause(self, directive):
        """CONTROL for the narrowed rule: a directive's dot must still be inert, or
        the directive splits into its own statement and the clause after it loses
        its head to the tokenizer."""
        policy = f'{directive} foo\nattr_rel(R) :- relation(S, R, O).\n'
        with pytest.raises(fcommon.FactlogError, match="attr_rel is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)

    def test_a_float_literal_still_does_not_terminate_a_clause(self):
        # CONTROL — the other exception the splitter carries.
        fcommon._assert_no_reserved_head('v(X) :- relation(X, "r", "2.5").\n')

    @pytest.mark.parametrize("name", _RESERVED)
    def test_a_body_reference_is_never_read_as_a_head(self, name):
        # The positional rule: only the atom immediately LEFT of a neck is a head.
        # A reserved name deeper in the body stays legal however the statement was
        # split.
        policy = f'lit(R, "a") :- foo(R), {name}(R, _, _), bar(R).\n'
        fcommon._assert_no_reserved_head(policy)

    @pytest.mark.parametrize("name", _RESERVED)
    def test_an_escaped_quote_cannot_blank_out_a_reserved_head(self, name):
        r"""`"[^"]*"` stopped inside `"q\""`, so the leftover `"` paired with the
        NEXT literal's opening quote and deleted every line in between — including
        a reserved head. pyrewire compiles that literal, so this is policy someone
        can legitimately write, and the guard passed it silently."""
        policy = (
            'a(X, "q\\"") :- relation(X, _, _).\n'
            f"{_HEADS[name]}\n"
            'b(X, "z") :- relation(X, _, _).\n'
        )
        with pytest.raises(fcommon.FactlogError, match=f"{name} is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)

    def test_an_escaped_quote_alone_does_not_reject_legitimate_policy(self):
        # CONTROL — the same literal with no reserved head must still load.
        fcommon._assert_no_reserved_head(
            'a(X, "q\\"") :- relation(X, _, _).\nb(X, "z") :- relation(X, _, _).\n'
        )

    def test_an_escaped_backslash_does_not_swallow_the_closing_quote(self):
        # `\\` ends the escape, so the quote after it closes the literal rather
        # than being consumed by it.
        policy = 'a(X, "c:\\\\tmp") :- relation(X, _, _).\nattr_rel(R) :- relation(R, R, R).\n'
        with pytest.raises(fcommon.FactlogError, match="attr_rel is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)

    def test_a_reserved_name_inside_an_escaped_literal_is_not_a_head(self):
        # CONTROL the other way: the literal is still a literal.
        fcommon._assert_no_reserved_head(
            'a(X, "he said \\"attr_rel(R)\\" once") :- relation(X, _, _).\n'
        )

    def test_inline_comment_does_not_reject_a_legitimate_policy(self, tmp_path):
        # CONTROL through the loader: ordinary commented policy still loads.
        dl = _make_kb(
            tmp_path,
            dl_text="// generated\n.decl conflict(entity: symbol, reason: symbol)\n",
            extra_text=(
                'conflict(X, "r") :- attr_rel(R), relation(X, R, _).  // attr_rel 은 몸통에서만\n'
                'note(X, "n") :- entity_node(X).  # entity_node 도 마찬가지\n'
            ),
        )
        result = fcommon._load_logic_policy_from(dl)
        assert "attr_rel" in result


# ---------------------------------------------------------------------------
# #329 round 3 — a bare fact fused onto the previous statement (#358)
# ---------------------------------------------------------------------------

_FUSED_FACT = {
    "canonical": 'canonical("갑봇", "참조", "유령").',
    "attr_rel": 'attr_rel("참조").',
    "entity_node": 'entity_node("2030.1").',
}


class TestAFusedBareFactIsNotHonoured:
    """A bare reserved FACT written after ` .` was absorbed into the previous
    statement and never examined, and the engine HONOURS it.

    The rule-head shape was already caught — two necks send it down the
    ``segments[:-1]`` scan — but a bare fact has one neck, so only the head
    segment was scanned and the fact sitting in the body half was invisible. The
    fix is in ``_split_policy_statements``: once ` .attr_rel(` no longer reads as
    a directive, the fact stands alone and the head tokenizer refuses it.

    Measured end to end. KB: ``갑봇 -통합-> 을서비스``, ``을서비스 -정식_운영-> 2030.1``,
    ``갑봇 -참조-> 병문서``, only ``정식_운영`` declared. Query ``path("갑봇","병문서")?``::

        clean                                    rc=0 errors:0
          - path 갑봇 -> 병문서: 갑봇 -> 병문서
        p(X) :- relation(X,_,_) .attr_rel("참조").   guard PASSED
                                                 rc=0 errors:0
          - path 갑봇 -> 병문서: (not found)        <- wrong, silent
        attr_rel("참조").          (same fact alone) guard REFUSED, rc=1

    The answer flips with no error and no warning: the engine/renderer divergence
    #329 exists to remove. `attr_rel` is pure EDB, so the engine honours a bare
    fact for it — unlike an IDB relation that has rules, whose in-program facts
    pyrewire ignores. That difference is why probing with an inert predicate
    reads as "no consequence" when there is one.

    The same root is a live defect on `main` for `canonical` (#358): with a
    ``requires_review(X, "canon_check") :- canonical(X, "참조", _).`` consumer,
    ``p(X) :- relation(X,_,_) .canonical("갑봇","참조","유령").`` moved main's report
    from ``policy findings: 0`` to ``policy findings: 1`` / ``- requires_review: 갑봇
    (canon_check)`` at rc=0, for a ``유령`` that is in no fact anywhere.
    """

    @pytest.mark.parametrize("name", _RESERVED)
    def test_a_fused_bare_fact_is_refused(self, name, tmp_path):
        dl = _make_kb(
            tmp_path,
            dl_text="// generated\n.decl requires_review(entity: symbol, reason: symbol)\n",
            extra_text=f'p(X) :- relation(X,_,_) .{_FUSED_FACT[name]}\n',
        )
        with pytest.raises(fcommon.FactlogError, match=f"{name} is a reserved engine"):
            fcommon._load_logic_policy_from(dl)

    @pytest.mark.parametrize("name", _RESERVED)
    def test_the_same_fact_standalone_is_refused(self, name, tmp_path):
        # CONTROL — refused before and after. The whole defect was that one space
        # separated this row from the one above it.
        dl = _make_kb(
            tmp_path,
            dl_text="// generated\n.decl requires_review(entity: symbol, reason: symbol)\n",
            extra_text=f"{_FUSED_FACT[name]}\n",
        )
        with pytest.raises(fcommon.FactlogError, match=f"{name} is a reserved engine"):
            fcommon._load_logic_policy_from(dl)

    def test_clean_policy_still_loads(self, tmp_path):
        # CONTROL — the clean row of the measurement above.
        dl = _make_kb(
            tmp_path,
            dl_text="// generated\n.decl requires_review(entity: symbol, reason: symbol)\n",
            extra_text='p(X) :- relation(X, _, _).\n',
        )
        assert "requires_review" in fcommon._load_logic_policy_from(dl)

    @pytest.mark.parametrize("name", _RESERVED)
    def test_literal_stripping_can_manufacture_the_adjacency(self, name):
        """Different provenance from the authored case: the source text has NO
        space before the dot. Removing the quoted literal creates one —
        ``O = "v1.0".canonical(`` becomes ``O = .canonical(`` — so a fix aimed at
        adjacency as the author typed it would not reach this."""
        policy = f'p(X) :- relation(X,R,O), O = "v1.0".{_FUSED_FACT[name]}\n'
        with pytest.raises(fcommon.FactlogError, match=f"{name} is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)

    @pytest.mark.parametrize("name", _RESERVED)
    def test_a_fused_rule_head_is_refused(self, name):
        # Already caught before this fix (two necks); pinned so the narrowing
        # cannot regress it.
        policy = f'p(X) :- relation(X,_,_) .{_HEADS[name]}\n'
        with pytest.raises(fcommon.FactlogError, match=f"{name} is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)


# ---------------------------------------------------------------------------
# #329 round 4 — a directive with no parens merges the same way `.decl` does
# ---------------------------------------------------------------------------

# Directives that name a relation without parenthesising anything, plus `.plan`,
# whose argument is a number. Each carries no clause-terminating '.', so the
# statement after it merges in.
_PARENLESS_DIRECTIVES = [
    ".output p2",
    ".printsize p2",
    ".input p2",
    ".limitsize p2",
    ".override p2",
    ".plan 0",
    '.pragma "x" "y"',
    ".type T",
]


class TestAParenlessDirectiveDoesNotHideTheNextStatement:
    """`.output p2` above a bare reserved fact let the fact through.

    ``.decl`` was stripped before tokenizing, so its merge was already handled;
    no other directive was, and a paren-less one leaves the merged statement
    starting with `.` — the head `re.match` finds nothing, there is no neck, and
    the segment scan had nothing to scan. Measured end to end on the previous
    head, KB as in :class:`TestAFusedBareFactIsNotHonoured`::

        clean                             rc=0  - path 갑봇 -> 병문서: 갑봇 -> 병문서
        .output p2 / attr_rel("참조").      rc=0  - path 갑봇 -> 병문서: (not found)
                                                errors: 0        <- wrong, silent
        p(X) :- … .attr_rel("참조").        rc=1  refused (closed in round 3)
        attr_rel("참조").                   rc=1  refused

    The mechanism differs from the round-3 one and keyword narrowing cannot reach
    it: `.output p2` IS a directive, so merging it is *correct* parsing. What was
    missing is that the merged text still has to be examined.

    All 8 paren-less directives × 3 reserved names are pinned here: 24 cells, all
    24 rejected before this round and all 24 passing after it. Only 6 of them are
    SILENT — measured, guard PASS **and** the engine compiles the program::

        .output    × attr_rel / entity_node / canonical
        .printsize × attr_rel / entity_node / canonical

    The other 18 (`.input`, `.limitsize`, `.override`, `.plan`, `.pragma`,
    `.type`) leave text pyrewire refuses with ParseError, so a regression there
    would be loud. They are pinned at this level anyway because the fix is one
    rule — the neckless statement gets scanned — rather than a list of directives
    to watch, and which column a directive falls in is a property of the parser,
    not of this guard. `TestAParenlessDirectiveReachesTheEngine` carries the
    end-to-end half for the cells that actually move an answer.
    """

    @pytest.mark.parametrize("name", _RESERVED)
    @pytest.mark.parametrize("directive", _PARENLESS_DIRECTIVES)
    def test_a_bare_reserved_fact_after_a_directive_is_refused(self, name, directive):
        policy = f"{directive}\n{_FUSED_FACT[name]}\n"
        with pytest.raises(fcommon.FactlogError, match=f"{name} is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)

    @pytest.mark.parametrize("name", _RESERVED)
    @pytest.mark.parametrize("directive", _PARENLESS_DIRECTIVES)
    def test_a_reserved_rule_head_after_a_directive_is_refused(self, name, directive):
        policy = f"{directive}\n{_HEADS[name]}\n"
        with pytest.raises(fcommon.FactlogError, match=f"{name} is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)

    @pytest.mark.parametrize("name", _RESERVED)
    def test_a_directive_and_a_fact_on_one_line_are_both_seen(self, name):
        """`.output p2 attr_rel("참조").` on ONE line is a program pyrewire
        compiles (measured), so the directive strip may consume the directive's
        own tokens and no more. Eating to end of line would delete this fact and
        manufacture the very bypass being closed."""
        policy = f'.output p2 {_FUSED_FACT[name]}\n'
        with pytest.raises(fcommon.FactlogError, match=f"{name} is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)

    @pytest.mark.parametrize("name", _RESERVED)
    def test_a_directive_may_name_a_reserved_relation(self, name):
        """CONTROL, and a program pyrewire compiles: `.output entity_node` asks
        the engine to print an engine-owned relation. It neither heads nor
        re-declares it, so it is legal and must load."""
        fcommon._assert_no_reserved_head(f'.output {name}\nfoo2(X, "a") :- relation(X, _, _).\n')

    @pytest.mark.parametrize("name", _RESERVED)
    def test_a_parenthesised_directive_naming_a_reserved_relation_is_allowed(self, name):
        # CONTROL — the parenthesised form is consumed with its parameters, so it
        # cannot be mistaken for a head either.
        fcommon._assert_no_reserved_head(
            f'.limitsize {name}(n=10)\nfoo2(X, "a") :- relation(X, _, _).\n'
        )

    def test_a_directive_is_still_not_a_clause_terminator(self):
        # CONTROL — a trailing directive must not become a statement whose only
        # atom is a reserved name.
        fcommon._assert_no_reserved_head(
            'foo2(X, "a") :- relation(X, _, _).\n.limitsize entity_node(n=10)\n'
        )

    @pytest.mark.parametrize("name", _RESERVED)
    @pytest.mark.parametrize(
        "directive",
        ['.pragma "x" "y"', '.pragma "x"', ".comp C", ".override q", ".functor f"],
        ids=["pragma-two", "pragma-one", "comp", "override", "functor"],
    )
    def test_the_strip_does_not_cross_a_newline(self, name, directive):
        """A directive's operand lives on the directive's own line.

        `.pragma "x" "y"` loses its quoted operands to the literal strip that runs
        first, leaving a bare `.pragma`; if the directive strip then matched
        `\\s+<name>` it would cross the newline and delete the head of the NEXT
        statement. Found by fuzzing the directive axis, which is also what the two
        earlier fuzzers were missing."""
        policy = f"{directive}\n{_HEADS[name]}\n"
        with pytest.raises(fcommon.FactlogError, match=f"{name} is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)

    @pytest.mark.parametrize("name", _RESERVED)
    def test_a_directive_operand_on_its_own_line_is_still_stripped(self, name):
        # CONTROL for the other side of that boundary: the operand IS consumed
        # when it sits on the directive's line, so a legal directive naming a
        # reserved relation still loads.
        fcommon._assert_no_reserved_head(f'.output {name}\nfoo2(X, "a") :- relation(X, _, _).\n')

    @pytest.mark.parametrize("name", _RESERVED)
    def test_the_loader_refuses_it_too(self, name, tmp_path):
        dl = _make_kb(
            tmp_path,
            dl_text="// generated\n.decl requires_review(entity: symbol, reason: symbol)\n",
            extra_text=f".output p2\n{_FUSED_FACT[name]}\n",
        )
        with pytest.raises(fcommon.FactlogError, match=f"{name} is a reserved engine"):
            fcommon._load_logic_policy_from(dl)


class TestTheHeadIsTheLastAtomBeforeANeck:
    """The positional rule, pinned by the one input that still distinguishes it.

    Reverting it to the earlier "any reserved name anywhere in a head-bearing
    segment" scan leaves the whole suite green, so it needs its own pin.

    Scope, stated because it narrowed: once directives are stripped, a program
    pyrewire COMPILES cannot reach a statement with two necks — every clause has
    one neck and terminates — so on compiling input the two scans now agree. The
    row below is malformed (pyrewire ParseErrors it), and what the positional
    rule buys there is that a legal `#227` body reference is not reported as a
    head merely because the clause above it lost its terminator.
    """

    @pytest.mark.parametrize("name", _RESERVED)
    def test_a_body_reference_left_of_a_second_neck_is_not_a_head(self, name):
        # segments: ['foo(X,a) ', ' <name>(X,_,_)\nbar(Y,"z") ', ' relation(...).']
        # The middle segment holds clause 1's BODY and clause 2's head; only the
        # latter is a head, and it is the last atom.
        policy = f'foo(X,a) :- {name}(X,_,_)\nbar(Y,"z") :- relation(Y,_,_).\n'
        fcommon._assert_no_reserved_head(policy)

    @pytest.mark.parametrize("name", _RESERVED)
    def test_the_same_shape_with_a_reserved_head_is_still_refused(self, name):
        # CONTROL — the reserved name IS the last atom before the second neck.
        policy = f'foo(X,a) :- bar(X,"z")\n{name}(Y,"r",O) :- relation(Y,"r",O).\n'
        with pytest.raises(fcommon.FactlogError, match=f"{name} is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)


# ---------------------------------------------------------------------------
# #329 round 5 — a directive must not swallow a clause as its operand
# ---------------------------------------------------------------------------

# Every directive, including the ones this engine does not implement. The
# consumption rule is about what a directive may OWN, which is a property of the
# grammar rather than of which directives pyrewire supports today.
_ALL_DIRECTIVES = [
    ".plan", ".output", ".printsize", ".input", ".limitsize", ".override",
    ".type", ".comp", ".init", ".functor", ".pragma", ".decl",
    ".symbol_type", ".number_type",
]


class TestADirectiveCannotSwallowAClause:
    """`.plan attr_rel("참조").` deleted the fact instead of exposing it.

    The strip consumed keyword + name + parenthesised args, so it read `attr_rel`
    as `.plan`'s operand and `("참조")` as that operand's parameters, and removed
    the whole thing — leaving nothing for the head tokenizer to find. pyrewire
    does the opposite: it skips the keyword and takes the fact. Measured::

        clean                                  rc=0  - path 갑봇 -> 병문서: 갑봇 -> 병문서
        p(X) :- … .  /  .plan attr_rel("참조").  rc=0  - path 갑봇 -> 병문서: (not found)
                                                     errors: 0     <- wrong, silent
        same fact with the `.plan ` removed     rc=1  refused

    Five characters flip the answer. This is the third over-consumption bug in
    the same strip — end-of-line, then across-newline, now same-line — so the
    rule is stated once and generally: an operand that is ITSELF a terminated
    clause is not an operand. Whatever would be consumed is given back when what
    follows it is `.` or `:-`.

    On the real assembled program only `.plan` is silent; the other directives
    leave text pyrewire refuses. All 14 are pinned anyway, for the reason the
    round-4 set is: which directive lands in which column is a property of the
    parser, and a version that implements `.plan` would move it.
    """

    @pytest.mark.parametrize("name", _RESERVED)
    @pytest.mark.parametrize("directive", _ALL_DIRECTIVES)
    def test_a_directive_does_not_swallow_a_following_fact(self, name, directive):
        policy = f'p(X) :- relation(X,_,_).\n{directive} {_FUSED_FACT[name]}\n'
        with pytest.raises(fcommon.FactlogError, match=f"{name} is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)

    @pytest.mark.parametrize("name", _RESERVED)
    @pytest.mark.parametrize("directive", _ALL_DIRECTIVES)
    def test_a_directive_does_not_swallow_a_following_rule_head(self, name, directive):
        # The neck is the other proof that what follows is a clause.
        policy = f'p(X) :- relation(X,_,_).\n{directive} {_HEADS[name]}\n'
        with pytest.raises(fcommon.FactlogError, match=f"{name} is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)

    @pytest.mark.parametrize("name", _RESERVED)
    def test_the_same_fact_without_the_directive_is_refused(self, name):
        # CONTROL — refused before and after; the directive is the entire
        # difference between this row and the one above.
        policy = f'p(X) :- relation(X,_,_).\n{_FUSED_FACT[name]}\n'
        with pytest.raises(fcommon.FactlogError, match=f"{name} is a reserved engine"):
            fcommon._assert_no_reserved_head(policy)

    @pytest.mark.parametrize("name", _RESERVED)
    def test_a_genuine_operand_is_still_consumed(self, name):
        """CONTROL for the other side: an operand NOT followed by `.` or `:-` is
        the directive's own, so a legal directive naming a reserved relation must
        still load. Losing this would turn the fix into a false-rejection."""
        fcommon._assert_no_reserved_head(
            f'.limitsize {name}(n=10)\nfoo2(X, "a") :- relation(X, _, _).\n'
        )
        fcommon._assert_no_reserved_head(
            f'.output {name}\nfoo2(X, "a") :- relation(X, _, _).\n'
        )

    def test_a_trailing_directive_is_still_consumed(self):
        # CONTROL — nothing follows it at all, so there is no clause to give back.
        fcommon._assert_no_reserved_head(
            'foo2(X, "a") :- relation(X, _, _).\n.limitsize entity_node(n=10)\n'
        )
