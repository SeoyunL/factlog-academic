# SPDX-License-Identifier: Apache-2.0
"""The logic report's header counts query evaluation, not just policy findings (#535).

`facts/logic_report.txt` tallied engine facts, review facts, policy findings, errors
and warnings — every axis except the questions. A KB whose six declared questions had
ALL routed to a human printed `policy findings: 21 / errors: 0 / warnings: 0` above six
`review_required` lines: six questions, zero answers, and no number anywhere saying so.
`queries: N (evaluated: M, …)` is that missing tally, in the same slot and with the same
standing as `policy findings:` — a COUNT of the section below, never a new failure
condition (the exit code still turns on errors alone).

The counting is `factlog.common.classify_query_results`, the same function `factlog
status` reads this section with (#536), so the two cannot tell different stories about
one file. The contract tests below pin that: they classify what status would parse back
OUT of the report and compare it against the header the report wrote IN.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import run_logic_check as rlc
from factlog import cli
from factlog.common import classify_query_results
from factlog.cli import _query_evaluation_section

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
    # The unicode line separators python's str.splitlines() breaks on and
    # compile_facts' control-character guard (#331) explicitly ACCEPTS in a value.
    # Read back with splitlines(), one of these inside a value turned one item into
    # two physical lines and every item AFTER it disappeared from status while the
    # header still counted it — a report whose two numbers described different
    # sections of one file.
    pytest.param(
        [
            "relation results: 1 rows; A, quote, line one\u2028line two",
            "relation results: 1 rows; B, quote, para\u2029break",
            "relation results: 1 rows; C, quote, next\u0085line",
            "review_required: who decides?",
        ],
        id="unicode-line-separators-in-values",
    ),
    # Academic entity names carry ": " (`BERT: Pre-training of…`) and the pinned path
    # form splits on it. A single leftmost split read `path A -> Fig: 3: A -> Fig: 3`
    # as unclassified — "0 answered" about a route the report had drawn correctly.
    pytest.param(
        [
            "path A -> Fig: 3: A -> Fig: 3",
            "path BERT: Pre-training -> C: BERT: Pre-training -> B -> C",
            "path A -> Fig: 3: (not found)",
        ],
        id="colon-bearing-entity-names",
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
        counts = classify_query_results(parsed)
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


class TestMalformedPolicyQueryIsNotEvaluated:
    """A policy query validate_query raises a HARD ERROR for must not be counted as
    an answered question (#535 review).

    `path`/`relation`/`count` each grew this guard (#284/#319/#321); the policy branch
    never did. `policy_result_line` filters the extent with a matcher that reads a bare
    token as a WILDCARD, so `needs_review("Alice")?` — a line the Errors section calls
    malformed — still rendered `needs_review results: N rows`, and the new header then
    promoted that line into `evaluated`. The report would say `errors: 3` and count the
    three errored questions as answered in the same breath: the same trap this branch
    closes for `unknown query predicate` one class up.
    """

    POLICY = {"needs_review"}
    FACTS = [{"subject": "Alice", "relation": "authored", "object": "P1"}]
    # Deliberately NON-EMPTY: an empty extent would render "0 rows" and hide the
    # defect behind a zero. With a real row the unguarded branch says "1 rows".
    INFERRED = {"needs_review": {("Alice", "low_conf")}, "path": set()}

    # bad arity, and a bare token the matcher reads as a wildcard — the two shapes
    # validate_query's policy branch errors on (tools/run_logic_check.py:269, 278).
    BAD = ['needs_review("Alice")?', "needs_review('Alice', R)?"]

    def _evaluate(self, monkeypatch, query):
        monkeypatch.setattr(rlc, "query_lines", lambda: [query])
        return rlc.evaluate_queries(self.FACTS, self.INFERRED, self.POLICY, hierarchy={})

    @pytest.mark.parametrize("query", BAD)
    def test_the_query_really_is_a_hard_error(self, query):
        """The premise: if these stopped erroring, the test below would prove nothing."""
        vocab = rlc.QueryVocabulary({"Alice"}, {"P1"}, {"authored"}, hierarchy={}, aliases={})
        errors, _warnings = rlc.validate_query(query, vocab, self.POLICY)
        assert errors, f"expected a hard error for {query!r}"

    @pytest.mark.parametrize("query", BAD)
    def test_no_row_count_is_rendered_for_it(self, monkeypatch, query):
        results = self._evaluate(monkeypatch, query)
        assert results == ["needs_review query malformed — see Errors above"], results
        assert not any("results:" in line for line in results), results

    @pytest.mark.parametrize("query", BAD)
    def test_the_header_counts_it_as_malformed_not_evaluated(self, monkeypatch, query):
        results = self._evaluate(monkeypatch, query)
        assert query_summary_line(results) == "queries: 1 (evaluated: 0, malformed: 1)"

    def test_a_well_formed_policy_query_still_reports_its_rows(self, monkeypatch):
        """The discriminator: the guard rejects SHAPES, never findings."""
        results = self._evaluate(monkeypatch, 'needs_review("Alice", R)?')
        assert results == ["needs_review results: 1 rows; Alice, R=low_conf"], results
        assert query_summary_line(results) == "queries: 1 (evaluated: 1)"

    def test_the_malformed_line_is_classified_by_predicate_not_by_a_fixed_list(self):
        """Policy predicates are named by the KB's own policy file, so the classifier
        cannot enumerate them. Matched against the three built-ins alone, a malformed
        policy query landed in `unclassified` — the drift bucket — instead of the
        bucket its own wording names."""
        line = query_summary_line(["any_policy_predicate query malformed — see Errors above"])
        assert line == "queries: 1 (evaluated: 0, malformed: 1)"


class TestUnicodeLineSeparatorsInValues:
    """A fact value may contain U+2028/U+2029/U+0085, and reading the report back with
    `str.splitlines()` split the item there (#535 review).

    compile_facts' control-character guard (#331) accepts those three deliberately, so
    they reach a rendered result line. Split on them, the item's second half does not
    start with `- `, status's section loop treated that as the end of the section, and
    EVERY item after the affected one vanished — while the header, counted from the
    in-memory list, still reported them. One file, two numbers, and the header was the
    one that looked authoritative.
    """

    ITEMS = [
        "relation results: 1 rows; A, quote, line one\u2028line two",
        "review_required: who decides?",
    ]

    def test_status_parses_back_every_item_the_header_counted(self):
        _seen, parsed = _query_evaluation_section(_report(self.ITEMS))
        assert parsed == self.ITEMS
        counts = classify_query_results(parsed)
        header = _header_counts(query_summary_line(self.ITEMS))
        assert header["total"] == len(parsed)
        assert header["review_required"] == counts["review_required"] == 1

    def test_the_item_after_the_separator_is_not_dropped(self):
        """The sharpest form: without the fix the review_required item is gone and
        status reports `1 without a result` for a question the report answered on."""
        _seen, parsed = _query_evaluation_section(_report(self.ITEMS))
        assert "review_required: who decides?" in parsed


class TestColonBearingEntityNames:
    """An entity whose name contains `": "` is answered, not `unclassified` (#535 review).

    Academic KBs are full of them (`BERT: Pre-training of Deep Bidirectional
    Transformers`). The pinned path item `path <start> -> <target>: <value>` splits on
    the first `": "`, so `path("A", "Fig: 3")?` — whose route the report drew exactly
    right — was counted as no answer at all. In #536 that only skewed a printed status
    line; the header writes the same false zero into a COMMITTED report artifact.
    """

    def test_the_answered_route_is_evaluated(self):
        assert query_summary_line(["path A -> Fig: 3: A -> Fig: 3"]) == "queries: 1 (evaluated: 1)"

    def test_a_multi_hop_route_through_a_colon_bearing_start(self):
        item = "path BERT: Pre-training -> C: BERT: Pre-training -> B -> C"
        assert query_summary_line([item]) == "queries: 1 (evaluated: 1)"

    def test_the_verified_negative_still_reads_as_an_answer(self):
        assert query_summary_line(["path A -> Fig: 3: (not found)"]) == "queries: 1 (evaluated: 1)"

    def test_an_unverified_endpoint_stays_unverified(self):
        item = (
            "path A -> Fig: 3: unverified — 'Fig: 3' is not accepted vocabulary "
            "(see Warnings above)"
        )
        assert query_summary_line([item]) == "queries: 1 (evaluated: 0, unverified: 1)"

    def test_a_value_that_is_not_a_route_is_still_unclassified(self):
        """The permissiveness is bounded: a split is accepted only when the value READS
        as a route between the two endpoints. ANSWERABLE stays a whitelist."""
        line = query_summary_line(["path A -> Fig: 3: some shape never seen before"])
        assert line == "queries: 1 (evaluated: 0, unclassified: 1)"


class TestTheClassifierIsNotACliDependency:
    """The bundled deterministic scripts do not reach into the CLI (#535 review).

    `tools/*` depends on `factlog.common`/`config`/`literal_types`/`md_lines`/
    `review_sections` — shared-vocabulary modules. Importing `factlog.cli`, a
    presentation layer, from `run_logic_check` would have made a step whose whole
    point is determinism depend on the layer above it, through an `_`-prefixed name
    carrying no stability contract. One classifier is right; the CLI is the wrong
    place to keep it.
    """

    def test_run_logic_check_does_not_import_the_cli(self):
        """Read as IMPORT STATEMENTS, not as text: the function-local
        `from factlog.cli import …` this replaces sits nowhere a `grep "^import"` would
        look, and a mention of the module in a comment is not a dependency."""
        import ast

        tree = ast.parse((REPO_ROOT / "tools" / "run_logic_check.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert "factlog.cli" not in imported, (
            f"the deterministic report depends on the CLI: {sorted(imported)}"
        )

    def test_both_readers_call_the_same_function_object(self):
        from factlog import common

        assert rlc.classify_query_results is common.classify_query_results
