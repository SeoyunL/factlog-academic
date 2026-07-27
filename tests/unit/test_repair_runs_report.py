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

from conftest import repair_runs_section as _section

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
    fields = dict(terms=list(terms), target=str(tmp_path), apply=False, all=False)
    fields.update(overrides)
    return cli.cmd_repair_runs(argparse.Namespace(**fields))


def _runs(tmp_path, name="r1.json"):
    return json.loads((tmp_path / "runs" / name).read_text(encoding="utf-8"))


def _bytes(tmp_path):
    return {p.name: p.read_bytes() for p in sorted((tmp_path / "runs").glob("*.json"))}


class TestRepairableClasses:
    def test_a_decision_missing_from_the_run_row_is_repairable(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,accepted,0.90,", "Q,rel,Z,sources/note.md,candidate,0.90,"],
            {"r1.json": [_run_row(status="candidate"), _unrelated_run_row()]},
        )
        assert _repair(kb, "A", "R", "X") == 3

        out = capsys.readouterr().out
        body = _section(out, "decided in candidates.csv, still pending in the run row")
        assert "A / R / X  ← sources/note.md" in body
        assert "r1.json  [candidate → accepted]" in body
        assert "1 run row(s) repairable in 1 file(s), 0 fact(s) left for a human" in out

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

        body = _section(capsys.readouterr().out, "decided in candidates.csv, still pending in the run row")
        assert "r1.json  [needs_review → accepted]" in body

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

        body = _section(capsys.readouterr().out, "retired in candidates.csv, still engine input in the run row")
        assert "A / R / X  ← sources/note.md" in body
        assert "r1.json  [accepted → superseded]" in body


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
        body = _section(out, "several candidates.csv rows share one fact — NOT repaired")
        assert "A / R / X  ← sources/note.md" in body
        assert "candidates.csv: candidate, superseded" in body
        assert "0 run row(s) repairable in 0 file(s), 1 fact(s) left for a human" in out
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
        assert "A / R / X  ← sources/note.md" in _section(
            out, "several candidates.csv rows share one fact — NOT repaired"
        )
        assert "0 run row(s) repairable in 0 file(s), 1 fact(s) left for a human" in out


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
        body = _section(out, "unrecognised status in candidates.csv — NOT repaired")
        assert "A / R / X  ← sources/note.md" in body
        assert "candidates.csv: aceptd" in body
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
        body = _section(out, "both stores decided, and they disagree — NOT repaired")
        assert "A / R / X  ← sources/note.md" in body
        assert "candidates.csv: accepted" in body
        assert "r1.json: confirmed" in body
        assert "0 run row(s) repairable in 0 file(s), 1 fact(s) left for a human" in out

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
        body = _section(out, "pending in candidates.csv, decided in the run row — NOT repaired")
        assert "A / R / X  ← sources/note.md" in body
        assert "r1.json: accepted" in body
        assert "0 run row(s) repairable" in out

    def test_two_different_pending_statuses_are_reported(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,candidate,0.90,"],
            {"r1.json": [_run_row(status="needs_review"), _unrelated_run_row()]},
        )
        assert _repair(kb, "A", "R", "X") == 3

        body = _section(
            capsys.readouterr().out,
            "both stores pending, with different pending statuses — NOT repaired",
        )
        assert "A / R / X  ← sources/note.md" in body
        assert "candidates.csv: candidate" in body
        assert "r1.json: needs_review" in body

    def test_a_run_row_with_no_csv_row_is_reported(self, tmp_path, capsys):
        kb = _kb(tmp_path, [], {"r1.json": [_run_row(status="candidate")]})
        assert _repair(kb, "A", "R", "X") == 3

        body = _section(capsys.readouterr().out, "run row with no candidates.csv row — NOT repaired")
        assert "A / R / X  ← sources/note.md" in body
        assert "candidates.csv: (no row)" in body

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

        body = _section(capsys.readouterr().out, "candidates.csv row with no run backing — NOT repaired")
        assert "A / R / X  ← sources/note.md" in body
        assert "runs/*.json: (no row)" in body
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
        body = _section(out, "decided in candidates.csv, still pending in the run row")
        assert "A / R / X  ← sources/note.md — PARTIAL" in body
        assert "aaa.json  [candidate → accepted]" in body
        assert "glob order, not by status" in body
        # the other copy is filed where a human will see it, marked as the same fact
        left = _section(out, "both stores decided, and they disagree — NOT repaired")
        assert "A / R / X  ← sources/note.md — PARTIAL" in left
        assert "zzz.json: confirmed" in left
        assert "1 run row(s) repairable in 1 file(s), 1 fact(s) left for a human" in out


