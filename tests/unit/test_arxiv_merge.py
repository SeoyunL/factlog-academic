# SPDX-License-Identifier: Apache-2.0
"""Merge an arXiv deposit into an existing original's provenance sidecar (#65).

Step 4b (#64) *detects* that a paper already in the KB via another database is
the same paper. Step 4c folds the arXiv view of it into that original's sidecar
(§7.3) as a ``merged`` outcome, instead of writing a second file or reporting a
bare skip. The original ``.md`` is never touched (P4); the sidecar is an
append-only audit ledger written with ``add_source`` (an import has no authority
to revise it, so a version divergence is a per-id error, not a silent update).

These tests pin every acceptance point: byte+mtime immutability of the original,
idempotence (one record after two merges), the version-divergence error and
batch isolation, ``--dry-run`` writing nothing, the ``merged`` label surviving
into both human and porcelain output, the Zotero/OpenAlex opt-out, and the
``datetime.date`` conversion that would otherwise crash at ``json.dumps``.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from factlog.integrations.arxiv.source_writer import ArxivSourceWriter
from factlog.integrations.arxiv.work_parser import ParsedArxivWork
from factlog.integrations.common.provenance import (
    is_sidecar,
    read_provenance,
    sidecar_path,
    write_provenance,
)
from factlog.integrations.openalex.source_writer import OpenAlexSourceWriter
from factlog.integrations.openalex.work_parser import ParsedWork
from factlog.integrations.zotero.source_writer import SourceWriter as ZoteroWriter


def _arxiv(arxiv_id="2311.09277", version=2, **over) -> ParsedArxivWork:
    base = dict(
        arxiv_id=arxiv_id,
        version=version,
        title="A Paper",
        authors=("Ada Lovelace",),
        abstract="An abstract.",
        primary_category="cs.CL",
        categories=("cs.CL", "cs.LG"),
        submitted=date(2023, 11, 15),
        last_updated=date(2023, 11, 20),
        doi=None,
        journal_ref="J. Foo 1 (2024) 1",
        comment="10 pages, 3 figures",
        withdrawn_by=None,
        abs_url=f"https://arxiv.org/abs/{arxiv_id}v{version}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v{version}",
    )
    return ParsedArxivWork(**{**base, **over})


def _openalex(openalex_id="W1", arxiv_id="2311.09277", doi=None, **over) -> ParsedWork:
    base = dict(
        openalex_id=openalex_id,
        title="A Paper",
        authors=("Ada Lovelace",),
        year=2023,
        journal="Journal of Foo",
        doi=doi,
        pmid=None,
        arxiv_id=arxiv_id,
        work_type="article",
    )
    return ParsedWork(**{**base, **over})


def _kb_with_openalex(tmp_path, **oa):
    """A KB whose sole source is an OpenAlex-primary record of the paper."""
    existing = OpenAlexSourceWriter().write(
        _openalex(**oa), tmp_path, imported_at="2026-01-01T00:00:00Z")
    assert existing.status == "imported"
    return tmp_path, existing.path


def _records(sidecar):
    return read_provenance(sidecar).records


def _arxiv_record(sidecar):
    return next(r for r in _records(sidecar) if r.type == "arxiv")


class TestMergeWritesTheSidecar:
    def test_merge_appends_an_arxiv_record_to_the_existing_sidecar(self, tmp_path):
        kb, existing = _kb_with_openalex(tmp_path)
        result = ArxivSourceWriter().write(_arxiv(), kb, imported_at="2026-02-02T00:00:00Z")
        assert result.status == "merged"
        assert result.path == existing  # points at the existing original

        sidecar = sidecar_path(existing)
        assert sidecar.exists()
        assert is_sidecar(sidecar)
        rec = _arxiv_record(sidecar)
        assert rec.id == "2311.09277"
        assert rec.imported_at == "2026-02-02T00:00:00Z"

    def test_sidecar_lives_outside_sources(self, tmp_path):
        kb, existing = _kb_with_openalex(tmp_path)
        ArxivSourceWriter().write(_arxiv(), kb, imported_at="t")
        # Never inside sources/, so no enumerator sees it as a source.
        assert not list((kb / "sources").glob("*.json"))
        assert (kb / "source-provenance").is_dir()

    def test_no_new_md_is_created(self, tmp_path):
        kb, _ = _kb_with_openalex(tmp_path)
        before = sorted(p.name for p in (kb / "sources").glob("*.md"))
        ArxivSourceWriter().write(_arxiv(), kb, imported_at="t")
        after = sorted(p.name for p in (kb / "sources").glob("*.md"))
        assert before == after  # exactly one .md, unchanged set


class TestOriginalIsImmutable:
    def test_original_md_bytes_and_mtime_ns_are_unchanged(self, tmp_path):
        kb, existing = _kb_with_openalex(tmp_path)
        before_bytes = existing.read_bytes()
        before_mtime = existing.stat().st_mtime_ns

        result = ArxivSourceWriter().write(_arxiv(), kb, imported_at="t")
        assert result.status == "merged"

        assert existing.read_bytes() == before_bytes
        # mtime_ns, not just bytes: a re-render+atomic-replace would bump it.
        assert existing.stat().st_mtime_ns == before_mtime


class TestIdempotence:
    def test_merging_twice_adds_exactly_one_record(self, tmp_path):
        kb, existing = _kb_with_openalex(tmp_path)
        sidecar = sidecar_path(existing)

        first = ArxivSourceWriter().write(_arxiv(), kb, imported_at="t")
        assert first.status == "merged"
        first_bytes = sidecar.read_bytes()

        # A second identical merge (fresh writer/index) is a byte-identical no-op.
        second = ArxivSourceWriter().write(_arxiv(), kb, imported_at="t")
        assert second.status == "merged"
        assert sidecar.read_bytes() == first_bytes
        assert sum(1 for r in _records(sidecar) if r.type == "arxiv") == 1

    def test_a_later_reimport_with_a_new_timestamp_is_an_idempotent_noop(self, tmp_path):
        # The CLI stamps a fresh imported_at every run. Idempotence must key on the
        # deposit, not the clock: a re-import of the same version is a no-op that
        # keeps the FIRST timestamp and leaves the ledger byte-identical (P3).
        kb, existing = _kb_with_openalex(tmp_path)
        sidecar = sidecar_path(existing)
        first = ArxivSourceWriter().write(_arxiv(), kb, imported_at="2026-01-01T00:00:00Z")
        assert first.status == "merged"
        first_bytes = sidecar.read_bytes()

        later = ArxivSourceWriter().write(_arxiv(), kb, imported_at="2026-09-09T00:00:00Z")
        assert later.status == "merged"
        assert sidecar.read_bytes() == first_bytes  # unchanged, first timestamp kept
        assert _arxiv_record(sidecar).imported_at == "2026-01-01T00:00:00Z"
        assert sum(1 for r in _records(sidecar) if r.type == "arxiv") == 1


class TestVersionDivergence:
    def test_a_newer_version_is_a_per_id_error_not_a_crash(self, tmp_path):
        kb, existing = _kb_with_openalex(tmp_path)
        sidecar = sidecar_path(existing)
        ArxivSourceWriter().write(_arxiv(version=2), kb, imported_at="t")
        before = sidecar.read_bytes()

        result = ArxivSourceWriter().write(_arxiv(version=3), kb, imported_at="t")
        assert result.status == "error"
        assert "arxiv-check-versions" in result.reason
        assert "v2" in result.reason  # names the recorded version
        # The ledger on disk is untouched.
        assert sidecar.read_bytes() == before
        assert _arxiv_record(sidecar).fields["version"] == 2

    def test_divergence_does_not_abort_the_rest_of_the_batch(self, tmp_path):
        from factlog.integrations.arxiv.importer import import_works

        kb, existing = _kb_with_openalex(tmp_path, openalex_id="W1", arxiv_id="2311.09277")
        # Seed the diverging paper's ledger at v2.
        ArxivSourceWriter().write(_arxiv(arxiv_id="2311.09277", version=2), kb, imported_at="t")

        # A batch: the diverged paper (now v3) plus a brand-new arXiv paper.
        report = import_works(
            [_arxiv(arxiv_id="2311.09277", version=3),
             _arxiv(arxiv_id="2005.13421", version=1, title="Fresh")],
            target=kb, imported_at="t",
        )
        statuses = {o.key: o.status for o in report.outcomes}
        assert statuses["2311.09277v3"] == "error"
        assert statuses["2005.13421v1"] == "imported"  # unaffected
        assert report.errors == 1 and report.imported == 1


class TestDryRun:
    def test_dry_run_predicts_merged_and_writes_nothing(self, tmp_path):
        kb, existing = _kb_with_openalex(tmp_path)
        sidecar = sidecar_path(existing)

        result = ArxivSourceWriter().plan(_arxiv(), kb)
        assert result.status == "merged"
        assert result.path == existing
        # Neither the sidecar nor any new .md was written.
        assert not sidecar.exists()
        assert not (kb / "source-provenance").exists()
        assert len(list((kb / "sources").glob("*.md"))) == 1

    def test_importer_dry_run_reports_merged_writes_no_sidecar(self, tmp_path):
        from factlog.integrations.arxiv.importer import import_works

        kb, existing = _kb_with_openalex(tmp_path)
        report = import_works([_arxiv()], target=kb, imported_at="t", dry_run=True)
        assert report.merged == 1
        assert report.outcomes[0].status == "merged"
        assert report.outcomes[0].path == existing
        assert not sidecar_path(existing).exists()


class TestRecordFields:
    """H3: the exact field set, and the date conversion that must precede write."""

    def test_record_carries_the_h3_fields(self, tmp_path):
        kb, existing = _kb_with_openalex(tmp_path)
        ArxivSourceWriter().write(
            _arxiv(withdrawn_by="admin"), kb, imported_at="t")
        rec = _arxiv_record(sidecar_path(existing)).to_dict()
        assert rec["version"] == 2
        assert rec["submitted"] == "2023-11-15"
        assert rec["last_updated"] == "2023-11-20"
        assert rec["comment"] == "10 pages, 3 figures"
        assert rec["primary_category"] == "cs.CL"
        assert rec["withdrawn_by"] == "admin"

    def test_record_excludes_urls_doi_journal_and_preprint(self, tmp_path):
        kb, existing = _kb_with_openalex(tmp_path)
        ArxivSourceWriter().write(
            _arxiv(doi="10.1/x"), kb, imported_at="t")
        rec = _arxiv_record(sidecar_path(existing)).to_dict()
        for excluded in ("abs_url", "pdf_url", "doi", "journal_ref", "preprint"):
            assert excluded not in rec

    def test_withdrawn_by_is_absent_when_not_withdrawn(self, tmp_path):
        kb, existing = _kb_with_openalex(tmp_path)
        ArxivSourceWriter().write(_arxiv(withdrawn_by=None), kb, imported_at="t")
        assert "withdrawn_by" not in _arxiv_record(sidecar_path(existing)).to_dict()

    def test_none_optional_fields_are_dropped(self, tmp_path):
        kb, existing = _kb_with_openalex(tmp_path)
        ArxivSourceWriter().write(
            _arxiv(comment=None, submitted=None, last_updated=None), kb, imported_at="t")
        rec = _arxiv_record(sidecar_path(existing)).to_dict()
        assert "comment" not in rec
        assert "submitted" not in rec
        assert "last_updated" not in rec

    def test_the_builder_converts_dates_so_write_never_sees_a_date(self, tmp_path):
        # Prove it structurally: the record the builder makes serializes cleanly,
        # and no field value is a date instance.
        writer = ArxivSourceWriter()
        record = writer._provenance_record(_arxiv(), imported_at="t")
        for value in record.fields.values():
            assert not isinstance(value, date)
        # And it round-trips through json without TypeError.
        json.dumps(record.to_dict())

    def test_a_raw_date_in_a_record_would_crash_write_provenance(self, tmp_path):
        # The failure the conversion prevents, pinned so a regression is caught.
        from factlog.integrations.common.provenance import Provenance, SourceRecord

        bad = Provenance(records=[SourceRecord(
            type="arxiv", id="x", imported_at="t",
            fields={"submitted": date(2023, 11, 15)})])
        with pytest.raises(TypeError):
            write_provenance(tmp_path / "source-provenance" / "x.json", bad)


class TestOptOut:
    """merges_cross_source gates the whole mechanism; Zotero/OpenAlex never merge."""

    def test_openalex_writer_never_merges_and_writes_no_sidecar(self, tmp_path):
        # An arXiv-primary file already exists; OpenAlex sees the same paper.
        ax = ArxivSourceWriter().write(
            _arxiv(arxiv_id="2311.09277"), tmp_path, imported_at="t")
        assert ax.status == "imported"
        result = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W7", arxiv_id="2311.09277"), tmp_path, imported_at="t")
        assert result.status == "skipped"
        assert not (tmp_path / "source-provenance").exists()

    def test_zotero_writer_does_not_expose_merge(self):
        assert ArxivSourceWriter.merges_cross_source is True
        assert OpenAlexSourceWriter.merges_cross_source is False
        assert ZoteroWriter.merges_cross_source is False

    def test_base_merge_hook_is_a_noop(self, tmp_path):
        from factlog.integrations.common.source_writer import BaseSourceWriter, WriteResult

        decision = WriteResult(tmp_path / "sources" / "x.md", "merged", "r")
        # The base hook returns the decision unchanged and touches no filesystem.
        out = BaseSourceWriter()._merge(object(), decision, "t")
        assert out is decision
        assert not (tmp_path / "source-provenance").exists()


class TestSameRecordReimportStillSkips:
    def test_arxiv_reimport_of_its_own_file_is_skipped_not_merged(self, tmp_path):
        (tmp_path / "sources").mkdir()
        first = ArxivSourceWriter().write(_arxiv(), tmp_path, imported_at="t")
        assert first.status == "imported"
        second = ArxivSourceWriter().write(_arxiv(version=3), tmp_path, imported_at="t")
        assert second.status == "skipped"
        assert second.reason == "already imported (arxiv_id match)"
        # A same-source re-import writes no sidecar.
        assert not (tmp_path / "source-provenance").exists()


class TestACorruptLedgerIsOnePapersProblem:
    """A merge reads a sidecar that a human or a crashed process may have left
    malformed. The failure must be scoped to that paper: the imports queued behind
    it are unrelated, and a KB should never become unimportable because one ledger
    is broken."""

    def _corrupt(self, original, text="{ corrupt"):
        sidecar = sidecar_path(original)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(text, encoding="utf-8")
        return sidecar

    def test_a_corrupt_sidecar_is_a_per_id_error_not_a_batch_crash(self, tmp_path):
        from factlog.integrations.arxiv.importer import import_works

        kb, original = _kb_with_openalex(tmp_path)
        self._corrupt(original)
        report = import_works(
            [_arxiv("2311.09277"), _arxiv("1706.03762", version=7),
             _arxiv("1810.04805", version=1)],
            target=kb, imported_at="t",
        )
        assert report.errors == 1
        # The two unrelated papers behind it still landed.
        assert report.imported == 2
        failed = next(o for o in report.outcomes if o.status == "error")
        assert "unreadable" in failed.reason

    @pytest.mark.parametrize(
        "text",
        ['{ corrupt', '{"schema_version": 99, "records": []}',
         '{"schema_version": 1}', '[]'],
    )
    def test_every_unreadable_shape_is_an_error_not_an_exception(self, tmp_path, text):
        kb, original = _kb_with_openalex(tmp_path)
        self._corrupt(original, text)
        result = ArxivSourceWriter().write(_arxiv("2311.09277"), kb, imported_at="t")
        assert result.status == "error"
        # The broken ledger is left exactly as found; nothing is overwritten.
        assert sidecar_path(original).read_text(encoding="utf-8") == text


class TestOnlyIdentifyingFieldsDiverge:
    """arXiv edits `comment` and `primary_category` without cutting a new version:
    a moderator recategorizes, an author appends "Accepted at ICML 2024". If that
    were a divergence, a routine re-import would error forever — and the suggested
    remedy, `arxiv-check-versions`, compares *versions* and could never clear it."""

    def _merge(self, kb, work):
        return ArxivSourceWriter().write(work, kb, imported_at="t")

    def test_a_changed_comment_at_the_same_version_is_absorbed(self, tmp_path):
        kb, original = _kb_with_openalex(tmp_path)
        self._merge(kb, _arxiv(version=2, comment="5 pages"))
        result = self._merge(kb, _arxiv(version=2, comment="Accepted at ICML 2024"))
        assert result.status == "merged"

    def test_a_recategorized_primary_category_is_absorbed(self, tmp_path):
        kb, original = _kb_with_openalex(tmp_path)
        self._merge(kb, _arxiv(version=2, primary_category="cs.CL"))
        result = self._merge(kb, _arxiv(version=2, primary_category="cs.LG"))
        assert result.status == "merged"

    def test_absorbed_drift_leaves_the_first_record_intact(self, tmp_path):
        # The ledger goes stale rather than lying: only a refresh may revise it.
        kb, original = _kb_with_openalex(tmp_path)
        self._merge(kb, _arxiv(version=2, comment="5 pages"))
        before = sidecar_path(original).read_bytes()
        self._merge(kb, _arxiv(version=2, comment="Accepted at ICML 2024"))
        assert sidecar_path(original).read_bytes() == before

    def test_a_version_bump_still_errors_and_names_both_versions(self, tmp_path):
        kb, original = _kb_with_openalex(tmp_path)
        self._merge(kb, _arxiv(version=2))
        result = self._merge(kb, _arxiv(version=3))
        assert result.status == "error"
        assert "v2" in result.reason and "v3" in result.reason

    def test_a_withdrawal_at_the_same_version_errors_without_claiming_a_new_version(
        self, tmp_path
    ):
        kb, original = _kb_with_openalex(tmp_path)
        self._merge(kb, _arxiv(version=2))
        result = self._merge(kb, _arxiv(version=2, withdrawn_by="admin"))
        assert result.status == "error"
        assert "withdrawn" in result.reason
        # Pointing at "record the new version" would send the user nowhere:
        # arxiv-check-versions compares versions, and this one did not change.
        assert "record the new version" not in result.reason
