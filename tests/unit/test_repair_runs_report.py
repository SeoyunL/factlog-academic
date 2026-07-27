# SPDX-License-Identifier: Apache-2.0
"""`repair-runs` must REPORT every class of candidates.csv/runs/*.json status drift (#566).

accept/reject/amend record only the decision they just made; reconciling two stores that
already drifted apart is a separate command's job, which their docstrings name and which
did not exist. This file pins the reading half: what the command classifies as repairable,
what it refuses to touch, and that reporting writes nothing.

Assertions read the run rows back as JSON. Asserting on candidates.csv would pass with the
command doing nothing at all -- the CSV is the side that is already right.
"""
from __future__ import annotations

import argparse
import json

from factlog import cli

HEADER = "subject,relation,object,source,status,confidence,note\n"


def _kb(tmp_path, csv_rows, run_files):
    """run_files: {filename: [item, ...]}; csv_rows: raw CSV lines without the newline."""
    for d in ("facts", "runs", "sources", "pages", "decisions", "policy"):
        (tmp_path / d).mkdir()
    (tmp_path / "facts" / "candidates.csv").write_text(
        HEADER + "".join(r + "\n" for r in csv_rows), encoding="utf-8"
    )
    for name, items in run_files.items():
        (tmp_path / "runs" / name).write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "sources" / "note.md").write_text("메모\n", encoding="utf-8")
    return tmp_path


def _run_row(subject="A", relation="R", obj="X", status="candidate", source="sources/note.md"):
    row = {
        "subject": subject,
        "relation": relation,
        "object": obj,
        "source": source,
        "confidence": "0.9",
        "note": "",
    }
    # A run item with NO status key at all is a real shape: merge coerces it to
    # needs_review, so this command has to read it as pending too.
    if status is not None:
        row["status"] = status
    return row


def _unrelated_run_row():
    """A live run item for a DIFFERENT fact, sharing the run file with the target.

    The normal shape of a real KB -- one run file holds every fact an extraction produced
    -- and the one dimension that tells a per-item decision apart from a per-file one. A
    mutation that repaired every item in a file it touched stays green without it (#565).
    """
    return _run_row(subject="Q", relation="rel", obj="Z", status="candidate")


def _repair(tmp_path, *terms, **overrides):
    fields = dict(terms=list(terms), target=str(tmp_path))
    fields.update(overrides)
    return cli.cmd_repair_runs(argparse.Namespace(**fields))


def _runs(tmp_path, name="r1.json"):
    return json.loads((tmp_path / "runs" / name).read_text(encoding="utf-8"))


def _bytes(tmp_path):
    return {p.name: p.read_bytes() for p in sorted((tmp_path / "runs").iterdir())}


class TestRepairableClasses:
    def test_a_decision_missing_from_the_run_row_is_repairable(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,accepted,0.90,", "Q,rel,Z,sources/note.md,candidate,0.90,"],
            {"r1.json": [_run_row(status="candidate"), _unrelated_run_row()]},
        )
        assert _repair(kb, "A", "R", "X") == 3

        out = capsys.readouterr().out
        assert "decided in candidates.csv, still pending in the run row:" in out
        assert "r1.json  [candidate → accepted]" in out
        assert "1 run row(s) repairable in 1 file(s), 0 left for a human" in out

    def test_a_blank_status_run_row_counts_as_pending(self, tmp_path, capsys):
        """merge coerces a missing/blank status to needs_review, so this must too.

        Left alone, that row rebuilds candidates.csv as needs_review and the decision is
        gone -- the same silent downgrade, in a row an extractor mis-stamped.
        """
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,accepted,0.90,"],
            {"r1.json": [_run_row(status=None), _unrelated_run_row()]},
        )
        assert _repair(kb, "A", "R", "X") == 3

        assert "r1.json  [needs_review → accepted]" in capsys.readouterr().out

    def test_a_retirement_the_run_row_never_heard_about_is_repairable(self, tmp_path, capsys):
        """CSV `superseded` over run `accepted` -- the eject-drift shape.

        Repairing it LOWERS the run row. That is not an arbitrary ranking: merge's own
        existing_superseded_keys pass puts a candidates.csv `superseded` above an engine
        status when it rebuilds, so writing it here agrees with the rebuild merge performs.
        An invariant of "never lower a run row" would leave this drift unfixable forever.
        """
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,superseded,0.90,"],
            {"r1.json": [_run_row(status="accepted"), _unrelated_run_row()]},
        )
        assert _repair(kb, "A", "R", "X") == 3

        out = capsys.readouterr().out
        assert "retired in candidates.csv, still engine input in the run row:" in out
        assert "r1.json  [accepted → superseded]" in out


