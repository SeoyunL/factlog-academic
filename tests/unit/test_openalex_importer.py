# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the OpenAlex importer (#51, spec §5.2)."""
from __future__ import annotations

import pytest

from factlog.integrations.openalex.api_client import OpenAlexError
from factlog.integrations.openalex.importer import (
    fetch_work,
    import_works,
    parse_works,
    resolve_work_id,
)
from factlog.integrations.openalex.work_parser import ParsedWork


def _work(work_id="W1", title="A paper", **over) -> ParsedWork:
    return ParsedWork(openalex_id=work_id, title=title, authors=("Ann Author",),
                      year=2023, **over)


def _kb(tmp_path):
    (tmp_path / "sources").mkdir()
    return tmp_path


class TestParseWorks:
    def test_parses_a_page(self):
        raw = [{"id": "https://openalex.org/W1", "title": "A"},
               {"id": "https://openalex.org/W2", "title": "B"}]
        assert [w.openalex_id for w in parse_works(raw)] == ["W1", "W2"]

    def test_unaddressable_records_are_dropped_not_fatal(self):
        raw = [{"id": "https://openalex.org/W1"}, {"no": "id"}, {"id": "W000"}, "junk"]
        assert [w.openalex_id for w in parse_works(raw)] == ["W1"]

    def test_empty_page(self):
        assert parse_works([]) == []


class TestImportWorks:
    def test_writes_each_work(self, tmp_path):
        kb = _kb(tmp_path)
        report = import_works([_work("W1"), _work("W2", title="B")], target=kb)
        assert report.imported == 2
        assert report.skipped == report.errors == 0
        assert len(list((kb / "sources").glob("*.md"))) == 2

    def test_import_order_is_by_work_id_not_input_order(self, tmp_path):
        # P3: reproducible collision suffixes require a deterministic order.
        kb = _kb(tmp_path)
        report = import_works([_work("W2"), _work("W1")], target=kb)
        assert [o.key for o in report.outcomes] == ["W1", "W2"]

    def test_same_slug_gets_a_suffix_in_id_order(self, tmp_path):
        kb = _kb(tmp_path)
        report = import_works([_work("W2", doi="10.1/b"), _work("W1", doi="10.1/a")], target=kb)
        names = [o.path.name for o in report.outcomes]
        assert names[1].endswith("-2.md")  # W2 sorts after W1, so W2 takes the suffix

    def test_dry_run_creates_no_files_but_names_them(self, tmp_path):
        kb = _kb(tmp_path)
        report = import_works([_work()], target=kb, dry_run=True)
        assert report.imported == 1
        assert report.outcomes[0].path.name.endswith(".md")
        assert list((kb / "sources").glob("*.md")) == []

    def test_reimport_is_skipped_with_a_reason(self, tmp_path):
        kb = _kb(tmp_path)
        import_works([_work()], target=kb)
        report = import_works([_work()], target=kb)
        assert report.skipped == 1
        assert "openalex_id match" in report.outcomes[0].reason

    def test_same_source_duplicate_is_skipped(self, tmp_path):
        # Two OpenAlex works sharing a DOI is a SAME-source duplicate (#71): it
        # stays ``skipped``, never a §7.3 merge. (Renamed from the old
        # ``test_cross_source_duplicate_is_skipped``: with OpenAlex now a merger,
        # only a match against ANOTHER database's file merges — see below.)
        kb = _kb(tmp_path)
        report = import_works(
            [_work("W1", doi="10.1/x"), _work("W2", title="Preprint", doi="10.1/x")], target=kb
        )
        assert report.imported == 1 and report.skipped == 1
        assert report.merged == 0
        assert "duplicate DOI" in report.outcomes[1].reason

    def test_cross_source_match_against_an_arxiv_file_is_merged(self, tmp_path):
        # The user-visible change of #73: an OpenAlex import that hits a paper
        # already in the KB via ANOTHER database (an arXiv deposit, matched on the
        # shared arXiv id) now reports ``merged`` — it used to report ``skipped``.
        from datetime import date

        from factlog.integrations.arxiv.source_writer import ArxivSourceWriter
        from factlog.integrations.arxiv.work_parser import ParsedArxivWork

        kb = _kb(tmp_path)
        ArxivSourceWriter().write(
            ParsedArxivWork(
                arxiv_id="2311.09277", version=1, title="A Paper", authors=("Ada Lovelace",),
                abstract="x", primary_category="cs.CL", categories=("cs.CL",),
                submitted=date(2023, 11, 15), last_updated=date(2023, 11, 15),
                doi=None, journal_ref=None, comment=None, withdrawn_by=None,
                abs_url="https://arxiv.org/abs/2311.09277v1",
                pdf_url="https://arxiv.org/pdf/2311.09277v1"),
            kb, imported_at="t")
        report = import_works(
            [_work("W1", doi=None, arxiv_id="2311.09277")], target=kb, imported_at="t2")
        assert report.merged == 1 and report.imported == 0 and report.skipped == 0
        assert "duplicate arXiv id" in report.outcomes[0].reason

    def test_missing_identity_is_an_error_outcome(self, tmp_path):
        kb = _kb(tmp_path)
        report = import_works([ParsedWork(openalex_id="")], target=kb)
        assert report.errors == 1
        assert report.outcomes[0].reason == "missing openalex_id"

    def test_untitled_work_is_labelled(self, tmp_path):
        report = import_works([_work(title=None)], target=_kb(tmp_path))
        assert report.outcomes[0].title == "(untitled)"

    def test_imported_at_is_stamped(self, tmp_path):
        kb = _kb(tmp_path)
        report = import_works([_work()], target=kb, imported_at="2026-07-09T00:00:00Z")
        assert 'imported_at: "2026-07-09T00:00:00Z"' in report.outcomes[0].path.read_text()

    def test_empty_input_reports_nothing(self, tmp_path):
        report = import_works([], target=_kb(tmp_path))
        assert report.outcomes == []
        assert report.imported == report.skipped == report.errors == 0


