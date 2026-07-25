# SPDX-License-Identifier: Apache-2.0
"""What a dropped row cost the declared questions (#538).

When merge drops a row because its source file is gone, every downstream summary
still reads clean: no candidate row cites the missing source, so there is no orphan
citation and no uncovered source, and a relation with no rows contradicts nothing in
the logic report. The KB has lost the ability to answer a question it declares in
`policy/questions.md`, and the only line about it was a count — `warning: N row(s)
dropped` — which is a quantity, not a consequence.

Two claims are pinned here, and they are different claims:

  * WHICH RELATIONS LEFT ENGINE INPUT. Judged against the merge's own output, because
    at drop time `facts/accepted.dl` still describes the previous run. A relation is
    gone when the projected post-merge engine input has no row for it — not when a
    dropped row merely named it (a dropped row with a surviving twin lost nothing).
  * WHICH QUESTIONS TURNED UNANSWERABLE. Measured as a counterfactual through #537's
    axis: the verdict is taken over the engine input this merge produces and over the
    one it would have produced had nothing dropped, and only a question that flips
    from answerable to not is reported. A question that was already unanswerable is
    `source_coverage`'s to report and is not news this merge caused.

The counterfactual is also the guard against #537's own false alarm. That draft
matched relation names against the question TEXT and called five questions the engine
had just answered unresolvable; a verdict that is wrong in a fixed way reads the same
on both sides of a drop, so it reports nothing here.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import merge_candidates as mc

_REPO = Path(__file__).resolve().parents[2]
_TOOL = _REPO / "tools" / "merge_candidates.py"
_HEADER = "subject,relation,object,source,status,confidence,note"


# --- fixtures -----------------------------------------------------------------


def _row(subject, relation, obj, source, status="confirmed", confidence="0.90", note=""):
    return {
        "subject": subject,
        "relation": relation,
        "object": obj,
        "source": source,
        "status": status,
        "confidence": confidence,
        "note": note,
    }


def _kb(
    tmp_path,
    *,
    run_rows,
    sources=("keep.md",),
    questions=None,
    query=None,
    attributes="- `총_문항_수`\n- `목표_비율`\n",
    candidates=None,
):
    """A KB whose runs/ asserts *run_rows* and whose sources/ holds only *sources*.

    Scaffolded by hand rather than by `factlog init`, so a test states exactly which
    of the two files the report reads (policy/questions.md, facts/query.dl) exist.

    *attributes* declares the literal-valued relations in policy/attribute-relations.md
    the way a real KB does. It is what lets the report still NAME a relation whose
    every row is gone: a vocabulary read off the rows alone goes blind at exactly the
    moment the report matters (source_coverage.relation_vocabulary).
    """
    kb = tmp_path / "kb"
    for name in ("sources", "pages", "facts", "decisions", "policy", "runs"):
        (kb / name).mkdir(parents=True)
    for name in sources:
        (kb / "sources" / name).write_text("# 내용\n", encoding="utf-8")
    (kb / "runs" / "facts-001.json").write_text(json.dumps(run_rows, ensure_ascii=False), encoding="utf-8")
    if questions is not None:
        (kb / "policy" / "questions.md").write_text(questions, encoding="utf-8")
    if query is not None:
        (kb / "facts" / "query.dl").write_text(query, encoding="utf-8")
    if attributes is not None:
        (kb / "policy" / "attribute-relations.md").write_text(attributes, encoding="utf-8")
    if candidates is not None:
        (kb / "facts" / "candidates.csv").write_text(
            "\n".join([_HEADER, *candidates]) + "\n", encoding="utf-8"
        )
    return kb


def _run(kb, *args):
    return subprocess.run(
        [sys.executable, str(_TOOL), "--wiki", str(kb), *args],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(_REPO)},
    )


def _impact(result):
    """The drop-impact block of a merge's stderr, indentation stripped."""
    lines = [line.strip() for line in result.stderr.splitlines()]
    out: list[str] = []
    for line in lines:
        if line.startswith("drop impact:"):
            out.append(line)
        elif out and line.startswith("- ["):
            out.append(line)
    return out


