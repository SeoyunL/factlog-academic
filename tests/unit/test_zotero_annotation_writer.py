# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Zotero annotation source writer (phase 3, #28)."""
from __future__ import annotations

from factlog.integrations.zotero.annotation_writer import (
    AnnotationResult,
    html_to_text,
    render_annotations,
    write_annotations,
)

BIB = {"zotero_key": "KH78JUPE", "title": "Neurosymbolic AI"}


def _hl(text="a passage", comment="", page="3", atype="highlight"):
    return {"data": {"itemType": "annotation", "annotationType": atype,
                     "annotationText": text, "annotationComment": comment,
                     "annotationPageLabel": page}}


def _note(html="<p>Comment: under review</p>"):
    return {"data": {"itemType": "note", "note": html}}


def _sources(tmp_path):
    return tmp_path / "sources"


class TestHtmlToText:
    def test_paragraphs_become_lines(self):
        assert html_to_text("<p>one</p><p>two</p>") == "one\ntwo"

    def test_br_and_entities(self):
        assert html_to_text("a<br/>b &amp; c") == "a\nb & c"

    def test_strips_tags_and_collapses(self):
        assert html_to_text("<div><b>bold</b>   text</div>") == "bold   text"

    def test_non_string(self):
        assert html_to_text(None) == ""

    def test_script_and_style_content_dropped(self):
        assert html_to_text("<script>alert('x')</script>keep") == "keep"
        assert html_to_text("<style>p{color:red}</style>body") == "body"

    def test_comment_dropped(self):
        assert html_to_text("a<!-- secret -->b") == "ab"

    def test_control_chars_removed(self):
        assert html_to_text("a\x1bb\x00c") == "abc"


class TestRender:
    def test_highlight_with_page_quote_comment(self):
        out = render_annotations(BIB, [_hl("key finding", "my note", "5")], [])
        assert 'zotero_key: "KH78JUPE"' in out
        assert "source_kind: annotations" in out
        assert "## Highlights" in out
        assert "### p. 5" in out
        assert "> key finding" in out
        assert "my note" in out

    def test_notes_section(self):
        out = render_annotations(BIB, [], [_note("<p>a note</p>")])
        assert "## Notes" in out
        assert "a note" in out

    def test_empty_when_nothing(self):
        assert render_annotations(BIB, [], []) == ""

    def test_empty_text_highlight_dropped(self):
        assert render_annotations(BIB, [_hl(text="", comment="")], []) == ""

    def test_comment_only_highlight_kept(self):
        out = render_annotations(BIB, [_hl(text="", comment="just a thought")], [])
        assert "just a thought" in out and "## Highlights" in out

    def test_no_imported_at_timestamp(self):
        assert "imported_at" not in render_annotations(BIB, [_hl()], [])

    def test_non_ascii(self):
        out = render_annotations({"zotero_key": "K", "title": "제목"}, [_hl("한글 강조", "메모")], [])
        assert "> 한글 강조" in out and "메모" in out


class TestWrite:
    def test_writes_notes_file(self, tmp_path):
        res = write_annotations(BIB, [_hl()], [], "paper-2025", tmp_path)
        assert res.status == "written"
        assert res.path == _sources(tmp_path) / "paper-2025-notes.md"
        assert res.path.read_text(encoding="utf-8").startswith("---\n")

    def test_no_content_writes_nothing(self, tmp_path):
        res = write_annotations(BIB, [], [], "paper-2025", tmp_path)
        assert res.status == "skipped" and res.path is None
        assert not _sources(tmp_path).exists()

    def test_idempotent_unchanged(self, tmp_path):
        write_annotations(BIB, [_hl()], [], "s", tmp_path)
        res = write_annotations(BIB, [_hl()], [], "s", tmp_path)
        assert res.status == "skipped" and res.reason == "unchanged"

    def test_fresh_update_on_new_highlight(self, tmp_path):
        write_annotations(BIB, [_hl("first")], [], "s", tmp_path)
        res = write_annotations(BIB, [_hl("first"), _hl("second")], [], "s", tmp_path)
        assert res.status == "updated"
        assert "second" in res.path.read_text(encoding="utf-8")

    def test_never_overwrites_non_ours_file(self, tmp_path):
        sources = _sources(tmp_path)
        sources.mkdir()
        squatter = sources / "s-notes.md"
        squatter.write_text("# a user's own notes\n", encoding="utf-8")
        res = write_annotations(BIB, [_hl()], [], "s", tmp_path)
        assert res.status == "skipped" and "not a zotero notes file" in res.reason
        assert squatter.read_text(encoding="utf-8").startswith("# a user's own notes")

    def test_returns_result_type(self, tmp_path):
        assert isinstance(write_annotations(BIB, [_hl()], [], "s", tmp_path), AnnotationResult)

    def test_marker_in_user_body_is_not_ours(self, tmp_path):
        # A user's own front-matter file whose BODY mentions the marker must not
        # be mistaken for ours (P4).
        sources = _sources(tmp_path)
        sources.mkdir()
        squatter = sources / "s-notes.md"
        squatter.write_text(
            "---\ntitle: My note\n---\n\nI use source_kind: annotations in factlog.\n",
            encoding="utf-8",
        )
        res = write_annotations(BIB, [_hl()], [], "s", tmp_path)
        assert res.status == "skipped" and "not a zotero notes file" in res.reason
        assert "My note" in squatter.read_text(encoding="utf-8")

    def test_long_title_still_detected_as_ours(self, tmp_path):
        bib = {"zotero_key": "K", "title": "T" * 600}
        first = write_annotations(bib, [_hl("first")], [], "s", tmp_path)
        assert first.status == "written"
        res = write_annotations(bib, [_hl("first"), _hl("second")], [], "s", tmp_path)
        assert res.status == "updated"  # our own file recognized despite long title

    def test_updated_then_idempotent(self, tmp_path):
        write_annotations(BIB, [_hl("a")], [], "s", tmp_path)
        write_annotations(BIB, [_hl("a"), _hl("b")], [], "s", tmp_path)  # updated
        res = write_annotations(BIB, [_hl("a"), _hl("b")], [], "s", tmp_path)
        assert res.status == "skipped" and res.reason == "unchanged"

    def test_multiline_highlight_quoted_per_line(self, tmp_path):
        res = write_annotations(BIB, [_hl("line one\nline two")], [], "s", tmp_path)
        text = res.path.read_text(encoding="utf-8")
        assert "> line one\n> line two" in text

    def test_missing_key_skipped(self, tmp_path):
        res = write_annotations({"zotero_key": "", "title": "T"}, [_hl()], [], "s", tmp_path)
        assert res.status == "skipped" and "zotero_key" in res.reason
        assert not _sources(tmp_path).exists()

    def test_unsafe_stem_skipped(self, tmp_path):
        for bad in ("../evil", "a/b", "a\\b"):
            res = write_annotations(BIB, [_hl()], [], bad, tmp_path)
            assert res.status == "skipped" and "unsafe" in res.reason
