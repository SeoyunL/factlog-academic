# SPDX-License-Identifier: Apache-2.0
"""Unit tests for :class:`PubMedSourceWriter` (#166).

The writer supplies only the ``pmid`` identity, the front matter, the body, and
the PubMed provenance record; every placement invariant (atomic write, slug,
uniqueness, sidecar) is the shared :class:`BaseSourceWriter`'s and is exercised by
the arXiv/OpenAlex suites. These tests pin what is PubMed-specific: the source
identity, the source-scoped MeSH major/minor split preserved from the qualifier
level (#53/#165), the source-scoped retraction signal (never a bare ``retracted:``),
the bare ``doi`` cross-source key, and the provenance record's field set.
"""
from __future__ import annotations

from factlog.integrations.common.provenance import read_provenance, sidecar_path
from factlog.integrations.pubmed.mesh import MeshHeading, MeshQualifier
from factlog.integrations.pubmed.source_writer import PubMedSourceWriter
from factlog.integrations.pubmed.work_parser import ParsedPubMedWork

# A record whose MeSH exercises the #53 landmine: "Risk Factors" is major ONLY
# through a qualifier (descriptor N, qualifier Y) — the level OpenAlex drops.
_MESH = (
    MeshHeading("Dementia", True, ()),
    MeshHeading("Risk Factors", False, (MeshQualifier("prevention & control", True),)),
    MeshHeading("Humans", False, ()),
)


def _pm(pmid="32738937", doi="10.1016/s0140-6736(20)30367-6", retracted=False,
        notice=None, **over) -> ParsedPubMedWork:
    base = dict(
        pmid=pmid,
        title="Dementia prevention, intervention, and care",
        authors=("Gill Livingston", "Jonathan Huntley"),
        journal="Lancet",
        year=2020,
        doi=doi,
        abstract="A structured abstract.",
        mesh_headings=_MESH,
        retracted=retracted,
        retraction_notice_pmid=notice,
    )
    return ParsedPubMedWork(**{**base, **over})


class TestIdentityAndSlug:
    def test_identity_is_the_pmid(self):
        assert PubMedSourceWriter().identity_of(_pm()) == "32738937"

    def test_slug_uses_first_author_year_title(self, tmp_path):
        (tmp_path / "sources").mkdir()
        result = PubMedSourceWriter().write(_pm(), tmp_path, imported_at="t")
        assert result.status == "imported"
        assert result.path.name == "gill-livingston-2020-dementia-prevention-intervention-and-care.md"

    def test_cross_ids_carries_bare_doi(self):
        # Bare `doi` so §7.1 detection scans the literal key across sources.
        assert PubMedSourceWriter().cross_ids(_pm()) == {"doi": "10.1016/s0140-6736(20)30367-6"}
        assert PubMedSourceWriter().cross_ids(_pm(doi=None)) == {}

    def test_pmid_is_contributed_as_a_cross_id_automatically(self):
        # Its identity key IS a cross-source id, so the base adds it without a
        # cross_ids re-declaration.
        values = PubMedSourceWriter()._cross_id_values(_pm(doi=None))
        assert values["pmid"] == "32738937"