# The KB of the issue in miniature: three rows on one source carry the whole
# vocabulary of q1, a fourth row on a surviving source carries q2's, and the source
# the three cite has been deleted since the run was written.
_LOST_ROWS = [
    _row("factlog 벤치마크", "총_문항_수", "120", "sources/gone.md"),
    _row("factlog 벤치마크", "목표_비율", "0.8", "sources/gone.md"),
    _row("노트", "설명", "벤치마크 사용법", "sources/keep.md"),
]

_QUESTIONS = (
    "# Research Questions\n\n"
    "- [q1] factlog 벤치마크의 총 문항 수는 몇 개인가?\n"
    "- [q2] 노트는 무엇을 설명하는가?\n"
)

_QUERY = (
    "// q1: factlog 벤치마크의 총 문항 수는 몇 개인가?\n"
    'relation("factlog 벤치마크", "총_문항_수", "120")?\n'
    "\n"
    "// q2: 노트는 무엇을 설명하는가?\n"
    'relation("노트", "설명", "벤치마크 사용법")?\n'
)


# --- the drop that costs an answer --------------------------------------------


class TestDropThatRemovesAQuestionsEvidence:
    def test_names_the_relations_and_the_questions(self, tmp_path):
        done = _run(_kb(tmp_path, run_rows=_LOST_ROWS, questions=_QUESTIONS, query=_QUERY))
        block = _impact(done)
        assert any(
            line.startswith("drop impact: 2 relation(s) left with no row in engine input:")
            and "총_문항_수" in line and "목표_비율" in line
            for line in block
        ), block
        assert any(
            "1 declared question(s) in policy/questions.md turned unanswerable: [q1]" in line
            for line in block
        ), block
        assert any(line.startswith("- [q1] factlog 벤치마크") for line in block), block

    def test_the_question_that_kept_its_rows_is_not_reported(self, tmp_path):
        # q2's row cites the surviving source, so its answer is untouched. Reporting
        # every declared question on any drop would be as useless as reporting none.
        done = _run(_kb(tmp_path, run_rows=_LOST_ROWS, questions=_QUESTIONS, query=_QUERY))
        assert not any(line.startswith("- [q2]") for line in _impact(done)), done.stderr

    def test_the_merge_still_succeeds(self, tmp_path):
        # A diagnostic, not a gate: the drop already warns, and turning it into a
        # failure is a separate decision nobody has taken.
        done = _run(_kb(tmp_path, run_rows=_LOST_ROWS, questions=_QUESTIONS, query=_QUERY))
        assert done.returncode == 0, done.stderr

    def test_it_is_printed_next_to_the_drop_warning(self, tmp_path):
        # The reader is looking at `warning: N row(s) dropped` when they need this,
        # so it goes to the same stream, after it.
        done = _run(_kb(tmp_path, run_rows=_LOST_ROWS, questions=_QUESTIONS, query=_QUERY))
        lines = [line.strip() for line in done.stderr.splitlines()]
        warning = next(i for i, line in enumerate(lines) if line.startswith("warning:"))
        impact = next(i for i, line in enumerate(lines) if line.startswith("drop impact:"))
        assert warning < impact, done.stderr


