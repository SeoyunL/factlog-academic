# SPDX-License-Identifier: Apache-2.0
"""The line structure of a markdown document (#515).

Which lines a renderer shows as code, where each heading begins and ends, and which
lines are list items. Nothing here names a review category or a KB file: those are
the review-section contract's, pinned in tests/unit/test_review_sections.py, and
this is the layer under it.

Every case here was a measured failure once. A heading quoted inside a code fence
was read as the section itself; a well-formed document with a four-space-indented
``` example read as permanently unclosed; a bullet quoted as a format example was
counted as a filed bullet at one end and as a duplicate at the other, and a
needs_review row vanished from the file a human reads while the KB reported valid.
"""
from __future__ import annotations

import pytest

from factlog.md_lines import (
    Heading,
    bullets,
    ends_inside_fence,
    fence_flags,
    headings,
    section_body_end,
    unclosed_fence_line,
)

TITLE = "# Open Questions"


class TestFenceScanning:
    """What counts as a fence, and what an unclosed one means.

    One scan answers all of it. It was written out twice — once for headings, once
    for "does the file end mid-fence" — and two copies of the rule for what a
    section is are the disease #495 was about.
    """

    @pytest.mark.parametrize("marker", ["```", "~~~"])
    def test_either_fence_marker_can_leave_a_file_open(self, marker):
        # `~~~` had no test at all: deleting it from the scan changed nothing.
        doc = f"{TITLE}\n\n{marker}\n## 모호한 관계명\n"
        assert ends_inside_fence(doc) is True
        assert unclosed_fence_line(doc) == 3
        assert [h.text for h in headings(doc)] == [TITLE]  # the fenced one is hidden

    @pytest.mark.parametrize("marker", ["```", "~~~"])
    def test_a_closed_fence_leaves_the_file_open_to_writing(self, marker):
        doc = f"{TITLE}\n\n{marker}\n## 모호한 관계명\n{marker}\n"
        assert ends_inside_fence(doc) is False
        assert unclosed_fence_line(doc) is None

    def test_three_spaces_of_indent_is_still_a_fence(self):
        # CommonMark: up to three spaces of indent, and it is a fence.
        doc = f"{TITLE}\n\n   ```\n## 모호한 관계명\n"
        assert ends_inside_fence(doc) is True

    def test_four_spaces_of_indent_is_an_indented_code_block_not_a_fence(self):
        """A well-formed document was being read as permanently unclosed.

        At four spaces the line is an indented code block — content that opens
        nothing — so the headings below it are real and the file validates.
        """
        doc = (
            f"{TITLE}\n\n## 중복 개념 후보\n\n예시:\n\n    ```\n\n"
            "## 모호한 관계명\n\n## 출처 부족\n\n## 기존 내용과 충돌할 수 있는 항목\n"
        )
        assert ends_inside_fence(doc) is False
        assert len(headings(doc)) == 5

    def test_an_odd_number_of_fences_really_does_end_open(self):
        # Not a false positive: the third fence opens and nothing closes it.
        doc = f"{TITLE}\n\n```\na\n```\n\n## 모호한 관계명\n\n```\nb\n"
        assert ends_inside_fence(doc) is True
        assert unclosed_fence_line(doc) == 9

    def test_a_tilde_block_may_quote_a_backtick_line(self):
        """The document the review-section reference tells people to write.

        Anyone spelling out the bullet format wraps the example in ``~~~`` precisely
        so the backticks inside need no escaping. Toggling on any marker made the
        quoted ``` "close" the tilde fence and the real ``~~~`` re-open it, so a
        correct file read as permanently unclosed: both writers refused it forever,
        pointing at the line that closes it and asking for it to be closed.
        """
        doc = (
            f"{TITLE}\n\n## 중복 개념 후보\n\n## 모호한 관계명\n\n형식 예시:\n\n"
            "~~~\n- needs_review: X / r / Y\n```\n~~~\n\n"
            "## 출처 부족\n\n## 기존 내용과 충돌할 수 있는 항목\n"
        )
        assert ends_inside_fence(doc) is False
        assert unclosed_fence_line(doc) is None
        assert len(headings(doc)) == 5
        assert bullets(doc) == []  # the example is still not a filed bullet

    def test_a_backtick_block_may_quote_a_tilde_line(self):
        doc = f"{TITLE}\n\n```\n~~~\n```\n\n## 모호한 관계명\n"
        assert ends_inside_fence(doc) is False
        assert [h.text for h in headings(doc)] == [TITLE, "## 모호한 관계명"]

    def test_a_longer_run_closes_but_a_shorter_one_does_not(self):
        # CommonMark: the closing run is at least as long as the opening one.
        assert ends_inside_fence(f"{TITLE}\n\n```\nx\n`````\n") is False
        assert ends_inside_fence(f"{TITLE}\n\n`````\nx\n```\n") is True

    def test_a_marker_carrying_an_info_string_cannot_close(self):
        # ```python opens a block; it never ends one.
        assert ends_inside_fence(f"{TITLE}\n\n```\nx\n```python\n") is True

    def test_the_flags_line_up_with_the_lines(self):
        doc = "a\n```\nb\n```\nc\n"
        flags, opened_at = fence_flags(doc)
        assert flags == [False, True, True, True, False]
        assert opened_at is None


