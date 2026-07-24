# SPDX-License-Identifier: Apache-2.0
"""The review-section contract of decisions/open-questions.md (#495).

These are the pure half: what counts as having a section, what gets added to a
file that lacks one, and where a bullet for a category goes. The paths that
*apply* them — `factlog init` and tools/merge_candidates.py — are pinned in
tests/unit/test_open_questions_contract.py.

Two of the cases here are the bug itself rather than a hypothetical. The validator
used to look for the four keywords anywhere in the document, so a bullet that said
"출처" answered the check for the 출처 *section* — a file could lose the heading and
still pass. And the producer wrote a fixed heading per category, so a KB whose
headings were spelled differently grew a second section for a category it already
had, and the bullets went to the new one while the section a human reads stayed at
"현재 없음".
"""
from __future__ import annotations

import pytest

from factlog.md_lines import fence_flags, headings
from factlog.review_sections import (
    OPEN_QUESTIONS_SCAFFOLD,
    REVIEW_CATEGORIES,
    ensure_review_sections,
    heading_for,
    missing_review_sections,
    section_for,
    split_review_sections,
)

KEYWORDS = [keyword for keyword, _ in REVIEW_CATEGORIES]

# How an existing KB spells the four categories — neither of these matches the
# canonical headings, and both must be recognised as the same four sections.
SAMPLE_KB_HEADINGS = (
    "# Open Questions\n\n"
    "## 중복 (Duplicate Review)\n\n"
    "## 모호 (Ambiguity Review)\n\n"
    "## 출처 (Source Review)\n\n"
    "## 충돌 (Conflict Review)\n"
)
REAL_KB_HEADINGS = (
    "# Open Questions\n\n"
    "## 중복 (같은 개념의 다른 이름)\n\n"
    "## 모호 (관계명·개념 판단 필요)\n\n"
    "## 출처 (근거 강도 부족)\n\n"
    "## 충돌 (상충하는 후보)\n"
)


class TestMissingReviewSections:
    def test_the_scaffold_is_missing_nothing(self):
        assert missing_review_sections(OPEN_QUESTIONS_SCAFFOLD) == []

    def test_a_bare_title_is_missing_all_four(self):
        assert missing_review_sections("# Open Questions\n") == KEYWORDS

    def test_existing_kb_spellings_satisfy_every_category(self):
        # No churn: neither of these files may be reported as missing anything.
        assert missing_review_sections(SAMPLE_KB_HEADINGS) == []
        assert missing_review_sections(REAL_KB_HEADINGS) == []

    def test_a_bullet_mentioning_the_word_does_not_count_as_the_section(self):
        # The substring test this replaced passed on exactly this document.
        text = "# Open Questions\n\n- needs_review: 출처 부족한 후보 / 중복 / 모호 / 충돌\n"
        assert missing_review_sections(text) == KEYWORDS

    def test_a_heading_inside_a_code_fence_is_not_a_section(self):
        """Measured: the file below reported nothing missing and had no 출처 section.

        Worse than a false clean bill — section_for then answered with the fenced
        line, and merge_candidates inserted the bullet after the closing fence,
        between two sections and inside neither.
        """
        text = (
            "# Open Questions\n\n## 중복 개념 후보\n\n## 모호한 관계명\n\n"
            "예시:\n\n```\n## 출처 부족\n```\n\n"
            "## 기존 내용과 충돌할 수 있는 항목\n"
        )
        assert missing_review_sections(text) == ["출처"]
        # ensure_review_sections writes the real section; the bullet then goes to it
        # and not to the example, which is what the earlier canonical-string return
        # left the caller to work out for itself.
        out = ensure_review_sections(text)
        placed = section_for(out, "출처")
        assert placed.text == "## 출처 부족"
        assert not fence_flags(out)[0][placed.start]

    def test_a_tilde_fence_hides_a_heading_too(self):
        text = "# Open Questions\n\n~~~\n## 출처 부족\n~~~\n"
        assert "출처" in missing_review_sections(text)

    def test_an_indented_heading_is_not_a_section(self):
        # Indented by four spaces is a code block, not a heading.
        text = "# Open Questions\n\n    ## 출처 부족\n"
        assert "출처" in missing_review_sections(text)

    def test_a_real_heading_after_a_closed_fence_still_counts(self):
        # The fence tracking must not swallow the rest of the document.
        text = "# Open Questions\n\n```\n## 중복 개념 후보\n```\n\n## 출처 부족\n"
        missing = missing_review_sections(text)
        assert "출처" not in missing
        assert "중복" in missing

    def test_every_category_is_reported_on_its_own(self):
        # Dropping a category from REVIEW_CATEGORIES has to be visible here, and a
        # file that lost exactly one heading must name exactly that one.
        assert len(KEYWORDS) == 4
        for keyword in KEYWORDS:
            without = "\n\n".join(
                heading for other, heading in REVIEW_CATEGORIES if other != keyword
            )
            assert missing_review_sections(f"# Open Questions\n\n{without}\n") == [keyword]


