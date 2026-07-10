# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the OpenAlex SourceWriter (#51, spec §5.4/§5.5 Step 4)."""
from __future__ import annotations

import pytest

from factlog.bibtex import parse_front_matter
from factlog.integrations.openalex.source_writer import OpenAlexSourceWriter
from factlog.integrations.openalex.work_parser import Concept, ParsedWork, PrimaryTopic
from factlog.integrations.zotero.source_writer import SourceWriter as ZoteroWriter


CONCEPTS = (
    Concept("Interpretability", 0.91, 3),
    Concept("Symbolic artificial intelligence", 0.49, 2),
    Concept("Paleontology", 0.0, 1),  # #54: unrelated roots score exactly 0.00
)
TOPIC = PrimaryTopic("Neural Networks and Applications", 0.9944,
                     "Artificial Intelligence", "Computer Science", "Physical Sciences")


def _work(**over) -> ParsedWork:
    base = dict(
        openalex_id="W3113149630",
        title="Neurosymbolic AI: the 3rd wave",
        # OpenAlex has no family/given split: display_name is always given-first.
        authors=("Artur d’Avila Garcez", "Luís C. Lamb"),
        year=2023,
        journal="Artificial Intelligence Review",
        doi="10.1007/s10462-023-10448-w",
        pmid=None,
        concepts=CONCEPTS,
        primary_topic=TOPIC,
        cited_by_count=321,
        work_type="article",
        openalex_is_retracted=False,
        abstract="Current advances in AI.",
        abstract_complete=True,
    )
    return ParsedWork(**{**base, **over})


def _front_matter(text: str) -> str:
    return text.split("---", 2)[1]


class TestRender:
    def test_front_matter_carries_the_spec_fields(self, tmp_path):
        text = OpenAlexSourceWriter().render(_work(), imported_at="2026-07-09T00:00:00Z")
        fm = _front_matter(text)
        assert 'openalex_id: "W3113149630"' in fm
        assert 'title: "Neurosymbolic AI: the 3rd wave"' in fm
        assert 'authors: ["Artur d’Avila Garcez", "Luís C. Lamb"]' in fm
        assert "year: 2023" in fm
        assert 'journal: "Artificial Intelligence Review"' in fm
        assert 'doi: "10.1007/s10462-023-10448-w"' in fm
        assert 'tags: ["Interpretability", "Symbolic artificial intelligence"]' in fm
        assert "cited_by_count: 321" in fm
        assert 'type: "article"' in fm
        assert "imported_from: openalex" in fm
        assert 'imported_at: "2026-07-09T00:00:00Z"' in fm

    def test_body_has_title_abstract_and_original_source(self):
        text = OpenAlexSourceWriter().render(_work())
        assert "# Neurosymbolic AI: the 3rd wave" in text
        assert "## Abstract\n\nCurrent advances in AI." in text
        assert "- OpenAlex: `https://openalex.org/W3113149630`" in text
        assert "- DOI: 10.1007/s10462-023-10448-w" in text

    def test_missing_abstract_is_stated_not_omitted(self):
        # 37% of live works have no abstract; the file must still be readable.
        assert "_No abstract available._" in OpenAlexSourceWriter().render(_work(abstract=""))

    def test_include_abstract_false_drops_the_section(self):
        text = OpenAlexSourceWriter(include_abstract=False).render(_work())
        assert "## Abstract" not in text

    def test_optional_fields_are_omitted_when_absent(self):
        fm = _front_matter(OpenAlexSourceWriter().render(
            _work(journal=None, doi=None, pmid=None, concepts=(), primary_topic=None,
                  cited_by_count=None, work_type=None, year=None, abstract="",
                  abstract_complete=None)))
        for absent in ("journal:", "doi:", "pmid:", "arxiv_id:", "tags:", "cited_by_count:",
                       "type:", "year:", "openalex_concepts:", "primary_topic:",
                       "abstract_complete:"):
            assert absent not in fm
        assert 'openalex_id: "W3113149630"' in fm

    def test_zero_citations_is_recorded_not_dropped(self):
        assert "cited_by_count: 0" in _front_matter(
            OpenAlexSourceWriter().render(_work(cited_by_count=0)))

    def test_pmid_is_recorded_when_present(self):
        text = OpenAlexSourceWriter().render(_work(pmid="32738937"))
        assert 'pmid: "32738937"' in _front_matter(text)
        assert "- PMID: 32738937" in text

    def test_arxiv_id_is_emitted_when_present(self):
        # The canonical base id, as a bare `arxiv_id:` key the cross-source index
        # and the arXiv writer both read (#64).
        fm = _front_matter(OpenAlexSourceWriter().render(_work(arxiv_id="2005.13421")))
        assert 'arxiv_id: "2005.13421"' in fm

    def test_arxiv_id_is_omitted_when_absent(self):
        fm = _front_matter(OpenAlexSourceWriter().render(_work()))
        assert "arxiv_id:" not in fm

    def test_control_characters_cannot_break_the_front_matter(self):
        fm = _front_matter(OpenAlexSourceWriter().render(_work(title='a\nb: "c"')))
        assert 'title: "a\\nb: \\"c\\""' in fm

    def test_no_title_renders_untitled(self):
        assert "# Untitled" in OpenAlexSourceWriter().render(_work(title=None))


