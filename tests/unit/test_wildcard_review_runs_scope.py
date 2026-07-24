# SPDX-License-Identifier: Apache-2.0
"""accept/reject must not touch runs rows the CSV gate reported as skipped (#477).

A KB predating #233 holds the human decision in candidates.csv while runs/*.json
still says `candidate`. A `-` wildcard used to reach those rows anyway: the gate
printed "non-pending skipped", left candidates.csv alone, and then flipped the run
row -- so the next merge, which rebuilds candidates.csv FROM runs/*.json, retired a
confirmed fact nothing had asked to retire.
"""
from __future__ import annotations

import argparse
import json

from factlog import cli

HEADER = "subject,relation,object,source,status,confidence,note\n"


def _kb(tmp_path, csv_rows, run_rows):
    for d in ("facts", "runs", "sources", "pages", "decisions", "policy"):
        (tmp_path / d).mkdir()
    (tmp_path / "facts" / "candidates.csv").write_text(
        HEADER + "".join(r + "\n" for r in csv_rows), encoding="utf-8"
    )
    (tmp_path / "runs" / "r1.json").write_text(
        json.dumps(run_rows, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "sources" / "note.md").write_text("메모\n", encoding="utf-8")
    return tmp_path


def _run_row(subject, obj, status):
    return {
        "subject": subject,
        "relation": "R",
        "object": obj,
        "source": "sources/note.md",
        "status": status,
        "confidence": "0.9",
        "note": "",
    }


def _invoke(tmp_path, terms, new_status, verb):
    args = argparse.Namespace(terms=terms, target=str(tmp_path), dry_run=False)
    return cli._apply_review_status(args, new_status, verb)


def _statuses(tmp_path):
    rows = json.loads((tmp_path / "runs" / "r1.json").read_text(encoding="utf-8"))
    return {r["subject"]: r["status"] for r in rows}


class TestWildcardDoesNotTouchSkippedRuns:
    def test_reject_leaves_drifted_confirmed_run_row_alone(self, tmp_path):
        """A confirmed csv row whose run row still says candidate must survive."""
        kb = _kb(
            tmp_path,
            [
                "A,R,X,sources/note.md,confirmed,0.90,",
                "B,R,Y,sources/note.md,candidate,0.90,",
            ],
            [_run_row("A", "X", "candidate"), _run_row("B", "Y", "candidate")],
        )
        _invoke(kb, ["-", "R", "-"], "superseded", "reject")

        # B was the only pending row, so only B may move -- in BOTH stores.
        assert _statuses(kb) == {"A": "candidate", "B": "superseded"}

    def test_accept_leaves_drifted_run_row_alone(self, tmp_path):
        """Same scoping in the accept direction."""
        kb = _kb(
            tmp_path,
            [
                "A,R,X,sources/note.md,accepted,0.90,",
                "B,R,Y,sources/note.md,needs_review,0.90,",
            ],
            [_run_row("A", "X", "candidate"), _run_row("B", "Y", "needs_review")],
        )
        _invoke(kb, ["-", "R", "-"], "accepted", "accept")

        assert _statuses(kb) == {"A": "candidate", "B": "accepted"}

    def test_reported_run_count_matches_rows_actually_decided(self, tmp_path, capsys):
        """The printed runs count must not exceed the decisions made."""
        kb = _kb(
            tmp_path,
            [
                "A,R,X,sources/note.md,confirmed,0.90,",
                "B,R,Y,sources/note.md,candidate,0.90,",
            ],
            [_run_row("A", "X", "candidate"), _run_row("B", "Y", "candidate")],
        )
        _invoke(kb, ["-", "R", "-"], "superseded", "reject")

        out = capsys.readouterr().out
        assert "1 candidate row(s) → superseded, 1 runs/*.json row(s) updated" in out


class TestDurabilityStillHolds:
    """#233 must keep working: the decision reaches runs/*.json."""

    def test_exact_triple_reject_writes_through(self, tmp_path):
        kb = _kb(
            tmp_path,
            ["B,R,Y,sources/note.md,candidate,0.90,"],
            [_run_row("B", "Y", "candidate")],
        )
        _invoke(kb, ["B", "R", "Y"], "superseded", "reject")
        assert _statuses(kb) == {"B": "superseded"}

    def test_blank_status_run_row_is_still_flipped(self, tmp_path):
        """merge coerces a blank status to pending, so the decision must reach it."""
        kb = _kb(
            tmp_path,
            ["B,R,Y,sources/note.md,candidate,0.90,"],
            [_run_row("B", "Y", "")],
        )
        _invoke(kb, ["B", "R", "Y"], "accepted", "accept")
        assert _statuses(kb) == {"B": "accepted"}
