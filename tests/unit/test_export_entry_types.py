# SPDX-License-Identifier: Apache-2.0
"""Work-type and venue-field resolution across all four integrations (#384).

Each integration records the work type under a different front-matter key, but
both exporters used to read only Zotero's ``item_type`` — so every OpenAlex,
arXiv and PubMed record exported as ``@misc``/``"document"``, and nine of a
25-record KB came out as ``@misc`` *carrying a* ``journal`` *field*, which is
not a valid standard-BibTeX pairing.

Four things are pinned here, because each failed differently:

* the *keys* (`TestWritersStillUseTheKeysWeRead`) — driven through the real
  ``SourceWriter``s, so renaming a key in a writer fails here instead of
  silently degrading the export again;
* the *types* (`TestTypeMaps`) — every mapping asserted against an explicit
  expected row, so an entry cannot be added or neutered without a test moving;
* the *venue field* (`TestVenueFieldValidity`) — standard BibTeX scopes
  `journal` to `@article` alone, so the pairing is checked against each entry
  type's own field list rather than against `@misc` as a special case;
* the *export path* (`TestExportPathOnDisk`) — through files on disk and
  ``cli.main``, because the in-memory helpers skip the front-matter read that
  truncates at 4096 bytes (#395).
"""
from __future__ import annotations

import json
import re
from datetime import date

import pytest

from factlog.bibtex import _ENTRY_TYPES, parse_front_matter, read_front_matter, to_bibtex
from factlog.csl import _CSL_TYPES, to_csl
from factlog.export_types import (
    resolve_source_type,
    should_promote_to_journal_type,
    venue_role,
)
from factlog.integrations.arxiv.source_writer import ArxivSourceWriter
from factlog.integrations.arxiv.work_parser import ParsedArxivWork
from factlog.integrations.openalex.api_client import WORK_TYPES
from factlog.integrations.openalex.source_writer import OpenAlexSourceWriter
from factlog.integrations.openalex.work_parser import ParsedWork
from factlog.integrations.pubmed.source_writer import PubMedSourceWriter
from factlog.integrations.pubmed.work_parser import ParsedPubMedWork
from factlog.integrations.zotero.item_parser import parse_item
from factlog.integrations.zotero.source_writer import SourceWriter as ZoteroSourceWriter

# --------------------------------------------------------------------------
# Records built by the real writers, so the keys under test are the keys that
# actually reach a KB.
# --------------------------------------------------------------------------


def _zotero_md(item_type: str = "journalArticle", journal: str = "Chest") -> str:
    parsed = parse_item({
        "key": "ABCD1234",
        "data": {
            "itemType": item_type,
            "title": "A Zotero record",
            "creators": [{"creatorType": "author", "lastName": "Kim", "firstName": "M"}],
            "date": "2005-03-01",
            "publicationTitle": journal,
            "DOI": "10.1378/chest.x",
        },
    })
    return ZoteroSourceWriter().render(parsed)


def _openalex_md(work_type: str = "article", journal: str | None = "The Lancet") -> str:
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
    return OpenAlexSourceWriter().render(parsed)


def _arxiv_md(journal_ref: str | None = None, n_authors: int = 1) -> str:
    parsed = ParsedArxivWork(
        arxiv_id="2012.05876",
        version=1,
        title="Neurosymbolic AI: the 3rd wave",
        authors=tuple(f"Author {i} of a large collaboration" for i in range(n_authors)),
        abstract="An arXiv deposit.",
        primary_category="cs.AI",
        categories=("cs.AI",),
        submitted=date(2020, 12, 10),
        last_updated=date(2020, 12, 10),
        journal_ref=journal_ref,
    )
    return ArxivSourceWriter().render(parsed)


def _pubmed_md() -> str:
    parsed = ParsedPubMedWork(
        pmid="16354850",
        title="Omega-3 fatty acids in COPD",
        authors=("Matsuyama W",),
        journal="Chest",
        year=2005,
        doi="10.1378/chest.128.6.3817",
        abstract="A PubMed record.",
    )
    return PubMedSourceWriter().render(parsed)


def _zotero_fm(item_type: str = "journalArticle", journal: str = "Chest") -> dict:
    return parse_front_matter(_zotero_md(item_type, journal))


def _openalex_fm(work_type: str = "article", journal: str | None = "The Lancet") -> dict:
    return parse_front_matter(_openalex_md(work_type, journal))


def _arxiv_fm(journal_ref: str | None = None) -> dict:
    return parse_front_matter(_arxiv_md(journal_ref))


