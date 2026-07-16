# SPDX-License-Identifier: Apache-2.0
"""Grouping keys fold to NFC, reports name a deterministic representative (#295).

#285/#292/#293 folded the relation-name MEMBERSHIP comparisons to NFC. This is the
grouping-gap completion: three consumers bucketed rows by the *raw* relation name,
so a KB that authored one logical relation in a mix of NFC and NFD (routine on
macOS) split a single relation into two buckets — and a contradiction, a duplicate,
or a competition that spanned the two spellings fell through, each half looking
clean on its own.

  - ``detect_conflicts`` — a cross-spelling contradiction was two 1-value buckets.
  - ``value_audit`` — a cross-spelling value duplicate never met.
  - ``corroboration`` — the competing-values view split the contest.

All three now bucket on the NFC form and, because the reported name must stay a
spelling that actually occurs (provenance, #227), report ``min(raw spellings)`` as
the deterministic representative. NFC-only KBs are unaffected: the fold is a no-op
and ``min`` of a one-element set is that element, so output is byte-identical.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import common  # noqa: E402
import value_audit  # noqa: E402

# One relation, two byte-distinct Unicode spellings (composed vs decomposed).
_R_NFC = "출판연도"
_R_NFD = unicodedata.normalize("NFD", _R_NFC)
_REP = min(_R_NFC, _R_NFD)  # the deterministic representative the reports must use

_HEADER = "subject,relation,object,source,status,confidence,note"


def _row(subject: str, relation: str, object_: str, source: str = "s", status: str = "accepted"):
    return {
        "subject": subject,
        "relation": relation,
        "object": object_,
        "source": source,
        "status": status,
        "confidence": "0.9",
        "note": "",
    }


def test_spellings_are_byte_distinct():
    assert _R_NFC != _R_NFD


# --- detect_conflicts --------------------------------------------------------

def test_detect_conflicts_folds_cross_spelling_contradiction():
    # Same subject, one relation authored NFC in one row and NFD in the other, two
    # distinct values. Split by raw spelling this is two clean 1-value buckets;
    # folded it is one contradiction reported under the min representative.
    facts = [_row("P", _R_NFC, "2020"), _row("P", _R_NFD, "2021")]
    conflicts = common.detect_conflicts(facts, {_R_NFC})

    assert list(conflicts) == [("P", _REP)]
    assert conflicts[("P", _REP)] == ["2020", "2021"]


def test_detect_conflicts_is_order_independent():
    facts = [_row("P", _R_NFC, "2020"), _row("P", _R_NFD, "2021")]
    assert common.detect_conflicts(facts, {_R_NFC}) == common.detect_conflicts(
        list(reversed(facts)), {_R_NFC}
    )


def test_detect_conflicts_nfc_only_unchanged():
    # No NFD anywhere: the fold is a no-op and the reported name is the verbatim NFC.
    facts = [_row("P", _R_NFC, "2020"), _row("P", _R_NFC, "2021")]
    conflicts = common.detect_conflicts(facts, {_R_NFC})
    assert list(conflicts) == [("P", _R_NFC)]


# --- value_audit -------------------------------------------------------------

def test_value_audit_folds_cross_spelling_duplicate():
    # A folded value duplicate (IL-8 / il 8) whose two rows are authored under two
    # relation spellings. Only when the relation buckets merge do the values meet.
    facts = [_row("S1", _R_NFC, "IL-8"), _row("S2", _R_NFD, "il 8")]
    dups = value_audit.audit(facts)["duplicates"]

    assert len(dups) == 1
    assert dups[0]["relation"] == _REP
    assert dups[0]["kind"] == "split"


def test_value_audit_nfc_only_unchanged():
    facts = [_row("S1", _R_NFC, "IL-8"), _row("S2", _R_NFC, "il 8")]
    dups = value_audit.audit(facts)["duplicates"]
    assert len(dups) == 1
    assert dups[0]["relation"] == _R_NFC


# --- corroboration (real tool, subprocess) -----------------------------------

def _write_kb(tmp_path: Path, rows: list[str]) -> Path:
    kb = tmp_path / "kb"
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        capture_output=True,
        check=True,
    )
    (kb / "sources" / "s1.md").write_text("s1\n", encoding="utf-8")
    (kb / "sources" / "s2.md").write_text("s2\n", encoding="utf-8")
    (kb / "sources" / "s3.md").write_text("s3\n", encoding="utf-8")
    (kb / "policy" / "single-valued.md").write_text(
        f"# single-valued\n- {_R_NFC}\n", encoding="utf-8"
    )
    (kb / "facts" / "candidates.csv").write_text(
        "\n".join([_HEADER, *rows]) + "\n", encoding="utf-8"
    )
    return kb


def _corroboration(kb: Path) -> str:
    proc = subprocess.run(
        [sys.executable, str(Path("tools") / "corroboration.py")],
        capture_output=True,
        text=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
        check=True,
    )
    return proc.stdout + proc.stderr


# object A is backed by one source under each relation spelling; B by one source.
_MIXED_ROWS = [
    f"P,{_R_NFC},A,sources/s1.md,accepted,0.9,",
    f"P,{_R_NFD},A,sources/s2.md,accepted,0.9,",
    f"P,{_R_NFC},B,sources/s3.md,accepted,0.9,",
]


def test_corroboration_folds_and_sums_across_spellings(tmp_path: Path):
    out = _corroboration(_write_kb(tmp_path, _MIXED_ROWS))

    assert "single-valued relation(s) with competing values" in out
    # A's support is summed across the two relation spellings (1 + 1 = 2 sources);
    # B keeps its single source. Reported under the min representative spelling.
    assert f"P / {_REP}: A (2 src); B (1 src)" in out


def test_corroboration_count_merge_is_order_independent(tmp_path: Path):
    a = _corroboration(_write_kb(tmp_path / "fwd", _MIXED_ROWS))
    b = _corroboration(_write_kb(tmp_path / "rev", list(reversed(_MIXED_ROWS))))
    # The competing section (everything after the per-fact listing) must be
    # identical regardless of candidate row order.
    marker = "single-valued relation(s) with competing values"
    assert a[a.index(marker):] == b[b.index(marker):]
