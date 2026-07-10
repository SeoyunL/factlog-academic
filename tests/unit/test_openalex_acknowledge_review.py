# SPDX-License-Identifier: Apache-2.0
"""Regressions for the review findings on `openalex-acknowledge-retraction` (#101).

Each test here pins a defect that was reproduced before it was fixed. They are separate
from the command's own suite because they guard *seams* — a predicate that existed in two
copies, and a porcelain column that a free-text field could displace.
"""
from __future__ import annotations

from pathlib import Path

import factlog.integrations.arxiv.check_versions as cv
import factlog.integrations.openalex.refresh as rf


class TestProvenanceOfIsNotDuplicated:
    """The CLI once re-derived "is this front-matter-only?" instead of asking the module
    that builds `sources`. The two copies disagreed on an empty tuple: the module said
    "ledger", the copy said "front-matter". A `LedgerEntry` with no sources would then be
    reported as ledger-backed by the refresh and refused as front-matter-only by the
    acknowledge command — #107 item 1's unsilenceable warning, rebuilt.

    Duplicated predicates are how #64 and #98 happened. Both integrations expose one.
    """

    def test_empty_sources_is_not_front_matter(self):
        # The guard that the CLI's copy lacked. `all(...)` over an empty tuple is True.
        assert all(str(s).startswith("sources/") for s in ()) is True
        assert rf.provenance_of(()) == "ledger"
        assert cv.provenance_of(()) == "ledger"

    def test_both_integrations_agree_on_every_shape(self):
        shapes = [
            (),
            ("source-provenance/a.json",),
            ("sources/a.md",),
            ("sources/sub/a.md",),
            ("source-provenance/a.json", "sources/b.md"),
        ]
        for sources in shapes:
            assert rf.provenance_of(sources) == cv.provenance_of(sources), sources

    def test_a_sidecar_path_is_never_mistaken_for_a_source(self):
        # "source-provenance/..." must not match a "sources/" prefix test.
        assert rf.provenance_of(("source-provenance/a.json",)) == "ledger"

    def test_front_matter_only_is_recognised(self):
        assert rf.provenance_of(("sources/a.md",)) == "front-matter"

    def test_a_mixed_entry_is_ledger_backed(self):
        # `collect_ledger_entries` never mixes the two today, but if it ever did, the
        # presence of a real ledger is what decides whether a decision can be recorded.
        assert rf.provenance_of(("source-provenance/a.json", "sources/b.md")) == "ledger"


class TestPorcelainColumnsCannotShift:
    """`un_retracted` is appended after the free-text `reason`, which interpolates an
    exception string. An `OSError`'s message carries a path, and a path may contain a tab —
    which would silently move the last column for every parser keying on it.
    """

    def _row(self, reason: str) -> list[str]:
        check = rf.RefreshCheck(openalex_id="W1", status=rf.STATUS_ERROR, reason=reason)
        lines = rf.porcelain_lines(
            [check], [], rf.summarize([check], []), target=Path("/tmp")
        )
        row = next(line for line in lines if line.startswith("check\t"))
        return row.split("\t")

    def test_a_tab_in_reason_does_not_add_a_column(self):
        clean = self._row("corrupt provenance ledger: bad value")
        tabbed = self._row("corrupt provenance ledger: bad\tvalue")
        assert len(tabbed) == len(clean) == 9

    def test_the_un_retracted_flag_stays_last(self):
        assert self._row("corrupt: a\tb\nc")[-1] == "0"

    def test_newlines_do_not_split_the_row(self):
        check = rf.RefreshCheck(
            openalex_id="W1", status=rf.STATUS_ERROR, reason="corrupt: a\nb"
        )
        lines = rf.porcelain_lines(
            [check], [], rf.summarize([check], []), target=Path("/tmp")
        )
        assert sum(1 for line in lines if line.startswith("check\t")) == 1


class TestUnRetractionNoteDoesNotPrescribeItself:
    """The acknowledge command prints this note before confirming. Telling the operator to
    run the command they are already running is noise (#107 item 7).
    """

    def _check(self) -> rf.RefreshCheck:
        return rf.RefreshCheck(
            openalex_id="W1",
            status=rf.STATUS_UNCHANGED,
            recorded_is_retracted=True,
            current_is_retracted=False,
            un_retracted=True,
            recorded_from="ledger",
        )

    def test_the_report_still_prescribes_the_command(self):
        assert "openalex-acknowledge-retraction" in rf.un_retraction_note(self._check())

    def test_the_command_preview_does_not(self):
        note = rf.un_retraction_note(self._check(), prescribe=False)
        assert "openalex-acknowledge-retraction" not in note
        # The substance survives; only the prescription is dropped.
        assert "no longer flags" in note

    def test_neither_form_claims_a_re_import_error(self):
        # `is_retracted` is not an identifying field. arXiv's wording would be false here.
        for note in (
            rf.un_retraction_note(self._check()),
            rf.un_retraction_note(self._check(), prescribe=False),
        ):
            assert "re-import does not error" in note
            assert "withdraw" not in note.lower()
