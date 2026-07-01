# SPDX-License-Identifier: Apache-2.0
"""Unit tests for KbContext (#107): read a non-default KB in-process."""
from __future__ import annotations

import common

HEADER = "subject,relation,object,source,status,confidence,note\n"


def _make_kb(root, *, facts_rows=(), attribute_rels=(), single_valued=()):
    (root / "facts").mkdir(parents=True, exist_ok=True)
    (root / "policy").mkdir(parents=True, exist_ok=True)
    (root / "facts" / "candidates.csv").write_text(
        HEADER + "".join(r + "\n" for r in facts_rows), encoding="utf-8"
    )
    if attribute_rels:
        (root / "policy" / "attribute-relations.md").write_text(
            "".join(f"- `{r}`\n" for r in attribute_rels), encoding="utf-8"
        )
    if single_valued:
        (root / "policy" / "single-valued.md").write_text(
            "".join(f"- `{r}`\n" for r in single_valued), encoding="utf-8"
        )
    return common.KbContext.for_root(root)


class TestPaths:
    def test_for_root_derives_paths(self, tmp_path):
        ctx = common.KbContext.for_root(tmp_path)
        assert ctx.candidates_csv == ctx.facts_dir / "candidates.csv"
        assert ctx.accepted_dl == ctx.facts_dir / "accepted.dl"
        assert ctx.logic_policy_dl == ctx.policy_dir / "logic-policy.dl"
        assert ctx.root == tmp_path.resolve()


class TestLoaders:
    def test_load_facts_reads_its_own_root(self, tmp_path):
        ctx = _make_kb(tmp_path, facts_rows=["A,rel,B,sources/a.md,accepted,0.9,"])
        rows = ctx.load_facts()
        assert len(rows) == 1 and rows[0]["subject"] == "A"

    def test_policy_methods_read_its_own_root(self, tmp_path):
        ctx = _make_kb(tmp_path, attribute_rels=["born_on"], single_valued=["capital_of"])
        assert ctx.attribute_relations() == {"born_on"}
        assert ctx.single_valued_relations() == {"capital_of"}


class TestExtraLogicPolicy:
    """Optional policy/logic-policy.extra.dl is concatenated onto the generated
    logic-policy.dl, for hand-authored rules the --check golden never touches (#120)."""

    def _write_policy(self, root, *, base, extra=None):
        (root / "policy").mkdir(parents=True, exist_ok=True)
        dl = root / "policy" / "logic-policy.dl"
        dl.write_text(base, encoding="utf-8")
        if extra is not None:
            (root / "policy" / "logic-policy.extra.dl").write_text(extra, encoding="utf-8")
        return dl

    def test_absent_extra_is_byte_identical(self, tmp_path):
        base = ".decl requires_review(entity: symbol, reason: symbol)\n"
        dl = self._write_policy(tmp_path, base=base)
        assert common._load_logic_policy_from(dl) == base.strip()

    def test_present_extra_is_appended(self, tmp_path):
        base = ".decl requires_review(entity: symbol, reason: symbol)\n"
        extra = '.decl after2030(entity: symbol, reason: symbol)\nafter2030(S, "x") :- launch_date(S, D), D >= 20300101.\n'
        dl = self._write_policy(tmp_path, base=base, extra=extra)
        assert common._load_logic_policy_from(dl) == base.strip() + "\n" + extra.strip()

    def test_empty_extra_is_byte_identical(self, tmp_path):
        base = ".decl requires_review(entity: symbol, reason: symbol)\n"
        dl = self._write_policy(tmp_path, base=base, extra="   \n\n")
        assert common._load_logic_policy_from(dl) == base.strip()

    def test_comment_only_extra_is_byte_identical(self, tmp_path):
        base = ".decl requires_review(entity: symbol, reason: symbol)\n"
        dl = self._write_policy(tmp_path, base=base, extra="// just a note\n// nothing here\n")
        assert common._load_logic_policy_from(dl) == base.strip()

    def test_hash_comment_only_extra_is_byte_identical(self, tmp_path):
        # Authors instinctively use `#` (every other policy file does).
        # A `#`-only stub must stay byte-identical — wirelog rejects `#` with
        # a ParseError if it leaks into the engine program.
        base = ".decl requires_review(entity: symbol, reason: symbol)\n"
        dl = self._write_policy(tmp_path, base=base, extra="# just a note\n# nothing here\n")
        assert common._load_logic_policy_from(dl) == base.strip()

    def test_extra_discovered_via_kb_context(self, tmp_path):
        base = ".decl requires_review(entity: symbol, reason: symbol)\n"
        extra = ".decl after2030(entity: symbol, reason: symbol)\n"
        self._write_policy(tmp_path, base=base, extra=extra)
        ctx = common.KbContext.for_root(tmp_path)
        assert "after2030" in common.policy_predicates(ctx.load_logic_policy())