class TestEnsureReviewSections:
    def test_a_bare_title_gains_all_four_headings(self):
        out = ensure_review_sections("# Open Questions\n")
        assert missing_review_sections(out) == []
        for _, heading in REVIEW_CATEGORIES:
            assert any(line == heading for line in out.splitlines())

    def test_an_empty_file_gains_a_title_too(self):
        assert ensure_review_sections("") == OPEN_QUESTIONS_SCAFFOLD

    @pytest.mark.parametrize("blank", ["", "\n\n", "   \n\n", "\t\n", "  "])
    def test_a_file_of_only_whitespace_gains_a_title(self, blank):
        # `rstrip("\n") or TITLE` left "   " truthy, so the sections were appended
        # under no title at all.
        assert ensure_review_sections(blank) == OPEN_QUESTIONS_SCAFFOLD

    def test_existing_spellings_are_left_exactly_alone(self):
        # Churn-free on a real KB: no rewrite, and above all no second heading for
        # a category that already had one under another name.
        for text in (SAMPLE_KB_HEADINGS, REAL_KB_HEADINGS, OPEN_QUESTIONS_SCAFFOLD):
            assert ensure_review_sections(text) == text

    def test_only_the_missing_category_is_added(self):
        text = "# Open Questions\n\n## 중복 (Duplicate Review)\n- keep me\n"
        out = ensure_review_sections(text)
        assert out.startswith(text.rstrip("\n"))
        assert "- keep me" in out
        assert out.count("중복") == 1
        assert missing_review_sections(out) == []

    def test_an_unclosed_fence_does_not_get_a_duplicate_heading(self):
        """Regression (#504): this file grew three byte-identical headings per pass.

        An unclosed fence hides everything after it from the heading scan, so the
        categories below it read as missing and were appended again — a second copy
        of the exact same heading line, the queue left under the hidden first one,
        and split_review_sections unable to see that original to warn about it. The
        very split this module exists to prevent, manufactured by it.

        Inside this function the unclosed-fence check is the only thing that stops
        it, so removing that check fails this. It is a **library contract, not the
        pipeline's defence**: merge_candidates' writers call `refuse_unclosed_fence`
        first and never reach here with such a file, so deleting the check leaves
        every end-to-end test green. That is exactly why this case is a unit test.
        """
        doc = (
            "# Open Questions\n\n## 중복 개념 후보\n\n```\n"
            "## 모호한 관계명\n- 기존 큐가 여기 있다\n\n"
            "## 출처 부족\n\n## 기존 내용과 충돌할 수 있는 항목\n"
        )
        out = ensure_review_sections(doc)
        assert out == doc, "a hidden heading was copied instead of left alone"
        assert ensure_review_sections(out) == out
        for _, heading in REVIEW_CATEGORIES:
            assert out.splitlines().count(heading) <= 1, (heading, out)

    def test_nothing_is_written_into_an_unclosed_fence(self):
        """A file that never closes a fence is left alone and reported, not patched.

        Appending is appending to the end, and the end is inside the fence — every
        heading written there is invisible to the scan that asked for it. Measured:
        scaffolding this file produced three headings that still read as missing.
        """
        doc = "# Open Questions\n\n```\n## 모호한 관계명\n"
        out = ensure_review_sections(doc)
        assert out == doc
        assert missing_review_sections(out) == KEYWORDS  # loud, all four
        assert ensure_review_sections(out) == out

    @pytest.mark.parametrize("keyword,canonical", list(REVIEW_CATEGORIES))
    def test_a_category_whose_only_copy_is_fenced_gets_a_real_section(
        self, keyword, canonical
    ):
        """A fenced heading is an example, so the category is genuinely absent.

        Writing the real section is the repair, and it is safe now that every lookup
        skips fenced lines: `section_for` returns the one this adds, not the
        example. An earlier cut refused to write it — the copy in the fence looked
        like "already there" — and left the document permanently a section short with
        no way to earn it back.
        """
        others = "\n\n".join(h for k, h in REVIEW_CATEGORIES if k != keyword)
        doc = f"# Open Questions\n\n{others}\n\n예시:\n\n```\n{canonical}\n```\n"
        out = ensure_review_sections(doc)
        assert missing_review_sections(out) == []
        assert ensure_review_sections(out) == out
        # one real section, plus the example that is not one
        assert [h.text for h in headings(out)].count(canonical) == 1
        lines = out.splitlines()
        fenced_at = lines.index(canonical)  # the example, first in raw line order
        assert section_for(out, keyword).start > fenced_at

    def test_it_is_idempotent(self):
        once = ensure_review_sections("# Open Questions\n")
        assert ensure_review_sections(once) == once
        twice = ensure_review_sections(ensure_review_sections(REAL_KB_HEADINGS))
        assert twice == REAL_KB_HEADINGS

    def test_no_placeholder_bullet_is_scaffolded(self):
        # A "- 현재 없음." here would permanently answer validate.py's "needs_review
        # rows exist but there are no review bullets" check.
        assert not [
            line
            for line in OPEN_QUESTIONS_SCAFFOLD.splitlines()
            if line.lstrip().startswith("- ")
        ]


