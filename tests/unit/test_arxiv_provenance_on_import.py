# SPDX-License-Identifier: Apache-2.0
"""Write the provenance ledger on every arXiv import, not only on a merge (#72).

Before this, ``_merge`` ran only on a ``merged`` outcome, so an ordinary
``arxiv-import`` — a new paper, no duplicate — wrote a ``.md`` and *no* sidecar.
The ledger existed only where a collision happened, an artifact of import order.
Now ``ArxivSourceWriter._record`` writes ``source-provenance/<slug>.json`` for a
new-file import too, reusing the same ``_provenance_record`` builder ``_merge``
uses, behind the same ``merges_cross_source`` opt-in.

These tests pin: the new-import sidecar and its one record; ``--dry-run`` writing
nothing; byte-identical re-import despite a fresh ``imported_at``; the ``-2``
collision putting each sidecar beside the right ``.md`` (risk 1); the ``.md``
written *last* so a sidecar failure orphans nothing (risk 2); a stale/pre-existing
sidecar handled without clobbering an audit entry (risk 3); the Zotero/OpenAlex
opt-out on a genuine ``imported`` outcome; and a mixed batch where one paper's
sidecar failure does not crash the rest.
"""
from __future__ import annotations

from datetime import date

from factlog.integrations.arxiv.importer import import_works
from factlog.integrations.arxiv.source_writer import ArxivSourceWriter
from factlog.integrations.arxiv.work_parser import ParsedArxivWork
from factlog.integrations.common.provenance import (
    Provenance,
    is_sidecar,
    read_provenance,
    sidecar_path,
    write_provenance,
)
from factlog.integrations.common.source_writer import BaseSourceWriter, WriteResult
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
        journal_ref=None,
        comment="10 pages",
        withdrawn_by=None,
        abs_url=f"https://arxiv.org/abs/{arxiv_id}v{version}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v{version}",
    )
    return ParsedArxivWork(**{**base, **over})


def _openalex(openalex_id="W1", arxiv_id="2311.09277", **over) -> ParsedWork:
    base = dict(
        openalex_id=openalex_id,
        title="A Paper",
        authors=("Ada Lovelace",),
        year=2023,
        journal="Journal of Foo",
        doi=None,
        pmid=None,
        arxiv_id=arxiv_id,
        work_type="article",
    )
    return ParsedWork(**{**base, **over})


def _records(sidecar):
    return read_provenance(sidecar).records


def _arxiv_records(sidecar):
    return [r for r in _records(sidecar) if r.type == "arxiv"]


class TestNewImportWritesItsOwnLedger:
    def test_import_writes_the_md_and_a_one_record_sidecar(self, tmp_path):
        result = ArxivSourceWriter().write(_arxiv(), tmp_path, imported_at="2026-02-02T00:00:00Z")
        assert result.status == "imported"
        assert result.path.exists()  # the .md

        sidecar = sidecar_path(result.path, tmp_path)
        assert sidecar.exists()
        assert is_sidecar(sidecar)
        recs = _arxiv_records(sidecar)
        assert len(recs) == 1
        assert recs[0].id == "2311.09277"
        assert recs[0].imported_at == "2026-02-02T00:00:00Z"
        assert recs[0].fields["version"] == 2

    def test_sidecar_lives_outside_sources(self, tmp_path):
        result = ArxivSourceWriter().write(_arxiv(), tmp_path, imported_at="t")
        # Never inside sources/, so no enumerator counts it as a source.
        assert not list((tmp_path / "sources").glob("*.json"))
        assert (tmp_path / "source-provenance").is_dir()
        assert sidecar_path(result.path, tmp_path).parent == tmp_path / "source-provenance"

    def test_the_record_reuses_the_merge_builder(self, tmp_path):
        # Same builder, so the field set matches _merge's exactly (H3).
        writer = ArxivSourceWriter()
        result = writer.write(_arxiv(withdrawn_by="admin"), tmp_path, imported_at="t")
        rec = _arxiv_records(sidecar_path(result.path, tmp_path))[0].to_dict()
        assert rec == writer._provenance_record(_arxiv(withdrawn_by="admin"), "t").to_dict()


