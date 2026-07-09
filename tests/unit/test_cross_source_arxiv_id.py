# SPDX-License-Identifier: Apache-2.0
"""Cross-source duplicate detection on the normalized arXiv id (#64, Step 4b).

The same paper arriving once from arXiv and once from OpenAlex must be recognised
as one paper. DOI almost never fires for a preprint, so the version-stripped
arXiv base id is the exact join key. These tests pin the four probe cases from
the issue's architecture review, the provenance-scoped identity fix, and the
tolerance policy for junk in hand-edited files.

Detection classifies the match; as of #65 the arXiv writer and, as of #73, the
OpenAlex writer *merge* a cross-source match into the existing original's sidecar
(``merged``). Only Zotero still reports a bare ``skipped``. The classification
itself — "same record re-imported" vs "same paper via another database" — is what
these tests pin; the sidecar mechanics live in ``test_arxiv_merge.py``.
"""
from __future__ import annotations

from datetime import date

import pytest

from factlog.bibtex import parse_front_matter
from factlog.integrations.arxiv.source_writer import ArxivSourceWriter
from factlog.integrations.arxiv.work_parser import ParsedArxivWork
from factlog.integrations.common.source_writer import (
    CROSS_SOURCE_IDS,
    _same_source,
    normalize_cross_id,
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
        categories=("cs.CL",),
        submitted=date(2023, 11, 15),
        last_updated=date(2023, 11, 20),
        doi=None,
        journal_ref=None,
        comment=None,
        withdrawn_by=None,
        abs_url=f"https://arxiv.org/abs/{arxiv_id}v{version}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v{version}",
    )
    return ParsedArxivWork(**{**base, **over})


def _openalex(openalex_id="W1", arxiv_id=None, **over) -> ParsedWork:
    base = dict(
        openalex_id=openalex_id,
        title="A Paper",
        authors=("Ada Lovelace",),
        year=2023,
        journal=None,
        doi=None,
        pmid=None,
        arxiv_id=arxiv_id,
        work_type="preprint",
    )
    return ParsedWork(**{**base, **over})


def _write_source_file(sources_dir, name, front_matter: dict):
    sources_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in front_matter.items():
        lines.append(f'{key}: "{value}"' if isinstance(value, str) else f"{key}: {value}")
    lines.append("---")
    lines.append("\n# hand-edited\n")
    (sources_dir / name).write_text("\n".join(lines), encoding="utf-8")


class TestNormalizeCrossId:
    def test_doi_is_lowercased(self):
        assert normalize_cross_id("doi", "10.1/ABC") == "10.1/abc"

    def test_pmid_passes_through(self):
        assert normalize_cross_id("pmid", " 32738937 ") == "32738937"

    def test_arxiv_id_is_in_the_cross_source_set(self):
        assert ("arxiv_id", "arXiv id") in CROSS_SOURCE_IDS

    def test_arxiv_version_is_stripped(self):
        assert normalize_cross_id("arxiv_id", "2311.09277v2") == "2311.09277"
        assert normalize_cross_id("arxiv_id", "2311.09277") == "2311.09277"

    def test_arxiv_versioned_and_bare_collide(self):
        assert normalize_cross_id("arxiv_id", "2311.09277v2") == \
            normalize_cross_id("arxiv_id", "2311.09277")

    def test_arxiv_old_style_subject_class_is_dropped(self):
        assert normalize_cross_id("arxiv_id", "math.GT/0309136") == "math/0309136"
        assert normalize_cross_id("arxiv_id", "math/0309136") == "math/0309136"

    def test_arxiv_url_form_is_canonicalised(self):
        assert normalize_cross_id("arxiv_id", "https://arxiv.org/abs/2311.09277v2") == "2311.09277"

    def test_malformed_arxiv_id_falls_back_to_stripped_value_not_raise(self):
        # A bad value must not raise here — it just won't match anything.
        assert normalize_cross_id("arxiv_id", "  not-an-id  ") == "not-an-id"

    def test_malformed_arxiv_id_is_not_lowercased(self):
        # arXiv ids are case-significant; the fallback leaves case untouched.
        assert normalize_cross_id("arxiv_id", "NotAnId") == "NotAnId"


