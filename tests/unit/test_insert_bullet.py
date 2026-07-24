# SPDX-License-Identifier: Apache-2.0
"""Regression tests for merge_candidates.insert_bullet idempotency (#104)."""
from __future__ import annotations

import inspect

import pytest

import merge_candidates as mc
from factlog.md_lines import section_body_end
from factlog.review_sections import (
    ensure_review_sections,
    missing_review_sections,
    section_for,
    split_review_sections,
)

SECTION = "## 출처 부족"


class TestInsertBullet:
    def test_exact_duplicate_is_skipped(self):
        base = f"# Open Questions\n\n{SECTION}\n- foo\n"
        assert mc.insert_bullet(base, "출처", "- foo") == base

    def test_prefix_substring_bullet_is_still_added(self):
        # #104: "- note" must NOT be considered present just because
        # "- note extra" already is.
        base = f"# Open Questions\n\n{SECTION}\n- note extra\n"
        out = mc.insert_bullet(base, "출처", "- note")
        assert "- note extra" in out
        # the new shorter bullet was actually inserted as its own line
        assert any(line.rstrip() == "- note" for line in out.splitlines())

    def test_the_section_is_scaffolded_before_the_bullet_is_filed(self):
        """A file with no 출처 section gets one from `ensure_review_sections`.

        `insert_bullet` used to append a heading of its own when the section was not
        found. That fallback wrote into a document whose shape nobody had checked —
        into an unclosed fence, measured — so it is gone: the writers scaffold first
        and `section_for` is loud if they did not.
        """
        text = ensure_review_sections("# Open Questions\n")
        out = mc.insert_bullet(text, "출처", "- bar")
        assert SECTION in out and "- bar" in out


class TestInsertBulletIntoASetextSection:
    """A Setext heading is two lines, and the blank-line guards have to know it.

    `heading.start + 1` is the underline, not the end of the heading, so a guard
    written against it sees a non-blank line where the heading itself is and inserts
    a blank line that does not belong to the file.
    """

    ADJACENT = "출처\n----\n충돌\n----\n"

    def test_an_empty_section_gets_the_bullet_flush_under_its_underline(self):
        out = mc.insert_bullet(self.ADJACENT, "출처", "- b")
        assert out.splitlines() == ["출처", "----", "- b", "", "충돌", "----"]

    def test_a_section_with_content_keeps_one_blank_line_before_the_bullet(self):
        text = "출처\n----\n문단\n\n충돌\n----\n"
        out = mc.insert_bullet(text, "출처", "- b")
        assert out.splitlines() == [
            "출처", "----", "문단", "", "- b", "", "충돌", "----",
        ]