class TestDryRunWritesNothing:
    def test_plan_writes_neither_md_nor_sidecar(self, tmp_path):
        result = ArxivSourceWriter().plan(_arxiv(), tmp_path)
        assert result.status == "imported"  # would import
        assert not result.path.exists()
        assert not (tmp_path / "source-provenance").exists()
        assert not (tmp_path / "sources").exists() or not list(
            (tmp_path / "sources").glob("*.md"))

    def test_importer_dry_run_writes_no_sidecar_dir(self, tmp_path):
        report = import_works([_arxiv()], target=tmp_path, imported_at="t", dry_run=True)
        assert report.imported == 1
        assert not (tmp_path / "source-provenance").exists()


class TestReimportIsAByteIdenticalNoop:
    def test_reimport_with_a_fresh_timestamp_leaves_the_sidecar_byte_identical(self, tmp_path):
        # The CLI stamps a fresh imported_at each run. A re-import is `skipped`
        # before any write, so the ledger must not be rewritten with the new clock.
        first = ArxivSourceWriter().write(_arxiv(), tmp_path, imported_at="2026-01-01T00:00:00Z")
        sidecar = sidecar_path(first.path, tmp_path)
        before = sidecar.read_bytes()

        again = ArxivSourceWriter().write(_arxiv(), tmp_path, imported_at="2026-09-09T00:00:00Z")
        assert again.status == "skipped"
        assert sidecar.read_bytes() == before
        assert _arxiv_records(sidecar)[0].imported_at == "2026-01-01T00:00:00Z"


class TestSlugCollisionRisk1:
    """`_unique_path` gives the second `.md` a `-2` name; `sidecar_path` derives
    from that FINAL name, so two different papers with the same base slug get two
    distinct sidecars — not one clobbering the other."""

    def test_two_papers_same_base_slug_get_two_distinct_sidecars(self, tmp_path):
        writer = ArxivSourceWriter()
        # Same author/year/title -> same base slug; different identity.
        first = writer.write(_arxiv(arxiv_id="1111.11111"), tmp_path, imported_at="t")
        second = writer.write(_arxiv(arxiv_id="2222.22222"), tmp_path, imported_at="t")
        assert first.status == second.status == "imported"
        assert first.path != second.path
        assert second.path.name.endswith("-2.md")  # the collision suffix

        s1, s2 = sidecar_path(first.path, tmp_path), sidecar_path(second.path, tmp_path)
        assert s1 != s2
        assert s1.exists() and s2.exists()
        # Each sidecar holds its OWN paper's record, not the other's.
        assert [r.id for r in _arxiv_records(s1)] == ["1111.11111"]
        assert [r.id for r in _arxiv_records(s2)] == ["2222.22222"]