class TestQuestionRoutedToReview:
    """The state the issue's own KB was in: every question evaluates to
    `review_required`, so the engine gate reads the same before and after the drop and
    cannot see the loss through it. The estimate `source_coverage` falls back to on a
    draft-less question is consulted for exactly those, and is still required to flip.
    """

    QUERY = (
        "// q1: factlog 벤치마크의 총 문항 수는 몇 개인가?\n"
        'review_required("factlog 벤치마크의 총 문항 수는 몇 개인가?")?\n'
        "\n"
        "// q2: 노트는 무엇을 설명하는가?\n"
        'relation("노트", "설명", "벤치마크 사용법")?\n'
    )

    def test_a_review_routed_question_is_still_reported_as_an_estimate(self, tmp_path):
        done = _run(_kb(tmp_path, run_rows=_LOST_ROWS, questions=_QUESTIONS, query=self.QUERY))
        block = _impact(done)
        line = next((line for line in block if line.startswith("- [q1]")), None)
        assert line is not None, block
        # Labelled as the weaker claim it is, so the gate's own verdict and an estimate
        # can never be read as the same thing...
        assert "estimated from the question text" in line, line
        # ...and labelled accurately: this question HAS a draft. Reusing
        # source_coverage's "no query draft" prefix here would send the reader to the
        # query step for a draft that is already written.
        assert "routes this question to human review" in line, line
        assert "no query draft" not in line, line


# --- a drop with no consequence -----------------------------------------------


class TestDropWithNoQuestionImpact:
    ROWS = [
        # Both rows carry the relation q1 asks about; only one loses its source, and
        # the triple q1 drafts is on the row that survives.
        _row("factlog 벤치마크", "설명", "옛 판본", "sources/gone.md"),
        _row("노트", "설명", "벤치마크 사용법", "sources/keep.md"),
    ]
    QUESTIONS = "# Research Questions\n\n- [q1] 노트는 무엇을 설명하는가?\n"
    QUERY = (
        "// q1: 노트는 무엇을 설명하는가?\n"
        'relation("노트", "설명", "벤치마크 사용법")?\n'
    )

    def test_says_so_rather_than_going_quiet(self, tmp_path):
        # The complaint in #538 is that the reader could not tell a harmless drop from
        # a costly one. Printing nothing for the harmless case leaves them there.
        done = _run(_kb(tmp_path, run_rows=self.ROWS, questions=self.QUESTIONS, query=self.QUERY))
        assert _impact(done) == [
            "drop impact: no relation left engine input; no declared question "
            "changed answerability"
        ], done.stderr

    def test_a_surviving_relation_is_not_reported_as_lost(self, tmp_path):
        done = _run(_kb(tmp_path, run_rows=self.ROWS, questions=self.QUESTIONS, query=self.QUERY))
        assert "설명" not in " ".join(_impact(done)), done.stderr


class TestRelationLostButNoQuestionAsksAboutIt:
    """The two findings are separate, and only one of them is here.

    A relation whose every row is gone is a real loss of vocabulary, and it is worth
    naming. But if no declared question leans on it, no ANSWER was lost — and that is
    the case where a clean-looking merge is telling the truth.
    """

    ROWS = [
        _row("factlog 벤치마크", "옛_관계", "값", "sources/gone.md"),
        _row("노트", "설명", "벤치마크 사용법", "sources/keep.md"),
    ]
    QUESTIONS = "# Research Questions\n\n- [q1] 노트는 무엇을 설명하는가?\n"
    QUERY = (
        "// q1: 노트는 무엇을 설명하는가?\n"
        'relation("노트", "설명", "벤치마크 사용법")?\n'
    )

    def test_both_halves_are_reported(self, tmp_path):
        done = _run(_kb(tmp_path, run_rows=self.ROWS, questions=self.QUESTIONS, query=self.QUERY))
        assert _impact(done) == [
            "drop impact: 1 relation(s) left with no row in engine input: 옛_관계",
            "drop impact: no declared question changed answerability",
        ], done.stderr


class TestNoRowsDropped:
    def test_no_drop_no_line(self, tmp_path):
        rows = [_row("notes", "설명", "benchmark 사용법", "sources/keep.md")]
        done = _run(_kb(tmp_path, run_rows=rows, questions=_QUESTIONS, query=_QUERY))
        assert _impact(done) == [], done.stderr
        assert "row(s) dropped" not in done.stderr, done.stderr


# --- the two files the report reads may be absent ------------------------------