class TestBullets:
    """What counts as a filed bullet — the reader both the producer and validator use.

    They each had their own before, and both counted lines inside code fences. A KB
    that documents its own bullet format in a fence therefore had the example stand in
    for the queue: the producer skipped the first real bullet as a duplicate of it,
    and the validator accepted it as proof that review bullets existed.
    """

    def test_a_bullet_in_a_fence_is_not_a_filed_bullet(self):
        text = f"{TITLE}\n\n형식 예시:\n\n```\n- needs_review: X / r / Y\n```\n"
        assert bullets(text) == []

    def test_real_bullets_are_returned_raw(self):
        text = f"{TITLE}\n\n## 출처 부족\n- needs_review: X / r / Y\n  - nested\n"
        assert bullets(text) == ["- needs_review: X / r / Y", "  - nested"]

    def test_the_example_and_the_real_bullet_are_told_apart(self):
        bullet = "- needs_review: W / related_to / G"
        text = f"{TITLE}\n\n```\n{bullet}\n```\n\n## 모호한 관계명\n{bullet}\n"
        assert bullets(text) == [bullet]

    def test_a_dash_that_is_not_a_list_item_does_not_count(self):
        assert bullets(f"{TITLE}\n\n---\n-notabullet\n") == []


class TestAtxHeadings:
    """Which ATX lines are headings, at what level, and where they sit."""

    def test_a_heading_is_a_place_and_a_level(self):
        doc = "# Title\n\n## 출처 부족\n- a\n"
        assert headings(doc) == [
            Heading(start=0, end=1, level=1, text="# Title"),
            Heading(start=2, end=3, level=2, text="## 출처 부족"),
        ]

    def test_every_level_is_returned(self):
        doc = "# a\n## b\n### c\n#### d\n##### e\n###### f\n"
        assert [h.level for h in headings(doc)] == [1, 2, 3, 4, 5, 6]

    def test_seven_hashes_is_not_a_heading(self):
        assert headings("####### x\n") == []

    def test_a_marker_with_no_space_after_it_is_not_a_heading(self):
        assert headings("##출처\n") == []

    def test_a_bare_marker_is_a_heading(self):
        assert [h.level for h in headings("#\n##\n")] == [1, 2]

    def test_the_text_is_the_raw_line(self):
        # Trailing whitespace included: the text goes into a warning a human reads
        # against the file, and re-spelling it would make the two disagree.
        assert headings("## 출처 부족  \n")[0].text == "## 출처 부족  "

    def test_an_indented_heading_is_not_one_here(self):
        # Narrower than CommonMark on purpose; see `_atx_level`.
        assert headings("   ## 출처 부족\n") == []

    def test_a_fenced_heading_is_never_a_heading(self):
        doc = "```\n## 출처 부족\n```\n\n## 출처 부족\n"
        assert [h.start for h in headings(doc)] == [4]


