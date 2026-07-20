# SPDX-License-Identifier: Apache-2.0
"""Entry/CSL type resolution across all four integrations (#384).

Each integration records the work type under a different front-matter key, but
both exporters used to read only Zotero's ``item_type`` — so every OpenAlex,
arXiv and PubMed record exported as ``@misc``/``"document"``, and nine of a
25-record KB came out as ``@misc`` *carrying a* ``journal`` *field*, which is
not a valid standard-BibTeX pairing.

These tests drive the real ``SourceWriter``s rather than hand-written front
matter, so renaming a key in a writer fails here instead of silently degrading
the export again.
"""
from __future__ import annotations

from datetime import date

from factlog.bibtex import parse_front_matter, resolve_source_type, to_bibtex
from factlog.csl import to_csl
from factlog.integrations.arxiv.source_writer import ArxivSourceWriter
from factlog.integrations.arxiv.work_parser import ParsedArxivWork
from factlog.integrations.openalex.source_writer import OpenAlexSourceWriter
from factlog.integrations.openalex.work_parser import ParsedWork
from factlog.integrations.pubmed.source_writer import PubMedSourceWriter
from factlog.integrations.pubmed.work_parser import ParsedPubMedWork
from factlog.integrations.zotero.item_parser import parse_item
from factlog.integrations.zotero.source_writer import SourceWriter as ZoteroSourceWriter


def _zotero_fm(item_type: str = "journalArticle") -> dict:
    parsed = parse_item({
        "key": "ABCD1234",
        "data": {
            "itemType": item_type,
            "title": "Zotero journal article",
            "creators": [{"creatorType": "author", "lastName": "Kim", "firstName": "M"}],
            "date": "2005-03-01",
            "publicationTitle": "Chest",
            "DOI": "10.1378/chest.x",
        },
    })
    return parse_front_matter(ZoteroSourceWriter().render(parsed))


def _openalex_fm(work_type: str = "article", journal: str | None = "The Lancet") -> dict:
    parsed = ParsedWork(
        openalex_id="W2038858046",
        title="Ileal-lymphoid-nodular hyperplasia",
        authors=("A J Wakefield",),
        year=1998,
        journal=journal,
        doi="10.1016/s0140-6736(97)11096-0",
        pmid="9500320",
        work_type=work_type,
        abstract="An OpenAlex record.",
    )
    return parse_front_matter(OpenAlexSourceWriter().render(parsed))


def _arxiv_fm(journal_ref: str | None = None) -> dict:
    parsed = ParsedArxivWork(
        arxiv_id="2012.05876",
        version=1,
        title="Neurosymbolic AI: the 3rd wave",
        authors=("Artur d'Avila Garcez",),
        abstract="An arXiv deposit.",
        primary_category="cs.AI",
        categories=("cs.AI",),
        submitted=date(2020, 12, 10),
        last_updated=date(2020, 12, 10),
        journal_ref=journal_ref,
    )
    return parse_front_matter(ArxivSourceWriter().render(parsed))


def _pubmed_fm() -> dict:
    parsed = ParsedPubMedWork(
        pmid="16354850",
        title="Omega-3 fatty acids in COPD",
        authors=("Matsuyama W",),
        journal="Chest",
        year=2005,
        doi="10.1378/chest.128.6.3817",
        abstract="A PubMed record.",
    )
    return parse_front_matter(PubMedSourceWriter().render(parsed))


ALL_SOURCES = {
    "zotero": _zotero_fm,
    "openalex": _openalex_fm,
    "arxiv": _arxiv_fm,
    "pubmed": _pubmed_fm,
}


class TestWritersStillUseTheKeysWeRead:
    """The premise of the fix: each writer emits the key the resolver probes."""

    def test_zotero_emits_item_type(self):
        assert _zotero_fm()["item_type"] == "journalArticle"

    def test_openalex_emits_type(self):
        assert _openalex_fm()["type"] == "article"

    def test_arxiv_emits_preprint_flag(self):
        assert _arxiv_fm()["preprint"] is True

    def test_pubmed_emits_no_type_key_only_journal(self):
        fm = _pubmed_fm()
        assert "item_type" not in fm and "type" not in fm and "preprint" not in fm
        assert fm["journal"] == "Chest"


