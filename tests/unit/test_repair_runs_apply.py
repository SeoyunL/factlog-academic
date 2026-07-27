# SPDX-License-Identifier: Apache-2.0
"""`repair-runs --apply` must write the human decision into the run row (#566).

Every payoff assertion here deletes `facts/candidates.csv` and re-merges. Reading the
run row back proves the command WROTE something; only the rebuild proves merge ADOPTS
it, and merge picks between two run files by glob order rather than by status, so those
are different claims. Where a rebuild cannot separate the correct behaviour from the
defect (a fact whose copies live in two files -- the winner is a lottery either way),
the per-file rows are asserted directly and the reason is stated at the test.

Asserting on candidates.csv without deleting it first would pass with the command doing
nothing at all: the CSV is the side that is already right, and merge restores the run
status FROM the CSV whenever that file is present.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import repair_runs_section as _section

ROOT = Path(__file__).resolve().parents[2]
MERGE = ROOT / "tools" / "merge_candidates.py"
HEADER = "subject,relation,object,source,status,confidence,note\n"


def _env(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "cfg")
    return env


def init_kb(tmp_path) -> Path:
    kb = tmp_path / "wiki"
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        cwd=ROOT, env=_env(tmp_path), check=True, capture_output=True, text=True,
    )
    (kb / "sources" / "a.md").write_text("a\n", encoding="utf-8")
    return kb


def repair(tmp_path, kb, *argv) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "factlog", "repair-runs", *argv, "--target", str(kb)],
        cwd=ROOT, env=_env(tmp_path), capture_output=True, text=True,
    )


def item(obj="B", status="candidate", subject="A", relation="knows", source="sources/a.md"):
    row = {
        "subject": subject, "relation": relation, "object": obj,
        "source": source, "confidence": 0.9, "note": "",
    }
    # A run item with NO status key at all is a real shape: merge coerces it to
    # needs_review, so a decision has to reach it too.
    if status is not None:
        row["status"] = status
    return row


def bystander():
    """A live run item for a DIFFERENT fact, sharing the run file with the target.

    The everyday shape (one run file per extraction, many facts in it) and the dimension
    that separates a per-ITEM write from a per-FILE one. Without it, a repair that flipped
    every item in a file it touched stays green (#565).
    """
    return item(subject="Q", relation="rel", obj="Z", status="candidate")


def write_runs(kb: Path, name: str, items: list[dict]) -> None:
    (kb / "runs" / name).write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


def write_csv(kb: Path, *lines: str) -> None:
    (kb / "facts" / "candidates.csv").write_text(HEADER + "".join(x + "\n" for x in lines), encoding="utf-8")


def run_statuses(kb: Path, name: str = "r1.json") -> list[tuple[str, str]]:
    return [
        (i["subject"], i["object"], i.get("status"))
        for i in json.loads((kb / "runs" / name).read_text(encoding="utf-8"))
    ]


def run_bytes(kb: Path) -> dict[str, bytes]:
    # *.json only: `factlog init` also puts a runs/sources/ directory there.
    return {p.name: p.read_bytes() for p in sorted((kb / "runs").glob("*.json"))}


def rebuilt(tmp_path, kb: Path) -> dict[tuple[str, str, str], str]:
    """Delete candidates.csv and rebuild it from runs/*.json alone -- the durability test.

    This is the only question that matters: does the decision survive `/factlog sync`?
    """
    (kb / "facts" / "candidates.csv").unlink()
    subprocess.run(
        [sys.executable, str(MERGE), "--wiki", str(kb)],
        cwd=ROOT, env=_env(tmp_path), capture_output=True, text=True, check=True,
    )
    import csv

    with (kb / "facts" / "candidates.csv").open(newline="", encoding="utf-8") as f:
        return {(r["subject"], r["relation"], r["object"]): r["status"] for r in csv.DictReader(f)}


class TestTheDurabilityPayoff:
    def test_a_decision_missing_from_the_run_row_survives_a_rebuild(self, tmp_path):
        kb = init_kb(tmp_path)
        write_runs(kb, "r1.json", [item(status="candidate"), bystander()])
        write_csv(kb, "A,knows,B,sources/a.md,accepted,0.90,", "Q,rel,Z,sources/a.md,candidate,0.90,")

        proc = repair(tmp_path, kb, "A", "knows", "B", "--apply")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "1 run row(s) repaired in 1 file(s), 0 fact(s) left for a human" in proc.stdout
        assert run_statuses(kb) == [("A", "B", "accepted"), ("Q", "Z", "candidate")]

        after = rebuilt(tmp_path, kb)
        assert after[("A", "knows", "B")] == "accepted"
        # the bystander is a different fact and this decision was never about it
        assert after[("Q", "rel", "Z")] == "candidate"

    def test_a_blank_status_run_row_takes_the_decision(self, tmp_path):
        """merge coerces a missing status to needs_review, so it is pending here too.

        Left alone, that row rebuilds candidates.csv as needs_review and the decision is
        gone -- the #233 downgrade, in a row an extractor mis-stamped.
        """
        kb = init_kb(tmp_path)
        write_runs(kb, "r1.json", [item(status=None), bystander()])
        write_csv(kb, "A,knows,B,sources/a.md,accepted,0.90,", "Q,rel,Z,sources/a.md,candidate,0.90,")

        assert repair(tmp_path, kb, "A", "knows", "B", "--apply").returncode == 0
        assert rebuilt(tmp_path, kb)[("A", "knows", "B")] == "accepted"

    def test_a_retirement_the_run_row_never_heard_about_survives_a_rebuild(self, tmp_path):
        """The eject-drift shape: candidates.csv `superseded`, run row `accepted`.

        Repairing it LOWERS the run row, and that is the whole point -- a rule of "never
        lower a run row" would leave this drift permanently unfixable. It is not an
        arbitrary ranking either: merge's existing_superseded_keys pass keeps a
        candidates.csv `superseded` over a re-asserted engine status on every rebuild, so
        writing it here agrees with the rebuild merge performs.
        """
        kb = init_kb(tmp_path)
        write_runs(kb, "r1.json", [item(status="accepted"), bystander()])
        write_csv(kb, "A,knows,B,sources/a.md,superseded,0.90,", "Q,rel,Z,sources/a.md,candidate,0.90,")

        proc = repair(tmp_path, kb, "A", "knows", "B", "--apply")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert run_statuses(kb) == [("A", "B", "superseded"), ("Q", "Z", "candidate")]

        after = rebuilt(tmp_path, kb)
        assert after[("A", "knows", "B")] == "superseded"
        assert after[("Q", "rel", "Z")] == "candidate"

    def test_every_run_file_holding_the_fact_is_repaired(self, tmp_path):
        """Stopping after the first file leaves the rebuild a lottery, not a failure.

        merge picks between two run files claiming one fact by glob order, not by status,
        so a rebuild can come out right with the second file still stale -- which is
        exactly why this asserts each file's rows directly. The rebuild below is kept as
        the durability check, not as the discriminator.
        """
        kb = init_kb(tmp_path)
        write_runs(kb, "aaa.json", [item(status="candidate"), bystander()])
        write_runs(kb, "zzz.json", [item(status="candidate")])
        write_csv(kb, "A,knows,B,sources/a.md,accepted,0.90,", "Q,rel,Z,sources/a.md,candidate,0.90,")

        proc = repair(tmp_path, kb, "A", "knows", "B", "--apply")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "2 run row(s) repaired in 2 file(s)" in proc.stdout
        assert run_statuses(kb, "aaa.json") == [("A", "B", "accepted"), ("Q", "Z", "candidate")]
        assert run_statuses(kb, "zzz.json") == [("A", "B", "accepted")]

        assert rebuilt(tmp_path, kb)[("A", "knows", "B")] == "accepted"


class TestTheDuplicateFactKeyGuard:
    """A round-trip amend leaves TWO candidates.csv rows on ONE fact_key (measured).

    `--set-object C` then `--set-object B` gives a live `candidate` row and the first
    amend's `superseded` tombstone, keyed identically. Picking "the" CSV status writes the
    tombstone over the LIVE run row and the fact that was alive is gone from the next
    rebuild -- the repair command as a destroyer. Nothing may be written for such a fact.
    """

    def _round_trip_kb(self, tmp_path) -> Path:
        kb = init_kb(tmp_path)
        # The live row FIRST in the run file. merge's dedup winner is decided by the
        # `source` string and, on a tie, by load order -- so with the tombstone first the
        # fact is already lost to dedup on any rebuild, defect or no defect, and the
        # rebuild could not tell the two apart. This order is what makes the payoff visible.
        write_runs(kb, "r1.json", [
            item(obj="B", status="candidate"),
            item(obj="B", status="superseded"),
            item(obj="C", status="superseded"),
            bystander(),
        ])
        write_csv(
            kb,
            "A,knows,B,sources/a.md,candidate,0.90,",
            "A,knows,B,sources/a.md,superseded,0.90,",
            "A,knows,C,sources/a.md,superseded,0.90,",
            "Q,rel,Z,sources/a.md,candidate,0.90,",
        )
        return kb

    def test_a_live_row_is_not_buried_by_its_own_tombstone(self, tmp_path):
        kb = self._round_trip_kb(tmp_path)
        before = run_bytes(kb)

        proc = repair(tmp_path, kb, "A", "knows", "B", "--apply")
        assert proc.returncode == 3, proc.stdout + proc.stderr
        assert "several candidates.csv rows share one fact — NOT repaired:" in proc.stdout
        assert "0 run row(s) repaired in 0 file(s), 1 fact(s) left for a human" in proc.stdout
        assert run_bytes(kb) == before

        # the payoff: the fact that was alive is still alive after a from-scratch rebuild
        assert rebuilt(tmp_path, kb)[("A", "knows", "B")] == "candidate"

    def test_the_shape_this_guards_is_what_a_round_trip_amend_actually_produces(self, tmp_path):
        """Reproduce the duplicate rows through the CLI, so the fixture above cannot rot.

        If `amend` ever stops leaving two rows on one fact_key, this goes red and the guard
        can be re-argued from evidence instead of from a hand-written fixture.
        """
        kb = init_kb(tmp_path)
        write_runs(kb, "r1.json", [item(status="candidate")])
        subprocess.run(
            [sys.executable, str(MERGE), "--wiki", str(kb)],
            cwd=ROOT, env=_env(tmp_path), capture_output=True, text=True, check=True,
        )
        for old, new in (("B", "C"), ("C", "B")):
            proc = subprocess.run(
                [sys.executable, "-m", "factlog", "amend", "A", "knows", old,
                 "--set-object", new, "--target", str(kb)],
                cwd=ROOT, env=_env(tmp_path), capture_output=True, text=True,
            )
            assert proc.returncode == 0, proc.stdout + proc.stderr

        import csv

        with (kb / "facts" / "candidates.csv").open(newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if (r["subject"], r["object"]) == ("A", "B")]
        assert sorted(r["status"] for r in rows) == ["candidate", "superseded"]

        proc = repair(tmp_path, kb, "A", "knows", "B", "--apply")
        assert proc.returncode == 3
        assert "several candidates.csv rows share one fact — NOT repaired:" in proc.stdout


class TestTheCsvStatusWhitelist:
    def test_a_typo_never_reaches_the_run_row(self, tmp_path):
        """The status here comes from a file a human edits, not from a code constant.

        A `not in REVIEW_STATUSES` gate lets `aceptd` through; merge's clean_row then
        demotes it to needs_review on the next rebuild, so the decision would be destroyed
        by the command asked to save it. Hence a whitelist of engine/superseded.
        """
        kb = init_kb(tmp_path)
        write_runs(kb, "r1.json", [item(status="candidate"), bystander()])
        write_csv(kb, "A,knows,B,sources/a.md,aceptd,0.90,", "Q,rel,Z,sources/a.md,candidate,0.90,")
        before = run_bytes(kb)

        proc = repair(tmp_path, kb, "A", "knows", "B", "--apply")
        assert proc.returncode == 3, proc.stdout + proc.stderr
        assert "unrecognised status in candidates.csv — NOT repaired:" in proc.stdout
        assert run_bytes(kb) == before

        assert rebuilt(tmp_path, kb)[("A", "knows", "B")] == "candidate"


class TestClassesApplyMustNotTouch:
    def test_a_decided_run_row_is_left_alone(self, tmp_path):
        """`confirmed` in runs against `accepted` in the CSV is two decisions, not drift
        this command may settle. The run row is also the last surviving copy of the
        `confirmed` ruling -- the CSV has already overwritten it."""
        kb = init_kb(tmp_path)
        write_runs(kb, "r1.json", [item(status="confirmed"), bystander()])
        write_csv(kb, "A,knows,B,sources/a.md,accepted,0.90,", "Q,rel,Z,sources/a.md,candidate,0.90,")
        before = run_bytes(kb)

        proc = repair(tmp_path, kb, "A", "knows", "B", "--apply")
        assert proc.returncode == 3, proc.stdout + proc.stderr
        assert "both stores decided, and they disagree — NOT repaired:" in proc.stdout
        assert run_bytes(kb) == before

        assert rebuilt(tmp_path, kb)[("A", "knows", "B")] == "confirmed"

    def test_no_run_item_is_invented_for_a_csv_only_row(self, tmp_path):
        """Creating one would mean fabricating a docspan and a run_id no extraction
        produced -- forged provenance. Out of scope, reported, never written."""
        kb = init_kb(tmp_path)
        write_runs(kb, "r1.json", [bystander()])
        write_csv(kb, "A,knows,B,sources/a.md,accepted,0.90,", "Q,rel,Z,sources/a.md,candidate,0.90,")
        before = run_bytes(kb)

        proc = repair(tmp_path, kb, "A", "knows", "B", "--apply")
        assert proc.returncode == 3, proc.stdout + proc.stderr
        assert "candidates.csv row with no run backing — NOT repaired:" in proc.stdout
        assert run_bytes(kb) == before

    def test_a_sourceless_row_is_never_written(self, tmp_path):
        """A fact_key with an empty source component matches every other sourceless row.

        accept/reject carry this guard inside `_apply_review_status`; repair-runs needs its
        own, or one decision sprays across every fact that merely shares the defect.
        """
        kb = init_kb(tmp_path)
        write_runs(kb, "r1.json", [item(status="candidate", source=""), bystander()])
        write_csv(kb, "A,knows,B,,accepted,0.90,", "Q,rel,Z,sources/a.md,candidate,0.90,")
        before = run_bytes(kb)

        proc = repair(tmp_path, kb, "A", "knows", "B", "--apply")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "skipped 1 runs/*.json row(s) with no source file" in proc.stdout
        assert run_bytes(kb) == before

    def test_the_filter_scopes_the_write(self, tmp_path):
        kb = init_kb(tmp_path)
        write_runs(kb, "r1.json", [item(status="candidate"), item(obj="D", status="candidate")])
        write_csv(kb, "A,knows,B,sources/a.md,accepted,0.90,", "A,knows,D,sources/a.md,accepted,0.90,")

        assert repair(tmp_path, kb, "A", "knows", "B", "--apply").returncode == 0
        assert run_statuses(kb) == [("A", "B", "accepted"), ("A", "D", "candidate")]


class TestPartialRepairs:
    def test_a_fact_whose_copies_disagree_is_named_partial(self, tmp_path):
        """Repairing only the pending copy leaves the rebuild a lottery, and a bare row
        counter would have called that a success."""
        kb = init_kb(tmp_path)
        write_runs(kb, "aaa.json", [item(status="candidate"), bystander()])
        write_runs(kb, "zzz.json", [item(status="confirmed")])
        write_csv(kb, "A,knows,B,sources/a.md,accepted,0.90,", "Q,rel,Z,sources/a.md,candidate,0.90,")

        proc = repair(tmp_path, kb, "A", "knows", "B", "--apply")
        assert proc.returncode == 3, proc.stdout + proc.stderr
        assert "A / knows / B  ← sources/a.md — PARTIAL" in proc.stdout
        assert "glob order, not by status" in proc.stdout
        assert "1 run row(s) repaired in 1 file(s), 1 fact(s) left for a human" in proc.stdout
        assert run_statuses(kb, "aaa.json") == [("A", "B", "accepted"), ("Q", "Z", "candidate")]
        assert run_statuses(kb, "zzz.json") == [("A", "B", "confirmed")]


class TestIdempotenceAndNoOp:
    def test_a_second_apply_writes_nothing(self, tmp_path):
        """Atomicity is per FILE, not per command: an interruption can leave the KB partly
        repaired, so running it again has to finish the job and then be a no-op."""
        kb = init_kb(tmp_path)
        write_runs(kb, "r1.json", [item(status="candidate"), bystander()])
        write_csv(kb, "A,knows,B,sources/a.md,accepted,0.90,", "Q,rel,Z,sources/a.md,candidate,0.90,")

        assert repair(tmp_path, kb, "A", "knows", "B", "--apply").returncode == 0
        after_first = run_bytes(kb)

        proc = repair(tmp_path, kb, "A", "knows", "B", "--apply")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "0 run row(s) repaired in 0 file(s), 0 fact(s) left for a human" in proc.stdout
        assert run_bytes(kb) == after_first

    def test_a_clean_kb_changes_no_byte(self, tmp_path):
        kb = init_kb(tmp_path)
        write_runs(kb, "r1.json", [item(status="accepted"), bystander()])
        write_csv(kb, "A,knows,B,sources/a.md,accepted,0.90,", "Q,rel,Z,sources/a.md,candidate,0.90,")
        before = run_bytes(kb)

        proc = repair(tmp_path, kb, "A", "knows", "B", "--apply")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "0 run row(s) repaired in 0 file(s), 0 fact(s) left for a human" in proc.stdout
        assert "warning" not in proc.stderr
        assert run_bytes(kb) == before

    def test_apply_does_not_recompile_accepted_dl(self, tmp_path):
        """repair-runs writes runs/*.json only, so the compiler's input (candidates.csv) is
        unchanged. Recompiling would be a side effect with no cause and would leave the
        user asking why their .dl moved."""
        kb = init_kb(tmp_path)
        write_runs(kb, "r1.json", [item(status="candidate"), bystander()])
        write_csv(kb, "A,knows,B,sources/a.md,accepted,0.90,", "Q,rel,Z,sources/a.md,candidate,0.90,")
        dl = kb / "facts" / "accepted.dl"
        dl.write_text("% sentinel\n", encoding="utf-8")
        csv_before = (kb / "facts" / "candidates.csv").read_bytes()

        proc = repair(tmp_path, kb, "A", "knows", "B", "--apply")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert dl.read_text(encoding="utf-8") == "% sentinel\n"
        assert (kb / "facts" / "candidates.csv").read_bytes() == csv_before
        assert "accepted.dl is unchanged" in proc.stdout


class TestTheMassRewriteGuard:
    def test_apply_without_a_selector_is_refused(self, tmp_path):
        kb = init_kb(tmp_path)
        write_runs(kb, "r1.json", [item(status="candidate"), bystander()])
        write_csv(kb, "A,knows,B,sources/a.md,accepted,0.90,", "Q,rel,Z,sources/a.md,candidate,0.90,")
        before = run_bytes(kb)

        proc = repair(tmp_path, kb, "--apply")
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "would rewrite run rows across the whole KB" in proc.stderr
        assert run_bytes(kb) == before

    def test_all_wildcards_are_no_selector_either(self, tmp_path):
        kb = init_kb(tmp_path)
        write_runs(kb, "r1.json", [item(status="candidate")])
        write_csv(kb, "A,knows,B,sources/a.md,accepted,0.90,")

        proc = repair(tmp_path, kb, "-", "-", "-", "--apply")
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "pass --all to mean it" in proc.stderr

    def test_all_means_it(self, tmp_path):
        kb = init_kb(tmp_path)
        write_runs(kb, "r1.json", [item(status="candidate"), bystander()])
        write_csv(kb, "A,knows,B,sources/a.md,accepted,0.90,", "Q,rel,Z,sources/a.md,candidate,0.90,")

        proc = repair(tmp_path, kb, "--apply", "--all")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert run_statuses(kb) == [("A", "B", "accepted"), ("Q", "Z", "candidate")]

    def test_reporting_needs_no_selector(self, tmp_path):
        """The guard is about writing. A KB-wide REPORT is the natural way to ask "is this
        KB drifted?" and must stay free."""
        kb = init_kb(tmp_path)
        write_runs(kb, "r1.json", [item(status="candidate")])
        write_csv(kb, "A,knows,B,sources/a.md,accepted,0.90,")

        proc = repair(tmp_path, kb)
        assert proc.returncode == 3, proc.stdout + proc.stderr
        assert "the whole KB" in proc.stdout


class TestATypedObjectSelector:
    """A selector must reach the fact it names, whatever spelling of it the user types.

    fact_key canonicalises an amount object, so `amount(7,억)` in runs and the canonical
    `amount(7,"억")` in candidates.csv are ONE fact. Measured before the fix: comparing the
    selector against RAW fields matched one store at a time -- the canonical spelling
    selected the CSV row and reported `no run backing` with the run row sitting right there,
    the bare spelling selected the run row and reported it as having no CSV row -- while the
    unfiltered `--all` scan repaired it correctly.

    That is the gate designed backwards: naming the fact (the careful thing) failed and
    reported a falsehood, rewriting the whole KB (the broad thing) worked. It is also
    exactly the drift class this command exists for -- `accept` DOES reach this row
    (test_accept_durable.sh block (l)), because it selects the CSV row and re-keys THAT row
    to find the run row.
    """

    def _amount_kb(self, tmp_path) -> Path:
        kb = init_kb(tmp_path)
        write_runs(kb, "r1.json", [
            item(subject="A", relation="costs", obj="amount(7,억)", status="candidate"),
            bystander(),
        ])
        # merged by the real merge, so candidates.csv holds whatever canonical form it
        # actually produces rather than a hand-written guess at it
        subprocess.run(
            [sys.executable, str(MERGE), "--wiki", str(kb)],
            cwd=ROOT, env=_env(tmp_path), capture_output=True, text=True, check=True,
        )
        csv_path = kb / "facts" / "candidates.csv"
        text = csv_path.read_text(encoding="utf-8")
        assert 'A,costs,"amount(7,""억"")",sources/a.md,candidate' in text, text
        csv_path.write_text(
            text.replace(
                'A,costs,"amount(7,""억"")",sources/a.md,candidate',
                'A,costs,"amount(7,""억"")",sources/a.md,accepted',
            ),
            encoding="utf-8",
        )
        return kb

    def test_the_canonical_spelling_reaches_the_run_row(self, tmp_path):
        kb = self._amount_kb(tmp_path)
        proc = repair(tmp_path, kb, "A", "costs", 'amount(7,"억")', "--apply")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "1 run row(s) repaired in 1 file(s), 0 fact(s) left for a human" in proc.stdout
        assert ("A", "amount(7,억)", "accepted") in run_statuses(kb)

        assert rebuilt(tmp_path, kb)[("A", "costs", 'amount(7,"억")')] == "accepted"

    def test_the_bare_spelling_reaches_the_same_row(self, tmp_path):
        kb = self._amount_kb(tmp_path)
        proc = repair(tmp_path, kb, "A", "costs", "amount(7,억)", "--apply")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "1 run row(s) repaired in 1 file(s), 0 fact(s) left for a human" in proc.stdout
        assert ("A", "amount(7,억)", "accepted") in run_statuses(kb)

        assert rebuilt(tmp_path, kb)[("A", "costs", 'amount(7,"억")')] == "accepted"

    def test_neither_spelling_reports_a_missing_counterpart(self, tmp_path):
        """The specific falsehood the raw-field filter told, pinned in both directions."""
        for n, selector in enumerate(('amount(7,"억")', "amount(7,억)")):
            kb = self._amount_kb(tmp_path / f"case{n}")
            proc = repair(tmp_path, kb, "A", "costs", selector)
            assert proc.returncode == 3, proc.stdout + proc.stderr
            assert "1 candidates.csv row(s), 1 runs/*.json row(s) in scope" in proc.stdout
            assert _section(proc.stdout, "candidates.csv row with no run backing — NOT repaired").strip() == "(none)"
            assert _section(proc.stdout, "run row with no candidates.csv row — NOT repaired").strip() == "(none)"

    def test_a_different_amount_is_still_excluded(self, tmp_path):
        """The counter-example: canonicalising the selector must not make amounts collide."""
        kb = self._amount_kb(tmp_path)
        before = run_bytes(kb)
        proc = repair(tmp_path, kb, "A", "costs", 'amount(8,"억")', "--apply")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "0 candidates.csv row(s), 0 runs/*.json row(s) in scope" in proc.stdout
        assert run_bytes(kb) == before


class TestUnreadableFilesDuringApply:
    def test_the_readable_files_are_still_repaired_and_the_exit_code_says_partial(self, tmp_path):
        """Named and survived, not fatal -- but a comparison that could not see every file
        is not "clean" and not plainly "drift found" either, so it gets its own code."""
        kb = init_kb(tmp_path)
        write_runs(kb, "good.json", [item(status="candidate"), bystander()])
        (kb / "runs" / "bin.json").write_bytes(b"\xff\xfe\x00binary")
        write_csv(kb, "A,knows,B,sources/a.md,accepted,0.90,", "Q,rel,Z,sources/a.md,candidate,0.90,")

        proc = repair(tmp_path, kb, "A", "knows", "B", "--apply")
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "could not read bin.json" in proc.stderr
        assert "the comparison is INCOMPLETE" in proc.stderr
        assert run_statuses(kb, "good.json") == [("A", "B", "accepted"), ("Q", "Z", "candidate")]

    def test_repairing_the_file_then_re_running_reaches_its_row(self, tmp_path):
        """The whole point of #566: after the user fixes the file, the decision can finally
        get there. `accept` cannot do it -- its CSV row is no longer pending."""
        kb = init_kb(tmp_path)
        write_runs(kb, "good.json", [item(status="candidate")])
        (kb / "runs" / "bin.json").write_bytes(b"\xff\xfe\x00binary")
        write_csv(kb, "A,knows,B,sources/a.md,accepted,0.90,")
        assert repair(tmp_path, kb, "A", "knows", "B", "--apply").returncode == 1

        write_runs(kb, "bin.json", [item(status="candidate")])  # the user restores the file
        proc = repair(tmp_path, kb, "A", "knows", "B", "--apply")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert run_statuses(kb, "bin.json") == [("A", "B", "accepted")]

        assert rebuilt(tmp_path, kb)[("A", "knows", "B")] == "accepted"
