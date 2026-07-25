# SPDX-License-Identifier: Apache-2.0
"""The coverage critic's question axis (#537).

The source axis cannot see a lost RELATION. When every row of a relation is
dropped at merge, no candidate row cites it, so there is no orphan citation and
no uncovered source — the summary reads `0 orphan citation(s)` and is, by its own
definition, correct. Meanwhile the questions the KB was built to answer have no
vocabulary left in engine input. A guard named "silent-omission" that reports a
clean line in that state is the omission.

The mapping from question to query is READ, not inferred: `facts/query.dl` is the
committed question→query-draft contract, each draft anchored by a `// q3: ...`
comment carrying the id policy/questions.md declares. The verdict on each draft is
`common.classify_query` — the engine's own gate. Both halves are pinned here, and
the last class of test in this file is the one that matters most: an end-to-end
contract against the bundled `examples/sample-kb` asserting the axis never calls a
question unresolvable that the ENGINE just answered. A previous implementation
matched relation names against the question TEXT and did exactly that for five of
the sample KB's seven questions.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from source_coverage import (
    draft_verdict,
    query_drafts,
    question_rows,
    relation_argument,
)

_REPO = Path(__file__).resolve().parents[2]
_TOOL = _REPO / "tools" / "source_coverage.py"
_SAMPLE_KB = _REPO / "examples" / "sample-kb"
_HEADER = "subject,relation,object,source,status,confidence,note"


# --- reading the mapping out of facts/query.dl --------------------------------


class TestQueryDrafts:
    IDS = {"q1", "q2", "q3"}

    def test_an_anchor_claims_the_query_that_follows_it(self):
        text = '// q1: Who developed it?\nrelation("A", "developed_by", "B")?\n'
        assert query_drafts(text, self.IDS) == {
            "q1": ['relation("A", "developed_by", "B")?']
        }

    def test_prose_between_the_anchor_and_the_query_does_not_break_the_link(self):
        # The committed convention (examples/sample-kb/facts/query.dl, q4-q7) puts
        # explanatory comment lines inside a question's block.
        text = (
            "// q1: Who developed it?\n"
            "// negative example: schema-valid but unsatisfied. Note the shape.\n"
            'relation("A", "developed_by", "B")?\n'
        )
        assert query_drafts(text, self.IDS)["q1"] == ['relation("A", "developed_by", "B")?']

    def test_an_anchor_claims_lines_until_the_next_anchor(self):
        text = (
            "// q1: first\n"
            'relation("A", "r", "B")?\n'
            "\n"
            "// q2: second\n"
            'relation("C", "r", "D")?\n'
        )
        assert query_drafts(text, self.IDS) == {
            "q1": ['relation("A", "r", "B")?'],
            "q2": ['relation("C", "r", "D")?'],
        }

    def test_several_queries_under_one_anchor(self):
        text = '// q1: first\nrelation("A", "r", "B")?\ncount("A", "r")?\n'
        assert query_drafts(text, self.IDS)["q1"] == [
            'relation("A", "r", "B")?',
            'count("A", "r")?',
        ]

    def test_the_bracketed_id_form_is_an_anchor_too(self):
        text = '// [q2] Who developed it?\nrelation("A", "r", "B")?\n'
        assert query_drafts(text, self.IDS) == {"q2": ['relation("A", "r", "B")?']}

    def test_the_files_prose_header_is_not_an_anchor(self):
        # Verbatim from examples/sample-kb/facts/query.dl. A query under a header
        # like this belongs to no question, and must not be credited to one.
        text = (
            "// query drafts derived from policy/questions.md per\n"
            "// skills/factlog/references/text-to-datalog.md\n"
            "// Only facts/accepted.dl entities/relations are used; multi-word\n"
            "// entities are quoted. Each query below maps 1:1 to a question in\n"
            'relation("A", "r", "B")?\n'
        )
        assert query_drafts(text, self.IDS) == {}

    def test_a_query_before_any_anchor_belongs_to_no_question(self):
        text = 'relation("A", "r", "B")?\n// q1: first\ncount("A", "r")?\n'
        assert query_drafts(text, self.IDS) == {"q1": ['count("A", "r")?']}

    def test_an_undeclared_question_anchor_ends_the_previous_block(self):
        # `// q9:` names a question policy/questions.md does not declare. Crediting
        # its query to q1 would let a neighbour's surviving draft mask q1's own.
        text = (
            "// q1: first\n"
            'relation("A", "r", "B")?\n'
            "// q9: not declared here\n"
            'relation("C", "gone", "D")?\n'
        )
        assert query_drafts(text, self.IDS) == {"q1": ['relation("A", "r", "B")?']}

    def test_prose_with_a_colon_does_not_end_a_block(self):
        # The counterpart of the test above: only an anchor-SHAPED id ends a block.
        text = (
            "// q1: first\n"
            "// Note: the constants below are all accepted vocabulary.\n"
            'relation("A", "r", "B")?\n'
        )
        assert query_drafts(text, self.IDS)["q1"] == ['relation("A", "r", "B")?']

    def test_an_empty_file_drafts_nothing(self):
        assert query_drafts("", self.IDS) == {}


class TestRelationArgument:
    def test_read_by_position_from_a_relation_query(self):
        assert relation_argument('relation("A", "develops", "B")?') == "develops"

    def test_read_by_position_from_a_count_query(self):
        assert relation_argument('count("A", "총_문항_수")?') == "총_문항_수"

    def test_a_variable_relation_names_nothing(self):
        assert relation_argument('relation("A", R, "B")?') == ""

    def test_a_query_with_no_relation_position_names_nothing(self):
        assert relation_argument('path("A", X)?') == ""


# --- the verdict, delegated to the engine's own gate --------------------------


def _accepted(*triples):
    return [{"subject": s, "relation": r, "object": o} for s, r, o in triples]


_ONE_FACT = _accepted(("Claude Code", "developed_by", "Anthropic"))


class TestDraftVerdict:
    def test_a_resolved_query_is_resolvable(self):
        assert draft_verdict(
            'relation("Claude Code", "developed_by", "Anthropic")?', _ONE_FACT, ""
        ) == ("resolvable", "")

    def test_a_verified_negative_is_resolvable_not_a_gap(self):
        # Every constant is accepted vocabulary; the triple simply is not a fact, so
        # the engine answers "0 rows". That is an ANSWER (sample-kb's q4), and
        # calling it a coverage gap is the false alarm this axis must not raise.
        state, _reason = draft_verdict(
            'relation("Claude Code", "developed_by", "Claude Code")?',
            _accepted(
                ("Claude Code", "developed_by", "Anthropic"),
                ("factlog", "is_a", "Claude Code"),
            ),
            "",
        )
        assert state == "resolvable"

    def test_a_relation_missing_from_engine_input_is_the_loss(self):
        assert draft_verdict(
            'relation("Claude Code", "develops", "Anthropic")?', _ONE_FACT, ""
        ) == ("lost", "relation 'develops' is not in engine input")

    def test_an_attribute_relation_with_no_rows_is_named(self):
        assert draft_verdict('count("Claude Code", "총_문항_수")?', _ONE_FACT, "") == (
            "lost",
            "relation '총_문항_수' is not in engine input",
        )

    def test_an_entity_missing_from_engine_input_is_a_loss_too(self):
        state, reason = draft_verdict(
            'relation("factlog", "developed_by", "Anthropic")?', _ONE_FACT, ""
        )
        assert state == "lost"
        assert reason.startswith("not in engine input — ")
        assert "factlog" in reason

    def test_review_required_is_routed_not_lost(self):
        assert draft_verdict('review_required("Which steps?")?', _ONE_FACT, "") == (
            "review",
            "routed to human review (review_required)",
        )

    def test_a_path_query_over_accepted_entities_is_resolvable(self):
        assert draft_verdict('path("Claude Code", X)?', _ONE_FACT, "")[0] == "resolvable"

    def test_a_malformed_draft_is_unusable_not_lost(self):
        state, reason = draft_verdict("relation(", _ONE_FACT, "")
        assert state == "unusable"
        assert reason.startswith("query draft is not usable — ")


class TestQuestionRows:
    QUESTIONS = [
        {"id": "q1", "question": "Who developed Claude Code?"},
        {"id": "q2", "question": "Does this KB record that Anthropic develops it?"},
    ]

    def _states(self, drafts, accepted=_ONE_FACT):
        return {
            row["id"]: (row["state"], row["reason"])
            for row in question_rows(self.QUESTIONS, drafts, accepted, "")
        }

    def test_a_question_with_no_draft_is_not_a_lost_relation(self):
        # "the query step has not run" is a different state from "engine input no
        # longer carries what the draft asks for" (#538).
        rows = self._states({})
        assert rows["q1"] == ("no_draft", "no query draft in facts/query.dl — run /factlog query")

    def test_the_draft_note_is_configurable_for_an_absent_file(self):
        rows = question_rows(self.QUESTIONS, {}, _ONE_FACT, "", "facts/query.dl absent")
        assert rows[0]["reason"] == "facts/query.dl absent"

    def test_each_question_is_judged_by_its_own_draft(self):
        rows = self._states({
            "q1": ['relation("Claude Code", "developed_by", "Anthropic")?'],
            "q2": ['relation("Claude Code", "develops", "Anthropic")?'],
        })
        assert rows["q1"][0] == "resolvable"
        assert rows["q2"] == ("lost", "relation 'develops' is not in engine input")

    def test_a_question_with_any_evaluable_draft_is_resolvable(self):
        # The engine answers it, so the axis must not call it unresolvable.
        rows = self._states({
            "q1": [
                'relation("Claude Code", "develops", "Anthropic")?',
                'relation("Claude Code", "developed_by", "Anthropic")?',
            ],
        })
        assert rows["q1"][0] == "resolvable"

    def test_a_loss_outranks_a_review_route_on_the_same_question(self):
        rows = self._states({
            "q1": [
                'review_required("Which steps?")?',
                'relation("Claude Code", "develops", "Anthropic")?',
            ],
        })
        assert rows["q1"] == ("lost", "relation 'develops' is not in engine input")

    def test_empty_engine_input_loses_every_drafted_relation(self):
        rows = self._states(
            {"q1": ['relation("Claude Code", "developed_by", "Anthropic")?']}, accepted=[]
        )
        assert rows["q1"][0] == "lost"


# --- CLI contract -------------------------------------------------------------


def _kb(tmp_path, *, questions=None, query=None, accepted=None, candidates=None,
        aliases=None, source="a.md"):
    """A minimal KB root; every engine input is written explicitly by the test."""
    kb = tmp_path / "kb"
    for name in ("sources", "pages", "facts", "decisions", "policy"):
        (kb / name).mkdir(parents=True)
    if source is not None:
        (kb / "sources" / source).write_text("내용\n", encoding="utf-8")
    if questions is not None:
        (kb / "policy" / "questions.md").write_text(questions, encoding="utf-8")
    if query is not None:
        (kb / "facts" / "query.dl").write_text(query, encoding="utf-8")
    if aliases is not None:
        (kb / "policy" / "relation-aliases.md").write_text(aliases, encoding="utf-8")
    if accepted is not None:
        (kb / "facts" / "accepted.dl").write_text(accepted, encoding="utf-8")
    rows = [_HEADER] + list(candidates or [])
    (kb / "facts" / "candidates.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return kb


def _run(kb, *args):
    return subprocess.run(
        [sys.executable, str(_TOOL), "--wiki", str(kb), *args],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(_REPO)},
    )


# The KB of the issue in miniature: a source fully covered by facts, and a declared
# question whose drafted relation is gone from engine input.
_LOST = dict(
    questions=(
        "# Research questions\n\n"
        "- [q3] factlog 벤치마크의 총 문항 수는?\n"
        "- [q6] C유형 질문의 주제는 무엇인가?\n"
    ),
    query=(
        "// q3: factlog 벤치마크의 총 문항 수는?\n"
        'count("arXiv_2603.20582", "총_문항_수")?\n'
        "\n"
        "// q6: C유형 질문의 주제는 무엇인가?\n"
        'review_required("C유형 질문의 주제는 무엇인가?")?\n'
    ),
    accepted='relation("arXiv_2603.20582", "벤치마크", "기하브라운운동").\n',
    candidates=['arXiv_2603.20582,벤치마크,기하브라운운동,sources/a.md,accepted,0.9,'],
)


class TestQuestionAxisOutput:
    def test_the_lost_relation_is_named_on_its_own_line(self, tmp_path):
        out = _run(_kb(tmp_path, **_LOST))
        assert out.returncode == 0, out.stderr
        assert (
            "questions: 2 declared; 0 with resolvable vocabulary, 1 unresolvable, "
            "1 routed to review" in out.stdout
        )
        assert (
            "  - [q3] factlog 벤치마크의 총 문항 수는?  "
            "(relation '총_문항_수' is not in engine input)" in out.stdout
        )
        assert (
            "  - [q6] C유형 질문의 주제는 무엇인가?  "
            "(routed to human review (review_required))" in out.stdout
        )

    def test_the_source_axis_alone_reports_nothing_wrong(self, tmp_path):
        # The premise of #537: the source line is clean in exactly this state, and
        # its wording is untouched by this axis (tests/test_coverage.sh greps it).
        out = _run(_kb(tmp_path, **_LOST))
        assert (
            "coverage: 1 source(s); 1 covered, 0 text gap(s), 0 binary needing conversion, "
            "0 orphan citation(s)" in out.stdout
        )

    def test_only_the_unresolved_questions_get_a_line(self, tmp_path):
        kb = _kb(
            tmp_path,
            questions=(
                "- [q1] Who is Claude Code developed by?\n"
                "- [q2] factlog 벤치마크의 총 문항 수는?\n"
            ),
            query=(
                "// q1: Who is Claude Code developed by?\n"
                'relation("Claude Code", "developed_by", "Anthropic")?\n'
                "// q2: factlog 벤치마크의 총 문항 수는?\n"
                'count("Claude Code", "총_문항_수")?\n'
            ),
            accepted='relation("Claude Code", "developed_by", "Anthropic").\n',
            candidates=['Claude Code,developed_by,Anthropic,sources/a.md,accepted,0.9,'],
        )
        out = _run(kb)
        assert out.returncode == 0, out.stderr
        assert "questions: 2 declared; 1 with resolvable vocabulary, 1 unresolvable" in out.stdout
        assert "- [q2] factlog 벤치마크의 총 문항 수는?" in out.stdout
        assert "- [q1]" not in out.stdout

    def test_a_question_with_no_draft_is_reported_apart_from_a_loss(self, tmp_path):
        kb = _kb(
            tmp_path,
            questions="- [q1] Who is Claude Code developed by?\n- [q2] 아직 초안이 없다?\n",
            query=(
                "// q1: Who is Claude Code developed by?\n"
                'relation("Claude Code", "develops", "Anthropic")?\n'
            ),
            accepted='relation("Claude Code", "developed_by", "Anthropic").\n',
            candidates=['Claude Code,developed_by,Anthropic,sources/a.md,accepted,0.9,'],
        )
        out = _run(kb)
        assert (
            "questions: 2 declared; 0 with resolvable vocabulary, 1 unresolvable, "
            "1 without a query draft" in out.stdout
        )
        assert "- [q1] Who is Claude Code developed by?  (relation 'develops' is not in engine input)" in out.stdout
        assert "- [q2] 아직 초안이 없다?  (no query draft in facts/query.dl — run /factlog query)" in out.stdout

    def test_reported_on_an_empty_kb_too(self, tmp_path):
        # No source files at all: the axis still reports, after the coverage line.
        kb = _kb(tmp_path, source=None, **{k: v for k, v in _LOST.items() if k != "candidates"})
        out = _run(kb)
        assert out.returncode == 0, out.stderr
        assert "coverage: no source files" in out.stdout
        assert "questions: 2 declared; 0 with resolvable vocabulary, 1 unresolvable" in out.stdout


class TestDegradedInputs:
    """A KB mid-setup still gets its source coverage: nothing here may raise."""

    def test_absent_questions_file(self, tmp_path):
        out = _run(_kb(tmp_path, questions=None, accepted='relation("A", "b", "C").\n'))
        assert out.returncode == 0, out.stderr
        assert "questions: 0 declared (missing policy/questions.md" in out.stdout
        assert "coverage: 1 source(s)" in out.stdout

    def test_questions_file_with_no_questions(self, tmp_path):
        out = _run(_kb(tmp_path, questions="# Research questions\n\n주석뿐인 파일.\n"))
        assert out.returncode == 0, out.stderr
        assert "questions: 0 declared (policy/questions.md has no questions" in out.stdout

    def test_a_malformed_questions_file_states_the_reason(self, tmp_path):
        out = _run(_kb(tmp_path, questions="- [q1] 첫 질문?\n- [q1] 같은 id 두 번?\n"))
        assert out.returncode == 0, out.stderr
        assert "questions: 0 declared (policy/questions.md line 2: duplicate question id" in out.stdout

    def test_absent_query_dl_is_stated_as_such(self, tmp_path):
        kb = _kb(tmp_path, **{k: v for k, v in _LOST.items() if k != "query"})
        out = _run(kb)
        assert out.returncode == 0, out.stderr
        assert (
            "questions: 2 declared; 0 with resolvable vocabulary, 0 unresolvable, "
            "2 without a query draft (facts/query.dl absent — run /factlog query)" in out.stdout
        )

    def test_absent_accepted_dl(self, tmp_path):
        kb = _kb(tmp_path, **{k: v for k, v in _LOST.items() if k != "accepted"})
        out = _run(kb)
        assert out.returncode == 0, out.stderr
        assert (
            "questions: 2 declared; 0 with resolvable vocabulary, 1 unresolvable, "
            "1 routed to review (facts/accepted.dl absent — run /factlog check)" in out.stdout
        )

    def test_absent_accepted_dl_still_reports_the_source_axis(self, tmp_path):
        kb = _kb(tmp_path, **{k: v for k, v in _LOST.items() if k != "accepted"})
        assert "coverage: 1 source(s)" in _run(kb).stdout

    def test_unreadable_relation_vocabulary_does_not_crash_the_report(self, tmp_path):
        # A self-mapping alias makes relation_aliases() fail loud inside the gate.
        # The source axis never read that file; a broken policy file must not take
        # the tool down.
        kb = _kb(tmp_path, aliases="- `uses` -> `uses`\n", **_LOST)
        out = _run(kb)
        assert out.returncode == 0, out.stderr
        assert "questions: 2 declared; vocabulary unreadable" in out.stdout
        assert "coverage: 1 source(s)" in out.stdout


class TestExitCodes:
    def test_default_run_is_informational(self, tmp_path):
        assert _run(_kb(tmp_path, **_LOST)).returncode == 0

    def test_strict_questions_fails_on_a_lost_relation(self, tmp_path):
        out = _run(_kb(tmp_path, **_LOST), "--strict-questions")
        assert out.returncode == 1
        assert "--strict-questions: 1 declared question(s) with no engine-input vocabulary" in out.stderr

    def test_strict_questions_is_clean_when_every_draft_resolves(self, tmp_path):
        kb = _kb(
            tmp_path,
            questions="- [q1] Who is Claude Code developed by?\n",
            query=(
                "// q1: Who is Claude Code developed by?\n"
                'relation("Claude Code", "developed_by", "Anthropic")?\n'
            ),
            accepted='relation("Claude Code", "developed_by", "Anthropic").\n',
            candidates=['Claude Code,developed_by,Anthropic,sources/a.md,accepted,0.9,'],
        )
        assert _run(kb, "--strict-questions").returncode == 0

    def test_a_question_with_no_draft_never_gates(self, tmp_path):
        # The normal state right after `factlog init`: nothing has been lost yet, so
        # the opt-in gate stays silent (it is a LOSS gate, not a completeness gate).
        kb = _kb(tmp_path, **{k: v for k, v in _LOST.items() if k != "query"})
        out = _run(kb, "--strict-questions")
        assert out.returncode == 0, out.stderr
        assert "--strict-questions:" not in out.stderr

    def test_strict_keeps_its_own_contract(self, tmp_path):
        # Every text source is covered and a question lost its relation: --strict is
        # about text sources, so it must stay silent here (#537 adds an axis, it does
        # not widen the flag automation already reads).
        out = _run(_kb(tmp_path, **_LOST), "--strict")
        assert out.returncode == 0, out.stderr
        assert "--strict:" not in out.stderr

    def test_strict_questions_on_an_uncovered_source_does_not_borrow_strict(self, tmp_path):
        # A text gap with no --strict: the question axis owns its own exit code.
        kb = _kb(tmp_path, questions="- [q1] Who is Claude Code developed by?\n",
                 query=('// q1: Who is Claude Code developed by?\n'
                        'relation("Claude Code", "developed_by", "Anthropic")?\n'),
                 accepted='relation("Claude Code", "developed_by", "Anthropic").\n')
        out = _run(kb, "--strict-questions")
        assert "GAP (text, run /factlog sync): sources/a.md" in out.stderr
        assert out.returncode == 0, out.stderr

    def test_both_axes_compose(self, tmp_path):
        kb = _kb(tmp_path, **_LOST)
        (kb / "sources" / "b.md").write_text("아무 사실도 없는 문서\n", encoding="utf-8")
        out = _run(kb, "--strict", "--strict-questions")
        assert out.returncode == 1
        assert "--strict: 1 text source(s) with no extracted facts" in out.stderr
        assert "--strict-questions: 1 declared question(s)" in out.stderr

    @pytest.mark.parametrize("flag", ["--strict", "--strict-questions"])
    def test_absent_questions_file_never_gates(self, tmp_path, flag):
        out = _run(_kb(tmp_path, questions=None,
                       accepted='relation("A", "b", "C").\n',
                       candidates=['A,b,C,sources/a.md,accepted,0.9,']), flag)
        assert out.returncode == 0, out.stderr


# --- the regression pin: agreement with the engine on the bundled sample KB ----


def _engine_answered(report: str) -> list[bool]:
    """Per query, in evaluation order: did the ENGINE answer it?

    The report's "Query evaluation" section holds one line per query in
    facts/query.dl order. `- relation results: N rows` and `- path results: ...`
    are answers (`0 rows` is a *verified negative* — still an answer);
    `unverified` means the gate refused the query for want of accepted
    vocabulary, and `review_required` is routed to a human.
    """
    lines: list[bool] = []
    inside = False
    for raw in report.splitlines():
        line = raw.strip()
        if line.startswith("Query evaluation"):
            inside = True
            continue
        if inside:
            if not line:
                break
            if not line.startswith("- "):
                continue
            lines.append("unverified" not in line and not line.startswith("- review_required"))
    return lines


@pytest.fixture(scope="module")
def measured(tmp_path_factory):
    """(engine report, coverage stdout, question ids in evaluation order).

    A COPY of the bundled sample KB run through the real pipeline. The repo's own
    `examples/sample-kb` is never written to.
    """
    pytest.importorskip("pyrewire", reason="the engine writes the report this pins")
    kb = tmp_path_factory.mktemp("sample") / "kb"
    shutil.copytree(_SAMPLE_KB, kb)
    env = {**os.environ, "PYTHONPATH": str(_REPO), "FACTLOG_ROOT": str(kb)}
    for tool in ("compile_facts.py", "run_logic_check.py"):
        done = subprocess.run(
            [sys.executable, str(_REPO / "tools" / tool)],
            capture_output=True, text=True, env=env,
        )
        assert done.returncode == 0, f"{tool}: {done.stderr}"
    report = (kb / "facts" / "logic_report.txt").read_text(encoding="utf-8")
    coverage = _run(kb)
    assert coverage.returncode == 0, coverage.stderr
    # Question ids in facts/query.dl order — the order the engine evaluates in.
    declared = set(
        re.findall(
            r"^\s*[-*]\s*\[([A-Za-z0-9_-]+)\]",
            (kb / "policy" / "questions.md").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    )
    drafts = query_drafts((kb / "facts" / "query.dl").read_text(encoding="utf-8"), declared)
    ids = [q_id for q_id, lines in drafts.items() for _line in lines]
    return report, coverage.stdout, ids


@pytest.mark.skipif(
    not _SAMPLE_KB.is_dir(), reason="examples/sample-kb is not bundled in this checkout"
)
class TestSampleKbAgreesWithTheEngine:
    """The pin that would have caught the reinvention (#537).

    The bundled `examples/sample-kb` is a healthy KB whose seven questions the
    engine answers five of. Running the real pipeline over a COPY of it and
    comparing the engine's own "Query evaluation" section with this axis is the
    only check that cannot be satisfied by a plausible-looking heuristic.
    """

    def test_the_engine_answers_five_of_the_seven(self, measured):
        # Guards the fixture itself: if the sample KB or the engine changes shape,
        # the comparison below must not silently compare nothing.
        report, _coverage, _ids = measured
        assert sum(_engine_answered(report)) == 5

    def test_no_question_the_engine_answered_is_called_unresolvable(self, measured):
        report, coverage, ids = measured
        answered = {
            q_id for q_id, ok in zip(ids, _engine_answered(report), strict=True) if ok
        }
        flagged = {
            line.split("]")[0].lstrip(" -[")
            for line in coverage.splitlines()
            if line.startswith("  - [")
        }
        assert answered, "the engine answered nothing — the comparison is vacuous"
        assert answered & flagged == set(), (
            f"reported as unresolved: {sorted(answered & flagged)}\n"
            f"--- engine ---\n{report}\n--- coverage ---\n{coverage}"
        )

    def test_the_summary_matches_the_engines_own_verdicts(self, measured):
        _report, coverage, _ids = measured
        assert (
            "questions: 7 declared; 5 with resolvable vocabulary, 1 unresolvable, "
            "1 routed to review" in coverage
        ), coverage

    def test_the_one_lost_relation_is_the_one_the_engine_called_unverified(self, measured):
        report, coverage, _ids = measured
        assert "'develops' is not accepted vocabulary" in report
        assert (
            "  - [q5] Does this KB record that Anthropic develops Claude Code?  "
            "(relation 'develops' is not in engine input)" in coverage
        ), coverage
