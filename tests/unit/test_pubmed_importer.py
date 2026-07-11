# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the PubMed importer (#166).

Drives :func:`import_outcome` over the four classified buckets a
:class:`PubMedFetchOutcome` carries — present, merged, deleted, unparseable —
plus the ``invalid`` PMIDs rejected before any request. No network: outcomes are
constructed directly. Pins that a deleted/unparseable/invalid PMID is a per-id
``error`` (never a batch crash), that a merged lineage still writes with its
redirect named, and that ``--dry-run`` writes nothing.
"""
from __future__ import annotations

from factlog.integrations.pubmed.importer import import_outcome
from factlog.integrations.pubmed.work_parser import (
    MergedRecord,
    ParsedPubMedWork,
    PresentRecord,
    PubMedFetchOutcome,
    UnparseableRecord,
)


def _pm(pmid, title="A paper", doi=None) -> ParsedPubMedWork:
    return ParsedPubMedWork(
        pmid=pmid, title=title, authors=("Ann Author",), journal="J", year=2020,
        doi=doi, abstract="An abstract.",
    )


def _present(*works) -> PubMedFetchOutcome:
    return PubMedFetchOutcome(present=tuple(PresentRecord(w.pmid, w) for w in works))


def _sources(kb):
    return sorted(p.name for p in (kb / "sources").glob("*.md"))


class TestPresentRecordsWrite:
    def test_a_present_record_is_imported(self, tmp_path):
        (tmp_path / "sources").mkdir()
        report = import_outcome(_present(_pm("111")), target=tmp_path, imported_at="t")
        assert report.imported == 1
        assert report.outcomes[0].status == "imported"
        assert report.outcomes[0].key == "111"
        assert len(_sources(tmp_path)) == 1

    def test_records_are_written_in_pmid_order(self, tmp_path):
        (tmp_path / "sources").mkdir()
        report = import_outcome(
            _present(_pm("333", "C"), _pm("111", "A"), _pm("222", "B")),
            target=tmp_path, imported_at="t")
        assert [o.key for o in report.outcomes] == ["111", "222", "333"]


class TestMergedLineageWritesWithRedirect:
    def test_a_single_request_merge_writes_and_names_the_redirect(self, tmp_path):
        (tmp_path / "sources").mkdir()
        outcome = PubMedFetchOutcome(
            merged=(MergedRecord(requested_pmid="111", returned_pmid="222", work=_pm("222")),))
        report = import_outcome(outcome, target=tmp_path, imported_at="t")
        assert report.imported == 1
        out = report.outcomes[0]
        assert out.key == "222"  # keyed on the PMID that actually arrived
        assert "requested PMID 111" in out.reason
        assert "222" in out.reason


class TestDeletedAndUnparseableAreErrors:
    def test_a_deleted_pmid_is_a_per_id_error(self, tmp_path):
        (tmp_path / "sources").mkdir()
        report = import_outcome(
            PubMedFetchOutcome(deleted=("999",)), target=tmp_path, imported_at="t")
        assert report.errors == 1
        assert report.outcomes[0].status == "error"
        assert report.outcomes[0].key == "999"
        assert "deleted" in report.outcomes[0].reason

    def test_an_unparseable_record_is_a_per_id_error(self, tmp_path):
        (tmp_path / "sources").mkdir()
        outcome = PubMedFetchOutcome(
            unparseable=(UnparseableRecord(index=2, reason="record has no PMID", xml="<x/>"),))
        report = import_outcome(outcome, target=tmp_path, imported_at="t")
        assert report.errors == 1
        assert "no PMID" in report.outcomes[0].reason

    def test_a_good_record_survives_a_sibling_deletion_and_unparseable(self, tmp_path):
        (tmp_path / "sources").mkdir()
        outcome = PubMedFetchOutcome(
            present=(PresentRecord("111", _pm("111")),),
            deleted=("999",),
            unparseable=(UnparseableRecord(index=1, reason="no PMID", xml="<x/>"),))
        report = import_outcome(outcome, target=tmp_path, imported_at="t")
        assert report.imported == 1 and report.errors == 2
        assert len(_sources(tmp_path)) == 1


class TestInvalidPmids:
    def test_an_invalid_pmid_is_a_per_id_error_and_does_not_reach_the_wire(self, tmp_path):
        (tmp_path / "sources").mkdir()
        report = import_outcome(
            None, invalid=[("0", "invalid PMID '0'")], target=tmp_path, imported_at="t")
        assert report.errors == 1
        assert report.outcomes[0].key == "0"
        assert report.outcomes[0].status == "error"

    def test_valid_and_invalid_coexist(self, tmp_path):
        (tmp_path / "sources").mkdir()
        report = import_outcome(
            _present(_pm("111")), invalid=[("0", "invalid")], target=tmp_path, imported_at="t")
        assert report.imported == 1 and report.errors == 1
        # Error outcomes sort after record outcomes.
        assert report.outcomes[0].status == "imported"
        assert report.outcomes[-1].key == "0"


class TestDryRunWritesNothing:
    def test_plan_creates_no_files(self, tmp_path):
        report = import_outcome(_present(_pm("111")), target=tmp_path, imported_at="t", dry_run=True)
        assert report.imported == 1
        assert not (tmp_path / "source-provenance").exists()
        assert not (tmp_path / "sources").exists() or not _sources(tmp_path)