class TestProbeCases:
    """The four measured cases from the issue's architecture-review comment."""

    def test_A_reimport_same_arxiv_paper_reports_identity_match(self, tmp_path):
        first = ArxivSourceWriter().write(_arxiv(), tmp_path, imported_at="2026-01-01T00:00:00Z")
        assert first.status == "imported"
        second = ArxivSourceWriter().write(_arxiv(), tmp_path)
        assert second.status == "skipped"
        assert second.reason == "already imported (arxiv_id match)"
        assert second.path == first.path

    def test_B_arxiv_import_of_a_paper_in_an_openalex_file_reports_cross_id(self, tmp_path):
        # OpenAlex wrote the paper first, carrying arxiv_id as a cross-id.
        oa = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W7", arxiv_id="2311.09277"), tmp_path,
            imported_at="2026-01-01T00:00:00Z")
        assert oa.status == "imported"
        # Now the same paper is arXiv-imported. It is NOT "already imported" —
        # it is a different record of the same paper, which Step 4c tells apart
        # and merges into the OpenAlex original's sidecar (§7.3).
        result = ArxivSourceWriter().write(_arxiv(arxiv_id="2311.09277"), tmp_path)
        assert result.status == "merged"
        assert result.reason == f"duplicate arXiv id 2311.09277 (already in {oa.path.name})"

    def test_C_versioned_existing_id_collides_with_bare_import(self, tmp_path):
        # An OpenAlex-authored file whose arxiv_id is versioned (hand-edited).
        _write_source_file(tmp_path / "sources", "existing.md",
                           {"openalex_id": "W9", "arxiv_id": "2311.09277v2",
                            "imported_from": "openalex", "title": "A Paper"})
        result = ArxivSourceWriter().write(_arxiv(arxiv_id="2311.09277", version=3), tmp_path)
        assert result.status == "merged"
        assert "duplicate arXiv id" in result.reason
        assert "already in existing.md" in result.reason

    def test_D_old_style_subject_class_collides(self, tmp_path):
        _write_source_file(tmp_path / "sources", "existing.md",
                           {"openalex_id": "W9", "arxiv_id": "math.GT/0309136",
                            "imported_from": "openalex", "title": "A Paper"})
        result = ArxivSourceWriter().write(
            _arxiv(arxiv_id="math/0309136", version=1), tmp_path)
        assert result.status == "merged"
        assert "duplicate arXiv id" in result.reason

    def test_reverse_openalex_import_of_a_paper_in_an_arxiv_file(self, tmp_path):
        ax = ArxivSourceWriter().write(_arxiv(arxiv_id="2311.09277"), tmp_path,
                                       imported_at="2026-01-01T00:00:00Z")
        assert ax.status == "imported"
        # As of #73 OpenAlex is a merger too, so the same paper reached through the
        # shared arXiv id folds into the arXiv original's sidecar (§7.3).
        result = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W7", arxiv_id="2311.09277"), tmp_path)
        assert result.status == "merged"
        assert result.reason == f"duplicate arXiv id 2311.09277 (already in {ax.path.name})"


class TestProvenanceScoping:
    def test_two_arxiv_records_of_the_same_base_are_identity_deduped(self, tmp_path):
        # Same paper, different version pins -> identity path, not cross-id.
        ArxivSourceWriter().write(_arxiv(arxiv_id="2311.09277", version=1), tmp_path)
        second = ArxivSourceWriter().write(_arxiv(arxiv_id="2311.09277", version=2), tmp_path)
        assert second.status == "skipped"
        assert "arxiv_id match" in second.reason

    def test_legacy_arxiv_file_without_imported_from_stays_idempotent(self, tmp_path):
        # P3: a hand-written/legacy arXiv file with no `imported_from` must still
        # register into by_identity, so a re-import is a no-op.
        _write_source_file(tmp_path / "sources", "legacy.md",
                           {"arxiv_id": "2311.09277", "title": "A Paper"})
        result = ArxivSourceWriter().write(_arxiv(arxiv_id="2311.09277"), tmp_path)
        assert result.status == "skipped"
        assert result.reason == "already imported (arxiv_id match)"
        assert result.path.name == "legacy.md"


class TestTolerance:
    def test_malformed_arxiv_id_in_a_file_does_not_crash_unrelated_import(self, tmp_path):
        # One corrupt hand-edited arxiv_id must not abort every import in the KB.
        _write_source_file(tmp_path / "sources", "junk.md",
                           {"arxiv_id": "!!!not-an-id!!!", "imported_from": "openalex",
                            "openalex_id": "W99", "title": "Junk"})
        result = ArxivSourceWriter().write(_arxiv(arxiv_id="2005.13421"), tmp_path)
        assert result.status == "imported"

    def test_malformed_id_does_not_false_match(self, tmp_path):
        # The junk value is left uncanonicalised, so a real id never collides with it.
        _write_source_file(tmp_path / "sources", "junk.md",
                           {"arxiv_id": "garbage", "imported_from": "openalex",
                            "openalex_id": "W99", "title": "Junk"})
        result = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W1", arxiv_id="2005.13421"), tmp_path)
        assert result.status == "imported"


