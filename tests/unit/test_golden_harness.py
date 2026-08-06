# SPDX-License-Identifier: Apache-2.0
"""Contract pins for ``tests/golden.sh`` itself (#354).

The golden harness is cited as evidence ("golden passes, so this change is
safe"), so its own failure modes matter as much as the engine's. Two of them
were live:

1. ``PYTHON`` was assigned unconditionally, so ``PYTHON=<interpreter> bash
   tests/golden.sh`` — the convention the other 38 harnesses in ``tests/``
   follow — was silently discarded. On a machine without ``/tmp/factlog-venv``
   the run fell through to a bare ``python3`` that may not have pyrewire.
2. When a step died, the artifact it should have written stayed on disk as the
   committed copy, and the following diff compared *that* against the golden
   file. It passed no matter what the branch changed.

Both are exercised by running the real harness as a subprocess against a
throwaway copy of ``examples/sample-kb``, with ``PYTHON`` pointed at a shim that
fails on purpose. Each test also asserts the shim was actually reached, so a
harness that ignores ``PYTHON`` cannot make the test pass by accident.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_SH = REPO_ROOT / "tests" / "golden.sh"
SAMPLE_KB = REPO_ROOT / "examples" / "sample-kb"


def _write_shim(path: Path, body: str) -> Path:
    """Write an executable ``/bin/sh`` shim and return it.

    An exec wrapper, not a symlink: symlinking a venv interpreter loses
    ``pyvenv.cfg`` and with it site-packages, which would make every case fail
    for a reason unrelated to what it pins.
    """
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_golden(tmp_path: Path, python: Path) -> subprocess.CompletedProcess[str]:
    kb = tmp_path / "kb"
    shutil.copytree(SAMPLE_KB, kb)
    env = os.environ.copy()
    env["FACTLOG_ROOT"] = str(kb)
    env["PYTHON"] = str(python)
    # golden.sh redirects XDG_CONFIG_HOME itself; pin HOME too so no path in
    # the run can reach the developer's real ~/.config/factlog/config.json.
    env["HOME"] = str(tmp_path / "home")
    return subprocess.run(
        ["bash", str(GOLDEN_SH)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_honours_python_from_environment(tmp_path: Path) -> None:
    """``PYTHON=<interpreter>`` selects the interpreter, as in every other harness.

    The shim refuses every invocation, so a harness that honours ``PYTHON``
    cannot report a pass. Before the fix the assignment was unconditional and
    the run reported 5 passed / 0 failed under some other interpreter.
    """
    shim = _write_shim(tmp_path / "refuse-all", 'echo "shim: refused $*" >&2\nexit 3\n')

    result = _run_golden(tmp_path, shim)
    combined = result.stdout + result.stderr

    # golden.sh folds each step's stderr into stdout (`2>&1`), so the shim's
    # marker can land on either stream — read both.
    assert "shim: refused" in combined, (
        "the harness never invoked $PYTHON, so it is still ignoring the "
        f"caller's interpreter\n{combined}"
    )
    assert result.returncode != 0
    assert "0 failed" not in result.stdout


def test_dead_step_cannot_produce_a_vacuous_pass(tmp_path: Path) -> None:
    """A step that dies must not leave its golden comparison reporting PASS.

    The shim runs the real interpreter for every step except
    ``run_logic_check.py``, which it refuses — reproducing the "pyrewire is
    missing" failure four lanes hit. ``facts/logic_report.txt`` is therefore
    never rewritten, and before the fix the harness diffed the committed copy
    against the golden copy (identical by construction) and printed
    ``PASS: facts/logic_report.txt matches golden``.
    """
    shim = _write_shim(
        tmp_path / "refuse-logic-check",
        "case \"$*\" in\n"
        '  *run_logic_check.py*) echo "shim: refused run_logic_check" >&2; exit 1 ;;\n'
        "esac\n"
        f'exec "{sys.executable}" "$@"\n',
    )

    result = _run_golden(tmp_path, shim)
    combined = result.stdout + result.stderr

    assert "shim: refused run_logic_check" in combined, (
        "step 2 was not made to fail, so this case proves nothing\n" + combined
    )
    # Step 1 still runs for real: the case must isolate step 2, not break
    # everything, or the absent PASS below would be uninformative.
    assert "PASS: facts/accepted.dl matches golden" in result.stdout, combined
    assert not re.search(r"^PASS: facts/logic_report\.txt", result.stdout, re.M), (
        "the logic_report comparison passed while step 2 was dead — it "
        f"compared the committed file, not this run's output\n{combined}"
    )
    assert result.returncode != 0
