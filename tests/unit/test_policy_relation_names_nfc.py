# SPDX-License-Identifier: Apache-2.0
"""Policy relation-name loading normalises to NFC (#285).

Policy files authored on macOS are frequently stored in NFD (decomposed Hangul),
while facts extracted elsewhere carry NFC (composed) relation names. The two are
canonically equivalent but byte-distinct, so a relation declared in a policy file
under NFD never matched an NFC fact: the declaration silently did nothing.

``common._relation_names_from`` is the single loader behind the one-name-per-line
relation policies (single-valued, identity, attribute…), so normalising there
fixes all of them at once. (Typed relation names are not loaded here; they get
their own NFC normalisation in ``_parse_typed_relations``.) These tests pin the
two downstream failures that motivated the fix — a missed contradiction and a
mis-classified duplicate — plus the loader invariant itself.
"""
from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import common  # noqa: E402
import value_audit  # noqa: E402

# Composed (NFC) Hangul relation names, and their decomposed (NFD) spellings as a
# macOS-authored policy file would store them. The two are byte-distinct.
_SV_NFC = "출판연도"          # single-valued: a paper has one publication year
_ID_NFC = "논문식별자"        # identity: the value (a DOI) names exactly one paper
_SV_NFD = unicodedata.normalize("NFD", _SV_NFC)
_ID_NFD = unicodedata.normalize("NFD", _ID_NFC)


def _row(subject: str, relation: str, object_: str) -> dict[str, str]:
    return {"subject": subject, "relation": relation, "object": object_, "status": "accepted"}


def test_nfd_and_nfc_spellings_are_byte_distinct():
    # Guards the premise: if these were already equal the other tests would pass
    # vacuously and prove nothing.
    assert _SV_NFD != _SV_NFC
    assert _ID_NFD != _ID_NFC


def test_relation_names_from_returns_nfc(tmp_path: Path):
    path = tmp_path / "single-valued.md"
    path.write_text(f"- `{_SV_NFD}`\n", encoding="utf-8")

    names = common._relation_names_from(path)

    assert names == {_SV_NFC}
    assert all(unicodedata.is_normalized("NFC", n) for n in names)
    assert _SV_NFD not in names


def test_detect_conflicts_sees_nfd_declared_single_valued_relation(tmp_path: Path):
    # An NFD-authored single-valued declaration must still contradict two distinct
    # NFC-authored values for the same subject. Before the fix the loaded name kept
    # its NFD bytes, never matched the NFC fact relation, and detect_conflicts
    # returned {} — the contradiction was silently dropped.
    path = tmp_path / "single-valued.md"
    path.write_text(f"- `{_SV_NFD}`\n", encoding="utf-8")
    single_valued = common._relation_names_from(path)

    facts = [
        _row("paperA", _SV_NFC, "2020"),
        _row("paperA", _SV_NFC, "2021"),
    ]
    conflicts = common.detect_conflicts(facts, single_valued)

    assert conflicts, "NFD-declared single-valued relation must catch the NFC conflict"
    assert conflicts[("paperA", _SV_NFC)] == ["2020", "2021"]


def test_value_audit_nfd_identity_relation_classifies_duplicate_record(tmp_path: Path):
    # An NFD-authored identity-relations.md must classify a folded value collision
    # across two subjects as a duplicate RECORD, not a spelling split. Before the
    # fix the loaded identity name kept its NFD bytes, so the NFC fact relation was
    # never recognised as an identity relation and the collision was mis-reported
    # as "split" (a false query-leak).
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    (policy_dir / "identity-relations.md").write_text(f"- `{_ID_NFD}`\n", encoding="utf-8")

    identity = common.identity_relations(root=tmp_path)
    assert identity == {_ID_NFC}
    assert all(unicodedata.is_normalized("NFC", n) for n in identity)

    # Same DOI under two case spellings, held by two different papers → duplicate
    # records once the relation is recognised as an identity relation.
    facts = [
        _row("paperA", _ID_NFC, "10.1000/X"),
        _row("paperB", _ID_NFC, "10.1000/x"),
    ]
    dup = value_audit.audit(facts, identity_relations=identity)["duplicates"][0]

    assert dup["kind"] == "duplicate_record"
