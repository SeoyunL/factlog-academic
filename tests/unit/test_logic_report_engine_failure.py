# SPDX-License-Identifier: Apache-2.0
"""facts/logic_report.txt must record a run the engine could not complete (#338).

The tool used to write the report only after ``run_wirelog`` returned, so an
engine that could not start left the file untouched. Whatever report was already
there stayed on disk and read as this run's result — the report is not
timestamped in its own text, and neither ``/factlog check``'s output nor the
freshness gate could tell the two apart.

Every case here runs the REAL tool as a subprocess and asserts on the file it
leaves behind. That matters more than usual: the bug is about a file NOT being
written, and any test that builds the report by calling a helper directly would
be asserting about a code path the failing run never reaches.

Two failure causes are covered, because they fail through different exception
types and only one of them is a FactlogError:

- ``facts/accepted.dl`` absent -> ``FactlogError`` raised by ``run_wirelog``;
- the engine package unimportable -> also a ``FactlogError``, from
  ``require_pyrewire_version``, but reached without touching the KB at all. This
  is the "broken engine environment" the issue describes, and the case where no
  amount of fixing the KB helps.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "tools" / "run_logic_check.py"
SAMPLE_KB = REPO_ROOT / "examples" / "sample-kb"

MARKER = "status: engine-did-not-run"


def _kb(tmp_path: Path) -> Path:
    """A KB with facts already compiled.

    ``examples/sample-kb`` ships with ``facts/accepted.dl`` compiled, which is
    what the engine reads; ``finalize`` rebuilds candidates.csv from runs/, so
    hand-writing candidates would not put facts in front of the engine.
    """
    kb = tmp_path / "kb"
    shutil.copytree(SAMPLE_KB, kb)
    return kb


def _run(kb: Path, extra_pythonpath: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if extra_pythonpath is not None:
        env["PYTHONPATH"] = os.pathsep.join(
            [str(extra_pythonpath), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, str(TOOL), "--wiki", str(kb)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )


def _report(kb: Path) -> Path:
    return kb / "facts" / "logic_report.txt"


def _break_engine_import(tmp_path: Path) -> Path:
    """A sys.path entry whose ``pyrewire`` cannot be imported.

    ``common`` guards its ``import pyrewire`` with ``except ImportError`` and
    turns the miss into a FactlogError at call time, so this reproduces an
    uninstalled/broken engine without uninstalling anything.
    """
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "pyrewire.py").write_text(
        'raise ImportError("simulated broken engine install")\n', encoding="utf-8"
    )
    return shim


class TestReportIsWrittenWhenTheEngineCannotRun:
    def test_missing_accepted_dl_still_writes_a_report(self, tmp_path):
        kb = _kb(tmp_path)
        (kb / "facts" / "accepted.dl").unlink()
        # The report from the copied KB is deleted first, so the file this test
        # finds can only have been written by THIS run. Without that, the
        # shipped report passes every assertion below except the marker.
        _report(kb).unlink()

        result = _run(kb)

        assert result.returncode != 0, result.stdout
        assert _report(kb).is_file(), (
            f"no report written; stderr={result.stderr!r}"
        )
        assert MARKER in _report(kb).read_text(encoding="utf-8")

    def test_unimportable_engine_still_writes_a_report(self, tmp_path):
        kb = _kb(tmp_path)
        _report(kb).unlink()

        result = _run(kb, extra_pythonpath=_break_engine_import(tmp_path))

        assert result.returncode != 0, result.stdout
        assert _report(kb).is_file(), (
            f"no report written; stderr={result.stderr!r}"
        )
        assert MARKER in _report(kb).read_text(encoding="utf-8")


class TestTheFailureReportDoesNotReadAsAResult:
    """The distinction the report has to carry: "the engine could not run" is
    not "the engine ran and found nothing"."""

    def test_previous_report_is_replaced_not_left_standing(self, tmp_path):
        # Deliberately NOT deleted: a successful report is left in place, which
        # is the state the bug produced — yesterday's answer presented as
        # today's.
        kb = _kb(tmp_path)
        before = _report(kb).read_text(encoding="utf-8")
        assert "engine facts: 7" in before  # the shipped report is a real result
        (kb / "facts" / "accepted.dl").unlink()

        result = _run(kb)

        after = _report(kb).read_text(encoding="utf-8")
        assert result.returncode != 0
        assert after != before
        assert "engine facts: 7" not in after

    def test_counts_are_absent_rather_than_zero(self, tmp_path):
        kb = _kb(tmp_path)
        _report(kb).unlink()
        (kb / "facts" / "accepted.dl").unlink()

        _run(kb)
        text = _report(kb).read_text(encoding="utf-8")

        # "engine facts: 0" would be a claim that the engine ran over an empty
        # KB. Nothing may render as a count.
        for field in ("engine facts:", "policy findings:", "errors:", "warnings:"):
            assert field not in text, f"{field!r} states a result the run never obtained"

    def test_report_names_the_cause(self, tmp_path):
        kb = _kb(tmp_path)
        _report(kb).unlink()
        (kb / "facts" / "accepted.dl").unlink()

        _run(kb)
        text = _report(kb).read_text(encoding="utf-8")

        assert "reason: missing facts/accepted.dl" in text

    def test_marker_is_a_whole_line(self, tmp_path):
        """The gate matches it with ``grep -qxF``; a marker buried in prose or
        given a trailing comment would silently stop being recognised."""
        kb = _kb(tmp_path)
        _report(kb).unlink()
        (kb / "facts" / "accepted.dl").unlink()

        _run(kb)

        assert MARKER in _report(kb).read_text(encoding="utf-8").splitlines()


class TestFailingStillFails:
    """Guards, not evidence: these hold before the fix too. They are what stops
    the fix from turning a failed check into a passing one."""

    def test_exit_code_and_stderr_are_unchanged(self, tmp_path):
        kb = _kb(tmp_path)
        (kb / "facts" / "accepted.dl").unlink()

        result = _run(kb)

        assert result.returncode == 1
        assert "missing facts/accepted.dl" in result.stderr

    def test_no_report_outside_a_kb_root(self, tmp_path):
        """``ensure_dirs`` fails before the engine is in the picture, and "this
        is not a factlog KB" is not a statement about the engine. Writing a
        report here would also mean creating the facts/ directory the check just
        refused to accept."""
        not_a_kb = tmp_path / "not-a-kb"
        not_a_kb.mkdir()

        result = _run(not_a_kb)

        assert result.returncode == 1
        assert not (not_a_kb / "facts").exists()


class TestSuccessPathUnchanged:
    def test_successful_report_carries_no_failure_marker(self, tmp_path):
        kb = _kb(tmp_path)
        _report(kb).unlink()

        result = _run(kb)

        assert result.returncode == 0, result.stderr
        text = _report(kb).read_text(encoding="utf-8")
        assert MARKER not in text
        assert "engine facts: 7" in text

    def test_successful_report_still_matches_the_golden_file(self, tmp_path):
        golden = REPO_ROOT / "tests" / "golden" / "logic_report.txt"
        if not golden.is_file():  # pragma: no cover - the file is committed
            pytest.skip("golden report not present")
        kb = _kb(tmp_path)
        _report(kb).unlink()

        _run(kb)

        assert _report(kb).read_text(encoding="utf-8") == golden.read_text(encoding="utf-8")
