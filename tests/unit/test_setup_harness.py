# SPDX-License-Identifier: Apache-2.0
"""Contract pins for ``tests/setup.sh`` itself (#361).

``PYTHON`` was assigned unconditionally, so ``PYTHON=<interpreter> bash
tests/setup.sh`` — the convention the rest of ``tests/`` follows — was silently
discarded, exactly as ``tests/golden.sh`` discarded it before #354. On a machine
without ``/tmp/factlog-venv`` a run pointed at an interpreter WITH pyrewire fell
through to a bare ``python3`` without it and reported ``0 passed, 9 failed``.

The misattribution runs the other way too, which is why this file exists rather
than the fix alone: a ``9 passed, 0 failed`` on such a machine was explained by
"because ``PYTHON`` was exported", a mechanism that did not exist — the run had
picked up a good ``python3`` from ``PATH``. Nothing in the harness output names
the interpreter it chose, so neither verdict could be checked by reading it.

Three cases run the real harness as a subprocess and one reads its source:

1. ``PYTHON`` is honoured. The shim refuses every invocation and the case
   asserts the shim was reached, so a harness that ignores ``PYTHON`` cannot
   pass by accident.
2. A named interpreter that cannot run stops the harness with a FATAL line
   instead of falling back to ``python3``. A silent fallback would measure a
   different interpreter and report the result as if it were about the tree
   under test — the defect above with an extra step.
3. The interpreter choice when ``PYTHON`` is unset is a source read, not a run.
   Exercising it would mean creating ``/tmp/factlog-venv``, a path shared with
   every other checkout on the machine, which a test must not do — the same
   exception ``test_golden_harness`` records for the same reason. Unlike
   golden.sh, this harness keeps that branch: it is a dev-environment script and
   the branch only runs when the caller named nothing, so the pin is that the
   preference survives *behind* the caller's value rather than ahead of it.

Case 1 runs Step 1, which begins ``rm -rf /tmp/factlog-setup-test-kb``. That
path is the harness's own scratch KB — ``tests/setup.sh`` is the only file in
the repo that names it — so running the harness deletes only output the harness
itself produced.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SH = REPO_ROOT / "tests" / "setup.sh"


def _write_shim(path: Path, body: str) -> Path:
    """Write an executable ``/bin/sh`` shim and return it.

    An exec wrapper, not a symlink: symlinking a venv interpreter loses
    ``pyvenv.cfg`` and with it site-packages.
    """
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_setup(tmp_path: Path, python: Path | str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHON"] = str(python)
    # setup.sh redirects XDG_CONFIG_HOME itself; pin HOME too so no path in the
    # run can reach the developer's real ~/.config/factlog/config.json (#62).
    env["HOME"] = str(tmp_path / "home")
    return subprocess.run(
        ["bash", str(SETUP_SH)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _code_lines(source: str) -> list[str]:
    return [line for line in source.splitlines() if not line.lstrip().startswith("#")]


def test_honours_python_from_environment(tmp_path: Path) -> None:
    """``PYTHON=<interpreter>`` selects the interpreter, as in every other harness.

    The shim refuses every invocation, so a harness that honours ``PYTHON``
    cannot report a pass. Before the fix the assignment was unconditional and the
    run reported its verdict under whatever ``/tmp/factlog-venv`` or ``python3``
    resolved to.
    """
    shim = _write_shim(tmp_path / "refuse-all", 'echo "shim: refused $*" >&2\nexit 3\n')

    result = _run_setup(tmp_path, shim)
    combined = result.stdout + result.stderr

    assert "shim: refused" in combined, (
        "the harness never invoked $PYTHON, so it is still ignoring the "
        f"caller's interpreter\n{combined}"
    )
    assert result.returncode != 0
    assert "0 failed" not in result.stdout


@pytest.mark.parametrize("kind", ["missing", "not-executable"])
def test_unusable_python_stops_the_run(tmp_path: Path, kind: str) -> None:
    """A named interpreter that cannot run must be fatal, never a fallback.

    Falling back to ``python3`` here would run the whole harness under an
    interpreter the caller did not choose and print a verdict that reads as one
    about ``factlog setup``. The run must stop before Step 1 instead, which is
    what the absent ``Setup results:`` line below checks.
    """
    if kind == "missing":
        python: Path | str = tmp_path / "no-such-python"
    else:
        python = tmp_path / "not-executable"
        Path(python).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        Path(python).chmod(0o644)

    result = _run_setup(tmp_path, python)
    combined = result.stdout + result.stderr

    assert result.returncode != 0, combined
    assert "FATAL: PYTHON is not an executable interpreter" in result.stderr, (
        "an unusable interpreter did not stop the harness\n" + combined
    )
    assert "Setup results:" not in result.stdout, (
        "the harness ran its steps anyway, under some other interpreter\n"
        + combined
    )


def test_venv_preference_sits_behind_the_callers_value() -> None:
    """The ``/tmp/factlog-venv`` preference must apply only when ``PYTHON`` is unset.

    Behavioural coverage stops at the two cases above; this guards the unset
    branch, which cannot be run without creating ``/tmp/factlog-venv`` (see the
    module docstring). Both halves are real: the branch was reached
    unconditionally, and dropping it instead of moving it behind the guard would
    change what an unset run selects.
    """
    code = _code_lines(SETUP_SH.read_text(encoding="utf-8"))
    guards = [i for i, line in enumerate(code) if 'if [ -z "${PYTHON:-}" ]; then' in line]
    venv = [i for i, line in enumerate(code) if "/tmp/factlog-venv/bin/python" in line]
    fallback = [i for i, line in enumerate(code) if 'PYTHON="python3"' in line]

    assert guards, "setup.sh no longer defers to a caller-supplied PYTHON"
    assert venv, (
        "setup.sh dropped the /tmp/factlog-venv preference; an unset run now "
        "selects a different interpreter than it used to"
    )
    assert fallback, "setup.sh lost its python3 fallback"
    assert min(venv) > guards[0] and min(fallback) > guards[0], (
        "the interpreter preference is assigned before the unset guard, so it "
        "overwrites the caller's PYTHON again"
    )
