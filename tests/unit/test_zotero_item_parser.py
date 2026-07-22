# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Zotero item -> standard dict parser (phase 1, #5).

The parser is pure and deterministic: same item -> same dict. These tests pin
the schema contract SourceWriter depends on and the edge cases from the issue
(empty fields, single-name creators, multi-line extra, non-ASCII authors,
retracted tags, creator/tag order preservation).
"""
from __future__ import annotations

from factlog import literal_types
from factlog.integrations.common.source_writer import normalize_cross_id
from factlog.integrations.zotero.item_parser import (
    _YEAR_RE,
    ItemParser,
    extract_pmid,
    extract_tags,
    extract_year,
    parse_creators,
    parse_item,
)


def _item(**data):
    """A pyzotero-style item wrapper around a data dict."""
    return {"key": data.get("key", "TOPKEY"), "data": data}


FULL = _item(
    key="ABCD1234",
    itemType="journalArticle",
    title="Omega-3 fatty acids and COPD",
    creators=[
        {"creatorType": "author", "firstName": "W", "lastName": "Matsuyama"},
        {"creatorType": "author", "firstName": "H", "lastName": "Mitsuyama"},
        {"creatorType": "editor", "firstName": "E", "lastName": "Editor"},
    ],
    date="2005-06",
    publicationTitle="Chest",
    DOI="10.1378/chest.128.6.3817",
    extra="PMID: 16354850\nPMCID: PMC12345",
    abstractNote="Background: ...",
    tags=[{"tag": "retracted"}, {"tag": "omega-3"}, {"tag": "copd"}],
    dateModified="2020-01-02T03:04:05Z",
)


class TestFullItem:
    def test_maps_all_fields(self):
        out = parse_item(FULL)
        assert out["zotero_key"] == "ABCD1234"
        assert out["item_type"] == "journalArticle"
        assert out["title"] == "Omega-3 fatty acids and COPD"
        assert out["year"] == "2005"
        assert out["date"] == "2005-06"
        assert out["journal"] == "Chest"
        assert out["doi"] == "10.1378/chest.128.6.3817"
        assert out["pmid"] == "16354850"
        assert out["abstract"] == "Background: ..."
        assert out["tags"] == ["retracted", "omega-3", "copd"]
        assert out["date_modified"] == "2020-01-02T03:04:05Z"
        assert out["retracted"] is True

    def test_only_authors_kept_in_order(self):
        authors = parse_item(FULL)["authors"]
        assert [a["name"] for a in authors] == ["Matsuyama W", "Mitsuyama H"]
        assert authors[0] == {"last": "Matsuyama", "first": "W", "name": "Matsuyama W"}

    def test_deterministic(self):
        assert parse_item(FULL) == parse_item(FULL)

    def test_class_wrapper_matches_function(self):
        assert ItemParser().parse(FULL) == parse_item(FULL)


class TestBareDataDict:
    def test_accepts_data_dict_without_wrapper(self):
        out = parse_item({"key": "K9", "itemType": "book", "title": "T"})
        assert out["zotero_key"] == "K9"
        assert out["item_type"] == "book"
        assert out["title"] == "T"


class TestEmptyAndMissing:
    def test_empty_item_yields_empty_schema(self):
        out = parse_item({})
        assert out["zotero_key"] == ""
        assert out["title"] == ""
        assert out["authors"] == []
        assert out["year"] == ""
        assert out["doi"] == ""
        assert out["pmid"] == ""
        assert out["tags"] == []
        assert out["retracted"] is False

    def test_non_dict_input(self):
        assert parse_item(None)["title"] == ""  # type: ignore[arg-type]

    def test_key_falls_back_to_wrapper(self):
        # data has no key, wrapper does.
        out = parse_item({"key": "WRAP", "data": {"title": "x"}})
        assert out["zotero_key"] == "WRAP"


class TestYear:
    def test_variants(self):
        assert extract_year("2005") == "2005"
        assert extract_year("2005-06-01") == "2005"
        assert extract_year("June 2005") == "2005"
        assert extract_year("") == ""
        assert extract_year(None) == ""
        assert extract_year("no digits here") == ""

    def test_year_is_normalized_to_ascii_digits(self):
        # Zotero holds whatever the library holds; a non-ASCII digit run is
        # upstream data the user cannot fix from inside factlog (#398).
        assert extract_year("２０２０-06-01") == "2020"  # full-width
        assert extract_year("2020-06-01") == "2020"  # half-width, unchanged
        assert extract_year("２0２0-06-01") == "2020"  # mixed
        assert extract_year("２０２０") == "2020"

    def test_year_normalizes_non_fullwidth_digit_scripts(self):
        # The counter-case for NFKC: `\d` matches these, but NFKC does NOT fold
        # them, so an NFKC-based fix would leave them non-ASCII.
        assert extract_year("٢٠٢٠-06-01") == "2020"  # Arabic-Indic
        assert extract_year("२०२०-06-01") == "2020"  # Devanagari
        assert extract_year("۲۰۲۰-06-01") == "2020"  # Extended Arabic-Indic
        assert extract_year("２0٢0") == "2020"  # scripts mixed within one run

    def test_year_does_not_invent_digits_from_non_digits(self):
        # The other half of rejecting NFKC: `①` and `²` are not `Nd`, so they are
        # not matched and cannot be folded into a year that was never stated.
        assert extract_year("①②③④") == ""
        assert extract_year("²²²²") == ""

    def test_normalized_year_is_accepted_by_literal_types(self):
        # The point of #398: the value this writes must survive the ASCII-only
        # parsers #388 installed, so an ordinary import stops producing
        # "does not parse" warnings. `year` reaches them as a bare number.
        raw = "２０２０-06-01"
        assert literal_types.parse_number(_YEAR_RE.search(raw).group(0)) is None
        assert literal_types.parse_number(extract_year(raw)) == 2020
        assert literal_types.non_ascii_digits(extract_year(raw)) == ""


class TestPmidAndDoi:
    def test_pmid_multiline_any_case(self):
        assert extract_pmid("Some note\npmid: 999\n") == "999"
        assert extract_pmid("PMID:12345") == "12345"
        assert extract_pmid("no id") == ""

    def test_pmid_is_normalized_to_ascii_digits(self):
        # A PMID is by definition a decimal integer, so respelling it in ASCII
        # names the same PubMed record.
        assert extract_pmid("PMID: １２３４５６７") == "1234567"
        assert extract_pmid("PMID: 1234567") == "1234567"
        assert extract_pmid("PMID: １23４567") == "1234567"
        assert literal_types.parse_number(extract_pmid("PMID: １２３４５６７")) == 1234567

    def test_doi_digits_are_not_normalized_known_limitation(self):
        # CHARACTERIZATION, NOT AN ENDORSEMENT. This pins what the code does
        # today so a future fix is a visible, intentional change — it does not
        # assert that today's behaviour is right. It is not.
        #
        # Under ISO 26324 a DOI prefix `10.<registrant>` is a decimal number
        # (`_DOI_CORE_RE` spells it `10\.\d+/`); only the suffix is opaque. So
        # the same "it is a number, respell it" argument that justifies
        # normalizing PMID applies to the DOI *prefix*, and leaving it produces
        # a real defect: `normalize_cross_id` only strips/lowercases, so the
        # two spellings below are different join keys and one paper imports as
        # two files. DOI is the primary cross-source join key, so later
        # OpenAlex/PubMed imports (always ASCII) then fail to match in silence.
        #
        # Pre-existing, out of scope for #398 (which fixes the producer of the
        # #388 warnings), tracked as #405. When #405 lands, this test is the
        # one to invert — the assertions below are exactly what it must change.
        out = parse_item(_item(DOI="10.１２３４/abc"))
        assert out["doi"] == "10.１２３４/abc"
        # The defect this leaves behind, pinned explicitly so it cannot be
        # mistaken for intended behaviour:
        assert normalize_cross_id("doi", "10.１２３４/abc") != normalize_cross_id(
            "doi", "10.1234/abc"
        )

    def test_doi_prefers_data_field(self):
        out = parse_item(_item(DOI="10.1/x", extra="DOI: 10.2/y"))
        assert out["doi"] == "10.1/x"

    def test_doi_falls_back_to_extra(self):
        out = parse_item(_item(extra="DOI: 10.2/y\nPMID: 5"))
        assert out["doi"] == "10.2/y"
        assert out["pmid"] == "5"

    def test_doi_from_url_form_strips_wrapper(self):
        # A doi.org URL in extra must yield the bare 10.x/y core, not ".org/...".
        out = parse_item(_item(extra="doi.org/10.1234/abc"))
        assert out["doi"] == "10.1234/abc"

    def test_doi_from_https_url_form(self):
        out = parse_item(_item(extra="DOI: https://doi.org/10.5/xyz"))
        assert out["doi"] == "10.5/xyz"

    def test_doi_trailing_punctuation_stripped(self):
        out = parse_item(_item(extra="DOI: 10.9/qrs."))
        assert out["doi"] == "10.9/qrs"

    def test_extra_without_doi_label_is_ignored(self):
        # A non-DOI identifier that happens to look like 10.x must not leak in.
        out = parse_item(_item(extra="Accession: 10.55 units"))
        assert out["doi"] == ""


class TestCreators:
    def test_single_name_creator(self):
        out = parse_creators([{"creatorType": "author", "name": "World Health Organization"}])
        assert out == [{"last": "", "first": "", "name": "World Health Organization"}]

    def test_missing_creator_type_treated_as_author(self):
        out = parse_creators([{"lastName": "Doe", "firstName": "J"}])
        assert out == [{"last": "Doe", "first": "J", "name": "Doe J"}]

    def test_non_ascii_author_preserved(self):
        out = parse_creators([{"creatorType": "author", "lastName": "김", "firstName": "무성"}])
        assert out == [{"last": "김", "first": "무성", "name": "김 무성"}]

    def test_lastname_only(self):
        out = parse_creators([{"creatorType": "author", "lastName": "Aristotle"}])
        assert out == [{"last": "Aristotle", "first": "", "name": "Aristotle"}]

    def test_empty_creator_dropped(self):
        assert parse_creators([{"creatorType": "author"}]) == []

    def test_editor_only_yields_no_authors(self):
        assert parse_creators([{"creatorType": "editor", "lastName": "E"}]) == []

    def test_creator_type_case_insensitive(self):
        out = parse_creators([{"creatorType": "Author", "lastName": "Doe"}])
        assert out == [{"last": "Doe", "first": "", "name": "Doe"}]

    def test_firstname_only_creator(self):
        out = parse_creators([{"creatorType": "author", "firstName": "Prince"}])
        assert out == [{"last": "", "first": "Prince", "name": "Prince"}]

    def test_order_preserved_not_sorted(self):
        out = parse_creators(
            [
                {"creatorType": "author", "lastName": "Zeta"},
                {"creatorType": "author", "lastName": "Alpha"},
            ]
        )
        assert [a["last"] for a in out] == ["Zeta", "Alpha"]

    def test_non_list_creators(self):
        assert parse_creators("oops") == []


class TestTags:
    def test_order_preserved(self):
        assert extract_tags([{"tag": "b"}, {"tag": "a"}]) == ["b", "a"]

    def test_string_and_dict_forms(self):
        assert extract_tags(["x", {"tag": "y"}, {"notag": 1}]) == ["x", "y"]

    def test_retracted_case_insensitive(self):
        assert parse_item(_item(tags=[{"tag": "Retracted"}]))["retracted"] is True
        assert parse_item(_item(tags=[{"tag": "omega"}]))["retracted"] is False

    def test_retracted_variants_flagged(self):
        for tag in ("Retraction", "Retracted Publication", "RETRACTED ARTICLE"):
            assert parse_item(_item(tags=[{"tag": tag}]))["retracted"] is True

    def test_automatic_tag_with_type_field_kept(self):
        assert extract_tags([{"tag": "mesh-term", "type": 1}]) == ["mesh-term"]


class TestDataFallback:
    def test_data_none_falls_back_to_item(self):
        # data is present but not a dict -> treat the wrapper itself as data.
        out = parse_item({"data": None, "title": "T", "key": "K"})
        assert out["title"] == "T"
        assert out["zotero_key"] == "K"


class TestFreshObjects:
    def test_lists_are_not_shared_between_calls(self):
        a = parse_item(FULL)
        b = parse_item(FULL)
        a["authors"].append({"last": "X", "first": "", "name": "X"})
        a["tags"].append("mutated")
        assert len(b["authors"]) == 2  # b untouched by mutating a
        assert "mutated" not in b["tags"]