class TestTheDuplicateFactKeyGuard:
    """A round-trip amend leaves TWO candidates.csv rows on ONE fact_key (measured).

    `--set-object C` then `--set-object B` gives a live `candidate` row and the first
    amend's `superseded` tombstone, both keyed identically. An implementation that picks
    "the" CSV status writes the tombstone over the LIVE run row, and the fact that was
    alive disappears from the next from-scratch rebuild -- the repair command as a
    destroyer. The guard must exist before anything can be written.
    """

    def test_a_decision_beside_a_pending_row_is_reported_not_repairable(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            [
                "A,R,X,sources/note.md,candidate,0.90,",
                "A,R,X,sources/note.md,superseded,0.90,",
            ],
            {"r1.json": [_run_row(status="candidate"), _unrelated_run_row()]},
        )
        assert _repair(kb, "A", "R", "X") == 3

        out = capsys.readouterr().out
        assert "several candidates.csv rows share one fact — NOT repaired:" in out
        assert "candidates.csv: candidate, superseded" in out
        assert "0 run row(s) repairable in 0 file(s), 1 left for a human" in out
        # and specifically NOT offered as a repair, in either direction
        assert "→ superseded]" not in out
        assert "→ candidate]" not in out

    def test_two_decisions_on_one_fact_are_reported_not_repairable(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            [
                "A,R,X,sources/note.md,accepted,0.90,",
                "A,R,X,sources/note.md,superseded,0.90,",
            ],
            {"r1.json": [_run_row(status="candidate"), _unrelated_run_row()]},
        )
        assert _repair(kb, "A", "R", "X") == 3

        out = capsys.readouterr().out
        assert "several candidates.csv rows share one fact — NOT repaired:" in out
        assert "0 run row(s) repairable in 0 file(s), 1 left for a human" in out


