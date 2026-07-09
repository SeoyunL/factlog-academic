# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the arXiv SourceWriter (#60, spec §11 Step 3).

Modelled on ``test_openalex_source_writer.py``. No network: every fixture is a
hand-built :class:`ParsedArxivWork`.
"""
from __future__ import annotations

from datetime import date

from factlog.bibtex import parse_front_matter
from factlog.integrations.arxiv.source_writer import ArxivSourceWriter
from factlog.integrations.arxiv.work_parser import ParsedArxivWork
from factlog.integrations.openalex.source_writer import OpenAlexSourceWriter
from factlog.integrations.openalex.work_parser import ParsedWork


def _work(**over) -> ParsedArxivWork:
    base = dict(
        arxiv_id="1706.03762",
        version=5,
        title="Attention Is All You Need",
        authors=("Ashish Vaswani", "Noam Shazeer"),
        abstract="The dominant sequence transduction models...",
        primary_category="cs.CL",
        categories=("cs.CL", "cs.LG"),
        submitted=date(2017, 6, 12),
        last_updated=date(2023, 8, 2),
        doi=None,
        journal_ref=None,
        comment=None,
        withdrawn_by=None,
        abs_url="https://arxiv.org/abs/1706.03762v5",
        pdf_url="https://arxiv.org/pdf/1706.03762v5",
    )
    return ParsedArxivWork(**{**base, **over})


def _front_matter(text: str) -> str:
    return text.split("---", 2)[1]


class TestRender:
    def test_front_matter_carries_the_spec_fields(self):
        text = ArxivSourceWriter().render(_work(), imported_at="2026-07-09T00:00:00Z")
        fm = _front_matter(text)
        assert 'arxiv_id: "1706.03762"' in fm
        assert "arxiv_version: 5" in fm
        assert 'title: "Attention Is All You Need"' in fm
        assert 'authors: ["Ashish Vaswani", "Noam Shazeer"]' in fm
        assert "year: 2017" in fm
        assert 'primary_category: "cs.CL"' in fm
        assert 'tags: ["cs.CL", "cs.LG"]' in fm
        assert "preprint: true" in fm
        assert "imported_from: arxiv" in fm
        assert 'imported_at: "2026-07-09T00:00:00Z"' in fm

    def test_body_has_title_abstract_and_versioned_source(self):
        text = ArxivSourceWriter().render(_work())
        assert "# Attention Is All You Need" in text
        assert "## Abstract\n\nThe dominant sequence transduction models..." in text
        # The abs URL carries the version, not the bare base id.
        assert "- arXiv: `https://arxiv.org/abs/1706.03762v5`" in text

    def test_doi_line_absent_when_no_doi(self):
        assert "- DOI:" not in ArxivSourceWriter().render(_work())

    def test_missing_abstract_is_stated_not_omitted(self):
        assert "_No abstract available._" in ArxivSourceWriter().render(_work(abstract=""))

    def test_include_abstract_false_drops_the_section(self):
        text = ArxivSourceWriter(include_abstract=False).render(_work())
        assert "## Abstract" not in text

    def test_no_title_renders_untitled(self):
        assert "# Untitled" in ArxivSourceWriter().render(_work(title=""))

    def test_optional_fields_omitted_when_absent(self):
        fm = _front_matter(ArxivSourceWriter().render(
            _work(authors=(), submitted=None, primary_category="", categories=(),
                  doi=None, journal_ref=None)))
        for absent in ("authors:", "year:", "primary_category:", "tags:", "doi:", "journal:"):
            assert absent not in fm
        # But the always-present keys survive.
        assert 'arxiv_id: "1706.03762"' in fm
        assert "arxiv_version: 5" in fm
        assert 'title: "Attention Is All You Need"' in fm
        assert "preprint: true" in fm

    def test_control_characters_cannot_break_the_front_matter(self):
        fm = _front_matter(ArxivSourceWriter().render(_work(title='a\nb: "c"')))
        assert 'title: "a\\nb: \\"c\\""' in fm


class TestPublishedButStillPreprint:
    """§5.2 D2: a journal_ref/doi means published, but the record stays a preprint."""

    def test_doi_is_a_bare_key_never_source_scoped(self):
        fm = _front_matter(ArxivSourceWriter().render(_work(doi="10.1145/3550547")))
        assert 'doi: "10.1145/3550547"' in fm
        # arxiv_doi would silently disable §7.1 cross-source dedup.
        assert "arxiv_doi:" not in fm

    def test_journal_ref_is_recorded(self):
        fm = _front_matter(ArxivSourceWriter().render(_work(journal_ref="NeurIPS 2017")))
        assert 'journal: "NeurIPS 2017"' in fm

    def test_preprint_stays_true_even_with_journal_and_doi(self):
        fm = _front_matter(ArxivSourceWriter().render(
            _work(journal_ref="NeurIPS 2017", doi="10.1145/3550547")))
        assert "preprint: true" in fm
        assert "preprint: false" not in fm

    def test_doi_appears_in_the_body_when_present(self):
        text = ArxivSourceWriter().render(_work(doi="10.1145/3550547"))
        assert "- DOI: 10.1145/3550547" in text


class TestWithdrawalIsSourceScoped:
    def test_author_withdrawal_names_the_agent_in_front_matter(self):
        fm = _front_matter(ArxivSourceWriter().render(_work(withdrawn_by="author")))
        assert "arxiv_withdrawn: true" in fm
        assert 'arxiv_withdrawn_by: "author"' in fm
        # Never a bare `withdrawn:` — that would read as an accepted claim.
        assert "\nwithdrawn:" not in fm

    def test_admin_withdrawal_records_admin(self):
        fm = _front_matter(ArxivSourceWriter().render(_work(withdrawn_by="admin")))
        assert 'arxiv_withdrawn_by: "admin"' in fm

    def test_withdrawal_keys_come_last(self):
        fm = _front_matter(ArxivSourceWriter().render(
            _work(withdrawn_by="admin"), imported_at="2026-07-09T00:00:00Z"))
        assert fm.index("imported_from:") < fm.index("arxiv_withdrawn:")
        assert fm.index("imported_at:") < fm.index("arxiv_withdrawn:")

    def test_author_body_warning_names_the_author_and_denies_retraction(self):
        text = ArxivSourceWriter().render(_work(withdrawn_by="author"))
        assert "withdrawn (by the author)" in text
        assert "Withdrawal is not retraction" in text
        # The word "retracted" is never used for an arXiv withdrawal.
        assert "retracted" not in text.lower()

    def test_admin_body_warning_names_arxiv_administrators(self):
        text = ArxivSourceWriter().render(_work(withdrawn_by="admin"))
        assert "withdrawn (by arXiv administrators)" in text
        assert "retracted" not in text.lower()

    def test_a_live_paper_carries_no_withdrawal_flag_or_warning(self):
        text = ArxivSourceWriter().render(_work())
        assert "arxiv_withdrawn" not in text
        assert "withdrawn" not in text.lower()


class TestFrontMatterStaysParseable:
    def test_export_parser_sees_only_intended_top_level_keys(self):
        fm = parse_front_matter(ArxivSourceWriter().render(
            _work(doi="10.1/x", journal_ref="J", withdrawn_by="admin")))
        assert fm["arxiv_id"] == "1706.03762"
        assert fm["title"] == "Attention Is All You Need"
        assert fm["doi"] == "10.1/x"
        assert fm["journal"] == "J"
        assert fm["preprint"] is True  # parse_front_matter coerces the YAML bool
        assert fm["arxiv_withdrawn_by"] == "admin"

    def test_every_front_matter_line_is_unindented(self):
        fm = _front_matter(ArxivSourceWriter().render(
            _work(doi="10.1/x", journal_ref="J", withdrawn_by="author")))
        for line in fm.splitlines():
            assert line == line.lstrip(), f"indented front-matter line: {line!r}"


class TestIdentityAndSlug:
    def test_identity_is_the_base_id_never_versioned(self):
        # P3 across versions rests on this: v5 and v6 share one identity.
        assert ArxivSourceWriter().identity_of(_work(version=5)) == "1706.03762"
        assert ArxivSourceWriter().identity_of(_work(version=6)) == "1706.03762"

    def test_cross_ids_uses_the_bare_doi_key(self):
        assert ArxivSourceWriter().cross_ids(_work(doi="10.1/x")) == {"doi": "10.1/x"}
        assert ArxivSourceWriter().cross_ids(_work(doi=None)) == {}

    def test_slug_year_comes_from_the_submission_date(self):
        # <published> (v1 submit) year, stable no matter which version is fetched.
        slug = ArxivSourceWriter().generate_slug(_work())
        assert slug == "ashish-vaswani-2017-attention-is-all-you-need.md"

    def test_slug_is_stable_across_versions(self):
        # A later version whose title changed still slugs from the submit year, but
        # identity (base id) is what actually keeps re-import idempotent.
        a = ArxivSourceWriter().generate_slug(_work(version=5))
        b = ArxivSourceWriter().generate_slug(_work(version=6))
        assert a == b

    def test_no_authors_becomes_anonymous(self):
        assert ArxivSourceWriter().generate_slug(_work(authors=())).startswith("anonymous-")

    def test_no_year_becomes_n_d(self):
        assert "-n-d-" in ArxivSourceWriter().generate_slug(_work(submitted=None))


class TestWrite:
    def test_writes_a_source_file(self, tmp_path):
        result = ArxivSourceWriter().write(_work(), tmp_path, "2026-07-09T00:00:00Z")
        assert result.status == "imported"
        assert result.path.exists()
        assert 'arxiv_id: "1706.03762"' in result.path.read_text(encoding="utf-8")

    def test_reimport_of_the_same_base_id_is_skipped(self, tmp_path):
        ArxivSourceWriter().write(_work(version=5), tmp_path)
        # A later version re-import matches on the base id and skips (P3).
        second = ArxivSourceWriter().write(_work(version=6, title="Different now"), tmp_path)
        assert second.status == "skipped"
        assert "arxiv_id match" in second.reason
        assert len(list((tmp_path / "sources").glob("*.md"))) == 1

    def test_existing_user_file_is_never_overwritten(self, tmp_path):
        sources = tmp_path / "sources"
        sources.mkdir()
        squatter = sources / ArxivSourceWriter().generate_slug(_work())
        squatter.write_text("# a user's own source\n", encoding="utf-8")
        result = ArxivSourceWriter().write(_work(), tmp_path)
        assert result.path != squatter
        assert squatter.read_text(encoding="utf-8").startswith("# a user's own source")

    def test_plan_creates_no_file(self, tmp_path):
        result = ArxivSourceWriter().plan(_work(), tmp_path)
        assert result.status == "imported"
        assert not result.path.exists()

    def test_distinct_papers_sharing_a_slug_get_a_suffix(self, tmp_path):
        w = ArxivSourceWriter()
        a = w.write(_work(arxiv_id="1706.03762"), tmp_path)
        b = w.write(_work(arxiv_id="1810.04805"), tmp_path)
        assert b.path.name.endswith("-2.md") and a.path != b.path


class TestCrossSourceDuplicates:
    def test_a_doi_already_from_openalex_is_merged(self, tmp_path):
        # The journal version arrived from OpenAlex; the arXiv preprint shares its
        # DOI and must be detected as the same paper (§7.1). As of Step 4c the
        # arXiv writer *merges* it into that original's sidecar (§7.3) rather than
        # writing twice or reporting a bare skip.
        OpenAlexSourceWriter().write(
            ParsedWork(openalex_id="W1", title="Attention Is All You Need",
                       authors=("Ashish Vaswani",), year=2017,
                       doi="10.1145/3550547"),
            tmp_path,
        )
        result = ArxivSourceWriter().write(_work(doi="10.1145/3550547"), tmp_path)
        assert result.status == "merged"
        assert "duplicate DOI" in result.reason

    def test_doi_match_is_case_insensitive(self, tmp_path):
        OpenAlexSourceWriter().write(
            ParsedWork(openalex_id="W1", title="X", doi="10.1145/3550547"), tmp_path)
        result = ArxivSourceWriter().write(_work(doi="10.1145/3550547".upper()), tmp_path)
        assert result.status == "merged"

    def test_papers_without_a_doi_are_not_deduplicated(self, tmp_path):
        w = ArxivSourceWriter()
        a = w.write(_work(arxiv_id="1706.03762", doi=None), tmp_path)
        b = w.write(_work(arxiv_id="1810.04805", doi=None), tmp_path)
        assert a.status == b.status == "imported"
        assert a.path != b.path
