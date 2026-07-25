# SPDX-License-Identifier: Apache-2.0
"""Regression tests for validate's heading-anchor slug logic (#105)."""
from __future__ import annotations

import validate


class TestSlugifyHeading:
    def test_plain(self):
        assert validate.slugify_heading("My Heading") == "my-heading"

    def test_strips_punctuation(self):
        # The bug: "## Plan (v2)" must anchor as "plan-v2", not "plan-(v2)".
        assert validate.slugify_heading("Plan (v2)") == "plan-v2"

    def test_keeps_unicode_letters(self):
        assert validate.slugify_heading("한글 제목") == "한글-제목"

    def test_keeps_existing_hyphen(self):
        assert validate.slugify_heading("Pre-flight Check") == "pre-flight-check"


class TestHeadingSlugs:
    def test_styled_heading_anchor_resolves(self):
        slugs = validate.heading_slugs("# Plan (v2)\n\nbody\n")
        assert "plan-v2" in slugs

    def test_duplicate_headings_suffixed(self):
        slugs = validate.heading_slugs("# Notes\n## Notes\n")
        assert "notes" in slugs
        assert "notes-1" in slugs

    def test_legacy_naive_slug_still_accepted(self):
        # Backward compat: a ref authored against the old slug still validates.
        slugs = validate.heading_slugs("# Plan (v2)\n")
        assert "plan-(v2)" in slugs


class TestHeadingSlugsDelegatesToMdLines:
    """heading_slugs asks md_lines which lines are headings, so it inherits the
    fence and syntax rules a renderer follows (#521).
    """

    def test_heading_inside_fence_is_not_an_anchor(self):
        # The #521 bug: a ``## Fake Anchor`` written as an example inside a code
        # fence was read as a real heading, so a ref to #fake-anchor — an anchor
        # no renderer emits — validated. Reverting the md_lines delegation to
        # ``startswith("#")`` brings the fake anchor back and fails this.
        slugs = validate.heading_slugs("# Real\n```\n## Fake Anchor\n```\n")
        assert "fake-anchor" not in slugs
        assert "real" in slugs

    def test_atx_closing_sequence_matches_renderer(self):
        # md_lines drops a closing ``##`` the way a renderer does: the title is
        # 'foo', not 'foo ##'.
        slugs = validate.heading_slugs("## foo ##\n")
        assert "foo" in slugs
        assert "foo-" not in slugs

    def test_spaceless_hash_is_not_a_heading(self):
        # ``#plan-v2`` has no space after the marker, so it is a paragraph, not a
        # heading — the old line scan invented a 'plan-v2' anchor for it.
        assert validate.heading_slugs("#plan-v2\n") == set()

    def test_duplicate_and_unicode_and_legacy_preserved(self):
        # No regression in the slug machinery itself.
        assert validate.heading_slugs("# Notes\n## Notes\n") >= {"notes", "notes-1"}
        assert "한글-제목" in validate.heading_slugs("# 한글 제목\n")
        assert validate.heading_slugs("# Plan (v2)\n") >= {"plan-v2", "plan-(v2)"}


class TestValidateSourceRef:
    def test_styled_section_no_longer_false_errors(self, tmp_path):
        (tmp_path / "doc.md").write_text("# Plan (v2)\n\nbody\n", encoding="utf-8")
        assert validate.validate_source_ref(tmp_path, "doc.md#plan-v2") is None

    def test_missing_section_still_reported(self, tmp_path):
        (tmp_path / "doc.md").write_text("# Intro\n", encoding="utf-8")
        err = validate.validate_source_ref(tmp_path, "doc.md#nope")
        assert err and "section does not exist" in err

    def test_fenced_heading_ref_rejected(self, tmp_path):
        # End-to-end of the #521 bug through the real call site.
        (tmp_path / "doc.md").write_text(
            "# Real\n```\n## Fake Anchor\n```\n", encoding="utf-8"
        )
        err = validate.validate_source_ref(tmp_path, "doc.md#fake-anchor")
        assert err and "section does not exist" in err
        assert validate.validate_source_ref(tmp_path, "doc.md#real") is None

    def test_front_matter_does_not_leak_anchors(self, tmp_path):
        # A source as the writers render it: YAML front matter, then a body heading.
        # The closing ``---`` has a Setext-underline shape, so feeding raw source to
        # md_lines would anchor the whole front matter as a level-2 heading. The
        # front_matter_body strip prevents that; removing it fails this test (and
        # only this one — the fence test above stays green).
        (tmp_path / "src.md").write_text(
            '---\ntitle: "A paper"\nauthors: [Ada, Bob]\n---\n\n# Real Heading\n',
            encoding="utf-8",
        )
        # The real body heading still anchors.
        assert validate.validate_source_ref(tmp_path, "src.md#real-heading") is None
        # Fed raw, md_lines reads the closing ``---`` as a Setext underline over the
        # YAML paragraph and coins ``title-a-paper-authors-ada-bob`` as an anchor.
        # This is the exact slug that leaks without the strip, so a ref to it must
        # be rejected — dropping front_matter_body (feeding read(path)) fails here.
        err = validate.validate_source_ref(tmp_path, "src.md#title-a-paper-authors-ada-bob")
        assert err and "section does not exist" in err