def _pubmed_fm() -> dict:
    return parse_front_matter(_pubmed_md())


ALL_SOURCES = {
    "zotero": _zotero_fm,
    "openalex": _openalex_fm,
    "arxiv": _arxiv_fm,
    "pubmed": _pubmed_fm,
}

# --------------------------------------------------------------------------
# The expected resolution, stated once, as
#   type -> (BibTeX entry, BibTeX venue field, CSL type, CSL venue field)
# An empty venue field means the venue is omitted. `TestTypeMaps` drives every
# row through the public exporters, so neutering a row fails a named test.
# --------------------------------------------------------------------------

_PERIODICAL = ("article", "journal", "article-journal", "container-title")
_IN_BOOK = ("incollection", "booktitle", "chapter", "container-title")
_IN_PROC = ("inproceedings", "booktitle", "paper-conference", "container-title")
_REPORT = ("techreport", "institution", "report", "publisher")
_THESIS = ("phdthesis", "school", "thesis", "publisher")
_WHOLE_BOOK = ("book", "", "book", "")
_PREPRINT = ("misc", "howpublished", "article", "publisher")

# Zotero itemType
ZOTERO_EXPECTED = {
    "journalArticle": _PERIODICAL,
    "magazineArticle": ("article", "journal", "article-magazine", "container-title"),
    "newspaperArticle": ("article", "journal", "article-newspaper", "container-title"),
    "conferencePaper": _IN_PROC,
    "book": _WHOLE_BOOK,
    "bookSection": _IN_BOOK,
    "encyclopediaArticle": ("incollection", "booktitle",
                            "entry-encyclopedia", "container-title"),
    "dictionaryEntry": ("incollection", "booktitle",
                        "entry-dictionary", "container-title"),
    "report": _REPORT,
    "thesis": _THESIS,
    "preprint": _PREPRINT,
}

# OpenAlex work type
OPENALEX_EXPECTED = {
    "article": _PERIODICAL,
    "review": _PERIODICAL,
    "book-review": _PERIODICAL,
    "letter": _PERIODICAL,
    "editorial": _PERIODICAL,
    "erratum": _PERIODICAL,
    "retraction": _PERIODICAL,
    "data-paper": _PERIODICAL,
    "conference-paper": _IN_PROC,
    "book-chapter": _IN_BOOK,
    "book-section": _IN_BOOK,
    "reference-entry": ("incollection", "booktitle",
                        "entry-encyclopedia", "container-title"),
    "dissertation": _THESIS,
    "report-component": _REPORT,
    "book": _WHOLE_BOOK,
    "report": _REPORT,
    "preprint": _PREPRINT,
    # Standard BibTeX has no @dataset/@software (those are biblatex); CSL does,
    # so these diverge by design — BibTeX is the coarser vocabulary, not a
    # contradiction. Both still place the venue in the same role (INFORMAL).
    "dataset": ("misc", "howpublished", "dataset", "publisher"),
    "software": ("misc", "howpublished", "software", "publisher"),
}

ALL_EXPECTED = {**ZOTERO_EXPECTED, **OPENALEX_EXPECTED}

# The fields standard BibTeX defines per entry type (required + optional, from
# the BibTeX manual). `journal` appears under @article ONLY — which is why the
# venue field is chosen per entry type rather than special-cased for @misc.
STANDARD_BIBTEX_FIELDS = {
    "article": {"author", "title", "journal", "year",
                "volume", "number", "pages", "month", "note"},
    "book": {"author", "editor", "title", "publisher", "year", "volume",
             "number", "series", "address", "edition", "month", "note"},
    "inproceedings": {"author", "title", "booktitle", "year", "editor", "volume",
                      "number", "series", "pages", "address", "month",
                      "organization", "publisher", "note"},
    "incollection": {"author", "title", "booktitle", "publisher", "year", "editor",
                     "volume", "number", "series", "type", "chapter", "pages",
                     "address", "edition", "month", "note"},
    "techreport": {"author", "title", "institution", "year",
                   "type", "number", "address", "month", "note"},
    "phdthesis": {"author", "title", "school", "year",
                  "type", "address", "month", "note"},
    "misc": {"author", "title", "howpublished", "month", "year", "note"},
}
# Not in the 1985 manual, but understood by every modern style and by biber.
TOLERATED_EXTENSIONS = {"doi"}

_VENUE_VALUE = "A Distinctive Venue Name"