class TestTheSummaryCounterUnit:
    """`K fact(s) left for a human` must count FACTS, in every class, always.

    The summary carries two units in one sentence -- "N run row(s) repaired" (rows, because
    that is what was written) and "K fact(s) left" (facts, because that is the unit of the
    remaining work). Each names its own unit, but only if K really is facts.

    It was not. Measured on the shipped code: one fact with four run rows reported
    `1 left` under `ambiguous-csv` and `3 left` under `both-decided`, because entries were
    per-fact in some classes and per-row in others, while the sentence leads with
    "run row(s)" and invites the reader to carry that unit across.

    A per-fixture string assertion cannot catch that: each number was right for its own
    fixture. Only a KB where the three candidate answers DIFFER can. This is that KB.
    """

    def _mixed_kb(self, tmp_path):
        """Three drifted facts, chosen so facts / entries / run rows are three numbers.

        * A/R/X -- ambiguous candidates.csv, 3 run rows  -> 1 fact, 1 entry, 3 rows
        * B/R/Y -- both stores decided, 2 run rows        -> 1 fact, 1 entry, 2 rows
        * C/R/Z -- pending CSV whose run copies split across TWO classes at once
                   (one decided, one pending-but-different) -> 1 fact, 2 entries, 2 rows
        * Q/rel/W -- agrees in both stores; must not be counted at all

        facts = 3, entries = 4, run rows = 7. Counting entries (the shape this class was
        added for) or rows (the shape before the grouping fix) both miss.
        """
        return _kb(
            tmp_path,
            [
                "A,R,X,sources/note.md,candidate,0.90,",
                "A,R,X,sources/note.md,superseded,0.90,",
                "B,R,Y,sources/note.md,accepted,0.90,",
                "C,R,Z,sources/note.md,candidate,0.90,",
                "Q,rel,W,sources/note.md,candidate,0.90,",
            ],
            {
                "aaa.json": [
                    _run_row(obj="X", status="candidate"),
                    _run_row(obj="X", status="superseded"),
                    _run_row(subject="B", obj="Y", status="confirmed"),
                    _run_row(subject="C", obj="Z", status="accepted"),
                    _run_row(subject="Q", relation="rel", obj="W", status="candidate"),
                ],
                "zzz.json": [
                    _run_row(obj="X", status="accepted"),
                    _run_row(subject="B", obj="Y", status="confirmed"),
                    _run_row(subject="C", obj="Z", status="needs_review"),
                ],
            },
        )

    def test_the_count_is_facts_not_entries_and_not_run_rows(self, tmp_path, capsys):
        kb = self._mixed_kb(tmp_path)
        assert _repair(kb) == 3

        out = capsys.readouterr().out
        assert "0 run row(s) repairable in 0 file(s), 3 fact(s) left for a human" in out
        # ...and the two numbers it must not have collapsed into, stated explicitly so a
        # future reader can see the fixture still separates them.
        assert "4 fact(s) left for a human" not in out   # entries
        assert "7 fact(s) left for a human" not in out   # run rows

    def test_every_run_row_is_still_listed_under_its_fact(self, tmp_path, capsys):
        """Counting facts must not cost the per-file detail D3 requires.

        merge picks between run files by glob order, not by status, so a human settling one
        of these facts has to see WHICH file holds WHICH status. Grouping the rows under one
        label is what makes the count honest; dropping them would trade one defect for
        another.
        """
        kb = self._mixed_kb(tmp_path)
        assert _repair(kb) == 3

        out = capsys.readouterr().out
        reported = "".join(
            _section(out, heading) for _n, heading in cli._REPAIR_RUNS_REPORTED
        )
        assert len([ln for ln in reported.splitlines() if ".json:" in ln]) == 7

        ambiguous = _section(out, "several candidates.csv rows share one fact — NOT repaired")
        assert ambiguous.count("A / R / X  ← sources/note.md") == 1
        assert "aaa.json: candidate" in ambiguous
        assert "aaa.json: superseded" in ambiguous
        assert "zzz.json: accepted" in ambiguous

    def test_one_fact_split_across_two_classes_is_still_one_fact(self, tmp_path, capsys):
        """C/R/Z is the case that separates "distinct facts" from "entries".

        Its pending CSV row has one decided run copy and one differently-pending run copy,
        so it is filed under two headings at once -- correctly, they are different problems
        -- but it is still ONE fact for a human to settle.
        """
        kb = self._mixed_kb(tmp_path)
        assert _repair(kb) == 3

        out = capsys.readouterr().out
        decided = _section(out, "pending in candidates.csv, decided in the run row — NOT repaired")
        pending = _section(out, "both stores pending, with different pending statuses — NOT repaired")
        assert "C / R / Z  ← sources/note.md" in decided
        assert "aaa.json: accepted" in decided
        assert "C / R / Z  ← sources/note.md" in pending
        assert "zzz.json: needs_review" in pending

    def test_a_label_is_not_repeated_once_per_run_row(self, tmp_path, capsys):
        """B/R/Y has two run rows in one class; the fact must be named once, not twice.

        Per-row entries printed the same fact label once per row, which is what made the
        count read as rows in the first place.
        """
        kb = self._mixed_kb(tmp_path)
        assert _repair(kb) == 3

        body = _section(
            capsys.readouterr().out, "both stores decided, and they disagree — NOT repaired"
        )
        assert body.count("B / R / Y  ← sources/note.md") == 1
        assert "aaa.json: confirmed" in body
        assert "zzz.json: confirmed" in body


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
        assert "0 run row(s) repairable in 0 file(s), 0 fact(s) left for a human" in out
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
        assert "0 run row(s) repairable in 0 file(s), 0 fact(s) left for a human" in out

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


