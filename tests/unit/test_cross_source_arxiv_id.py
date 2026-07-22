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
from factlog.integrations.zotero.item_parser import extract_pmid
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

    def test_an_ascii_pmid_is_only_stripped(self):
        # Digit spelling is handled in TestPmidDigitSpelling; an ASCII id is
        # untouched apart from the strip.
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


class TestDoiDigitSpelling:
    """Where the DOI fold stops: prefix normalized, suffix preserved (#405).

    Under ISO 26324 the registrant code in ``10.<registrant>`` is a decimal
    number, so a non-ASCII spelling of it names the same registrant and must
    produce the same join key — otherwise a full-width DOI from Zotero never
    matches the ASCII one from OpenAlex and one paper imports as two files. The
    suffix is an opaque string, where respelling a character would name a
    *different* identifier, so it is left exactly as written. The boundary is the
    first ``/`` — later slashes are part of the suffix.

    Two properties of the implementation are pinned here as well as the outcome,
    because a mutant that drops either survives the rest of the suite: the guard
    that refuses to fold a head which is not a DOI prefix, and the choice of the
    *first* slash as the split.
    """

    def test_full_width_prefix_collides_with_ascii(self):
        assert normalize_cross_id("doi", "10.１２３４/abc") == "10.1234/abc"

    def test_non_latin_digit_scripts_in_the_prefix_fold(self):
        # The counter-case for NFKC, as in the parser: `\d` matches these but
        # NFKC does not fold them.
        assert normalize_cross_id("doi", "10.٢٣٤/abc") == "10.234/abc"  # Arabic-Indic
        assert normalize_cross_id("doi", "10.२३४/abc") == "10.234/abc"  # Devanagari
        assert normalize_cross_id("doi", "10.１2٣/abc") == "10.123/abc"  # mixed

    def test_directory_code_folds_too(self):
        # `10` is as much a decimal number as the registrant code.
        assert normalize_cross_id("doi", "１０.1234/abc") == "10.1234/abc"

    def test_subdivided_registrant_code_folds(self):
        # DOI Handbook 2.2.2: a registrant may subdivide its code (the handbook's
        # own example is `10.1000.10`). Each part is still a decimal number, so a
        # grammar that accepted only one part would leave these DOIs splitting
        # into two files — the exact defect this fix exists to close.
        assert normalize_cross_id("doi", "10.１０００.１０/xyz") == "10.1000.10/xyz"
        assert normalize_cross_id("doi", "10.1000.10.5/xyz") == "10.1000.10.5/xyz"

    def test_suffix_is_preserved_not_folded(self):
        # The opaque half. Folding here would invent a different identifier.
        assert normalize_cross_id("doi", "10.1234/ａ１b") == "10.1234/ａ１b"

    def test_the_split_is_the_first_slash_not_the_last(self):
        # A DOI suffix may contain slashes (`10.1002/x/y` is ordinary), and every
        # one of them is inside the opaque half. Splitting on the last slash would
        # drag part of the suffix into the folded head.
        assert normalize_cross_id("doi", "10.１２３４/x/y") == "10.1234/x/y"
        assert normalize_cross_id("doi", "10.１２３４/x/１") == "10.1234/x/１"

    def test_suffixes_that_differ_only_in_digit_spelling_stay_distinct_keys(self):
        # The consequence of preserving the suffix, asserted rather than implied:
        # these are two identifiers, not two spellings of one.
        assert normalize_cross_id("doi", "10.1234/abc１") != normalize_cross_id(
            "doi", "10.1234/abc1"
        )

    def test_lowercasing_still_applies_to_the_whole_value(self):
        assert normalize_cross_id("doi", "10.１２３４/ABC") == "10.1234/abc"

    def test_non_digit_lookalikes_do_not_become_a_prefix(self):
        # `²` and `①` are category `No`, never matched by `\d`, so the head does
        # not fold into a DOI prefix and the value is only lowercased. Folding
        # them would invent a registrant the source never named.
        assert normalize_cross_id("doi", "10.1²/abc") == "10.1²/abc"
        assert normalize_cross_id("doi", "10.①/abc") == "10.①/abc"

    def test_a_value_that_is_not_a_doi_prefix_is_left_alone(self):
        # Junk in a hand-edited file must not be quietly rewritten; it simply
        # fails to match anything. The URL form is out of scope either way — it
        # was never a matching key.
        assert normalize_cross_id("doi", "https://doi.org/10.１２３４/abc") == \
            "https://doi.org/10.１２３４/abc"
        assert normalize_cross_id("doi", "not-a-doi") == "not-a-doi"
        assert normalize_cross_id("doi", "10.1234") == "10.1234"

    def test_the_prefix_guard_blocks_a_head_that_is_not_a_doi_prefix(self):
        # These pin the *guard*, which the cases above do not: each head here
        # carries a non-ASCII digit, so dropping the "is it really a DOI prefix?"
        # check would fold it. Only a value whose head is a DOI prefix may be
        # rewritten; a labelled or scheme-prefixed value is something this
        # function does not claim to understand, and it says so by not touching
        # it rather than by guessing.
        assert normalize_cross_id("doi", "urn:１２３/abc") == "urn:１２３/abc"
        assert normalize_cross_id("doi", "doi:10.１２３４/abc") == "doi:10.１２３４/abc"
        assert normalize_cross_id("doi", "11.１２３４/abc") == "11.１２３４/abc"

    def test_full_width_and_ascii_doi_import_as_one_file(self, tmp_path):
        # End-to-end repro from #405: before the fix these wrote
        # kim-2020-paper-one.md and kim-2020-paper-one-2.md.
        def item(key, doi):
            return {
                "zotero_key": key,
                "title": "Paper One",
                "doi": doi,
                "authors": [{"last": "Kim", "first": "A"}],
                "year": "2020",
            }

        first = ZoteroWriter().write(item("K1", "10.１２３４/abc"), tmp_path)
        second = ZoteroWriter().write(item("K2", "10.1234/abc"), tmp_path)
        assert first.status == "imported"
        assert second.status == "skipped"
        assert "duplicate DOI" in second.reason
        assert second.path == first.path
        assert [p.name for p in sorted((tmp_path / "sources").glob("*.md"))] == \
            [first.path.name]