class TestSidecarFailureOrphansNoMdRisk2:
    """The `.md` is written LAST. If the sidecar write fails the import errors and
    no `.md` is left behind — the reverse order would leave an orphan whose mere
    existence (P4: never rewritten; P3: re-import skips) suppresses the ledger
    forever."""

    def _sidecar_for(self, writer, parsed, kb):
        return sidecar_path(kb / "sources" / writer.generate_slug(parsed), kb)

    def test_an_unusable_sidecar_directory_leaves_no_md(self, tmp_path):
        writer = ArxivSourceWriter()
        parsed = _arxiv()
        # source-provenance occupied by a plain file: the sidecar cannot be
        # created under it (NotADirectoryError, an OSError).
        (tmp_path / "source-provenance").write_text("not a dir", encoding="utf-8")

        result = writer.write(parsed, tmp_path, imported_at="t")
        assert result.status == "error"
        assert "cannot write" in result.reason
        # No orphan: the .md was never created.
        assert not list((tmp_path / "sources").glob("*.md"))

    def test_a_write_failure_leaves_no_md_and_reports_cannot_write(self, tmp_path):
        writer = ArxivSourceWriter()
        parsed = _arxiv()
        # A read-only source-provenance dir: the (absent) sidecar reads as empty,
        # then the write into the dir fails with a PermissionError (OSError).
        prov = tmp_path / "source-provenance"
        prov.mkdir()
        prov.chmod(0o500)
        try:
            result = writer.write(parsed, tmp_path, imported_at="t")
        finally:
            prov.chmod(0o700)  # let tmp cleanup remove it
        assert result.status == "error"
        assert "cannot write" in result.reason
        assert not list((tmp_path / "sources").glob("*.md"))

    def test_a_corrupt_sidecar_at_a_new_paths_slot_is_replaced_not_a_blocker(self, tmp_path):
        # It cannot be this original's ledger — the original does not exist yet.
        # A corrupt ledger for a source that is gone must not make the slug
        # permanently unimportable.
        writer = ArxivSourceWriter()
        parsed = _arxiv()
        sidecar = self._sidecar_for(writer, parsed, tmp_path)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("{ corrupt", encoding="utf-8")

        result = writer.write(parsed, tmp_path, imported_at="t")
        assert result.status == "imported"
        assert [r.id for r in read_provenance(sidecar).records] == [parsed.arxiv_id]


class TestPreexistingSidecarRisk3:
    """A sidecar can pre-exist at a NEW `.md`'s path only via a stale ledger left
    by a deleted source whose slug is reused, or a prior run that wrote the sidecar
    then failed before the `.md`. Either way it is not this original's ledger —
    the original does not exist yet — so it is replaced. Appending would make the
    new record's provenance name a source it never had."""

    def test_a_stale_sidecar_from_a_deleted_source_is_replaced(self, tmp_path):
        writer = ArxivSourceWriter()
        # Import a paper, then delete its .md, orphaning the sidecar.
        first = writer.write(_arxiv(arxiv_id="1111.11111"), tmp_path, imported_at="t")
        stale = sidecar_path(first.path, tmp_path)
        first.path.unlink()
        assert stale.exists()

        # A DIFFERENT paper now reuses the freed base slug (no -2, the .md is gone).
        second = ArxivSourceWriter().write(_arxiv(arxiv_id="2222.22222"), tmp_path, imported_at="t")
        assert second.status == "imported"
        assert sidecar_path(second.path, tmp_path) == stale  # same path, reused
        ids = sorted(r.id for r in _arxiv_records(stale))
        # Only the new paper. Keeping 1111.11111 would make this original's ledger
        # assert it came from a paper it has nothing to do with.
        assert ids == ["2222.22222"]

    def test_a_sidecar_already_holding_our_record_self_heals_and_writes_the_md(self, tmp_path):
        # Models a prior run that wrote the sidecar then failed before the .md.
        writer = ArxivSourceWriter()
        parsed = _arxiv()
        sidecar = sidecar_path(tmp_path / "sources" / writer.generate_slug(parsed), tmp_path)
        record = writer._provenance_record(parsed, imported_at="t")
        write_provenance(sidecar, Provenance(records=[record]))
        before = sidecar.read_bytes()

        result = writer.write(parsed, tmp_path, imported_at="t")
        assert result.status == "imported"
        assert result.path.exists()  # the missing .md is now created
        assert sidecar.read_bytes() == before  # idempotent no-op on the ledger


