# SPDX-License-Identifier: Apache-2.0
"""The KB-vocabulary bridge (#576): reaching a source the lexical scan cannot.

The shell baseline (tests/test_ask_wiki_search.sh) pins what an ANSWER looks like
once the bridge fires. These assert the decisions behind it — which vocabulary a
question is allowed to reach, which provenance rows may be joined, and what a KB
missing either half degrades to. They are separated because a rendered-answer pin
can only ever show that ONE combination came out right; the rules below each have a
counter-example that no single fixture answer would reveal.
"""
from __future__ import annotations

import unicodedata

import ask_router
from common import KbContext

# A KB shaped like the reference one: Korean facts, English source prose.
ACCEPTED_DL = """\
relation("arXiv_2410.03726", "이점", "투명하고_해석가능하며_동적인_추론_과정").
relation("arXiv_2411.03225", "이점", "설명가능성_향상").
relation("PMID_12947198", "이점", "평형실험에서_가려지는_부분집단_노출").
"""

CANDIDATES_CSV = """\
subject,relation,object,source,status,confidence,note
arXiv_2410.03726,이점,투명하고_해석가능하며_동적인_추론_과정,sources/tilwani.md#abstract,confirmed,0.90,
arXiv_2411.03225,이점,설명가능성_향상,sources/wickramarachchi.md#abstract,accepted,0.85,
PMID_12947198,이점,평형실험에서_가려지는_부분집단_노출,sources/fret.md#abstract,confirmed,0.80,
"""

SOURCE_MD = """\
---
title: "{title}"
year: "2024"
---

# {title}

## Abstract

{body}

## Original source

- DOI: 10.5555/x
"""


def build_kb(tmp_path, *, accepted=ACCEPTED_DL, candidates=CANDIDATES_CSV):
    """A KB root with the three English sources and whichever fact files are asked
    for. `accepted`/`candidates` of None means the file is absent, which is the
    shape `factlog init` leaves behind and the shape the degrade path must survive."""
    root = tmp_path / "kb"
    (root / "facts").mkdir(parents=True)
    (root / "sources").mkdir()
    (root / "policy").mkdir()
    for name, title, body in (
        ("tilwani", "Neurosymbolic AI approach to Attribution", "NesyAI offers transparent, interpretable reasoning."),
        ("wickramarachchi", "Knowledge Graphs of Driving Scenes", "Improved grounding, alignment and explainability."),
        ("fret", "Time-resolved FRET after solution jumps", "Subpopulations hidden at equilibrium are exposed."),
    ):
        (root / "sources" / f"{name}.md").write_text(
            SOURCE_MD.format(title=title, body=body), encoding="utf-8"
        )
    if accepted is not None:
        (root / "facts" / "accepted.dl").write_text(accepted, encoding="utf-8")
    if candidates is not None:
        (root / "facts" / "candidates.csv").write_text(candidates, encoding="utf-8")
    return root


def accepted_facts(root):
    return KbContext.for_root(root).load_accepted_facts()


