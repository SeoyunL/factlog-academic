# SPDX-License-Identifier: Apache-2.0
"""The logic report's header counts query evaluation, not just policy findings (#535).

`facts/logic_report.txt` tallied engine facts, review facts, policy findings, errors
and warnings — every axis except the questions. A KB whose six declared questions had
ALL routed to a human printed `policy findings: 21 / errors: 0 / warnings: 0` above six
`review_required` lines: six questions, zero answers, and no number anywhere saying so.
`queries: N (evaluated: M, …)` is that missing tally, in the same slot and with the same
standing as `policy findings:` — a COUNT of the section below, never a new failure
condition (the exit code still turns on errors alone).

The counting is `factlog.cli._classify_query_results`, the same function `factlog status`
reads this section with (#536), so the two cannot tell different stories about one file.
The contract tests below pin that: they classify what status would parse back OUT of the
report and compare it against the header the report wrote IN.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from factlog import cli
from factlog.cli import _classify_query_results, _query_evaluation_section

from run_logic_check import query_summary_line

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_REPORT = REPO_ROOT / "tests" / "golden" / "logic_report.txt"


class TestTheReportedIssue:
    """The exact shape from #535: every query routed to review_required."""

    def test_six_review_required_are_counted_in_the_header(self):
        items = [f"review_required: question {i}?" for i in range(1, 7)]
        assert query_summary_line(items) == "queries: 6 (evaluated: 0, review_required: 6)"

    def test_the_line_reads_like_its_neighbours(self):
        """`policy findings: 21` sits two lines up — same `label: count` shape, so a
        reader (and a grep) meets one header format, not two."""
        line = query_summary_line(["review_required: q?"])
        assert re.fullmatch(r"queries: \d+ \(.+\)", line), line


class TestCounting:
    def test_mixed_evaluated_and_unanswerable(self):
        items = [
            "relation results: 1 rows; Claude Code, developed_by, Anthropic",
            "relation results: 0 rows",
            "review_required: who decides?",
        ]
        assert query_summary_line(items) == "queries: 3 (evaluated: 2, review_required: 1)"

    def test_no_queries_at_all(self):
        """The three "nothing to evaluate" placeholders (no query.dl, empty query.dl,
        no line produced a result) are not results and reach this function as an empty
        list — the same list status parses back, which drops them by name."""
        assert query_summary_line([]) == "queries: 0 (evaluated: 0)"

    def test_a_verified_zero_is_evaluated(self):
        """run_logic_check works hard to keep "the engine checked and found nothing"
        apart from "the engine never got to check" (#347/#350/#362). A verified
        negative is an answer; the header must not undo that distinction."""
        items = ["count results: 0 (distinct objects)", "path A -> B: (not found)"]
        assert query_summary_line(items) == "queries: 2 (evaluated: 2)"

    def test_every_reason_bucket_appears(self):
        items = [
            "relation results: 2 rows; A, r, B; A, r, C",
            "review_required: who decides?",
            "relation results: unverified — 'develops' is not accepted vocabulary "
            "(see Warnings above)",
            "count query malformed — see Errors above",
            "unknown query predicate — see Errors above",
            "some shape this function has never seen",
        ]
        assert query_summary_line(items) == (
            "queries: 6 (evaluated: 1, review_required: 1, unverified: 1, malformed: 1, "
            "unknown predicate: 1, unclassified: 1)"
        )

    def test_zero_buckets_are_omitted(self):
        """Only the reasons that actually occurred; a healthy KB reads
        `queries: 2 (evaluated: 2)`, not five zeroes."""
        assert query_summary_line(["relation results: 1 rows; A, r, B"] * 2) == (
            "queries: 2 (evaluated: 2)"
        )