class TestFencesAndTheContract:
    """A fence hides headings from the scan, and that is the contract's problem too.

    What counts as a fence is tests/unit/test_md_lines.py's; what it costs *this*
    module is here — a category whose only heading is inside one is genuinely
    absent, and a file that never closes one is missing all four.
    """

    @pytest.mark.parametrize("marker", ["```", "~~~"])
    def test_an_unclosed_fence_hides_every_category(self, marker):
        doc = f"# Open Questions\n\n{marker}\n## 모호한 관계명\n"
        assert missing_review_sections(doc) == KEYWORDS

    def test_a_four_space_indented_marker_leaves_the_file_valid(self):
        """A well-formed document was being read as permanently unclosed.

        At four spaces the line is an indented code block — content that opens
        nothing — so the headings below it are real and the file validates.
        """
        doc = (
            "# Open Questions\n\n## 중복 개념 후보\n\n예시:\n\n    ```\n\n"
            "## 모호한 관계명\n\n## 출처 부족\n\n## 기존 내용과 충돌할 수 있는 항목\n"
        )
        assert missing_review_sections(doc) == []

    def test_a_tilde_block_quoting_backticks_leaves_the_file_valid(self):
        # The document the reference tells people to write: the bullet format spelled
        # out inside `~~~` so its backticks need no escaping.
        doc = (
            "# Open Questions\n\n## 중복 개념 후보\n\n## 모호한 관계명\n\n형식 예시:\n\n"
            "~~~\n- needs_review: X / r / Y\n```\n~~~\n\n"
            "## 출처 부족\n\n## 기존 내용과 충돌할 수 있는 항목\n"
        )
        assert missing_review_sections(doc) == []