class TestClassesItRefusesToTouch:
    def test_an_unrecognised_csv_status_is_never_repairable(self, tmp_path, capsys):
        """The status here comes from a file a human edits, unlike accept/reject's constant.

        A `not in REVIEW_STATUSES` gate lets a typo through; merge's clean_row then demotes
        it to needs_review on the next rebuild, and the decision is destroyed by the command
        asked to save it. Hence a whitelist.
        """
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,aceptd,0.90,"],
            {"r1.json": [_run_row(status="candidate"), _unrelated_run_row()]},
        )
        assert _repair(kb, "A", "R", "X") == 3

        out = capsys.readouterr().out
        assert "unrecognised status in candidates.csv — NOT repaired:" in out
        assert "candidates.csv: aceptd" in out
        assert "→ aceptd]" not in out
        assert "0 run row(s) repairable" in out

    def test_two_disagreeing_decisions_are_reported_only(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,accepted,0.90,"],
            {"r1.json": [_run_row(status="confirmed"), _unrelated_run_row()]},
        )
        assert _repair(kb, "A", "R", "X") == 3

        out = capsys.readouterr().out
        assert "both stores decided, and they disagree — NOT repaired:" in out
        assert "0 run row(s) repairable in 0 file(s), 1 left for a human" in out

    def test_a_decision_only_the_run_row_holds_is_reported_only(self, tmp_path, capsys):
        """The run row is the last surviving copy; writing the CSV's pending status over it
        would destroy it, and writing the run status into candidates.csv is not this
        command's job -- it writes runs/*.json only."""
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,candidate,0.90,"],
            {"r1.json": [_run_row(status="accepted"), _unrelated_run_row()]},
        )
        assert _repair(kb, "A", "R", "X") == 3

        out = capsys.readouterr().out
        assert "pending in candidates.csv, decided in the run row — NOT repaired:" in out
        assert "0 run row(s) repairable" in out

    def test_two_different_pending_statuses_are_reported(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,candidate,0.90,"],
            {"r1.json": [_run_row(status="needs_review"), _unrelated_run_row()]},
        )
        assert _repair(kb, "A", "R", "X") == 3

        assert "both stores pending, with different pending statuses — NOT repaired:" in (
            capsys.readouterr().out
        )

    def test_a_run_row_with_no_csv_row_is_reported(self, tmp_path, capsys):
        kb = _kb(tmp_path, [], {"r1.json": [_run_row(status="candidate")]})
        assert _repair(kb, "A", "R", "X") == 3

        out = capsys.readouterr().out
        assert "run row with no candidates.csv row — NOT repaired:" in out
        assert "candidates.csv: (no row)" in out

    def test_a_csv_row_with_no_run_backing_is_reported_not_created(self, tmp_path, capsys):
        """Creating a run item would mean inventing a docspan and a run_id no extraction
        produced -- forged provenance. This class is named in the report and left alone;
        it is also the shape an `amend` correction and the #218 ratchet protect."""
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,accepted,0.90,"],
            {"r1.json": [_unrelated_run_row()]},
        )
        assert _repair(kb, "A", "R", "X") == 3

        out = capsys.readouterr().out
        assert "candidates.csv row with no run backing — NOT repaired:" in out
        assert "runs/*.json: (no row)" in out
        assert _runs(kb) == [_unrelated_run_row()]


class TestPartialRepairIsNamed:
    def test_a_fact_whose_run_rows_disagree_is_marked_partial(self, tmp_path, capsys):
        """merge's dedup winner does not look at status.

        It compares the `source` string and, when those tie, keeps whichever run file the
        glob loaded first. So repairing only the pending copy of a fact whose other copy is
        already decided leaves the next from-scratch rebuild a lottery -- and a bare
        "N row(s) repaired" line cannot say so.
        """
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,accepted,0.90,"],
            {
                "aaa.json": [_run_row(status="candidate"), _unrelated_run_row()],
                "zzz.json": [_run_row(status="confirmed")],
            },
        )
        assert _repair(kb, "A", "R", "X") == 3

        out = capsys.readouterr().out
        assert "A / R / X  ← sources/note.md — PARTIAL" in out
        assert "aaa.json  [candidate → accepted]" in out
        assert "glob order, not by status" in out
        assert "1 run row(s) repairable in 1 file(s), 1 left for a human" in out


