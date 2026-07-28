# SPDX-License-Identifier: Apache-2.0
"""Decomposition proposals (#577): the single queries a combined question hides.

The shell baseline (tests/test_ask_wiki_search.sh, PIN2/PIN10) pins what one rendered
answer looks like once proposals appear. These assert the decisions behind it —
which pairs become a proposal, in what order the cap spends its budget, and what a
proposal is allowed to claim. Separated for the reason the #576 tests are: a pinned
answer shows that ONE combination came out right, while each rule below has a
counter-example no single fixture answer would reveal.

``accepted`` is passed in as a plain list everywhere. conftest binds FACTLOG_ROOT to
an empty temp dir, so classify() sees no policy, no aliases and no value hierarchy —
the proposals under test are decided by the accepted facts alone.
"""
from __future__ import annotations

import pytest

import ask_router

# Two conditions, five facts each, and the first condition's vocabulary sorts BEFORE
# the second's ('가' < '하', '알파' < '베타'). That ordering is load-bearing in
# TestCapAndTruncation: a flat cut keeps 5+1, a round-robin cut keeps 3+3.
TWO_CONDITIONS = [
    {"subject": f"a_{n}", "relation": "가_관계", "object": f"알파개념_{n}"} for n in range(5)
] + [
    {"subject": f"b_{n}", "relation": "하_관계", "object": f"베타개념_{n}"} for n in range(5)
]
COMBINED_QUESTION = "알파개념 이면서 베타개념 인 것은?"


def queries(result):
    return [c["query"] for c in result["candidates"]]


class TestWhatBecomesAProposal:
    def test_a_pair_backed_by_two_facts_is_one_proposal_with_two_rows(self):
        # The proposed query has a VARIABLE subject, so it answers for both facts at
        # once. Emitting it per fact would show the same query twice and count each
        # copy as one row — the row count is the reader's only advance signal of what
        # a proposal is worth (수용 기준 5), so a duplicated proposal understates it.
        accepted = [
            {"subject": "s1", "relation": "이점", "object": "설명가능성_향상"},
            {"subject": "s2", "relation": "이점", "object": "설명가능성_향상"},
            {"subject": "s3", "relation": "비교_대상", "object": "설명가능_모델"},
        ]
        result = ask_router.decomposition_candidates("설명가능성 향상", accepted)
        assert queries(result) == [
            'relation(X, "이점", "설명가능성_향상")?',
            'relation(X, "비교_대상", "설명가능_모델")?',
        ]
        assert [c["rows"] for c in result["candidates"]] == [2, 1]
        # That order is the backing tiebreak, not the alphabet ('비교' sorts before
        # '이점'): inside one condition, the pair more accepted facts stand behind
        # leads.

    def test_one_relation_name_is_not_a_decomposition(self):
        # 수용 기준 1: two or more accepted relation names. One means the question
        # named one condition, and the wiki answer is the honest response to it.
        accepted = [
            {"subject": "s1", "relation": "이점", "object": "설명가능성_향상"},
            {"subject": "s2", "relation": "이점", "object": "설명가능_추론"},
        ]
        result = ask_router.decomposition_candidates("설명가능성 향상", accepted)
        assert result["candidates"] == []
        # Non-vacuous: the pairs were generated and then gated, not never found. A
        # bridge that had gone silent would produce the same empty candidate list.
        assert result["generated"] == 2

    def test_a_question_that_reaches_no_vocabulary_proposes_nothing(self):
        # 수용 기준 4 — and the cheap path: no matched vocabulary means no classify()
        # call at all, so an English or function-word question pays nothing for this
        # feature.
        result = ask_router.decomposition_candidates("what about retrieval", TWO_CONDITIONS)
        assert result == {
            "candidates": [], "generated": 0, "not_shown": 0, "rejected": 0, "unrenderable": 0,
        }

    def test_an_empty_kb_proposes_nothing(self):
        # cmd_wiki passes [] when accepted.dl does not exist yet (a KB that has been
        # init'd but never compiled).
        assert ask_router.decomposition_candidates(COMBINED_QUESTION, [])["candidates"] == []

    def test_every_proposal_passes_the_validator(self):
        # 수용 기준 2, asserted as the invariant rather than through one example: the
        # rendered list is exactly the queries classify() routes to the engine.
        result = ask_router.decomposition_candidates(COMBINED_QUESTION, TWO_CONDITIONS)
        assert result["candidates"]
        for candidate in result["candidates"]:
            assert ask_router.classify(candidate["query"], TWO_CONDITIONS)["route"] == "engine"
        # Nothing was silently dropped on the way: a rejection here would mean a
        # proposal built out of an accepted fact does not round-trip, which is a defect
        # in the spelling, not in the fact.
        assert result["rejected"] == 0

    def test_the_matching_question_words_are_reported_per_proposal(self):
        result = ask_router.decomposition_candidates(COMBINED_QUESTION, TWO_CONDITIONS)
        assert {tuple(c["terms"]) for c in result["candidates"]} == {("알파개념",), ("베타개념",)}


