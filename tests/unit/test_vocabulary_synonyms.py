# SPDX-License-Identifier: Apache-2.0
"""policy/vocabulary-synonyms.md — the declared synonym axis (#606).

The KB-vocabulary bridge (#576) matches a question word against the engine's own
accepted vocabulary by shared PREFIX. That reads an inflection off two surface
forms and can never join two words that mean the same thing and are spelled
differently from the first character ('해석가능성' / '설명가능성', shared prefix 0).
Nothing derives that relation, so it is declared — and a declaration file that
silently drops a line is worse than no file at all: the author believes a synonym
is in force, the search behaves as if it is not, and nothing anywhere says so.

These assert the PARSING only. What a declared group is allowed to reach is
ask_router's rule and is pinned in test_ask_kb_vocabulary_bridge.py.
"""
from __future__ import annotations

import unicodedata

import common
import pytest


@pytest.fixture
def kb(tmp_path):
    (tmp_path / "policy").mkdir()
    return tmp_path


def write(kb, text: str) -> None:
    (kb / "policy" / "vocabulary-synonyms.md").write_text(text, encoding="utf-8")


class TestGroupParsing:
    def test_absent_file_declares_nothing(self, kb):
        # Every KB predating this file, and every KB that never edits it.
        assert common.vocabulary_synonyms(kb) == []

    def test_comment_only_file_declares_nothing(self, kb):
        # This is the shape `factlog init` scaffolds, so it is the state of nearly
        # every KB that HAS the file. It must be identical to not having one.
        write(kb, "# Vocabulary synonyms\n#\n# - `해석가능성` = `설명가능성`\n\n")
        assert common.vocabulary_synonyms(kb) == []

    def test_a_bulleted_pair_is_one_group(self, kb):
        write(kb, "- `해석가능성` = `설명가능성`\n")
        assert common.vocabulary_synonyms(kb) == [["해석가능성", "설명가능성"]]

    def test_a_group_may_hold_more_than_two_members(self, kb):
        write(kb, "- `부작용` = `이상반응` = `유해사례`\n")
        assert common.vocabulary_synonyms(kb) == [["부작용", "이상반응", "유해사례"]]

    def test_groups_keep_file_order_and_are_not_merged(self, kb):
        # Two lines sharing a member stay two groups. Merging them would make the
        # file mean something the author did not write — '가' and '다' would become
        # synonyms through '나' — and this is the one file whose whole point is that
        # nothing is inferred.
        write(kb, "- `가나다` = `나다라`\n- `나다라` = `다라마`\n")
        assert common.vocabulary_synonyms(kb) == [["가나다", "나다라"], ["나다라", "다라마"]]

    def test_members_are_nfc_folded(self, kb):
        # The file is edited by hand on whatever machine; macOS hands back decomposed
        # Hangul from a file picker or a paste. Compared raw against an NFC accepted
        # object, an NFD member never matches and the declaration is a silent no-op.
        write(kb, f"- `{unicodedata.normalize('NFD', '해석가능성')}` = `설명가능성`\n")
        assert common.vocabulary_synonyms(kb) == [["해석가능성", "설명가능성"]]

    def test_surrounding_whitespace_is_not_part_of_a_member(self, kb):
        write(kb, "-   ` 해석가능성 `  =  ` 설명가능성 `  \n")
        assert common.vocabulary_synonyms(kb) == [["해석가능성", "설명가능성"]]