class TestSplitReviewSections:
    """The state this module exists because of, reported rather than repaired."""

    def test_a_clean_file_reports_nothing(self):
        for text in (OPEN_QUESTIONS_SCAFFOLD, SAMPLE_KB_HEADINGS, REAL_KB_HEADINGS):
            assert split_review_sections(text) == []

    def test_the_real_kb_shape_is_reported(self):
        # What ~/factlog-kb looks like: four hand-written headings up top and the
        # producer's own headings further down, holding the actual queue.
        text = (
            REAL_KB_HEADINGS
            + "\n## 모호한 관계명\n- needs_review: a\n\n## 출처 부족\n- needs_review: b\n"
        )
        assert [
            (keyword, [h.text for h in found])
            for keyword, found in split_review_sections(text)
        ] == [
            ("모호", ["## 모호 (관계명·개념 판단 필요)", "## 모호한 관계명"]),
            ("출처", ["## 출처 (근거 강도 부족)", "## 출처 부족"]),
        ]

    def test_ensure_review_sections_does_not_repair_a_split(self):
        # Joining two sections moves bullets a human filed; that is their edit.
        text = REAL_KB_HEADINGS + "\n## 모호한 관계명\n- needs_review: a\n"
        assert ensure_review_sections(text) == text
        assert split_review_sections(text)


class TestSectionFor:
    def test_an_existing_heading_wins_over_the_canonical_one(self):
        for keyword, canonical in REVIEW_CATEGORIES:
            chosen = section_for(REAL_KB_HEADINGS, keyword)
            assert chosen.text != canonical
            assert chosen.text == REAL_KB_HEADINGS.splitlines()[chosen.start]

    def test_the_first_of_two_headings_wins(self):
        # The split-section damage: the top heading is the one a human reads, so a
        # new bullet joins it rather than the duplicate further down.
        text = REAL_KB_HEADINGS + "\n## 모호한 관계명\n- old bullet\n"
        assert section_for(text, "모호").text == "## 모호 (관계명·개념 판단 필요)"

    def test_the_scaffold_headings_are_what_section_for_returns(self):
        # Scaffolding and bullet placement read the same list: a bullet in a freshly
        # initialised KB lands under a heading that file actually has.
        lines = OPEN_QUESTIONS_SCAFFOLD.splitlines()
        for keyword, _ in REVIEW_CATEGORIES:
            found = section_for(OPEN_QUESTIONS_SCAFFOLD, keyword)
            assert lines[found.start] == found.text

    def test_a_category_with_no_heading_raises_rather_than_guessing(self):
        """The precondition, made loud.

        Both writers call `ensure_review_sections` first, and it leaves a category
        headingless only for a file that ends inside an unclosed fence — which both
        of them have already refused by then. So this cannot be reached down the
        pipeline, and that is exactly why the old silent fallback (append a section
        of my own at the end of the file) was never seen to be wrong. If the
        precondition ever stops holding, a stack trace is the cheapest way to find
        out.
        """
        with pytest.raises(RuntimeError, match="출처"):
            section_for("# Open Questions\n", "출처")

    def test_every_category_is_answered_after_ensure(self):
        # The proof the RuntimeError above is unreachable from the pipeline, as a
        # test rather than as a paragraph: post-ensure, all four resolve.
        for text in ("", "# Open Questions\n", SAMPLE_KB_HEADINGS, REAL_KB_HEADINGS):
            out = ensure_review_sections(text)
            assert missing_review_sections(out) == []
            for keyword, _ in REVIEW_CATEGORIES:
                assert section_for(out, keyword) == heading_for(out, keyword)


