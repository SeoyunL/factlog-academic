# SPDX-License-Identifier: Apache-2.0
"""Unit tests for CSL-JSON export core."""
from __future__ import annotations

from factlog.csl import _YEAR_RE, to_csl
from factlog.text_norm import fold_decimal_digits

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

    def test_comma_form_compound_surname(self):
        # "Family, Given" (what source_writer now emits) splits unambiguously.
        item = to_csl({"authors": ["Faronius, Håkan Karlsson", "Martires, Pedro Zuidberg Dos"]}, "k")
        assert item["author"][0] == {"family": "Faronius", "given": "Håkan Karlsson"}
        assert item["author"][1] == {"family": "Martires", "given": "Pedro Zuidberg Dos"}

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


class TestYearDigitFolding:
    """The year is folded to ASCII digits explicitly, not by luck of `int()` (#399).

    Every expectation here was ALREADY the observed output before the fold existed
    — `\\d{4}` matched the whole `Nd` category and `int()` then accepted it. The
    tests pin that behaviour to the explicit fold so a later narrowing of the
    regex to `[0-9]` cannot silently drop years, and pin the boundary (`No`
    characters) that neither spelling ever accepted.
    """

    def test_ascii_year_unchanged(self):
        assert to_csl({"year": "2020"}, "k")["issued"] == {"date-parts": [[2020]]}

    def test_full_width_year_folds_to_ascii(self):
        # Was correct before only because `int("２０２０") == 2020`.
        assert to_csl({"year": "２０２０"}, "k")["issued"] == {"date-parts": [[2020]]}

    def test_mixed_width_year_folds(self):
        assert to_csl({"year": "２0２0"}, "k")["issued"] == {"date-parts": [[2020]]}

    def test_non_latin_decimal_digits_fold(self):
        # Arabic-Indic: `Nd` like the full-width forms, so it folds the same way.
        assert to_csl({"year": "٢٠٢٠"}, "k")["issued"] == {"date-parts": [[2020]]}

    def test_full_width_year_embedded_in_text(self):
        item = to_csl({"year": "출판 ２０２０년"}, "k")
        assert item["issued"] == {"date-parts": [[2020]]}

    def test_folding_does_not_disturb_surrounding_characters(self):
        assert fold_decimal_digits("출판 ２０２０년") == "출판 2020년"
        assert fold_decimal_digits("n.d.") == "n.d."

    def test_folding_preserves_length(self):
        # Position-preserving is what makes folding equivalent to the old `\d{4}`:
        # a fold that changed length could create or destroy a 4-digit run.
        for value in ("2020", "２０２０", "２0２0", "٢٠٢٠", "출판 ２０２０년"):
            assert len(fold_decimal_digits(value)) == len(value)

    def test_year_pattern_accepts_only_ascii_digits(self):
        # Asserted on the pattern rather than on `to_csl` because it is NOT
        # observable through output: the fold leaves no `Nd` character behind, so
        # `\d{4}` and `[0-9]{4}` accept identical inputs downstream. This is the
        # documentary half of the issue — the pattern should state the set it
        # accepts. It is a set assertion, not a spelling one: any equivalent
        # spelling (`\d{4}` with `re.ASCII`, `(?a:\d{4})`, `[0-9][0-9][0-9][0-9]`)
        # passes. The fold itself is guarded by the behaviour tests above.
        assert _YEAR_RE.search("2020")
        assert not _YEAR_RE.search("２０２０")

    def test_superscript_and_circled_digits_are_not_years(self):
        # Category `No`, not `Nd`. `\d` never matched them, and the fold must not
        # start matching them — NFKC would have, which is why it is not used.
        assert "issued" not in to_csl({"year": "20²0"}, "k")
        assert "issued" not in to_csl({"year": "①②③④"}, "k")
        assert fold_decimal_digits("20²0") == "20²0"

    def test_too_few_digits_still_omits_issued(self):
        assert "issued" not in to_csl({"year": "１２３"}, "k")

    def test_first_four_digit_run_wins(self):
        assert to_csl({"year": "１２３４５"}, "k")["issued"] == {"date-parts": [[1234]]}
