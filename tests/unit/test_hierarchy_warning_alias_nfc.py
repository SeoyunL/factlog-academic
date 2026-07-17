# SPDX-License-Identifier: Apache-2.0
"""value_hierarchy_warnings must not condemn a working declaration (#211 inverted).

``_canon_rel`` (``factlog/common.py:1190``) is::

    return _canonical_value(aliases.get(name, name))

The fold runs on the RESULT, so the ``aliases.get`` still probes the NFC-keyed alias map
with the raw name. An NFD-authored alias row therefore never resolves to its canonical,
the declared relation looks unused, and the checker emits::

    value-hierarchy: no accepted fact uses relation 'study_type' — declaration has no effect

The declaration is not ineffective — the NFC-authored KB subsumes correctly on the same
facts. This is worse than noise: the docstring at :1182-1187 exists specifically to stop
a user from deleting a live declaration ("a user who believes it and deletes the
declaration gets the silent omission back"), and the check makes that exact mistake one
Unicode form over. Following its advice converts a false warning into a real silent
omission.

Fix mirrors ``_canonicalize`` (:942): ``_canonical_value(aliases.get(_nfc(name), name))``.
"""
from __future__ import annotations

import unicodedata

import pytest

import common


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


STUDY_TYPE = "연구유형"
HIERARCHY_MD = "- study_type: `코호트연구` ⊂ `관찰연구`\n"
ALIASES_MD = f"# relation aliases\n\n- `{_nfc(STUDY_TYPE)}` -> `study_type`\n"


@pytest.fixture
def kb(tmp_path):
    def _build(form: str):
        root = tmp_path / form
        (root / "policy").mkdir(parents=True)
        (root / "facts").mkdir(parents=True)
        (root / "policy" / "relation-aliases.md").write_text(ALIASES_MD, encoding="utf-8")
        (root / "policy" / "value-hierarchy.md").write_text(HIERARCHY_MD, encoding="utf-8")
        return root

    return _build


def _facts(form: str) -> list[dict[str, str]]:
    return [
        {
            "subject": "p1",
            "relation": unicodedata.normalize(form, STUDY_TYPE),
            "object": _nfc("코호트연구"),
            "status": "accepted",
        }
    ]


class TestNoFalseDeclarationHasNoEffect:
    @pytest.mark.parametrize("form", ["NFC", "NFD"])
    def test_declaration_is_not_reported_unused(self, kb, form):
        root = kb(form)
        warnings = common.value_hierarchy_warnings(root=root, facts=_facts(form))
        assert not any("has no effect" in w for w in warnings), (
            f"{form}-authored alias row: a working declaration was reported unused "
            f"-> {warnings}"
        )

    def test_nfc_and_nfd_agree(self, kb):
        nfc = common.value_hierarchy_warnings(root=kb("NFC"), facts=_facts("NFC"))
        nfd = common.value_hierarchy_warnings(root=kb("NFD"), facts=_facts("NFD"))
        assert nfc == nfd, f"NFC -> {nfc}, NFD -> {nfd}"


# A hierarchy declared on the alias RAW name (`연구유형`), not the canonical
# (`study_type`). The facts index by canonical name, so the declaration key must
# resolve through the alias axis or a live declaration is condemned "no effect".
ALIAS_RAW_HIERARCHY_MD = f"- {_nfc(STUDY_TYPE)}: `코호트연구` ⊂ `관찰연구`\n"


@pytest.fixture
def alias_raw_kb(tmp_path):
    def _build(form: str):
        root = tmp_path / f"aliasraw-{form}"
        (root / "policy").mkdir(parents=True)
        (root / "facts").mkdir(parents=True)
        (root / "policy" / "relation-aliases.md").write_text(ALIASES_MD, encoding="utf-8")
        (root / "policy" / "value-hierarchy.md").write_text(ALIAS_RAW_HIERARCHY_MD, encoding="utf-8")
        return root

    return _build


class TestHierarchyDeclaredOnAliasRawName:
    """#344: the declared relation name is resolved through the SAME alias axis as
    the facts. A KB declaring the hierarchy on the alias raw name `연구유형` (rather
    than the canonical `study_type`) must not be told its live declaration has no
    effect — following that advice deletes a working declaration (#211 inverted).
    Independent of NFC/NFD: the fold is on the alias axis, not the Unicode form.
    """

    @pytest.mark.parametrize("form", ["NFC", "NFD"])
    def test_alias_raw_declaration_is_not_reported_unused(self, alias_raw_kb, form):
        root = alias_raw_kb(form)
        warnings = common.value_hierarchy_warnings(root=root, facts=_facts(form))
        assert not any("has no effect" in w for w in warnings), (
            f"{form}: a hierarchy declared on the alias raw name was condemned "
            f"though it is live -> {warnings}"
        )

    @pytest.mark.parametrize("form", ["NFC", "NFD"])
    def test_the_condemned_declaration_would_really_be_live(self, alias_raw_kb, form):
        """Cross-check: the declaration the checker (before #344) called inert
        actually subsumes the facts, so the false warning would delete a live one."""
        root = alias_raw_kb(form)
        facts = _facts(form)
        hierarchy = common.value_hierarchy(root)
        aliases = common.relation_aliases(root)
        assert common.relation_row_matches(
            ['"p1"', f'"{_nfc(STUDY_TYPE)}"', '"관찰연구"'], facts[0], aliases, hierarchy
        ), "the declaration the warning condemns is in fact live"


DEAD_RELATION = "nosuch"
DEAD_HIERARCHY_MD = f"- {DEAD_RELATION}: `코호트연구` ⊂ `관찰연구`\n"


@pytest.fixture
def dead_kb(tmp_path):
    """A KB declaring a relation that no alias and no fact ever mentions."""

    def _build(form: str):
        root = tmp_path / f"dead-{form}"
        (root / "policy").mkdir(parents=True)
        (root / "facts").mkdir(parents=True)
        (root / "policy" / "relation-aliases.md").write_text(ALIASES_MD, encoding="utf-8")
        (root / "policy" / "value-hierarchy.md").write_text(DEAD_HIERARCHY_MD, encoding="utf-8")
        return root

    return _build


class TestTheAdviceIsSound:
    """`condemned` and `inert` must be the same thing, asserted both ways.

    Pinned on a genuinely unused relation rather than on the live one: a dead
    declaration must both be reported (a) and really subsume nothing (b).
    Keeping the true positive is what holds the #326 fold honest — silencing the
    false warning by weakening the check for every relation would satisfy "no
    false alarm" while destroying #211, and (a) fails the moment that happens.
    No early-out, so this cannot lapse back into a vacuous skip.
    """

    @pytest.mark.parametrize("form", ["NFC", "NFD"])
    def test_a_condemned_declaration_really_is_inert(self, dead_kb, form):
        root = dead_kb(form)
        facts = _facts(form)
        warnings = common.value_hierarchy_warnings(root=root, facts=facts)

        condemned = any(DEAD_RELATION in w and "has no effect" in w for w in warnings)
        assert condemned, (
            f"relation {DEAD_RELATION!r} is mentioned by no alias and no fact, but "
            f"the checker stayed silent — #211's real warning was lost -> {warnings}"
        )

        hierarchy = common.value_hierarchy(root)
        aliases = common.relation_aliases(root)
        subsumes = common.relation_row_matches(
            ['"p1"', f'"{DEAD_RELATION}"', '"관찰연구"'], facts[0], aliases, hierarchy
        )
        assert not subsumes, (
            "the checker advises deleting this declaration, but it is live — "
            "deleting it would reintroduce the silent omission #211 exists to prevent"
        )