def _fm_for(source_type: str, vocabulary: str) -> dict:
    """A fully populated record of one type, for field-level assertions."""
    fm = {"title": "A Title", "authors": ["Kim, M"], "year": "2005",
          "journal": _VENUE_VALUE, "doi": "10.1/x"}
    if vocabulary == "zotero":
        fm["item_type"] = source_type
    else:
        fm["type"] = source_type
        fm["imported_from"] = "openalex"
    return fm


def _entry_of(out: str) -> str:
    return out.split("{", 1)[0].lstrip("@")


def _bibtex_fields(out: str) -> dict[str, str]:
    return {m.group(1): m.group(2)
            for m in re.finditer(r"^  (\w+) = \{(.*)\},$", out, flags=re.M)}


def _bibtex_venue_field(fm: dict) -> str:
    """Which BibTeX field carries the record's venue ('' if omitted)."""
    for name, value in _bibtex_fields(to_bibtex(fm, "k")).items():
        if value == fm.get("journal"):
            return name
    return ""


def _csl_venue_field(fm: dict) -> str:
    """Which CSL variable carries the record's venue ('' if omitted)."""
    for name, value in to_csl(fm, "k").items():
        if value == fm.get("journal"):
            return name
    return ""


def _resolution_of(fm: dict) -> tuple[str, str, str, str]:
    return (_entry_of(to_bibtex(fm, "k")), _bibtex_venue_field(fm),
            to_csl(fm, "k")["type"], _csl_venue_field(fm))


class TestWritersStillUseTheKeysWeRead:
    """The premise of the fix: each writer emits the key the resolver probes."""

    def test_zotero_emits_item_type(self):
        assert _zotero_fm()["item_type"] == "journalArticle"

    def test_openalex_emits_type_and_its_provenance_marker(self):
        fm = _openalex_fm()
        assert fm["type"] == "article"
        # `type` is trusted only alongside this marker; see TestResolveSourceType.
        assert fm["imported_from"] == "openalex"

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
        # PubMed answers no key; the `journal` inference is a separate decision.
        assert resolve_source_type(_pubmed_fm()) is None

    def test_item_type_is_probed_first(self):
        fm = {"item_type": "book", "type": "article",
              "imported_from": "openalex", "preprint": True}
        assert resolve_source_type(fm) == "book"

    def test_type_beats_the_preprint_flag(self):
        fm = {"type": "article", "imported_from": "openalex", "preprint": True}
        assert resolve_source_type(fm) == "article"

    def test_bare_type_is_trusted_only_on_an_openalex_record(self):
        """`type` is the ledger's RESERVED key for the source name (#73), so a
        front-matter `type` is read only where the OpenAlex writer put it."""
        assert resolve_source_type({"type": "article"}) is None
        assert resolve_source_type({"type": "article", "imported_from": "zotero"}) is None
        assert resolve_source_type(
            {"type": "article", "imported_from": "openalex"}) == "article"

    def test_preprint_flag_must_be_the_boolean_true(self):
        """Not merely truthy. The front-matter parser only lowercases `true`/
        `false` into booleans, so `preprint: False` survives as the *string*
        `'False'` — which is truthy and would otherwise flag a non-preprint."""
        assert resolve_source_type({"preprint": "False"}) is None
        assert resolve_source_type({"preprint": "no"}) is None
        assert resolve_source_type({"preprint": 1}) is None
        assert resolve_source_type({"preprint": False}) is None
        assert resolve_source_type({"preprint": True}) == "preprint"

    def test_blank_and_non_string_keys_fall_through(self):
        fm = {"item_type": "  ", "type": "article", "imported_from": "openalex"}
        assert resolve_source_type(fm) == "article"
        assert resolve_source_type({"item_type": 7, "preprint": True}) == "preprint"
        assert resolve_source_type({}) is None


class TestShouldPromoteToJournalType:
    """The inference fires only where nothing was declared (the #384 narrowing)."""

    def test_fires_only_when_no_key_declared_a_type(self):
        assert should_promote_to_journal_type({"journal": "Chest"}, None) is True
        assert should_promote_to_journal_type({}, None) is False

    def test_never_overrides_a_declared_type(self):
        # Zotero fills `journal` from publicationTitle for ANY item type, so a
        # magazine article names a journal without being one.
        assert should_promote_to_journal_type(
            {"journal": "The Economist"}, "magazineArticle") is False
        # And an arXiv deposit stays a preprint once published (#60).
        assert should_promote_to_journal_type({"journal": "Nature"}, "preprint") is False


