# SPDX-License-Identifier: Apache-2.0
"""The logic report names the factlog that produced it (#554).

``facts/logic_report.txt`` is the artifact the determinism gate makes you show
verbatim as evidence — and it could not identify its own producer. It named the
engine (``engine: wirelog / pyrewire``), the input and the policy, but nothing
about the factlog code that assembled it. With a bundled 0.6.0 and a working tree
0.7.0 both present (the ordinary state of a plugin that ships its own ``tools/``),
the same KB yields reports no reader can tell apart after the fact, and four
already-closed defects (#208, #491, #527, #547) were re-diagnosed as live bugs
from exactly such artifacts.

The load-bearing test in this file is
``test_two_trees_produce_two_different_lines``: it runs the SAME report twice with
``PYTHONPATH`` fronting two different factlog trees and asserts the line differs.
That is the issue's own verification method — "check that bundled and editable runs
point the line at different versions/paths" — reproduced rather than asserted.

Everything runs in subprocesses, because the whole question is which package a
*process* imported; an in-process assertion would only ever describe the test
runner's own import.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pyrewire", reason="run_logic_check needs the engine to write a report")

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPILE = REPO_ROOT / "tools" / "compile_facts.py"
CHECK = REPO_ROOT / "tools" / "run_logic_check.py"
HEADER = "subject,relation,object,source,status,confidence,note"
CANDIDATES = "A,uses,B,sources/a.md,confirmed,0.90,\n"
QUERY = 'relation("A", "uses", "B")?\n'


def _env(root: Path, package_root: Path = REPO_ROOT) -> dict[str, str]:
    """Environment for a tool run, with *package_root* fronting ``factlog``.

    Same shape as ``test_report_policy_provenance._env``: tools import their sibling
    ``common`` off ``sys.path[0]``, but the package import must be pinned or an
    editable install pointing elsewhere decides it — which is the very confusion
    under test here, so it is made an explicit parameter instead of an accident.

    Both roots are listed, *package_root* first, and the inherited ``PYTHONPATH`` is
    dropped rather than appended. Two reasons, both measured:

    * ``tools/factlog_config.py`` inserts the repo root at ``sys.path[0]`` *only if it
      is not already on the path*. Naming it explicitly (behind ``package_root``) makes
      the winner a property of this environment instead of a property of whatever
      ``PYTHONPATH`` the developer happened to export before running pytest.
    * the repo root must stay reachable regardless, since the tools import
      ``factlog.common`` and friends by package name.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(package_root), str(REPO_ROOT)])
    env["FACTLOG_ROOT"] = str(root)
    return env


def _new_kb(tmp_path: Path, name: str) -> Path:
    kb = tmp_path / name
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        check=True,
        capture_output=True,
        env=_env(tmp_path),
    )
    (kb / "sources" / "a.md").write_text("a\n", encoding="utf-8")
    (kb / "facts" / "candidates.csv").write_text(f"{HEADER}\n{CANDIDATES}", encoding="utf-8")
    (kb / "facts" / "query.dl").write_text(QUERY, encoding="utf-8")
    return kb


def _run_check(
    kb: Path, package_root: Path = REPO_ROOT, tools_root: Path = REPO_ROOT
) -> subprocess.CompletedProcess[str]:
    env = _env(kb, package_root)
    subprocess.run(
        [sys.executable, str(tools_root / "tools" / "compile_facts.py")],
        cwd=kb,
        check=True,
        capture_output=True,
        env=env,
    )
    return subprocess.run(
        [sys.executable, str(tools_root / "tools" / "run_logic_check.py")],
        cwd=kb,
        capture_output=True,
        text=True,
        env=env,
    )


def _report(kb: Path) -> str:
    return (kb / "facts" / "logic_report.txt").read_text(encoding="utf-8")


def _factlog_line(report: str) -> str:
    """The single ``factlog: `` line, whole.

    Exactly one, asserted here rather than at each call site: two producers named
    in one artifact is the same unanswerable question the missing line was.
    """
    matches = [line for line in report.splitlines() if line.startswith("factlog: ")]
    assert len(matches) == 1, f"expected exactly one 'factlog: ' line, got {matches!r}"
    return matches[0]


def _measure(package_root: Path, cwd: Path) -> str:
    """What that tree's factlog answers when asked directly — the reader's own check.

    Run from a *cwd holding no ``factlog/`` directory*, because ``python -c`` puts the
    working directory at ``sys.path[0]``: measuring from the repo root would answer
    "the repo root" no matter what ``PYTHONPATH`` says, which is how the first version
    of this test managed to compare a tree against itself.
    """
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
        env=_env(cwd, package_root),
    )
    return proc.stdout.strip()


