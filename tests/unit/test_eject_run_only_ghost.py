# SPDX-License-Identifier: Apache-2.0
"""`eject` must SEE the ghost sources that live only in runs/*.json — and must not
destroy the last copy of a fact on the way (#559).

merge drops a run row whose source is gone BEFORE writing facts/candidates.csv, so
that row is invisible to a cited set built from the CSV: `eject --orphans` reported
"no orphaned sources found" while every merge kept dropping and warning about the
same rows. The loop never closed.

Teaching eject to read runs/*.json closes it, and introduces the first eject class
for which a `superseded` tombstone is not automatic. Every other orphan reaches the
retirement step BECAUSE it has a candidates.csv row; a run-only ghost has none, so
the naive union strips the run row at exit 0 and the fact is gone from the KB with
no audit trail and no way back. These tests pin both halves: the ghost is found,
AND a tombstone is written before the strip.

Two shapes are load-bearing enough to state here:

* the tombstone LOOKUP is `common.fact_key` on BOTH sides (run row and CSV row).
  That key is wider than `match_row`, which does not strip the CSV value — and the
  width is the safe direction. Look the fact up with the narrow key and a KB whose
  CSV row is `"  sources/live.md "` gets a tombstone for a fact a human already
  ruled on; the next merge folds the two rows and `superseded` wins, silently
  erasing the decision. TestNoDemotion is that net.
* the tombstone is written to candidates.csv BEFORE the run rows are stripped, so a
  crash between the two converges on retirement (merge preserves a tombstone), never
  on a fact with no copy anywhere.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MERGE = ROOT / "tools" / "merge_candidates.py"
VALIDATE = ROOT / "tools" / "validate.py"
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
    return kb


def run(tmp_path, *argv) -> subprocess.CompletedProcess:
    """Drive the real CLI: exit code and the two streams a user actually reads."""
    return subprocess.run(
        [sys.executable, "-m", "factlog", *argv],
        cwd=ROOT, env=_env(tmp_path), capture_output=True, text=True,
    )


def eject(tmp_path, kb, *argv) -> subprocess.CompletedProcess:
    return run(tmp_path, "eject", *argv, "--target", str(kb))


def merge(tmp_path, kb) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(MERGE), "--wiki", str(kb)],
        cwd=ROOT, env=_env(tmp_path), capture_output=True, text=True,
    )


def validate(tmp_path, kb) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(VALIDATE), "--target", str(kb)],
        cwd=ROOT, env=_env(tmp_path), capture_output=True, text=True,
    )


def csv_line(subject, relation, object_, source, status="confirmed", confidence="0.90", note="") -> str:
    """One candidates.csv line, quoted the way the writers quote it.

    Hand-formatted lines are fine until a value carries a comma — an `amount(7,"억")`
    object splits into two fields and the fixture stops describing the KB it names.
    """
    import io

    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(
        [subject, relation, object_, source, status, confidence, note]
    )
    return buf.getvalue()


def write_csv(kb: Path, *rows: str) -> None:
    (kb / "facts" / "candidates.csv").write_text(HEADER + "".join(rows), encoding="utf-8")


def read_rows(kb: Path) -> list[dict[str, str]]:
    with (kb / "facts" / "candidates.csv").open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run_row(subject="유령", relation="참조", object_="대상", source="sources/ghost.md",
            status="candidate", confidence="0.9", note="") -> dict[str, str]:
    return {
        "subject": subject, "relation": relation, "object": object_, "source": source,
        "status": status, "confidence": confidence, "note": note,
    }


def write_run(kb: Path, name: str, *rows: dict) -> Path:
    path = kb / "runs" / name
    path.write_text(json.dumps(list(rows), ensure_ascii=False), encoding="utf-8")
    return path


def ghost_kb(tmp_path, *, csv_rows: tuple[str, ...] = (), run_rows=None) -> Path:
    """A KB whose only trace of a fact is a runs/*.json row citing a missing file.

    The DEFAULT candidates.csv is header-only, which is the commonest shape of this
    state: merge dropped the row before writing the file. It is also the shape the
    retirement step used to skip entirely (`if rows:`), so a fix measured on a KB
    with unrelated rows in it would pass while doing nothing here.
    """
    kb = init_kb(tmp_path)
    write_csv(kb, *csv_rows)
    write_run(kb, "2026-01-01-ghost.json", *(run_rows or [run_row()]))
    return kb


class TestRunOnlyGhostIsSelected:
    def test_orphans_finds_a_ghost_with_no_candidates_row(self, tmp_path):
        kb = ghost_kb(tmp_path)
        proc = eject(tmp_path, kb, "--orphans")
        assert proc.returncode == 0, proc.stderr
        assert "1 orphaned source(s)" in proc.stdout, proc.stdout
        assert "sources/ghost.md" in proc.stdout

    def test_orphans_leaves_a_live_source_alone(self, tmp_path):
        kb = init_kb(tmp_path)
        (kb / "sources" / "live.md").write_text("live\n", encoding="utf-8")
        write_csv(kb)
        path = write_run(kb, "r.json", run_row(source="sources/live.md"))
        before = path.read_text(encoding="utf-8")
        proc = eject(tmp_path, kb, "--orphans")
        assert "no orphaned sources found" in proc.stdout, proc.stdout
        assert path.read_text(encoding="utf-8") == before

    def test_a_ref_outside_the_source_roots_is_not_auto_selected(self, tmp_path):
        """The scan only auto-selects refs under the two source roots, so a
        malformed citation is never ejected by a command the user did not aim."""
        kb = ghost_kb(tmp_path, run_rows=[run_row(source="ghosty.md")])
        path = kb / "runs" / "2026-01-01-ghost.json"
        before = path.read_text(encoding="utf-8")
        proc = eject(tmp_path, kb, "--orphans")
        assert "no orphaned sources found" in proc.stdout, proc.stdout
        assert path.read_text(encoding="utf-8") == before

    def test_a_ref_outside_the_source_roots_is_ejectable_by_name(self, tmp_path):
        """The other half: naming it works, so the report can point at a route that
        exists. `matches()` compares the exact ref before any prefix rule, and the
        union puts a run-only ref into the known set for it to compare against."""
        kb = ghost_kb(tmp_path, run_rows=[run_row(source="ghosty.md")])
        proc = eject(tmp_path, kb, "ghosty.md")
        assert proc.returncode == 0, proc.stderr + proc.stdout
        assert "1 matched source ref(s)" in proc.stdout, proc.stdout
        assert not (kb / "runs" / "2026-01-01-ghost.json").exists()

    def test_a_ref_outside_the_source_roots_gets_no_tombstone(self, tmp_path):
        """validate REJECTS a candidates.csv source outside the two roots, so writing
        a tombstone for one would put the KB into the permanent "validation failed"
        state #562 removed. Stripped without one — and said out loud, because it is
        the one route through this command that still drops a last copy."""
        kb = ghost_kb(tmp_path, run_rows=[run_row(source="ghosty.md")])
        proc = eject(tmp_path, kb, "ghosty.md")
        assert read_rows(kb) == []
        assert "no tombstone" in proc.stderr, proc.stderr
        assert "ghosty.md" in proc.stderr
        assert validate(tmp_path, kb).returncode == 0, validate(tmp_path, kb).stdout


class TestTombstone:
    def test_header_only_candidates_csv_gets_a_tombstone(self, tmp_path):
        """★ The typical KB. `if rows:` used to wrap the whole retirement step, so on
        a header-only candidates.csv — no rows to iterate — the strip ran and nothing
        was ever written. A fix measured on a KB with unrelated rows in it passes
        while this, the ordinary state, silently loses the fact."""
        kb = ghost_kb(tmp_path)
        proc = eject(tmp_path, kb, "--orphans")
        assert proc.returncode == 0, proc.stderr
        rows = read_rows(kb)
        assert len(rows) == 1, rows
        assert rows[0]["subject"] == "유령"
        assert rows[0]["relation"] == "참조"
        assert rows[0]["object"] == "대상"
        assert rows[0]["source"] == "sources/ghost.md"
        assert rows[0]["status"] == "superseded"
        assert not (kb / "runs" / "2026-01-01-ghost.json").exists()

    def test_the_summary_counts_tombstones_apart_from_retired_rows(self, tmp_path):
        """"Rows retired" and "rows invented" are different events. Summing them
        makes a KB that lost a fact read exactly like one that retired a row."""
        kb = ghost_kb(tmp_path)
        proc = eject(tmp_path, kb, "--orphans")
        assert "0 candidate row(s) superseded, 1 tombstone(s) written" in proc.stdout, proc.stdout

    def test_missing_candidates_csv_is_created_with_the_fact_header(self, tmp_path):
        """A KB with no candidates.csv at all still has to keep the fact. The
        `fieldnames or FACT_HEADER` fallback gets its first real use here."""
        kb = ghost_kb(tmp_path)
        (kb / "facts" / "candidates.csv").unlink()
        assert eject(tmp_path, kb, "--orphans").returncode == 0
        text = (kb / "facts" / "candidates.csv").read_text(encoding="utf-8")
        assert text.startswith(HEADER)
        assert read_rows(kb)[0]["status"] == "superseded"

    def test_tombstone_values_are_merges_own_normalisation(self, tmp_path):
        """The tombstone must equal the row merge WOULD have written, or the next
        merge treats it as a different fact and the retirement does not hold:
        stripped subject/relation, canonical amount, two-decimal confidence."""
        kb = ghost_kb(tmp_path, run_rows=[run_row(
            subject="  A  ", relation=" R ", object_="amount(7,억)",
            source="sources/ghost.md", confidence="0.9",
        )])
        assert eject(tmp_path, kb, "--orphans").returncode == 0
        row = read_rows(kb)[0]
        assert row["subject"] == "A"
        assert row["relation"] == "R"
        assert row["object"] == 'amount(7,"억")'
        assert row["confidence"] == "0.90"
        assert "last copy" in row["note"]

    def test_tombstone_source_is_the_matched_key_not_the_raw_run_value(self, tmp_path):
        """Write the raw value and the next scan cannot find its own tombstone: the
        ref stays "cited with no retired row" forever and `--orphans` never reaches
        "nothing to do"."""
        kb = ghost_kb(tmp_path, run_rows=[run_row(source=" sources/ghost.md#sec3 ")])
        assert eject(tmp_path, kb, "--orphans").returncode == 0
        assert read_rows(kb)[0]["source"] == "sources/ghost.md"
        again = eject(tmp_path, kb, "--orphans")
        assert again.returncode == 0
        assert "no orphaned sources found" in again.stdout, again.stdout
        assert len(read_rows(kb)) == 1

    def test_two_run_rows_of_one_fact_write_one_tombstone(self, tmp_path):
        """merge collapses rows differing only in confidence/note into ONE fact
        (fact_key carries neither), so two tombstones would be two rows of a fact
        that has one."""
        kb = ghost_kb(tmp_path, run_rows=[
            run_row(confidence="0.9", note="first"),
            run_row(confidence="0.4", note="second"),
        ])
        assert eject(tmp_path, kb, "--orphans").returncode == 0
        assert len(read_rows(kb)) == 1

    def test_incomplete_run_row_is_stripped_without_a_tombstone(self, tmp_path):
        """merge drops a row missing one of the first four fields, and a tombstone
        built from one would fail validate (empty subject). So it is stripped with no
        tombstone — the documented cost of matching merge's completeness rule.

        The ref reaches the scan through an unrelated candidates.csv row (a live one:
        a ref whose rows are ALL superseded is skipped by the idempotency filter),
        which is what isolates the incomplete row's OWN fact: nothing may be written
        for it.
        """
        kb = ghost_kb(
            tmp_path,
            csv_rows=("A,rel,B,sources/ghost.md,confirmed,0.90,\n",),
            run_rows=[run_row(subject="", object_="C")],
        )
        proc = eject(tmp_path, kb, "--orphans")
        assert proc.returncode == 0, proc.stderr
        assert "0 tombstone(s) written" in proc.stdout, proc.stdout
        assert not (kb / "runs" / "2026-01-01-ghost.json").exists()
        rows = read_rows(kb)
        assert [(r["subject"], r["object"]) for r in rows] == [("A", "B")], rows


class TestNoDemotion:
    """★ The KB-corrupting mistake: a tombstone written for a fact candidates.csv
    ALREADY carries. The two rows fold on the next merge and `superseded` wins, so a
    human's `confirmed` disappears with no warning — and the #218 ratchet does not
    stop it, because the fact is not being deleted, only re-decided.

    Every case here is a shape where the run row and the CSV row are ONE fact to
    merge but differ literally. `fact_key` on both sides sees that; a raw compare, or
    `match_row`'s unstripped CSV key, does not.
    """

    def _guard(self, tmp_path, kb, *, expect_status):
        before = read_rows(kb)
        assert len(before) == 1, before
        proc = eject(tmp_path, kb, "--orphans")
        assert proc.returncode == 0, proc.stderr
        assert "tombstone(s) written" in proc.stdout
        assert "1 tombstone(s) written" not in proc.stdout, proc.stdout
        after = read_rows(kb)
        assert len(after) == 1, after
        assert after[0]["status"] == expect_status, after
        # ...and it still reads that way to the next merge. Its exit code is not the
        # subject here: a KB whose runs/ no longer assert a `confirmed` row makes the
        # #218 ratchet REFUSE the rebuild (rc 1), which is the pre-existing contract.
        # What must hold either way is that no second row appeared and no decision
        # was rewritten.
        merged = merge(tmp_path, kb)
        assert "Traceback" not in merged.stderr, merged.stderr
        final = read_rows(kb)
        assert len(final) == 1, final
        assert final[0]["status"] == expect_status, final

    def test_anchored_csv_row_and_anchored_run_row(self, tmp_path):
        kb = ghost_kb(
            tmp_path,
            csv_rows=("유령,참조,대상,sources/ghost.md#sec3,confirmed,0.90,\n",),
            run_rows=[run_row(source="sources/ghost.md#sec3")],
        )
        # The CSV row IS matched (match_row cuts the anchor), so it retires — the
        # contract since the command existed. What must not happen is a SECOND row.
        self._guard(tmp_path, kb, expect_status="superseded")

    def test_anchored_csv_row_and_plain_run_row(self, tmp_path):
        kb = ghost_kb(
            tmp_path,
            csv_rows=("유령,참조,대상,sources/ghost.md#sec3,confirmed,0.90,\n",),
            run_rows=[run_row(source="sources/ghost.md")],
        )
        self._guard(tmp_path, kb, expect_status="superseded")

    def test_padded_csv_source_and_clean_run_row(self, tmp_path):
        """The demotion case proper: `match_row` does NOT strip, so this row is not
        retired by the command at all — and if the tombstone lookup used that same
        narrow key, the command would ADD a `superseded` row for a fact the human
        confirmed, and merge would fold the decision away."""
        kb = ghost_kb(
            tmp_path,
            csv_rows=("유령,참조,대상,  sources/ghost.md ,confirmed,0.90,\n",),
            run_rows=[run_row(source="sources/ghost.md")],
        )
        self._guard(tmp_path, kb, expect_status="confirmed")

    def test_object_merge_would_canonicalise_and_a_padded_csv_source(self, tmp_path):
        """Same demotion, reached through the OBJECT axis: NFD vs NFC inside an
        amount compound is one fact to merge (fact_key folds then canonicalises)."""
        kb = ghost_kb(
            tmp_path,
            csv_rows=(
                csv_line("유령", "참조", unicodedata.normalize("NFD", 'amount(7,"억")'),
                         "  sources/ghost.md "),
            ),
            run_rows=[run_row(
                object_=unicodedata.normalize("NFC", "amount(7,억)"),
                source="sources/ghost.md",
            )],
        )
        self._guard(tmp_path, kb, expect_status="confirmed")


class TestPurgeRefusesTheLastCopy:
    def test_orphans_purge_refuses_and_changes_nothing(self, tmp_path):
        """--purge on a run-only ghost is the one route this change would make
        IRREVERSIBLE: no tombstone by definition, and the run row gone too."""
        kb = ghost_kb(tmp_path)
        before_csv = (kb / "facts" / "candidates.csv").read_text(encoding="utf-8")
        before_run = (kb / "runs" / "2026-01-01-ghost.json").read_text(encoding="utf-8")
        proc = eject(tmp_path, kb, "--orphans", "--purge")
        assert proc.returncode == 1, proc.stdout
        assert (kb / "facts" / "candidates.csv").read_text(encoding="utf-8") == before_csv
        assert (kb / "runs" / "2026-01-01-ghost.json").read_text(encoding="utf-8") == before_run

    def test_the_refusal_names_the_two_pass_route(self, tmp_path):
        """A refusal with no way forward is a wall. The two-pass route is the one
        measured to work, and it is the reason no --force exists."""
        kb = ghost_kb(tmp_path)
        err = eject(tmp_path, kb, "--orphans", "--purge").stderr
        assert "factlog eject --orphans --target" in err, err
        assert "factlog eject --orphans --purge --target" in err, err

    def test_the_two_pass_route_actually_works(self, tmp_path):
        kb = ghost_kb(tmp_path)
        assert eject(tmp_path, kb, "--orphans").returncode == 0
        assert [r["status"] for r in read_rows(kb)] == ["superseded"]
        second = eject(tmp_path, kb, "--orphans", "--purge")
        assert second.returncode == 0, second.stderr
        assert read_rows(kb) == []

    def test_fact_mode_purge_refuses_the_last_copy(self, tmp_path):
        """Fact mode strips runs/*.json under --purge, keyed on the TRIPLE, so it
        reaches run rows of sources candidates.csv never carried."""
        kb = init_kb(tmp_path)
        (kb / "sources" / "a.md").write_text("a\n", encoding="utf-8")
        write_csv(kb, "X,rel,Y,sources/a.md,confirmed,0.90,\n")
        write_run(kb, "r.json",
                  run_row(subject="X", relation="rel", object_="Y", source="sources/a.md"),
                  run_row(subject="X", relation="rel", object_="Y", source="sources/b.md"))
        before = (kb / "runs" / "r.json").read_text(encoding="utf-8")
        proc = eject(tmp_path, kb, "--fact", "X", "rel", "Y", "--purge")
        assert proc.returncode == 1, proc.stdout
        assert len(read_rows(kb)) == 1
        assert (kb / "runs" / "r.json").read_text(encoding="utf-8") == before

    def test_the_fact_mode_refusal_names_a_route_that_works(self, tmp_path):
        """A refusal is only legitimate if the way out it prints actually works."""
        kb = init_kb(tmp_path)
        (kb / "sources" / "a.md").write_text("a\n", encoding="utf-8")
        write_csv(kb, "X,rel,Y,sources/a.md,confirmed,0.90,\n")
        write_run(kb, "r.json",
                  run_row(subject="X", relation="rel", object_="Y", source="sources/a.md"),
                  run_row(subject="X", relation="rel", object_="Y", source="sources/b.md"))
        assert eject(tmp_path, kb, "--fact", "X", "rel", "Y", "--purge").returncode == 1
        # sources/b.md is gone from disk, so --orphans is the route the message names.
        assert eject(tmp_path, kb, "--orphans").returncode == 0
        assert {r["source"] for r in read_rows(kb)} == {"sources/a.md", "sources/b.md"}
        second = eject(tmp_path, kb, "--fact", "X", "rel", "Y", "--purge")
        assert second.returncode == 0, second.stderr
        assert read_rows(kb) == []

    def test_purge_proceeds_when_every_run_row_has_a_candidates_row(self, tmp_path):
        kb = init_kb(tmp_path)
        (kb / "sources" / "a.md").write_text("a\n", encoding="utf-8")
        write_csv(kb, "X,rel,Y,sources/a.md,confirmed,0.90,\n")
        write_run(kb, "r.json",
                  run_row(subject="X", relation="rel", object_="Y", source="sources/a.md"))
        proc = eject(tmp_path, kb, "--fact", "X", "rel", "Y", "--purge")
        assert proc.returncode == 0, proc.stderr
        assert read_rows(kb) == []
        assert not (kb / "runs" / "r.json").exists()


class TestDryRunHonesty:
    def test_dry_run_prints_what_it_would_strip_and_the_last_copies(self, tmp_path):
        """Before the scan moved into selection, the only pre-flight signal a user
        got was `candidates.csv: 0 row(s) to supersede` — which reads as "nothing
        happens" on the very KB where a fact was about to disappear."""
        kb = ghost_kb(tmp_path)
        proc = eject(tmp_path, kb, "--orphans", "--dry-run")
        assert proc.returncode == 0, proc.stderr
        assert "1 row(s) to strip" in proc.stdout, proc.stdout
        assert "1 file(s) would be emptied" in proc.stdout, proc.stdout
        assert "LAST COPY: 1 fact(s)" in proc.stdout, proc.stdout

    def test_dry_run_and_the_real_run_print_the_same_plan(self, tmp_path):
        """One computation, printed twice — the only way the preview can be trusted."""
        def plan_lines(out: str) -> list[str]:
            return [ln for ln in out.splitlines() if ln.startswith("  ")]

        kb = ghost_kb(tmp_path)
        dry = eject(tmp_path, kb, "--orphans", "--dry-run")
        real = eject(tmp_path, kb, "--orphans")
        assert plan_lines(dry.stdout) == plan_lines(real.stdout), (dry.stdout, real.stdout)

    def test_dry_run_changes_nothing(self, tmp_path):
        kb = ghost_kb(tmp_path)
        before = (kb / "runs" / "2026-01-01-ghost.json").read_text(encoding="utf-8")
        assert eject(tmp_path, kb, "--orphans", "--dry-run").returncode == 0
        assert (kb / "runs" / "2026-01-01-ghost.json").read_text(encoding="utf-8") == before
        assert read_rows(kb) == []


class TestRunFilePreservation:
    def test_a_live_row_in_the_same_file_survives_exactly(self, tmp_path):
        """runs/*.json is the recovery artifact #218 tells users to restore. A file
        holding one ghost row and one live row must come back holding exactly the
        live row — existence alone does not catch an unconditional unlink, and a
        content check does."""
        kb = init_kb(tmp_path)
        (kb / "sources" / "live.md").write_text("live\n", encoding="utf-8")
        write_csv(kb)
        live = run_row(subject="살아", object_="있음", source="sources/live.md")
        path = write_run(kb, "mixed.json", run_row(), live)
        assert eject(tmp_path, kb, "--orphans").returncode == 0
        assert path.is_file(), "the run file holding a live row was deleted"
        assert json.loads(path.read_text(encoding="utf-8")) == [live]

    def test_a_file_of_only_ghost_rows_is_removed(self, tmp_path):
        kb = ghost_kb(tmp_path)
        assert eject(tmp_path, kb, "--orphans").returncode == 0
        assert not (kb / "runs" / "2026-01-01-ghost.json").exists()


class TestRoundTrip:
    def test_ghost_kb_survives_eject_then_merge_then_validate(self, tmp_path):
        """The loop this issue is about, closed end to end: the cleanup command
        leaves a KB merge accepts, validate passes, and a re-run calls clean."""
        kb = ghost_kb(tmp_path)
        (kb / "sources" / "live.md").write_text("live\n", encoding="utf-8")
        write_run(kb, "live.json", run_row(subject="살아", source="sources/live.md"))
        assert eject(tmp_path, kb, "--orphans").returncode == 0

        merged = merge(tmp_path, kb)
        assert merged.returncode == 0, merged.stderr
        assert "carried over 1 superseded tombstone(s)" in merged.stdout, merged.stdout
        statuses = {r["source"]: r["status"] for r in read_rows(kb)}
        assert statuses["sources/ghost.md"] == "superseded"
        assert "sources/live.md" in statuses

        validated = validate(tmp_path, kb)
        assert validated.returncode == 0, validated.stdout + validated.stderr

        again = eject(tmp_path, kb, "--orphans")
        assert "no orphaned sources found" in again.stdout, again.stdout
