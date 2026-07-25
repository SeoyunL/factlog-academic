# SPDX-License-Identifier: Apache-2.0
"""The coverage critic's question axis (#537).

The source axis cannot see a lost RELATION. When every row of a relation is
dropped at merge, no candidate row cites it, so there is no orphan citation and
no uncovered source — the summary reads `0 orphan citation(s)` and is, by its own
definition, correct. Meanwhile the questions the KB was built to answer have no
vocabulary left in engine input. A guard named "silent-omission" that reports a
clean line in that state is the omission.

Since #558 the source axis reports one more figure, read off runs/*.json rather
than candidates.csv: the sources run rows cite that are not on disk. That covers
the row-level cause of SOME losses of this shape (the source file was deleted), so
the source line is no longer unconditionally clean in a "relation dropped at
merge" state. It is still clean in the state THIS file measures, and that is a
property of the fixtures rather than an accident: `_kb` writes no runs/*.json at
all, so the new figure is 0 and omitted. The #537 premise holds wherever a
relation was lost for a reason OTHER than its source disappearing — a policy
change, a rewritten extraction, a row edited by hand. A fixture that starts
writing run files must re-read the summary assertion below rather than assume it.

Where a question HAS a draft in `facts/query.dl`, the mapping is READ, not
inferred: each draft is anchored by a `// q3: ...` comment carrying the id
policy/questions.md declares, and the verdict is `common.classify_query` — the
engine's own gate. `facts/query.dl` is written by the LLM `/factlog query` step, so
a question can have no draft at all; those fall back to an estimate off the
question text, labelled as an estimate in the report.

The last class in this file is the one that matters most: an end-to-end contract
against the bundled `examples/sample-kb` asserting the axis never calls a question
unresolvable that the ENGINE just answered. An implementation that ignored the
committed drafts and matched relation names against the question TEXT did exactly
that for five of the sample KB's seven questions.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest
from source_coverage import (
    draft_verdict,
    effective_relations,
    estimated_verdict,
    mentioned_relations,
    query_drafts,
    question_rows,
    relation_argument,
    relation_probes,
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
        ) == ("lost", "relation 'develops' has no rows in engine input")

    def test_an_attribute_relation_with_no_rows_is_named(self):
        # The report frame is English and QUOTES the name as declared, the repo's
        # convention for a message about a Korean identifier (tools/validate.py).
        assert draft_verdict('count("Claude Code", "총_문항_수")?', _ONE_FACT, "") == (
            "lost",
            "relation '총_문항_수' has no rows in engine input",
        )

    def test_an_entity_missing_from_engine_input_is_a_loss_too(self):
        state, reason = draft_verdict(
            'relation("factlog", "developed_by", "Anthropic")?', _ONE_FACT, ""
        )
        assert state == "lost"
        assert reason.startswith("no rows in engine input — ")
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


# --- the fallback estimate, for a question with no draft ----------------------


class TestRelationProbes:
    """A relation is stored `총_문항_수`; a question spells it any of three ways."""

    def test_separator_folded_form_is_probed(self):
        assert "총 문항 수" in relation_probes("총_문항_수")

    def test_separator_removed_form_is_probed_for_cjk(self):
        # `총문항수는?` is ordinary Korean spelling. Without this probe the question
        # named no relation at all, which reads as "never grounded" instead of the
        # loss (or the coverage) it actually is.
        assert "총문항수" in relation_probes("총_문항_수")

    def test_name_as_declared_is_always_probed(self):
        assert "총_문항_수" in relation_probes("총_문항_수")

    def test_hyphen_folds_too(self):
        assert "published year" in relation_probes("published-year")

    def test_ascii_fold_needs_a_word_longer_than_two_chars(self):
        # `is_a` -> "is a" matches nearly every English question; a question would
        # be reported grounded in a relation it never names.
        assert relation_probes("is_a") == ["is_a"]
        assert "developed by" in relation_probes("developed_by")

    def test_the_joined_probe_stays_cjk_only(self):
        # English never drops the separator, so `developedby` would only be noise.
        assert "developedby" not in relation_probes("developed_by")

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


class TestEffectiveRelations:
    def test_a_broad_name_contained_in_a_narrower_one_is_shadowed(self):
        assert effective_relations(["총_문항_수", "문항_수"]) == ["총_문항_수"]

    def test_independent_relations_all_survive(self):
        assert effective_relations(["developed_by", "uses"]) == ["developed_by", "uses"]

    def test_nothing_to_shadow(self):
        assert effective_relations([]) == []


class TestEstimatedVerdict:
    ALIASES: dict[str, str] = {}

    def _verdict(self, question, vocab, accepted, aliases=None):
        return estimated_verdict(
            question, vocab, accepted, self.ALIASES if aliases is None else aliases
        )

    def test_a_surviving_broad_relation_must_not_answer_for_the_lost_narrow_one(self):
        # THE regression this axis exists to catch, reproduced with a relation pair
        # that really is declared together in a KB: 총_문항_수 has zero rows, 문항_수
        # still has one about the very subject the question names. Judged as
        # independent evidence, the broad survivor made the report read
        # `0 unresolvable` — the silence the tool is named after.
        assert self._verdict(
            "factlog 벤치마크의 총 문항 수는?",
            {"총_문항_수", "문항_수"},
            _accepted(("factlog 벤치마크", "문항_수", "20")),
        ) == ("lost", "relation '총_문항_수' has no rows in engine input")

    def test_the_reason_names_the_relation_that_is_actually_missing(self):
        # 총_문항_수 HAS rows (on another subject) and 문항_수 does not. Blaming
        # 문항_수 would point at a relation the question never named on its own —
        # it is only there as a substring — and would hide the real shortfall,
        # which is the subject mismatch.
        assert self._verdict(
            "factlog 벤치마크의 총 문항 수는?",
            {"총_문항_수", "문항_수"},
            _accepted(("다른 벤치", "총_문항_수", "60")),
        ) == (
            "unmatched",
            "relation '총_문항_수' has rows in engine input, but none about "
            "anything the question names",
        )

    def test_a_relation_gone_from_engine_input_is_named(self):
        assert self._verdict(
            "factlog 벤치마크의 총 문항 수는?", {"총_문항_수"}, []
        ) == ("lost", "relation '총_문항_수' has no rows in engine input")

    def test_evidence_about_a_named_entity_resolves(self):
        assert self._verdict(
            "Who is Claude Code developed by?", {"developed_by"}, _ONE_FACT
        ) == ("resolvable", "")

    def test_rows_under_a_named_relation_about_nothing_the_question_names(self):
        # Naming a relation is not evidence on its own: 벤치마크 survives on an
        # unrelated arXiv paper in the issue's KB.
        state, reason = self._verdict(
            "Who is factlog developed by?", {"developed_by"}, _ONE_FACT
        )
        assert state == "unmatched"
        assert reason.startswith("relation 'developed_by' has rows in engine input")

    def test_a_question_naming_no_relation_has_no_vocabulary(self):
        assert self._verdict("C유형 질문의 주제는 무엇인가?", {"채점_정의"}, []) == (
            "no_vocabulary",
            "the question names no relation this KB declares",
        )

    def test_an_nfd_stored_question_still_finds_its_evidence(self):
        # macOS stores Korean text NFD. The relation match normalises the question,
        # so the grounding half has to see the SAME normalised text or every
        # question in an NFD questions.md reads as ungrounded.
        question = unicodedata.normalize("NFD", "factlog 벤치마크의 총 문항 수는?")
        assert self._verdict(
            question, {"총_문항_수"}, _accepted(("factlog 벤치마크", "총_문항_수", "20"))
        ) == ("resolvable", "")

    def test_a_separator_free_spelling_still_finds_its_relation(self):
        assert self._verdict(
            "factlog 벤치마크의 총문항수는?", {"총_문항_수"}, []
        ) == ("lost", "relation '총_문항_수' has no rows in engine input")

    def test_alias_is_not_mistaken_for_a_missing_relation(self):
        # The question names the alias; engine input carries the canonical.
        assert self._verdict(
            "Claude Code 게재연도는?",
            {"게재연도"},
            _accepted(("Claude Code", "published_year", "2024")),
            {"게재연도": "published_year"},
        ) == ("resolvable", "")


class TestQuestionRows:
    QUESTIONS = [
        {"id": "q1", "question": "Who developed Claude Code?"},
        {"id": "q2", "question": "Does this KB record that Anthropic develops it?"},
    ]
    VOCAB = {"developed_by", "develops"}

    def _rows(self, drafts, accepted=_ONE_FACT):
        return {
            row["id"]: row
            for row in question_rows(self.QUESTIONS, drafts, accepted, "", self.VOCAB, {})
        }

    def test_a_question_with_no_draft_falls_back_to_the_estimate(self):
        # "the query step has not run" is a different claim from "the engine
        # refused the draft" (#538), so the reason says which one this is.
        row = self._rows({})["q2"]
        assert row["estimated"] is True
        assert row["state"] == "lost"
        assert row["reason"] == (
            "no query draft; estimated from the question text: "
            "relation 'develops' has no rows in engine input"
        )

    def test_a_drafted_question_is_never_labelled_an_estimate(self):
        row = self._rows({
            "q1": ['relation("Claude Code", "developed_by", "Anthropic")?'],
        })["q1"]
        assert row["estimated"] is False
        assert row["state"] == "resolvable"

    def test_each_question_is_judged_by_its_own_draft(self):
        rows = self._rows({
            "q1": ['relation("Claude Code", "developed_by", "Anthropic")?'],
            "q2": ['relation("Claude Code", "develops", "Anthropic")?'],
        })
        assert rows["q1"]["state"] == "resolvable"
        assert rows["q2"]["reason"] == "relation 'develops' has no rows in engine input"

    def test_a_question_with_any_evaluable_draft_is_resolvable(self):
        # The engine answers it, so the axis must not call it unresolvable.
        rows = self._rows({
            "q1": [
                'relation("Claude Code", "develops", "Anthropic")?',
                'relation("Claude Code", "developed_by", "Anthropic")?',
            ],
        })
        assert rows["q1"]["state"] == "resolvable"

    def test_a_loss_outranks_a_review_route_on_the_same_question(self):
        rows = self._rows({
            "q1": [
                'review_required("Which steps?")?',
                'relation("Claude Code", "develops", "Anthropic")?',
            ],
        })
        assert rows["q1"]["state"] == "lost"

    def test_empty_engine_input_loses_every_drafted_relation(self):
        rows = self._rows(
            {"q1": ['relation("Claude Code", "developed_by", "Anthropic")?']}, accepted=[]
        )
        assert rows["q1"]["state"] == "lost"


# --- CLI contract -------------------------------------------------------------


def _kb(tmp_path, *, questions=None, query=None, accepted=None, candidates=None,
        attributes=None, single_valued=None, aliases=None, source="a.md", raw=None):
    """A minimal KB root; every engine input is written explicitly by the test.

    ``raw`` writes BYTES for a named policy file, for the tests that hand this axis
    a file it cannot decode.
    """
    kb = tmp_path / "kb"
    for name in ("sources", "pages", "facts", "decisions", "policy"):
        (kb / name).mkdir(parents=True)
    if source is not None:
        (kb / "sources" / source).write_text("내용\n", encoding="utf-8")
    if questions is not None:
        (kb / "policy" / "questions.md").write_text(questions, encoding="utf-8")
    if query is not None:
        (kb / "facts" / "query.dl").write_text(query, encoding="utf-8")
    if attributes is not None:
        (kb / "policy" / "attribute-relations.md").write_text(attributes, encoding="utf-8")
    if single_valued is not None:
        (kb / "policy" / "single-valued.md").write_text(single_valued, encoding="utf-8")
    if aliases is not None:
        (kb / "policy" / "relation-aliases.md").write_text(aliases, encoding="utf-8")
    if accepted is not None:
        (kb / "facts" / "accepted.dl").write_text(accepted, encoding="utf-8")
    rows = [_HEADER] + list(candidates or [])
    (kb / "facts" / "candidates.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    for name, blob in (raw or {}).items():
        (kb / "policy" / name).write_bytes(blob)
    return kb


def _hangul(text: str) -> bool:
    return any("가" <= ch <= "힣" for ch in text)


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

# The same KB with the query step never run — the state the fallback estimate is
# there for. `문항_수` still has a row about the very subject q3 names, so a broad
# relation is standing where the lost narrow one used to be.
_LOST_NO_DRAFT = dict(
    questions="- [q3] factlog 벤치마크의 총 문항 수는?\n",
    attributes="총_문항_수\n문항_수\n",
    accepted='relation("factlog 벤치마크", "문항_수", "20").\n',
    candidates=['factlog 벤치마크,문항_수,20,sources/a.md,accepted,0.9,'],
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
            "(relation '총_문항_수' has no rows in engine input)" in out.stdout
        )
        assert (
            "  - [q6] C유형 질문의 주제는 무엇인가?  "
            "(routed to human review (review_required))" in out.stdout
        )

    @pytest.mark.parametrize("kb_kwargs", [_LOST, _LOST_NO_DRAFT], ids=["drafted", "estimated"])
    def test_the_report_frames_are_english_around_the_quoted_original(
        self, tmp_path, kb_kwargs
    ):
        # The repo's convention for a message about a Korean identifier
        # (tools/validate.py): an ENGLISH frame quoting the name as declared. This
        # file used to be the only tool under tools/ emitting Korean prose.
        out = _run(_kb(tmp_path, **kb_kwargs))
        summary = [ln for ln in out.stdout.splitlines() if ln.startswith("questions:")]
        reasons = [
            ln.rsplit("  (", 1)[1] for ln in out.stdout.splitlines() if ln.startswith("  - [")
        ]
        assert summary and reasons, out.stdout
        for frame in summary + reasons:
            # The question text and the relation name are quoted verbatim; strip
            # those and nothing Korean may be left in the wording around them.
            bare = re.sub(r"'[^']*'", "''", frame)
            assert not _hangul(bare), frame

    def test_the_source_axis_alone_reports_nothing_wrong(self, tmp_path):
        # The premise of #537: the source line is clean in exactly this state, and
        # its wording is untouched by this axis (tests/test_coverage.sh greps it).
        # "This state" is the narrower one #558 leaves: the relation's rows are
        # gone but its SOURCE FILE is still on disk and no run file cites a missing
        # one, so the run-cited figure is 0 and omitted from the line. A KB that
        # lost the relation BY deleting the source now DOES get a line — from the
        # source axis, not this one — which is #558's point, not a dent in #537's.
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

    def test_a_drafted_verdict_and_an_estimate_are_labelled_differently(self, tmp_path):
        kb = _kb(
            tmp_path,
            questions=(
                "- [q1] Who is Claude Code developed by?\n"
                "- [q2] factlog 벤치마크의 총 문항 수는?\n"
            ),
            query=(
                "// q1: Who is Claude Code developed by?\n"
                'relation("Claude Code", "develops", "Anthropic")?\n'
            ),
            attributes="총_문항_수\n",
            accepted='relation("Claude Code", "developed_by", "Anthropic").\n',
            candidates=['Claude Code,developed_by,Anthropic,sources/a.md,accepted,0.9,'],
        )
        out = _run(kb)
        assert "1 question(s) have no query draft — estimated" in out.stdout
        assert (
            "- [q1] Who is Claude Code developed by?  "
            "(relation 'develops' has no rows in engine input)" in out.stdout
        )
        assert (
            "- [q2] factlog 벤치마크의 총 문항 수는?  (no query draft; estimated from the "
            "question text: relation '총_문항_수' has no rows in engine input)" in out.stdout
        )

    def test_the_estimate_catches_the_loss_a_broad_relation_would_mask(self, tmp_path):
        # CLI end of the pin: 문항_수 survives on the subject the question names and
        # must not answer for the missing 총_문항_수.
        out = _run(_kb(tmp_path, **_LOST_NO_DRAFT), "--strict-questions")
        assert out.returncode == 1, out.stdout
        assert "questions: 1 declared; 0 with resolvable vocabulary, 1 unresolvable" in out.stdout
        assert "relation '총_문항_수' has no rows in engine input" in out.stdout

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
        # facts/query.dl is LLM-authored, so its absence is a normal state and the
        # summary says the verdicts below it are estimates.
        kb = _kb(tmp_path, **{k: v for k, v in _LOST.items() if k != "query"})
        out = _run(kb)
        assert out.returncode == 0, out.stderr
        assert (
            "(facts/query.dl absent — run /factlog query; questions estimated from text)"
            in out.stdout
        )

    def test_a_non_utf8_questions_file_does_not_take_the_tool_down(self, tmp_path):
        # A Korean policy file stored EUC-KR is a real thing in this repo's world.
        # The source axis never read policy/questions.md, so raising here would take
        # source coverage away from a KB that had it before this axis existed.
        kb = _kb(tmp_path, questions=None, accepted='relation("A", "b", "C").\n',
                 candidates=['A,b,C,sources/a.md,accepted,0.9,'],
                 raw={"questions.md": b"\xff\xfe- [q1] hi\n"})
        out = _run(kb)
        assert out.returncode == 0, out.stderr
        assert "coverage: 1 source(s); 1 covered" in out.stdout
        assert "questions: 0 declared (" in out.stdout
        assert "codec can't decode" in out.stdout

    def test_a_non_utf8_questions_file_on_a_kb_with_no_sources(self, tmp_path):
        # `main` reports an empty KB down a SEPARATE early-return branch, and that
        # is the branch the regression was reported on ("coverage: no source files"
        # printed, then the traceback). Both branches run the question axis, so both
        # get a pin.
        kb = _kb(tmp_path, questions=None, source=None,
                 raw={"questions.md": b"\xff\xfe- [q1] hi\n"})
        out = _run(kb)
        assert out.returncode == 0, out.stderr
        assert "coverage: no source files" in out.stdout
        assert "questions: 0 declared (" in out.stdout
        assert "codec can't decode" in out.stdout

    @pytest.mark.parametrize("flag", ["--strict", "--strict-questions"])
    def test_a_non_utf8_questions_file_never_gates(self, tmp_path, flag):
        # An unreadable file is not a finding either axis may fail a build on.
        kb = _kb(tmp_path, questions=None, accepted='relation("A", "b", "C").\n',
                 candidates=['A,b,C,sources/a.md,accepted,0.9,'],
                 raw={"questions.md": b"\xff\xfe- [q1] hi\n"})
        out = _run(kb, flag)
        assert out.returncode == 0, out.stderr

    def test_a_non_utf8_relation_policy_file_does_not_take_the_tool_down(self, tmp_path):
        kb = _kb(tmp_path, questions="- [q1] 총 문항 수는?\n",
                 accepted='relation("A", "b", "C").\n',
                 candidates=['A,b,C,sources/a.md,accepted,0.9,'],
                 raw={"attribute-relations.md": b"\xff\xfe\xc3\xd1_\xb9\xae\xc7\xd7_\xbc\xf6\n"})
        out = _run(kb)
        assert out.returncode == 0, out.stderr
        assert "coverage: 1 source(s); 1 covered" in out.stdout
        assert "questions: 1 declared; vocabulary unreadable" in out.stdout

    def test_a_non_utf8_relation_policy_file_never_gates(self, tmp_path):
        kb = _kb(tmp_path, questions="- [q1] 총 문항 수는?\n",
                 accepted='relation("A", "b", "C").\n',
                 candidates=['A,b,C,sources/a.md,accepted,0.9,'],
                 raw={"attribute-relations.md": b"\xff\xfe\xc3\xd1_\xb9\xae\xc7\xd7_\xbc\xf6\n"})
        assert _run(kb, "--strict-questions").returncode == 0

    def test_a_relation_declared_only_in_single_valued_md_is_still_named(self, tmp_path):
        # A relation declared in single-valued.md (or typed-relations.md) and then
        # emptied is exactly as lost as one declared in attribute-relations.md;
        # leaving those files out of the vocabulary downgraded the report to the
        # vaguer "names no relation this KB declares".
        kb = _kb(tmp_path, questions="- [q1] What is the published year of Claude Code?\n",
                 single_valued="published_year\n",
                 accepted='relation("Claude Code", "developed_by", "Anthropic").\n',
                 candidates=['Claude Code,developed_by,Anthropic,sources/a.md,accepted,0.9,'])
        out = _run(kb)
        assert "relation 'published_year' has no rows in engine input" in out.stdout

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

    def test_a_question_naming_no_known_relation_never_gates(self, tmp_path):
        # The scaffolded question of a fresh `factlog init` KB: nothing has been
        # lost, so the opt-in gate stays silent (it is a LOSS gate, not a
        # completeness gate) while the line is still reported.
        kb = _kb(tmp_path, questions="- [q1] 이 KB는 무엇을 다루는가?\n",
                 accepted='relation("A", "b", "C").\n',
                 candidates=['A,b,C,sources/a.md,accepted,0.9,'])
        out = _run(kb, "--strict-questions")
        assert out.returncode == 0, out.stderr
        assert "--strict-questions:" not in out.stderr
        assert "1 naming no known relation" in out.stdout

    def test_an_estimate_that_finds_a_loss_does_gate(self, tmp_path):
        out = _run(_kb(tmp_path, **_LOST_NO_DRAFT), "--strict-questions")
        assert out.returncode == 1
        assert "--strict-questions: 1 declared question(s)" in out.stderr

    def test_rows_about_other_subjects_do_not_gate(self, tmp_path):
        # The relation is THERE; only the estimate's entity match falls short. That
        # is too weak a signal to fail a build on, so it is reported, not gated.
        kb = _kb(tmp_path, questions="- [q3] factlog 벤치마크의 총 문항 수는?\n",
                 attributes="총_문항_수\n",
                 accepted='relation("다른 벤치", "총_문항_수", "60").\n',
                 candidates=['다른 벤치,총_문항_수,60,sources/a.md,accepted,0.9,'])
        out = _run(kb, "--strict-questions")
        assert out.returncode == 0, out.stderr
        assert "1 with no matching rows" in out.stdout

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
            "(relation 'develops' has no rows in engine input)" in coverage
        ), coverage