class TestTypeMaps:
    """Every mapping asserted against an expected row, via the public exporters."""

    def test_maps_cover_exactly_the_expected_vocabulary(self):
        assert set(_ENTRY_TYPES) == set(ALL_EXPECTED)
        assert set(_CSL_TYPES) == set(ALL_EXPECTED)

    @pytest.mark.parametrize(("item_type", "expected"), sorted(ZOTERO_EXPECTED.items()))
    def test_zotero_vocabulary(self, item_type, expected):
        assert _resolution_of(_fm_for(item_type, "zotero")) == expected

    @pytest.mark.parametrize(("work_type", "expected"), sorted(OPENALEX_EXPECTED.items()))
    def test_openalex_vocabulary(self, work_type, expected):
        assert _resolution_of(_fm_for(work_type, "openalex")) == expected

    def test_openalex_keys_are_real_openalex_work_types(self):
        """The vocabulary has one authority (api_client.WORK_TYPES); a typo here
        would be a dead map entry that no record can ever match."""
        assert set(OPENALEX_EXPECTED) <= set(WORK_TYPES)

    def test_unknown_type_falls_back_to_a_universally_valid_pairing(self):
        fm = _fm_for("holotape", "zotero")
        # @misc + howpublished is valid whatever the unknown type turns out to be.
        assert _resolution_of(fm) == ("misc", "howpublished", "document", "publisher")


class TestVenueFieldValidity:
    """No entry may carry a field its own entry type does not define.

    Fixing only the `@misc` + `journal` pairing left the same defect on five
    other types: standard BibTeX defines `journal` for `@article` alone, so
    `@book`/`@incollection`/`@inproceedings`/`@techreport`/`@phdthesis` were all
    emitting a field their type does not have. `@inproceedings`/`@incollection`
    additionally require `booktitle`, so the venue was both misfiled and missing.
    """

    @pytest.mark.parametrize("source_type", sorted(ZOTERO_EXPECTED))
    def test_zotero_entries_emit_only_defined_fields(self, source_type):
        self._assert_fields_defined(_fm_for(source_type, "zotero"))

    @pytest.mark.parametrize("source_type", sorted(OPENALEX_EXPECTED))
    def test_openalex_entries_emit_only_defined_fields(self, source_type):
        self._assert_fields_defined(_fm_for(source_type, "openalex"))

    def test_records_from_every_integration_emit_only_defined_fields(self):
        for build in ALL_SOURCES.values():
            self._assert_fields_defined(build())
        self._assert_fields_defined(_arxiv_fm(journal_ref="Nature 585, 357 (2020)"))

    @staticmethod
    def _assert_fields_defined(fm: dict) -> None:
        out = to_bibtex(fm, "k")
        entry = _entry_of(out)
        allowed = STANDARD_BIBTEX_FIELDS[entry] | TOLERATED_EXTENSIONS
        emitted = set(_bibtex_fields(out))
        assert emitted <= allowed, (
            f"@{entry} emitted {sorted(emitted - allowed)}, "
            f"which standard BibTeX does not define for it"
        )

    def test_journal_is_emitted_only_on_article_entries(self):
        for source_type in ALL_EXPECTED:
            for vocabulary in ("zotero", "openalex"):
                fm = _fm_for(source_type, vocabulary)
                out = to_bibtex(fm, "k")
                if "journal" in _bibtex_fields(out):
                    assert _entry_of(out) == "article"


class TestZoteroOutputIsUnchanged:
    """The fix targets the other three sources; Zotero types must not be hijacked.

    `journal` is filled from `publicationTitle` for every Zotero item type, so an
    inference that ignored the declared type re-typed magazine and newspaper
    articles as journal articles — worse than the old default, since CSL has
    dedicated types for both.
    """

    @pytest.mark.parametrize(("item_type", "entry", "csl"), [
        ("journalArticle", "article", "article-journal"),
        ("magazineArticle", "article", "article-magazine"),
        ("newspaperArticle", "article", "article-newspaper"),
        ("preprint", "misc", "article"),
        ("holotape", "misc", "document"),  # unmapped: still the default
    ])
    def test_declared_type_wins_over_the_journal_field(self, item_type, entry, csl):
        fm = _zotero_fm(item_type)
        assert fm["journal"]  # the field that used to hijack the type
        assert _entry_of(to_bibtex(fm, "k")) == entry
        assert to_csl(fm, "k")["type"] == csl