class TestTagsMapping:
    """#54 Mapping B: `tags` is concepts scoring above zero, most confident first."""

    def test_zero_scored_concepts_never_reach_tags(self):
        fm = _front_matter(OpenAlexSourceWriter().render(_work()))
        assert 'tags: ["Interpretability", "Symbolic artificial intelligence"]' in fm
        assert "Paleontology" not in fm.split("openalex_concepts:")[0]

    def test_the_dropped_concepts_survive_in_provenance(self):
        # §4.3: the tags filter is lossy by design; nothing measured is discarded.
        fm = _front_matter(OpenAlexSourceWriter().render(_work()))
        assert 'openalex_concepts: [{name: "Interpretability", score: 0.9100, level: 3}, ' \
               '{name: "Symbolic artificial intelligence", score: 0.4900, level: 2}, ' \
               '{name: "Paleontology", score: 0.0000, level: 1}]' in fm

    def test_concepts_key_is_gone(self):
        # Renamed to `tags`, which is the field it actually feeds.
        assert "\nconcepts:" not in _front_matter(OpenAlexSourceWriter().render(_work()))

    def test_a_work_whose_concepts_all_score_zero_gets_no_tags(self):
        work = _work(concepts=(Concept("Art", 0.0, 0),))
        fm = _front_matter(OpenAlexSourceWriter().render(work))
        assert "tags:" not in fm
        assert "openalex_concepts:" in fm

    def test_unscored_concept_is_recorded_without_a_score(self):
        fm = _front_matter(OpenAlexSourceWriter().render(_work(concepts=(Concept("X"),))))
        assert 'openalex_concepts: [{name: "X"}]' in fm

    def test_wrong_sense_entity_is_written_as_documented(self):
        # Irreducible by any threshold; the P1 gate catches it, not the writer.
        work = _work(concepts=(Concept("Object (grammar)", 0.57, 2),))
        assert 'tags: ["Object (grammar)"]' in _front_matter(OpenAlexSourceWriter().render(work))


class TestMeshTerms:
    def test_mesh_terms_are_written(self):
        # v2 §3.2: openalex-import populates mesh_terms as a flat descriptor list.
        fm = _front_matter(OpenAlexSourceWriter().render(_work(mesh_terms=("Aging", "Animals"))))
        assert 'mesh_terms: ["Aging", "Animals"]' in fm

    def test_absent_when_the_work_has_no_mesh(self):
        assert "mesh_terms:" not in _front_matter(OpenAlexSourceWriter().render(_work()))

    def test_no_major_minor_distinction_is_recorded(self):
        fm = _front_matter(OpenAlexSourceWriter().render(_work(mesh_terms=("Aging",))))
        assert "major" not in fm.lower()


class TestPrimaryTopic:
    def test_hierarchy_is_written_as_flat_keys(self):
        fm = _front_matter(OpenAlexSourceWriter().render(_work()))
        assert 'primary_topic: "Neural Networks and Applications"' in fm
        assert "primary_topic_score: 0.9944" in fm
        assert 'primary_topic_subfield: "Artificial Intelligence"' in fm
        assert 'primary_topic_field: "Computer Science"' in fm
        assert 'primary_topic_domain: "Physical Sciences"' in fm

    def test_a_low_score_is_recorded_not_hidden(self):
        # #54: primary_topic can be the top of three topics all scoring ~0.06.
        work = _work(primary_topic=PrimaryTopic("Libraries and Information Services", 0.0621))
        assert "primary_topic_score: 0.0621" in _front_matter(
            OpenAlexSourceWriter().render(work))

    def test_absent_hierarchy_levels_are_omitted(self):
        fm = _front_matter(OpenAlexSourceWriter().render(_work(primary_topic=PrimaryTopic("T"))))
        assert 'primary_topic: "T"' in fm
        for absent in ("primary_topic_score:", "primary_topic_subfield:",
                       "primary_topic_field:", "primary_topic_domain:"):
            assert absent not in fm


class TestAbstractComplete:
    def test_true_is_recorded(self):
        assert "abstract_complete: true" in _front_matter(OpenAlexSourceWriter().render(_work()))

    def test_false_is_recorded(self):
        assert "abstract_complete: false" in _front_matter(
            OpenAlexSourceWriter().render(_work(abstract_complete=False)))

    def test_absent_when_there_is_no_abstract(self):
        fm = _front_matter(OpenAlexSourceWriter().render(
            _work(abstract="", abstract_complete=None)))
        assert "abstract_complete:" not in fm


