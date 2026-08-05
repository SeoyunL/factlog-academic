# SPDX-License-Identifier: Apache-2.0
"""policy/attribute-relations.md must recognise a relation however it is spelled.

``relation/3`` — engine input and every python consumer — stores the RAW relation
name the fact was written with, and the exclusion compared that raw name to the
raw text of attribute-relations.md. Two spellings of the same relation therefore
turned the whole declaration off, silently:

* **alias** — declare `정식_운영`, alias `출시일` -> `정식_운영`, write the fact
  as `출시일`. Measured end to end before the fix:
  ``- path 갑봇 -> 2030.1: 갑봇 -> 을서비스 -> 2030.1``.
* **NFD** — author attribute-relations.md on macOS (NFD is routine there) while
  the facts are NFC. Same measured line.

The NFD half must be closed by carrying BOTH normal forms, not by folding one
side: the engine matches attr_rel/1 against relation/3's raw R and cannot fold,
so a renderer that folded would diverge from it. Folding the policy side alone
also just moves the miss onto NFD-authored facts — measured, it broke
tests/test_conflicts.sh's NFD case on the neighbouring single-valued axis.

Neither is a #329 regression — both predate it on the entity axis too — but the
declaration's whole contract is "these objects are not entities", and a
first-class feature (relation-aliases) was silently voiding it.
"""
from __future__ import annotations

import unicodedata

import pytest

from factlog import common as fl_common

REL = "정식_운영"
ALIAS = "출시일"


def _kb(tmp_path, declared: str, aliases: str | None = None):
    policy = tmp_path / "policy"
    policy.mkdir(parents=True, exist_ok=True)
    (policy / "attribute-relations.md").write_text(f"- `{declared}`\n", encoding="utf-8")
    if aliases is not None:
        (policy / "relation-aliases.md").write_text(aliases, encoding="utf-8")
    return fl_common.KbContext.for_root(tmp_path)


class TestAliasSpelling:
    def test_a_surface_alias_of_a_declared_relation_is_an_attribute_relation(self, tmp_path):
        ctx = _kb(tmp_path, REL, aliases=f"- `{ALIAS}` -> `{REL}`\n")
        assert {REL, ALIAS} <= ctx.attribute_relations()

    def test_declaring_the_surface_name_also_covers_the_canonical(self, tmp_path):
        # The policy author may write either spelling; expansion is bidirectional.
        ctx = _kb(tmp_path, ALIAS, aliases=f"- `{ALIAS}` -> `{REL}`\n")
        assert {REL, ALIAS} <= ctx.attribute_relations()

    def test_an_unrelated_alias_is_not_pulled_in(self, tmp_path):
        # CONTROL — the expansion must not swallow the rest of the alias file.
        ctx = _kb(tmp_path, REL, aliases=f"- `{ALIAS}` -> `{REL}`\n- `협력` -> `통합`\n")
        assert ctx.attribute_relations().isdisjoint({"협력", "통합"})


class TestUnicodeSpelling:
    def test_an_nfd_authored_declaration_matches_nfc_facts(self, tmp_path):
        ctx = _kb(tmp_path, unicodedata.normalize("NFD", REL))
        assert REL in ctx.attribute_relations()

    def test_an_nfd_authored_declaration_still_matches_nfd_facts(self, tmp_path):
        # The half a policy-side fold would have broken. Both spellings are
        # carried, so neither KB loses its exclusion.
        nfd = unicodedata.normalize("NFD", REL)
        assert nfd in _kb(tmp_path, nfd).attribute_relations()

    def test_an_nfc_authored_declaration_also_covers_nfd_facts(self, tmp_path):
        assert unicodedata.normalize("NFD", REL) in _kb(tmp_path, REL).attribute_relations()

    def test_an_nfc_authored_declaration_still_covers_itself(self, tmp_path):
        # CONTROL — passes before and after.
        assert REL in _kb(tmp_path, REL).attribute_relations()


class TestNoAliasFileIsUnchanged:
    """CONTROL — passes before and after. A KB with no relation-aliases.md must
    behave exactly as before, and must not even read the file: #242's gate
    invariant (tests/unit/test_query_literal_nfc.py) pins the read count."""

    def test_declaration_without_aliases(self, tmp_path):
        assert REL in _kb(tmp_path, REL).attribute_relations()

    def test_no_declaration_never_reads_the_alias_file(self, tmp_path, monkeypatch):
        reads: list[int] = []
        monkeypatch.setattr(
            fl_common, "relation_aliases", lambda *a, **k: reads.append(1) or {}
        )
        policy = tmp_path / "policy"
        policy.mkdir(parents=True, exist_ok=True)
        assert fl_common.KbContext.for_root(tmp_path).attribute_relations() == set()
        assert reads == []


class TestExclusionActuallyApplies:
    """The spelling fix has to reach the exclusion, not just the name set."""

    @pytest.mark.parametrize(
        "declared,fact_relation,aliases",
        [
            (REL, ALIAS, f"- `{ALIAS}` -> `{REL}`\n"),
            (unicodedata.normalize("NFD", REL), REL, None),
            (REL, unicodedata.normalize("NFD", REL), None),
        ],
    )
    def test_the_literal_is_not_a_path_node(self, tmp_path, monkeypatch, declared, fact_relation, aliases):
        ctx = _kb(tmp_path, declared, aliases=aliases)
        monkeypatch.setattr(fl_common, "attribute_relations", ctx.attribute_relations)
        facts = [
            {"subject": "갑봇", "relation": "통합", "object": "을서비스",
             "status": "accepted", "source": "sources/a.md"},
            {"subject": "을서비스", "relation": fact_relation, "object": "2030.1",
             "status": "accepted", "source": "sources/a.md"},
        ]
        assert fl_common.dependency_path(facts, "갑봇", "2030.1") == []
        assert fl_common.dependency_path(facts, "갑봇", "을서비스") == ["갑봇", "을서비스"]
