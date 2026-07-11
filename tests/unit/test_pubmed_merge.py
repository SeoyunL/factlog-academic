# SPDX-License-Identifier: Apache-2.0
"""Cross-source merge of a PubMed record into an existing original (#166, §5.1/§7.3).

When a PubMed import matches a paper already in the KB via another database — the
same DOI, or the same PMID a record echoes — the PubMed view is folded into that
original's provenance sidecar instead of a second ``.md``. These tests pin the
Done-when acceptance points: no double file, MeSH coexistence beside OpenAlex's
flat ``mesh_terms``, a byte-identical re-import no-op, and ``--dry-run`` writing
nothing. Counter-examples: a same-source re-import is a plain skip, not a merge.
"""
from __future__ import annotations

from factlog.integrations.common.provenance import read_provenance, sidecar_path
from factlog.integrations.openalex.source_writer import OpenAlexSourceWriter
from factlog.integrations.openalex.work_parser import ParsedWork
from factlog.integrations.pubmed.mesh import MeshHeading, MeshQualifier
from factlog.integrations.pubmed.source_writer import PubMedSourceWriter
from factlog.integrations.pubmed.work_parser import ParsedPubMedWork
from factlog.integrations.zotero.source_writer import SourceWriter as ZoteroWriter

_MESH = (
    MeshHeading("Dementia", True, ()),
    MeshHeading("Risk Factors", False, (MeshQualifier("prevention & control", True),)),
    MeshHeading("Humans", False, ()),
)
_DOI = "10.1016/s0140-6736(20)30367-6"


def _pm(pmid="32738937", doi=_DOI, **over) -> ParsedPubMedWork:
    base = dict(
        pmid=pmid, title="Dementia prevention", authors=("Gill Livingston",),
        journal="Lancet", year=2020, doi=doi, abstract="An abstract.",
        mesh_headings=_MESH,
    )
    return ParsedPubMedWork(**{**base, **over})


def _openalex(openalex_id="W1", doi=_DOI, pmid=None, mesh_terms=("Dementia", "Humans"),
              **over) -> ParsedWork:
    base = dict(
        openalex_id=openalex_id, title="Dementia prevention", authors=("Gill Livingston",),
        year=2020, journal="Lancet", doi=doi, pmid=pmid, arxiv_id=None,
        work_type="article", mesh_terms=mesh_terms,
    )
    return ParsedWork(**{**base, **over})


def _kb_with_openalex(tmp_path, **oa):
    existing = OpenAlexSourceWriter().write(_openalex(**oa), tmp_path, imported_at="2026-01-01T00:00:00Z")
    assert existing.status == "imported"
    return tmp_path, existing.path


def _records(sidecar):
    return read_provenance(sidecar).records


class TestMergeOnSharedDoi:
    def test_a_matching_doi_merges_into_the_existing_original(self, tmp_path):
        kb, existing = _kb_with_openalex(tmp_path)
        before_md = sorted(p.name for p in (kb / "sources").glob("*.md"))
        result = PubMedSourceWriter().write(_pm(), kb, imported_at="2026-02-02T00:00:00Z")
        assert result.status == "merged"
        assert result.path == existing  # the existing original, not a new file
        # No second .md written.
        assert sorted(p.name for p in (kb / "sources").glob("*.md")) == before_md

    def test_the_pubmed_record_is_appended_beside_openalex(self, tmp_path):
        kb, existing = _kb_with_openalex(tmp_path)
        PubMedSourceWriter().write(_pm(), kb, imported_at="2026-02-02T00:00:00Z")
        recs = _records(sidecar_path(existing, kb))
        assert sorted(r.type for r in recs) == ["openalex", "pubmed"]
        pubmed = next(r for r in recs if r.type == "pubmed")
        assert pubmed.id == "32738937"
        assert pubmed.imported_at == "2026-02-02T00:00:00Z"


