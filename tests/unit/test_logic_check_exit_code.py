# SPDX-License-Identifier: Apache-2.0
"""run_logic_check exits non-zero when the report records errors (#283).

The freshness gate runs run_logic_check and, before this change, the tool
printed a report saying ``errors: N`` yet still exited 0 — so a broken query or
an incomplete fact row was written verbatim into the artifact but never stopped
the pipeline. main() now returns 1 when (and only when) ``errors`` is non-empty.

These are end-to-end pins driven through the real CLI: ``factlog init`` builds a
KB, ``compile_facts`` writes engine input, then ``run_logic_check`` writes the
report. That path runs wirelog, so without pyrewire no report exists to assert
on and the whole module skips. Warnings and policy findings are asserted NOT to
affect the exit code — only errors do.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pyrewire", reason="run_logic_check needs the engine to write a report")

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPILE = REPO_ROOT / "tools" / "compile_facts.py"
CHECK = REPO_ROOT / "tools" / "run_logic_check.py"
HEADER = "subject,relation,object,source,status,confidence,note"


def _env(kb: Path) -> dict[str, str]:
    env = dict(os.environ)
    # The tool scripts import their sibling ``common`` via sys.path[0] (the
    # script dir), but compile_facts imports ``factlog.common``, so the package
    # must be importable — put the repo root ahead of any editable install that
    # points elsewhere.
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    env["FACTLOG_ROOT"] = str(kb)
    return env


def _new_kb(tmp_path: Path, candidates_body: str, query_body: str) -> Path:
    kb = tmp_path / "wiki"
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        check=True,
        capture_output=True,
        env=_env(tmp_path),
    )
    (kb / "sources" / "a.md").write_text("a\n", encoding="utf-8")
    (kb / "facts" / "candidates.csv").write_text(
        f"{HEADER}\n{candidates_body}", encoding="utf-8"
    )
    (kb / "facts" / "query.dl").write_text(query_body, encoding="utf-8")
    return kb


def _run_check(kb: Path) -> subprocess.CompletedProcess[str]:
    subprocess.run(
        [sys.executable, str(COMPILE)],
        cwd=kb,
        check=True,
        capture_output=True,
        env=_env(kb),
    )
    return subprocess.run(
        [sys.executable, str(CHECK)],
        cwd=kb,
        capture_output=True,
        text=True,
        env=_env(kb),
    )


def _report_errors(kb: Path) -> int:
    for line in (kb / "facts" / "logic_report.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("errors:"):
            return int(line.split(":", 1)[1])
    raise AssertionError("logic_report.txt has no 'errors:' line")


class TestLogicCheckExitCode:
    def test_errors_exit_nonzero_and_report_records_them(self, tmp_path):
        # Three distinct error sources: an incomplete fact row, an arity-wrong
        # relation query, and an unknown query predicate.
        kb = _new_kb(
            tmp_path,
            candidates_body=(
                "PMID_1,uses,X,sources/a.md,accepted,0.90,\n"
                "PMID_2,uses,,sources/a.md,accepted,0.90,broken\n"
            ),
            query_body='relation("A", "uses")?\nfrobnicate("A")?\n',
        )
        result = _run_check(kb)
        assert result.returncode != 0, result.stdout
        assert _report_errors(kb) > 0

    def test_clean_kb_exits_zero(self, tmp_path):
        kb = _new_kb(
            tmp_path,
            candidates_body="PMID_1,uses,X,sources/a.md,accepted,0.90,\n",
            query_body='relation("PMID_1", "uses", "X")?\n',
        )
        result = _run_check(kb)
        assert result.returncode == 0, result.stdout
        assert _report_errors(kb) == 0

    def test_warnings_alone_do_not_fail_the_check(self, tmp_path):
        # A typo status is a warning, not an error: the report must count it
        # under warnings and still exit 0. Pins that the gate keys on errors
        # only, never on warnings or policy findings.
        kb = _new_kb(
            tmp_path,
            candidates_body="PMID_1,uses,X,sources/a.md,bogus_status,0.90,typo\n",
            query_body='relation("PMID_1", "uses", "X")?\n',
        )
        result = _run_check(kb)
        report = (kb / "facts" / "logic_report.txt").read_text(encoding="utf-8")
        assert "unknown status treated as non-engine input: bogus_status" in report
        assert result.returncode == 0, result.stdout
        assert _report_errors(kb) == 0