class TestBibtexEntryTypes:
    def test_each_integration_gets_a_typed_entry(self):
        assert _entry_of(to_bibtex(_zotero_fm(), "k")) == "article"
        assert _entry_of(to_bibtex(_openalex_fm(), "k")) == "article"
        assert _entry_of(to_bibtex(_pubmed_fm(), "k")) == "article"
        # An arXiv deposit is a preprint; #60 says it stays one.
        assert _entry_of(to_bibtex(_arxiv_fm(), "k")) == "misc"

    def test_pubmed_is_typed_purely_from_its_journal(self):
        out = to_bibtex(_pubmed_fm(), "k")
        assert out.startswith("@article{") and "journal = {Chest}," in out

    def test_misc_records_its_venue_as_howpublished(self):
        out = to_bibtex(_arxiv_fm(journal_ref="Nature 585, 357 (2020)"), "k")
        assert out.startswith("@misc{")
        assert "howpublished = {Nature 585, 357 (2020)}," in out
        assert "journal = " not in out

    def test_no_misc_entry_ever_carries_a_journal_field(self):
        """The defect's original signature: 9/25 entries were @misc + journal."""
        variants = [build() for build in ALL_SOURCES.values()]
        variants += [
            _zotero_fm("preprint"), _zotero_fm("magazineArticle"), _zotero_fm("holotape"),
            _openalex_fm(work_type="preprint"), _openalex_fm(work_type="dataset"),
            _arxiv_fm(journal_ref="Nature 585, 357 (2020)"),
        ]
        offenders = [
            fm for fm in variants
            if _entry_of(to_bibtex(fm, "k")) == "misc" and "journal = " in to_bibtex(fm, "k")
        ]
        assert offenders == []


class TestCslTypes:
    def test_each_integration_gets_a_typed_item(self):
        assert to_csl(_zotero_fm(), "k")["type"] == "article-journal"
        assert to_csl(_openalex_fm(), "k")["type"] == "article-journal"
        assert to_csl(_pubmed_fm(), "k")["type"] == "article-journal"
        assert to_csl(_arxiv_fm(), "k")["type"] == "article"  # preprint

    def test_no_document_item_ever_carries_a_container_title(self):
        for build in ALL_SOURCES.values():
            item = to_csl(build(), "k")
            assert not (item["type"] == "document" and item.get("container-title"))


class TestExportersAgree:
    """Both exporters must make the same judgement, on types *and* on venues.

    Checked on resolved output rather than on the static maps: the two once
    applied the `journal` inference on different conditions, and later placed
    the same venue in `howpublished` (BibTeX) and `container-title` (CSL) —
    which pandoc turns into `publisher` vs `container-title`, i.e. two exports
    of one KB contradicting each other about where the work was published.
    """

    @pytest.mark.parametrize(("fm", "expected"), [
        (_zotero_fm(), _PERIODICAL),
        (_zotero_fm("magazineArticle"),
         ("article", "journal", "article-magazine", "container-title")),
        (_zotero_fm("preprint"), _PREPRINT),
        (_zotero_fm("book"), _WHOLE_BOOK),
        (_zotero_fm("thesis"), _THESIS),
        (_openalex_fm(), _PERIODICAL),
        (_openalex_fm(work_type="conference-paper"), _IN_PROC),
        (_openalex_fm(work_type="dataset"),
         ("misc", "howpublished", "dataset", "publisher")),
        (_arxiv_fm(journal_ref="Nature 585, 357 (2020)"), _PREPRINT),
        (_pubmed_fm(), _PERIODICAL),
    ])
    def test_resolution_matches_expectation(self, fm, expected):
        assert _resolution_of(fm) == expected

    def test_venue_role_is_a_single_judgement(self):
        """Both venue-field tables are keyed by the same role, so neither can
        pick a field the other did not agree to."""
        from factlog.bibtex import _VENUE_FIELDS as BIB_FIELDS
        from factlog.csl import _VENUE_FIELDS as CSL_FIELDS

        assert set(BIB_FIELDS) == set(CSL_FIELDS)
        for build in ALL_SOURCES.values():
            fm = build()
            role = venue_role(fm)
            assert (_bibtex_venue_field(fm) or "") == (BIB_FIELDS[role] if fm.get("journal") else "")
            assert (_csl_venue_field(fm) or "") == (CSL_FIELDS[role] if fm.get("journal") else "")

    def test_untyped_record_agrees_too(self):
        assert _resolution_of({"title": "T"}) == ("misc", "", "document", "")


