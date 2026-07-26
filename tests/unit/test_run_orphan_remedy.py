# SPDX-License-Identifier: Apache-2.0
"""The remedy a run-orphan line prints must be one the named command performs (#558).

The first version of this report printed ONE hint for every run-cited missing
source: "`factlog eject --orphans` does not cover these". True for the blind spot
that issue is about — a `candidate` ghost row, which merge silently drops from
candidates.csv, taking it out of the cited set eject built from that file.

False for a ghost row a human has ruled on. The #218 ratchet REFUSES the rebuild
that would delete such a row, so it stays in candidates.csv, eject sees it, and
one `factlog eject --orphans` retires the row and strips it from runs/*.json. The
hint sent the reader to hand-edit run files while the command they were told not
to use would have done it.

Since #559 the blind spot is not blind either: eject reads runs/*.json too, so it
retires a run-only ghost as well — writing a `superseded` tombstone first, because
that run row is the fact's last copy. What is left un-auto-selected is a ref
OUTSIDE the two source roots, which the scan refuses to guess about; naming it
still works. So the answer is three-valued, and a two-way split now prints the
"candidates.csv still carries rows for it" wording for a ref that table has never
heard of — the same shape of lie, one class over.

The class is decided per ref by `eject_visible_refs`, derived from cmd_eject's own
rule. Note what that rule is NOT: coverage's own `orphans` list, which counts
engine statuses only. A `needs_review` ghost is absent from `orphans` and still
ejectable, so keying the hint on `orphans` would have kept the same lie for that
status.
"""
from __future__ import annotations

import json
import unicodedata

import pytest
from source_coverage import (
    EJECT_BLOCKED,
    EJECT_BY_NAME,
    EJECT_CSV_ROW,
    EJECT_RUN_ONLY,
    eject_visible_refs,
    report_run_orphans,
    report_unreadable_runs,
    run_orphan_sources,
)

_HEADER = "subject,relation,object,source,status,confidence,note\n"


def write_csv(tmp_path, *rows):
    """Write candidates.csv into a KB-shaped tree and return the KB root."""
    (tmp_path / "facts").mkdir(exist_ok=True)
    (tmp_path / "facts" / "candidates.csv").write_text(
        _HEADER + "".join(rows), encoding="utf-8"
    )
    return tmp_path