class TestSectionBodyEnd:
    """Where a section's body ends — where a new line goes.

    This used to be a `startswith("## ")` walk of insert_bullet's own, which stopped
    at a fenced example and inserted the bullet into the code block.
    """

    FENCED = (
        "# Open Questions\n\n"          # 0,1
        "## 중복 개념 후보\n\n"           # 2,3
        "```\n"                          # 4
        "## 출처 부족\n"                  # 5  <- example, not a section
        "```\n\n"                        # 6,7
        "## 출처 부족\n"                  # 8  <- the real one
        "- 기존 항목\n"                   # 9
    )

    def _at(self, text: str, start: int) -> Heading:
        return next(h for h in headings(text) if h.start == start)

    def test_a_fenced_heading_is_never_the_section(self):
        assert [h.start for h in headings(self.FENCED) if "출처" in h.text] == [8]

    def test_a_section_runs_to_the_next_real_heading(self):
        #    0                 1   2               3     4   5            6
        doc = "# Open Questions\n\n## 중복 개념 후보\n- a\n\n## 출처 부족\n- b\n"
        assert section_body_end(doc, self._at(doc, 2)) == 5

    def test_a_section_with_no_heading_after_it_runs_to_the_end(self):
        doc = "# Open Questions\n\n## 중복 개념 후보\n- a\n"
        assert section_body_end(doc, self._at(doc, 2)) == 4

    def test_a_fenced_heading_does_not_cut_a_section_short(self):
        """The end scan skips fenced `## ` lines too, or a bullet lands in the fence."""
        #    0                 1   2               3     4     5            6     7     8   9
        doc = (
            "# Open Questions\n\n## 중복 개념 후보\n- a\n"
            "```\n## 출처 부족\n```\n"
            "- b\n\n## 출처 부족\n"
        )
        assert section_body_end(doc, self._at(doc, 2)) == 9

    def test_a_subsection_does_not_end_its_parent(self):
        doc = "## 출처 부족\n### 세부\n- a\n## 다음\n"
        assert section_body_end(doc, self._at(doc, 0)) == 3

    def test_a_level_one_heading_does_end_a_level_two_section(self):
        doc = "## 출처 부족\n- a\n# 다른 문서\n"
        assert section_body_end(doc, self._at(doc, 0)) == 2


