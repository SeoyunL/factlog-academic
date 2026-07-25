# SPDX-License-Identifier: Apache-2.0
"""`factlog status` reports the question/query axis (#536).

status summarised facts, vocabulary, sources, conflicts and the logic report but
said nothing about the KB's *questions*, so a KB whose six declared questions had
all lost their supporting facts — every one of them routed to `review_required` —
printed a clean summary. The one axis that had stopped working was the one axis
status never mentioned.

These tests drive the real CLI (`cli.main(["status", ...])`) and read the printed
line, never a reimplementation of the counting.
"""
from __future__ import annotations

import pytest

from factlog import cli

_REPORT_HEAD = """\
Logic Check Report
==================
engine: wirelog / pyrewire
input: facts/accepted.dl
engine facts: 0
errors: 0
warnings: 0

Query evaluation:
"""


def _kb(tmp_path, *, questions: list[str] | None = None, query_results: list[str] | None = None,
        questions_md: str | None = None):
    """A minimal KB: sources/ (what makes it a KB), policy/questions.md, and a
    logic report whose `Query evaluation:` section carries *query_results*.

    *query_results* is None for "no logic_report.txt yet".
    """
    (tmp_path / "sources").mkdir(parents=True)
    (tmp_path / "facts").mkdir()
    (tmp_path / "policy").mkdir()
    if questions_md is not None:
        (tmp_path / "policy" / "questions.md").write_text(questions_md, encoding="utf-8")
    elif questions is not None:
        body = "# Research questions\n\n" + "".join(f"- [q{i}] {q}\n" for i, q in enumerate(questions, 1))
        (tmp_path / "policy" / "questions.md").write_text(body, encoding="utf-8")
    if query_results is not None:
        text = _REPORT_HEAD + "".join(f"- {item}\n" for item in query_results)
        (tmp_path / "facts" / "logic_report.txt").write_text(text, encoding="utf-8")
    return tmp_path


