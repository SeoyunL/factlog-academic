# SPDX-License-Identifier: Apache-2.0
"""Unit tests for BibTeX export core (front matter reader + formatter)."""
from __future__ import annotations

from factlog.bibtex import (
    is_annotation_source,
    parse_front_matter,
    read_front_matter,
    safe_cite_key,
    to_bibtex,
)

FM_TEXT = (
    '---\n'
    'zotero_key: "ABCD"\n'
    'item_type: "journalArticle"\n'
    'title: "Omega-3 & COPD: a \\"study\\""\n'
    'authors: ["Matsuyama W", "Mitsuyama H"]\n'
    'year: "2005"\n'
    'journal: "Chest"\n'
    'doi: "10.1378/x"\n'
    'pmid: "16354850"\n'
    'retracted: true\n'
    '---\n\n# body\n'
)


class TestParse:
    def test_reads_scalars_lists_bools(self):
        fm = parse_front_matter(FM_TEXT)
        assert fm["zotero_key"] == "ABCD"
        assert fm["authors"] == ["Matsuyama W", "Mitsuyama H"]
        assert fm["title"] == 'Omega-3 & COPD: a "study"'  # unescaped
        assert fm["retracted"] is True

    def test_no_front_matter(self):
        assert parse_front_matter("# just a body\n") == {}

    def test_reads_file(self, tmp_path):
        f = tmp_path / "s.md"
        f.write_text(FM_TEXT, encoding="utf-8")
        assert read_front_matter(f)["journal"] == "Chest"

    def test_annotation_marker(self):
        assert is_annotation_source({"source_kind": "annotations"}) is True
        assert is_annotation_source({"item_type": "book"}) is False


class TestCiteKey:
    def test_sanitizes(self):
        assert safe_cite_key("matsuyama-2005-omega3") == "matsuyama-2005-omega3"
        assert safe_cite_key("김무성 2005!") == "2005"  # non-ascii collapsed
        assert safe_cite_key("!!!") == "ref"


class TestToBibtex:
    def test_full_entry(self):
        out = to_bibtex(parse_front_matter(FM_TEXT), "matsuyama-2005")
        assert out.startswith("@article{matsuyama-2005,")
        assert "author = {Matsuyama W and Mitsuyama H}," in out
        assert r'title = {Omega-3 \& COPD: a "study"},' in out  # & escaped, quotes literal
        assert "year = {2005}," in out
        assert "journal = {Chest}," in out
        assert "doi = {10.1378/x}," in out
        assert "note = {PMID: 16354850}," in out
        assert out.rstrip().endswith("}")

    def test_entry_type_mapping(self):
        assert to_bibtex({"item_type": "preprint", "title": "T"}, "k").startswith("@misc{")
        assert to_bibtex({"item_type": "book", "title": "T"}, "k").startswith("@book{")
        assert to_bibtex({"item_type": "weird", "title": "T"}, "k").startswith("@misc{")
        assert to_bibtex({"title": "T"}, "k").startswith("@misc{")

    def test_empty_fields_omitted(self):
        out = to_bibtex({"item_type": "book", "title": "T"}, "k")
        assert "author" not in out and "doi" not in out and "note" not in out

    def test_escaping_special_chars(self):
        out = to_bibtex({"title": "a_b % c # d $ e"}, "k")
        assert r"\_" in out and r"\%" in out and r"\#" in out and r"\$" in out

    def test_non_ascii_kept(self):
        out = to_bibtex({"title": "제목", "authors": ["김 무성"]}, "k")
        assert "제목" in out and "김 무성" in out