class TestSetextHeadings:
    """``출처`` over ``----`` is a heading, and a renderer decides which ones are.

    Two lines, so most of what can go wrong is about the line above the underline.
    Every expectation here was checked against markdown-it-py in commonmark mode;
    where a case looks surprising, the renderer is the reason.
    """

    def test_a_dash_underline_is_a_level_two_heading(self):
        assert headings("출처\n----\n") == [
            Heading(start=0, end=2, level=2, text="출처\n----")
        ]

    def test_an_equals_underline_is_a_level_one_heading(self):
        # Which is why it is not a review section: that contract asks for level 2.
        assert headings("출처\n====\n") == [
            Heading(start=0, end=2, level=1, text="출처\n====")
        ]

    def test_one_dash_is_enough(self):
        assert [h.level for h in headings("출처\n-\n")] == [2]

    def test_three_spaces_of_indent_is_still_an_underline(self):
        assert [h.level for h in headings("출처\n   ----\n")] == [2]

    def test_four_spaces_of_indent_is_an_indented_code_block(self):
        assert headings("출처\n    ----\n") == []

    def test_trailing_whitespace_after_the_underline_is_allowed(self):
        assert [h.level for h in headings("출처\n----   \n")] == [2]

    def test_a_mixed_run_is_not_an_underline(self):
        assert headings("출처\n-=-\n") == []

    def test_a_list_item_over_dashes_is_a_list_and_a_rule(self):
        """The worst false positive there is, and the renderer says so.

        ``- a`` opens a list and ``출처`` lazily continues that item, so the
        ``----`` below is a thematic break *outside* the list. Reading it as a
        heading would tell the file it has a section a reader never sees — the exact
        disagreement between scanner and renderer this module exists to prevent.
        """
        assert headings("- a\n----\n") == []
        assert headings("- a\n출처\n----\n") == []
        assert headings("1. a\n출처\n----\n") == []
        assert headings("* a\n출처\n----\n") == []
        assert headings("+ a\n출처\n----\n") == []

    def test_a_paragraph_after_a_blank_line_below_a_list_is_a_heading(self):
        # The blank line closes the item, so this one really is top-level.
        assert [h.start for h in headings("- a\n\n출처\n----\n")] == [2]

    def test_an_atx_heading_over_dashes_is_a_heading_and_a_rule(self):
        assert [h.text for h in headings("# Open Questions\n----\n")] == [
            "# Open Questions"
        ]

    def test_a_blank_line_over_dashes_is_a_rule(self):
        assert headings("출처\n\n----\n") == []

    def test_a_paragraph_right_under_an_atx_heading_still_underlines(self):
        # An ATX heading ends a block, so a paragraph may start on the next line.
        assert [(h.start, h.level) for h in headings("## x\n출처\n----\n")] == [
            (0, 2),
            (1, 2),
        ]

    def test_a_fenced_title_or_underline_is_not_a_heading(self):
        assert headings("```\n출처\n----\n```\n") == []          # both inside
        assert headings("출처\n```\n----\n```\n") == []          # underline inside

    def test_a_block_quote_heading_is_not_a_top_level_one(self):
        assert headings("> 출처\n> ----\n") == []

    def test_a_multi_line_paragraph_starts_the_heading_at_its_first_line(self):
        assert headings("foo\n출처\n----\n") == [
            Heading(start=0, end=3, level=2, text="foo\n출처\n----")
        ]

    def test_a_qualified_heading_carries_its_keyword(self):
        (found,) = headings("출처 (근거 강도 부족)\n-----\n")
        assert found.level == 2 and "출처" in found.text

    def test_an_underline_on_the_last_line_still_counts(self):
        assert [h.level for h in headings("출처\n----")] == [2]

    def test_a_body_line_repeating_the_title_is_not_a_second_heading(self):
        """What #500 was: the old lookup found this line, not the heading.

        `lines.index("출처")` answers "which line says 출처", and that is line 0 in
        an ATX file only by luck. Here the heading is two lines and its first line
        is not unique.
        """
        doc = "출처\n----\n- x\n\n출처\n"
        assert [(h.start, h.end) for h in headings(doc)] == [(0, 2)]

    def test_a_section_ends_at_the_next_title_line_not_its_underline(self):
        doc = "출처\n----\n- x\n\n충돌\n----\n"
        first, second = headings(doc)
        assert (first.start, first.end) == (0, 2)
        assert (second.start, second.end) == (4, 6)
        assert section_body_end(doc, first) == 4

    def test_atx_and_setext_mix_in_one_file(self):
        doc = "# Open Questions\n\n중복\n----\n- a\n\n## 출처 부족\n- b\n"
        assert [(h.start, h.level) for h in headings(doc)] == [(0, 1), (2, 2), (6, 2)]

    def test_an_underline_does_not_reach_back_into_a_finished_heading(self):
        # Both are headings of one line each, not one heading of three.
        assert [(h.start, h.end) for h in headings("abc\n===\n출처\n====\n")] == [
            (0, 2),
            (2, 4),
        ]

    def test_a_thematic_break_above_is_a_boundary_not_a_title(self):
        assert [(h.start, h.level) for h in headings("***\n출처\n----\n")] == [(1, 2)]

    def test_an_indented_code_block_above_is_not_the_title(self):
        assert [(h.start, h.level) for h in headings("    x\n출처\n----\n")] == [(1, 2)]

    def test_an_indented_continuation_line_is_part_of_the_title(self):
        # Four spaces after a paragraph has started is only a continuation line.
        assert [(h.start, h.end) for h in headings("abc\n    출처\n----\n")] == [(0, 3)]

    def test_an_empty_list_marker_takes_no_continuation(self):
        """``-`` on its own is a list item that a paragraph cannot lazily continue.

        So ``출처`` below it really is top-level and the ``----`` really does
        underline it — the renderer says so, and this is the one place where a
        container marker above is *not* a reason to refuse. Anything indented under
        the empty marker becomes its content instead, and then it is.
        """
        assert [(h.start, h.level) for h in headings("-\n출처\n----\n")] == [(1, 2)]
        assert headings("-\n  ind\n----\n") == []

    def test_an_html_block_is_not_a_heading(self):
        # It runs to the next blank line and takes the underline in with it.
        assert headings("<div>\nx\n</div>\n출처\n----\n") == []


