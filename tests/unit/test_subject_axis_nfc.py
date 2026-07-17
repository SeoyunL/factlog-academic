# SPDX-License-Identifier: Apache-2.0
"""The SUBJECT axis of the grouping key folds to NFC (#310).

#295 (relation) and #307 (object) folded two of the three axes of the conflict /
competition grouping key. #310 completes it on the subject: two rows about one
subject authored in a mix of NFC and NFD (routine on macOS) hashed to two buckets,
so a single-valued relation holding two *different* values under that subject —
a real contradiction — was split into two clean-looking halves and never fired.
That is the dangerous direction: a missed conflict lets the finalize gate pass on
inconsistent data.

Both the reported subject and relation are the deterministic representative
``min(raw spellings seen)`` (provenance, #227), and the fold is pure NFC, so
subjects that differ by more than Unicode form (full-width, compatibility chars)
stay distinct. Because ``eject``/``amend`` already fold both sides to NFC when
matching a triple, the representative spelling the report prints resolves every
row of the conflict regardless of how each was authored — verified end to end.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import common  # noqa: E402

_REL = "출판연도"
_SUBJ_NFC = "김철수"
_SUBJ_NFD = unicodedata.normalize("NFD", _SUBJ_NFC)
_SUBJ_REP = min(_SUBJ_NFC, _SUBJ_NFD)

_HEADER = "subject,relation,object,source,status,confidence,note"


def _row(subject: str, relation: str, object_: str, source: str = "s"):
    return {
        "subject": subject,
        "relation": relation,
        "object": object_,
        "source": source,
        "status": "accepted",
        "confidence": "0.9",
        "note": "",
    }


def test_spellings_are_byte_distinct():
    assert _SUBJ_NFC != _SUBJ_NFD


# --- detect_conflicts --------------------------------------------------------

def test_cross_spelling_subject_contradiction_is_detected():
    # One subject in two spellings, two different values: a real contradiction that
    # a raw-subject key would split into two conflict-free buckets and miss.
    facts = [_row(_SUBJ_NFC, _REL, "2020"), _row(_SUBJ_NFD, _REL, "2021")]
    conflicts = common.detect_conflicts(facts, {_REL})

    assert list(conflicts) == [(_SUBJ_REP, _REL)]
    assert conflicts[(_SUBJ_REP, _REL)] == ["2020", "2021"]


def test_genuinely_different_subjects_stay_separate():
    # Two DIFFERENT subjects (not Unicode-equivalent) must not merge into a false
    # cross-subject conflict.
    facts = [_row("김철수", _REL, "2020"), _row("이영희", _REL, "2021")]
    assert common.detect_conflicts(facts, {_REL}) == {}


def test_nfc_only_single_spelling_conflict_unchanged():
    facts = [_row(_SUBJ_NFC, _REL, "2020"), _row(_SUBJ_NFC, _REL, "2021")]
    conflicts = common.detect_conflicts(facts, {_REL})
    assert list(conflicts) == [(_SUBJ_NFC, _REL)]


def test_order_independent():
    facts = [_row(_SUBJ_NFC, _REL, "2020"), _row(_SUBJ_NFD, _REL, "2021")]
    assert common.detect_conflicts(facts, {_REL}) == common.detect_conflicts(
        list(reversed(facts)), {_REL}
    )


# --- subprocess: check_conflicts, eject resolution, corroboration ------------

def _make_kb(tmp_path: Path, rows: list[str], sources: list[str]) -> Path:
    kb = tmp_path / "kb"
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        capture_output=True,
        check=True,
    )
    for s in sources:
        (kb / "sources" / s).write_text(s + "\n", encoding="utf-8")
    (kb / "policy" / "single-valued.md").write_text(f"# single-valued\n- {_REL}\n", encoding="utf-8")
    (kb / "facts" / "candidates.csv").write_text("\n".join([_HEADER, *rows]) + "\n", encoding="utf-8")
    return kb


def _run(argv: list[str], kb: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
    )


def _check_conflicts(kb: Path) -> subprocess.CompletedProcess:
    return _run([sys.executable, str(Path("tools") / "check_conflicts.py"), "--wiki", str(kb)], kb)


def test_check_conflicts_and_eject_resolution_end_to_end(tmp_path: Path):
    # The subject-2020 value is authored in BOTH spellings; the contradiction is
    # 2020 vs 2021. The reported representative spelling must, via eject, match and
    # retire every row of the losing value regardless of how each was authored.
    kb = _make_kb(
        tmp_path,
        [
            f"{_SUBJ_NFC},{_REL},2020,sources/s1.md,accepted,0.9,",
            f"{_SUBJ_NFD},{_REL},2020,sources/s2.md,accepted,0.9,",
            f"{_SUBJ_NFC},{_REL},2021,sources/s3.md,accepted,0.9,",
        ],
        ["s1.md", "s2.md", "s3.md"],
    )
    # Conflict is present before resolution.
    assert _check_conflicts(kb).returncode == 1

    # Eject the losing value under the reported representative subject spelling.
    ej = _run(
        [sys.executable, "-m", "factlog", "eject", "--fact", _SUBJ_REP, _REL, "2020",
         "--target", str(kb), "--purge"],
        kb,
    )
    assert ej.returncode == 0, ej.stdout + ej.stderr
    # BOTH the NFC- and NFD-authored 2020 rows matched and were purged.
    assert "2 candidate row(s) to purge" in ej.stdout, ej.stdout

    # Conflict is gone: only 2021 remains under the subject.
    assert _check_conflicts(kb).returncode == 0


def test_corroboration_three_axis_merge(tmp_path: Path):
    # Subject and value each authored in two spellings collapse to one competitor
    # (source union across spellings), reported under the min representatives, while
    # a genuinely different value W is the second competitor.
    val_nfc = "관찰연구"
    val_nfd = unicodedata.normalize("NFD", val_nfc)
    val_rep = min(val_nfc, val_nfd)
    kb = _make_kb(
        tmp_path,
        [
            f"{_SUBJ_NFC},{_REL},{val_nfc},sources/s1.md,accepted,0.9,",
            f"{_SUBJ_NFD},{_REL},{val_nfd},sources/s2.md,accepted,0.9,",
            f"{_SUBJ_NFC},{_REL},실험연구,sources/s3.md,accepted,0.9,",
        ],
        ["s1.md", "s2.md", "s3.md"],
    )
    out = _run([sys.executable, str(Path("tools") / "corroboration.py")], kb).stdout
    assert "competing values" in out
    # One subject, one value each in two spellings: value backed by 2 unioned
    # sources, reported under both min representatives.
    assert f"{_SUBJ_REP} / {_REL}: {val_rep} (2 src); 실험연구 (1 src)" in out
