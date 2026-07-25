# SPDX-License-Identifier: Apache-2.0
"""The remedy a run-orphan line prints must be one the named command performs (#558).

The first version of this report printed ONE hint for every run-cited missing
source: "`factlog eject --orphans` does not cover these". True for the blind spot
the issue is about — a `candidate` ghost row, which merge silently drops from
candidates.csv, taking it out of the cited set eject builds from that file.

False for a ghost row a human has ruled on. The #218 ratchet REFUSES the rebuild
that would delete such a row, so it stays in candidates.csv, eject sees it, and
one `factlog eject --orphans` retires the row and strips it from runs/*.json. The
hint sent the reader to hand-edit run files while the command they were told not
to use would have done it.

So the class is decided per ref, by what `eject --orphans` can actually act on —
`eject_visible_refs`, derived from cmd_eject's own rule. Note what that rule is
NOT: coverage's own `orphans` list, which counts engine statuses only. A
`needs_review` ghost is absent from `orphans` and still ejectable, so keying the
hint on `orphans` would have kept the same lie for that status.
"""
from __future__ import annotations

import unicodedata

import pytest
from source_coverage import eject_visible_refs, report_run_orphans, report_unreadable_runs

_HEADER = "subject,relation,object,source,status,confidence,note\n"


def write_csv(tmp_path, *rows):
    path = tmp_path / "candidates.csv"
    path.write_text(_HEADER + "".join(rows), encoding="utf-8")
    return path


class TestEjectVisibleRefs:
    @pytest.mark.parametrize("status", ["confirmed", "accepted", "needs_review", "candidate", "superseded"])
    def test_every_status_is_visible_to_eject(self, tmp_path, status):
        """cmd_eject reads the raw CSV and filters by nothing. A status filter here
        would re-introduce the false hint for whichever statuses it left out."""
        csv = write_csv(tmp_path, f"A,rel,B,sources/ghost.md,{status},0.90,\n")
        assert eject_visible_refs(csv) == {"sources/ghost.md"}

    def test_anchor_is_stripped(self, tmp_path):
        csv = write_csv(tmp_path, "A,rel,B,sources/ghost.md#sec,confirmed,0.90,\n")
        assert eject_visible_refs(csv) == {"sources/ghost.md"}

    def test_nfd_folds_onto_nfc(self, tmp_path):
        """eject NFC-folds its cited refs, so the run-orphan key (also NFC) matches."""
        nfc = "sources/방법론.md"
        csv = write_csv(tmp_path, f"A,rel,B,{unicodedata.normalize('NFD', nfc)},confirmed,0.90,\n")
        assert eject_visible_refs(csv) == {nfc}

    def test_runs_sources_prefix_is_visible(self, tmp_path):
        csv = write_csv(tmp_path, "A,rel,B,runs/sources/conv.md,confirmed,0.90,\n")
        assert eject_visible_refs(csv) == {"runs/sources/conv.md"}

    def test_ref_outside_the_source_roots_is_not_visible(self, tmp_path):
        """eject's orphan scan only matches refs under the two source roots, so a
        bare filename is one it will never retire — promising otherwise is the lie
        this function exists to prevent."""
        csv = write_csv(tmp_path, "A,rel,B,ghost.md,confirmed,0.90,\n")
        assert eject_visible_refs(csv) == set()

    def test_leading_whitespace_is_not_stripped(self, tmp_path):
        """cmd_eject does not strip the CSV value, so ' sources/x.md' does not start
        with 'sources/' for it and is not ejectable. `load_facts` DOES strip, which
        is why this reads the raw CSV — a stripped read would claim a cleanup that
        command would not perform."""
        csv = write_csv(tmp_path, "A,rel,B, sources/ghost.md,confirmed,0.90,\n")
        assert eject_visible_refs(csv) == set()

    def test_empty_csv_is_empty(self, tmp_path):
        assert eject_visible_refs(write_csv(tmp_path)) == set()


def stderr_lines(capsys):
    return [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]


class TestRemedyBranches:
    def test_ref_gone_from_candidates_gets_the_issue_559_hint(self, capsys):
        report_run_orphans([("sources/ghost.md", 3)], set())
        lines = stderr_lines(capsys)
        assert lines[0] == (
            "  RUN ROWS cite a missing source (dropped at merge, 3 row(s)): sources/ghost.md"
        )
        assert lines[1] == (
            "  run rows cite 1 missing source(s) (3 row(s) total); inspect runs/*.json — "
            "`factlog eject --orphans` does not cover these (see #559)"
        )

    def test_ref_still_in_candidates_is_pointed_at_eject(self, capsys):
        report_run_orphans([("sources/ghost.md", 3)], {"sources/ghost.md"})
        lines = stderr_lines(capsys)
        assert lines[0] == (
            "  RUN ROWS cite a missing source (3 row(s); candidates.csv still carries "
            "rows for it): sources/ghost.md"
        )
        assert lines[1] == (
            "  run rows cite 1 missing source(s) (3 row(s) total) that candidates.csv "
            "still carries; retire them with `factlog eject --orphans`"
        )
        # The claim it must NOT make about a ref that command DOES clean.
        assert "does not cover these" not in "\n".join(lines)

    def test_a_mixed_kb_gets_both_hints_each_counting_only_its_own(self, capsys):
        """One ghost of each class in one KB — the state that rules out a single
        verdict for the whole report. Each hint's counts cover its own class only."""
        report_run_orphans(
            [("sources/blind.md", 2), ("sources/kept.md", 5)],
            {"sources/kept.md"},
        )
        err = "\n".join(stderr_lines(capsys))
        assert (
            "  run rows cite 1 missing source(s) (2 row(s) total); inspect runs/*.json — "
            "`factlog eject --orphans` does not cover these (see #559)" in err
        )
        assert (
            "  run rows cite 1 missing source(s) (5 row(s) total) that candidates.csv "
            "still carries; retire them with `factlog eject --orphans`" in err
        )
        assert "(dropped at merge, 2 row(s)): sources/blind.md" in err
        assert "(5 row(s); candidates.csv still carries rows for it): sources/kept.md" in err

    def test_nothing_to_report_prints_nothing(self, capsys):
        """The 0-case contract: no line at all, not a line reading '0 missing'."""
        report_run_orphans([], {"sources/ghost.md"})
        assert stderr_lines(capsys) == []


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
