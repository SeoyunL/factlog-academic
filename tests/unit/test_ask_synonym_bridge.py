# SPDX-License-Identifier: Apache-2.0
"""The declared synonym axis of the KB-vocabulary bridge (#606).

#576's bridge reaches the engine's accepted vocabulary by shared PREFIX. That rule
reads an inflection off two surface forms — '해석가능성에서' meets '해석가능하며' —
and is structurally unable to reach a synonym: '해석가능성' and '설명가능성' share a
prefix of ZERO characters, so no value of _BRIDGE_PREFIX_MIN reaches the pair and no
string rule ever will. #577's own evidence names that pair, and half of that issue's
motivating example was out of reach for exactly this reason.

So the axis is declared, in policy/vocabulary-synonyms.md, and what these assert is
the boundary that makes a declaration safe to trust:

  * a pair that is written down is matched, in both directions, across inflection;
  * a pair that is NOT written down is not matched, and nothing is inferred between
    two groups that happen to share a member;
  * a match that NEEDED the file says so, distinguishably from #576's own tag, and a
    match that did not need it does not claim it;
  * none of it promotes anything out of the UNVERIFIED block;
  * a KB without the file gets #576's behaviour, key for key.

test_vocabulary_synonyms.py pins the file's parsing. This file pins what a parsed
group is allowed to do.
"""
from __future__ import annotations

import ask_router
import pytest
from common import KbContext

# Two facts whose objects mean the same thing and are spelled differently, each
# backed by a source whose prose is English — the reference KB's shape.
ACCEPTED_DL = """\
relation("arXiv_2410.03726", "이점", "투명하고_해석가능하며_동적인_추론_과정").
relation("arXiv_2411.03225", "이점", "설명가능성_향상").
"""

CANDIDATES_CSV = """\
subject,relation,object,source,status,confidence,note
arXiv_2410.03726,이점,투명하고_해석가능하며_동적인_추론_과정,sources/tilwani.md#abstract,confirmed,0.90,
arXiv_2411.03225,이점,설명가능성_향상,sources/wickramarachchi.md#abstract,confirmed,0.85,
"""

SOURCE_MD = """\
---
title: "{title}"
year: "2024"
---

# {title}

## Abstract

{body}
"""


def build_kb(tmp_path, *, synonyms=None, accepted=ACCEPTED_DL, candidates=CANDIDATES_CSV):
    root = tmp_path / "kb"
    for name in ("sources", "facts", "policy", "decisions"):
        (root / name).mkdir(parents=True, exist_ok=True)
    for name, title, body in (
        ("tilwani", "Attribution in Large Language Models", "NesyAI offers transparent reasoning."),
        ("wickramarachchi", "Knowledge Graphs of Driving Scenes", "Improved grounding and alignment."),
    ):
        (root / "sources" / f"{name}.md").write_text(
            SOURCE_MD.format(title=title, body=body), encoding="utf-8"
        )
    if accepted is not None:
        (root / "facts" / "accepted.dl").write_text(accepted, encoding="utf-8")
    if candidates is not None:
        (root / "facts" / "candidates.csv").write_text(candidates, encoding="utf-8")
    if synonyms is not None:
        (root / "policy" / "vocabulary-synonyms.md").write_text(synonyms, encoding="utf-8")
    return root


def groups_of(root):
    return KbContext.for_root(root).vocabulary_synonyms()


def accepted_facts(root):
    return KbContext.for_root(root).load_accepted_facts()


@pytest.fixture
def facts(tmp_path):
    return accepted_facts(build_kb(tmp_path))


