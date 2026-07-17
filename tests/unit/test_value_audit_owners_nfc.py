# SPDX-License-Identifier: Apache-2.0
"""value_audit owners fold to NFC — the last axis of the hardening (#314).

value_audit classifies a folded value collision in an IDENTITY relation as a
``duplicate_record`` only when it spans MORE THAN ONE subject (two records of one
thing); otherwise it is a ``split`` (a query leak). That subject count was taken
over RAW subject strings, so ONE subject authored in a mix of NFC and NFD (routine
on macOS) counted as two owners — and a categorical split was misclassified as a
duplicate_record, which the ``--strict`` gate then EXEMPTS from the leak count. So
the bug was unsound in the dangerous direction: a real leak slipped the gate.

Folding the owner set to NFC fixes both the classification and the cosmetic
``subjects`` display (which otherwise listed one subject twice, once per spelling).
The fold is pure NFC, so subjects that differ by more than Unicode form (full-width,
compatibility characters) stay distinct. The object axis is untouched: ``subjects``
is keyed on the raw object and the collision values are raw object strings from the
same source, so that axis is already raw-on-raw consistent.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import value_audit  # noqa: E402

_ID_REL = "논문식별자"  # an identity relation: the value (a DOI) names one paper
_SUBJ_NFC = "김철수"
_SUBJ_NFD = unicodedata.normalize("NFD", _SUBJ_NFC)
_SUBJ_REP = min(_SUBJ_NFC, _SUBJ_NFD)


def _row(subject: str, relation: str, object_: str):
    return {"subject": subject, "relation": relation, "object": object_, "status": "accepted"}


def test_subject_spellings_are_byte_distinct():
    assert _SUBJ_NFC != _SUBJ_NFD


# --- classification ----------------------------------------------------------

def test_one_subject_two_spellings_is_a_split_not_duplicate_record():
    # One subject, authored NFC and NFD, sharing a folded identity value across two
    # surfaces. Raw-counted this looks like two owners (a duplicate record); folded
    # it is one owner, so it is a categorical split — a real query leak.
    facts = [_row(_SUBJ_NFC, _ID_REL, "10.1000/X"), _row(_SUBJ_NFD, _ID_REL, "10.1000/x")]
    dup = value_audit.audit(facts, identity_relations={_ID_REL})["duplicates"][0]
    assert dup["kind"] == "split"
    assert dup["subjects"] == _SUBJ_REP  # single deterministic representative


def test_two_genuine_subjects_is_still_a_duplicate_record():
    facts = [_row("김철수", _ID_REL, "10.1000/X"), _row("이영희", _ID_REL, "10.1000/x")]
    dup = value_audit.audit(facts, identity_relations={_ID_REL})["duplicates"][0]
    assert dup["kind"] == "duplicate_record"


def test_full_width_subject_stays_distinct_from_ascii():
    # NFC (canonical), NOT NFKC (compatibility): a full-width 'Ａ' is a different
    # subject from ascii 'A', so two such subjects remain two owners.
    facts = [_row("Ａ", _ID_REL, "10.1000/X"), _row("A", _ID_REL, "10.1000/x")]
    dup = value_audit.audit(facts, identity_relations={_ID_REL})["duplicates"][0]
    assert dup["kind"] == "duplicate_record"


def test_categorical_split_subjects_display_is_one_representative():
    # A categorical (non-identity) relation: always a split. The point here is the
    # display — one subject in two spellings must show ONE representative, not the
    # same name twice.
    rel = "대상질환"
    facts = [_row(_SUBJ_NFC, rel, "IL-8"), _row(_SUBJ_NFD, rel, "il 8")]
    dup = value_audit.audit(facts, identity_relations=set())["duplicates"][0]
    assert dup["kind"] == "split"
    assert dup["subjects"] == _SUBJ_REP
    assert "," not in dup["subjects"]  # not "김철수, 김철수"


# --- no regression / determinism --------------------------------------------

def test_nfc_only_unchanged():
    facts = [_row("김철수", _ID_REL, "10.1000/X"), _row("이영희", _ID_REL, "10.1000/x")]
    dup = value_audit.audit(facts, identity_relations={_ID_REL})["duplicates"][0]
    assert dup["kind"] == "duplicate_record"
    assert dup["subjects"] == "김철수, 이영희"


def test_owner_display_is_order_independent():
    facts = [_row(_SUBJ_NFC, _ID_REL, "10.1000/X"), _row(_SUBJ_NFD, _ID_REL, "10.1000/x")]
    a = value_audit.audit(facts, identity_relations={_ID_REL})["duplicates"]
    b = value_audit.audit(list(reversed(facts)), identity_relations={_ID_REL})["duplicates"]
    assert a == b


# --- --strict gate soundness (real tool, subprocess) -------------------------

def _run_strict(tmp_path: Path, rows: list[str]) -> subprocess.CompletedProcess:
    kb = tmp_path / "kb"
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        capture_output=True,
        check=True,
    )
    (kb / "sources" / "s.md").write_text("s\n", encoding="utf-8")
    (kb / "policy" / "identity-relations.md").write_text(f"# identity\n- {_ID_REL}\n", encoding="utf-8")
    header = "subject,relation,object,source,status,confidence,note"
    (kb / "facts" / "candidates.csv").write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(Path("tools") / "value_audit.py"), "--strict"],
        capture_output=True,
        text=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
    )


def test_strict_catches_leak_that_a_false_duplicate_record_would_exempt(tmp_path: Path):
    # One subject, two spellings, sharing a folded identity value: a real query leak.
    # Misclassified as a duplicate_record it would slip --strict (exit 0); as a split
    # it is a provable leak and --strict must exit non-zero.
    proc = _run_strict(
        tmp_path,
        [
            f"{_SUBJ_NFC},{_ID_REL},10.1000/X,sources/s.md,accepted,0.9,",
            f"{_SUBJ_NFD},{_ID_REL},10.1000/x,sources/s.md,accepted,0.9,",
        ],
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr


def test_strict_does_not_fire_on_a_genuine_duplicate_record(tmp_path: Path):
    # Two genuine subjects sharing an identifying value is a possible duplicate
    # record, not a leak — --strict stays 0.
    proc = _run_strict(
        tmp_path,
        [
            f"김철수,{_ID_REL},10.1000/X,sources/s.md,accepted,0.9,",
            f"이영희,{_ID_REL},10.1000/x,sources/s.md,accepted,0.9,",
        ],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
