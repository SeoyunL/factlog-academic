# SPDX-License-Identifier: Apache-2.0
"""Contract pins for ``tests/setup.sh`` itself (#361).

``PYTHON`` was assigned unconditionally, so ``PYTHON=<interpreter> bash
tests/setup.sh`` — the form the rest of ``tests/`` uses — was silently
discarded, exactly as ``tests/golden.sh`` discarded it before #354. A run
pointed at an interpreter WITH pyrewire fell through to a bare ``python3``
without it and reported ``0 passed, 9 failed``, a verdict that named ``setup``
for a fact about the interpreter. Nothing in the output said which interpreter
had run, so the verdict could not be checked by reading it.

Every case runs the real harness as a subprocess:

1. ``PYTHON`` is honoured. The shim satisfies the engine probe and refuses
   everything after it, and the case asserts the shim was reached, so a harness
   that ignores ``PYTHON`` cannot pass by accident.
2. The interpreter that was selected appears in the output, under both an
   explicit ``PYTHON`` and none.
3. An interpreter without pyrewire skips at exit 0 instead of reporting
   failures about ``setup``. This is also how the unset branch gets exercised
   without creating ``/tmp/factlog-venv`` — a path shared with every other
   checkout on the machine, which a test must not create.
4. A ``PYTHON`` that cannot run at all is fatal rather than a skip. The engine
   probe fails identically for a mistyped path and for a real interpreter
   missing pyrewire, so without the separate check a typo would be reported as
   "this machine has no engine".

The interpreter choice is pinned by running the harness, never by reading its
source: a source-text pin fails on an equivalent shell rewrite while passing on
a rewrite that keeps the text and drops the behaviour.
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


def _run_setup(
    tmp_path: Path,
    python: Path | str | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the harness. ``python=None`` leaves ``PYTHON`` unset, which is the
    only way to exercise the branch that picks the interpreter itself. ``cwd``
    defaults to the checkout, the one directory in which a relative ``PYTHON``
    cannot expose where the harness resolves it."""
    env = os.environ.copy()
    if python is None:
        env.pop("PYTHON", None)
    else:
        env["PYTHON"] = str(python)
    # setup.sh redirects XDG_CONFIG_HOME itself; pin HOME too so no path in the
    # run can reach the developer's real ~/.config/factlog/config.json (#62).
    env["HOME"] = str(tmp_path / "home")
    # Keep the run's KB inside this case's scratch directory. The harness's
    # default is /tmp/factlog-setup-test-kb, which every checkout and every
    # parallel lane on the machine shares, and Step 1 begins by deleting it.
    env["SETUP_KB"] = str(tmp_path / "kb")
    return subprocess.run(
        ["bash", str(SETUP_SH)],
        cwd=str(cwd or REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def _engine_shim(tmp_path: Path, name: str, body: str) -> Path:
    """A shim that satisfies ``-c "import pyrewire"`` and then runs ``body``.

    Every case needs to get past the engine probe to reach the steps; a shim
    that refuses the probe too would only ever pin the skip.
    """
    return _write_shim(
        tmp_path / name,
        'case "$*" in\n  *"import pyrewire"*) exit 0 ;;\nesac\n' + body,
    )


def test_honours_python_from_environment(tmp_path: Path) -> None:
    """``PYTHON=<interpreter>`` selects the interpreter, as in every other harness.

    The shim refuses everything after the engine probe, so a harness that
    honours ``PYTHON`` cannot report a pass. Before the fix the assignment was
    unconditional and the run reported its verdict under whatever ``python3``
    resolved to.

    The refused Step 1 makes this the case that can also see WHERE the run put
    its KB — Step 2 names the path in each ``FAIL:`` line — so it pins
    ``SETUP_KB`` too. Without that assertion the harness could go back to the
    hardcoded ``/tmp/factlog-setup-test-kb`` and every case here would stay
    green while deleting a directory the whole machine shares.
    """
    shim = _engine_shim(
        tmp_path, "refuse-all", 'echo "shim: refused $*" >&2\nexit 3\n'
    )

    result = _run_setup(tmp_path, shim)
    combined = result.stdout + result.stderr

    assert "shim: refused" in combined, (
        "the harness never invoked $PYTHON, so it is still ignoring the "
        f"caller's interpreter\n{combined}"
    )
    assert result.returncode != 0
    assert "0 failed" not in result.stdout
    assert str(tmp_path / "kb") in combined, (
        "the run ignored SETUP_KB and used its machine-wide default, which it "
        f"deletes before Step 1\n{combined}"
    )


def test_relative_python_resolves_where_the_steps_run(tmp_path: Path) -> None:
    """A relative ``PYTHON`` must be resolved in one directory, not two.

    The steps invoke ``$PYTHON`` from ``PLUGIN_ROOT``. When the engine probe was
    left in the caller's cwd instead, a relative path to an interpreter that HAS
    pyrewire reported ``SKIP: pyrewire not installed`` and exited 0 from every
    cwd except the repo root: a green run that measured nothing, and the
    "mistyped path" / "machine without the engine" confusion the surrounding
    check exists to prevent, running in the other direction.

    The case runs from outside the checkout with a path relative to it, so a
    check performed in the wrong directory resolves to nothing. Nothing is
    written inside the checkout: the shim lives in ``tmp_path`` and is named by
    a path relative to the repo root.
    """
    stepped = tmp_path / "stepped"
    shim = _engine_shim(tmp_path, "relative", f': > "{stepped}"\nexit 3\n')
    relative = os.path.relpath(shim, REPO_ROOT)
    assert not os.path.isabs(relative)

    result = _run_setup(tmp_path, relative, cwd=tmp_path)
    combined = result.stdout + result.stderr

    assert "SKIP:" not in result.stdout, (
        "an interpreter that has the engine was reported as a machine without "
        "it, because the probe resolved the path somewhere the steps do not\n"
        + combined
    )
    assert "FATAL:" not in result.stderr, combined
    assert stepped.exists(), (
        "the harness never reached a step with the caller's interpreter\n"
        + combined
    )


def test_output_names_the_interpreter_that_ran(tmp_path: Path) -> None:
    """The run must say which interpreter it used, given one or choosing one.

    A verdict printed without the interpreter beside it is what let ``0 passed,
    9 failed`` be read as a broken tree and a green run be attributed to an
    exported ``PYTHON`` that was in fact discarded. Reading the output has to be
    enough to tell.
    """
    shim = _engine_shim(tmp_path, "named", "exit 3\n")

    named = _run_setup(tmp_path, shim)
    assert f"PYTHON: {shim}" in named.stdout, (
        "the run did not name the interpreter it was given\n"
        + named.stdout
        + named.stderr
    )

    chosen = _run_setup(tmp_path)
    assert "PYTHON: python3" in chosen.stdout, (
        "the run did not name the interpreter it chose for itself\n"
        + chosen.stdout
        + chosen.stderr
    )


def test_interpreter_without_the_engine_skips(tmp_path: Path) -> None:
    """No pyrewire means no verdict about ``setup`` — skip, do not fail.

    ``setup`` on an interpreter without the engine cannot reach the "already
    satisfied, skip install" path this harness exists to check, so the nine
    ``FAIL:`` lines it used to print described the interpreter while naming
    ``setup``. This is the repo's existing form for engine-dependent harnesses
    (``test_canonical_rule_firing.sh``, ``test_attr_path_exclusion.sh``).

    It also covers the unset branch: ``PYTHON`` unset resolves to ``python3``,
    and the alternative — proving what an unset run selects by creating
    ``/tmp/factlog-venv`` — would write to a path the whole machine shares.
    """
    # Marker files, not stderr: the harness runs the probe with both streams
    # redirected to /dev/null, so a shim that announced itself there would leave
    # this case unable to tell "probed and refused" from "never probed".
    probed = tmp_path / "probed"
    stepped = tmp_path / "stepped"
    shim = _write_shim(
        tmp_path / "no-engine",
        'case "$*" in\n'
        f'  *"import pyrewire"*) : > "{probed}"; exit 1 ;;\n'
        "esac\n"
        f': > "{stepped}"\nexit 0\n',
    )

    result = _run_setup(tmp_path, shim)
    combined = result.stdout + result.stderr

    assert probed.exists(), "the harness never probed for the engine\n" + combined
    assert result.returncode == 0, "an absent engine is a skip, not a failure\n" + combined
    assert result.stdout.startswith(f"PYTHON: {shim}\n"), combined
    assert "SKIP: pyrewire not installed" in result.stdout, (
        "the run gave no skip line, so a reader cannot tell it measured "
        "nothing\n" + combined
    )
    assert not stepped.exists(), (
        "the harness ran its steps without the engine\n" + combined
    )
    assert "FAIL:" not in combined, (
        "the run reported failures about `setup` for a fact about the "
        "interpreter\n" + combined
    )


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
    assert "SKIP:" not in result.stdout, (
        "a path that cannot run was reported as an absent engine, which is a "
        "fact about the machine and not about the value the caller passed\n"
        + combined
    )
    assert "Setup results:" not in result.stdout, (
        "the harness ran its steps anyway, under some other interpreter\n"
        + combined
    )