class TestPmidDigitSpelling:
    """A PMID folds whole, because it has no opaque half (#421).

    A PMID is a positive decimal integer by definition, so a non-ASCII spelling is
    a rendering of the same PubMed record and must produce the same join key. That
    is the DOI argument (#405) without the suffix caveat: there is no part of a
    PMID where respelling a character would name a *different* record, so the fold
    covers the value rather than a leading part of it.

    The Zotero parser folds a PMID at the import boundary already (#398), so what
    these pin is the reachable remainder: values that path wrote *before* #398 and
    does not repair, and hand-edited files. Both are read off disk by the index,
    which is where a boundary-only fold can never reach them. The guard is pinned
    separately from the outcome, because a mutant that drops it survives every
    case where the value is well-formed.
    """

    def test_full_width_collides_with_ascii(self):
        assert normalize_cross_id("pmid", "１２３４５６７８") == "12345678"

    def test_non_latin_digit_scripts_fold(self):
        # The counter-case for NFKC, as in the parser: `\d` matches these but
        # NFKC does not fold them.
        assert normalize_cross_id("pmid", "٢٣٤") == "234"  # Arabic-Indic
        assert normalize_cross_id("pmid", "२३४") == "234"  # Devanagari
        assert normalize_cross_id("pmid", "１2٣") == "123"  # mixed

    def test_the_strip_and_the_fold_compose(self):
        # The fold runs on the stripped value, so a padded full-width id still
        # lands on the bare ASCII key rather than on " 123 " or "１２３".
        assert normalize_cross_id("pmid", " １２３ ") == "123"

    def test_the_guard_leaves_a_value_that_is_not_a_bare_pmid_alone(self):
        # These pin the *guard*: each carries a non-ASCII digit, so dropping the
        # "is the folded value a bare PMID?" check would rewrite it. A labelled or
        # URL-shaped value is something this function does not claim to
        # understand, and it says so by not touching it.
        assert normalize_cross_id("pmid", "pmid:１２３") == "pmid:１２３"
        assert normalize_cross_id("pmid", "https://pubmed.ncbi.nlm.nih.gov/１２３") == \
            "https://pubmed.ncbi.nlm.nih.gov/１２３"
        assert normalize_cross_id("pmid", "１２３abc") == "１２３abc"

    def test_non_digit_lookalikes_do_not_fold(self):
        # `²` and `①` are category `No`, never matched by `\d`, so the folded value
        # is not a bare PMID and the guard returns the original untouched.
        assert normalize_cross_id("pmid", "1２²") == "1２²"
        assert normalize_cross_id("pmid", "①②③") == "①②③"

    def test_full_width_and_ascii_pmid_import_as_one_file(self, tmp_path):
        # End-to-end: a source carrying the full-width PMID a pre-#398 Zotero
        # import wrote (measured: that `extract_pmid` returned `１２３４５６７８`
        # verbatim), then the same paper arriving with the ASCII id. Before this
        # fix these wrote kim-2020-paper-one.md and kim-2020-paper-one-2.md.
        def item(key, pmid):
            return {
                "zotero_key": key,
                "title": "Paper One",
                "pmid": pmid,
                "authors": [{"last": "Kim", "first": "A"}],
                "year": "2020",
            }

        first = ZoteroWriter().write(item("K1", "１２３４５６７８"), tmp_path)
        second = ZoteroWriter().write(item("K2", "12345678"), tmp_path)
        assert first.status == "imported"
        assert second.status == "skipped"
        assert "duplicate PMID" in second.reason
        assert second.path == first.path
        assert [p.name for p in sorted((tmp_path / "sources").glob("*.md"))] == \
            [first.path.name]

    def test_a_hand_edited_full_width_pmid_matches_an_openalex_import(self, tmp_path):
        # The other reachable half: an existing file's full-width `pmid:` is read
        # by the index, so the fold has to happen on the derived key — folding at
        # an import boundary alone would never reach this file.
        _write_source_file(tmp_path / "sources", "existing.md",
                           {"zotero_key": "K1", "pmid": "１２３４５６７８",
                            "imported_from": "zotero", "title": "Paper One"})
        result = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W1", pmid="12345678"), tmp_path)
        assert result.status == "merged"
        assert "duplicate PMID" in result.reason
        assert "already in existing.md" in result.reason

    def test_two_different_pmids_stay_distinct(self, tmp_path):
        # The fold must not collapse unrelated records into one file.
        w = OpenAlexSourceWriter()
        a = w.write(_openalex(openalex_id="W1", pmid="12345678"), tmp_path)
        b = w.write(_openalex(openalex_id="W2", pmid="１２３４５６７９",
                              title="Another Paper"), tmp_path)
        assert a.status == b.status == "imported"
        assert a.path != b.path


class TestPmidImportBoundaryIsUnaffected:
    """The path that already folds (#398) keeps behaving exactly as before."""

    def test_extract_pmid_still_folds_at_the_boundary(self):
        # The fold on the derived key is additive: the value written to the file
        # is still the ASCII one the parser produced.
        assert extract_pmid("PMID: １２３４５６７８") == "12345678"

    def test_a_folded_import_writes_the_ascii_pmid_and_dedups_on_it(self, tmp_path):
        item = {
            "zotero_key": "K1",
            "title": "Paper One",
            "pmid": extract_pmid("PMID: １２３４５６７８"),
            "authors": [{"last": "Kim", "first": "A"}],
            "year": "2020",
        }
        first = ZoteroWriter().write(item, tmp_path)
        fm = parse_front_matter(first.path.read_text(encoding="utf-8"))
        assert fm["pmid"] == "12345678"
        second = ZoteroWriter().write({**item, "zotero_key": "K2"}, tmp_path)
        assert second.status == "skipped"
        assert "duplicate PMID" in second.reason


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