class TestFrontMatter:
    def _fm(self, tmp_path, **over):
        (tmp_path / "sources").mkdir()
        result = PubMedSourceWriter().write(_pm(**over), tmp_path, imported_at="2026-01-01T00:00:00Z")
        return result.path.read_text(encoding="utf-8")

    def test_core_fields_present(self, tmp_path):
        fm = self._fm(tmp_path)
        assert "pmid: \"32738937\"" in fm
        assert "journal: \"Lancet\"" in fm
        assert "year: 2020" in fm
        assert "doi: \"10.1016/s0140-6736(20)30367-6\"" in fm
        assert "imported_from: pubmed" in fm
        assert "imported_at: \"2026-01-01T00:00:00Z\"" in fm

    def test_mesh_is_source_scoped_and_split_major_minor(self, tmp_path):
        fm = self._fm(tmp_path)
        # "Risk Factors" is major only through its qualifier — it must land in major,
        # the exact case OpenAlex's descriptor-only reading loses.
        assert "pubmed_mesh_major: [\"Dementia\", \"Risk Factors\"]" in fm
        assert "pubmed_mesh_minor: [\"Humans\"]" in fm
        # Never OpenAlex's flat key.
        assert "mesh_terms:" not in fm

    def test_unindexed_record_emits_no_mesh_keys(self, tmp_path):
        fm = self._fm(tmp_path, mesh_headings=())
        assert "pubmed_mesh_major" not in fm
        assert "pubmed_mesh_minor" not in fm

    def test_retraction_is_source_scoped_never_bare(self, tmp_path):
        fm = self._fm(tmp_path, retracted=True, notice="99999")
        assert "pubmed_retracted: true" in fm
        assert "pubmed_retraction_notice_pmid: \"99999\"" in fm
        # The word must never appear as a bare top-level claim.
        assert "\nretracted:" not in fm

    def test_absent_retraction_emits_nothing(self, tmp_path):
        fm = self._fm(tmp_path, retracted=False)
        assert "pubmed_retracted" not in fm


class TestBody:
    def test_body_links_pubmed_and_doi(self, tmp_path):
        (tmp_path / "sources").mkdir()
        text = PubMedSourceWriter().write(_pm(), tmp_path, imported_at="t").path.read_text()
        assert "https://pubmed.ncbi.nlm.nih.gov/32738937/" in text
        assert "- DOI: 10.1016/s0140-6736(20)30367-6" in text

    def test_retracted_body_carries_the_warning_and_notice(self, tmp_path):
        (tmp_path / "sources").mkdir()
        text = PubMedSourceWriter().write(
            _pm(retracted=True, notice="99999"), tmp_path, imported_at="t").path.read_text()
        assert "reports this paper as retracted" in text
        assert "PMID 99999" in text


class TestProvenanceRecord:
    def _rec(self, tmp_path, **over):
        (tmp_path / "sources").mkdir()
        result = PubMedSourceWriter().write(_pm(**over), tmp_path, imported_at="2026-05-05T00:00:00Z")
        return read_provenance(sidecar_path(result.path, tmp_path)).records[0]

    def test_record_type_and_id(self, tmp_path):
        rec = self._rec(tmp_path)
        assert rec.type == "pubmed"
        assert rec.id == "32738937"
        assert rec.imported_at == "2026-05-05T00:00:00Z"

    def test_record_carries_bibliography_and_mesh(self, tmp_path):
        rec = self._rec(tmp_path)
        assert rec.fields["doi"] == "10.1016/s0140-6736(20)30367-6"
        assert rec.fields["journal"] == "Lancet"
        assert rec.fields["pubmed_mesh_major"] == ["Dementia", "Risk Factors"]
        assert rec.fields["pubmed_mesh_minor"] == ["Humans"]

    def test_retraction_fields_only_when_retracted(self, tmp_path):
        clean = self._rec(tmp_path)
        assert "retracted" not in clean.fields
        assert "retraction_verified_at" not in clean.fields

    def test_retraction_fields_carry_the_signal_and_verified_at(self, tmp_path):
        rec = self._rec(tmp_path, retracted=True, notice="99999")
        assert rec.fields["retracted"] is True
        assert rec.fields["retraction_notice_pmid"] == "99999"
        # verified_at is the import clock — when PubMed was consulted.
        assert rec.fields["retraction_verified_at"] == "2026-05-05T00:00:00Z"


class TestOptIns:
    def test_writer_merges_and_surfaces_candidates(self):
        assert PubMedSourceWriter.merges_cross_source is True
        assert PubMedSourceWriter.surfaces_candidates is True

    def test_no_identifying_fields_so_drift_is_never_a_permanent_error(self):
        # PubMed ships no refresh command in this issue; an identifying field would
        # raise a per-id error nothing could clear.
        assert PubMedSourceWriter._IDENTIFYING_FIELDS == ()