class TestQuestionsFileAbsent:
    def test_relations_are_still_named_and_the_gap_is_stated(self, tmp_path):
        done = _run(_kb(tmp_path, run_rows=_LOST_ROWS, questions=None, query=_QUERY))
        block = _impact(done)
        # The relation half needs no questions.md, so it still reports.
        assert any("총_문항_수" in line for line in block), block
        # "no question was affected" and "no question could be checked" are different
        # findings, and the reader is here looking for the second one.
        assert any(
            line.startswith("drop impact: no declared questions to check") for line in block
        ), block

    def test_the_merge_still_succeeds(self, tmp_path):
        done = _run(_kb(tmp_path, run_rows=_LOST_ROWS, questions=None, query=_QUERY))
        assert done.returncode == 0, done.stderr


class TestQueryDraftFileAbsent:
    """`facts/query.dl` is written by the LLM `/factlog query` step, so its absence is
    a normal state rather than a loss — and a state the report must not confuse with
    "engine input no longer carries what the draft asks for"."""

    def test_says_the_drafts_are_missing_and_labels_the_estimate(self, tmp_path):
        done = _run(_kb(tmp_path, run_rows=_LOST_ROWS, questions=_QUESTIONS, query=None))
        block = _impact(done)
        assert any("facts/query.dl absent" in line for line in block), block
        line = next((line for line in block if line.startswith("- [q1]")), None)
        assert line is not None, block
        assert "estimated from the question text" in line, line

    def test_the_merge_still_succeeds(self, tmp_path):
        done = _run(_kb(tmp_path, run_rows=_LOST_ROWS, questions=_QUESTIONS, query=None))
        assert done.returncode == 0, done.stderr


# --- the projection: what engine input this merge produces ---------------------


class TestEngineAtoms:
    def test_only_engine_statuses_reach_accepted_dl(self):
        rows = [
            _row("A", "r", "B", "sources/a.md", status="confirmed"),
            _row("C", "r", "D", "sources/a.md", status="accepted"),
            _row("E", "r", "F", "sources/a.md", status="candidate"),
            _row("G", "r", "H", "sources/a.md", status="needs_review"),
            _row("I", "r", "J", "sources/a.md", status="superseded"),
        ]
        assert [atom["subject"] for atom in mc.engine_atoms(rows)] == ["A", "C"]

    def test_the_same_triple_from_two_sources_is_one_atom(self):
        rows = [
            _row("A", "r", "B", "sources/a.md"),
            _row("A", "r", "B", "sources/b.md"),
        ]
        assert len(mc.engine_atoms(rows)) == 1


class TestCounterfactualStatus:
    """A dropped row never reaches main's status-preservation passes, so its own
    status understates what it would have contributed: a fact a human accepted comes
    back from the run as 'candidate' and is re-promoted from candidates.csv."""

    def _key(self, row):
        return mc.fact_key(row["subject"], row["relation"], row["object"], row["source"])

    def test_a_recorded_acceptance_is_restored(self):
        row = _row("A", "r", "B", "sources/a.md", status="candidate")
        assert mc.counterfactual_status(row, set(), {self._key(row): "accepted"}, set()) == "accepted"

    def test_a_tombstone_wins_outright(self):
        row = _row("A", "r", "B", "sources/a.md", status="confirmed")
        key = self._key(row)
        assert mc.counterfactual_status(row, {key}, {key: "accepted"}, {key}) == "superseded"

    def test_a_deliberate_re_review_holds_an_engine_status_back(self):
        row = _row("A", "r", "B", "sources/a.md", status="accepted")
        key = self._key(row)
        assert mc.counterfactual_status(row, set(), {}, {key}) == "needs_review"

    def test_a_re_review_leaves_a_candidate_alone(self):
        # Mirrors main's hold, which only overrides ENGINE statuses.
        row = _row("A", "r", "B", "sources/a.md", status="candidate")
        assert mc.counterfactual_status(row, set(), {}, {self._key(row)}) == "candidate"

    def test_otherwise_the_rows_own_status_stands(self):
        row = _row("A", "r", "B", "sources/a.md", status="confirmed")
        assert mc.counterfactual_status(row, set(), {}, set()) == "confirmed"


