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
POLICY_KB = REPO_ROOT / "tests" / "golden-kb"

# The artifacts the harness regenerates, in both KBs. Tracked files: running a
# test must not write to any of them.
TRACKED_ARTIFACTS = [
    SAMPLE_KB / "facts" / "accepted.dl",
    SAMPLE_KB / "facts" / "logic_report.txt",
    POLICY_KB / "facts" / "accepted.dl",
    POLICY_KB / "facts" / "logic_report.txt",
]


def _write_shim(path: Path, body: str) -> Path:
    """Write an executable ``/bin/sh`` shim and return it.

    An exec wrapper, not a symlink: symlinking a venv interpreter loses
    ``pyvenv.cfg`` and with it site-packages, which would make every case fail
    for a reason unrelated to what it pins.
    """
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_golden_at(
    kb: Path, tmp_path: Path, python: Path
) -> subprocess.CompletedProcess[str]:
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


def _run_golden(tmp_path: Path, python: Path) -> subprocess.CompletedProcess[str]:
    kb = tmp_path / "kb"
    shutil.copytree(SAMPLE_KB, kb)
    return _run_golden_at(kb, tmp_path, python)


def _fingerprint(path: Path) -> tuple[int, int, bytes]:
    """Identity, write time and contents — enough to see a rewrite that happens
    to reproduce the same bytes. ``_atomic_write_text`` is temp-file + os.replace,
    so a rewrite always lands a new inode even when the contents are unchanged."""
    stat = path.stat()
    return (stat.st_ino, stat.st_mtime_ns, path.read_bytes())


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
    # Anchored at both ends: the harness also reports on the COMMITTED copy of
    # this file, which is a different and legitimately-true claim. Matching it
    # here would make this case fail for the wrong reason.
    assert not re.search(
        r"^PASS: facts/logic_report\.txt matches golden$", result.stdout, re.M
    ), (
        "the logic_report comparison passed while step 2 was dead — it "
        f"compared the committed file, not this run's output\n{combined}"
    )
    assert result.returncode != 0


def test_run_writes_nothing_inside_the_checkout(tmp_path: Path) -> None:
    """A green run must leave every tracked KB artifact byte- AND inode-identical.

    The harness regenerates ``facts/accepted.dl`` and ``facts/logic_report.txt``
    for each KB. It used to do that in place: KB 1 wherever the caller pointed —
    ``examples/sample-kb`` for the documented local invocation — and KB 2 at the
    fixed in-repo path ``tests/golden-kb``, which no caller can redirect. So the
    "pure-function unit layer" rewrote tracked fixtures, and a tool that emitted
    different bytes left a mutated fixture staged by the next ``git add -A``.

    Contents alone would not see this: a green run reproduces the same bytes. The
    inode does move, because the writer is temp-file + ``os.replace``.
    """
    before = {path: _fingerprint(path) for path in TRACKED_ARTIFACTS}

    result = _run_golden_at(SAMPLE_KB, tmp_path, Path(sys.executable))

    assert result.returncode == 0, (
        "this case pins where a GREEN run writes; it did not go green\n"
        f"{result.stdout}\n{result.stderr}"
    )
    changed = [
        str(path.relative_to(REPO_ROOT))
        for path in TRACKED_ARTIFACTS
        if _fingerprint(path) != before[path]
    ]
    assert not changed, (
        "the harness wrote to tracked files in the checkout: " + ", ".join(changed)
    )


def test_dead_conflicts_step_is_not_read_as_detection(tmp_path: Path) -> None:
    """Step 4 must tell "found a contradiction" apart from "could not run".

    ``check_conflicts.py`` exits 1 when it finds a conflict, so the fixture KB —
    which declares one on purpose — makes exit 1 the expected result. But a tool
    that cannot start exits non-zero too, and keying the verdict on the code
    alone reported ``PASS: check_conflicts.py exit 1 (the declared contradiction
    is detected)`` for an interpreter that did not exist. That is the same
    vacuous-PASS defect this issue exists to remove, so it gets its own pin.

    The shim runs the real interpreter for every step except ``check_conflicts``,
    which it refuses with exit 1 and no findings.
    """
    shim = _write_shim(
        tmp_path / "refuse-conflicts",
        "case \"$*\" in\n"
        '  *check_conflicts.py*) echo "shim: refused check_conflicts" >&2; exit 1 ;;\n'
        "esac\n"
        f'exec "{sys.executable}" "$@"\n',
    )

    result = _run_golden(tmp_path, shim)
    combined = result.stdout + result.stderr

    assert "shim: refused check_conflicts" in combined, (
        "step 4 was not made to fail, so this case proves nothing\n" + combined
    )
    # The earlier steps still run for real, so an absent PASS below is about
    # step 4's verdict and not about a harness that fell over.
    assert "PASS: facts/logic_report.txt matches golden" in result.stdout, combined
    assert not re.search(r"^PASS: check_conflicts\.py exit 1", result.stdout, re.M), (
        "the harness read a dead check_conflicts as a detected contradiction\n"
        + combined
    )
    assert result.returncode != 0
