"""The specialisation-chain gate must find its typed spec THE way grouping does (#286).

`detect_conflicts` groups values with ``_lookup_typed_spec(relation, typed, aliases)``
(the single lookup rule #244 introduced), but the chain suppression used a raw
``typed.get(canon)``. ``typed`` is keyed by the NFC name, so an NFD-authored relation
missed its spec there: the chain check fell back to string comparison, the declared
hierarchy never matched, and a pair the source states at two levels of precision was
reported as a contradiction -- which makes check_conflicts exit 1 and finalize refuse
to compile (the very #219 failure mode, back for NFD).

Grouping already folds NFD via the shared lookup, so the two sides disagreed only on
the chain lookup; unifying them is exactly the "one lookup rule" this issue asks for.
"""

import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from factlog.common import TypedRelSpec, detect_conflicts  # noqa: E402

nfc = lambda v: unicodedata.normalize("NFC", v)  # noqa: E731
nfd = lambda v: unicodedata.normalize("NFD", v)  # noqa: E731


def rows(*triples):
    return [
        {"subject": s, "relation": r, "object": o, "status": "accepted"} for s, r, o in triples
    ]


# Reproduction from the issue: facts, hierarchy and spec are identical; only the
# unicode normal form of the RELATION name differs between the two cases. The parent
# value amount(1000000000,"원") is the scalar twin of 10억, so amount(7,"억") sits on a
# declared chain under amount(10,"억") -- the pair is not a contradiction.
UNITS = {"억": 100000000, "원": 1}
REL = "예산"
SPEC = TypedRelSpec(type="amount", alias="budget", units=UNITS)
FACT_LO = 'amount(7,"억")'
FACT_HI = 'amount(10,"억")'
PARENT = 'amount(1000000000,"원")'


def _hier(rel):
    return {rel: {FACT_LO: {PARENT}}}


def test_nfc_relation_is_not_a_false_conflict():
    rel = nfc(REL)
    typed = {nfc(REL): SPEC}
    facts = rows(("갑사", rel, FACT_LO), ("갑사", rel, FACT_HI))
    assert detect_conflicts(facts, {rel}, typed, {}, _hier(rel)) == {}


def test_nfd_relation_is_not_a_false_conflict():
    """The regression: before the fix this returned a conflict because the chain
    gate's typed.get(NFD) missed the NFC-keyed spec and fell back to raw strings."""
    rel = nfd(REL)
    typed = {nfc(REL): SPEC}
    facts = rows(("갑사", rel, FACT_LO), ("갑사", rel, FACT_HI))
    assert detect_conflicts(facts, {rel}, typed, {}, _hier(rel)) == {}


def test_a_genuine_contradiction_without_a_hierarchy_still_fires_nfc():
    """No declared chain: two distinct scalars are a real conflict, NFC form."""
    rel = nfc(REL)
    typed = {nfc(REL): SPEC}
    facts = rows(("갑사", rel, FACT_LO), ("갑사", rel, FACT_HI))
    assert (("갑사", rel)) in detect_conflicts(facts, {rel}, typed, {}, {})


def test_a_genuine_contradiction_without_a_hierarchy_still_fires_nfd():
    """The no-regression twin: an NFD relation with no chain must still be caught,
    so the unified lookup did not accidentally suppress real contradictions."""
    rel = nfd(REL)
    typed = {nfc(REL): SPEC}
    facts = rows(("갑사", rel, FACT_LO), ("갑사", rel, FACT_HI))
    assert (("갑사", rel)) in detect_conflicts(facts, {rel}, typed, {}, {})


def test_nfd_siblings_off_the_declared_chain_are_still_a_conflict():
    """Over-suppression guard: a hierarchy IS declared, but the two facts do not
    form a chain, so the conflict must survive -- even for an NFD relation. Finding
    the spec via the shared lookup must not turn the chain gate into a blanket
    "typed relation is never a conflict"; it only excuses values on ONE declared
    ancestor chain. The declaration is 7억 ⊂ 10억; the facts are 10억 and 20억.
    20억 sits nowhere on that chain, and 10억 (the chain's top) does not subsume it,
    so neither value dominates the other."""
    rel = nfd(REL)
    typed = {nfc(REL): SPEC}
    hier = {rel: {FACT_LO: {PARENT}}}  # amount(7,"억") ⊂ amount(1000000000,"원") == 10억
    facts = rows(("갑사", rel, 'amount(10,"억")'), ("갑사", rel, 'amount(20,"억")'))
    assert (("갑사", rel)) in detect_conflicts(facts, {rel}, typed, {}, hier)