class TestExportPathOnDisk:
    """The real path: files on disk, read through `read_front_matter`, via the CLI.

    The in-memory helpers above never exercise the 4096-byte front-matter read,
    which is where a large author list silently costs a record its metadata.
    """

    @staticmethod
    def _kb(tmp_path, extra: dict[str, str] | None = None):
        sources = tmp_path / "sources"
        sources.mkdir()
        files = {
            "zotero.md": _zotero_md(),
            "openalex.md": _openalex_md(),
            "arxiv.md": _arxiv_md(),
            "pubmed.md": _pubmed_md(),
        }
        files.update(extra or {})
        for name, text in files.items():
            (sources / name).write_text(text, encoding="utf-8")
        return tmp_path

    def _export(self, tmp_path, fmt: str, extra=None) -> str:
        from factlog.cli import main
        kb = self._kb(tmp_path, extra)
        out = tmp_path / f"out.{fmt}"
        assert main(["export", f"--{fmt}", "--target", str(kb), "-o", str(out)]) == 0
        return out.read_text(encoding="utf-8")

    def test_bibtex_distribution_over_a_four_source_kb(self, tmp_path):
        text = self._export(tmp_path, "bibtex")
        entries = sorted(line for line in text.splitlines() if line.startswith("@"))
        assert entries == [
            "@article{openalex,", "@article{pubmed,", "@article{zotero,", "@misc{arxiv,",
        ]

    def test_csl_distribution_over_a_four_source_kb(self, tmp_path):
        items = json.loads(self._export(tmp_path, "csl"))
        assert {i["id"]: i["type"] for i in items} == {
            "zotero": "article-journal", "openalex": "article-journal",
            "pubmed": "article-journal", "arxiv": "article",
        }

    def test_exported_kb_emits_no_undefined_field(self, tmp_path):
        """The issue's acceptance criterion, generalised past @misc and asserted
        on real CLI output: every entry carries only fields its type defines."""
        text = self._export(tmp_path, "bibtex", extra={
            "arxiv-published.md": _arxiv_md(journal_ref="Nature 585, 357 (2020)"),
            "zotero-preprint.md": _zotero_md("preprint"),
            "zotero-magazine.md": _zotero_md("magazineArticle", "The Economist"),
            "zotero-book.md": _zotero_md("book", "A Series"),
            "zotero-chapter.md": _zotero_md("bookSection", "A Companion To Things"),
            "zotero-thesis.md": _zotero_md("thesis", "A University"),
        })
        entries = [e for e in re.split(r"(?=^@)", text, flags=re.M) if e.startswith("@")]
        assert len(entries) == 10
        for entry in entries:
            name = _entry_of(entry)
            allowed = STANDARD_BIBTEX_FIELDS[name] | TOLERATED_EXTENSIONS
            assert set(_bibtex_fields(entry)) <= allowed, entry

    def test_large_author_list_truncates_front_matter(self, tmp_path):
        """Known pre-existing defect, pinned here rather than fixed — see #395.

        `read_front_matter` reads only the first 4096 bytes, and the arXiv writer
        emits a single long `authors:` line before `year`/`journal`/`doi`/
        `preprint`, so a large collaboration pushes all of them out of the
        window. The record keeps only `title` (plus the `arxiv_id`/
        `arxiv_version` that precede `authors`) and loses author, year, venue,
        DOI and its type key. Verified byte-identical on main, so this branch
        neither causes nor worsens it; fixing it means changing the scan window
        or the writer's key order, both outside this change. When #395 lands,
        these assertions flip and should be updated.
        """
        path = tmp_path / "big.md"
        text = _arxiv_md(journal_ref="Nature 585, 357 (2020)", n_authors=200)
        path.write_text(text, encoding="utf-8")
        assert len(text.split("---")[1].encode()) > 4096

        fm = read_front_matter(path)
        assert sorted(fm) == ["arxiv_id", "arxiv_version", "authors", "title"]
        assert set(_bibtex_fields(to_bibtex(fm, "big"))) == {"title"}

    def test_large_author_list_still_emits_only_defined_fields(self, tmp_path):
        text = self._export(tmp_path, "bibtex", extra={
            "big.md": _arxiv_md(journal_ref="Nature 585, 357 (2020)", n_authors=200),
        })
        entries = [e for e in re.split(r"(?=^@)", text, flags=re.M) if e.startswith("@")]
        for entry in entries:
            allowed = STANDARD_BIBTEX_FIELDS[_entry_of(entry)] | TOLERATED_EXTENSIONS
            assert set(_bibtex_fields(entry)) <= allowed, entry
