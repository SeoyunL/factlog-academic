"""A declared subtype is not a contradiction with its supertype (#219).

`연구유형: 코호트연구 ⊂ 관찰연구` means a cohort study IS an observational study, so a
paper carrying both is being described at two levels of precision -- both rows are
true. check_conflicts did not know the hierarchy, so it reported a conflict, finalize
refused to compile, and the resolution text told the user to retire one of two facts
a human had already checked.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from factlog.common import detect_conflicts  # noqa: E402

# The shape value_hierarchy() returns: transitively CLOSED at load time, so
# 전향코호트's ancestors already include 관찰연구.
HIER = {
    "연구유형": {
        "코호트연구": {"관찰연구"},
        "전향코호트": {"코호트연구", "관찰연구"},
    }
}
SV = {"연구유형"}


def rows(*triples):
    return [
        {"subject": s, "relation": r, "object": o, "status": "accepted"} for s, r, o in triples
    ]


def test_supertype_and_subtype_are_not_a_conflict():
    facts = rows(("P1", "연구유형", "관찰연구"), ("P1", "연구유형", "코호트연구"))
    assert detect_conflicts(facts, SV, hierarchy=HIER) == {}


def test_siblings_are_still_a_conflict():
    facts = rows(("P2", "연구유형", "관찰연구"), ("P2", "연구유형", "실험연구"))
    assert ("P2", "연구유형") in detect_conflicts(facts, SV, hierarchy=HIER)


def test_transitive_chain_is_not_a_conflict():
    facts = rows(
        ("P3", "연구유형", "관찰연구"),
        ("P3", "연구유형", "코호트연구"),
        ("P3", "연구유형", "전향코호트"),
    )
    assert detect_conflicts(facts, SV, hierarchy=HIER) == {}


def test_a_chain_plus_a_sibling_is_still_a_conflict():
    """The most-specific value must dominate EVERY other value, not merely one.

    관찰연구 / 코호트연구 sit on a chain, but 실험연구 is a genuine sibling -- a paper
    cannot be both. Requiring "some pair is related" would have swallowed this.
    """
    facts = rows(
        ("P4", "연구유형", "관찰연구"),
        ("P4", "연구유형", "코호트연구"),
        ("P4", "연구유형", "실험연구"),
    )
    assert ("P4", "연구유형") in detect_conflicts(facts, SV, hierarchy=HIER)


def test_no_hierarchy_declared_behaves_as_before():
    facts = rows(("P1", "연구유형", "관찰연구"), ("P1", "연구유형", "코호트연구"))
    assert ("P1", "연구유형") in detect_conflicts(facts, SV, hierarchy={})


def test_hierarchy_is_scoped_to_its_relation():
    """A subtype declared under one relation must not excuse another relation."""
    facts = rows(("P5", "분류", "관찰연구"), ("P5", "분류", "코호트연구"))
    assert ("P5", "분류") in detect_conflicts(facts, {"분류"}, hierarchy=HIER)


def test_a_typed_amount_hierarchy_matches_its_own_declaration():
    """Both sides go through the same key. Passing canonical_value to one and not the
    other meant a typed relation never matched its declaration, so the false conflict
    survived for exactly the values a hierarchy is most useful for."""
    hier = {"규모": {'amount(7,"억")': {'amount(10,"억")'}}}
    facts = rows(("P", "규모", 'amount(7,"억")'), ("P", "규모", 'amount(10,"억")'))
    assert detect_conflicts(facts, {"규모"}, hierarchy=hier) == {}


def test_an_NFD_fact_meets_its_NFC_declaration():
    """Policy files are NFC-normalized at parse time; fact rows are not, and
    macOS-authored text is routinely NFD."""
    import unicodedata

    nfd = lambda v: unicodedata.normalize("NFD", v)  # noqa: E731
    facts = rows(("P1", "연구유형", nfd("관찰연구")), ("P1", "연구유형", nfd("코호트연구")))
    assert detect_conflicts(facts, SV, hierarchy=HIER) == {}


def test_multiple_inheritance_is_not_a_conflict():
    """A ⊂ B and A ⊂ C with B, C unrelated: A is still the one most specific value that
    every other value subsumes, and all three are true of the subject."""
    hier = {"연구유형": {"코호트연구": {"관찰연구", "종단연구"}}}
    facts = rows(
        ("P", "연구유형", "관찰연구"),
        ("P", "연구유형", "종단연구"),
        ("P", "연구유형", "코호트연구"),
    )
    assert detect_conflicts(facts, {"연구유형"}, hierarchy=hier) == {}


def test_the_declaration_is_read_from_the_policy_file(tmp_path, monkeypatch):
    """The end-to-end path: policy/value-hierarchy.md -> value_hierarchy() -> the gate.

    Every other test injects a dict literal, so nothing pinned that a real policy file
    reaches detect_conflicts at all.
    """
    import factlog.common as fc

    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "value-hierarchy.md").write_text("- 연구유형: 코호트연구 ⊂ 관찰연구\n", encoding="utf-8")
    monkeypatch.setattr(fc, "POLICY_DIR", policy)
    facts = rows(("P1", "연구유형", "관찰연구"), ("P1", "연구유형", "코호트연구"))
    assert detect_conflicts(facts, SV) == {}  # hierarchy defaults to value_hierarchy()


def test_the_typed_scaler_equivalence_reaches_the_hierarchy():
    """_group_key collapses 억 ↔ 조; the hierarchy check must use the same key.

    Comparing raw strings meant a declaration written in 조 never met a fact written in
    억, even though the two are the same number and the grouping already treats them as
    one value -- the same "declaration and fact never meet" failure this issue is about,
    one level down.
    """
    from factlog.common import TypedRelSpec

    typed = {"매출": TypedRelSpec(type="amount", alias="rev")}
    hier = {"매출": {'amount(7,"억")': {'amount(1,"조")'}}}
    same = rows(("P", "매출", 'amount(7,"억")'), ("P", "매출", 'amount(10000,"억")'))
    assert detect_conflicts(same, {"매출"}, typed, hierarchy=hier) == {}

    # a genuine sibling in the same units is still a conflict
    sib = rows(("Q", "매출", 'amount(7,"억")'), ("Q", "매출", 'amount(5,"억")'))
    assert ("Q", "매출") in detect_conflicts(sib, {"매출"}, typed, hierarchy=hier)