class TestTheBulletKeepsABlankLineOnBothSides:
    """Both blank-line guards fire on the same document, and neither may cost the other.

    The bullet is placed at the end of its section's body. When that body ends in a
    content line a blank line is opened *above* the bullet, and when the next line
    starts a heading a blank line is needed *below* it. The documents below are the
    ones where **both** apply, and they are the ones no test covered: the two cases
    above this class end their section with a blank line already, so the leading
    guard never fires in them and the interaction is never exercised.

    It was a real defect and not a hypothetical. The two guards ran as two inserts,
    and the first shifted the index the second compared against, so whenever the
    leading blank line was opened the trailing one was skipped — measured on 139 of
    516 generated ATX documents, a regression against main. On a Setext heading the
    trailing blank line is not cosmetic: without it the heading's title line is
    swallowed by the bullet's list as a lazy continuation and its underline becomes a
    horizontal rule, so the section stops existing, reads as missing, and the next
    merge appends a second one for a category that already had one. That is the
    damage #500 is about, re-created by the fix for it.
    """

    def test_a_heading_immediately_after_the_body(self):
        text = "# Open Questions\n\n## 출처 부족\n- x\n## 모호한 관계명\n"
        assert mc.insert_bullet(text, "출처", "- b").splitlines() == [
            "# Open Questions", "", "## 출처 부족", "- x", "", "- b", "",
            "## 모호한 관계명",
        ]

    def test_a_setext_heading_after_a_closing_fence(self):
        """The shape the review-section reference tells people to write.

        A section that spells out the bullet format in a fence ends its body on the
        closing fence line — a content line, so the leading guard fires — and the
        next section may be underlined rather than prefixed.
        """
        text = "# Open Questions\n\n## 출처 부족\n- x\n```\n예시\n```\n모호\n----\n"
        assert mc.insert_bullet(text, "출처", "- b").splitlines() == [
            "# Open Questions", "", "## 출처 부족", "- x", "```", "예시", "```", "",
            "- b", "", "모호", "----",
        ]

    def test_a_setext_heading_after_a_thematic_break(self):
        text = "# Open Questions\n\n## 출처 부족\n- x\n***\n모호\n----\n"
        assert mc.insert_bullet(text, "출처", "- b").splitlines() == [
            "# Open Questions", "", "## 출처 부족", "- x", "***", "", "- b", "",
            "모호", "----",
        ]

    @pytest.mark.parametrize(
        "text",
        [
            "# Open Questions\n\n중복\n----\n- x\n***\n모호\n----\n출처\n----\n충돌\n----\n",
            "# Open Questions\n\n중복\n----\n- x\n```\n예시\n```\n모호\n----\n출처\n----\n충돌\n----\n",
        ],
    )
    def test_filing_a_bullet_never_costs_the_file_a_section(self, text):
        """The precondition `section_for` relies on, checked after a write.

        `section_for` raises rather than guessing because every writer calls
        `ensure_review_sections` first and so every category has a heading. That
        argument is only sound if writing a bullet cannot *destroy* a heading — and
        while the two guards disagreed about coordinates, it could: the category
        below the bullet went missing, the next `ensure_review_sections` appended a
        second section for it, and the round after that `section_for` had two.
        """
        before = ensure_review_sections(text)
        assert missing_review_sections(before) == []
        # 중복 deliberately: its body ends in a content line, so the leading guard
        # fires and the trailing one has to survive it. Filing into a section whose
        # body is already blank-terminated exercises neither.
        after = mc.insert_bullet(before, "중복", "- b")
        assert missing_review_sections(after) == [], after
        # nothing to scaffold, so nothing is appended and no split is manufactured
        assert ensure_review_sections(after) == after
        assert split_review_sections(after) == []
        # and every category still resolves, which is what makes the raise unreachable
        for keyword in ("중복", "모호", "출처", "충돌"):
            assert section_for(after, keyword) is not None


class TestTheSectionIsResolvedAgainstTheTextBeingEdited:
    """`insert_bullet` takes a category keyword, so a stale position cannot be passed.

    `section_for` answers with a `Heading`, and a `Heading` is a pair of line indices
    — valid only for the exact document they were read from. While `insert_bullet`
    accepted one, the ordinary refactoring of "resolve once, file several bullets in
    a loop" produced stale coordinates, and it did so **silently**: measured on a
    freshly scaffolded file, the 출처 and 충돌 rows landed inside the 모호 section
    while `missing_review_sections` and `split_review_sections` were both empty and
    the run exited 0.

    Under `origin/main` the same hoist was harmless, because `section_for` returned a
    string and `insert_bullet` looked it up again in the current text every time. The
    move to positions is what created the hazard, so the fix belongs with it.
    """

    def test_a_keyword_is_what_it_takes(self):
        # Pinning the signature, because the whole guarantee is that there is no
        # position to hold: a keyword means the same thing in every version of the
        # file, so hoisting one out of a loop is not a mistake anyone can make.
        assert list(inspect.signature(mc.insert_bullet).parameters) == [
            "text", "keyword", "bullet",
        ]

    def test_a_heading_is_rejected_rather_than_quietly_misused(self):
        text = ensure_review_sections("# Open Questions\n")
        with pytest.raises(TypeError):
            mc.insert_bullet(text, section_for(text, "모호"), "- x")

    def test_three_bullets_in_a_loop_each_reach_their_own_section(self):
        """The reproduction, as the loop a caller actually writes.

        Every write moves the lines below it, so the second and third bullets are
        filed into a document that no longer matches any position read before the
        first.
        """
        text = ensure_review_sections("# Open Questions\n")
        for keyword in ("모호", "출처", "충돌"):
            text = mc.insert_bullet(text, keyword, f"- needs_review: {keyword}")
        for keyword in ("모호", "출처", "충돌"):
            section = section_for(text, keyword)
            body = text.splitlines()[section.end:section_body_end(text, section)]
            assert f"- needs_review: {keyword}" in body, (keyword, text)
        assert missing_review_sections(text) == []
        assert split_review_sections(text) == []