class TestResolveWorkId:
    def _seed(self, tmp_path, text):
        kb = _kb(tmp_path)
        (kb / "sources" / "paper.md").write_text(text, encoding="utf-8")
        return kb

    def test_reads_openalex_id_from_front_matter(self, tmp_path):
        kb = self._seed(tmp_path, '---\nopenalex_id: "W42"\n---\n\n# x\n')
        assert resolve_work_id(kb, "paper.md") == "W42"

    def test_accepts_a_bare_stem(self, tmp_path):
        kb = self._seed(tmp_path, '---\nopenalex_id: "W42"\n---\n')
        assert resolve_work_id(kb, "paper") == "W42"

    def test_missing_source_is_an_error(self, tmp_path):
        with pytest.raises(OpenAlexError, match="no source absent.md"):
            resolve_work_id(_kb(tmp_path), "absent")

    def test_source_without_openalex_id_is_an_error(self, tmp_path):
        kb = self._seed(tmp_path, "# a plain user source\n")
        with pytest.raises(OpenAlexError, match="records no openalex_id"):
            resolve_work_id(kb, "paper")

    def test_zotero_source_is_an_error(self, tmp_path):
        kb = self._seed(tmp_path, '---\nzotero_key: "ABC"\n---\n')
        with pytest.raises(OpenAlexError, match="records no openalex_id"):
            resolve_work_id(kb, "paper")

    @pytest.mark.parametrize("bad", ["", "   ", None])
    def test_blank_slug_is_an_error(self, tmp_path, bad):
        with pytest.raises(OpenAlexError, match="non-empty source slug"):
            resolve_work_id(_kb(tmp_path), bad)


class TestFetchWork:
    class Client:
        def __init__(self):
            self.calls = []

        def get_work(self, work_id):
            self.calls.append(("id", work_id))
            return {"id": "https://openalex.org/W1", "title": "A"}

        def get_work_by_doi(self, doi):
            self.calls.append(("doi", doi))
            return {"id": "https://openalex.org/W1", "title": "A"}

    def test_by_work_id(self):
        client = self.Client()
        assert fetch_work(client, work_id="W1").openalex_id == "W1"
        assert client.calls == [("id", "W1")]

    def test_by_doi(self):
        client = self.Client()
        assert fetch_work(client, doi="10.1/x").openalex_id == "W1"
        assert client.calls == [("doi", "10.1/x")]

    def test_neither_selector_is_an_error(self):
        with pytest.raises(OpenAlexError, match="exactly one of"):
            fetch_work(self.Client())

    def test_both_selectors_is_an_error(self):
        with pytest.raises(OpenAlexError, match="exactly one of"):
            fetch_work(self.Client(), work_id="W1", doi="10.1/x")