class TestABlankCandidatesCsvStatus:
    """A blank/whitespace status is what the engine-status whitelist UNIQUELY defends.

    An unrecognised status (`aceptd`) is caught twice over -- by the whitelist AND by the
    unrecognised-status class -- so a mutation to either alone leaves the suite green. A
    BLANK status slips past the unrecognised class on purpose (`if st and ...`: an empty
    field is a normal shape merge coerces, not a typo), so the whitelist is the only thing
    between it and the run rows.

    Measured with the whitelist flipped to `not in REVIEW_STATUSES` at both gates: a blank
    CSV status is read as a DECISION and written into the run row -- `r1.json
    [candidate → ]`, an empty string as a status. merge coerces that to needs_review on the
    next rebuild, so the run row's real `candidate` is destroyed by the command asked to
    protect it.
    """

    def test_a_blank_status_is_pending_not_a_decision(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,,0.90,", "Q,rel,Z,sources/note.md,candidate,0.90,"],
            {"r1.json": [_run_row(status="candidate"), _unrelated_run_row()]},
        )
        assert _repair(kb, "A", "R", "X") == 3

        out = capsys.readouterr().out
        body = _section(out, "both stores pending, with different pending statuses — NOT repaired")
        assert "A / R / X  ← sources/note.md" in body
        # the report describes the FILE, so it shows the blank -- not merge's needs_review
        # fallback, which would send a user grepping candidates.csv for a word it lacks.
        assert "candidates.csv: (blank)" in body
        assert "r1.json: candidate" in body
        assert "0 run row(s) repairable in 0 file(s), 1 fact(s) left for a human" in out
        # ...and it is never offered as something to write
        assert "→ ]" not in out
        assert _section(out, "decided in candidates.csv, still pending in the run row").strip() == "(none)"

    def test_a_whitespace_only_status_is_the_same_case(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            ['A,R,X,sources/note.md,"   ",0.90,'],
            {"r1.json": [_run_row(status="candidate"), _unrelated_run_row()]},
        )
        assert _repair(kb, "A", "R", "X") == 3

        body = _section(
            capsys.readouterr().out,
            "both stores pending, with different pending statuses — NOT repaired",
        )
        assert "candidates.csv: (blank)" in body

    def test_a_blank_status_agreeing_with_the_run_row_is_clean(self, tmp_path, capsys):
        """The counter-example: blank means needs_review to merge, so a `needs_review` run
        row AGREES and there is no drift at all. Without this, a rule that flagged every
        blank status as drift would look correct."""
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,,0.90,"],
            {"r1.json": [_run_row(status="needs_review"), _unrelated_run_row()]},
        )
        assert _repair(kb, "A", "R", "X") == 0

        assert "0 run row(s) repairable in 0 file(s), 0 fact(s) left for a human" in (
            capsys.readouterr().out
        )


