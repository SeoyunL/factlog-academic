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

from check_conflicts import detect_conflicts  # noqa: E402

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
