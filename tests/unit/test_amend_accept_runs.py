# SPDX-License-Identifier: Apache-2.0
"""`amend --accept` must write the promotion into runs/*.json, not candidates.csv only (#565).

merge REBUILDS candidates.csv from runs/*.json, so a decision that never reaches the
run row is not durable: delete candidates.csv, re-merge, and the fact is a candidate
again -- the #233 silent downgrade, reopened for the one command that already had the
runs pass wired up. `accept` and `amend --accept` disagreed about the very same row.

The run rows are read back directly here. Asserting on candidates.csv (even after a
re-merge that keeps the file) passes with the defect still in place, because merge
restores the run status FROM the CSV whenever the CSV is there.
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
    if run_rows is not None:
        (tmp_path / "runs" / "r1.json").write_text(
            json.dumps(run_rows, ensure_ascii=False), encoding="utf-8"
        )
    (tmp_path / "sources" / "note.md").write_text("메모\n", encoding="utf-8")
    return tmp_path


def _run_row(obj, status, note=""):
    row = {
        "subject": "A",
        "relation": "R",
        "object": obj,
        "source": "sources/note.md",
        "confidence": "0.9",
        "note": note,
    }
    # A run item with NO status key at all is a real shape: merge normalizes it to
    # needs_review, so it must be treated as pending here too.
    if status is not None:
        row["status"] = status
    return row


def _amend(tmp_path, triple=("A", "R", "X"), **overrides):
    fields = dict(
        subject=triple[0],
        relation=triple[1],
        object=triple[2],
        set_subject=None,
        set_relation=None,
        set_object=None,
        set_note=None,
        accept=False,
        dry_run=False,
        target=str(tmp_path),
    )
    fields.update(overrides)
    return cli.cmd_amend(argparse.Namespace(**fields))


def _runs(tmp_path):
    return json.loads((tmp_path / "runs" / "r1.json").read_text(encoding="utf-8"))


def _statuses(tmp_path):
    return {r["object"]: r.get("status") for r in _runs(tmp_path)}


class TestAcceptReachesTheRunRow:
    def test_accept_only_promotes_the_pending_run_row(self, tmp_path):
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,candidate,0.90,"],
            [_run_row("X", "candidate")],
        )
        assert _amend(kb, accept=True) == 0

        assert _statuses(kb) == {"X": "accepted"}

    def test_run_item_with_no_status_key_is_treated_as_pending(self, tmp_path):
        """merge coerces a missing/blank status to needs_review, so amend must too.

        Left alone, the run row rebuilds candidates.csv as needs_review and the
        promotion is gone -- the same downgrade, in a row an extractor mis-stamped.
        """
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,needs_review,0.90,"],
            [_run_row("X", None)],
        )
        assert _amend(kb, accept=True) == 0

        assert _statuses(kb) == {"X": "accepted"}

    def test_confirmed_run_row_is_left_alone(self, tmp_path):
        """A human `confirmed` in runs/*.json is not overwritten by --accept.

        The CSV gate above DOES write `accepted` over a confirmed CSV row (the
        transition docs/reference/review.md pins), and a plain re-merge makes that
        stick. So this run row is the last surviving copy of the confirmed ruling --
        recoverable only by rebuilding from runs/*.json alone. Promoting it here would
        burn the only backup, which is why the runs pass is pending-only.
        """
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,confirmed,0.90,"],
            [_run_row("X", "confirmed")],
        )
        assert _amend(kb, accept=True) == 0

        assert _statuses(kb) == {"X": "confirmed"}


class TestAcceptWithATripleChange:
    def test_corrected_row_is_accepted_and_the_old_one_retired(self, tmp_path):
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,candidate,0.90,"],
            [_run_row("X", "candidate")],
        )
        assert _amend(kb, set_object="Y", accept=True) == 0

        # The old triple keeps run backing as a tombstone (#220 defect 1) and the
        # corrected triple arrives as its own accepted item.
        assert _statuses(kb) == {"X": "superseded", "Y": "accepted"}

    def test_a_confirmed_original_yields_a_confirmed_correction(self, tmp_path):
        """The promotion is judged on the status the item had BEFORE the tombstone.

        Two rules meet here. The old item may only become `superseded` -- promoting a
        tombstone revives a retired value as engine input (#220 defect 2). And the
        pending-only rule says a `confirmed` row is not something this decision
        decided. So the correction inherits `confirmed`: NOT `accepted`.

        This is pinned deliberately. Forcing the correction to `accepted` to make the
        flag look obeyed would downgrade a human ruling to an engine promotion, and
        `confirmed` would be gone from both stores at once.
        """
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,confirmed,0.90,"],
            [_run_row("X", "confirmed")],
        )
        assert _amend(kb, set_object="Y", accept=True) == 0

        assert _statuses(kb) == {"X": "superseded", "Y": "confirmed"}


class TestValueOnlyEditsDoNotDecide:
    def test_set_note_alone_leaves_the_run_status_untouched(self, tmp_path):
        """amend deals in values; without --accept it decides nothing."""
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,candidate,0.90,"],
            [_run_row("X", "candidate")],
        )
        assert _amend(kb, set_note="checked") == 0

        assert _statuses(kb) == {"X": "candidate"}
        assert _runs(kb)[0]["note"] == "checked"