class TestNothingInsideABlockQuoteIsRead:
    """The one place this module knowingly answers differently from a renderer.

    markdown-it-py reports ``> 출처`` over ``> ----`` as an ``<h2>`` — inside a
    ``<blockquote>``. This module reports no heading at all, and that is the
    intended answer rather than a gap: quoted text is something the document cites,
    and a section that exists only inside a quotation is not a place a bullet can be
    filed. It is not a regression either — the ``## `` scan this replaced did not see
    a quoted ATX heading either.

    Pinned because nothing was pinning it. The behaviour fell out of the ``> `` check
    in `_container_content` and out of `_atx_level` requiring column 0, so a change
    to either would have moved it silently, in a module whose whole argument is that
    it gives the answer a renderer gives.
    """

    def test_a_quoted_atx_heading_is_not_a_heading(self):
        assert headings("> ## 출처 부족\n") == []

    def test_a_quoted_setext_heading_is_not_a_heading(self):
        assert headings("> 출처\n> ----\n") == []

    def test_a_quoted_heading_does_not_hide_a_real_one(self):
        doc = "> ## 인용된 것\n\n## 출처 부족\n"
        assert [(h.start, h.text) for h in headings(doc)] == [(2, "## 출처 부족")]

    def test_a_quoted_bullet_is_not_a_filed_bullet(self):
        assert bullets("> - needs_review: X\n") == []

    def test_a_quoted_fence_neither_opens_nor_is_reported_unclosed(self):
        # Every reader here stops at `> `, and they have to agree about it: a fence
        # that opened for one and not the others would make the rest of the document
        # code to one reader and prose to the next.
        assert ends_inside_fence("> ```\n> x\n") is False
        assert unclosed_fence_line("> ```\n> x\n") is None
        assert headings("> ```\n> x\n\n## 출처 부족\n")[0].text == "## 출처 부족"

    def test_an_indented_heading_inside_a_list_item_is_not_a_heading(self):
        assert headings("- item\n  ## 출처 부족\n") == []

    def test_an_indented_bullet_is_still_a_filed_bullet(self):
        # The one nesting that does count, and deliberately: a sub-bullet under a
        # filed row is part of the same queue entry.
        assert bullets("- outer\n  - inner\n") == ["- outer", "  - inner"]