class TestRecordingIsGatedByMergesCrossSource:
    """`_record` is gated by `merges_cross_source`. Zotero opts out and writes no
    sidecar; OpenAlex now opts in (#73) and leaves its own one-record ledger."""

    def test_openalex_import_writes_its_own_one_record_sidecar(self, tmp_path):
        # The inverse of the old #72 gap: an OpenAlex-primary import now records a
        # ledger of its own, symmetric with arXiv.
        result = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W9", arxiv_id="9999.99999"), tmp_path, imported_at="t")
        assert result.status == "imported"
        sidecar = sidecar_path(result.path, tmp_path)
        assert sidecar.exists()
        recs = read_provenance(sidecar).records
        assert [r.type for r in recs] == ["openalex"]
        assert recs[0].id == "W9"

    def test_zotero_import_writes_no_sidecar(self, tmp_path):
        result = ZoteroWriter().write(
            {"zotero_key": "ABCD1234", "title": "A Zotero Paper",
             "authors": [{"last": "Lovelace", "first": "Ada"}], "year": "2023"},
            tmp_path, imported_at="t")
        assert result.status == "imported"
        assert not (tmp_path / "source-provenance").exists()

    def test_base_record_hook_is_a_noop(self, tmp_path):
        decision = WriteResult(tmp_path / "sources" / "x.md", "imported", "")
        out = BaseSourceWriter()._record(object(), decision, "t", tmp_path)
        assert out is decision
        assert not (tmp_path / "source-provenance").exists()

    def test_the_writers_that_record_are_exactly_the_mergers(self):
        assert ArxivSourceWriter.merges_cross_source is True
        assert OpenAlexSourceWriter.merges_cross_source is True
        assert ZoteroWriter.merges_cross_source is False


class TestMergeStillBehavesAsBefore:
    def test_an_openalex_primary_paper_is_merged_not_reimported(self, tmp_path):
        existing = OpenAlexSourceWriter().write(_openalex(), tmp_path, imported_at="t")
        assert existing.status == "imported"
        before_md = sorted(p.name for p in (tmp_path / "sources").glob("*.md"))

        result = ArxivSourceWriter().write(_arxiv(), tmp_path, imported_at="t")
        assert result.status == "merged"
        assert result.path == existing.path  # the existing original
        # No new .md; the merge folds into the existing original's sidecar.
        assert sorted(p.name for p in (tmp_path / "sources").glob("*.md")) == before_md
        assert _arxiv_records(sidecar_path(existing.path, tmp_path))[0].id == "2311.09277"


class TestMixedBatch:
    def test_imported_merged_skipped_error_and_a_recording_failure_coexist(self, tmp_path):
        # Seed an OpenAlex-primary paper (to be merged) and an arXiv paper (to be
        # re-imported => skipped).
        OpenAlexSourceWriter().write(
            _openalex(openalex_id="W1", arxiv_id="2311.09277"), tmp_path, imported_at="t")
        ArxivSourceWriter().write(_arxiv(arxiv_id="1706.03762", version=5), tmp_path, imported_at="t")

        # Sabotage one brand-new paper's sidecar so its record write fails, without
        # affecting any other paper: occupy its path with a directory, which
        # `os.replace` cannot overwrite with a file.
        writer = ArxivSourceWriter()
        boom = _arxiv(arxiv_id="9000.00001", version=1, title="Boom")
        bad = sidecar_path(tmp_path / "sources" / writer.generate_slug(boom), tmp_path)
        bad.mkdir(parents=True, exist_ok=True)

        report = import_works(
            [
                _arxiv(arxiv_id="2311.09277", version=2),        # merged (OpenAlex-primary)
                _arxiv(arxiv_id="1706.03762", version=5),        # skipped (already arXiv)
                _arxiv(arxiv_id="2005.13421", version=1, title="Fresh"),  # imported
                boom,                                            # error (bad sidecar)
            ],
            target=tmp_path, imported_at="t",
        )
        statuses = {o.key: o.status for o in report.outcomes}
        assert statuses["2311.09277v2"] == "merged"
        assert statuses["1706.03762v5"] == "skipped"
        assert statuses["2005.13421v1"] == "imported"
        assert statuses["9000.00001v1"] == "error"
        assert report.imported == 1 and report.merged == 1
        assert report.skipped == 1 and report.errors == 1
        # The failing paper left no orphaned .md.
        assert not any(p.name.startswith("ada-lovelace-2023-boom") for p in (tmp_path / "sources").glob("*.md"))