class TestResolveSourceType:
    def test_probes_each_integrations_key(self):
        assert resolve_source_type(_zotero_fm()) == "journalArticle"
        assert resolve_source_type(_openalex_fm()) == "article"
        assert resolve_source_type(_arxiv_fm()) == "preprint"
        # PubMed answers no key; the `journal` fallback is the caller's job.
        assert resolve_source_type(_pubmed_fm()) is None

    def test_item_type_wins_so_zotero_kbs_are_unchanged(self):
        fm = {"item_type": "book", "type": "article", "preprint": True}
        assert resolve_source_type(fm) == "book"

    def test_type_beats_the_preprint_flag(self):
        assert resolve_source_type({"type": "article", "preprint": True}) == "article"

    def test_blank_and_non_string_keys_fall_through(self):
        assert resolve_source_type({"item_type": "  ", "type": "article"}) == "article"
        assert resolve_source_type({"item_type": 7, "preprint": True}) == "preprint"
        # `preprint: false` is not an answer, it is an absence of one.
        assert resolve_source_type({"preprint": False}) is None
        assert resolve_source_type({}) is None


class TestBibtexEntryTypes:
    def test_each_integration_gets_a_typed_entry(self):
        assert to_bibtex(_zotero_fm(), "k").startswith("@article{")
        assert to_bibtex(_openalex_fm(), "k").startswith("@article{")
        assert to_bibtex(_pubmed_fm(), "k").startswith("@article{")
        # An arXiv deposit with no journal_ref is genuinely unpublished.
        assert to_bibtex(_arxiv_fm(), "k").startswith("@misc{")

    def test_openalex_conference_paper(self):
        out = to_bibtex(_openalex_fm(work_type="conference-paper", journal=None), "k")
        assert out.startswith("@inproceedings{")

    def test_openalex_preprint_without_journal_stays_misc(self):
        out = to_bibtex(_openalex_fm(work_type="preprint", journal=None), "k")
        assert out.startswith("@misc{")

    def test_journal_promotes_an_otherwise_misc_entry(self):
        # An unmapped OpenAlex type that still names a journal must not emit
        # the invalid @misc-with-journal pairing.
        out = to_bibtex(_openalex_fm(work_type="editorial"), "k")
        assert out.startswith("@article{") and "journal = {The Lancet}," in out

    def test_arxiv_with_journal_ref_is_cited_as_article(self):
        out = to_bibtex(_arxiv_fm(journal_ref="Nature 585, 357 (2020)"), "k")
        assert out.startswith("@article{")

    def test_no_misc_entry_ever_carries_a_journal_field(self):
        """The defect's signature: 9/25 entries were @misc *with* a journal."""
        variants = [
            _zotero_fm(), _zotero_fm("preprint"), _zotero_fm("weird"),
            _openalex_fm(), _openalex_fm(work_type="preprint"),
            _openalex_fm(work_type="editorial"), _openalex_fm(work_type="retraction"),
            _arxiv_fm(), _arxiv_fm(journal_ref="Nature 585, 357 (2020)"),
            _pubmed_fm(),
        ]
        misc_with_journal = [
            fm for fm in variants
            if to_bibtex(fm, "k").startswith("@misc{") and fm.get("journal")
        ]
        assert misc_with_journal == []


class TestCslTypes:
    def test_each_integration_gets_a_typed_item(self):
        assert to_csl(_zotero_fm(), "k")["type"] == "article-journal"
        assert to_csl(_openalex_fm(), "k")["type"] == "article-journal"
        assert to_csl(_pubmed_fm(), "k")["type"] == "article-journal"
        assert to_csl(_arxiv_fm(), "k")["type"] == "article"  # preprint

    def test_openalex_conference_paper(self):
        item = to_csl(_openalex_fm(work_type="conference-paper", journal=None), "k")
        assert item["type"] == "paper-conference"

    def test_journal_promotes_an_otherwise_untyped_item(self):
        assert to_csl(_openalex_fm(work_type="editorial"), "k")["type"] == "article-journal"

    def test_no_document_item_ever_carries_a_container_title(self):
        for build in ALL_SOURCES.values():
            item = to_csl(build(), "k")
            assert not (item["type"] == "document" and item.get("container-title"))


class TestBibtexAndCslAgree:
    """The two exporters map the same vocabulary, so they must not disagree."""

    _EQUIVALENT = {
        ("article", "article-journal"), ("inproceedings", "paper-conference"),
        ("book", "book"), ("incollection", "chapter"), ("techreport", "report"),
        ("phdthesis", "thesis"), ("misc", "article"), ("misc", "document"),
    }

    def test_every_known_type_maps_consistently(self):
        from factlog.bibtex import _ENTRY_TYPES
        from factlog.csl import _CSL_TYPES

        assert set(_ENTRY_TYPES) == set(_CSL_TYPES)
        for source_type, entry in _ENTRY_TYPES.items():
            pair = (entry, _CSL_TYPES[source_type])
            assert pair in self._EQUIVALENT, f"{source_type} maps to {pair}"