class TestMisclassificationTraps:
    """The three defects #536's review found in this classification. Repeating any of
    them here would print a header that contradicts the list beneath it."""

    def test_unknown_predicate_is_not_evaluated(self):
        """validate_query raises `query unknown predicate` as a HARD ERROR for this
        item: the question is confirmed unanswerable, not answered."""
        assert query_summary_line(["unknown query predicate — see Errors above"]) == (
            "queries: 1 (evaluated: 0, unknown predicate: 1)"
        )

    def test_fact_values_carrying_domain_vocabulary_stay_evaluated(self):
        """`relation results: N rows; …` appends ENGINE FACT VALUES verbatim, and
        `unverified`/`malformed` are ordinary factlog domain words ("unverified
        preprint"). A substring test against the whole item would flip two answered
        questions into the unanswerable buckets."""
        items = [
            "relation results: 1 rows; arXiv:2401.00001, status, unverified preprint",
            "relation results: 1 rows; dataset-7, quality, malformed rows dropped",
        ]
        assert query_summary_line(items) == "queries: 2 (evaluated: 2)"

    def test_review_required_prefix_stops_at_the_colon(self):
        """A policy may `.decl` a predicate named `review_required_manual`, whose
        ordinary `… results: N rows` findings ARE answers."""
        items = [
            "review_required_manual results: 3 rows; A, needs a human",
            "review_required: who decides?",
        ]
        assert query_summary_line(items) == "queries: 2 (evaluated: 1, review_required: 1)"

    def test_an_unrecognised_shape_is_shown_not_folded_into_evaluated(self):
        """ANSWERABLE is a whitelist. If evaluate_queries grows a result form neither
        side has learned, it surfaces as an unexplained bucket instead of silently
        inflating the headline number."""
        line = query_summary_line(["future results: something new"])
        assert line == "queries: 1 (evaluated: 0, unclassified: 1)"


def _header_counts(line: str) -> dict[str, int]:
    """`queries: 6 (evaluated: 0, review_required: 6)` -> {'evaluated': 0, …}."""
    inner = re.fullmatch(r"queries: (\d+) \((.*)\)", line)
    assert inner, f"unparseable header line: {line!r}"
    counts = {"total": int(inner[1])}
    for note in inner[2].split(", "):
        label, _, n = note.rpartition(": ")
        counts[label] = int(n)
    return counts


ITEM_SETS = [
    pytest.param([f"review_required: question {i}?" for i in range(1, 7)], id="all-review"),
    pytest.param([], id="none"),
    pytest.param(
        [
            "relation results: 1 rows; Claude Code, developed_by, Anthropic",
            "relation results: 0 rows",
            "review_required: who decides?",
            "unknown query predicate — see Errors above",
            "path query malformed — see Errors above",
            "count results: unverified — 'develops' is not accepted vocabulary "
            "(see Warnings above)",
        ],
        id="mixed",
    ),
    pytest.param(
        [
            "relation results: 1 rows; arXiv:2401.00001, status, unverified preprint",
            "review_required_manual results: 2 rows; A, malformed rows dropped",
        ],
        id="domain-vocabulary-in-values",
    ),
]


def _report(items: list[str]) -> str:
    """A logic report as run_logic_check writes one, header line included."""
    return (
        "Logic Check Report\n"
        "==================\n"
        "engine: wirelog / pyrewire\n"
        "input: facts/accepted.dl\n"
        "engine facts: 0\n"
        "policy findings: 0\n"
        f"{query_summary_line(items)}\n"
        "errors: 0\n"
        "warnings: 0\n"
        "\n"
        "Query evaluation:\n" + "".join(f"- {item}\n" for item in items)
    )