class TestAStaleSidecarNeverAttachesToANewPaper:
    """`_record` runs only for a file that does not exist yet, so any sidecar at
    its path belongs to something else — a deleted source whose slug this paper
    now reuses. Appending would make the new original's ledger name a source it
    never had."""

    def _work(self, arxiv_id, title="Attention Is All You Need",
              author="Ashish Vaswani", year=2017):
        return _arxiv(arxiv_id=arxiv_id, version=1, title=title,
                      authors=(author,), submitted=date(year, 1, 1),
                      last_updated=date(year, 1, 1))

    def test_a_deleted_papers_ledger_is_not_inherited_by_its_slug_successor(self, tmp_path):
        (tmp_path / "sources").mkdir()
        first = ArxivSourceWriter().write(self._work("1706.03762"), tmp_path, imported_at="t1")
        sidecar = sidecar_path(first.path, tmp_path)
        assert sidecar.is_file()

        first.path.unlink()  # the user deletes the source; the ledger is left behind

        # A different paper whose author, year and title produce the same slug.
        second = ArxivSourceWriter().write(self._work("2401.09999"), tmp_path, imported_at="t2")
        assert second.status == "imported"
        assert second.path.name == first.path.name  # the slug really is reused

        ids = [r.id for r in read_provenance(sidecar_path(second.path, tmp_path)).records]
        assert ids == ["2401.09999"], (
            "the new original's ledger claims it also came from the deleted paper"
        )

    def test_a_retry_after_a_failed_md_write_is_byte_identical(self, tmp_path):
        # The sidecar is written before the `.md`. A crash between the two leaves
        # a sidecar with exactly the record the retry will write.
        (tmp_path / "sources").mkdir()
        work = self._work("1706.03762")
        writer = ArxivSourceWriter()
        decision = writer._resolve(work, tmp_path, "write")
        writer._record(work, decision, "t1", tmp_path)
        before = sidecar_path(decision.path, tmp_path).read_bytes()

        result = ArxivSourceWriter().write(work, tmp_path, imported_at="t1")
        assert result.status == "imported"
        assert sidecar_path(result.path, tmp_path).read_bytes() == before


class TestAFailedRecordDoesNotFreeItsSlug:
    """`_reserve` claims the filename before `_record` runs, and a `_record`
    failure does not release it. That is deliberate: the module promises
    reproducible collision suffixes for a deterministic input order, and a suffix
    that depended on whether an unrelated IO error occurred would not be
    reproducible. The failed slot stays unused rather than shifting every later
    name."""

    def test_a_later_paper_keeps_the_suffix_it_would_have_had_anyway(self, tmp_path):
        (tmp_path / "sources").mkdir()
        writer = ArxivSourceWriter()
        first = _arxiv(arxiv_id="1111.11111")
        second = _arxiv(arxiv_id="2222.22222")  # same author/year/title -> same base slug
        assert writer.generate_slug(first) == writer.generate_slug(second)

        # Make the first paper's sidecar write fail: occupy its path with a dir.
        doomed = sidecar_path(tmp_path / "sources" / writer.generate_slug(first), tmp_path)
        doomed.mkdir(parents=True, exist_ok=True)

        a = writer.write(first, tmp_path, imported_at="t")
        b = writer.write(second, tmp_path, imported_at="t")

        assert a.status == "error"
        assert b.status == "imported"
        # `-2`, exactly as if the first import had succeeded. The suffix does not
        # depend on whether an unrelated write happened to fail.
        assert b.path.name.endswith("-2.md")
        assert [r.id for r in _arxiv_records(sidecar_path(b.path, tmp_path))] == ["2222.22222"]