class TestAnAbsenceIsProvisionalWhileAFileCannotBeRead:
    """`csv-only` and `run-only` are the only classes whose verdict is an ABSENCE.

    Every other class names rows it actually saw, so an unreadable file can only add to it.
    These two it can falsify: the row said to be missing may be sitting in the file that
    would not open -- which is #566's own scenario, where the sole backing was unreadable.
    """

    def test_a_csv_only_verdict_is_marked_provisional(self, tmp_path, capsys):
        kb = _kb(tmp_path, ["A,R,X,sources/note.md,accepted,0.90,"], {})
        (kb / "runs" / "bin.json").write_bytes(b"\xff\xfe\x00binary")
        assert _repair(kb, "A", "R", "X") == 1

        body = _section(capsys.readouterr().out, "candidates.csv row with no run backing — NOT repaired")
        assert "PROVISIONAL" in body
        assert "the backing this row appears to lack may be inside one of them" in body
        # and the flat claim is gone: it is "none I could read", not "none".
        assert "runs/*.json: (no readable row)" in body
        assert "runs/*.json: (no row)\n" not in body

    def test_a_run_only_verdict_is_marked_provisional_too(self, tmp_path, capsys):
        """Its `no candidates.csv row` half IS sound -- the CSV is read whole or not at all
        -- so that line stays flat. What an unreadable file hides here is the fact's OTHER
        run rows, and the note says exactly that rather than hedging the wrong half."""
        kb = _kb(tmp_path, [], {"good.json": [_run_row(status="candidate")]})
        (kb / "runs" / "bin.json").write_bytes(b"\xff\xfe\x00binary")
        assert _repair(kb, "A", "R", "X") == 1

        body = _section(capsys.readouterr().out, "run row with no candidates.csv row — NOT repaired")
        assert "PROVISIONAL" in body
        assert "this fact's other run rows may be inside one of them" in body
        assert "candidates.csv: (no row)" in body

    def test_a_readable_kb_states_the_absence_flatly(self, tmp_path, capsys):
        """The counter-example. With every file readable the hedge must NOT appear, or it
        becomes noise the reader learns to skip -- and then it says nothing when it matters.
        """
        kb = _kb(
            tmp_path,
            ["A,R,X,sources/note.md,accepted,0.90,"],
            {"r1.json": [_unrelated_run_row()]},
        )
        assert _repair(kb, "A", "R", "X") == 3

        body = _section(capsys.readouterr().out, "candidates.csv row with no run backing — NOT repaired")
        assert "PROVISIONAL" not in body
        assert "runs/*.json: (no row)" in body


class TestUsage:
    def test_more_than_three_terms_is_a_usage_error(self, tmp_path, capsys):
        kb = _kb(tmp_path, [], {})
        assert _repair(kb, "A", "R", "X", "extra") == 2

        assert "too many terms" in capsys.readouterr().err

    def test_a_non_kb_target_is_refused(self, tmp_path, capsys):
        assert _repair(tmp_path) == 1

        assert "is not a factlog KB" in capsys.readouterr().err
