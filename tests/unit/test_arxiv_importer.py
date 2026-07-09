# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the arXiv importer (#60, spec §11 Step 3)."""
from __future__ import annotations

from datetime import date

from factlog.integrations.arxiv.id_normalizer import ArxivId
from factlog.integrations.arxiv.importer import ImportReport, import_works
from factlog.integrations.arxiv.work_parser import ParsedArxivWork


def _work(arxiv_id="1706.03762", version=5, title="A paper", **over) -> ParsedArxivWork:
    base = dict(
        arxiv_id=arxiv_id,
        version=version,
        title=title,
        authors=("Ann Author",),
        abstract="An abstract.",
        primary_category="cs.CL",
        categories=("cs.CL",),
        submitted=date(2017, 6, 12),
        last_updated=date(2017, 6, 12),
        abs_url=f"https://arxiv.org/abs/{arxiv_id}v{version}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v{version}",
    )
    return ParsedArxivWork(**{**base, **over})


def _kb(tmp_path):
    (tmp_path / "sources").mkdir()
    return tmp_path


class TestImportWorks:
    def test_writes_each_work(self, tmp_path):
        kb = _kb(tmp_path)
        report = import_works(
            [_work("1706.03762"), _work("1810.04805", title="BERT")], target=kb)
        assert report.imported == 2
        assert report.skipped == report.errors == 0
        assert len(list((kb / "sources").glob("*.md"))) == 2

    def test_work_outcome_key_is_the_versioned_id(self, tmp_path):
        report = import_works([_work("1706.03762", version=5)], target=_kb(tmp_path))
        assert report.outcomes[0].key == "1706.03762v5"

    def test_the_highest_version_in_a_batch_wins_the_identity_slot(self, tmp_path):
        # Identity is the base id (P3), so only one version of a paper can land.
        # Writing the lowest would hand a user who asked for v2 the text of v1
        # while reporting success. Order the batch lowest-first to prove the
        # write order, not the argument order, decides.
        kb = _kb(tmp_path)
        report = import_works(
            [_work("1706.03762", version=1, title="draft"),
             _work("1706.03762", version=2, title="revised")],
            target=kb,
        )
        assert report.imported == 1
        assert report.skipped == 1
        written = next((kb / "sources").glob("*.md")).read_text()
        assert 'arxiv_version: 2' in written
        assert "revised" in written

    def test_a_version_skipped_for_a_newer_one_says_so(self, tmp_path):
        report = import_works(
            [_work("1706.03762", version=1), _work("1706.03762", version=2)],
            target=_kb(tmp_path),
        )
        skipped = next(o for o in report.outcomes if o.status == "skipped")
        assert skipped.key == "1706.03762v1"
        assert skipped.reason == "superseded by 1706.03762v2 in this batch"

    def test_a_paper_already_in_the_kb_is_not_reported_as_superseded(self, tmp_path):
        # The "superseded" wording is only true within one batch. A plain
        # re-import must keep the identity-match reason.
        kb = _kb(tmp_path)
        import_works([_work("1706.03762", version=2)], target=kb)
        report = import_works([_work("1706.03762", version=2)], target=kb)
        assert report.outcomes[0].reason.startswith("already imported")

    def test_import_order_is_by_id_then_version(self, tmp_path):
        # Deterministic order -> reproducible collision suffixes and porcelain.
        kb = _kb(tmp_path)
        report = import_works(
            [_work("1810.04805", version=2), _work("1706.03762", version=9),
             _work("1706.03762", version=1)],
            target=kb,
        )
        assert [o.key for o in report.outcomes] == [
            "1706.03762v1", "1706.03762v9", "1810.04805v2"]

    def test_dry_run_creates_no_files_but_names_them(self, tmp_path):
        kb = _kb(tmp_path)
        report = import_works([_work()], target=kb, dry_run=True)
        assert report.imported == 1
        assert report.outcomes[0].path.name.endswith(".md")
        assert list((kb / "sources").glob("*.md")) == []

    def test_reimport_is_skipped_with_a_reason(self, tmp_path):
        kb = _kb(tmp_path)
        import_works([_work()], target=kb)
        report = import_works([_work()], target=kb)
        assert report.skipped == 1
        assert "arxiv_id match" in report.outcomes[0].reason

    def test_reimport_of_a_later_version_is_still_a_no_op(self, tmp_path):
        # P3/P4: identity is the base id, so a newer version skips and the original
        # file is never rewritten.
        kb = _kb(tmp_path)
        import_works([_work(version=5)], target=kb)
        report = import_works([_work(version=6, title="Retitled")], target=kb)
        assert report.skipped == 1
        assert len(list((kb / "sources").glob("*.md"))) == 1

    def test_imported_at_is_stamped(self, tmp_path):
        kb = _kb(tmp_path)
        report = import_works([_work()], target=kb, imported_at="2026-07-09T00:00:00Z")
        assert 'imported_at: "2026-07-09T00:00:00Z"' in report.outcomes[0].path.read_text()

    def test_untitled_work_is_labelled(self, tmp_path):
        report = import_works([_work(title="")], target=_kb(tmp_path))
        assert report.outcomes[0].title == "(untitled)"