class TestMergeOnSharedPmid:
    def test_a_pmid_echoed_by_openalex_merges(self, tmp_path):
        # The OpenAlex record carries no DOI but records the PMID; the join key is
        # the PMID this writer contributes automatically as a cross-source id.
        kb, existing = _kb_with_openalex(tmp_path, doi=None, pmid="32738937")
        result = PubMedSourceWriter().write(_pm(doi=None), kb, imported_at="t")
        assert result.status == "merged"
        assert result.path == existing
        assert len(list((kb / "sources").glob("*.md"))) == 1


class TestMeshCoexists:
    def test_openalex_flat_mesh_terms_is_preserved_alongside_pubmed_mesh(self, tmp_path):
        kb, existing = _kb_with_openalex(tmp_path)
        before_md_bytes = existing.read_bytes()
        PubMedSourceWriter().write(_pm(), kb, imported_at="t")
        # The original .md is byte-immutable, so OpenAlex's flat mesh_terms survives.
        assert existing.read_bytes() == before_md_bytes
        assert "mesh_terms: [\"Dementia\", \"Humans\"]" in existing.read_text()
        # And PubMed's richer major/minor lives in the sidecar, not overwriting it.
        pubmed = next(r for r in _records(sidecar_path(existing, kb)) if r.type == "pubmed")
        assert pubmed.fields["pubmed_mesh_major"] == ["Dementia", "Risk Factors"]
        assert pubmed.fields["pubmed_mesh_minor"] == ["Humans"]


class TestOriginalImmutableAndIdempotent:
    def test_original_bytes_and_mtime_are_unchanged(self, tmp_path):
        kb, existing = _kb_with_openalex(tmp_path)
        before_bytes = existing.read_bytes()
        before_mtime = existing.stat().st_mtime_ns
        assert PubMedSourceWriter().write(_pm(), kb, imported_at="t").status == "merged"
        assert existing.read_bytes() == before_bytes
        assert existing.stat().st_mtime_ns == before_mtime

    def test_reimport_with_a_fresh_timestamp_is_a_byte_identical_noop(self, tmp_path):
        kb, existing = _kb_with_openalex(tmp_path)
        sidecar = sidecar_path(existing, kb)
        first = PubMedSourceWriter().write(_pm(), kb, imported_at="2026-01-01T00:00:00Z")
        assert first.status == "merged"
        before = sidecar.read_bytes()

        later = PubMedSourceWriter().write(_pm(), kb, imported_at="2099-09-09T00:00:00Z")
        assert later.status == "merged"
        assert sidecar.read_bytes() == before  # first timestamp kept
        pubmed = next(r for r in _records(sidecar) if r.type == "pubmed")
        assert pubmed.imported_at == "2026-01-01T00:00:00Z"
        assert sum(1 for r in _records(sidecar) if r.type == "pubmed") == 1


class TestDryRun:
    def test_plan_predicts_merged_and_writes_nothing(self, tmp_path):
        kb, existing = _kb_with_openalex(tmp_path)
        sidecar = sidecar_path(existing, kb)
        before = sidecar.read_bytes()
        result = PubMedSourceWriter().plan(_pm(), kb)
        assert result.status == "merged"
        assert result.path == existing
        assert sidecar.read_bytes() == before
        assert not any(r.type == "pubmed" for r in _records(sidecar))
        assert len(list((kb / "sources").glob("*.md"))) == 1


class TestSameSourceReimportIsPlainSkip:
    def test_a_pubmed_reimport_of_its_own_file_is_skipped_not_merged(self, tmp_path):
        (tmp_path / "sources").mkdir()
        first = PubMedSourceWriter().write(_pm(), tmp_path, imported_at="t")
        assert first.status == "imported"
        sidecar = sidecar_path(first.path, tmp_path)
        before = sidecar.read_bytes()
        second = PubMedSourceWriter().write(_pm(), tmp_path, imported_at="t")
        assert second.status == "skipped"
        assert "already imported" in second.reason
        assert sidecar.read_bytes() == before
        assert sum(1 for r in _records(sidecar) if r.type == "pubmed") == 1


class TestZoteroStillOptsOut:
    def test_only_the_mergers_merge(self):
        assert PubMedSourceWriter.merges_cross_source is True
        assert ZoteroWriter.merges_cross_source is False