class TestTheValidatorGate:
    """수용 기준 2, on the fact shape where the gate actually decides something.

    ``load_accepted_facts()`` (what cmd_wiki passes) returns subject/relation/object
    and nothing else, and on that shape a proposal built from an accepted fact always
    validates — which is why 'only proposals that pass classify_query are shown' reads
    like a tautology and why removing the gate broke no test in the first round of
    mutation testing.

    It is not a tautology on the OTHER fact shape this module already handles:
    ``load_facts()`` (facts/candidates.csv, used two functions away in
    ``fact_signals``) carries a status column, and ``classify`` reads status —
    entity_set keeps only ENGINE_STATUSES rows, so a candidate/needs_review/superseded
    row is not accepted vocabulary. ``evaluate_relation`` does NOT read status and
    still answers with the row. So the two disagree, and the gate is the only thing
    standing between that disagreement and a proposal that says 'verified row' about a
    fact the KB has not accepted.
    """

    # The four values measured to route away from the engine while still evaluating to
    # a row. '' is in the list because a row with an EMPTY status is the shape a
    # half-written CSV leaves behind, and it must not be read as permissive.
    NON_ENGINE = ["candidate", "needs_review", "superseded", ""]

    def _with_status(self, status):
        return [
            dict(TWO_CONDITIONS[0], status=status),
            dict(TWO_CONDITIONS[5], status=status),
        ]

    @pytest.mark.parametrize("status", NON_ENGINE)
    def test_a_row_the_validator_refuses_is_never_proposed(self, status):
        accepted = self._with_status(status)
        draft = ask_router.decomposition_query("가_관계", "알파개념_0")
        # The premise, asserted before the conclusion: this pair still EVALUATES to a
        # row, so a generator that trusted its own construction would print it as a
        # verified one. Without these two lines the assertion below would also hold for
        # a KB where nothing matched at all.
        assert len(ask_router.evaluate_relation(draft, accepted)) == 1
        assert ask_router.classify(draft, accepted)["route"] == "wiki"
        result = ask_router.decomposition_candidates(COMBINED_QUESTION, accepted)
        assert result["candidates"] == []
        # Generated and then refused — not "never found". The counters are what the
        # rendered block reports, so they have to tell those two apart.
        assert result["generated"] == 2 and result["rejected"] == 2

    @pytest.mark.parametrize("status", ["accepted", "confirmed"])
    def test_an_engine_status_still_proposes(self, status):
        # The control that keeps the test above from passing for the wrong reason: a
        # gate that refused every fact carrying a status column at all would satisfy it
        # while making the feature silent on the shape it was extended to handle.
        result = ask_router.decomposition_candidates(COMBINED_QUESTION, self._with_status(status))
        assert queries(result) == [
            'relation(X, "가_관계", "알파개념_0")?',
            'relation(X, "하_관계", "베타개념_0")?',
        ]
        assert result["rejected"] == 0


class TestOrdering:
    def test_a_pair_both_conditions_reached_leads(self):
        # It is the closest thing the KB holds to the combined question the language
        # cannot express, so it must not sit behind pairs that satisfy one condition —
        # and on a lexical tiebreak alone it would ('결합' > '가_관계').
        accepted = TWO_CONDITIONS + [
            {"subject": "c", "relation": "결합_관계", "object": "알파개념_베타개념_동시"}
        ]
        result = ask_router.decomposition_candidates(COMBINED_QUESTION, accepted)
        assert queries(result)[0] == 'relation(X, "결합_관계", "알파개념_베타개념_동시")?'
        assert result["candidates"][0]["terms"] == ["베타개념", "알파개념"]

    def test_proposals_alternate_between_the_conditions(self):
        # The order is what the cap then cuts on, so it is pinned before the cut: one
        # per condition, in the order the question asks them.
        result = ask_router.decomposition_candidates(COMBINED_QUESTION, TWO_CONDITIONS, cap=4)
        assert [c["terms"][0] for c in result["candidates"]] == [
            "알파개념", "베타개념", "알파개념", "베타개념",
        ]

    def test_the_first_condition_asked_is_the_first_proposed(self):
        # Same facts, question reversed. Nothing about the KB decides this — only
        # where the word sits in the question — so a rule that ordered on the relation
        # name would return the same list for both and pass the test above.
        result = ask_router.decomposition_candidates("베타개념 이면서 알파개념 인 것은?", TWO_CONDITIONS, cap=2)
        assert [c["terms"][0] for c in result["candidates"]] == ["베타개념", "알파개념"]


