# SPDX-License-Identifier: Apache-2.0
"""Unit tests for CSL-JSON export core."""
from __future__ import annotations

from factlog.csl import to_csl

FM = {
    "item_type": "journalArticle",
    "title": "A Study",
    "authors": ["Matsuyama W", "UNESCO"],
    "year": "2005",
    "journal": "Chest",
    "doi": "10.1/x",
    "pmid": "163",
}


class TestToCsl:
    def test_full_item(self):
        item = to_csl(FM, "doe-2005")
        assert item["id"] == "doe-2005"
        assert item["type"] == "article-journal"
        assert item["title"] == "A Study"
        assert item["issued"] == {"date-parts": [[2005]]}
        assert item["container-title"] == "Chest"
        assert item["DOI"] == "10.1/x" and item["PMID"] == "163"

    def test_author_family_given_and_literal(self):
        authors = to_csl(FM, "k")["author"]
        assert authors[0] == {"family": "Matsuyama", "given": "W"}
        assert authors[1] == {"literal": "UNESCO"}  # single token -> literal

    def test_type_mapping_and_default(self):
        assert to_csl({"item_type": "book"}, "k")["type"] == "book"
        assert to_csl({"item_type": "weird"}, "k")["type"] == "document"
        assert to_csl({}, "k")["type"] == "document"

    def test_empty_fields_omitted(self):
        item = to_csl({"item_type": "book", "title": "T"}, "k")
        assert "author" not in item and "DOI" not in item and "issued" not in item

    def test_non_numeric_year_omits_issued(self):
        assert "issued" not in to_csl({"title": "T", "year": "n.d."}, "k")

    def test_non_ascii(self):
        item = to_csl({"title": "제목", "authors": ["김 무성"]}, "k")
        assert item["title"] == "제목"
        assert item["author"][0] == {"family": "김", "given": "무성"}