class TestFrontMatterStaysParseable:
    """`bibtex.parse_front_matter` strips each line before matching `key: value`.

    An indented `score:` or `field:` inside a nested mapping would therefore be
    read as a *top-level* key and corrupt `factlog export`. Every key this writer
    adds must stay on one line.
    """

    def test_export_parser_sees_only_intended_top_level_keys(self):
        fm = parse_front_matter(OpenAlexSourceWriter().render(_work()))
        assert fm["title"] == "Neurosymbolic AI: the 3rd wave"
        assert fm["doi"] == "10.1007/s10462-023-10448-w"
        assert fm["primary_topic"] == "Neural Networks and Applications"
        # The nested field names must NOT have leaked into the top level.
        for leaked in ("score", "name", "level", "subfield", "field", "domain"):
            assert leaked not in fm

    def test_scored_concepts_do_not_shadow_the_title(self):
        # A concept literally named "title" must not overwrite fm["title"].
        work = _work(concepts=(Concept("title", 0.9, 1),))
        fm = parse_front_matter(OpenAlexSourceWriter().render(work))
        assert fm["title"] == "Neurosymbolic AI: the 3rd wave"

    def test_every_front_matter_line_is_unindented(self):
        for line in _front_matter(OpenAlexSourceWriter().render(_work())).splitlines():
            assert line == line.lstrip(), f"indented front-matter line: {line!r}"

    def test_bibtex_export_still_finds_the_bibliographic_fields(self):
        fm = parse_front_matter(OpenAlexSourceWriter().render(_work()))
        assert fm["year"] == "2023"
        assert fm["journal"] == "Artificial Intelligence Review"
        assert fm["authors"] == ["Artur d’Avila Garcez", "Luís C. Lamb"]


class TestRetractionIsSourceScoped:
    def test_retracted_flag_is_namespaced_never_bare(self):
        fm = _front_matter(OpenAlexSourceWriter().render(_work(openalex_is_retracted=True)))
        assert "openalex_is_retracted: true" in fm
        # A bare `retracted:` is §7.2's merged claim, which PubMed owns.
        assert "\nretracted:" not in fm

    def test_retracted_body_warns_that_the_flag_is_unverified(self):
        text = OpenAlexSourceWriter().render(_work(openalex_is_retracted=True))
        assert "OpenAlex flags this work as retracted" in text
        assert "unverified" in text

    def test_unretracted_work_carries_no_flag_and_no_warning(self):
        text = OpenAlexSourceWriter().render(_work())
        assert "openalex_is_retracted" not in text
        assert "retracted" not in text.lower()


class TestSlug:
    def test_slug_uses_the_full_first_author_name(self):
        # OpenAlex exposes no surname field, and raw_author_name is inconsistent
        # ("Pedregosa, Fabian" vs "J. R. Quinlan" vs "Witten, I. H. (Ian H.) 62970").
        # Guessing a surname would repeat the compound-surname bug of #45; the
        # slug is cosmetic (identity lives in `openalex_id`), so use the whole name.
        assert OpenAlexSourceWriter().generate_slug(_work()) == (
            "artur-d-avila-garcez-2023-neurosymbolic-ai-the-3rd-wave.md")

    def test_no_authors_becomes_anonymous(self):
        assert OpenAlexSourceWriter().generate_slug(_work(authors=())).startswith("anonymous-")

    def test_no_year_becomes_n_d(self):
        assert "-n-d-" in OpenAlexSourceWriter().generate_slug(_work(year=None))

    def test_no_title_becomes_untitled(self):
        assert OpenAlexSourceWriter().generate_slug(_work(title=None)).endswith("-untitled.md")

    def test_long_title_is_byte_capped(self):
        slug = OpenAlexSourceWriter().generate_slug(_work(title="word " * 200))
        assert len(slug.encode("utf-8")) <= 190 + len(".md")