class TestMissingAndInvalid:
    def test_missing_id_becomes_a_per_id_error(self, tmp_path):
        kb = _kb(tmp_path)
        report = import_works(
            [_work("1706.03762")], missing=[ArxivId("9999.99999")], target=kb)
        assert report.imported == 1 and report.errors == 1
        err = [o for o in report.outcomes if o.status == "error"][0]
        assert err.key == "9999.99999"
        assert "no entry returned by arXiv" in err.reason

    def test_pinned_missing_version_keeps_its_version_in_the_key(self, tmp_path):
        report = import_works(
            [], missing=[ArxivId("1706.03762", 99)], target=_kb(tmp_path))
        assert report.outcomes[0].key == "1706.03762v99"

    def test_invalid_id_becomes_a_per_id_error(self, tmp_path):
        kb = _kb(tmp_path)
        report = import_works(
            [_work()], invalid=[("notanid", "invalid arXiv id 'notanid'")], target=kb)
        assert report.imported == 1 and report.errors == 1
        err = [o for o in report.outcomes if o.status == "error"][0]
        assert err.key == "notanid"
        assert "invalid arXiv id" in err.reason

    def test_a_bad_id_does_not_stop_the_good_ones(self, tmp_path):
        kb = _kb(tmp_path)
        report = import_works(
            [_work("1706.03762")],
            missing=[ArxivId("9999.99999")],
            invalid=[("notanid", "bad")],
            target=kb,
        )
        assert report.imported == 1 and report.errors == 2
        assert len(list((kb / "sources").glob("*.md"))) == 1

    def test_work_outcomes_precede_sorted_error_outcomes(self, tmp_path):
        kb = _kb(tmp_path)
        report = import_works(
            [_work("1706.03762")],
            missing=[ArxivId("2000.00002")],
            invalid=[("2000.00001-bad", "bad")],
            target=kb,
        )
        keys = [o.key for o in report.outcomes]
        assert keys[0] == "1706.03762v5"
        # Errors follow, sorted by key.
        assert keys[1:] == sorted(keys[1:])


class TestCrossSourceDuplicate:
    def test_two_arxiv_deposits_sharing_a_doi_are_skipped_not_merged(self, tmp_path):
        # Both records are arXiv's own. A shared DOI makes the second a plain
        # duplicate, not another *database's* view of the paper, so it must not be
        # folded into the first arXiv record's ledger. Merging is for a record this
        # writer did not write.
        kb = _kb(tmp_path)
        report = import_works(
            [_work("1706.03762", doi="10.1/x"),
             _work("1810.04805", title="dup", doi="10.1/x")],
            target=kb,
        )
        assert report.imported == 1 and report.skipped == 1
        assert report.merged == 0
        skipped = [o for o in report.outcomes if o.status == "skipped"][0]
        assert "duplicate DOI" in skipped.reason
        assert not (tmp_path / "source-provenance").exists()

    def test_a_doi_shared_with_another_database_is_merged(self, tmp_path):
        # The same join key, but the existing file was written by OpenAlex. Now it
        # is another database's view of the paper, and the deposit is recorded.
        kb = _kb(tmp_path)
        (kb / "sources" / "openalex.md").write_text(
            '---\nopenalex_id: "W1"\ndoi: "10.1/x"\nimported_from: openalex\n---\n',
            encoding="utf-8",
        )
        report = import_works([_work("1706.03762", doi="10.1/x")], target=kb)
        assert report.merged == 1 and report.imported == 0
        assert (kb / "source-provenance" / "openalex.json").is_file()


class TestReport:
    def test_empty_input_reports_nothing(self, tmp_path):
        report = import_works([], target=_kb(tmp_path))
        assert report.outcomes == []
        assert report.imported == report.skipped == report.errors == 0

    def test_counts_are_derived_from_outcomes(self):
        from factlog.integrations.arxiv.importer import WorkOutcome

        report = ImportReport([
            WorkOutcome("imported", "a", "A"),
            WorkOutcome("skipped", "b", "B"),
            WorkOutcome("merged", "d", "D"),
            WorkOutcome("error", "c", ""),
        ])
        assert (report.imported, report.skipped, report.merged, report.errors) == (1, 1, 1, 1)