class TestSharedPrefixFloor:
    """What a question word is allowed to reach in the accepted vocabulary.

    The whole rule is a shared PREFIX of at least _BRIDGE_PREFIX_MIN characters, so
    both halves need a counter-example: a stem that reaches across an inflection the
    lexical matcher cannot, and a fragment that must not reach at all.
    """

    def test_stem_reaches_across_korean_inflection(self, tmp_path):
        # '해석가능성에서' (question, 조사 attached) vs '해석가능하며' (KB, 어미 attached).
        # Neither contains the other, so no substring rule finds this; the 4-character
        # shared stem '해석가능' is the only thing the two forms have in common.
        matched = ask_router._bridged_facts("해석가능성에서 어떤 이점이 있는가", accepted_facts(build_kb(tmp_path)))
        assert [key[0] for key in matched] == ["arXiv_2410.03726"]

    def test_two_character_relation_does_not_drag_in_its_whole_extent(self, tmp_path):
        # '이점' is the relation on all three facts and the question asks '이점을'.
        # Below the floor it matches, and the answer becomes every 이점 fact in the KB
        # regardless of topic — the question's grammar, not its subject. Measured on
        # the reference KB that is 33 facts / 21 subjects instead of 11 / 8.
        matched = ask_router._bridged_facts("어떤 이점을 갖는가", accepted_facts(build_kb(tmp_path)))
        assert matched == {}

    def test_two_character_function_stem_can_never_bridge(self, tmp_path):
        # '논문' is the noise #571 needed a stop-word enumeration against (67 of 186
        # source files on the reference KB). Here it is unreachable by construction:
        # a 2-character token cannot share a 3-character prefix with anything.
        matched = ask_router._bridged_facts("이 논문 저 논문", accepted_facts(build_kb(tmp_path)))
        assert matched == {}

    def test_underscore_normal_form_meets_the_questions_natural_form(self, tmp_path):
        # 수용 기준 4: the KB writes '설명가능성_향상', the user types '설명가능성'.
        matched = ask_router._bridged_facts("설명가능성 향상", accepted_facts(build_kb(tmp_path)))
        assert [key[0] for key in matched] == ["arXiv_2411.03225"]

    def test_ascii_question_words_do_not_bridge(self, tmp_path):
        # The bridge crosses the SCRIPT boundary the lexical matcher cannot. An ASCII
        # term already matches English source text directly, so bridging it would add
        # rows without adding reach.
        kb = build_kb(tmp_path, accepted='relation("s", "이점", "transparent_reasoning").\n')
        assert ask_router._bridged_facts("transparent reasoning", accepted_facts(kb)) == {}

    def test_question_function_words_are_dropped_before_matching(self, tmp_path):
        # The bridge reads _keywords(), so #571's stop-word filter is in force on this
        # path too — there is no second tokenizer for '논문은' to come back through.
        assert ask_router._bridge_terms("이것은 무엇인가") == []

    def test_nfd_authored_vocabulary_still_bridges(self, tmp_path):
        # accepted.dl is written by whatever editor touched it last. Compared raw, an
        # NFD object never equals an NFC question character by character, and the KB
        # would go quiet with no error anywhere.
        nfd = unicodedata.normalize("NFD", '설명가능성_향상')
        kb = build_kb(tmp_path, accepted=f'relation("s", "이점", "{nfd}").\n')
        matched = ask_router._bridged_facts("설명가능성", accepted_facts(kb))
        assert len(matched) == 1


class TestProvenanceJoin:
    """accepted.dl has no source field; the path exists only in candidates.csv."""

    def test_confirmed_and_accepted_rows_are_joined(self, tmp_path):
        bridge = ask_router.kb_vocabulary_bridge("해석가능성 설명가능성", build_kb(tmp_path))
        assert sorted(bridge) == ["sources/tilwani.md", "sources/wickramarachchi.md"]

    def test_superseded_row_is_not_joined(self, tmp_path):
        # Promoting a retired source makes the UNVERIFIED block cite evidence the KB
        # has already discarded — worse than the cross-lingual miss being fixed.
        kb = build_kb(tmp_path, candidates=CANDIDATES_CSV.replace(
            "sources/tilwani.md#abstract,confirmed", "sources/tilwani.md#abstract,superseded"))
        assert ask_router.kb_vocabulary_bridge("해석가능성", kb) == {}

    def test_needs_review_row_is_not_joined(self, tmp_path):
        kb = build_kb(tmp_path, candidates=CANDIDATES_CSV.replace(
            "sources/tilwani.md#abstract,confirmed", "sources/tilwani.md#abstract,needs_review"))
        assert ask_router.kb_vocabulary_bridge("해석가능성", kb) == {}

    def test_named_facts_are_carried_through_for_the_citation(self, tmp_path):
        # The rendered tag says WHICH accepted facts reached this file. They are the
        # only justification for it being in the answer, so a count would leave the
        # reader unable to check the bridge's judgement.
        bridge = ask_router.kb_vocabulary_bridge("해석가능성", build_kb(tmp_path))
        assert bridge["sources/tilwani.md"]["facts"] == [
            "arXiv_2410.03726, 이점, 투명하고_해석가능하며_동적인_추론_과정"
        ]

    def test_section_anchor_resolves_to_the_heading_line(self, tmp_path):
        # candidates.csv records '#abstract' — a section, not a line. Left unresolved,
        # a 3-line window at the file head is seven lines of YAML and zero prose.
        # Asserted through search(), which is where the fragment meets the file.
        assert ask_router.search("해석가능성", build_kb(tmp_path), limit=None)[0]["line"] == 8

    def test_missing_anchor_falls_back_to_the_file_head(self, tmp_path):
        kb = build_kb(tmp_path, candidates=CANDIDATES_CSV.replace("sources/tilwani.md#abstract", "sources/tilwani.md"))
        assert ask_router.kb_vocabulary_bridge("해석가능성", kb)["sources/tilwani.md"]["fragment"] == ""
        assert ask_router.search("해석가능성", kb, limit=None)[0]["line"] == 1

    def test_unresolvable_anchor_falls_back_to_the_file_head(self, tmp_path):
        kb = build_kb(tmp_path, candidates=CANDIDATES_CSV.replace("#abstract", "#no-such-section"))
        assert ask_router.search("해석가능성", kb, limit=None)[0]["line"] == 1