class TestScopeAndCleanliness:
    def test_agreement_is_clean_and_exits_zero(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,accepted,0.90,", "Q,rel,Z,sources/note.md,candidate,0.90,"],
            {"r1.json": [_run_row(status="accepted"), _unrelated_run_row()]},
        )
        before = _bytes(kb)
        assert _repair(kb) == 0

        out = capsys.readouterr().out
        assert "0 run row(s) repairable in 0 file(s), 0 left for a human" in out
        assert _bytes(kb) == before

    def test_reporting_writes_nothing_even_when_drift_is_found(self, tmp_path):
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,accepted,0.90,"],
            {"r1.json": [_run_row(status="candidate"), _unrelated_run_row()]},
        )
        before = _bytes(kb)
        csv_before = (kb / "facts" / "candidates.csv").read_bytes()
        dl = kb / "facts" / "accepted.dl"
        assert _repair(kb, "A", "R", "X") == 3

        assert _bytes(kb) == before
        assert (kb / "facts" / "candidates.csv").read_bytes() == csv_before
        # and no recompile side effect: repair-runs does not change accepted.dl's input,
        # so touching it would only raise "why did my .dl change?".
        assert not dl.exists()

    def test_the_filter_scopes_the_report(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,accepted,0.90,", "Q,rel,Z,sources/note.md,accepted,0.90,"],
            {"r1.json": [_run_row(status="candidate"), _unrelated_run_row()]},
        )
        assert _repair(kb, "Q", "rel", "Z") == 3

        out = capsys.readouterr().out
        assert "Q / rel / Z" in out
        assert "A / R / X" not in out
        assert "1 run row(s) repairable" in out

    def test_a_sourceless_row_is_skipped_not_matched(self, tmp_path, capsys):
        """A fact_key with an empty source component matches every OTHER sourceless row.

        accept/reject carry this guard inside `_apply_review_status`, so repair-runs needs
        its own; without it a decision keyed on the empty source sprays across facts that
        merely share the defect.
        """
        kb = _kb(
            tmp_path,
            ["A,R,X,,accepted,0.90,"],
            {"r1.json": [_run_row(status="candidate", source=""), _unrelated_run_row()]},
        )
        assert _repair(kb, "A", "R", "X") == 0

        out = capsys.readouterr().out
        assert "skipped 1 candidates.csv row(s) with no source file" in out
        assert "skipped 1 runs/*.json row(s) with no source file" in out
        assert "0 run row(s) repairable in 0 file(s), 0 left for a human" in out

    def test_an_anchor_only_source_keys_to_the_same_empty_source(self, tmp_path, capsys):
        """fact_key cuts at '#', so a bare anchor is the empty-source case in disguise.

        Judged on the KEY's source component, not the raw field, exactly as
        `_apply_review_status` does -- one guard, both shapes.
        """
        kb = _kb(
            tmp_path,
            ["A,R,X,#sec1,accepted,0.90,"],
            {"r1.json": [_run_row(status="candidate", source="#sec1")]},
        )
        assert _repair(kb, "A", "R", "X") == 0

        assert "skipped 1 candidates.csv row(s) with no source file" in capsys.readouterr().out

    def test_every_class_gets_a_heading_even_when_empty(self, tmp_path, capsys):
        """A command whose job is "compare two stores" must not lie by silence.

        Printing only the fixable rows would make "nothing to do" and "nothing I am allowed
        to touch" the same output.
        """
        kb = _kb(tmp_path, [], {})
        assert _repair(kb) == 0

        out = capsys.readouterr().out
        for _name, heading in cli._REPAIR_RUNS_REPAIRABLE + cli._REPAIR_RUNS_REPORTED:
            assert f"{heading}:" in out
        assert out.count("  (none)") == len(cli._REPAIR_RUNS_REPAIRABLE) + len(cli._REPAIR_RUNS_REPORTED)


class TestUnreadableRunFiles:
    def test_an_unreadable_file_is_named_and_exits_one(self, tmp_path, capsys):
        """Named and survived, not fatal -- but the comparison is partial, so the exit code
        must differ from both "clean" (0) and "drift found" (3). A report nobody can trust
        is worse than none, and this is the very failure that created the drift (#563)."""
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,accepted,0.90,"],
            {"good.json": [_run_row(status="candidate")]},
        )
        (kb / "runs" / "bin.json").write_bytes(b"\xff\xfe\x00binary")
        assert _repair(kb, "A", "R", "X") == 1

        err = capsys.readouterr().err
        assert "could not read bin.json" in err
        assert "the comparison is INCOMPLETE" in err

    def test_invalid_json_gets_the_same_treatment_as_undecodable_bytes(self, tmp_path, capsys):
        kb = _kb(tmp_path, [], {"good.json": [_run_row(status="candidate")]})
        (kb / "runs" / "broken.json").write_text("not json{", encoding="utf-8")
        assert _repair(kb) == 1

        assert "could not read broken.json" in capsys.readouterr().err


class TestUsage:
    def test_more_than_three_terms_is_a_usage_error(self, tmp_path, capsys):
        kb = _kb(tmp_path, [], {})
        assert _repair(kb, "A", "R", "X", "extra") == 2

        assert "too many terms" in capsys.readouterr().err

    def test_a_non_kb_target_is_refused(self, tmp_path, capsys):
        assert _repair(tmp_path) == 1

        assert "is not a factlog KB" in capsys.readouterr().err
