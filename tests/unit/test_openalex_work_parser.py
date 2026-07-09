# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the OpenAlex Work parser (#51, spec §5.4).

Fixtures mirror live ``api.openalex.org`` payloads recorded during the #51
spike: identifiers arrive as URLs, and most fields are optional.
"""
from __future__ import annotations

import dataclasses

import pytest

from factlog.integrations.openalex.api_client import OpenAlexError
from factlog.integrations.openalex.work_parser import (
    Concept,
    ParsedWork,
    PrimaryTopic,
    is_placeholder_title,
    parse_work,
)

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
        {"display_name": "Interpretability", "score": 0.91, "level": 3},
        {"display_name": "Symbolic artificial intelligence", "score": 0.49, "level": 2},
        # Real shape from #54: the unrelated root concept scores exactly 0.00.
        {"display_name": "Paleontology", "score": 0.0, "level": 1},
    ],
    "primary_topic": {
        "display_name": "Neural Networks and Applications",
        "score": 0.9944,
        "subfield": {"display_name": "Artificial Intelligence"},
        "field": {"display_name": "Computer Science"},
        "domain": {"display_name": "Physical Sciences"},
    },
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
            concepts=(
                Concept("Interpretability", 0.91, 3),
                Concept("Symbolic artificial intelligence", 0.49, 2),
                Concept("Paleontology", 0.0, 1),
            ),
            primary_topic=PrimaryTopic(
                display_name="Neural Networks and Applications",
                score=0.9944,
                subfield="Artificial Intelligence",
                field="Computer Science",
                domain="Physical Sciences",
            ),
            cited_by_count=321,
            work_type="article",
            openalex_is_retracted=False,
            abstract="Current advances in AI",
            abstract_complete=True,
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

    @pytest.mark.parametrize("topic", [None, {}, "junk", {"score": 1},
                                       {"display_name": "  "}])
    def test_missing_primary_topic_becomes_none(self, topic):
        assert parse_work({**WORK, "primary_topic": topic}).primary_topic is None

    def test_primary_topic_without_hierarchy_keeps_the_name(self):
        parsed = parse_work({**WORK, "primary_topic": {"display_name": "T"}})
        assert parsed.primary_topic == PrimaryTopic("T")

    @pytest.mark.parametrize("bad", [{"subfield": "flat"}, {"subfield": {"x": 1}}])
    def test_malformed_hierarchy_nodes_become_none(self, bad):
        parsed = parse_work({**WORK, "primary_topic": {"display_name": "T", **bad}})
        assert parsed.primary_topic.subfield is None

    def test_primary_topic_score_can_be_near_zero_and_is_kept(self):
        # #54: primary_topic is just the top entry of topics[], and that can be 0.06.
        parsed = parse_work({**WORK, "primary_topic": {"display_name": "T", "score": 0.06}})
        assert parsed.primary_topic.score == 0.06


class TestConceptScores:
    def test_scores_and_levels_are_preserved(self):
        assert parse_work(WORK).concepts[0] == Concept("Interpretability", 0.91, 3)

    @pytest.mark.parametrize("score", [None, "0.9", True, [1]])
    def test_malformed_score_becomes_none(self, score):
        work = {**WORK, "concepts": [{"display_name": "X", "score": score}]}
        assert parse_work(work).concepts[0].score is None

    def test_integer_score_is_coerced_to_float(self):
        work = {**WORK, "concepts": [{"display_name": "X", "score": 1}]}
        assert parse_work(work).concepts[0].score == 1.0

    @pytest.mark.parametrize("level", [None, "3", -1, True])
    def test_malformed_level_becomes_none(self, level):
        work = {**WORK, "concepts": [{"display_name": "X", "level": level}]}
        assert parse_work(work).concepts[0].level is None

    def test_level_zero_is_kept_not_dropped(self):
        work = {**WORK, "concepts": [{"display_name": "X", "level": 0}]}
        assert parse_work(work).concepts[0].level == 0

    def test_unnamed_concept_is_dropped(self):
        work = {**WORK, "concepts": [{"score": 0.9}, {"display_name": "X", "score": 0.1}]}
        assert [c.name for c in parse_work(work).concepts] == ["X"]


class TestTagsFilter:
    """#54 Mapping B: tags are the concepts scoring above zero, most confident first."""

    def test_zero_scored_concepts_are_dropped(self):
        # 12 of 13 clearly-unrelated concepts in the #54 sample scored exactly 0.00.
        assert parse_work(WORK).tags == ("Interpretability", "Symbolic artificial intelligence")

    def test_tags_are_ordered_by_descending_score(self):
        work = {**WORK, "concepts": [
            {"display_name": "Low", "score": 0.1},
            {"display_name": "High", "score": 0.9},
            {"display_name": "Mid", "score": 0.5},
        ]}
        assert parse_work(work).tags == ("High", "Mid", "Low")

    def test_ties_keep_the_api_order(self):
        work = {**WORK, "concepts": [
            {"display_name": "First", "score": 0.5},
            {"display_name": "Second", "score": 0.5},
        ]}
        assert parse_work(work).tags == ("First", "Second")

    def test_a_barely_positive_score_survives(self):
        work = {**WORK, "concepts": [{"display_name": "X", "score": 0.0001}]}
        assert parse_work(work).tags == ("X",)

    def test_unscored_concepts_are_excluded_from_tags_but_kept_in_concepts(self):
        # Precision over recall: an unknown-confidence term is not worth a wrong
        # canonical alias. Empty tags is a visible failure; noisy tags is a silent one.
        work = {**WORK, "concepts": [{"display_name": "X"}]}
        parsed = parse_work(work)
        assert parsed.tags == ()
        assert parsed.concepts == (Concept("X"),)

    def test_wrong_sense_entities_survive_the_filter_as_documented(self):
        # No threshold removes these; the P1 human gate is what catches them.
        work = {**WORK, "concepts": [
            {"display_name": "Object detection", "score": 0.62},
            {"display_name": "Object (grammar)", "score": 0.57},
        ]}
        assert "Object (grammar)" in parse_work(work).tags

    def test_no_concepts_yields_no_tags(self):
        assert parse_work({**WORK, "concepts": []}).tags == ()


