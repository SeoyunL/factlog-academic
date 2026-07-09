# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the OpenAlex Work parser (#51, spec §5.4).

Fixtures mirror live ``api.openalex.org`` payloads recorded during the #51
spike: identifiers arrive as URLs, and most fields are optional.
"""
from __future__ import annotations

import dataclasses

import pytest

from factlog.integrations.openalex.api_client import OpenAlexError
from factlog.integrations.openalex.work_parser import ParsedWork, parse_work

# A real work, trimmed. Note every identifier is a URL.
WORK = {
    "id": "https://openalex.org/W3113149630",
    "doi": "https://doi.org/10.1007/S10462-023-10448-W",
    "title": "Neurosymbolic AI: the 3rd wave",
    "display_name": "Neurosymbolic AI: the 3rd wave",
    "publication_year": 2023,
    "type": "article",
    "cited_by_count": 321,
    "is_retracted": False,
    "ids": {
        "openalex": "https://openalex.org/W3113149630",
        "doi": "https://doi.org/10.1007/s10462-023-10448-w",
        "pmid": "https://pubmed.ncbi.nlm.nih.gov/32738937",
    },
    "primary_location": {"source": {"display_name": "Artificial Intelligence Review"}},
    "authorships": [
        {"author_position": "first", "author": {"display_name": "Artur d’Avila Garcez"}},
        {"author_position": "last", "author": {"display_name": "Luís C. Lamb"}},
    ],
    "concepts": [
        {"display_name": "Interpretability", "score": 0.91},
        {"display_name": "Symbolic artificial intelligence", "score": 0.49},
    ],
    "abstract_inverted_index": {"Current": [0], "advances": [1], "in": [2], "AI": [3]},
    "mesh": [
        {"descriptor_ui": "D000375", "descriptor_name": "Aging", "is_major_topic": False},
    ],
}


class TestHappyPath:
    def test_parses_a_full_work(self):
        parsed = parse_work(WORK)
        assert parsed == ParsedWork(
            openalex_id="W3113149630",
            title="Neurosymbolic AI: the 3rd wave",
            authors=("Artur d’Avila Garcez", "Luís C. Lamb"),
            year=2023,
            journal="Artificial Intelligence Review",
            doi="10.1007/s10462-023-10448-w",
            pmid="32738937",
            concepts=("Interpretability", "Symbolic artificial intelligence"),
            cited_by_count=321,
            work_type="article",
            openalex_is_retracted=False,
            abstract="Current advances in AI",
            mesh_terms=("Aging",),
        )

    def test_identifiers_are_reduced_to_bare_forms(self):
        parsed = parse_work(WORK)
        # §7.1 duplicate detection matches on these, so URLs would never match.
        assert parsed.openalex_id == "W3113149630"
        assert parsed.doi == "10.1007/s10462-023-10448-w"  # also lowercased
        assert parsed.pmid == "32738937"

    def test_openalex_url_is_derived_from_the_bare_id(self):
        assert parse_work(WORK).openalex_url == "https://openalex.org/W3113149630"

    def test_has_abstract_reflects_restoration(self):
        assert parse_work(WORK).has_abstract is True
        assert parse_work({**WORK, "abstract_inverted_index": None}).has_abstract is False

    def test_parsed_work_is_immutable(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            parse_work(WORK).title = "x"


class TestIdentifier:
    def test_missing_id_is_the_one_hard_error(self):
        with pytest.raises(OpenAlexError, match="has no 'id'"):
            parse_work({k: v for k, v in WORK.items() if k != "id"})

    @pytest.mark.parametrize("bad", [None, "", "   ", 42])
    def test_unusable_id_raises(self, bad):
        with pytest.raises(OpenAlexError):
            parse_work({**WORK, "id": bad})

    def test_non_dict_payload_raises(self):
        with pytest.raises(OpenAlexError, match="expected an OpenAlex work object"):
            parse_work(["nope"])

    def test_bare_id_is_accepted(self):
        assert parse_work({"id": "W1"}).openalex_id == "W1"


class TestDegradation:
    """On a live query of 100 works: 37 had no abstract, 21 no journal, 17 no DOI, 8 no authors."""

    def test_minimal_payload_degrades_rather_than_raising(self):
        parsed = parse_work({"id": "https://openalex.org/W1"})
        assert parsed == ParsedWork(openalex_id="W1")
        assert parsed.title is None
        assert parsed.authors == ()
        assert parsed.abstract == ""
        assert parsed.openalex_is_retracted is False

    def test_falls_back_to_display_name_when_title_absent(self):
        work = {k: v for k, v in WORK.items() if k != "title"}
        assert parse_work(work).title == "Neurosymbolic AI: the 3rd wave"

    @pytest.mark.parametrize("blank", [None, "", "   ", 7])
    def test_blank_title_becomes_none(self, blank):
        work = {**WORK, "title": blank, "display_name": blank}
        assert parse_work(work).title is None

    @pytest.mark.parametrize(
        "location",
        [None, {}, {"source": None}, {"source": {}}, {"source": {"display_name": "  "}}, "junk"],
    )
    def test_missing_journal_becomes_none(self, location):
        assert parse_work({**WORK, "primary_location": location}).journal is None

    @pytest.mark.parametrize("bad_doi", [None, "", "not-a-doi", 42, "10.1/x"])
    def test_malformed_doi_in_payload_becomes_none_not_an_error(self, bad_doi):
        # A mistyped `--doi` from the user is an error; junk inside an API
        # payload must not abort an otherwise usable record.
        assert parse_work({**WORK, "doi": bad_doi}).doi is None

    @pytest.mark.parametrize("ids", [None, {}, {"pmid": None}, {"pmid": "abc"}, {"pmid": "0"}, "junk"])
    def test_missing_or_malformed_pmid_becomes_none(self, ids):
        assert parse_work({**WORK, "ids": ids}).pmid is None

    @pytest.mark.parametrize("authorships", [None, [], "junk", [None, 7]])
    def test_missing_authors_becomes_empty_tuple(self, authorships):
        assert parse_work({**WORK, "authorships": authorships}).authors == ()

    @pytest.mark.parametrize("count", [None, -1, "321", True, 1.5])
    def test_malformed_cited_by_count_becomes_none(self, count):
        assert parse_work({**WORK, "cited_by_count": count}).cited_by_count is None

    def test_zero_citations_is_kept_not_dropped(self):
        assert parse_work({**WORK, "cited_by_count": 0}).cited_by_count == 0

    @pytest.mark.parametrize("year", [None, "2023", 23, 12345, True, 1.5])
    def test_malformed_year_becomes_none(self, year):
        assert parse_work({**WORK, "publication_year": year}).year is None

    @pytest.mark.parametrize("concepts", [None, [], "junk", [{"score": 1}], [7]])
    def test_missing_concepts_becomes_empty_tuple(self, concepts):
        assert parse_work({**WORK, "concepts": concepts}).concepts == ()

    @pytest.mark.parametrize("mesh", [None, [], "junk", [{"descriptor_ui": "D1"}]])
    def test_missing_mesh_becomes_empty_tuple(self, mesh):
        assert parse_work({**WORK, "mesh": mesh}).mesh_terms == ()


class TestAuthors:
    def test_authors_are_ordered_first_middle_last(self):
        work = {**WORK, "authorships": [
            {"author_position": "last", "author": {"display_name": "Zed"}},
            {"author_position": "first", "author": {"display_name": "Ann"}},
            {"author_position": "middle", "author": {"display_name": "Bob"}},
        ]}
        assert parse_work(work).authors == ("Ann", "Bob", "Zed")

    def test_unknown_positions_sort_last_preserving_order(self):
        work = {**WORK, "authorships": [
            {"author": {"display_name": "NoPos1"}},
            {"author_position": "first", "author": {"display_name": "Ann"}},
            {"author_position": "bogus", "author": {"display_name": "NoPos2"}},
        ]}
        assert parse_work(work).authors == ("Ann", "NoPos1", "NoPos2")

    def test_middle_authors_keep_their_relative_order(self):
        work = {**WORK, "authorships": [
            {"author_position": "middle", "author": {"display_name": "M1"}},
            {"author_position": "middle", "author": {"display_name": "M2"}},
        ]}
        assert parse_work(work).authors == ("M1", "M2")

    def test_falls_back_to_raw_author_name(self):
        # §5.6 Risk 3: disambiguation can fail; record what the source said.
        work = {**WORK, "authorships": [
            {"author_position": "first", "author": {"display_name": None},
             "raw_author_name": "Garcez, A."},
        ]}
        assert parse_work(work).authors == ("Garcez, A.",)

    def test_authorship_without_any_name_is_dropped(self):
        work = {**WORK, "authorships": [
            {"author_position": "first", "author": {"display_name": "Ann"}},
            {"author_position": "middle", "author": {}},
            {"author_position": "last"},
        ]}
        assert parse_work(work).authors == ("Ann",)

    def test_non_dict_author_is_dropped(self):
        work = {**WORK, "authorships": [{"author": "Ann"}, {"author": {"display_name": "Bob"}}]}
        assert parse_work(work).authors == ("Bob",)


class TestRetractionIsSourceScoped:
    def test_flag_is_named_for_its_source(self):
        # OpenAlex flagged the Lancet dementia report as retracted; PubMed does
        # not. §7.2 gives PubMed priority, so this field must not read as the
        # merged `retracted:` claim.
        assert "openalex_is_retracted" in ParsedWork.__dataclass_fields__
        assert "retracted" not in ParsedWork.__dataclass_fields__

    def test_true_flag_is_carried(self):
        assert parse_work({**WORK, "is_retracted": True}).openalex_is_retracted is True

    @pytest.mark.parametrize("truthy", ["true", 1, "yes", [1]])
    def test_only_a_real_bool_true_counts(self, truthy):
        # A truthy string must not silently mark a paper retracted.
        assert parse_work({**WORK, "is_retracted": truthy}).openalex_is_retracted is False

    @pytest.mark.parametrize("falsy", [None, False, 0, ""])
    def test_absent_or_false_flag_is_false(self, falsy):
        assert parse_work({**WORK, "is_retracted": falsy}).openalex_is_retracted is False
