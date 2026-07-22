# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Zotero annotation source writer (phase 3, #28)."""
from __future__ import annotations

from factlog.integrations.zotero.annotation_writer import (
    _HEAD_SCAN_BYTES,
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


def _squatter(tmp_path, text):
    """A user's own file sitting on the target path."""
    sources = _sources(tmp_path)
    sources.mkdir(exist_ok=True)
    path = sources / "s-notes.md"
    path.write_text(text, encoding="utf-8")
    return path


def _assert_write_refused(tmp_path, text):
    """Writing over a user's file is refused *and* the file survives untouched.

    Snapshots the bytes and mtime before the write, so the check cannot pass by
    comparing the file to its own post-write state.
    """
    path = _squatter(tmp_path, text)
    before, before_mtime = path.read_bytes(), path.stat().st_mtime_ns
    res = write_annotations(BIB, [_hl()], [], "s", tmp_path)
    assert res.status == "skipped" and "not a zotero notes file" in res.reason
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime
    return path


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

    def test_marker_is_first_line_after_opening_fence(self):
        # _is_ours only reads the head, so the marker's position in the block is a
        # contract, not a formatting choice. Asserted on the rendered text because
        # every round-trip test keeps the whole front matter inside the head and so
        # passes no matter where in the block the marker sits.
        out = render_annotations({"zotero_key": "K", "title": "T" * 600}, [_hl()], [])
        assert out.split("\n")[:2] == ["---", "source_kind: annotations"]

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
        squatter = _assert_write_refused(tmp_path, "# a user's own notes\n")
        assert squatter.read_text(encoding="utf-8").startswith("# a user's own notes")

    def test_returns_result_type(self, tmp_path):
        assert isinstance(write_annotations(BIB, [_hl()], [], "s", tmp_path), AnnotationResult)

    def test_marker_in_user_body_is_not_ours(self, tmp_path):
        # A user's own front-matter file whose BODY mentions the marker must not
        # be mistaken for ours (P4). The block closes inside the head here, so this
        # is the control for the two unclosed-block cases below.
        squatter = _assert_write_refused(
            tmp_path,
            "---\ntitle: My note\n---\n\nI use source_kind: annotations in factlog.\n",
        )
        assert "My note" in squatter.read_text(encoding="utf-8")

    def test_unclosed_front_matter_with_marker_is_not_ours(self, tmp_path):
        # The block never closes, so the marker line cannot be told apart from the
        # user's body. Ownership must not be claimed (#418).
        _assert_write_refused(
            tmp_path,
            "---\ntitle: My note\nsource_kind: annotations\n\nmy own notes, no fence\n",
        )

    def test_closing_fence_past_scan_window_is_not_ours(self, tmp_path):
        # The block does close, but past the scanned head, so the marker's position
        # is again unknowable from the head alone (#418).
        filler = "".join(f"x: {'y' * 76}\n" for _ in range(60))
        text = f"---\ntitle: My note\nsource_kind: annotations\n{filler}---\n\nbody\n"
        assert text.index("\n---\n\nbody") > _HEAD_SCAN_BYTES
        _assert_write_refused(tmp_path, text)

    def test_long_title_still_detected_as_ours(self, tmp_path):
        bib = {"zotero_key": "K", "title": "T" * 600}
        first = write_annotations(bib, [_hl("first")], [], "s", tmp_path)
        assert first.status == "written"
        res = write_annotations(bib, [_hl("first"), _hl("second")], [], "s", tmp_path)
        assert res.status == "updated"  # our own file recognized despite long title

    def test_over_long_front_matter_stops_updating_our_own_file(self, tmp_path):
        # CHARACTERIZATION, NOT AN ENDORSEMENT. Front matter that does not close
        # inside the head makes us disown a file we wrote ourselves: it is written
        # once, never updated again, and the skip reason ("not a zotero notes file")
        # is false. Left as-is because reaching it needs a ~4000-character title and
        # the alternative — a wider head — puts real user files back at risk of
        # being overwritten (see _HEAD_SCAN_BYTES). Follow-up: #430, which proposes
        # capping the emitted title so the trade-off disappears. This test pins the
        # current behaviour, so fixing #430 must flip it rather than delete it: the
        # over-long case should then assert "updated", not "skipped".
        probe_len = 100
        probe = render_annotations({"zotero_key": "K", "title": "T" * probe_len}, [_hl()], [])
        # Accepted while the whole "\n---" fence fits in the head, so the last title
        # length still recognized puts the fence at exactly _HEAD_SCAN_BYTES - 4.
        last_ok = probe_len + (_HEAD_SCAN_BYTES - 3 - probe.index("\n---", 3)) - 1
        assert last_ok == 4016  # measured cliff for the current front-matter layout

        for title_len, expected in ((last_ok, "updated"), (last_ok + 1, "skipped")):
            bib = {"zotero_key": "K", "title": "T" * title_len}
            target = tmp_path / str(title_len)
            assert write_annotations(bib, [_hl("first")], [], "s", target).status == "written"
            res = write_annotations(bib, [_hl("first"), _hl("second")], [], "s", target)
            assert res.status == expected
        assert "not a zotero notes file" in res.reason  # the false reason, pinned

    def test_large_body_still_detected_as_ours(self, tmp_path):
        # Our own fence sits at the top, so a body far past the scanned head must
        # not turn the fail-closed rule into a refusal to update our own file.
        many = [_hl("z" * 200, page=str(i)) for i in range(40)]
        first = write_annotations(BIB, many, [], "s", tmp_path)
        assert first.status == "written"
        assert len(first.path.read_bytes()) > _HEAD_SCAN_BYTES
        res = write_annotations(BIB, many + [_hl("newest")], [], "s", tmp_path)
        assert res.status == "updated"

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