def _questions_line(tmp_path, capsys) -> str:
    rc = cli.main(["status", "--target", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln.strip().startswith("questions:")]
    assert len(lines) == 1, f"expected exactly one questions line, got {lines!r}\n{out}"
    return lines[0]


def _status_out(tmp_path, capsys) -> str:
    cli.main(["status", "--target", str(tmp_path)])
    return capsys.readouterr().out


class TestTheReportedIssue:
    """The exact shape from #536: every declared question routed to review_required."""

    def test_six_declared_none_answerable(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            questions=[f"question {i}?" for i in range(1, 7)],
            query_results=[f"review_required: question {i}?" for i in range(1, 7)],
        )
        assert _questions_line(kb, capsys) == (
            "  questions:  6 declared, 0 answerable (6 review_required) — see facts/query.dl"
        )

    def test_the_line_is_aligned_with_its_neighbours(self, tmp_path, capsys):
        """Same label column as sources:/conflicts:/logic: — the shell harness and a
        human both read status as a fixed-width table."""
        kb = _kb(tmp_path, questions=["q?"], query_results=["relation results: 1 rows; A, r, B"])
        out = _status_out(kb, capsys)
        starts = {
            ln.split(":", 1)[0] + ":": len(ln) - len(ln.split(":", 1)[1].lstrip())
            for ln in out.splitlines()
            if ln.startswith("  ") and ":" in ln and ln.strip().split(":")[0] in
            {"facts", "vocabulary", "sources", "conflicts", "logic", "questions", "engine"}
        }
        assert len(set(starts.values())) == 1, starts


class TestAnswerableCounting:
    def test_a_resolved_query_is_answerable(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            questions=["a?", "b?"],
            query_results=["relation results: 1 rows; A, r, B", "review_required: b?"],
        )
        assert _questions_line(kb, capsys) == (
            "  questions:  2 declared, 1 answerable (1 review_required) — see facts/query.dl"
        )

    def test_a_verified_zero_counts_as_answerable(self, tmp_path, capsys):
        """A verified negative IS an answer. run_logic_check keeps "checked, found
        nothing" apart from "never got to check" (#347/#350/#362); status must not
        collapse the two one line lower."""
        kb = _kb(
            tmp_path,
            questions=["a?", "b?"],
            query_results=["relation results: 0 rows", "path A -> B: (not found)"],
        )
        assert _questions_line(kb, capsys) == (
            "  questions:  2 declared, 2 answerable — see facts/query.dl"
        )

    def test_unverified_and_malformed_are_not_answerable(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            questions=["a?", "b?", "c?", "d?"],
            query_results=[
                "relation results: unverified — 'develops' is not accepted vocabulary (see Warnings above)",
                "path query malformed — see Errors above",
                "review_required: c?",
                # The emitter's real wording (tools/run_logic_check.py:565) — a count
                # answer is never a bare "count results: N".
                "count results: 2 (distinct objects)",
            ],
        )
        assert _questions_line(kb, capsys) == (
            "  questions:  4 declared, 1 answerable (1 review_required, 1 unverified, 1 malformed)"
            " — see facts/query.dl"
        )

    def test_a_declared_question_with_no_result_line_is_counted(self, tmp_path, capsys):
        kb = _kb(tmp_path, questions=["a?", "b?", "c?"], query_results=["relation results: 1 rows; A, r, B"])
        assert _questions_line(kb, capsys) == (
            "  questions:  3 declared, 1 answerable (2 without a result) — see facts/query.dl"
        )

    @pytest.mark.parametrize("placeholder", [
        "no facts/query.dl found",
        "facts/query.dl is empty (no queries to evaluate)",
        "facts/query.dl has 2 line(s) but none produced a result",
    ])
    def test_the_reports_file_level_placeholders_are_not_results(self, tmp_path, capsys, placeholder):
        """Those three lines describe the FILE, not a question. Counting one as a
        result would let a KB with no query.dl at all report an answered question."""
        kb = _kb(tmp_path, questions=["a?", "b?"], query_results=[placeholder])
        assert _questions_line(kb, capsys) == (
            "  questions:  2 declared, 0 answerable (2 without a result) — see facts/query.dl"
        )


class TestNothingToReport:
    def test_no_questions_declared_reads_n_a(self, tmp_path, capsys):
        kb = _kb(tmp_path, query_results=["relation results: 1 rows; A, r, B"])
        assert _questions_line(kb, capsys) == "  questions:  n/a (none declared in policy/questions.md)"

    def test_a_questions_file_with_only_headings_reads_n_a(self, tmp_path, capsys):
        kb = _kb(tmp_path, questions_md="# Research questions\n\nNo bullets here.\n",
                 query_results=["relation results: 1 rows; A, r, B"])
        assert _questions_line(kb, capsys) == "  questions:  n/a (none declared in policy/questions.md)"

    def test_no_logic_report_says_unknown_not_zero(self, tmp_path, capsys):
        """Absent evidence is not evidence of zero answerable questions — claiming
        "0 answerable" for an unchecked KB is the mirror image of the #536 bug."""
        kb = _kb(tmp_path, questions=["a?", "b?"])
        assert _questions_line(kb, capsys) == (
            "  questions:  2 declared, answerable unknown (no logic_report.txt yet — run /factlog check)"
        )

    def test_a_missing_report_does_not_raise(self, tmp_path, capsys):
        kb = _kb(tmp_path, questions=["a?"])
        assert cli.main(["status", "--target", str(kb)]) == 0
        capsys.readouterr()

    def test_a_report_without_a_query_section_says_unknown_not_zero(self, tmp_path, capsys):
        """A report file with no `Query evaluation:` header at all — truncated
        mid-write, or written by something other than run_logic_check.

        This test used to be named `..._is_survivable` and pinned the opposite
        contract: `0 answerable (2 without a result)`. That name fixed a conclusion
        ("it doesn't crash") over the question the line actually answers, and the
        conclusion contradicted `test_no_logic_report_says_unknown_not_zero` two
        tests up — the same commit called one absence of evidence "unknown" and the
        other "zero". Zero is the reading that reproduces the #536 illusion: a
        confident number with nothing behind it. Renamed and re-pinned to unknown by
        the maintainer's decision (2026-07-25).
        """
        kb = _kb(tmp_path, questions=["a?", "b?"])
        (kb / "facts" / "logic_report.txt").write_text("errors: 0\nwarnings: 0\n", encoding="utf-8")
        assert _questions_line(kb, capsys) == (
            "  questions:  2 declared, answerable unknown"
            " (logic_report.txt has no Query evaluation section — run /factlog check)"
        )

    def test_a_report_without_a_query_section_does_not_raise(self, tmp_path, capsys):
        """What the old name was protecting, kept as its own test."""
        kb = _kb(tmp_path, questions=["a?"])
        (kb / "facts" / "logic_report.txt").write_text("errors: 0\nwarnings: 0\n", encoding="utf-8")
        assert cli.main(["status", "--target", str(kb)]) == 0
        capsys.readouterr()


class TestEvidenceForTheAnswerableCount:
    """The four states the report can be in, and what each licenses status to claim.

    "No answerable questions" and "no evidence about answerable questions" are
    different findings; the count on this line is only meaningful when the report
    actually evaluated the queries.
    """

    def test_no_report_file_is_unknown(self, tmp_path, capsys):
        kb = _kb(tmp_path, questions=["a?", "b?"])
        assert _questions_line(kb, capsys) == (
            "  questions:  2 declared, answerable unknown (no logic_report.txt yet — run /factlog check)"
        )

    def test_a_report_with_no_query_section_is_unknown(self, tmp_path, capsys):
        kb = _kb(tmp_path, questions=["a?", "b?"])
        (kb / "facts" / "logic_report.txt").write_text(
            "Logic Check Report\n==================\nerrors: 0\nwarnings: 0\n", encoding="utf-8"
        )
        assert _questions_line(kb, capsys) == (
            "  questions:  2 declared, answerable unknown"
            " (logic_report.txt has no Query evaluation section — run /factlog check)"
        )

    def test_an_empty_query_section_is_a_stated_zero_not_unknown(self, tmp_path, capsys):
        """The section IS there and carries no result: run_logic_check evaluated the
        queries and produced nothing. That is a zero we can state, and it must not be
        blurred into the unknown above — the fix belongs in facts/query.dl, not in
        re-running the check."""
        kb = _kb(tmp_path, questions=["a?", "b?"], query_results=[])
        assert _questions_line(kb, capsys) == (
            "  questions:  2 declared, 0 answerable (2 without a result) — see facts/query.dl"
        )

    def test_a_file_level_placeholder_is_also_a_stated_zero(self, tmp_path, capsys):
        """Same state as above by another route: the section exists but its only line
        describes the FILE, so it yields no items. Still evidence, still zero."""
        kb = _kb(tmp_path, questions=["a?", "b?"], query_results=["no facts/query.dl found"])
        assert _questions_line(kb, capsys) == (
            "  questions:  2 declared, 0 answerable (2 without a result) — see facts/query.dl"
        )

    def test_a_section_with_items_is_counted(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            questions=["a?", "b?"],
            query_results=["relation results: 1 rows; A, r, B", "review_required: b?"],
        )
        assert _questions_line(kb, capsys) == (
            "  questions:  2 declared, 1 answerable (1 review_required) — see facts/query.dl"
        )


class TestStaleReport:
    def test_a_stale_report_says_so_instead_of_pointing_at_query_dl(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            questions=["a?", "b?"],
            query_results=["relation results: 1 rows; A, r, B", "review_required: b?"],
        )
        report = kb / "facts" / "logic_report.txt"
        query = kb / "facts" / "query.dl"
        query.write_text('relation("A","r","B")?\n', encoding="utf-8")
        import os
        os.utime(report, (0, 1_000_000))
        os.utime(query, (0, 2_000_000))  # an input newer than the report
        line = _questions_line(kb, capsys)
        assert line == (
            "  questions:  2 declared, 1 answerable (1 review_required)"
            " — from a STALE report; run /factlog check"
        )

    def test_a_fresh_report_points_at_query_dl(self, tmp_path, capsys):
        kb = _kb(tmp_path, questions=["a?"], query_results=["relation results: 1 rows; A, r, B"])
        report = kb / "facts" / "logic_report.txt"
        query = kb / "facts" / "query.dl"
        query.write_text('relation("A","r","B")?\n', encoding="utf-8")
        import os
        os.utime(query, (0, 1_000_000))
        os.utime(report, (0, 2_000_000))  # report newer than every input
        assert _questions_line(kb, capsys).endswith(" — see facts/query.dl")


class TestDriftBetweenQuestionsAndQueries:
    """facts/query.dl is drafted 1:1 from policy/questions.md. More evaluated
    queries than declared questions means the counts describe two populations."""

    def test_more_query_lines_than_questions_warns(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            questions=["a?"],
            query_results=["relation results: 1 rows; A, r, B", "relation results: 0 rows", "review_required: c?"],
        )
        out = _status_out(kb, capsys)
        assert "  questions:  1 declared, 2 answerable (1 review_required) — see facts/query.dl" in out
        assert (
            "              ⚠ the report evaluated 3 query line(s) for 1 declared question(s)"
            " — facts/query.dl and policy/questions.md are no longer 1:1"
        ) in out

    def test_a_matching_count_does_not_warn(self, tmp_path, capsys):
        kb = _kb(tmp_path, questions=["a?", "b?"],
                 query_results=["relation results: 1 rows; A, r, B", "review_required: b?"])
        assert "no longer 1:1" not in _status_out(kb, capsys)


class TestQuestionCountingFollowsLoadQuestions:
    """The count is bound to the TARGET KB's policy/questions.md and takes the same
    lines common.load_questions() parses — bullets and numbered items, not headings.
    (load_questions itself reads the ambient FACTLOG_ROOT, so status cannot use it
    for a --target KB without counting another KB's questions.)"""

    @pytest.mark.parametrize("body, expected", [
        ("# H\n\n- [q1] a?\n- [q2] b?\n", 2),
        ("- a?\n* b?\n1. c?\n2. d?\n", 4),
        ("# only a heading\n\nsome prose\n", 0),
        ("- [q1] a?\n\n\n- [q2] b?\n", 2),
        ("# q\n- q1: a?\n- q2. b?\n", 2),
    ])
    def test_declared_counts(self, tmp_path, capsys, body, expected):
        kb = _kb(tmp_path, questions_md=body, query_results=[])
        line = _questions_line(kb, capsys)
        if expected == 0:
            assert line == "  questions:  n/a (none declared in policy/questions.md)"
        else:
            assert line.startswith(f"  questions:  {expected} declared, 0 answerable")

    def test_it_reads_the_target_kb_not_the_ambient_root(self, tmp_path, capsys, monkeypatch):
        other = tmp_path / "other"
        (other / "policy").mkdir(parents=True)
        (other / "policy" / "questions.md").write_text("- [q1] x?\n- [q2] y?\n- [q3] z?\n", encoding="utf-8")
        monkeypatch.chdir(other)
        kb = _kb(tmp_path / "kb", questions=["a?"], query_results=["relation results: 0 rows"])
        assert _questions_line(kb, capsys) == (
            "  questions:  1 declared, 1 answerable — see facts/query.dl"
        )


class TestUnknownPredicateIsNotAnswerable:
    """`unknown query predicate` is the item run_logic_check emits for a line
    validate_query raises a HARD ERROR for (`query unknown predicate`,
    tools/run_logic_check.py:203-205). Counting it as answered reproduced the very
    illusion #536 set out to remove, with the sign flipped: a report saying
    `errors=2` printed `2 answerable`."""

    def test_it_is_not_counted_as_an_answer(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            questions=["a?", "b?"],
            query_results=["unknown query predicate — see Errors above"] * 2,
        )
        assert _questions_line(kb, capsys) == (
            "  questions:  2 declared, 0 answerable (2 unknown predicate) — see facts/query.dl"
        )


class TestValuesAreNotMatched:
    """`relation results: N rows; …` and `path A -> B: …` append ENGINE FACT VALUES
    verbatim (tools/run_logic_check.py:522-530). "unverified" and "malformed" are
    ordinary factlog domain vocabulary — an unverified preprint, a malformed
    randomisation — so a substring test against the whole item turns answered
    questions into unanswerable ones."""

    def test_a_row_value_saying_unverified_is_still_an_answer(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            questions=["a?", "b?"],
            query_results=[
                "relation results: 1 rows; Study X, evidence_status, unverified preprint",
                "relation results: 1 rows; Study Y, protocol_status, malformed randomisation",
            ],
        )
        assert _questions_line(kb, capsys) == (
            "  questions:  2 declared, 2 answerable — see facts/query.dl"
        )

    def test_a_route_through_such_a_node_is_still_an_answer(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            questions=["a?"],
            query_results=["path Study X -> Anthropic: Study X -> unverified preprint -> Anthropic"],
        )
        assert _questions_line(kb, capsys) == (
            "  questions:  1 declared, 1 answerable — see facts/query.dl"
        )

    def test_a_path_row_listing_such_a_node_is_still_an_answer(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            questions=["a?"],
            query_results=["path results: 2 rows; A -> unverified preprint; B -> malformed randomisation"],
        )
        assert _questions_line(kb, capsys) == (
            "  questions:  1 declared, 1 answerable — see facts/query.dl"
        )

    def test_the_unverified_verdict_itself_is_still_caught(self, tmp_path, capsys):
        """Reading the head must not cost the real verdict: the emitter's own
        `<pred> results: unverified — …` still classifies."""
        kb = _kb(
            tmp_path,
            questions=["a?", "b?"],
            query_results=[
                "relation results: unverified — 'develops' is not accepted vocabulary (see Warnings above)",
                "path A -> B: unverified — 'B' is not accepted vocabulary (see Warnings above)",
            ],
        )
        assert _questions_line(kb, capsys) == (
            "  questions:  2 declared, 0 answerable (2 unverified) — see facts/query.dl"
        )


class TestReviewRequiredNeedsItsColon:
    """The policy predicate names come from `.decl` in policy/logic-policy.dl and are
    free-form (factlog/common.py:1038-1060), so a KB may declare
    `review_required_manual` — whose findings are emitted through policy_result_line
    as ordinary `… results: N rows` ANSWERS. The review_required item the emitter
    actually writes is always `review_required: <question>`
    (tools/run_logic_check.py:568-570)."""

    def test_a_policy_predicate_with_the_same_prefix_is_answerable(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            questions=["a?"],
            query_results=["review_required_manual results: 3 rows; A, r1; B, r2; C, r3"],
        )
        assert _questions_line(kb, capsys) == (
            "  questions:  1 declared, 1 answerable — see facts/query.dl"
        )

    def test_the_real_review_required_item_still_classifies(self, tmp_path, capsys):
        kb = _kb(tmp_path, questions=["a?"], query_results=["review_required: a?"])
        assert _questions_line(kb, capsys) == (
            "  questions:  1 declared, 0 answerable (1 review_required) — see facts/query.dl"
        )


class TestAnswerableIsAWhitelist:
    """Nothing falls through TO answerable. An item shape this classifier has not
    learned is `unclassified` and shown on the line — otherwise every new branch in
    `evaluate_queries` would silently tilt the default towards "the engine answered
    this"."""

    @pytest.mark.parametrize("item, expected", [
        # answerable — every shape evaluate_queries/policy_result_line can emit as one
        ("requires_review results: 1 rows; factlog, capability_check", "answerable"),
        ("requires_review results: 0 rows", "answerable"),
        ("relation results: 2 rows; A, r, B; C, r, D", "answerable"),
        ("relation results: 0 rows", "answerable"),
        ("count results: 0 (distinct objects)", "answerable"),
        ("count results: 12 (distinct objects)", "answerable"),
        ("path results: 0 rows", "answerable"),
        ("path results: 1 rows; A -> B", "answerable"),
        ("path A -> B: (not found)", "answerable"),
        ("path A -> C: A -> B -> C", "answerable"),
        ("path A -> B: reachable (engine); no route through the accepted facts", "answerable"),
        # not answerable
        ("relation results: unverified — 'x' is not accepted vocabulary (see Warnings above)",
         "1 unverified"),
        ("count results: unverified — 'x' is not accepted vocabulary (see Warnings above)",
         "1 unverified"),
        ("path results: unverified — 'x' is not accepted vocabulary (see Warnings above)",
         "1 unverified"),
        ("requires_review results: unverified — 'x' is not accepted vocabulary (see Warnings above)",
         "1 unverified"),
        ("path A -> B: unverified — 'B' is not accepted vocabulary (see Warnings above)",
         "1 unverified"),
        ("path query malformed — see Errors above", "1 malformed"),
        ("relation query malformed — see Errors above", "1 malformed"),
        ("count query malformed — see Errors above", "1 malformed"),
        ("review_required: a?", "1 review_required"),
        ("unknown query predicate — see Errors above", "1 unknown predicate"),
        # a shape the classifier does not know
        ("something the report has not emitted before", "1 unclassified"),
        ("relation results: some rows", "1 unclassified"),
        ("path A -> B: X -> Y", "1 unclassified"),
    ])
    def test_each_emitted_item_shape(self, tmp_path, capsys, item, expected):
        kb = _kb(tmp_path, questions=["a?"], query_results=[item])
        line = _questions_line(kb, capsys)
        if expected == "answerable":
            assert line == "  questions:  1 declared, 1 answerable — see facts/query.dl"
        else:
            assert line == f"  questions:  1 declared, 0 answerable ({expected}) — see facts/query.dl"


class TestTheGoldenReportContract:
    """Every other test here hand-writes its `Query evaluation:` section, so all of
    them would keep passing if the classifier and the emitter drifted apart. This one
    feeds the checked-in report run_logic_check actually produced."""

    def test_the_golden_report_classifies(self, tmp_path, capsys):
        from pathlib import Path

        golden = Path(__file__).resolve().parents[1] / "golden" / "logic_report.txt"
        section = golden.read_text(encoding="utf-8").split("Query evaluation:\n", 1)[1]
        items = [ln.removeprefix("- ") for ln in section.splitlines() if ln.startswith("- ")]
        assert len(items) == 7, items  # guard: the golden file still has its queries

        kb = _kb(tmp_path, questions=[f"q{i}?" for i in range(1, 8)])
        (kb / "facts" / "logic_report.txt").write_text(
            golden.read_text(encoding="utf-8"), encoding="utf-8"
        )
        assert _questions_line(kb, capsys) == (
            "  questions:  7 declared, 5 answerable (1 review_required, 1 unverified)"
            " — see facts/query.dl"
        )
