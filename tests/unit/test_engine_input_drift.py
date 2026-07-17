# SPDX-License-Identifier: Apache-2.0
"""Catch a truncated / hand-edited accepted.dl that disagrees with candidates.csv (#328).

engine_relation_gap (#308) compares two readers of the SAME file (accepted.dl), so it
only sees the engine emptying under a consistent disk — never the edge that actually
drifts, candidates.csv → accepted.dl. engine_input_drift is the reader for that edge: it
compares the deduped engine-input rows of candidates.csv (the exact collapse compile_facts
applies) against the deduped accepted.dl count, and errors on a mismatch so the exit gate
(#283) stops the pipeline rather than signing a confirmed fact's '0 rows' as a verified
negative.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from run_logic_check import engine_input_drift


def _candidates(n_confirmed, *, needs_review=0, duplicate=0):
    rows = [
        {"subject": f"S{i}", "relation": "uses", "object": f"O{i}", "status": "confirmed"}
        for i in range(n_confirmed)
    ]
    # Duplicate triples collapse to one engine atom (compile dedups), so they must not
    # inflate the expected count.
    rows += [dict(rows[0]) for _ in range(duplicate)]
    # needs_review rows are NOT engine input and must be excluded from the expected count.
    rows += [
        {"subject": f"R{i}", "relation": "uses", "object": f"O{i}", "status": "needs_review"}
        for i in range(needs_review)
    ]
    return rows


def _accepted(n):
    return [{"subject": f"S{i}", "relation": "uses", "object": f"O{i}"} for i in range(n)]


class TestEngineInputDriftHelper:
    """Pure function — no engine, runs everywhere."""

    def test_matching_counts_is_fine(self):
        assert engine_input_drift(_candidates(7), _accepted(7)) is None

    def test_truncated_accepted_is_an_error(self):
        msg = engine_input_drift(_candidates(7), _accepted(3))
        assert msg is not None
        assert "7 engine-input fact(s)" in msg
        assert "holds 3" in msg

    def test_accepted_with_more_than_candidates_is_an_error(self):
        # A hand-added accepted.dl row with no confirmed source also drifts.
        assert engine_input_drift(_candidates(3), _accepted(5)) is not None

    def test_dedup_aware_on_the_candidates_side(self):
        # Two duplicate confirmed rows collapse to one atom; accepted.dl holds that one.
        assert engine_input_drift(_candidates(4, duplicate=3), _accepted(4)) is None

    def test_needs_review_rows_are_not_counted(self):
        # 5 confirmed + 4 needs_review on the candidates side; accepted holds the 5.
        assert engine_input_drift(_candidates(5, needs_review=4), _accepted(5)) is None

    def test_empty_kb_does_not_fire(self):
        assert engine_input_drift([], []) is None


# --- Engine-backed integration ----------------------------------------------
try:
    import pyrewire  # noqa: F401

    _HAVE_ENGINE = True
except ImportError:  # pragma: no cover - depends on the install
    _HAVE_ENGINE = False


def _kb(tmp_path, n=7):
    kb = tmp_path / "kb"
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        capture_output=True, check=True,
    )
    (kb / "sources" / "a.md").write_text("a\n")
    lines = ["subject,relation,object,source,status,confidence,note"]
    lines += [f"S{i},uses,O{i},sources/a.md,confirmed,0.9," for i in range(n)]
    (kb / "facts" / "candidates.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    subprocess.run(
        [sys.executable, str(Path("tools") / "compile_facts.py")],
        capture_output=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
        check=True,
    )
    return kb


def _run_check(kb):
    return subprocess.run(
        [sys.executable, "-c",
         "import os, sys; sys.path.insert(0, os.getcwd()); "
         "sys.path.insert(0, os.path.join(os.getcwd(), 'tools'))\n"
         "import run_logic_check as rlc\nprint('RC', rlc.main())"],
        capture_output=True, text=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
    )


@pytest.mark.skipif(not _HAVE_ENGINE, reason="pyrewire not installed")
class TestTruncatedAcceptedTripsCheck:
    def test_truncated_accepted_makes_check_exit_nonzero(self, tmp_path):
        kb = _kb(tmp_path, n=7)
        accepted = kb / "facts" / "accepted.dl"
        # Truncate 7 → 3 at a line boundary (the state an interrupted plain write leaves):
        # keep the header comments + the first 3 relation rows, drop the rest.
        kept, rel = [], 0
        for line in accepted.read_text(encoding="utf-8").split("\n"):
            if line.startswith("relation("):
                if rel >= 3:
                    continue
                rel += 1
            kept.append(line)
        accepted.write_text("\n".join(kept), encoding="utf-8")

        out = _run_check(kb)
        assert "RC 1" in out.stdout, out.stdout + out.stderr
        report = (kb / "facts" / "logic_report.txt").read_text(encoding="utf-8")
        assert "engine input drift" in report
        assert "7 engine-input fact(s)" in report and "holds 3" in report

    def test_healthy_kb_does_not_trip_drift(self, tmp_path):
        kb = _kb(tmp_path, n=7)
        out = _run_check(kb)
        assert "RC None" in out.stdout, out.stdout + out.stderr
        assert "engine input drift" not in (kb / "facts" / "logic_report.txt").read_text(encoding="utf-8")