class TestTheGuardsThatOnlyThisShapeReaches:
    """Documents that reach a guard nothing else in this file reaches.

    Each of these was a mutant that survived the whole suite. They are collected
    here rather than scattered because they share a reason: the obvious document for
    a rule turns out to be decided by an *earlier* rule, so the guard under test is
    never actually run. Every expectation below was checked against markdown-it-py,
    and on all three the renderer agrees with this module.
    """

    def test_a_closing_fence_above_an_underline_is_a_boundary(self):
        """Reaches the fence check inside the walk up from an underline.

        A fenced *underline* never gets that far — it is refused before the walk
        starts — so the check inside `_paragraph_start` was only reachable through a
        fenced line sitting above an unfenced underline. Renderer: an empty code
        block, then a horizontal rule.
        """
        assert headings("```\n```\n----\n") == []

    def test_a_quoted_paragraph_above_a_plain_underline_is_not_a_title(self):
        """Reaches the block-quote branch of `_container_content`.

        `> 출처` over `> ----` does not: its underline is `> ----`, which is refused
        by `_setext_underline` before any container is looked at. The branch is only
        reached when the quote is above and the underline is plain — and then the
        paragraph is a lazy continuation *inside* the quote, so the `----` below it is
        a horizontal rule outside it. Renderer: a block quote reading "q 출처", then a
        rule.
        """
        assert headings("> q\n출처\n----\n") == []

    def test_an_indented_line_between_title_and_underline_is_continuation(self):
        """Reaches the indentation ceiling in `_is_thematic_break`.

        At four spaces `----` is neither a thematic break nor an underline; it is an
        ordinary continuation line of the paragraph above, and the *next* `----`
        underlines the whole thing. Drop the ceiling and the indented line becomes a
        break, ending the paragraph and taking the heading with it. Renderer: one
        level-2 heading spanning all three lines.
        """
        assert headings("출처\n    ----\n----\n") == [
            Heading(start=0, end=3, level=2, text="출처\n    ----\n----")
        ]


class TestHeadingTitle:
    """What a heading says, once, for everyone who has to show it to a human.

    Two consumers were deriving it from `text` and doing it differently — a
    validator warning that printed the raw span (so a one-line operator message
    carried a literal newline through the middle of it) and the renderer-parity
    oracle, which is the worst possible place for a second copy: a test that
    normalises its own side of a comparison can be made to agree with anything.
    """

    def test_both_spellings_of_the_same_heading_have_the_same_title(self):
        # The point of the property: a reader sees one heading, so anything showing
        # a heading to a reader shows one thing.
        assert headings("## 출처 부족\n")[0].title == "출처 부족"
        assert headings("출처 부족\n----\n")[0].title == "출처 부족"

    def test_a_setext_title_carries_no_underline(self):
        # The literal `\n` that reached an operator's warning came from here.
        found = headings("출처\n----\n")[0]
        assert found.text == "출처\n----"
        assert "\n" not in found.title

    def test_a_multi_line_setext_title_keeps_its_own_newline(self):
        # Not the underline, but the paragraph really is two lines and saying so is
        # more honest than picking one.
        assert headings("abc\n출처\n----\n")[0].title == "abc\n출처"

    def test_an_atx_closing_sequence_is_dropped(self):
        assert headings("## foo ##\n")[0].title == "foo"
        assert headings("## ###\n")[0].title == ""

    def test_a_hash_that_is_not_a_closing_sequence_is_kept(self):
        # A renderer keeps it: the run has to be preceded by a space to close.
        assert headings("## foo#\n")[0].title == "foo#"

    def test_trailing_whitespace_is_not_part_of_the_title(self):
        assert headings("## 출처 부족  \n")[0].title == "출처 부족"

    def test_a_paragraph_beginning_with_a_hash_is_still_setext(self):
        """Why the split is on line count and not on the first character.

        `#foo` has no space after the marker, so it is not an ATX heading — it is a
        paragraph, and an underline turns it into a Setext heading whose text starts
        with `#`. Reading the first character would strip that `#` off the title.
        """
        found = headings("#foo\n----\n")[0]
        assert (found.start, found.end, found.level) == (0, 2, 2)
        assert found.title == "#foo"