class TestStatusContract:
    """One report, one story. The header is written from the items; status reads the
    items back off disk. If these two counts ever disagree, #535's confusion is back —
    now with the header itself as the misleading number."""

    @pytest.mark.parametrize("items", ITEM_SETS)
    def test_header_matches_what_status_parses_back(self, items):
        seen, parsed = _query_evaluation_section(_report(items))
        assert seen
        assert parsed == items  # the round-trip itself, before any counting
        counts = _classify_query_results(parsed)
        header = _header_counts(query_summary_line(items))
        assert header["total"] == len(parsed)
        assert header["evaluated"] == counts["answerable"]
        for label, key in (
            ("review_required", "review_required"),
            ("unverified", "unverified"),
            ("malformed", "malformed"),
            ("unknown predicate", "unknown_predicate"),
            ("unclassified", "unclassified"),
        ):
            assert header.get(label, 0) == counts[key]

    @pytest.mark.parametrize("items", ITEM_SETS)
    def test_status_questions_line_reports_the_same_numbers(self, items, tmp_path, capsys):
        """Through the real `factlog status`, not a reimplementation of its counting."""
        (tmp_path / "sources").mkdir()
        (tmp_path / "facts").mkdir()
        (tmp_path / "policy").mkdir()
        # One declared question per evaluated item, and never zero: with nothing
        # declared status prints `n/a` and never reaches the counting branch.
        declared = max(len(items), 1)
        (tmp_path / "policy" / "questions.md").write_text(
            "# Research questions\n\n"
            + "".join(f"- [q{i}] question {i}?\n" for i in range(1, declared + 1)),
            encoding="utf-8",
        )
        (tmp_path / "facts" / "logic_report.txt").write_text(_report(items), encoding="utf-8")

        assert cli.main(["status", "--target", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        line = next(ln for ln in out.splitlines() if ln.strip().startswith("questions:"))
        header = _header_counts(query_summary_line(items))

        assert f"{header['evaluated']} answerable" in line, f"{line!r} vs {header}"
        for label, word in (
            ("review_required", "review_required"),
            ("unverified", "unverified"),
            ("malformed", "malformed"),
            ("unknown predicate", "unknown predicate"),
            ("unclassified", "unclassified"),
        ):
            n = header.get(label, 0)
            if n:
                assert f"{n} {word}" in line, f"{line!r} vs {header}"

    def test_the_golden_report_header_matches_its_own_section(self):
        """tests/golden/logic_report.txt is the byte-for-byte determinism pin
        (tests/golden.sh). Its committed header must describe its committed section,
        or the fixture teaches the wrong number."""
        text = GOLDEN_REPORT.read_text(encoding="utf-8")
        header = next(ln for ln in text.splitlines() if ln.startswith("queries: "))
        _, items = _query_evaluation_section(text)
        assert header == query_summary_line(items)
        assert header == "queries: 7 (evaluated: 5, review_required: 1, unverified: 1)"


HEADER_CSV = "subject,relation,object,source,status,confidence,note"


def _env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    # The tool scripts import their sibling ``common`` via sys.path[0], but they also
    # import ``factlog`` — put the repo root ahead of any editable install pointing
    # elsewhere, the way the sibling end-to-end tests do.
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    env["FACTLOG_ROOT"] = str(root)
    return env


class TestGeneratedReport:
    """End-to-end through the real scripts: the header the engine run actually writes."""

    def test_a_kb_whose_every_question_needs_review(self, tmp_path):
        pytest.importorskip("pyrewire", reason="run_logic_check needs the engine")
        kb = tmp_path / "wiki"
        subprocess.run(
            [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
            check=True,
            capture_output=True,
            env=_env(tmp_path),
        )
        (kb / "sources" / "a.md").write_text("a\n", encoding="utf-8")
        (kb / "facts" / "candidates.csv").write_text(
            f"{HEADER_CSV}\nA,uses,B,sources/a.md,confirmed,0.9,\n", encoding="utf-8"
        )
        (kb / "facts" / "query.dl").write_text(
            'review_required("who decides?")?\n'
            'review_required("who reviews?")?\n'
            'relation("A", "uses", O)?\n',
            encoding="utf-8",
        )
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "compile_facts.py")],
            cwd=kb,
            check=True,
            capture_output=True,
            env=_env(kb),
        )
        run = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "run_logic_check.py")],
            cwd=kb,
            capture_output=True,
            text=True,
            env=_env(kb),
        )
        assert run.returncode == 0, run.stderr
        text = (kb / "facts" / "logic_report.txt").read_text(encoding="utf-8")
        header = next(ln for ln in text.splitlines() if ln.startswith("queries: "))
        assert header == "queries: 3 (evaluated: 1, review_required: 2)"
        # Same standing as `policy findings:` — a tally, never a failure condition.
        assert "errors: 0" in text
        _, items = _query_evaluation_section(text)
        assert header == query_summary_line(items)