class TestAbstractComplete:
    def test_true_for_a_contiguous_index(self):
        assert parse_work(WORK).abstract_complete is True

    def test_false_when_a_position_is_missing(self):
        # W2913668833 is missing positions 479, 482, 491: a token was dropped.
        work = {**WORK, "abstract_inverted_index": {"a": [0], "b": [1], "d": [3]}}
        parsed = parse_work(work)
        assert parsed.abstract == "a b d"
        assert parsed.abstract_complete is False

    def test_false_when_positions_repeat(self):
        work = {**WORK, "abstract_inverted_index": {"first": [0], "second": [0]}}
        assert parse_work(work).abstract_complete is False

    @pytest.mark.parametrize("index", [None, {}, "junk"])
    def test_none_when_there_is_no_abstract(self, index):
        # Absent, not False: there is nothing to be complete about.
        parsed = parse_work({**WORK, "abstract_inverted_index": index})
        assert parsed.abstract == ""
        assert parsed.abstract_complete is None

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


class TestMeshTerms:
    """#53: OpenAlex repeats mesh rows — one work returned 108 rows, 26 distinct."""

    def test_repeated_descriptors_are_deduplicated(self):
        work = {**WORK, "mesh": [
            {"descriptor_name": "Aging", "qualifier_name": "physiology"},
            {"descriptor_name": "Aging", "qualifier_name": "genetics"},
            {"descriptor_name": "Aging", "qualifier_name": None},
            {"descriptor_name": "Animals"},
        ]}
        assert parse_work(work).mesh_terms == ("Aging", "Animals")

    def test_first_seen_order_is_preserved(self):
        work = {**WORK, "mesh": [{"descriptor_name": n} for n in ["Zebra", "Aging", "Zebra"]]}
        assert parse_work(work).mesh_terms == ("Zebra", "Aging")

    def test_major_topic_flag_is_never_read(self):
        # Unreliable before ~2022 (#53); descriptors carry no major/minor mark.
        work = {**WORK, "mesh": [
            {"descriptor_name": "Aging", "is_major_topic": True},
            {"descriptor_name": "Aging", "is_major_topic": False},
        ]}
        assert parse_work(work).mesh_terms == ("Aging",)

    @pytest.mark.parametrize("mesh", [None, [], "junk", [{"descriptor_ui": "D1"}], [7]])
    def test_missing_mesh_becomes_empty_tuple(self, mesh):
        assert parse_work({**WORK, "mesh": mesh}).mesh_terms == ()


class TestIsPlaceholderTitle:
    @pytest.mark.parametrize("raw", ["null", "NULL", " Null "])
    def test_detects_the_literal_null_string(self, raw):
        assert is_placeholder_title(raw) is True

    @pytest.mark.parametrize("raw", [None, "", "Nullity", "On Null Hypotheses", 7, "none"])
    def test_leaves_everything_else_alone(self, raw):
        assert is_placeholder_title(raw) is False

    def test_such_a_work_is_still_parsed_not_rejected(self):
        # A paper legitimately titled "Null" is possible; callers warn, not drop.
        parsed = parse_work({**WORK, "title": "null", "display_name": "null"})
        assert parsed.title == "null"
        assert is_placeholder_title(parsed.title) is True


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