class TestLostRelations:
    def test_a_relation_only_the_dropped_rows_carried_is_lost(self):
        kept = [_row("A", "kept_rel", "B", "sources/a.md")]
        dropped = [_row("C", "gone_rel", "D", "sources/gone.md")]
        assert mc.lost_relations(kept, dropped, {}) == ["gone_rel"]

    def test_a_relation_with_a_surviving_row_is_not_lost(self):
        kept = [_row("A", "shared", "B", "sources/a.md")]
        dropped = [_row("C", "shared", "D", "sources/gone.md")]
        assert mc.lost_relations(kept, dropped, {}) == []

    def test_a_declared_alias_of_a_survivor_is_not_lost(self):
        # Canonical comparison, so an alias is not read as a missing relation.
        kept = [_row("A", "developed_by", "B", "sources/a.md")]
        dropped = [_row("C", "made_by", "D", "sources/gone.md")]
        assert mc.lost_relations(kept, dropped, {"made_by": "developed_by"}) == []

    def test_a_dropped_row_that_was_never_engine_input_loses_nothing(self):
        # A candidate/needs_review row contributes no atom to accepted.dl, so its loss
        # cannot take a relation out of engine input.
        kept = [_row("A", "kept_rel", "B", "sources/a.md")]
        dropped = [_row("C", "pending_rel", "D", "sources/gone.md", status="candidate")]
        assert mc.lost_relations(kept, dropped, {}) == []

    def test_the_names_are_sorted(self):
        kept = [_row("A", "kept_rel", "B", "sources/a.md")]
        dropped = [
            _row("C", "zeta", "D", "sources/gone.md"),
            _row("E", "alpha", "F", "sources/gone.md"),
        ]
        assert mc.lost_relations(kept, dropped, {}) == ["alpha", "zeta"]


# --- the counterfactual, without a subprocess ----------------------------------


class TestUnanswerableQuestions:
    QUESTIONS = [{"id": "q1", "question": "누가 A를 만들었는가?"}]
    DRAFTS = {"q1": ['relation("A", "developed_by", "B")?']}

    def test_a_question_whose_evidence_was_dropped_is_reported(self):
        lost = mc.unanswerable_questions(
            self.QUESTIONS,
            self.DRAFTS,
            [_row("X", "other", "Y", "sources/a.md")],
            [_row("A", "developed_by", "B", "sources/gone.md")],
            "",
            {},
        )
        assert [entry[0] for entry in lost] == ["q1"]

    def test_a_question_that_was_already_unanswerable_is_not_reported(self):
        # THE false-positive guard. This question's vocabulary was missing before the
        # drop too, so the loss is not this merge's — it is #537's axis to report, and
        # attributing it here is how the two reports start contradicting each other.
        lost = mc.unanswerable_questions(
            self.QUESTIONS,
            self.DRAFTS,
            [_row("X", "other", "Y", "sources/a.md")],
            [_row("P", "unrelated", "Q", "sources/gone.md")],
            "",
            {},
        )
        assert lost == []

    def test_a_question_still_answerable_after_the_drop_is_not_reported(self):
        lost = mc.unanswerable_questions(
            self.QUESTIONS,
            self.DRAFTS,
            [_row("A", "developed_by", "B", "sources/a.md")],
            [_row("A", "developed_by", "B", "sources/gone.md")],
            "",
            {},
        )
        assert lost == []

    def test_a_dropped_row_that_was_never_engine_input_changes_nothing(self):
        lost = mc.unanswerable_questions(
            self.QUESTIONS,
            self.DRAFTS,
            [_row("X", "other", "Y", "sources/a.md")],
            [_row("A", "developed_by", "B", "sources/gone.md", status="needs_review")],
            "",
            {},
        )
        assert lost == []