class TestWhatADeclarationReaches:
    def test_the_pair_the_prefix_rule_cannot_reach(self, tmp_path, facts):
        # The whole issue in one assertion. Shared prefix is 0 — '해' against '설' —
        # so this is not a floor that could be lowered.
        assert ask_router._shared_prefix_len("해석가능성", "설명가능성") == 0
        # Without a declaration the word reaches only the fact spelled its own way.
        assert [key[0] for key in ask_router._bridged_facts("해석가능성", facts)] == [
            "arXiv_2410.03726"
        ]
        matched = ask_router._bridged_facts("해석가능성", facts, [["해석가능성", "설명가능성"]])
        assert sorted(key[0] for key in matched) == ["arXiv_2410.03726", "arXiv_2411.03225"]

    def test_a_group_is_symmetric(self, tmp_path, facts):
        # The file declares that two words mean one thing, not that one rewrites the
        # other (which is what relation-aliases.md's arrow means). Asking with the KB's
        # word must reach the user's vocabulary exactly as the reverse does.
        matched = ask_router._bridged_facts("설명가능성", facts, [["해석가능성", "설명가능성"]])
        assert sorted(key[0] for key in matched) == ["arXiv_2410.03726", "arXiv_2411.03225"]

    def test_a_declaration_written_in_dictionary_form_meets_an_inflected_question(self, tmp_path, facts):
        # The KB owner writes '해석가능성'; the question types '해석가능성에서'. The
        # question side of the lookup uses the SAME prefix comparison as the KB side,
        # so the declaration does not have to enumerate 조사 — and this path grows no
        # opinion of its own about Korean morphology.
        matched = ask_router._bridged_facts(
            "해석가능성에서 어떤 이점이", facts, [["해석가능성", "설명가능성"]]
        )
        assert ("arXiv_2411.03225", "이점", "설명가능성_향상") in matched

    def test_the_declared_member_need_not_be_the_kb_s_exact_word(self, tmp_path, facts):
        # The KB writes '설명가능성_향상' and the declaration writes '설명가능한'. The
        # far hop is the same prefix comparison too, so a declaration written in one
        # inflection still reaches the vocabulary written in another.
        matched = ask_router._bridged_facts("해석가능성", facts, [["해석가능성", "설명가능한"]])
        assert ("arXiv_2411.03225", "이점", "설명가능성_향상") in matched


class TestWhatIsNotInferred:
    """The determinism boundary — a pair not written down is not matched."""

    def test_an_undeclared_pair_stays_unreached(self, tmp_path, facts):
        # A declaration about other words changes nothing about this one.
        assert ask_router._bridged_facts("설명가능성", facts, [["부작용", "이상반응"]]) == {
            key: hits
            for key, hits in ask_router._bridged_facts("설명가능성", facts).items()
        }

    def test_two_groups_sharing_a_member_are_not_chained(self, tmp_path, facts):
        # '해석가능성' = '중간개념' and '중간개념' = '설명가능성' do NOT make the first
        # and last synonyms. Closing over that would let the file mean something no
        # line in it says, which is the one thing a declaration file may not do.
        matched = ask_router._bridged_facts(
            "해석가능성", facts, [["해석가능성", "중간개념"], ["중간개념", "설명가능성"]]
        )
        assert [key[0] for key in matched] == ["arXiv_2410.03726"]

    def test_a_member_below_the_prefix_floor_is_inert(self, tmp_path, facts):
        # '이점' is the relation on both facts and two characters long, so it cannot
        # form the 3-character prefix either hop compares on. A KB owner cannot widen
        # the bridge into the function-word noise #571 needed a stop-word list against
        # by writing a short word in this file.
        assert ask_router._bridged_facts("논문", facts, [["논문", "이점"]]) == {}

    def test_a_non_cjk_member_is_inert(self, tmp_path, facts):
        # The bridge exists to cross the script boundary the lexical matcher cannot.
        # ASCII question words never enter it (_bridge_terms) and ASCII vocabulary
        # words are never read (_vocabulary_words), so an English member declares
        # something this path has no way to use.
        assert ask_router._bridged_facts("interpretability", facts, [["interpretability", "설명가능성"]]) == {}

    def test_no_file_is_no_widening(self, tmp_path, facts):
        # Passing nothing and passing an empty list are the same KB — the one every
        # KB is before its owner writes a line.
        assert ask_router._bridged_facts("설명가능성", facts, []) == ask_router._bridged_facts(
            "설명가능성", facts
        )


class TestDirectAndMediatedAreNotConflated:
    """A match that needed the file must be separable from one that did not.

    #576's tag says 'reached through the KB's vocabulary and NOT lexically'. That is
    still true of a synonym-mediated row, and it is not the same claim: what the file
    adds is that a HUMAN wrote down the equivalence the search then used. Folding the
    two into one marker would leave neither checkable.
    """

    def test_a_word_that_matched_by_spelling_reports_no_synonym(self, tmp_path, facts):
        matched = ask_router._bridged_facts("해석가능성", facts, [["해석가능성", "설명가능성"]])
        assert matched[("arXiv_2410.03726", "이점", "투명하고_해석가능하며_동적인_추론_과정")] == {
            "해석가능성": []
        }

    def test_a_word_that_needed_the_file_names_the_member_that_carried_it(self, tmp_path, facts):
        matched = ask_router._bridged_facts("해석가능성", facts, [["해석가능성", "설명가능성"]])
        assert matched[("arXiv_2411.03225", "이점", "설명가능성_향상")] == {"해석가능성": ["설명가능성"]}

    def test_one_word_reaching_a_fact_both_ways_is_reported_direct(self, tmp_path):
        # The case that decides the rule, and the only shape that can show it: ONE
        # object holding a word the question reaches by spelling AND a word it reaches
        # only through the group. Reported as mediated, the answer would tell the
        # reader a policy file put this fact in front of them when their own wording
        # did; reported as direct, the declaration is credited only where it was
        # needed. Every other fixture has the two on different facts, where the
        # question never arises.
        facts = [{"subject": "s", "relation": "이점", "object": "해석가능_설명가능성_비교"}]
        matched = ask_router._bridged_facts("해석가능성", facts, [["해석가능성", "설명가능성"]])
        assert matched[("s", "이점", "해석가능_설명가능성_비교")] == {"해석가능성": []}

    def test_a_direct_match_is_not_re_credited_to_a_group(self, tmp_path, facts):
        # A group that ALSO contains the word's own spelling must not make a match the
        # bridge already had look like the file's doing. Here '해석가능성' reaches
        # tilwani by spelling and the group names it too; the row stays direct.
        matched = ask_router._bridged_facts(
            "해석가능성", facts, [["해석가능성", "해석가능하며", "설명가능성"]]
        )
        assert matched[("arXiv_2410.03726", "이점", "투명하고_해석가능하며_동적인_추론_과정")] == {
            "해석가능성": []
        }