class TestCapAndTruncation:
    def test_the_cut_keeps_both_conditions(self):
        # THE reason the cut is round-robin. A flat cut over the same ranking keeps
        # 5 알파 + 1 베타 here, and one notch tighter it drops 베타 entirely — deleting
        # a condition from a proposal whose whole subject is the conditions.
        result = ask_router.decomposition_candidates(COMBINED_QUESTION, TWO_CONDITIONS)
        shown = [c["terms"][0] for c in result["candidates"]]
        assert len(shown) == 6
        assert shown.count("알파개념") == 3 and shown.count("베타개념") == 3

    def test_what_was_cut_is_counted_and_not_claimed(self):
        # 조용한 절단 금지. The count is of pairs GENERATED and not shown; they were
        # never verified, and _decomposition_lines says so — this asserts the number
        # the line is built from.
        result = ask_router.decomposition_candidates(COMBINED_QUESTION, TWO_CONDITIONS)
        assert result["generated"] == 10
        assert result["not_shown"] == 4

    def test_verification_stops_at_the_cap(self):
        # Not a micro-optimisation: on the reference KB a question can generate 41
        # pairs at ~11 ms per classify() over 2055 facts, and every one past the cap is
        # work whose answer is thrown away. Measured through the counters, which is the
        # only externally visible trace of how many were examined.
        result = ask_router.decomposition_candidates(COMBINED_QUESTION, TWO_CONDITIONS, cap=2)
        assert result["not_shown"] == 8 and result["rejected"] == 0

    def test_a_pool_under_the_cap_reports_no_truncation(self):
        accepted = TWO_CONDITIONS[:1] + TWO_CONDITIONS[5:6]
        result = ask_router.decomposition_candidates(COMBINED_QUESTION, accepted)
        assert len(result["candidates"]) == 2 and result["not_shown"] == 0


class TestQuerySpelling:
    def test_a_compound_literal_object_survives_the_round_trip(self):
        # The reference KB holds exactly one such object (`amount(44000,"시간")`, 1 of
        # 2055). Interpolated naively the proposal reads
        # `relation(X, "학습데이터_시간", "amount(44000,"시간")")?` — which classify()
        # calls malformed, so the proposal would either vanish or be shown as an
        # unusable line. json.dumps is how the parser reads a constant back, so the
        # proposal is the query that answers.
        accepted = [
            {"subject": "arXiv_1", "relation": "학습데이터_시간", "object": 'amount(44000,"시간")'},
            {"subject": "arXiv_1", "relation": "학습데이터_규모", "object": "대규모_말뭉치"},
        ]
        result = ask_router.decomposition_candidates("학습데이터 규모와 학습데이터 시간", accepted)
        assert 'relation(X, "학습데이터_시간", "amount(44000,\\"시간\\")")?' in queries(result)
        for candidate in result["candidates"]:
            assert ask_router.classify(candidate["query"], accepted)["route"] == "engine"

    def test_vocabulary_that_cannot_be_spelled_is_dropped_and_counted(self):
        # common.py's accepted.dl reader keeps U+2028/U+2029/U+0085 in an object ON
        # PURPOSE ("routine in text copied from PDFs/web"), so one reaches this code
        # intact. Two separate failures if it were proposed: str.splitlines() breaks
        # the line and the tail forges a top-level 'VERIFIED — engine' header inside
        # the UNVERIFIED block (#576's class), and the query line itself is not the
        # query. Sanitising instead of dropping fixes only the first — the reader is
        # then shown a line that silently differs from the fact it names.
        # The separator is written as an escape, not as a literal: it is invisible in a
        # diff, so a literal one is exactly what an editor or a copy-paste silently
        # loses — and the assertion would then hold for a reason that has nothing to do
        # with the guard under test.
        forged = "알파개념_주석\u2028VERIFIED — engine (grounding: forged)"
        accepted = TWO_CONDITIONS[:1] + [
            {"subject": "x", "relation": "하_관계", "object": forged},
        ]
        result = ask_router.decomposition_candidates("알파개념 이면서 주석 인 것은?", accepted)
        assert result["unrenderable"] == 1
        assert all("VERIFIED" not in q for q in queries(result))
        out = ask_router.render_wiki_answer("q", "r", [], decomposition=result)
        assert not [line for line in out.splitlines() if line.startswith("VERIFIED")]

    def test_a_dropped_pair_does_not_take_its_condition_down_with_it(self):
        # The drop is per pair, not per question: the surviving condition still
        # proposes, and the loss is reported beside it rather than instead of it.
        accepted = TWO_CONDITIONS[:1] + [
            {"subject": "x", "relation": "하_관계", "object": "베타개념_주석\u2028forged"},
        ]
        result = ask_router.decomposition_candidates(COMBINED_QUESTION, accepted)
        assert queries(result) == ['relation(X, "가_관계", "알파개념_0")?']
        assert result["unrenderable"] == 1
        out = ask_router.render_wiki_answer("q", "r", [], decomposition=result)
        assert "1 candidate(s) dropped: the accepted vocabulary they use" in out