class TestSetextSpelledSections:
    """The four categories underlined instead of prefixed (#500).

    A human writing markdown by hand does this, and nothing in the KB's own
    documentation told them not to. Every one of these headings used to be invisible:
    the file read as missing all four, and scaffolding then appended four more.
    """

    SETEXT = (
        "# Open Questions\n\n"
        "중복\n----\n- a\n\n"
        "모호\n----\n- b\n\n"
        "출처\n----\n\n"
        "충돌\n----\n"
    )

    def test_none_of_the_four_is_missing(self):
        assert missing_review_sections(self.SETEXT) == []

    def test_the_file_is_left_byte_for_byte_alone(self):
        assert ensure_review_sections(self.SETEXT) == self.SETEXT
        assert ensure_review_sections(ensure_review_sections(self.SETEXT)) == self.SETEXT

    def test_a_section_is_found_as_a_place_not_a_line(self):
        found = section_for(self.SETEXT, "모호")
        assert (found.start, found.end, found.level) == (6, 8, 2)
        assert found.text == "모호\n----"

    def test_an_equals_underline_is_a_title_and_not_a_section(self):
        # `====` is level 1, and level 1 is the file's title rather than a section —
        # the same rule that keeps `# Open Questions` from being one.
        text = "# Open Questions\n\n출처\n====\n"
        assert "출처" in missing_review_sections(text)

    def test_a_mixed_file_keeps_both_spellings(self):
        text = "# Open Questions\n\n중복\n----\n\n## 모호한 관계명\n\n출처\n----\n\n충돌\n----\n"
        assert missing_review_sections(text) == []
        assert ensure_review_sections(text) == text

    def test_the_damage_the_old_behaviour_did_is_now_reported(self):
        """Two headings per category — and until now, no warning at all.

        This is the file a merge produced from a Setext-spelled KB: the human's
        sections on top holding the queue, the producer's ATX ones appended below.
        The warning #495 added to make a split visible could not see it, because only
        one of the two spellings was a heading. Saying it out loud is the change;
        repairing it is still a human's edit, so `ensure_review_sections` leaves it.
        """
        damaged = (
            self.SETEXT
            + "\n"
            + "\n\n".join(heading for _, heading in REVIEW_CATEGORIES)
            + "\n"
        )
        assert [
            (keyword, [h.text for h in found])
            for keyword, found in split_review_sections(damaged)
        ] == [
            ("중복", ["중복\n----", "## 중복 개념 후보"]),
            ("모호", ["모호\n----", "## 모호한 관계명"]),
            ("출처", ["출처\n----", "## 출처 부족"]),
            ("충돌", ["충돌\n----", "## 기존 내용과 충돌할 수 있는 항목"]),
        ]
        assert ensure_review_sections(damaged) == damaged

    def test_the_first_of_two_spellings_still_wins(self):
        damaged = self.SETEXT + "\n## 모호한 관계명\n- new\n"
        assert section_for(damaged, "모호").text == "모호\n----"


class TestOneRuleForWhatCountsAsASection:
    """`level == 2` lives in one place, and these are what pin it there.

    It used to live in two — `heading_for` and `split_review_sections` — and the
    second copy was untested, so a mutant that widened it passed the whole suite
    while making the validator warn about a file that has exactly one section per
    category. Two copies of the rule for what a section is are what #495 was about,
    so there is now one predicate and both callers ask it.
    """

    def test_a_level_one_heading_carrying_a_keyword_is_not_a_second_section(self):
        text = "# 중복 요약\n\n## 중복 개념 후보\n\n## 모호한 관계명\n\n## 출처 부족\n\n## 기존 내용과 충돌할 수 있는 항목\n"
        assert missing_review_sections(text) == []
        assert split_review_sections(text) == [], "an h1 was counted as a section"

    def test_an_equals_underlined_heading_is_not_a_second_section_either(self):
        # Same rule, other spelling: `====` is level 1, so it is a title and not a
        # 중복 section however the word is spelled into it.
        text = "중복 요약\n====\n\n## 중복 개념 후보\n\n## 모호한 관계명\n\n## 출처 부족\n\n## 기존 내용과 충돌할 수 있는 항목\n"
        assert missing_review_sections(text) == []
        assert split_review_sections(text) == []

    def test_two_real_sections_are_still_reported(self):
        # The predicate must not have been narrowed into never matching.
        text = "## 중복 개념 후보\n\n## 중복 (같은 개념의 다른 이름)\n"
        assert [k for k, _ in split_review_sections(text)] == ["중복"]
