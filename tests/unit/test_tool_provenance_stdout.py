# SPDX-License-Identifier: Apache-2.0
"""``compile_facts`` / ``merge_candidates`` / ``source_coverage`` name their factlog (#554).

The three write/report steps print a ``target KB`` line so a reader knows which KB
they acted on; none of them said which factlog did the acting. This pins the added
line on all three, and — the part that is easy to get wrong — pins that it did NOT
leak into ``--help``.

That second half is a standing contract, not a nicety.
``tests/unit/test_unaimed_engine_step_guard.py`` fixed it for the KB announcement:
``--help`` and a rejected argument must produce pure argparse output, with no
execution context in stdout. A provenance line printed before the strict parse would
break it for every tool at once, and ``--help`` is what a script parses when it is
probing for a flag.

Subprocesses throughout: which package a *process* imported cannot be observed from
inside the test runner's own import.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HEADER = "subject,relation,object,source,status,confidence,note"

TOOLS = {
    "compile_facts": REPO_ROOT / "tools" / "compile_facts.py",
    "merge_candidates": REPO_ROOT / "tools" / "merge_candidates.py",
    "source_coverage": REPO_ROOT / "tools" / "source_coverage.py",
}


def _env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["FACTLOG_ROOT"] = str(root)
    return env


def _expected_line(cwd: Path) -> str:
    """The line as this repo's factlog would render it, measured not hardcoded."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import factlog; print(f'factlog: {factlog.__version__} ({factlog.__file__})')",
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env=_env(cwd),
    )
    return proc.stdout.strip()


def _kb(tmp_path: Path) -> Path:
    kb = tmp_path / "kb"
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        check=True,
        capture_output=True,
        env=_env(tmp_path),
    )
    (kb / "sources" / "a.md").write_text("a\n", encoding="utf-8")
    (kb / "facts" / "candidates.csv").write_text(
        f"{HEADER}\nA,uses,B,sources/a.md,confirmed,0.90,\n", encoding="utf-8"
    )
    return kb


def _run(tool: Path, kb: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(tool), *args],
        cwd=str(kb),
        capture_output=True,
        text=True,
        env=_env(kb),
    )


class TestToolStdoutNamesTheRunningFactlog:
    def test_compile_facts_announces_before_the_target_kb(self, tmp_path):
        kb = _kb(tmp_path)
        result = _run(TOOLS["compile_facts"], kb)
        assert result.returncode == 0, result.stdout + result.stderr
        lines = result.stdout.splitlines()
        # Whole-line equality, and position: which code is running explains a
        # surprising target far more often than the reverse, so it comes first.
        assert lines[0] == _expected_line(tmp_path)
        assert lines[1].startswith("compile_facts: target KB ")

    def test_merge_candidates_announces_before_the_target_kb(self, tmp_path):
        kb = _kb(tmp_path)
        # runs/*.json is candidates.csv's source of truth: an empty runs/ makes the
        # tool refuse the rebuild (#218), which would exercise the refusal path
        # instead of the announcement.
        (kb / "runs").mkdir(exist_ok=True)
        (kb / "runs" / "facts-001.json").write_text(
            json.dumps(
                [
                    {
                        "subject": "A",
                        "relation": "uses",
                        "object": "B",
                        "source": "sources/a.md",
                        "status": "confirmed",
                        "confidence": 0.9,
                        "note": "",
                    }
                ]
            ),
            encoding="utf-8",
        )
        result = _run(TOOLS["merge_candidates"], kb)
        assert result.returncode == 0, result.stdout + result.stderr
        lines = result.stdout.splitlines()
        assert lines[0] == _expected_line(tmp_path)
        assert lines[1].startswith("merge_candidates: target KB ")

    def test_source_coverage_announces_before_its_first_figure(self, tmp_path):
        kb = _kb(tmp_path)
        result = _run(TOOLS["source_coverage"], kb)
        assert result.returncode == 0, result.stdout + result.stderr
        lines = result.stdout.splitlines()
        assert lines[0] == _expected_line(tmp_path)
        # It has no `target KB` line of its own; what matters is that the identity
        # precedes the coverage figures it is meant to attribute.
        assert any(ln.startswith("coverage: ") for ln in lines[1:])


class TestHelpStaysPureArgparse:
    """``--help`` must not leak execution context — the contract #547 pinned.

    A tool's ``--help`` is parsed by scripts and read by people probing for a flag;
    printing where the code lives (and, for the sibling KB line, which KB is active)
    turns a usage message into a machine-specific artifact. Everything added by #554
    therefore sits AFTER the strict parse.
    """

    def test_help_stdout_carries_no_provenance_line(self, tmp_path):
        kb = _kb(tmp_path)
        for name, tool in TOOLS.items():
            result = _run(tool, kb, "--help")
            assert result.returncode == 0, f"{name}: {result.stdout}{result.stderr}"
            assert result.stdout.startswith("usage:"), name
            assert "factlog: " not in result.stdout, f"{name} leaked provenance into --help"
            # The sibling contract, re-measured here so a regression in either is
            # caught by whichever test runs first.
            assert "target KB" not in result.stdout, name

    def test_a_rejected_argument_carries_no_provenance_line(self, tmp_path):
        kb = _kb(tmp_path)
        for name, tool in TOOLS.items():
            result = _run(tool, kb, "--targt", str(kb))
            assert result.returncode == 2, f"{name}: expected argparse exit 2"
            assert "factlog: " not in result.stdout, name
            assert "factlog: " not in result.stderr, name