class TestArxivIdExtraction:
    """§7.1 / #64: the arXiv id is mined from ``locations[]``, never from ``ids``.

    Real URL shapes and the ``best_oa_location`` trap were measured live in #57/#64
    (W2972343689, W3017961061). The id is the join key that lets an OpenAlex work
    be recognised as the same paper as an arXiv record.
    """

    def test_no_locations_means_no_arxiv_id(self):
        assert parse_work(WORK).arxiv_id is None

    def test_arxiv_id_is_never_taken_from_ids(self):
        # Even if `ids` somehow carried an arxiv key, it must not be read.
        work = {**WORK, "ids": {**WORK["ids"], "arxiv": "http://arxiv.org/abs/2005.13421"}}
        assert parse_work(work).arxiv_id is None

    def test_landing_page_url_yields_the_base_id(self):
        work = {**WORK, "locations": [
            {"landing_page_url": "http://arxiv.org/abs/2005.13421"},
        ]}
        assert parse_work(work).arxiv_id == "2005.13421"

    def test_pdf_url_is_used_when_landing_is_absent(self):
        work = {**WORK, "locations": [
            {"pdf_url": "https://arxiv.org/pdf/2005.13421"},
        ]}
        assert parse_work(work).arxiv_id == "2005.13421"

    def test_version_is_stripped_to_the_base(self):
        work = {**WORK, "locations": [
            {"landing_page_url": "https://arxiv.org/abs/2311.09277v2"},
        ]}
        assert parse_work(work).arxiv_id == "2311.09277"

    def test_export_arxiv_org_subdomain_is_admitted(self):
        # export.arxiv.org is arXiv; its host ends with `.arxiv.org`.
        work = {**WORK, "locations": [
            {"pdf_url": "http://export.arxiv.org/pdf/2004.10964"},
        ]}
        assert parse_work(work).arxiv_id == "2004.10964"

    def test_doi_org_arxiv_datacite_shape_is_ignored(self):
        # A real locations[] entry: host is doi.org, not arxiv.org. Feeding it to
        # normalize_arxiv_id would RAISE; the host filter must exclude it.
        work = {**WORK, "locations": [
            {"landing_page_url": "https://doi.org/10.48550/arxiv.2004.10964"},
        ]}
        assert parse_work(work).arxiv_id is None

    def test_doi_org_then_export_arxiv_finds_the_arxiv_one(self):
        # Multi-location: the doi.org shape is skipped, export.arxiv.org parses.
        work = {**WORK, "locations": [
            {"landing_page_url": "https://doi.org/10.48550/arxiv.2004.10964"},
            {"pdf_url": "http://export.arxiv.org/pdf/2004.10964"},
        ]}
        assert parse_work(work).arxiv_id == "2004.10964"

    def test_non_arxiv_preprint_hosts_are_ignored(self):
        work = {**WORK, "locations": [
            {"landing_page_url": "https://www.biorxiv.org/content/10.1101/2020.01.01.123456"},
            {"pdf_url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1234567"},
        ]}
        assert parse_work(work).arxiv_id is None

    def test_best_oa_location_is_not_read(self):
        # For a published paper best_oa_location is the JOURNAL, not arXiv (#64).
        work = {**WORK,
                "best_oa_location": {"landing_page_url": "http://arxiv.org/abs/2005.13421"},
                "locations": [
                    {"landing_page_url": "https://aclanthology.org/W19-3302"},
                ]}
        assert parse_work(work).arxiv_id is None

    def test_first_arxiv_location_in_order_wins(self):
        # landing before pdf within a location; locations in array order.
        work = {**WORK, "locations": [
            {"landing_page_url": "https://arxiv.org/abs/2005.13421",
             "pdf_url": "https://arxiv.org/pdf/9999.99999"},
        ]}
        assert parse_work(work).arxiv_id == "2005.13421"

    def test_old_style_id_is_canonicalised(self):
        work = {**WORK, "locations": [
            {"landing_page_url": "https://arxiv.org/abs/math.GT/0309136"},
        ]}
        assert parse_work(work).arxiv_id == "math/0309136"

    def test_malformed_arxiv_url_does_not_crash_the_parse(self):
        # An arXiv-hosted URL that is not an id (homepage) is skipped, not fatal.
        work = {**WORK, "locations": [
            {"landing_page_url": "https://arxiv.org/"},
            {"pdf_url": "https://arxiv.org/pdf/2005.13421"},
        ]}
        assert parse_work(work).arxiv_id == "2005.13421"

    def test_non_list_locations_degrade_to_none(self):
        assert parse_work({**WORK, "locations": "oops"}).arxiv_id is None