def write_run(tmp_path, *sources):
    (tmp_path / "runs").mkdir(exist_ok=True)
    (tmp_path / "runs" / "r.json").write_text(
        json.dumps([
            {"subject": "A", "relation": "rel", "object": f"B{i}", "source": src,
             "status": "candidate", "confidence": "0.9", "note": ""}
            for i, src in enumerate(sources)
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    return tmp_path


class TestEjectVisibleRefs:
    @pytest.mark.parametrize("status", ["confirmed", "accepted", "needs_review", "candidate", "superseded"])
    def test_every_status_is_visible_to_eject(self, tmp_path, status):
        """cmd_eject reads the raw CSV and filters by nothing. A status filter here
        would re-introduce the false hint for whichever statuses it left out."""
        root = write_csv(tmp_path, f"A,rel,B,sources/ghost.md,{status},0.90,\n")
        assert eject_visible_refs(root) == {"sources/ghost.md": EJECT_CSV_ROW}

    def test_anchor_is_stripped(self, tmp_path):
        root = write_csv(tmp_path, "A,rel,B,sources/ghost.md#sec,confirmed,0.90,\n")
        assert eject_visible_refs(root) == {"sources/ghost.md": EJECT_CSV_ROW}

    def test_nfd_folds_onto_nfc(self, tmp_path):
        """eject NFC-folds its cited refs, so the run-orphan key (also NFC) matches."""
        nfc = "sources/방법론.md"
        root = write_csv(tmp_path, f"A,rel,B,{unicodedata.normalize('NFD', nfc)},confirmed,0.90,\n")
        assert eject_visible_refs(root) == {nfc: EJECT_CSV_ROW}

    def test_runs_sources_prefix_is_visible(self, tmp_path):
        root = write_csv(tmp_path, "A,rel,B,runs/sources/conv.md,confirmed,0.90,\n")
        assert eject_visible_refs(root) == {"runs/sources/conv.md": EJECT_CSV_ROW}

    def test_a_run_only_ref_is_its_own_class(self, tmp_path):
        """candidates.csv has nothing for it, so `--orphans` retires it by writing a
        tombstone — a different sentence from "the table still carries rows"."""
        root = write_run(write_csv(tmp_path), "sources/ghost.md")
        assert eject_visible_refs(root) == {"sources/ghost.md": EJECT_RUN_ONLY}

    def test_a_ref_in_both_stores_is_the_candidates_class(self, tmp_path):
        """The CSV row is the one a reader can look at, so it names the remedy."""
        root = write_run(
            write_csv(tmp_path, "A,rel,B,sources/ghost.md,confirmed,0.90,\n"),
            "sources/ghost.md",
        )
        assert eject_visible_refs(root) == {"sources/ghost.md": EJECT_CSV_ROW}

    def test_ref_outside_the_source_roots_is_by_name_only(self, tmp_path):
        """eject's orphan SCAN only matches refs under the two source roots — the
        rule that keeps a malformed citation from being ejected by a command nobody
        aimed. Naming it works, and that is what the hint must say: sending the
        reader to `--orphans` here, or to a manual inspection, are both false."""
        root = write_csv(tmp_path, "A,rel,B,ghost.md,confirmed,0.90,\n")
        assert eject_visible_refs(root) == {"ghost.md": EJECT_BY_NAME}

    def test_leading_whitespace_is_its_own_class(self, tmp_path):
        """cmd_eject does not strip the CSV value, so ' sources/x.md' is not a row
        its candidates.csv matcher retires — and because the table DOES hold the
        fact, no tombstone is written for it either. Filing this under run-only made
        the report promise a tombstone the command measurably does not write (it
        holds the run rows back and exits 1 instead), so it is a class of its own."""
        root = write_run(
            write_csv(tmp_path, "A,rel,B, sources/ghost.md,confirmed,0.90,\n"),
            "sources/ghost.md",
        )
        assert eject_visible_refs(root) == {
            " sources/ghost.md": EJECT_BY_NAME,
            "sources/ghost.md": EJECT_BLOCKED,
        }

    def test_a_trailing_pad_is_a_row_the_scan_retires(self, tmp_path):
        """Whitespace is not what blocks — the PREFIX TEST is, and only a LEADING pad
        breaks it. eject keeps the raw value in its cited set, so 'sources/x.md  '
        still starts with 'sources/': the orphan scan matches that ref in its own
        right and `match_row` retires the row. Measured `1 run row(s) stripped, 1
        candidate row(s) superseded` at rc 0 — an ordinary cleanup — while this
        report was calling it blocked and sending the reader to hand-edit the table
        for nothing."""
        root = write_run(
            write_csv(tmp_path, "A,rel,B,sources/ghost.md ,confirmed,0.90,\n"),
            "sources/ghost.md",
        )
        assert eject_visible_refs(root)["sources/ghost.md"] == EJECT_CSV_ROW

    def test_one_blocked_row_decides_a_ref_that_also_has_a_retiring_one(self, tmp_path):
        """Some run rows ARE held back on such a ref, so the blocked hint — exit 1,
        fix the whitespace — is the one that describes the run the user gets."""
        root = write_run(
            write_csv(
                tmp_path,
                "A,rel,B,sources/ghost.md ,confirmed,0.90,\n",
                "C,rel,D, sources/ghost.md,confirmed,0.90,\n",
            ),
            "sources/ghost.md",
        )
        assert eject_visible_refs(root)["sources/ghost.md"] == EJECT_BLOCKED

    def test_empty_kb_is_empty(self, tmp_path):
        assert eject_visible_refs(write_csv(tmp_path)) == {}

    def test_a_kb_with_no_candidates_csv_reads_the_run_files(self, tmp_path):
        """The commonest shape of the #559 state: merge dropped every row, so there
        is a KB whose table says nothing and whose run files hold the facts."""
        root = write_run(tmp_path, "sources/ghost.md")
        assert eject_visible_refs(root) == {"sources/ghost.md": EJECT_RUN_ONLY}


def stderr_lines(capsys):
    return [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]


class TestRemedyBranches:
    def test_a_run_only_ref_is_promised_the_tombstone_it_gets(self, capsys):
        """The remedy has to describe what the command LEAVES, not just that it
        works: the row a reader finds in candidates.csv afterwards is one this
        cleanup created, and reading it as a survivor would misdate the decision."""
        report_run_orphans([("sources/ghost.md", 3)], {"sources/ghost.md": EJECT_RUN_ONLY})
        lines = stderr_lines(capsys)
        assert lines[0] == (
            "  RUN ROWS cite a missing source (dropped at merge, 3 row(s)): sources/ghost.md"
        )
        assert lines[1] == (
            "  run rows cite 1 missing source(s) (3 row(s) total) whose only copy is in "
            "runs/*.json; `factlog eject --orphans` retires them, writing a `superseded` "
            "tombstone into candidates.csv first"
        )

    def test_ref_still_in_candidates_is_pointed_at_eject(self, capsys):
        report_run_orphans([("sources/ghost.md", 3)], {"sources/ghost.md": EJECT_CSV_ROW})
        lines = stderr_lines(capsys)
        assert lines[0] == (
            "  RUN ROWS cite a missing source (3 row(s); candidates.csv still carries "
            "rows for it): sources/ghost.md"
        )
        assert lines[1] == (
            "  run rows cite 1 missing source(s) (3 row(s) total) that candidates.csv "
            "still carries; retire them with `factlog eject --orphans`"
        )
        assert "whose only copy is in runs" not in "\n".join(lines)

    def test_a_ref_outside_the_source_roots_is_told_to_name_it(self, capsys):
        """`--orphans` will not auto-select it, and no amount of inspecting
        runs/*.json removes the rows — measured, `factlog eject <ref>` does."""
        report_run_orphans([("ghosty.md", 2)], {"ghosty.md": EJECT_BY_NAME})
        lines = stderr_lines(capsys)
        assert lines[0] == (
            "  RUN ROWS cite a missing source (dropped at merge, 2 row(s); outside the "
            "source roots): ghosty.md"
        )
        assert lines[1] == (
            "  run rows cite 1 missing source(s) (2 row(s) total) that --orphans will not "
            "auto-select (the ref is outside sources/ and runs/sources/); name each one: "
            "`factlog eject <ref>` — which strips those rows with NO tombstone, since "
            "candidates.csv cannot hold such a source, and exits 1 to say the fact is gone"
        )

    def test_a_blocked_ref_is_sent_to_the_whitespace_not_to_the_command(self, capsys):
        """The run-only hint asserts a tombstone. On this KB the command writes none
        and holds the rows back, so the class needs its own sentence — and the fix is
        in candidates.csv, not in another flag."""
        report_run_orphans([("sources/ghost.md", 2)], {"sources/ghost.md": EJECT_BLOCKED})
        lines = stderr_lines(capsys)
        assert lines[0] == (
            "  RUN ROWS cite a missing source (2 row(s); candidates.csv holds it under a "
            "whitespace-differing source): sources/ghost.md"
        )
        assert lines[1] == (
            "  run rows cite 1 missing source(s) (2 row(s) total) that candidates.csv holds "
            "under a `source` differing only by whitespace; `factlog eject --orphans` LEAVES "
            "those run rows in place (exit 1) rather than delete what merge rebuilds from — "
            "fix the whitespace in candidates.csv, then re-run it"
        )
        assert "writing a `superseded` tombstone" not in "\n".join(lines)

    def test_an_unknown_ref_falls_to_the_route_that_always_works(self, capsys):
        """Naming a ref holds whatever the two stores say, so an unclassified ref is
        never sent to a command that would silently do nothing for it."""
        report_run_orphans([("sources/mystery.md", 1)], {})
        assert "name each one" in "\n".join(stderr_lines(capsys))

    def test_a_mixed_kb_gets_all_three_hints_each_counting_only_its_own(self, capsys):
        """One ghost of each class in one KB — the state that rules out a single
        verdict for the whole report. Each hint's counts cover its own class only."""
        report_run_orphans(
            [("blind.md", 7), ("sources/ghost.md", 2), ("sources/kept.md", 5)],
            {"sources/kept.md": EJECT_CSV_ROW,
             "sources/ghost.md": EJECT_RUN_ONLY,
             "blind.md": EJECT_BY_NAME},
        )
        err = "\n".join(stderr_lines(capsys))
        assert (
            "  run rows cite 1 missing source(s) (2 row(s) total) whose only copy is in "
            "runs/*.json; `factlog eject --orphans` retires them, writing a `superseded` "
            "tombstone into candidates.csv first" in err
        )
        assert (
            "  run rows cite 1 missing source(s) (5 row(s) total) that candidates.csv "
            "still carries; retire them with `factlog eject --orphans`" in err
        )
        assert (
            "  run rows cite 1 missing source(s) (7 row(s) total) that --orphans will not "
            "auto-select" in err
        )
        assert "(dropped at merge, 2 row(s)): sources/ghost.md" in err
        assert "(dropped at merge, 7 row(s); outside the source roots): blind.md" in err
        assert "(5 row(s); candidates.csv still carries rows for it): sources/kept.md" in err

    def test_nothing_to_report_prints_nothing(self, capsys):
        """The 0-case contract: no line at all, not a line reading '0 missing'."""
        report_run_orphans([], {"sources/ghost.md": EJECT_CSV_ROW})
        assert stderr_lines(capsys) == []

    def test_one_class_alone_never_prints_another_at_zero(self, capsys):
        """Each `if` guards the state where only the OTHER classes exist — the
        ordinary state of a KB. Break one and the summary of an empty class is
        printed at zero, which for `kept` is the false remedy this module removed:
        "retire them with eject --orphans" on a KB where it retires nothing."""
        report_run_orphans([("sources/ghost.md", 1)], {"sources/ghost.md": EJECT_RUN_ONLY})
        err = "\n".join(stderr_lines(capsys))
        assert "candidates.csv still carries" not in err
        assert "will not auto-select" not in err
        report_run_orphans([("sources/kept.md", 1)], {"sources/kept.md": EJECT_CSV_ROW})
        err = "\n".join(stderr_lines(capsys))
        assert "whose only copy is in runs" not in err
        assert "will not auto-select" not in err
        report_run_orphans([("ghosty.md", 1)], {"ghosty.md": EJECT_BY_NAME})
        err = "\n".join(stderr_lines(capsys))
        assert "whose only copy is in runs" not in err
        assert "candidates.csv still carries" not in err


class TestDeterministicOrder:
    """Two ghosts is where order starts to exist, and the docstring promises it is
    by path. Nothing enforced that: the two `sorted()` calls behind this could both
    be deleted with every other test still green, leaving the stderr block of a
    multi-ghost KB in whatever order the filesystem yielded — so two runs of the
    same report on the same KB could differ, and a diff of them would show noise
    rather than a change.
    """

    def build(self, tmp_path, refs_per_file):
        (tmp_path / "runs").mkdir()
        for name, refs in refs_per_file.items():
            rows = [
                {
                    "subject": "갑봇",
                    "relation": "통합",
                    "object": f"을서비스{i}",
                    "source": ref,
                    "status": "candidate",
                    "confidence": "0.9",
                    "note": "",
                }
                for i, ref in enumerate(refs)
            ]
            (tmp_path / "runs" / name).write_text(
                json.dumps(rows, ensure_ascii=False), encoding="utf-8"
            )
        return tmp_path

    def test_run_orphans_come_back_sorted_by_path(self, tmp_path):
        """File order and within-file row order both run against the answer, so a
        version that preserved either would be caught."""
        self.build(
            tmp_path,
            {
                "z-second.json": ["sources/m.md", "sources/a.md"],
                "a-first.json": ["sources/z.md"],
            },
        )
        assert run_orphan_sources(tmp_path) == [
            ("sources/a.md", 1),
            ("sources/m.md", 1),
            ("sources/z.md", 1),
        ]

    def test_the_printed_block_follows_that_order(self, capsys):
        report_run_orphans(
            [("sources/a.md", 1), ("sources/m.md", 2)],
            {"sources/a.md": EJECT_RUN_ONLY, "sources/m.md": EJECT_RUN_ONLY},
        )
        lines = stderr_lines(capsys)
        assert "sources/a.md" in lines[0]
        assert "sources/m.md" in lines[1]


class TestUnreadableRunReport:
    def test_each_skipped_file_is_named(self, capsys):
        report_unreadable_runs(["bad.json", "worse.json"])
        lines = stderr_lines(capsys)
        assert lines == [
            "  skipped unreadable runs/bad.json — its rows are NOT in the counts above "
            "(merge cannot read it either)",
            "  skipped unreadable runs/worse.json — its rows are NOT in the counts above "
            "(merge cannot read it either)",
        ]

    def test_nothing_skipped_prints_nothing(self, capsys):
        report_unreadable_runs([])
        assert stderr_lines(capsys) == []
