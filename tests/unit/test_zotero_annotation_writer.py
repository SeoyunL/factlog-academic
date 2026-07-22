# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Zotero annotation source writer (phase 3, #28)."""
from __future__ import annotations

from factlog.integrations.zotero._textio import yaml_scalar
from factlog.integrations.zotero.annotation_writer import (
    _HEAD_SCAN_CHARS,
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


def _assert_write_refused(tmp_path, text, reason="not a zotero notes file"):
    """Writing over a user's file is refused *and* the file survives untouched.

    Snapshots the bytes and mtime before the write, so the check cannot pass by
    comparing the file to its own post-write state. ``reason`` is asserted too:
    the two refusals say different things and must not be reported alike (#430).
    """
    path = _squatter(tmp_path, text)
    before, before_mtime = path.read_bytes(), path.stat().st_mtime_ns
    res = write_annotations(BIB, [_hl()], [], "s", tmp_path)
    assert res.status == "skipped" and reason in res.reason
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
        # user's body. Ownership must not be claimed (#418), and the refusal says
        # we could not classify the file rather than claiming it is not ours (#430).
        _assert_write_refused(
            tmp_path,
            "---\ntitle: My note\nsource_kind: annotations\n\nmy own notes, no fence\n",
            reason="does not close inside the scanned head",
        )

    def test_closing_fence_past_scan_window_is_not_ours(self, tmp_path):
        # The block does close, but past the scanned head, so the marker's position
        # is again unknowable from the head alone (#418).
        filler = "".join(f"x: {'y' * 76}\n" for _ in range(60))
        text = f"---\ntitle: My note\nsource_kind: annotations\n{filler}---\n\nbody\n"
        assert text.index("\n---\n\nbody") > _HEAD_SCAN_CHARS
        _assert_write_refused(tmp_path, text, reason="does not close inside the scanned head")

    def test_unterminated_and_foreign_reasons_differ(self, tmp_path):
        # Both refusals skip, but a reader has to be able to tell "someone else's
        # file" from "a file we cannot classify" — reporting the first for the
        # second was the false report in #430.
        foreign = write_annotations(BIB, [_hl()], [], "s", tmp_path)
        assert foreign.status == "written"  # sanity: the helper below overwrites it
        _squatter(tmp_path, "---\ntitle: mine\n---\n\nbody\n")
        a = write_annotations(BIB, [_hl()], [], "s", tmp_path)
        _squatter(tmp_path, "---\ntitle: mine\nsource_kind: annotations\nno fence\n")
        b = write_annotations(BIB, [_hl()], [], "s", tmp_path)
        assert a.status == b.status == "skipped"
        assert a.reason != b.reason

    def test_long_title_still_detected_as_ours(self, tmp_path):
        bib = {"zotero_key": "K", "title": "T" * 600}
        first = write_annotations(bib, [_hl("first")], [], "s", tmp_path)
        assert first.status == "written"
        res = write_annotations(bib, [_hl("first"), _hl("second")], [], "s", tmp_path)
        assert res.status == "updated"  # our own file recognized despite long title

    def test_title_is_capped_so_the_fence_stays_inside_the_head(self, tmp_path):
        # #418 pinned the cliff here as a characterization: past a ~4000-character
        # title the closing fence left the head and we disowned our own file for
        # good. #430 removed the cliff by capping the emitted title, so this test
        # now pins the invariant that replaced it — the fence lands inside the head
        # whatever the title is — while keeping the property that made the old test
        # useful: the boundary is DERIVED from the front-matter layout and then
        # contrasted with the measured number, so it breaks loudly instead of going
        # vacuous. Measured, rather than assumed: a layout change trips the two
        # derived-vs-measured contrasts (this one and the zotero_key one below) and
        # nothing else; an off-by-one in the cap trips three invariant assertions.
        probe_len = 100
        probe = render_annotations({"zotero_key": "K", "title": "T" * probe_len}, [_hl()], [])
        # The longest title still emitted verbatim is the one putting the whole
        # "\n---" fence at exactly _HEAD_SCAN_CHARS - 4; beyond it the cap bites.
        last_verbatim = probe_len + (_HEAD_SCAN_CHARS - 3 - probe.index("\n---", 3)) - 1
        assert last_verbatim == 4016  # measured for the current front-matter layout

        for title_len in (last_verbatim, last_verbatim + 1, 20000):
            title = "T" * title_len
            bib = {"zotero_key": "K", "title": title}
            text = render_annotations(bib, [_hl()], [])
            assert text.index("\n---", 3) + len("\n---") <= _HEAD_SCAN_CHARS
            emitted = text.split("title: ", 1)[1].split("\n", 1)[0]
            assert (emitted == f'"{title}"') is (title_len == last_verbatim)

            target = tmp_path / str(title_len)
            assert write_annotations(bib, [_hl("first")], [], "s", target).status == "written"
            res = write_annotations(bib, [_hl("first"), _hl("second")], [], "s", target)
            assert res.status == "updated"  # no length disowns our own file any more

    def test_capped_title_is_marked_as_cut(self, tmp_path):
        # A cut title must not read as if it were the real one, and the heading must
        # show the same title the front matter does. Compared through yaml_scalar
        # rather than by slicing the quotes off: the front matter carries the
        # ESCAPED title and the heading the raw one, so a backslash-heavy title
        # would fail a naive comparison while the code is right.
        for title in ("T" * 20000, "\\" * 20000, 'a"b' * 8000):
            text = render_annotations({"zotero_key": "K", "title": title}, [_hl()], [])
            emitted = text.split("title: ", 1)[1].split("\n", 1)[0]
            assert emitted.endswith('…"')
            heading = next(ln for ln in text.splitlines() if ln.startswith("# Annotations — "))
            assert yaml_scalar(heading[len("# Annotations — ") :]) == emitted

    def test_escape_heavy_title_is_capped_on_emitted_width(self, tmp_path):
        # yaml_scalar doubles a backslash, so counting input characters would let
        # the front matter grow to twice the budget and push the fence back out.
        for ch in ("\\", '"', "\t"):
            bib = {"zotero_key": "K", "title": ch * 20000}
            text = render_annotations(bib, [_hl()], [])
            assert text.index("\n---", 3) + len("\n---") <= _HEAD_SCAN_CHARS
            target = tmp_path / str(ord(ch))
            assert write_annotations(bib, [_hl("first")], [], "s", target).status == "written"
            res = write_annotations(bib, [_hl("first"), _hl("second")], [], "s", target)
            assert res.status == "updated"

    def test_long_zotero_key_shrinks_the_title_not_the_fence(self, tmp_path):
        # The budget is shared: a longer key leaves less room for the title. Probed
        # AT and PAST the point where the key alone would exhaust it, because that
        # is where sharing stops being enough and the key has to be capped too —
        # capping only the title moved the #430 cliff onto the key rather than
        # removing it (measured: it reappeared at key_len=4018). Derived from the
        # layout and then contrasted, like the title boundary above.
        probe_key, probe_title = "K", "T" * 100
        probe = render_annotations({"zotero_key": probe_key, "title": probe_title}, [_hl()], [])
        slack = _HEAD_SCAN_CHARS - (probe.index("\n---", 3) + len("\n---"))
        exhausts_budget = len(probe_key) + len(probe_title) + slack
        assert exhausts_budget == 4017  # longest key still emitted verbatim, measured

        for key_len in (3000, exhausts_budget, exhausts_budget + 1, 20000):
            bib = {"zotero_key": "K" * key_len, "title": "T" * 20000}
            text = render_annotations(bib, [_hl()], [])
            assert text.index("\n---", 3) + len("\n---") <= _HEAD_SCAN_CHARS
            emitted_key = text.split("zotero_key: ", 1)[1].split("\n", 1)[0]
            assert (emitted_key == '"' + "K" * key_len + '"') is (key_len <= exhausts_budget)

            target = tmp_path / str(key_len)
            assert write_annotations(bib, [_hl("first")], [], "s", target).status == "written"
            res = write_annotations(bib, [_hl("first"), _hl("second")], [], "s", target)
            assert res.status == "updated"

    def test_key_is_served_before_the_title(self, tmp_path):
        # When the two cannot both fit, the key wins the budget: it identifies the
        # item, the title only describes it.
        text = render_annotations({"zotero_key": "K" * 2000, "title": "T" * 20000}, [_hl()], [])
        assert '"' + "K" * 2000 + '"' in text  # key verbatim
        assert text.split("title: ", 1)[1].split("\n", 1)[0].endswith('…"')  # title cut

    def test_large_body_still_detected_as_ours(self, tmp_path):
        # Our own fence sits at the top, so a body far past the scanned head must
        # not turn the fail-closed rule into a refusal to update our own file.
        many = [_hl("z" * 200, page=str(i)) for i in range(40)]
        first = write_annotations(BIB, many, [], "s", tmp_path)
        assert first.status == "written"
        assert len(first.path.read_bytes()) > _HEAD_SCAN_CHARS
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