class TestNoRegression:
    def test_openalex_records_without_an_arxiv_id_import_independently(self, tmp_path):
        w = OpenAlexSourceWriter()
        a = w.write(_openalex(openalex_id="W1"), tmp_path)
        b = w.write(_openalex(openalex_id="W2", title="Another Paper"), tmp_path)
        assert a.status == b.status == "imported"
        assert a.path != b.path

    def test_zotero_records_are_unaffected(self, tmp_path):
        item = {
            "zotero_key": "ABCD1234",
            "title": "A Zotero Paper",
            "authors": [{"last": "Lovelace", "first": "Ada"}],
            "year": "2023",
        }
        first = ZoteroWriter().write(item, tmp_path)
        assert first.status == "imported"
        second = ZoteroWriter().write(item, tmp_path)
        assert second.status == "skipped"
        assert "zotero_key match" in second.reason

    def test_openalex_arxiv_id_is_written_and_read_back(self, tmp_path):
        # The value we write is the value a later run's index reads.
        w = OpenAlexSourceWriter()
        r = w.write(_openalex(openalex_id="W1", arxiv_id="2005.13421"), tmp_path)
        fm = parse_front_matter(r.path.read_text(encoding="utf-8"))
        assert fm["arxiv_id"] == "2005.13421"
        # A second run skips on the arXiv id via a fresh writer/index.
        again = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W2", arxiv_id="2005.13421"), tmp_path)
        assert again.status == "skipped"
        assert "duplicate arXiv id" in again.reason


@pytest.mark.parametrize("writer_cls,name", [
    (ArxivSourceWriter, "arxiv"),
    (OpenAlexSourceWriter, "openalex"),
    (ZoteroWriter, "zotero"),
])
def test_each_writer_declares_its_source_name(writer_cls, name):
    assert writer_cls.source_name == name


class TestProvenanceNeverGatesTheIdentityLookup:
    """`imported_from` chooses how a skip is *reported*. It must never decide
    whether the existing file is *found*.

    Scoping the identity index by provenance breaks P3 in silence: `openalex_id`
    and `zotero_key` are not cross-source ids, so a file whose `imported_from` a
    human capitalised or misspelled would not be found at all, and re-importing
    it would write a second file.
    """

    def _kb(self, tmp_path, front_matter):
        (tmp_path / "sources").mkdir()
        (tmp_path / "sources" / "existing.md").write_text(front_matter, encoding="utf-8")
        return tmp_path

    @pytest.mark.parametrize(
        "imported_from",
        ["openalex", "OpenAlex", "OPENALEX", " openalex ", None, "openalexx", "hand-entered"],
    )
    def test_openalex_reimport_always_skips_whatever_provenance_says(
        self, tmp_path, imported_from
    ):
        line = f"imported_from: {imported_from}\n" if imported_from else ""
        kb = self._kb(tmp_path, f'---\nopenalex_id: "W1"\n{line}---\n')
        result = OpenAlexSourceWriter().plan(_openalex(), kb)
        assert result.status == "skipped", (
            f"imported_from={imported_from!r} made a re-import write a second file"
        )

    @pytest.mark.parametrize("imported_from", ["arxiv", "ArXiv", None, "typo"])
    def test_arxiv_reimport_never_writes_a_second_file_whatever_provenance_says(
        self, tmp_path, imported_from
    ):
        # The existing file is always *found* (P3), so a re-import never writes a
        # second .md. Provenance decides how the match is *reported*: an own/legacy
        # file is a same-source ``skipped``; a foreign provenance string classifies
        # it as another database's record, which the arXiv writer ``merged``s. Both
        # are non-writing outcomes; the invariant is that neither imports.
        line = f"imported_from: {imported_from}\n" if imported_from else ""
        kb = self._kb(tmp_path, f'---\narxiv_id: "2311.09277"\n{line}---\n')
        result = ArxivSourceWriter().plan(_arxiv(), kb)
        assert result.status != "imported"
        assert result.path.name == "existing.md"
        if _same_source(imported_from or "", "arxiv"):
            assert result.status == "skipped"
        else:
            assert result.status == "merged"

    @pytest.mark.parametrize("imported_from", ["openalex", "OpenAlex", None])
    def test_provenance_case_does_not_change_the_reported_reason(
        self, tmp_path, imported_from
    ):
        # A human editing the case of `imported_from` must not reclassify the file
        # as a foreign one.
        line = f"imported_from: {imported_from}\n" if imported_from else ""
        kb = self._kb(tmp_path, f'---\nopenalex_id: "W1"\n{line}---\n')
        assert "already imported" in OpenAlexSourceWriter().plan(_openalex(), kb).reason


class TestSourceNameMatchesEmittedProvenance:
    """`source_name` and the `imported_from:` a writer emits are two independent
    literals. If they drift, the writer stops recognising its own files and every
    re-import writes a duplicate. Pin them together."""

    @pytest.mark.parametrize(
        ("writer", "parsed"),
        [
            (ArxivSourceWriter(), _arxiv()),
            (OpenAlexSourceWriter(), _openalex()),
        ],
    )
    def test_a_writer_declares_the_provenance_it_writes(self, writer, parsed):
        rendered = writer.render(parsed, imported_at="t")
        assert f"imported_from: {writer.source_name}" in rendered

    def test_a_writer_recognises_a_file_it_just_rendered(self, tmp_path):
        # The end-to-end form of the same guard, independent of the exact key name.
        (tmp_path / "sources").mkdir()
        writer = ArxivSourceWriter()
        writer.write(_arxiv(), tmp_path, imported_at="t")
        again = ArxivSourceWriter().plan(_arxiv(), tmp_path)
        assert again.status == "skipped"
        assert "already imported" in again.reason