class TestWrite:
    def test_writes_a_source_file(self, tmp_path):
        result = OpenAlexSourceWriter().write(_work(), tmp_path, "2026-07-09T00:00:00Z")
        assert result.status == "imported"
        assert result.path.exists()
        assert 'openalex_id: "W3113149630"' in result.path.read_text(encoding="utf-8")

    def test_reimport_of_the_same_work_is_skipped(self, tmp_path):
        w = OpenAlexSourceWriter()
        first = w.write(_work(), tmp_path)
        second = OpenAlexSourceWriter().write(_work(), tmp_path)
        assert second.status == "skipped"
        assert second.path == first.path
        assert "openalex_id match" in second.reason

    def test_missing_identity_is_an_error(self, tmp_path):
        result = OpenAlexSourceWriter().write(ParsedWork(openalex_id=""), tmp_path)
        assert result.status == "error"
        assert result.reason == "missing openalex_id"

    def test_distinct_works_sharing_a_slug_get_a_suffix(self, tmp_path):
        w = OpenAlexSourceWriter()
        a = w.write(_work(openalex_id="W1", doi="10.1/a"), tmp_path)
        b = w.write(_work(openalex_id="W2", doi="10.1/b"), tmp_path)
        assert b.path.name.endswith("-2.md") and a.path != b.path

    def test_existing_user_file_is_never_overwritten(self, tmp_path):
        sources = tmp_path / "sources"
        sources.mkdir()
        squatter = sources / OpenAlexSourceWriter().generate_slug(_work())
        squatter.write_text("# a user's own source\n", encoding="utf-8")
        result = OpenAlexSourceWriter().write(_work(), tmp_path)
        assert result.path != squatter
        assert squatter.read_text(encoding="utf-8").startswith("# a user's own source")

    def test_plan_creates_no_file(self, tmp_path):
        result = OpenAlexSourceWriter().plan(_work(), tmp_path)
        assert result.status == "imported"
        assert not result.path.exists()

    def test_written_file_round_trips_through_the_dedupe_index(self, tmp_path):
        # The identity we write must be the identity a later run reads back.
        OpenAlexSourceWriter().write(_work(), tmp_path)
        assert OpenAlexSourceWriter().plan(_work(), tmp_path).status == "skipped"


class TestCrossSourceDuplicates:
    """§7.1: preprint/journal records of one paper, and Zotero/OpenAlex overlap."""

    def test_two_openalex_records_sharing_a_doi_are_deduplicated(self, tmp_path):
        # W3113149630 (journal) and W4394646531 (preprint) are the same paper.
        w = OpenAlexSourceWriter()
        first = w.write(_work(openalex_id="W3113149630"), tmp_path)
        second = w.write(_work(openalex_id="W4394646531", title="Neurosymbolic AI: The 3rd Wave"),
                         tmp_path)
        assert second.status == "skipped"
        assert second.path == first.path
        assert "duplicate DOI" in second.reason

    def test_openalex_import_merges_into_an_existing_zotero_source(self, tmp_path):
        zotero_item = {
            "zotero_key": "ABCD1234",
            "title": "Neurosymbolic AI",
            "authors": [{"last": "Garcez", "first": "Artur"}],
            "year": "2023",
            "doi": "10.1007/S10462-023-10448-W",  # upper case; DOIs are case-insensitive
        }
        z = ZoteroWriter().write(zotero_item, tmp_path)
        # The same published work reached via the shared DOI. Zotero writes no
        # sidecar, but OpenAlex is a §7.3 merger (#73): it records its own view in a
        # sidecar beside the Zotero original rather than writing a second file.
        result = OpenAlexSourceWriter().write(_work(), tmp_path)
        assert result.status == "merged"
        assert result.path == z.path
        assert "duplicate DOI" in result.reason
        from factlog.integrations.common.provenance import read_provenance, sidecar_path
        recs = read_provenance(sidecar_path(z.path, tmp_path)).records
        assert [r.type for r in recs] == ["openalex"]

    def test_pmid_match_without_doi(self, tmp_path):
        w = OpenAlexSourceWriter()
        w.write(_work(openalex_id="W1", doi=None, pmid="32738937"), tmp_path)
        second = w.write(_work(openalex_id="W2", doi=None, pmid="32738937"), tmp_path)
        assert second.status == "skipped"
        assert "duplicate PMID" in second.reason

    def test_works_without_doi_or_pmid_are_not_deduplicated(self, tmp_path):
        # 15% of live works have no DOI; they must still import.
        w = OpenAlexSourceWriter()
        a = w.write(_work(openalex_id="W1", doi=None, pmid=None), tmp_path)
        b = w.write(_work(openalex_id="W2", doi=None, pmid=None), tmp_path)
        assert a.status == b.status == "imported"
        assert a.path != b.path

    def test_skip_duplicates_false_writes_both(self, tmp_path):
        w = OpenAlexSourceWriter(skip_duplicates=False)
        a = w.write(_work(openalex_id="W1"), tmp_path)
        b = w.write(_work(openalex_id="W2"), tmp_path)
        assert b.status == "imported" and a.path != b.path


class TestSharedCoreParity:
    """The two writers must agree on the machinery they now share."""

    @pytest.mark.parametrize("writer", [OpenAlexSourceWriter(), ZoteroWriter()])
    def test_both_expose_the_same_contract(self, writer):
        for method in ("plan", "write", "render", "generate_slug"):
            assert callable(getattr(writer, method))

    def test_identity_keys_differ(self):
        assert OpenAlexSourceWriter.identity_key == "openalex_id"
        assert ZoteroWriter.identity_key == "zotero_key"