class TestMissingLogicPolicyDl:
    """A freshly `init`ed KB has no compiled logic-policy.dl yet (#190).

    When logic-policy.md defines no compilable rules this is a legitimate empty
    policy → return '' so `check` completes with 0 findings (mirroring how
    `/factlog ask` is already graceful). When the .md DOES define rules but was
    never compiled, keep failing loud (do not silently drop the policy)."""

    def _dl(self, root, *, md=None):
        (root / "policy").mkdir(parents=True, exist_ok=True)
        if md is not None:
            (root / "policy" / "logic-policy.md").write_text(md, encoding="utf-8")
        return root / "policy" / "logic-policy.dl"

    def test_absent_dl_no_md_is_empty_policy(self, tmp_path):
        dl = self._dl(tmp_path)  # no .md, no .dl at all
        assert common._load_logic_policy_from(dl) == ""

    def test_absent_dl_prose_only_md_is_empty_policy(self, tmp_path):
        md = "# Logic policy\n\nWrite rules like `- [c1] ... ` here later.\n"
        # Prose text — no bullet with a [id] tag → no rules → empty policy.
        dl = self._dl(tmp_path, md=md)
        assert common._load_logic_policy_from(dl) == ""

    def test_absent_dl_empty_md_is_empty_policy(self, tmp_path):
        dl = self._dl(tmp_path, md="   \n\n")
        assert common._load_logic_policy_from(dl) == ""

    def test_absent_dl_with_extra_dl_preserves_hand_authored_rules(self, tmp_path):
        # Fresh KB: no compiled logic-policy.dl, prose-only logic-policy.md (no
        # rules) → empty base policy. But a hand-authored logic-policy.extra.dl
        # (#120 typed-comparison rule) MUST NOT be silently dropped just because
        # the compiled .dl is absent (justinjoy review). The extra.dl merge tail
        # has to run even on the no-policy branch.
        md = "# Logic policy\n\nWrite rules like `- [c1] ... ` here later.\n"
        dl = self._dl(tmp_path, md=md)
        extra = (
            ".decl after2030(entity: symbol, reason: symbol)\n"
            'after2030(S, "x") :- launch_date(S, D), D >= 20300101.\n'
        )
        (tmp_path / "policy" / "logic-policy.extra.dl").write_text(
            extra, encoding="utf-8"
        )
        text = common._load_logic_policy_from(dl)
        # The rule survives (not an empty string) and stays byte-clean: because
        # the base is empty there must be NO leading newline.
        assert text == extra.strip()
        assert "after2030" in text
        # And the predicate is preserved end-to-end (no silent drop).
        assert "after2030" in common.policy_predicates(text)

    def _assert_raises_uncompiled(self, dl):
        try:
            common._load_logic_policy_from(dl)
        except common.FactlogError as exc:
            msg = str(exc)
            assert "generate_logic_policy" in msg or "/factlog add" in msg
            assert "--force" not in msg
        else:
            raise AssertionError("expected FactlogError for uncompiled rules")

    def test_absent_dl_with_rules_still_raises(self, tmp_path):
        md = "# Logic policy\n\n- [c1] flag when `requires_review`\n"
        dl = self._dl(tmp_path, md=md)
        self._assert_raises_uncompiled(dl)

    def test_absent_dl_numbered_list_rule_still_raises(self, tmp_path):
        # Numbered lists compile too (markdown_policy_items accepts `\d+.`), so a
        # regex that only saw dashes would SILENTLY DROP this policy (#190 review).
        md = "# Logic policy\n\n1. [c1] flag when `requires_review`\n"
        dl = self._dl(tmp_path, md=md)
        self._assert_raises_uncompiled(dl)

    def test_absent_dl_multiline_bullet_rule_still_raises(self, tmp_path):
        # A bullet whose backtick relation wraps onto a continuation line still
        # compiles; a single-line regex would miss it and drop the policy.
        md = "# Logic policy\n\n- [c1] flag when the fact is\n  `requires_review`\n"
        dl = self._dl(tmp_path, md=md)
        self._assert_raises_uncompiled(dl)

    def test_absent_dl_fenced_example_only_is_empty_policy(self, tmp_path):
        # A .md that documents the rule grammar only inside a ``` code fence
        # defines ZERO live rules — markdown_policy_items skips fenced lines. A
        # look-alike regex would (wrongly) see the fenced bullet and hard-fail.
        md = (
            "# Logic policy\n\nExample syntax:\n\n"
            "```\n- [c1] flag when `requires_review`\n```\n\n"
            "Add your real rules above.\n"
        )
        dl = self._dl(tmp_path, md=md)
        assert common._load_logic_policy_from(dl) == ""

    def test_has_rules_helper(self, tmp_path):
        (tmp_path / "rules.md").write_text("- [c1] flag `rel`\n", encoding="utf-8")
        (tmp_path / "prose.md").write_text("just prose, no bullets\n", encoding="utf-8")
        (tmp_path / "notag.md").write_text("- a bullet with `rel` but no id\n", encoding="utf-8")
        assert common.logic_policy_md_has_rules(tmp_path / "rules.md") is True
        assert common.logic_policy_md_has_rules(tmp_path / "prose.md") is False
        assert common.logic_policy_md_has_rules(tmp_path / "notag.md") is False
        assert common.logic_policy_md_has_rules(tmp_path / "missing.md") is False

    def test_has_rules_matches_compiler(self, tmp_path):
        """has_rules(md) ⟺ generate_logic_policy compiles ≥1 rule — the drift
        boundary the #190 review demanded be pinned. fixture_policy_json raises
        SystemExit when it produces no rule, so 'compiles' == 'did not raise'."""
        import generate_logic_policy as glp

        cases = {
            "dash": "- [c1] flag `requires_review`\n",
            "numbered": "1. [c1] flag `requires_review`\n",
            "multiline": "- [c1] flag when\n  `requires_review`\n",
            "star": "* [c1] flag `requires_review`\n",
            "no_backtick": "- [c1] flag with no relation\n",
            "no_tag": "- flag `requires_review` but no id\n",
            "prose": "Just prose describing the policy.\n",
            "fenced_only": "```\n- [c1] flag `requires_review`\n```\n",
        }
        for name, md in cases.items():
            path = tmp_path / f"{name}.md"
            path.write_text(md, encoding="utf-8")
            helper = common.logic_policy_md_has_rules(path)
            try:
                glp.fixture_policy_json(md)
                compiler = True
            except SystemExit:
                compiler = False
            assert helper == compiler, f"{name}: helper={helper} compiler={compiler}"


class TestTwoKbsAreIndependent:
    def test_contexts_do_not_bleed(self, tmp_path):
        # Two KBs with different attribute-relation policies, read in one process.
        kb1 = _make_kb(tmp_path / "kb1", attribute_rels=["born_on"])
        kb2 = _make_kb(tmp_path / "kb2", attribute_rels=["height_cm"])
        assert kb1.attribute_relations() == {"born_on"}
        assert kb2.attribute_relations() == {"height_cm"}

    def test_entity_set_honours_context_attribute_rels(self, tmp_path):
        # 'born_on' is an attribute relation in this KB, so its object (a literal)
        # is excluded from the entity set while the subject is kept.
        ctx = _make_kb(tmp_path, attribute_rels=["born_on"])
        facts = [
            {"subject": "Ada", "relation": "born_on", "object": "1815", "status": "accepted"},
            {"subject": "Ada", "relation": "knows", "object": "Charles", "status": "accepted"},
        ]
        ents = common.entity_set(facts, ctx.attribute_relations())
        assert "Ada" in ents and "Charles" in ents
        assert "1815" not in ents  # literal object of an attribute relation
