# SPDX-License-Identifier: Apache-2.0
"""The coverage critic's question axis (#537).

The source axis cannot see a lost RELATION. When every row of a relation is
dropped at merge, no candidate row cites it, so there is no orphan citation and
no uncovered source — the summary reads `0 orphan citation(s)` and is, by its own
definition, correct. Meanwhile the six questions the KB was built to answer have
no vocabulary left in engine input. A guard named "silent-omission" that reports
a clean line in that state is the omission.

These tests pin both halves: the per-question verdict (which relation a question
names, and whether engine input still holds a row under it about something the
question names) and the CLI contract around it — the existing `coverage:` line is
untouched, the default run stays informational (exit 0), and the question axis
gates only under its own opt-in `--strict-questions`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from source_coverage import (
    mentioned_relations,
    question_rows,
    relation_probes,
    supported_relations,
)

_REPO = Path(__file__).resolve().parents[2]
_TOOL = _REPO / "tools" / "source_coverage.py"
_HEADER = "subject,relation,object,source,status,confidence,note"


# --- pure helpers -------------------------------------------------------------


class TestRelationProbes:
    """A relation is stored `총_문항_수`; a question spells it `총 문항 수`."""

    def test_separator_folded_form_is_probed(self):
        assert "총 문항 수" in relation_probes("총_문항_수")

    def test_name_as_declared_is_always_probed(self):
        assert "총_문항_수" in relation_probes("총_문항_수")

    def test_hyphen_folds_too(self):
        assert "published year" in relation_probes("published-year")

    def test_ascii_fold_needs_a_word_longer_than_two_chars(self):
        # `is_a` -> "is a" matches nearly every English question; a question would
        # be reported grounded in a relation it never names.
        assert relation_probes("is_a") == ["is_a"]
        assert "developed by" in relation_probes("developed_by")

    def test_a_name_without_separators_probes_only_itself(self):
        assert relation_probes("uses") == ["uses"]


class TestMentionedRelations:
    def test_most_specific_first(self):
        # 총_문항_수 contains 문항_수: the narrower name is the one the author meant
        # and the one worth naming in the report.
        vocab = {"총_문항_수", "문항_수", "벤치마크"}
        # Equal-length names fall back to a sorted (deterministic) order.
        assert mentioned_relations("factlog 벤치마크의 총 문항 수는?", vocab) == [
            "총_문항_수", "문항_수", "벤치마크",
        ]

    def test_a_relation_the_question_never_names_is_not_matched(self):
        assert mentioned_relations("A유형 질문의 목표 비율은?", {"채점_정의"}) == []

    def test_ascii_relation_matched_on_word_boundaries(self):
        assert mentioned_relations("Who is Claude Code developed by?", {"developed_by"}) == [
            "developed_by",
        ]
        # 'api' must not match inside 'therapist' (ask_router's boundary rule).
        assert mentioned_relations("who is the therapist?", {"api"}) == []


def _accepted(*triples):
    return [{"subject": s, "relation": r, "object": o} for s, r, o in triples]


class TestQuestionRows:
    ALIASES: dict[str, str] = {}
    VOCAB = {"developed_by", "총_문항_수"}

    def _rows(self, question, accepted, vocab=None):
        return question_rows(
            [{"id": "q1", "question": question}],
            self.VOCAB if vocab is None else vocab,
            accepted,
            self.ALIASES,
        )[0]

    def test_resolvable_when_evidence_holds_a_row_under_a_named_relation(self):
        row = self._rows(
            "Who is Claude Code developed by?",
            _accepted(("Claude Code", "developed_by", "Anthropic")),
        )
        assert row["resolvable"] is True
        assert row["reason"] == ""

    def test_relation_gone_from_engine_input_is_named(self):
        row = self._rows("factlog 벤치마크의 총 문항 수는?", _accepted())
        assert row["resolvable"] is False
        assert row["reason"] == "관계 '총_문항_수' 가 engine 입력에 없음"

    def test_a_missing_relation_is_named_even_when_another_named_one_survives(self):
        # 벤치마크 survives on an unrelated paper; the LOST relation is the news.
        row = self._rows(
            "factlog 벤치마크의 총 문항 수는?",
            _accepted(("arXiv_2603.20582", "벤치마크", "기하브라운운동")),
            vocab={"총_문항_수", "벤치마크"},
        )
        assert row["resolvable"] is False
        assert row["reason"] == "관계 '총_문항_수' 가 engine 입력에 없음"

    def test_rows_under_a_named_relation_about_nothing_the_question_names(self):
        # The relation has engine-input rows, but none about an entity the question
        # names — naming a relation is not evidence on its own.
        row = self._rows(
            "Who is factlog developed by?",
            _accepted(("Claude Code", "developed_by", "Anthropic")),
        )
        assert row["resolvable"] is False
        assert row["reason"] == (
            "관계 'developed_by' 의 engine 입력 행이 질문이 언급한 개체와 맞물리지 않음"
        )

    def test_a_question_naming_no_relation_is_unresolvable(self):
        row = self._rows("C유형 질문의 주제는 무엇인가?", _accepted())
        assert row["resolvable"] is False
        assert row["reason"] == "질문에서 KB 관계 어휘를 찾지 못함"

    def test_alias_is_not_mistaken_for_a_missing_relation(self):
        # The question names the alias; engine input carries the canonical.
        rows = question_rows(
            [{"id": "q1", "question": "Claude Code 게재연도는?"}],
            {"게재연도"},
            _accepted(("Claude Code", "published_year", "2024")),
            {"게재연도": "published_year"},
        )
        assert rows[0]["resolvable"] is True


class TestSupportedRelations:
    def test_counts_a_relation_with_rows_canonically(self):
        supported = supported_relations(
            _accepted(("A", "게재연도", "2024")), {"게재연도": "published_year"}
        )
        assert supported == {"published_year"}

    def test_empty_engine_input_supports_nothing(self):
        assert supported_relations([], {}) == set()


# --- CLI contract -------------------------------------------------------------


def _kb(tmp_path, *, questions=None, accepted=None, candidates=None, attributes=None,
        aliases=None, source="a.md"):
    """A minimal KB root; every engine input is written explicitly by the test."""
    kb = tmp_path / "kb"
    for name in ("sources", "pages", "facts", "decisions", "policy"):
        (kb / name).mkdir(parents=True)
    if source is not None:
        (kb / "sources" / source).write_text("내용\n", encoding="utf-8")
    if questions is not None:
        (kb / "policy" / "questions.md").write_text(questions, encoding="utf-8")
    if attributes is not None:
        (kb / "policy" / "attribute-relations.md").write_text(attributes, encoding="utf-8")
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


# The KB of the issue in miniature: a source fully covered by facts, and declared
# questions whose relations are gone from engine input.
_LOST = dict(
    questions=(
        "# Research questions\n\n"
        "- [q3] factlog 벤치마크의 총 문항 수는?\n"
        "- [q6] C유형 질문의 주제는 무엇인가?\n"
    ),
    attributes="총_문항_수\n",
    accepted='relation("arXiv_2603.20582", "벤치마크", "기하브라운운동").\n',
    candidates=['arXiv_2603.20582,벤치마크,기하브라운운동,sources/a.md,accepted,0.9,'],
)


class TestQuestionAxisOutput:
    def test_every_question_unresolvable(self, tmp_path):
        out = _run(_kb(tmp_path, **_LOST))
        assert out.returncode == 0, out.stderr
        assert "questions: 2 declared; 0 with resolvable vocabulary, 2 unresolvable" in out.stdout
        assert (
            "  - [q3] factlog 벤치마크의 총 문항 수는?  (관계 '총_문항_수' 가 engine 입력에 없음)"
            in out.stdout
        )
        assert (
            "  - [q6] C유형 질문의 주제는 무엇인가?  (질문에서 KB 관계 어휘를 찾지 못함)"
            in out.stdout
        )

    def test_the_source_axis_alone_reports_nothing_wrong(self, tmp_path):
        # The premise of #537: the source line is clean in exactly this state.
        out = _run(_kb(tmp_path, **_LOST))
        assert (
            "coverage: 1 source(s); 1 covered, 0 text gap(s), 0 binary needing conversion, "
            "0 orphan citation(s)" in out.stdout
        )

    def test_only_the_unresolvable_questions_get_a_line(self, tmp_path):
        kb = _kb(
            tmp_path,
            questions=(
                "- [q1] Who is Claude Code developed by?\n"
                "- [q2] factlog 벤치마크의 총 문항 수는?\n"
            ),
            attributes="총_문항_수\n",
            accepted='relation("Claude Code", "developed_by", "Anthropic").\n',
            candidates=['Claude Code,developed_by,Anthropic,sources/a.md,accepted,0.9,'],
        )
        out = _run(kb)
        assert out.returncode == 0, out.stderr
        assert "questions: 2 declared; 1 with resolvable vocabulary, 1 unresolvable" in out.stdout
        assert "- [q2] factlog 벤치마크의 총 문항 수는?" in out.stdout
        assert "- [q1]" not in out.stdout

    def test_reported_on_an_empty_kb_too(self, tmp_path):
        # No source files at all: the axis still reports, after the coverage line.
        kb = _kb(tmp_path, source=None, **{k: v for k, v in _LOST.items() if k != "candidates"})
        out = _run(kb)
        assert out.returncode == 0, out.stderr
        assert "coverage: no source files" in out.stdout
        assert "questions: 2 declared; 0 with resolvable vocabulary, 2 unresolvable" in out.stdout


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

    def test_absent_accepted_dl(self, tmp_path):
        kb = _kb(tmp_path, **{k: v for k, v in _LOST.items() if k != "accepted"})
        out = _run(kb)
        assert out.returncode == 0, out.stderr
        assert (
            "questions: 2 declared; 0 with resolvable vocabulary, 2 unresolvable "
            "(facts/accepted.dl absent — run /factlog check)" in out.stdout
        )

    def test_absent_accepted_dl_still_reports_the_source_axis(self, tmp_path):
        kb = _kb(tmp_path, **{k: v for k, v in _LOST.items() if k != "accepted"})
        assert "coverage: 1 source(s)" in _run(kb).stdout

    def test_unreadable_relation_vocabulary_does_not_crash_the_report(self, tmp_path):
        # A self-mapping alias makes relation_aliases() fail loud. The source axis
        # never read that file; a broken policy file must not take the tool down.
        kb = _kb(tmp_path, aliases="- `uses` -> `uses`\n", **_LOST)
        out = _run(kb)
        assert out.returncode == 0, out.stderr
        assert "questions: 2 declared; vocabulary unreadable" in out.stdout
        assert "coverage: 1 source(s)" in out.stdout


class TestExitCodes:
    def test_default_run_is_informational(self, tmp_path):
        assert _run(_kb(tmp_path, **_LOST)).returncode == 0

    def test_strict_questions_fails_on_an_unresolvable_question(self, tmp_path):
        out = _run(_kb(tmp_path, **_LOST), "--strict-questions")
        assert out.returncode == 1
        assert "--strict-questions: 2 declared question(s) with no engine-input vocabulary" in out.stderr

    def test_strict_questions_is_clean_when_every_question_resolves(self, tmp_path):
        kb = _kb(
            tmp_path,
            questions="- [q1] Who is Claude Code developed by?\n",
            accepted='relation("Claude Code", "developed_by", "Anthropic").\n',
            candidates=['Claude Code,developed_by,Anthropic,sources/a.md,accepted,0.9,'],
        )
        assert _run(kb, "--strict-questions").returncode == 0

    def test_strict_keeps_its_own_contract(self, tmp_path):
        # Every text source is covered and the questions are unresolvable: --strict
        # is about text sources, so it must stay silent here (#537 adds an axis, it
        # does not widen the flag automation already reads).
        out = _run(_kb(tmp_path, **_LOST), "--strict")
        assert out.returncode == 0, out.stderr
        assert "--strict:" not in out.stderr

    def test_strict_questions_on_an_uncovered_source_does_not_borrow_strict(self, tmp_path):
        # A text gap with no --strict: the question axis owns its own exit code.
        kb = _kb(tmp_path, questions="- [q1] Who is Claude Code developed by?\n",
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
        assert "--strict-questions: 2 declared question(s)" in out.stderr

    @pytest.mark.parametrize("flag", ["--strict", "--strict-questions"])
    def test_absent_questions_file_never_gates(self, tmp_path, flag):
        out = _run(_kb(tmp_path, questions=None,
                       accepted='relation("A", "b", "C").\n',
                       candidates=['A,b,C,sources/a.md,accepted,0.9,']), flag)
        assert out.returncode == 0, out.stderr
