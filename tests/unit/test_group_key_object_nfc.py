# SPDX-License-Identifier: Apache-2.0
"""Untyped grouping keys fold the OBJECT to NFC (#307).

#295 folded the RELATION axis of the grouping key; #307 completes it on the OBJECT
axis for untyped values. A value authored in a mix of NFC and NFD (routine on
macOS) hashed to two distinct raw keys, so ``detect_conflicts`` saw a single-valued
relation "holding two values" and fired a FALSE conflict, and ``corroboration``
reported a false competition — for two spellings of the very same value.

The fold is pure NFC, deliberately NOT ``canonical_value``: ``canonical_value`` also
applies amount normalization, which on an UNTYPED relation would fold amount-shaped
strings and leak scalar equivalence into predicates that never declared it (the
#224/#218 contract; the "unparseable object degrades to raw key" case). Grouping
only needs Unicode-form equivalence, so pure NFC is the minimal, correct fold.

The scalar (typed) branch is untouched, and the displayed value stays verbatim:
detect_conflicts keeps the raw strings per key and reports ``min(raws)``.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import common  # noqa: E402
from common import TypedRelSpec  # noqa: E402

_REL = "연구유형"
_VAL_NFC = "관찰연구"
_VAL_NFD = unicodedata.normalize("NFD", _VAL_NFC)
_REP = min(_VAL_NFC, _VAL_NFD)

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
    assert _VAL_NFC != _VAL_NFD


# --- detect_conflicts: the false conflict is gone ----------------------------

def test_same_value_two_spellings_is_not_a_conflict():
    facts = [_row("P", _REL, _VAL_NFC), _row("P", _REL, _VAL_NFD)]
    assert common.detect_conflicts(facts, {_REL}) == {}


def test_real_value_difference_still_conflicts():
    facts = [_row("P", _REL, "2020"), _row("P", _REL, "2021")]
    conflicts = common.detect_conflicts(facts, {_REL})
    assert list(conflicts.values()) == [["2020", "2021"]]


def test_conflict_report_value_stays_verbatim_min_representative():
    # A genuine conflict where one value is authored in two spellings: the two
    # spellings collapse to one value, and the reported value is the min raw form.
    facts = [
        _row("P", _REL, _VAL_NFC),
        _row("P", _REL, _VAL_NFD),
        _row("P", _REL, "실험연구"),
    ]
    conflicts = common.detect_conflicts(facts, {_REL})
    assert list(conflicts.values()) == [sorted([_REP, "실험연구"])]


# --- no regressions: typed / unparseable / hierarchy -------------------------

_AMOUNT = TypedRelSpec(type="amount", alias="rev", units={"억": 100_000_000, "조": 1_000_000_000_000})


def test_typed_scalar_equivalence_unaffected():
    typed = {"매출": _AMOUNT}
    facts = [_row("P", "매출", 'amount(5400,"억")'), _row("P", "매출", 'amount(0.54,"조")')]
    assert common.detect_conflicts(facts, {"매출"}, typed) == {}


def test_unparseable_amount_degrades_to_raw_and_still_conflicts():
    typed = {"매출": _AMOUNT}
    facts = [_row("P", "매출", 'amount(bad,"x")'), _row("P", "매출", 'amount(other,"y")')]
    assert common.detect_conflicts(facts, {"매출"}, typed)


def test_hierarchy_suppression_survives_nfd_subtype():
    # 코호트연구 ⊂ 관찰연구 (transitively closed); the subtype authored NFD must still
    # match the declaration, so a paper carrying both is not a conflict (#219).
    hierarchy = {"연구유형": {"코호트연구": {"관찰연구"}}}
    facts = [
        _row("P", "연구유형", unicodedata.normalize("NFD", "코호트연구")),
        _row("P", "연구유형", "관찰연구"),
    ]
    assert common.detect_conflicts(facts, {"연구유형"}, None, None, hierarchy) == {}


# --- subprocess: check_conflicts and corroboration ---------------------------

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


def test_check_conflicts_exit_zero_for_two_spellings_of_one_value(tmp_path: Path):
    # The finalize gate must not block on a false conflict: same subject, same value
    # in two Unicode spellings is not a contradiction, so check_conflicts exits 0.
    kb = _make_kb(
        tmp_path,
        [
            f"P,{_REL},{_VAL_NFC},sources/s1.md,accepted,0.9,",
            f"P,{_REL},{_VAL_NFD},sources/s2.md,accepted,0.9,",
        ],
        ["s1.md", "s2.md"],
    )
    proc = _run([sys.executable, str(Path("tools") / "check_conflicts.py"), "--wiki", str(kb)], kb)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_corroboration_no_false_competition_for_two_spellings(tmp_path: Path):
    kb = _make_kb(
        tmp_path,
        [
            f"P,{_REL},{_VAL_NFC},sources/s1.md,accepted,0.9,",
            f"P,{_REL},{_VAL_NFD},sources/s2.md,accepted,0.9,",
        ],
        ["s1.md", "s2.md"],
    )
    proc = _run([sys.executable, str(Path("tools") / "corroboration.py")], kb)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    # One value in two spellings is NOT a competition.
    assert "competing values" not in out


def test_corroboration_min_representative_in_real_competition(tmp_path: Path):
    kb = _make_kb(
        tmp_path,
        [
            f"P,{_REL},{_VAL_NFC},sources/s1.md,accepted,0.9,",
            f"P,{_REL},{_VAL_NFD},sources/s2.md,accepted,0.9,",
            f"P,{_REL},실험연구,sources/s3.md,accepted,0.9,",
        ],
        ["s1.md", "s2.md", "s3.md"],
    )
    proc = _run([sys.executable, str(Path("tools") / "corroboration.py")], kb)
    out = proc.stdout + proc.stderr
    assert "competing values" in out
    # The two spellings collapse to one value backed by 2 sources, reported under
    # the min representative, competing with the genuinely different value.
    assert f"{_REP} (2 src)" in out
