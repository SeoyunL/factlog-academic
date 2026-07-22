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
  ``cli.main``, because the in-memory helpers skip the front-matter read
  entirely, which is where a large author list used to cost a record its
  metadata (#395, fixed).
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from factlog.bibtex import (
    _ENTRY_TYPES,
    _FRONT_MATTER_CHUNK_CHARS,
    parse_front_matter,
    read_front_matter,
    to_bibtex,
)
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
_WHOLE_BOOK = ("book", "series", "book", "collection-title")
_PREPRINT = ("misc", "howpublished", "article", "container-title")

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
    "dataset": ("misc", "howpublished", "dataset", "container-title"),
    "software": ("misc", "howpublished", "software", "container-title"),
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
        assert _resolution_of(fm) == ("misc", "howpublished", "document", "container-title")


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

    def test_preprints_carry_genre_and_others_do_not(self):
        """CSL 1.0.2 has no `preprint` type, so the status rides in `genre`.

        Scoped to actual preprints: datasets, software and unmapped types share
        the preprint *venue* treatment (INFORMAL) but are not preprints, and
        labelling them so would assert something the record does not say.
        """
        assert to_csl(_arxiv_fm(), "k")["genre"] == "Preprint"
        assert to_csl(_zotero_fm("preprint"), "k")["genre"] == "Preprint"
        assert to_csl(_openalex_fm(work_type="preprint"), "k")["genre"] == "Preprint"
        for fm in (_zotero_fm(), _pubmed_fm(), _openalex_fm(),
                   _openalex_fm(work_type="dataset"),
                   _openalex_fm(work_type="software"),
                   _zotero_fm("holotape")):
            assert "genre" not in to_csl(fm, "k")


class TestVenueValueIsNeverDiscarded:
    """Every role *moves* the venue to a differently-named field; none drops it.

    A `SERIES` role once mapped to "" in both exporters, so a Zotero `book`
    carrying a venue lost the value outright in BibTeX *and* CSL — worse than
    the misfiled `journal` this change set out to fix, because a misfiled value
    can still be recovered by hand and a discarded one cannot.
    """

    @pytest.mark.parametrize("source_type", sorted(ZOTERO_EXPECTED))
    def test_zotero_venue_survives_into_some_field(self, source_type):
        self._assert_venue_survives(_fm_for(source_type, "zotero"))

    @pytest.mark.parametrize("source_type", sorted(OPENALEX_EXPECTED))
    def test_openalex_venue_survives_into_some_field(self, source_type):
        self._assert_venue_survives(_fm_for(source_type, "openalex"))

    @staticmethod
    def _assert_venue_survives(fm: dict) -> None:
        assert _bibtex_venue_field(fm), f"BibTeX dropped the venue for {fm}"
        assert _csl_venue_field(fm), f"CSL dropped the venue for {fm}"

    def test_no_role_maps_to_an_empty_field_name(self):
        from factlog.bibtex import _VENUE_FIELDS as BIB_FIELDS
        from factlog.csl import _VENUE_FIELDS as CSL_FIELDS

        assert all(BIB_FIELDS.values()), "a BibTeX role discards the venue"
        assert all(CSL_FIELDS.values()), "a CSL role discards the venue"


class TestExportersAgree:
    """Both exporters must resolve the same *role*, on types and on venues.

    Checked on resolved output rather than on the static maps, because the two
    once applied the `journal` inference on different conditions and a static
    comparison could not see it.

    Agreeing on the role does not mean agreeing on the field name: for INFORMAL
    the exporters deliberately diverge (`howpublished` vs `container-title`),
    since standard BibTeX gives `@misc` only `howpublished` while CSL can state
    the venue accurately. The same divergence-by-design already exists for
    dataset/software, where BibTeX has the coarser vocabulary.
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
         ("misc", "howpublished", "dataset", "container-title")),
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

    The in-memory helpers above never exercise the front-matter read, which is
    where a large author list used to silently cost a record its metadata (#395).
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

    def test_large_author_list_keeps_the_whole_front_matter(self, tmp_path):
        """#395, fixed: the front-matter read now stops at the closing fence.

        This assertion was inverted — #384 pinned the defect here rather than
        fixing it. `read_front_matter` used to read only the first 4096 bytes,
        and the arXiv writer emits a single long `authors:` line before
        `year`/`journal`/`preprint`, so a large collaboration pushed all of them
        out of the window: the record kept `arxiv_id`/`arxiv_version`/`authors`/
        `title` alone and lost author, year, venue and its type key.

        The fixture's block is deliberately larger than the old window, so this
        test is only meaningful while that stays true — hence the size assertion.
        """
        path = tmp_path / "big.md"
        text = _arxiv_md(journal_ref="Nature 585, 357 (2020)", n_authors=200)
        path.write_text(text, encoding="utf-8")
        # Guards the guard: a block under 4096 bytes would pass even unfixed.
        assert len(text.split("---")[1].encode()) > 4096

        fm = read_front_matter(path)
        # Every key the writer emitted survives, in particular the ones that used
        # to fall past the window.
        for key in ("year", "journal", "preprint", "primary_category", "imported_from"):
            assert key in fm, f"{key} lost to truncation"
        assert isinstance(fm["authors"], list) and len(fm["authors"]) == 200

        entry = to_bibtex(fm, "big")
        # The type key survived, so the record is still resolved as a preprint
        # rather than falling back to a bare default.
        assert entry.startswith("@misc{big,")
        assert set(_bibtex_fields(entry)) == {"author", "title", "year", "howpublished"}

    def test_front_matter_read_stops_at_the_closing_fence(self, tmp_path):
        """A `---` in the *body* is not a fence, and the body is not parsed.

        Pins the two halves of the fence-terminated read: keys below the closing
        fence never enter the dict, and a body large enough to dwarf any fixed
        window costs nothing because the read stops early.
        """
        path = tmp_path / "fenced.md"
        body = "\n".join(f"body_key_{i}: not front matter" for i in range(20_000))
        path.write_text(
            f'---\ntitle: "T"\nyear: "2020"\n---\n\n{body}\n', encoding="utf-8",
        )
        assert path.stat().st_size > 4096

        assert read_front_matter(path) == {"title": "T", "year": "2020"}

    def test_file_without_opening_fence_has_no_front_matter(self, tmp_path):
        """An ingest conversion carries an HTML provenance comment, not YAML.

        `cmd_export` relies on `{}` here to report the file as skipped.
        """
        path = tmp_path / "converted.md"
        path.write_text("<!-- provenance -->\n\ntitle: not front matter\n", encoding="utf-8")
        assert read_front_matter(path) == {}

    def test_unreadable_path_is_reported_as_no_front_matter(self, tmp_path):
        """OSError degrades to `{}` (skipped), it does not abort the export."""
        assert read_front_matter(tmp_path / "does-not-exist.md") == {}

    @staticmethod
    def _fenced_at(offset: int) -> str:
        """A source whose closing fence starts exactly at byte `offset`."""
        head = '---\ntitle: "T"\n'
        pad = "p: " + "x" * (offset - len(head) - 4) + "\n"
        assert len(head + pad) == offset
        return head + pad + '\n---\n\nLEAK: leaked\n'

    @pytest.mark.parametrize("offset", range(-3, 4))
    def test_fence_straddling_a_chunk_boundary_is_found(self, tmp_path, offset):
        """The closing fence is found even when it spans two reads.

        `read_front_matter` pulls `_FRONT_MATTER_CHUNK_CHARS` at a time, so a
        `\\n---` sitting astride a boundary is split across them. The loop
        re-scans the *accumulated* text rather than the latest chunk for exactly
        this reason, and nothing else here pins that: the 200-author block
        (7904B) fits in the first read, so its loop never iterates at all.

        Without the accumulation the fence is missed, `title` vanishes with the
        discarded chunk, and body keys past the fence leak into the front matter.

        The offsets are computed *from the constant*, so retuning the chunk size
        moves the fixture with it instead of silently aiming at nothing.
        """
        path = tmp_path / f"straddle{offset}.md"
        path.write_text(self._fenced_at(_FRONT_MATTER_CHUNK_CHARS + offset), encoding="utf-8")

        fm = read_front_matter(path)
        assert fm["title"] == "T"
        assert "LEAK" not in fm, "body key past the closing fence leaked in"

    def test_chunk_size_must_cover_the_opening_fence(self, tmp_path):
        """A chunk under 3 chars cannot see `---`, so every source reads as empty.

        Pins the lower bound the constant's comment documents: the opening-fence
        test runs on the *first* read alone, so a chunk of 1 or 2 makes
        `startswith("---")` false for a perfectly well-formed file.
        """
        assert _FRONT_MATTER_CHUNK_CHARS >= 3

    @staticmethod
    def _chars_read(monkeypatch) -> list[int]:
        """Instrument `Path.open` so a test can assert how much a read cost."""
        total = [0]
        real_open = Path.open

        def counting_open(self, *args, **kwargs):
            handle = real_open(self, *args, **kwargs)
            real_read = handle.read

            def read(size=-1):
                data = real_read(size)
                total[0] += len(data)
                return data

            handle.read = read
            return handle

        monkeypatch.setattr(Path, "open", counting_open)
        return total

    def test_missing_opening_fence_does_not_read_the_body(self, tmp_path, monkeypatch):
        """The opening-fence check is a read budget, not just a shortcut.

        `cmd_export` walks every `.md` under both source roots, including ingest
        conversions that carry an HTML provenance comment instead of YAML. With
        no opening fence there is no closing fence to find either, so without
        this check the search would run to EOF on every such file — reading a
        whole body to conclude it has nothing.
        """
        path = tmp_path / "converted.md"
        path.write_text("<!-- provenance -->\n" + "filler line\n" * 200_000, encoding="utf-8")
        size = path.stat().st_size
        assert size > 1_000_000

        read = self._chars_read(monkeypatch)
        assert read_front_matter(path) == {}
        assert read[0] < size / 10, f"read {read[0]} chars of a {size}-byte body"

    def test_unclosed_front_matter_is_bounded(self, tmp_path, monkeypatch):
        """An opening fence that is never closed stops at the cap, not at EOF."""
        path = tmp_path / "unclosed.md"
        path.write_text('---\ntitle: "T"\n' + "filler: line\n" * 400_000, encoding="utf-8")
        size = path.stat().st_size
        assert size > 2 * (1 << 20)

        read = self._chars_read(monkeypatch)
        assert read_front_matter(path)["title"] == "T"
        assert read[0] <= (1 << 20) + 8192, f"read {read[0]} chars of a {size}-byte file"

    def test_large_author_list_still_emits_only_defined_fields(self, tmp_path):
        text = self._export(tmp_path, "bibtex", extra={
            "big.md": _arxiv_md(journal_ref="Nature 585, 357 (2020)", n_authors=200),
        })
        entries = [e for e in re.split(r"(?=^@)", text, flags=re.M) if e.startswith("@")]
        for entry in entries:
            allowed = STANDARD_BIBTEX_FIELDS[_entry_of(entry)] | TOLERATED_EXTENSIONS
            assert set(_bibtex_fields(entry)) <= allowed, entry