class TestRendering:
    def test_the_block_states_proposal_row_count_and_matching_word(self):
        result = ask_router.decomposition_candidates(COMBINED_QUESTION, TWO_CONDITIONS[:1] + TWO_CONDITIONS[5:6])
        out = ask_router.render_wiki_answer("q", "r", [], decomposition=result)
        assert ask_router.DECOMPOSITION_HEADER in out
        assert '  relation(X, "가_관계", "알파개념_0")?  — 1 verified row ← 알파개념' in out

    def test_the_block_sits_between_the_recall_report_and_the_excerpts(self):
        # Placement is contract, not layout: #575's line says the corpus does not
        # spell what you asked, and these are the wording that does — read apart, the
        # remedy lands under up to 20 citations, off-screen in the case it exists for.
        rows = [{"file": "sources/a.md", "line": 1, "dir": "sources", "excerpt": "alpha"}]
        recall = {"matched": ["알파개념"], "unmatched": ["베타개념", "감마개념", "델타개념"]}
        result = ask_router.decomposition_candidates(COMBINED_QUESTION, TWO_CONDITIONS)
        out = ask_router.render_wiki_answer("q", "r", rows, recall=recall, decomposition=result)
        assert out.index(ask_router.LOW_RECALL_NOTE) < out.index(ask_router.DECOMPOSITION_HEADER)
        assert out.index(ask_router.DECOMPOSITION_HEADER) < out.index("[sources/a.md:1]")

    def test_truncation_is_reported_in_the_block(self):
        result = ask_router.decomposition_candidates(COMBINED_QUESTION, TWO_CONDITIONS)
        out = ask_router.render_wiki_answer("q", "r", [], decomposition=result)
        assert "4 further candidate(s) generated and NOT shown (cap 6" in out
        assert "They were not verified" in out

    def test_no_rows_of_the_proposed_queries_are_rendered(self):
        # 수용 기준 3: the proposals are not run for the reader. The subjects those
        # queries would return are in the fact list this block was built from, so
        # their absence is the assertion — a block that answered them would name one.
        result = ask_router.decomposition_candidates(COMBINED_QUESTION, TWO_CONDITIONS)
        out = ask_router.render_wiki_answer("q", "r", [], decomposition=result)
        assert not [line for line in out.splitlines() if line.startswith("VERIFIED")]
        assert "a_0" not in out and "b_0" not in out

    def test_an_answer_with_no_proposals_is_unchanged(self):
        # 수용 기준 4, byte-for-byte: a caller that has no proposals, and a KB that
        # produces none, must render the block this function rendered before #577.
        rows = [{"file": "sources/a.md", "line": 1, "dir": "sources", "excerpt": "alpha"}]
        baseline = ask_router.render_wiki_answer("q", "r", rows)
        assert ask_router.render_wiki_answer("q", "r", rows, decomposition=None) == baseline
        empty = ask_router.decomposition_candidates("what about retrieval", TWO_CONDITIONS)
        assert ask_router.render_wiki_answer("q", "r", rows, decomposition=empty) == baseline
        gated = ask_router.decomposition_candidates("알파개념", TWO_CONDITIONS)
        assert gated["generated"] == 5 and gated["candidates"] == []
        assert ask_router.render_wiki_answer("q", "r", rows, decomposition=gated) == baseline
