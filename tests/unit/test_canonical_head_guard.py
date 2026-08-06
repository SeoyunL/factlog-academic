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
