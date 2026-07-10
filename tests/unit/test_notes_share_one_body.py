# SPDX-License-Identifier: Apache-2.0
"""The two branches of each note must share one body (#110).

`withdrawal_note` and `retraction_note` each answer for a ledger-backed paper and for a
front-matter-only one. The front-matter answer is the ledger answer plus a sentence about
the missing ledger.

When both branches restated the shared prose, only the ledger string was pinned
byte-for-byte by a test. An edit to the ledger note therefore broke exactly one test; a
maintainer would update that literal and the front-matter note would silently keep the
old prose. Reproduced before the fix: appending a sentence to the ledger branch and
updating its literal left the suite green with the two notes disagreeing.

The body is now built once and the front-matter branch appends to it, so the prefix
relation holds by construction. These tests assert it anyway — a future refactor could
reintroduce the duplication, and this is the assertion that would catch it.
"""
from __future__ import annotations

import factlog.integrations.arxiv.check_versions as cv
import factlog.integrations.openalex.refresh as rf


def _arxiv(recorded_from: str, *, recorded: str | None = None) -> cv.VersionCheck:
    return cv.VersionCheck(
        arxiv_id="0704.0001",
        status=cv.STATUS_UNCHANGED,
        recorded_version=7,
        current_version=7,
        withdrawn_by="author",
        recorded_withdrawn_by=recorded,
        newly_withdrawn=True,
        recorded_from=recorded_from,
        # A pre-#82 paper with no sidecar: the branch whose remedy is the backfill. Without
        # this the default SIDECAR_READABLE would route to the arxiv-import branch (#132).
        sidecar_state=cv.SIDECAR_ABSENT,
    )


def _openalex(recorded_from: str) -> rf.RefreshCheck:
    return rf.RefreshCheck(
        openalex_id="W1",
        status=rf.STATUS_UNCHANGED,
        current_is_retracted=True,
        newly_retracted=True,
        recorded_from=recorded_from,
    )


class TestWithdrawalNoteSharesOneBody:
    def test_the_ledger_note_is_a_prefix_of_the_front_matter_note(self):
        ledger = cv.withdrawal_note(_arxiv("ledger"))
        front_matter = cv.withdrawal_note(_arxiv("front-matter"))
        # Not merely "both mention withdrawal" — the ledger text must survive verbatim.
        assert front_matter.startswith(ledger.replace("the ledger", "the front matter"))

    def test_only_the_front_matter_note_carries_the_backfill_pointer(self):
        # `_arxiv` carries `arxiv_version` (`recorded_version=7`), so the note names the
        # command that builds the ledger (#114), never the closed issue #105 that tracked it.
        assert "arxiv-backfill-provenance" in cv.withdrawal_note(_arxiv("front-matter"))
        assert "#105" not in cv.withdrawal_note(_arxiv("front-matter"))
        assert "arxiv-backfill-provenance" not in cv.withdrawal_note(_arxiv("ledger"))

    def test_the_suffix_is_the_only_difference(self):
        ledger = cv.withdrawal_note(_arxiv("ledger"))
        front_matter = cv.withdrawal_note(_arxiv("front-matter"))
        suffix = front_matter[len(ledger.replace("the ledger", "the front matter")) :]
        assert "no provenance ledger" in suffix
        assert "cannot be acknowledged" in suffix

    def test_the_prefix_holds_when_a_value_was_recorded(self):
        # The `provenance` clause takes its other arm here; the relation must still hold.
        ledger = cv.withdrawal_note(_arxiv("ledger", recorded="admin"))
        front_matter = cv.withdrawal_note(_arxiv("front-matter", recorded="admin"))
        assert front_matter.startswith(ledger.replace("the ledger", "the front matter"))


class TestRetractionNoteSharesOneBody:
    def test_the_ledger_note_is_a_prefix_of_the_front_matter_note(self):
        ledger = rf.retraction_note(_openalex("ledger"))
        front_matter = rf.retraction_note(_openalex("front-matter"))
        assert front_matter.startswith(ledger.replace("the ledger", "the front matter"))

    def test_only_the_front_matter_note_carries_the_backfill_pointer(self):
        # It names the command that builds a ledger (#115), not the issue that tracked it.
        assert "openalex-backfill-provenance" in rf.retraction_note(_openalex("front-matter"))
        assert "openalex-backfill-provenance" not in rf.retraction_note(_openalex("ledger"))

    def test_the_suffix_is_the_only_difference(self):
        ledger = rf.retraction_note(_openalex("ledger"))
        front_matter = rf.retraction_note(_openalex("front-matter"))
        suffix = front_matter[len(ledger.replace("the ledger", "the front matter")) :]
        assert "no provenance ledger" in suffix
        assert "cannot be acknowledged" in suffix


class TestVocabularyStaysSplit:
    """Withdrawal is arXiv's word, retraction is OpenAlex's. One shared body per note,
    never one shared body across notes."""

    def test_no_retraction_in_the_arxiv_notes_beyond_the_disclaimer(self):
        for note in (cv.withdrawal_note(_arxiv("ledger")),
                     cv.withdrawal_note(_arxiv("front-matter"))):
            assert "retracted" not in note.lower()
            assert "Withdrawal is not retraction" in note

    def test_no_withdrawal_in_the_openalex_notes(self):
        for note in (rf.retraction_note(_openalex("ledger")),
                     rf.retraction_note(_openalex("front-matter"))):
            assert "withdraw" not in note.lower()
