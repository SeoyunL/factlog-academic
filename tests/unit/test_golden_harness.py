# SPDX-License-Identifier: Apache-2.0
"""Contract pins for ``tests/golden.sh`` itself (#354).

The golden harness is cited as evidence ("golden passes, so this change is
safe"), so its own failure modes matter as much as the engine's. Two families
were live.

Claims the output made but did not hold:

1. ``PYTHON`` was assigned unconditionally, so ``PYTHON=<interpreter> bash
   tests/golden.sh`` — the convention 39 of the 42 harnesses in ``tests/``
   follow — was silently discarded. On a machine without ``/tmp/factlog-venv``
   the run fell through to a bare ``python3`` that may not have pyrewire. The
   first fix kept that venv as an implicit fallback, which left this harness
   choosing a different interpreter from every other one on a machine that has
   the path, so the unset branch is pinned too.
2. When a step died, the artifact it should have written stayed on disk as the
   committed copy, and the following diff compared *that* against the golden
   file. It passed no matter what the branch changed.
3. Step 4 keyed "the declared contradiction is detected" on exit code 1 alone,
   which a tool that cannot start also returns.

Writes that escaped the scratch copy — running a test must not touch the
checkout, or anything else the caller owns:

4. Both KBs were regenerated in place, so the unit layer rewrote tracked
   fixtures.
5. ``cp -R`` dereferences neither a symlinked source operand nor an inner
   symlink, so a "copy" of a symlinked KB wrote back into the original — and the
   ``rm -f`` that makes a dead step fail honestly deleted the caller's real file.

Two further cases guard modes the remedy for 5 introduced rather than ones that
were live. ``cp -RL`` cannot follow a cyclic symlink: it skips the subtree and
carries on, so the copy the run would measure is silently incomplete, and the
harness's stop on a failed copy is pinned. ``cp`` also copies modes, so a
read-only source KB produced a read-only copy and the run died mid-step with no
verdict line at all.

Every case runs the real harness as a subprocess, most against a throwaway copy
of ``examples/sample-kb``, with ``PYTHON`` pointed at a shim that fails on
purpose. Each shim case asserts the shim was actually reached, so a harness that
ignores ``PYTHON`` cannot make a case pass by accident; each write case
fingerprints inodes, because a rewrite that reproduces identical bytes is still
a write.

One case is a source read rather than a run — the interpreter choice when
``PYTHON`` is unset. Exercising it would mean creating ``/tmp/factlog-venv``,
a path shared with every other checkout on the machine, which a test must not
do. It is noted as the exception rather than left to look like the others.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Every case shells out to the real harness, which runs the wirelog engine. CI
# calls this job the "pure-function unit layer"; this file is not that, and
# skipping is more honest than failing on an env that simply has no engine.
pytest.importorskip("pyrewire")

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
    assert "PASS: [kb1] facts/accepted.dl matches golden" in result.stdout, combined
    # Anchored at both ends, and across every KB: the harness also reports on the
    # COMMITTED copy of this file, which is a different and legitimately-true
    # claim, and it runs two KBs whose verdicts differ only by the label.
    assert not re.search(
        r"^PASS: \[[^]]+\] facts/logic_report\.txt matches golden$", result.stdout, re.M
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
    assert "PASS: [kb1] facts/logic_report.txt matches golden" in result.stdout, combined
    assert not re.search(
        r"^PASS: \[kb2\] check_conflicts\.py exit 1", result.stdout, re.M
    ), (
        "the harness read a dead check_conflicts as a detected contradiction\n"
        + combined
    )
    assert result.returncode != 0


def _artifacts(root: Path) -> list[Path]:
    return [root / "facts" / "accepted.dl", root / "facts" / "logic_report.txt"]


def _refuse_logic_check_shim(tmp_path: Path) -> Path:
    return _write_shim(
        tmp_path / "refuse-logic-check-2",
        "case \"$*\" in\n"
        '  *run_logic_check.py*) echo "shim: refused run_logic_check" >&2; exit 1 ;;\n'
        "esac\n"
        f'exec "{sys.executable}" "$@"\n',
    )


def test_symlinked_kb_root_is_not_written_through(tmp_path: Path) -> None:
    """A KB reached through a symlink must be read-only too.

    ``cp -R`` does not dereference a symlinked SOURCE operand — POSIX has ``-R``
    imply ``-P`` — so copying a symlinked KB root produced a *symlink* pointing
    back at the caller's real KB, and every step wrote straight through it. The
    run went green while the caller's KB was rewritten, which is exactly what
    running on a copy was supposed to stop.

    ``test_run_writes_nothing_inside_the_checkout`` cannot see this: it passes a
    real directory.
    """
    real = tmp_path / "realkb"
    shutil.copytree(SAMPLE_KB, real)
    link = tmp_path / "symkb"
    link.symlink_to(real, target_is_directory=True)
    before = {path: _fingerprint(path) for path in _artifacts(real)}

    result = _run_golden_at(link, tmp_path, Path(sys.executable))

    assert result.returncode == 0, (
        "this case pins where a GREEN run writes; it did not go green\n"
        f"{result.stdout}\n{result.stderr}"
    )
    changed = [
        path.name for path in _artifacts(real) if _fingerprint(path) != before[path]
    ]
    assert not changed, (
        "the harness wrote through the symlink into the caller's KB: "
        + ", ".join(changed)
    )


def test_symlinked_kb_root_survives_a_dead_step(tmp_path: Path) -> None:
    """A dead step must not delete the caller's file through a symlinked root.

    Worse than the write-through above, and worse than what it replaced: the
    working copy has its artifact removed before each step so a dead step has
    nothing to compare. Through a symlinked root that ``rm -f`` reached the real
    file, the step then died, and nothing recreated it — no signal needed, and
    the EXIT trap only removes the scratch directory.
    """
    real = tmp_path / "realkb"
    shutil.copytree(SAMPLE_KB, real)
    link = tmp_path / "symkb"
    link.symlink_to(real, target_is_directory=True)
    report = real / "facts" / "logic_report.txt"
    assert report.is_file()

    result = _run_golden_at(link, tmp_path, _refuse_logic_check_shim(tmp_path))
    combined = result.stdout + result.stderr

    assert "shim: refused run_logic_check" in combined, (
        "step 2 was not made to fail, so this case proves nothing\n" + combined
    )
    assert report.is_file(), (
        "a dead step deleted the caller's logic_report.txt through the symlink"
    )


def test_symlink_inside_the_kb_is_not_written_through(tmp_path: Path) -> None:
    """An inner symlink whose target escapes the KB is a second write-back path.

    ``cp -R`` preserves inner symlinks AS symlinks, so a KB whose ``facts/`` is a
    link to somewhere outside the KB had its steps write to that outside
    directory. Dereferencing on copy materialises it as a real directory in the
    scratch copy instead.
    """
    kb = tmp_path / "kb"
    shutil.copytree(SAMPLE_KB, kb)
    outside = tmp_path / "outside-facts"
    shutil.move(str(kb / "facts"), str(outside))
    (kb / "facts").symlink_to(outside, target_is_directory=True)
    before = {path: _fingerprint(path) for path in _artifacts(kb)}

    result = _run_golden_at(kb, tmp_path, Path(sys.executable))

    assert result.returncode == 0, (
        "this case pins where a GREEN run writes; it did not go green\n"
        f"{result.stdout}\n{result.stderr}"
    )
    changed = [
        path.name for path in _artifacts(kb) if _fingerprint(path) != before[path]
    ]
    assert not changed, (
        "the harness wrote through an inner symlink, outside the KB: "
        + ", ".join(changed)
    )


def test_symlink_cycle_in_the_kb_fails_loudly(tmp_path: Path) -> None:
    """Dereferencing on copy cannot follow a cycle, so it must stop and say so.

    ``cp -RL`` exits 1 on a cyclic symlink and copies everything else, so without
    a check the harness would run on a silently incomplete copy. This pins the
    loud stop. A KB like this is pathological; the point is that the copy step
    can fail at all, and that failing is not mistaken for a passing run.
    """
    kb = tmp_path / "kb"
    shutil.copytree(SAMPLE_KB, kb)
    (kb / "pages" / "loop").symlink_to(kb, target_is_directory=True)

    result = _run_golden_at(kb, tmp_path, Path(sys.executable))
    combined = result.stdout + result.stderr

    assert result.returncode != 0, combined
    assert "could not copy" in combined, (
        "the copy failed without saying so, or did not fail at all\n" + combined
    )
    assert "0 failed" not in result.stdout, combined


def test_read_only_source_kb_still_produces_a_verdict(tmp_path: Path) -> None:
    """A KB the caller keeps read-only must run like any other.

    ``cp`` copies modes, so the working copy inherited the source's permissions:
    the ``rm -f`` that clears an artifact before its step failed, ``set -e``
    killed the run partway through Step 1 with no ``Golden results:`` line at
    all, and the EXIT trap could not remove its own scratch directory either.
    Running on a copy is supposed to mean the source's properties do not reach
    the run.
    """
    kb = tmp_path / "kb"
    shutil.copytree(SAMPLE_KB, kb)
    subprocess.run(["chmod", "-R", "a-w", str(kb)], check=True)
    try:
        result = _run_golden_at(kb, tmp_path, Path(sys.executable))
    finally:
        # Leave tmp_path removable regardless of the outcome.
        subprocess.run(["chmod", "-R", "u+w", str(kb)], check=True)
    combined = result.stdout + result.stderr

    assert "Golden results:" in result.stdout, (
        "the run died without reporting a verdict\n" + combined
    )
    assert result.returncode == 0, combined
    assert "Permission denied" not in combined, combined


def test_interpreter_selection_matches_the_other_harnesses(tmp_path: Path) -> None:
    """golden.sh must pick its interpreter the way every other harness does.

    Behavioural coverage stops at "``PYTHON`` is honoured" (above); what this
    guards is the *unset* branch, which cannot be tested by running the harness
    without creating ``/tmp/factlog-venv`` — a path shared with every other
    checkout on the machine, so a test must not.

    It is still a real pin: the harness carried an implicit
    ``/tmp/factlog-venv`` branch ahead of ``python3``, so on a machine with that
    path it silently ran a different interpreter from the other 39 harnesses,
    and nothing in the output said so.
    """
    source = GOLDEN_SH.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )

    assert 'PYTHON="${PYTHON:-python3}"' in code, (
        "golden.sh no longer selects its interpreter the way tests/*.sh do"
    )
    assert "factlog-venv" not in code, (
        "golden.sh reintroduced a machine-dependent interpreter path; callers "
        "pass PYTHON explicitly, as they do for every other harness"
    )