class TestGracefulDegrade:
    """수용 기준 5 — a KB missing either half behaves exactly as it did before."""

    def test_no_accepted_dl(self, tmp_path):
        assert ask_router.kb_vocabulary_bridge("해석가능성", build_kb(tmp_path, accepted=None)) == {}

    def test_no_candidates_csv(self, tmp_path):
        assert ask_router.kb_vocabulary_bridge("해석가능성", build_kb(tmp_path, candidates=None)) == {}

    def test_accepted_fact_that_joins_nothing(self, tmp_path):
        # accepted.dl and candidates.csv out of step — the fact was accepted, the row
        # backing it is gone. No source path exists to promote, and that is not an
        # error the ask path may raise.
        kb = build_kb(tmp_path, candidates="subject,relation,object,source,status,confidence,note\n")
        assert ask_router.kb_vocabulary_bridge("해석가능성", kb) == {}

    def test_unparseable_accepted_dl(self, tmp_path):
        # load_accepted_facts raises FactlogError here. `ask` must not crash on a
        # hand-edited fact file: the lexical answer is still worth returning.
        kb = build_kb(tmp_path, accepted="relation(this is not a fact).\n")
        assert ask_router.kb_vocabulary_bridge("해석가능성", kb) == {}


class TestSearchIntegration:
    """What the bridge does to search() — the surface every caller sees."""

    def test_bridged_source_is_returned_and_tagged(self, tmp_path):
        root = build_kb(tmp_path)
        results = ask_router.search("해석가능성", root, limit=None)
        assert [r["file"] for r in results] == ["sources/tilwani.md"]
        assert results[0]["via"]["facts"] == [
            "arXiv_2410.03726, 이점, 투명하고_해석가능하며_동적인_추론_과정"
        ]

    def test_bridged_excerpt_carries_the_english_prose(self, tmp_path):
        # The point of the anchor: a citation the reader cannot read is not reach.
        results = ask_router.search("해석가능성", build_kb(tmp_path), limit=None)
        assert "NesyAI offers transparent, interpretable reasoning." in results[0]["excerpt"]

    def test_lexically_matched_file_is_not_also_promoted(self, tmp_path):
        # 수용 기준 2 defines the tag as "reached via KB vocabulary and NOT by a
        # lexical match", so a file the scan already cited must not be tagged — and
        # must not appear twice under two banners.
        results = ask_router.search("해석가능성 interpretable", build_kb(tmp_path), limit=None)
        assert [r["file"] for r in results] == ["sources/tilwani.md"]
        assert "via" not in results[0]

    def test_rows_the_bridge_did_not_produce_gain_no_key(self, tmp_path):
        # A KB with no bridge returns byte-identical output; nothing downstream has to
        # learn a new key it will always find empty.
        results = ask_router.search("interpretable", build_kb(tmp_path, accepted=None), limit=None)
        assert results and all("via" not in r for r in results)

    def test_sync_ignored_source_is_not_promoted(self, tmp_path):
        # A source excluded from sync is not evidence for wiki exploration either —
        # the same rule the lexical scan applies, applied to the promoted path.
        root = build_kb(tmp_path)
        (root / "policy" / "sync-ignore.md").write_text("- sources/tilwani.md\n", encoding="utf-8")
        assert ask_router.search("해석가능성", root, limit=None) == []

    def test_source_outside_the_wiki_corpus_is_not_promoted(self, tmp_path):
        # pages/ is engine-derived, so it is excluded from the corpus by design; a
        # provenance row pointing there must not become the path back in.
        root = build_kb(tmp_path, candidates=CANDIDATES_CSV.replace("sources/tilwani.md", "pages/tilwani.md"))
        (root / "pages").mkdir()
        (root / "pages" / "tilwani.md").write_text("# 해석가능\n", encoding="utf-8")
        assert ask_router.search("해석가능성", root, limit=None) == []

    def test_recall_tally_does_not_count_a_bridged_term_as_matched(self, tmp_path):
        # #575 reports which of the question's keywords the CORPUS contains. A bridged
        # term still occurs nowhere in the corpus text; counting it would make that
        # diagnostic claim the corpus holds wording it does not.
        recall: dict[str, list[str]] = {}
        ask_router.search("해석가능성", build_kb(tmp_path), limit=None, recall=recall)
        assert recall == {"matched": [], "unmatched": ["해석가능성"]}


