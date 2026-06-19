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