class TestReportNamesItsProducer:
    def test_report_carries_one_factlog_line_matching_the_running_package(self, tmp_path):
        kb = _new_kb(tmp_path, "kb")
        result = _run_check(kb)
        assert result.returncode == 0, result.stdout + result.stderr

        # Whole-line equality against an independent measurement of the SAME tree the
        # subprocess imported. A substring check would pass on a line that dropped the
        # path, which is the half that distinguishes two copies of one release.
        assert _factlog_line(_report(kb)) == _measure(REPO_ROOT, tmp_path)

    def test_the_line_sits_directly_under_the_rule_and_above_the_engine(self, tmp_path):
        # Position is part of the contract: the producer's identity belongs in the
        # header block beside the engine's, not buried in the body where a reader
        # skimming the first screen would miss it.
        kb = _new_kb(tmp_path, "kb_pos")
        assert _run_check(kb).returncode == 0
        lines = _report(kb).splitlines()
        assert lines[0] == "Logic Check Report"
        assert lines[1] == "=================="
        assert lines[2].startswith("factlog: ")
        assert lines[3] == "engine: wirelog / pyrewire"

    def test_stdout_names_the_same_factlog_before_the_target_kb(self, tmp_path):
        kb = _new_kb(tmp_path, "kb_stdout")
        result = _run_check(kb)
        assert result.returncode == 0, result.stdout + result.stderr
        out = result.stdout.splitlines()
        assert out[0] == _measure(REPO_ROOT, tmp_path)
        assert out[1].startswith("run_logic_check: target KB ")

    def test_target_kb_stays_out_of_the_report(self, tmp_path):
        """The line that was added is not a licence for the one that was not.

        ``target KB`` names an input LOCATION the reader already knows — they opened
        the file there — and putting it in the artifact would bake a developer's home
        directory into a byte-compared golden. ``factlog:`` names the PRODUCER, the
        same category as ``engine:``. ``test_unaimed_engine_step_guard.py`` pins the
        first half; this pins that #554 did not quietly undo it.
        """
        kb = _new_kb(tmp_path, "kb_no_target")
        assert _run_check(kb).returncode == 0
        assert "target KB" not in _report(kb)


def _second_tree(tmp_path: Path, *, with_tools: bool) -> Path:
    """A real, runnable copy of factlog at a made-up version.

    A copy rather than a stub: the point is that two *working* installations write
    distinguishable reports, which a fake package could not demonstrate. The version
    is prefixed rather than replaced so the copy is unmistakable in a diff and can
    never collide with a real release number.
    """
    other_root = tmp_path / ("bundled-copy" if with_tools else "other-tree")
    shutil.copytree(REPO_ROOT / "factlog", other_root / "factlog")
    if with_tools:
        shutil.copytree(REPO_ROOT / "tools", other_root / "tools")
    init = other_root / "factlog" / "__init__.py"
    init.write_text(
        init.read_text(encoding="utf-8").replace('__version__ = "', '__version__ = "9.9.9+'),
        encoding="utf-8",
    )
    return other_root


def _without_provenance(kb: Path) -> list[str]:
    return [ln for ln in _report(kb).splitlines() if not ln.startswith("factlog: ")]


class TestTwoTreesAreDistinguishable:
    """The verification #554 asks for, run rather than argued.

    "Check that the bundled run and the editable run point the line at different
    versions and paths" is the issue's own acceptance criterion, and it is the only
    thing that proves the line carries information rather than a constant. Both of
    the field shapes are exercised: a foreign package fronted on ``PYTHONPATH``, and
    a wholly bundled copy running its own ``tools/``.
    """

    def test_two_trees_produce_two_different_lines(self, tmp_path):
        """Same repo tools, different factlog package fronted on ``PYTHONPATH``.

        This is the whole issue in one assertion. Before the fix these two runs
        produced BYTE-IDENTICAL reports: same KB, same engine, different code, no way
        to tell afterwards which one you were holding.
        """
        other_root = _second_tree(tmp_path, with_tools=False)

        kb_here = _new_kb(tmp_path, "kb_here")
        kb_there = _new_kb(tmp_path, "kb_there")
        assert _run_check(kb_here, REPO_ROOT).returncode == 0
        assert _run_check(kb_there, other_root).returncode == 0

        line_here = _factlog_line(_report(kb_here))
        line_there = _factlog_line(_report(kb_there))

        assert line_here != line_there
        # Both halves move, and each is checked against that tree's own measurement —
        # not against a literal, which would only pin this machine.
        assert line_here == _measure(REPO_ROOT, tmp_path)
        assert line_there == _measure(other_root, tmp_path)
        assert "9.9.9+" in line_there and "9.9.9+" not in line_here
        assert str(other_root) in line_there

        # And the reports are otherwise identical: the new line is the ONLY thing that
        # tells them apart, so it is carrying the whole distinction — which is both the
        # proof it works and the proof it changed nothing else.
        assert _without_provenance(kb_here) == _without_provenance(kb_there)

    def test_a_bundled_copy_running_its_own_tools_names_itself(self, tmp_path):
        """The #553 shape: bundled ``tools/`` beside a bundled package.

        A plugin ships its own ``tools/``, and ``tools/factlog_config.py`` puts its own
        parent directory on ``sys.path`` — so a bundled run imports the bundled package
        even when a working tree is installed. That is precisely the skew this line
        exists to make visible, so it is measured with the real bundle layout and not
        only with an environment variable.
        """
        bundle = _second_tree(tmp_path, with_tools=True)
        kb = _new_kb(tmp_path, "kb_bundled")

        result = _run_check(kb, package_root=bundle, tools_root=bundle)

        assert result.returncode == 0, result.stdout + result.stderr
        line = _factlog_line(_report(kb))
        assert line == _measure(bundle, tmp_path)
        assert str(bundle / "factlog" / "__init__.py") in line
        assert str(REPO_ROOT / "factlog" / "__init__.py") not in line