class TestRendering:
    """수용 기준 2 and 3 — how a promoted excerpt is presented."""

    def test_tag_and_backing_facts_precede_the_excerpt(self, tmp_path):
        out = ask_router.render_wiki_answer(
            "해석가능성", "unknown entity", ask_router.search("해석가능성", build_kb(tmp_path), limit=None)
        )
        lines = out.splitlines()
        header = next(i for i, line in enumerate(lines) if line.startswith("[sources/tilwani.md:"))
        assert lines[header].endswith(ask_router.VIA_KB_VOCABULARY_TAG)
        assert lines[header + 1] == (
            "    ← accepted: arXiv_2410.03726, 이점, 투명하고_해석가능하며_동적인_추론_과정"
        )
        # The excerpt follows the attribution, not the other way round: the reader must
        # not first read English prose that no keyword of theirs matched and work out
        # afterwards why it is there.
        assert lines[header + 2].startswith("    ")
        assert "← accepted" not in lines[header + 2]

    def test_promoted_excerpt_stays_in_the_unverified_block(self, tmp_path):
        # The core contract (수용 기준 3). Reaching prose through an accepted fact says
        # the FACT was accepted, never that the sentence was checked.
        out = ask_router.render_wiki_answer(
            "해석가능성", "unknown entity", ask_router.search("해석가능성", build_kb(tmp_path), limit=None)
        )
        assert out.startswith("UNVERIFIED — wiki exploration")
        assert "WARNING: unverified candidates" in out
        # No line STARTS a VERIFIED block — the substring is in 'UNVERIFIED' on line 1,
        # so an unanchored `not in` would pass no matter what this function emitted.
        assert not [line for line in out.splitlines() if line.startswith("VERIFIED")]
        assert ask_router.VIA_KB_VOCABULARY_TAG in out

    def test_rows_without_via_render_as_before(self, tmp_path):
        # render_wiki_answer is called with rows built elsewhere (and by tests) that
        # carry only the four base keys.
        rows = [{"file": "sources/a.md", "line": 1, "dir": "sources", "excerpt": "alpha"}]
        out = ask_router.render_wiki_answer("q", "r", rows)
        assert "[sources/a.md:1] (sources)\n    alpha" in out
        assert ask_router.VIA_KB_VOCABULARY_TAG not in out