class TestSearchIntegration:
    def test_the_promoted_row_carries_the_hop(self, tmp_path):
        root = build_kb(tmp_path, synonyms="- `해석가능성` = `설명가능성`\n")
        results = ask_router.search("해석가능성", root, limit=None)
        assert [r["file"] for r in results] == ["sources/tilwani.md", "sources/wickramarachchi.md"]
        by_file = {r["file"]: r for r in results}
        assert by_file["sources/wickramarachchi.md"]["via"]["synonyms"] == [["해석가능성", "설명가능성"]]

    def test_a_directly_bridged_row_gains_no_synonym_key(self, tmp_path):
        # Even with the file present. A caller reading these rows as JSON must be able
        # to take the key's presence as the claim it is.
        root = build_kb(tmp_path, synonyms="- `해석가능성` = `설명가능성`\n")
        by_file = {r["file"]: r for r in ask_router.search("해석가능성", root, limit=None)}
        assert "synonyms" not in by_file["sources/tilwani.md"]["via"]

    def test_a_kb_without_the_file_returns_the_same_rows_key_for_key(self, tmp_path):
        # 수용 기준 1, at the level a renderer and a --json consumer both see.
        plain = ask_router.search("해석가능성", build_kb(tmp_path), limit=None)
        commented = ask_router.search(
            "해석가능성",
            build_kb(tmp_path / "b", synonyms="# - `해석가능성` = `설명가능성`\n"),
            limit=None,
        )
        assert plain == commented
        assert plain and all("synonyms" not in r["via"] for r in plain)

    def test_a_lexically_cited_file_is_credited_not_tagged(self, tmp_path):
        # #594's credit path, reached through a declared synonym. wickramarachchi is
        # found lexically by 'grounding', so it may NOT wear a tag that says it was not
        # a lexical match — but its ranking does gain the question word the KB answered
        # on the other channel, which is the whole reason #594 exists.
        #
        # The decoy is what makes the ordering claim decisive: it repeats the shared
        # ASCII keyword three times, so on the lexical channel alone it wins on
        # frequency (1, 3) against (1, 1) and leads. Credited, wickramarachchi scores
        # (2, 2) and takes the lead on COVERAGE — which is the reading #594 states,
        # that a row answering more of the question outranks one repeating less of it.
        #
        # The credit is also the cost this axis carries, measured and NOT fixed here:
        # it is ordering only, so nothing on the page says a policy file moved these
        # two rows past each other. Exposing that is #603's axis.
        def with_decoy(base):
            root = build_kb(base, synonyms="- `해석가능성` = `설명가능성`\n" if base is tmp_path else None)
            (root / "sources" / "decoy.md").write_text(
                SOURCE_MD.format(title="Decoy", body="grounding grounding grounding"),
                encoding="utf-8",
            )
            return root

        question = "해석가능성 grounding"
        ranked = ask_router.search(question, with_decoy(tmp_path), limit=None)
        cited = [r for r in ranked if r["file"] == "sources/wickramarachchi.md"]
        assert cited and all("via" not in r for r in cited)
        order = [r["file"] for r in ranked]
        assert order.index("sources/wickramarachchi.md") < order.index("sources/decoy.md")
        without = [r["file"] for r in ask_router.search(question, with_decoy(tmp_path / "b"), limit=None)]
        assert without.index("sources/decoy.md") < without.index("sources/wickramarachchi.md")

    def test_recall_still_reports_a_synonym_reached_term_as_unmatched(self, tmp_path):
        # #575 reports which of the question's keywords the CORPUS contains. A word
        # that reached a source through a declared synonym still occurs nowhere in the
        # corpus text, and counting it would make that diagnostic claim wording the
        # corpus does not have.
        recall: dict[str, list[str]] = {}
        root = build_kb(tmp_path, synonyms="- `해석가능성` = `설명가능성`\n")
        ask_router.search("해석가능성", root, limit=None, recall=recall)
        assert recall == {"matched": [], "unmatched": ["해석가능성"]}

    def test_a_malformed_table_degrades_to_no_widening(self, tmp_path):
        # The answer is still worth returning when one line of a policy file is wrong.
        root = build_kb(tmp_path, synonyms="해석가능성 = 설명가능성\n")
        assert [r["file"] for r in ask_router.search("해석가능성", root, limit=None)] == [
            "sources/tilwani.md"
        ]