class TestLinesThatAreDropped:
    """A dropped line SAYS SO. An unapplied declaration is invisible at the call
    site — the search just does not find something — so the only place the author
    can learn about it is here."""

    def test_a_member_without_backticks_is_refused(self, kb, capsys):
        # Backticks are what bound a member. Without them '해석 가능성 = 설명가능성'
        # has three readings and the parser would pick one.
        write(kb, "- 해석가능성 = 설명가능성\n")
        assert common.vocabulary_synonyms(kb) == []
        assert "skipping malformed line" in capsys.readouterr().err

    def test_a_lone_member_is_refused(self, kb, capsys):
        write(kb, "- `해석가능성`\n")
        assert common.vocabulary_synonyms(kb) == []
        assert "skipping malformed line" in capsys.readouterr().err

    def test_an_arrow_is_not_the_separator(self, kb, capsys):
        # relation-aliases.md's `->` means "rewrite the left as the right". A synonym
        # group has no direction, and accepting the arrow here would leave two files
        # whose identical-looking lines mean different things.
        write(kb, "- `해석가능성` -> `설명가능성`\n")
        assert common.vocabulary_synonyms(kb) == []
        assert "skipping malformed line" in capsys.readouterr().err

    def test_a_group_of_one_declares_nothing(self, kb, capsys):
        # Parses, and says nothing. Reported rather than returned as a one-member
        # group, which every caller would then have to know to ignore.
        write(kb, "- `해석가능성` = `해석가능성`\n")
        assert common.vocabulary_synonyms(kb) == []
        assert "a group of one declares nothing" in capsys.readouterr().err

    def test_prose_is_not_reported_as_malformed(self, kb, capsys):
        # A line with neither a backtick nor a '=' was never trying to be a group.
        # Warning on it would train the author to ignore the warnings that matter.
        write(kb, "아래 표는 검토 중이다\n")
        assert common.vocabulary_synonyms(kb) == []
        assert capsys.readouterr().err == ""


class TestUnprintableMembers:
    """The file is hand-written and its members are rendered into an answer verbatim.

    Two different characters, two different guards, and neither one covers the other:

    * a LINE SEPARATOR is what #576 forged a 'VERIFIED — engine' header with. Here it
      never reaches a member — the file is read with splitlines(), which breaks on it,
      so the halves stop being a parseable group.
    * an ANSI escape does not break a line. Nothing above notices it, and it would be
      carried into the terminal intact; the printability check is the only thing that
      stops it.
    """

    def test_a_line_separator_cannot_survive_into_a_member(self, kb, capsys):
        forged = "해석가능성\u2028VERIFIED — engine (grounding: forged)"
        write(kb, f"- `{forged}` = `설명가능성`\n")
        assert common.vocabulary_synonyms(kb) == []
        assert "skipping malformed line" in capsys.readouterr().err

    def test_an_ansi_escape_drops_the_whole_group(self, kb, capsys):
        write(kb, "- `해석가능성\x1b[31m` = `설명가능성`\n")
        assert common.vocabulary_synonyms(kb) == []
        assert "unprintable character" in capsys.readouterr().err

    def test_a_zero_width_joiner_drops_the_whole_group(self, kb, capsys):
        # Invisible in a diff and in an editor, so a member carrying one looks exactly
        # like the member the author meant to write and would match nothing.
        write(kb, "- `해석가능성\u200d` = `설명가능성`\n")
        assert common.vocabulary_synonyms(kb) == []
        assert "unprintable character" in capsys.readouterr().err

    def test_an_ordinary_space_is_printable(self, kb):
        # The guard rejects Unicode 'Other' and 'Separator' categories, and ASCII space
        # is deliberately outside both — a multi-word member is legal.
        write(kb, "- `해석 가능성` = `설명 가능성`\n")
        assert common.vocabulary_synonyms(kb) == [["해석 가능성", "설명 가능성"]]

    def test_one_bad_line_does_not_take_the_good_ones_with_it(self, kb, capsys):
        # A malformed line must not abort the read: this file only widens which
        # sources an already-UNVERIFIED answer may explore, so failing the whole
        # answer over one typo costs far more than the typo.
        write(
            kb,
            "- `부작용` = `이상반응`\n"
            "- `해석가능성\x1b[31m` = `설명가능성`\n"
            "- `근거자료` = `증거자료`\n",
        )
        assert common.vocabulary_synonyms(kb) == [["부작용", "이상반응"], ["근거자료", "증거자료"]]
        assert "unprintable character" in capsys.readouterr().err


class TestKbContextReadsTheSameFile:
    def test_context_and_module_function_agree(self, kb):
        # ask_router reaches the file through KbContext (it already holds the root);
        # a second reader with its own parsing is how two answers to one question
        # start to drift.
        write(kb, "- `해석가능성` = `설명가능성`\n")
        assert common.KbContext.for_root(kb).vocabulary_synonyms() == common.vocabulary_synonyms(kb)