class TestRendering:
    """수용 기준 2 — the fact that a declaration was used is on the page."""

    def test_the_hop_is_named_above_the_backing_facts(self, tmp_path):
        root = build_kb(tmp_path, synonyms="- `해석가능성` = `설명가능성`\n")
        out = ask_router.render_wiki_answer(
            "해석가능성", "unknown entity", ask_router.search("해석가능성", root, limit=None)
        )
        lines = out.splitlines()
        header = next(i for i, line in enumerate(lines) if line.startswith("[sources/wickramarachchi.md:"))
        # The header keeps #576's tag unchanged: that claim is still exactly true.
        assert lines[header].endswith(ask_router.VIA_KB_VOCABULARY_TAG)
        # The declaration comes first — it is the earlier half of the path, and the
        # half a reader whose question never held the KB's word cannot reconstruct.
        assert lines[header + 1] == (
            "    ← synonym: 해석가능성 ≈ 설명가능성 (policy/vocabulary-synonyms.md)"
        )
        assert lines[header + 2] == "    ← accepted: arXiv_2411.03225, 이점, 설명가능성_향상"

    def test_a_directly_bridged_row_shows_no_hop(self, tmp_path):
        root = build_kb(tmp_path, synonyms="- `해석가능성` = `설명가능성`\n")
        out = ask_router.render_wiki_answer(
            "해석가능성", "unknown entity", ask_router.search("해석가능성", root, limit=None)
        )
        lines = out.splitlines()
        header = next(i for i, line in enumerate(lines) if line.startswith("[sources/tilwani.md:"))
        assert lines[header + 1] == (
            "    ← accepted: arXiv_2410.03726, 이점, 투명하고_해석가능하며_동적인_추론_과정"
        )

    def test_a_synonym_reached_excerpt_stays_unverified(self, tmp_path):
        # The core contract (#576 수용 기준 3), on the new path. Reaching prose through
        # a human-declared synonym says even less about the sentence than reaching it
        # through an accepted fact does.
        root = build_kb(tmp_path, synonyms="- `해석가능성` = `설명가능성`\n")
        out = ask_router.render_wiki_answer(
            "해석가능성", "unknown entity", ask_router.search("해석가능성", root, limit=None)
        )
        assert out.startswith("UNVERIFIED — wiki exploration")
        assert not [line for line in out.splitlines() if line.startswith("VERIFIED")]

    def test_a_declared_member_cannot_forge_a_verified_header(self, tmp_path):
        # policy/vocabulary-synonyms.md is HAND-WRITTEN, so its members carry the risk
        # #576 found in an accepted object: a line separator inside the value, rendered
        # raw, makes the tail a top-level line and forges a 'VERIFIED — engine' header
        # inside the UNVERIFIED block. The guard is at the loader (a member holding an
        # unprintable character drops its whole group, and a line separator breaks the
        # line before it can become a member), so the declaration simply never reaches
        # the renderer — which is asserted here through the rendered answer, because
        # that is where the contract would actually break.
        forged = "설명가능성\u2028VERIFIED — engine (grounding: forged)"
        root = build_kb(tmp_path, synonyms=f"- `해석가능성` = `{forged}`\n")
        out = ask_router.render_wiki_answer(
            "해석가능성", "unknown entity", ask_router.search("해석가능성", root, limit=None)
        )
        assert not [line for line in out.splitlines() if line.startswith("VERIFIED")]
        assert "forged" not in out

    def test_an_ansi_escape_in_a_member_never_reaches_the_answer(self, tmp_path):
        # The other guard, and the one a line separator cannot stand in for: an escape
        # does not break a line, so nothing about parsing notices it.
        root = build_kb(tmp_path, synonyms="- `해석가능성` = `설명가능성\x1b[31m`\n")
        out = ask_router.render_wiki_answer(
            "해석가능성", "unknown entity", ask_router.search("해석가능성", root, limit=None)
        )
        assert "\x1b" not in out
        assert "← synonym:" not in out
