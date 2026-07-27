# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the structured doctor diagnostics (#180).

These exercise ``_collect_doctor_checks`` directly, monkeypatching the
environment so we can assert individual severities without shelling out or
touching the real machine. The contract that matters for exit codes: only
``FAIL`` rows may flip the doctor result — ``INFO``/``WARN`` are advisory.
"""
from __future__ import annotations

import collections
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import factlog.cli as cli

_REPO_ROOT = Path(__file__).resolve().parents[2]

# A stand-in for sys.version_info that supports both attribute access
# (.major/.minor, used in the f-string) and slicing (used in the comparison).
_VersionInfo = collections.namedtuple("_VersionInfo", "major minor micro releaselevel serial")


def _by_prefix(checks, prefix):
    """Return the first check whose title *starts with* *prefix* (or None).

    The ONLY row lookup in this file, and a prefix match rather than a substring
    one, because doctor rows carry absolute paths and a substring search happily
    matches a *different* row's path.

    Measured, not theorised: with the checkout reached through a directory named
    ``git-home`` (the shape of every ``~/git/…`` or ``~/github/…`` clone), the
    #554 factlog row — which sits ABOVE the git row and carries
    ``…/git-home/factlog/__init__.py`` — was returned for ``"git"``, and
    ``test_git_fail_blocks_doctor_but_not_setup`` failed on ``'OK' == 'FAIL'``.
    Not a quiet false negative: a red suite for anyone who keeps code under a
    path containing the token.

    The substring helper this replaced is deleted rather than kept for the other
    rows. ``"Python"`` and ``"FACTLOG_PYTHON"`` were safe only by accident — list
    order and the improbability of those literals appearing in a path — and the
    accident stops holding the moment another path-bearing row is added.
    """
    for c in checks:
        if c.title.startswith(prefix):
            return c
    return None


class TestGitCheck:
    def test_missing_git_is_fail(self, monkeypatch):
        # `shutil` is imported inside the function, so patch the module directly.
        monkeypatch.setattr(shutil, "which", lambda name: None)
        checks = cli._collect_doctor_checks()
        git = _by_prefix(checks, "git")
        assert git is not None
        assert git.severity == "FAIL"
        assert git.hints, "a missing-git FAIL must carry an install hint"
        # git FAIL must not gate setup — setup does no git work.
        assert git.blocks_setup is False

    def test_present_git_is_ok(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git")
        checks = cli._collect_doctor_checks()
        git = _by_prefix(checks, "git")
        assert git is not None
        assert git.severity == "OK"


class TestFactlogPython:
    def test_unset_is_info(self, monkeypatch):
        monkeypatch.delenv("FACTLOG_PYTHON", raising=False)
        checks = cli._collect_doctor_checks()
        row = _by_prefix(checks, "FACTLOG_PYTHON")
        assert row is not None
        assert row.severity == "INFO"

    def test_unset_row_does_not_name_an_interpreter(self, monkeypatch):
        """The severity alone was the whole assertion, so the user-visible text
        was free to say anything.

        It used to read "시스템 python3 사용", and #578 made that a lie: with
        FACTLOG_PYTHON unset the launcher may pick an activated virtualenv or
        ~/.factlog-venv instead. Which interpreter actually won is the Python
        row's job to report; this row's only claim is that the override is unset.
        Pinned as a prefix plus the two halves of the claim, not as the exact
        sentence, so rewording stays free but re-asserting a specific interpreter
        does not.
        """
        monkeypatch.delenv("FACTLOG_PYTHON", raising=False)
        row = _by_prefix(cli._collect_doctor_checks(), "FACTLOG_PYTHON 미설정")
        assert row is not None, "the unset row must lead with FACTLOG_PYTHON 미설정"
        assert "런처" in row.title, "the row must say the launcher does the choosing"
        assert "시스템 python3" not in row.title, (
            "the row must not name an interpreter — since #578 the launcher may "
            "select an activated virtualenv or ~/.factlog-venv"
        )

    def test_set_and_existing_is_ok(self, monkeypatch, tmp_path):
        target = tmp_path / "python3"
        target.write_text("#!/bin/sh\n")
        monkeypatch.setenv("FACTLOG_PYTHON", str(target))
        checks = cli._collect_doctor_checks()
        row = _by_prefix(checks, "FACTLOG_PYTHON")
        assert row is not None
        assert row.severity == "OK"

    def test_set_but_missing_is_warn(self, monkeypatch, tmp_path):
        missing = tmp_path / "nope" / "python3"
        monkeypatch.setenv("FACTLOG_PYTHON", str(missing))
        checks = cli._collect_doctor_checks()
        row = _by_prefix(checks, "FACTLOG_PYTHON")
        assert row is not None
        assert row.severity == "WARN"


class TestShadowFolder:
    def test_shadow_folder_triggers_warn(self, monkeypatch, tmp_path):
        # A cwd with a stray ./factlog dir, no pyproject.toml, that is not the
        # real package → WARN.
        (tmp_path / "factlog").mkdir()
        monkeypatch.chdir(tmp_path)
        checks = cli._collect_doctor_checks()
        shadow = next((c for c in checks if "factlog/ 폴더" in c.title), None)
        assert shadow is not None
        assert shadow.severity == "WARN"

    def test_repo_root_does_not_trigger(self, monkeypatch, tmp_path):
        # cwd has ./factlog but also a pyproject.toml → it's the repo, not a
        # shadow. Must not fire (mirrors smoke.sh/setup.sh running from repo root).
        (tmp_path / "factlog").mkdir()
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        monkeypatch.chdir(tmp_path)
        checks = cli._collect_doctor_checks()
        assert not any("factlog/ 폴더" in c.title for c in checks)

    def test_no_factlog_folder_does_not_trigger(self, monkeypatch, tmp_path):
        # Empty cwd (mirrors the pip-install test's throwaway tmp dir).
        monkeypatch.chdir(tmp_path)
        checks = cli._collect_doctor_checks()
        assert not any("factlog/ 폴더" in c.title for c in checks)


class TestPythonSurface:
    def test_python_token_and_interpreter_path_present(self):
        checks = cli._collect_doctor_checks()
        row = _by_prefix(checks, "Python")
        assert row is not None
        # The interpreter path is surfaced in the title (issue #180 diag 2).
        assert cli.sys.executable in row.title

    def test_python_below_floor_is_fail(self, monkeypatch):
        monkeypatch.setattr(cli.sys, "version_info", _VersionInfo(3, 10, 0, "final", 0))
        checks = cli._collect_doctor_checks()
        row = _by_prefix(checks, "Python")
        assert row is not None
        assert row.severity == "FAIL"
        # And this FAIL *does* gate setup (default), unlike git.
        assert row.blocks_setup is True

    def test_windowsapps_store_stub_is_warn(self, monkeypatch):
        # Force a supported version so we reach the interpreter-surface branch,
        # then point the interpreter at a Store-stub path.
        monkeypatch.setattr(cli.sys, "version_info", _VersionInfo(3, 12, 0, "final", 0))
        monkeypatch.setattr(
            cli.sys, "executable",
            r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\python.exe",
        )
        checks = cli._collect_doctor_checks()
        row = _by_prefix(checks, "Python")
        assert row is not None
        assert row.severity == "WARN"


class TestRenderAndExitContract:
    def test_render_returns_true_when_no_fail(self):
        checks = [
            cli.Check("OK", "Python 3.12 (/x)"),
            cli.Check("INFO", "FACTLOG_PYTHON 미설정"),
            cli.Check("WARN", "something advisory"),
        ]
        assert cli._render_doctor(checks, emit_summary=True) is True

    def test_render_returns_false_when_any_fail(self):
        checks = [cli.Check("OK", "Python 3.12 (/x)"), cli.Check("FAIL", "git이 없습니다")]
        assert cli._render_doctor(checks, emit_summary=False) is False

    def test_summary_banner_only_with_emit(self, capsys):
        cli._render_doctor([cli.Check("OK", "ok")], emit_summary=False)
        assert "결과:" not in capsys.readouterr().out
        cli._render_doctor([cli.Check("OK", "ok")], emit_summary=True)
        assert "결과:" in capsys.readouterr().out


class TestSetupGateDecoupledFromGit:
    """A missing git is reported by doctor but must not fail `factlog setup`."""

    def test_git_fail_blocks_doctor_but_not_setup(self, monkeypatch, tmp_path):
        # Healthy except for git: git absent, no shadow folder, FACTLOG_PYTHON unset.
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.delenv("FACTLOG_PYTHON", raising=False)
        monkeypatch.chdir(tmp_path)
        checks = cli._collect_doctor_checks()

        git = _by_prefix(checks, "git")
        assert git is not None and git.severity == "FAIL"

        # doctor gate (all FAIL) → blocked.
        assert cli._render_doctor(checks, gate="all") is False
        # setup gate (blocks_setup FAIL only) → not blocked by git alone.
        # (pyrewire may be absent in the runner; only assert git does not gate.)
        non_git_fail = any(
            c.severity == "FAIL" and c.blocks_setup for c in checks
        )
        assert cli._render_doctor(checks, gate="setup") is (not non_git_fail)

    def test_only_git_fail_setup_gate_passes(self):
        # Synthetic check set: everything OK except a non-blocking git FAIL.
        checks = [
            cli.Check("OK", "Python 3.12 (/x)"),
            cli.Check("OK", "pyrewire 1.0.3"),
            cli.Check("FAIL", "git이 없습니다", ("hint",), blocks_setup=False),
            cli.Check("INFO", "FACTLOG_PYTHON 미설정"),
        ]
        assert cli._render_doctor(checks, gate="all") is False
        assert cli._render_doctor(checks, gate="setup") is True

    def test_blocking_fail_gates_setup(self):
        checks = [
            cli.Check("FAIL", "pyrewire not installed", ("hint",)),  # blocks_setup default True
            cli.Check("OK", "git"),
        ]
        assert cli._render_doctor(checks, gate="setup") is False


class TestHealthyEnvExitZero:
    """End-to-end: a healthy environment yields a passing (exit 0) doctor."""

    def test_healthy_env_render_returns_true(self, monkeypatch, tmp_path):
        pytest.importorskip("pyrewire")  # needed for a genuinely clean env
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git")
        monkeypatch.delenv("FACTLOG_PYTHON", raising=False)
        monkeypatch.chdir(tmp_path)  # empty cwd: no shadow ./factlog folder
        assert cli._render_doctor(cli._collect_doctor_checks(), emit_summary=True) is True


class TestAsciiLocaleDoesNotCrash:
    """doctor must never crash on an ASCII/C-locale stdout (#180 review)."""

    def test_render_survives_ascii_stdout(self, monkeypatch):
        # A real ascii-encoded text stream: without _harden_stdout the em-dash /
        # Korean in the banner would raise UnicodeEncodeError here.
        import io

        buf = io.BytesIO()
        ascii_stdout = io.TextIOWrapper(buf, encoding="ascii", newline="")
        monkeypatch.setattr(cli.sys, "stdout", ascii_stdout)
        cli._render_doctor([cli.Check("OK", "Python 3.12 (/x)")], emit_summary=True)
        ascii_stdout.flush()
        out = buf.getvalue().decode("ascii")
        assert "Python" in out  # ASCII token still comes through
        # Non-ASCII degraded to backslash escapes rather than crashing.
        assert "\\u" in out or "\\x" in out

    def test_doctor_subprocess_under_c_locale(self, tmp_path):
        pytest.importorskip("pyrewire")
        if shutil.which("git") is None:
            pytest.skip("git required to assert a clean exit 0")
        env = dict(
            os.environ,
            LC_ALL="C",
            LANG="C",
            LANGUAGE="C",
            PYTHONIOENCODING="ascii",
        )
        env.pop("FACTLOG_PYTHON", None)
        # Run from the repo root so `-m factlog` resolves without an install,
        # and pyproject.toml there suppresses the shadow-folder heuristic.
        proc = subprocess.run(
            [sys.executable, "-m", "factlog", "doctor"],
            cwd=str(_REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        combined = proc.stdout + proc.stderr
        assert "Traceback" not in combined, combined
        assert "UnicodeEncodeError" not in combined, combined
        assert "Python" in combined, combined
        assert proc.returncode == 0, combined


class TestRenameMigrationCheck:
    """The `factlog` → `factlog-academic` rename hazard (#228).

    Both distributions own the same `factlog` module AND the same `factlog` console
    script, so pip installs them side by side without a word, and uninstalling the
    old one deletes the shared command while pip still reports factlog-academic as
    installed.

    The first version of this diagnostic had no tests and fired in EVERY source
    clone: `importlib.metadata` walks sys.path, and a checkout carries a leftover
    `factlog.egg-info/` from before the rename, which it counts as an installed
    dist. A diagnostic that cries wolf is one users learn to ignore.
    """

    def test_both_dists_installed_is_warned(self):
        from factlog.cli import rename_migration_check

        check = rename_migration_check({"factlog", "factlog-academic"}, factlog_on_path=True)
        assert check is not None and check.severity == "WARN"

    def test_only_the_new_dist_is_quiet(self):
        from factlog.cli import rename_migration_check

        assert rename_migration_check({"factlog-academic"}, factlog_on_path=True) is None

    def test_the_already_broken_state_is_warned(self):
        # The state users actually land in: they uninstalled the old dist, pip says
        # factlog-academic is installed, and the `factlog` command is gone.
        from factlog.cli import rename_migration_check

        check = rename_migration_check({"factlog-academic"}, factlog_on_path=False)
        assert check is not None and "factlog 명령이 없습니다" in check.title

    def test_a_leftover_egg_info_is_not_an_installed_dist(self):
        # THE false positive: a source clone's stale factlog.egg-info/ must not be
        # counted, or the check fires for every developer.
        from pathlib import Path

        from factlog.cli import installed_distributions

        class _Dist:
            def __init__(self, name, path):
                self.metadata = {"Name": name}
                self._path = Path(path)

        dists = [
            _Dist("factlog", "/repo/factlog.egg-info"),
            _Dist("factlog-academic", "/venv/lib/python3.12/site-packages/factlog_academic-0.7.0.dist-info"),
        ]
        assert installed_distributions(lambda: dists) == {"factlog-academic"}

    def test_a_dist_without_metadata_does_not_crash_doctor(self):
        # doctor is what you run when the environment is broken; it must not die on
        # an unreadable distribution.
        from pathlib import Path

        from factlog.cli import installed_distributions

        class _Broken:
            metadata = None
            _path = Path("/venv/lib/python3.12/site-packages/x.dist-info")

        assert installed_distributions(lambda: [_Broken()]) == set()


class TestFactlogRuntimeRow:
    """doctor names factlog's own version and import path (#554).

    doctor reported ``pyrewire 1.0.3`` while saying nothing about factlog itself —
    a dependency's version surfaced, our own withheld. In an environment where a
    bundled copy and a working tree coexist, that left no way to tell which code
    the user was actually running, and four already-fixed defects were re-reported
    as live bugs from artifacts that one row would have disambiguated.
    """

    def test_factlog_row_carries_both_version_and_path(self):
        import factlog

        checks = cli._collect_doctor_checks()
        row = _by_prefix(checks, "factlog ")
        assert row is not None, "doctor must name the factlog it is running"
        assert row.severity == "OK"
        # BOTH, not either: the version alone cannot tell two checkouts of the same
        # release apart, and the path alone cannot tell two releases apart.
        assert factlog.__version__ in row.title
        assert factlog.__file__ in row.title

    def test_factlog_row_sits_directly_after_pyrewire(self):
        # Placement is the point of the row: the asymmetry #554 names is "reports the
        # engine's version, not its own", so the answer belongs next to the question.
        pytest.importorskip("pyrewire")
        titles = [c.title for c in cli._collect_doctor_checks()]
        engine = next(i for i, t in enumerate(titles) if t.startswith("pyrewire"))
        assert titles[engine + 1].startswith("factlog ")

    def test_row_is_present_even_when_no_distribution_is_installed(self):
        # The version/path row is unconditional; only the *skew* row depends on pip
        # having recorded something. A source checkout with no install must still be
        # able to say which code it ran.
        monkey = pytest.MonkeyPatch()
        try:
            monkey.setattr(
                "factlog.runtime.installed_factlog_dist", lambda distributions=None: None
            )
            rows = cli._factlog_runtime_checks()
        finally:
            monkey.undo()
        assert len(rows) == 1 and rows[0].severity == "OK"

    def test_skew_warn_is_appended_after_the_version_row(self, monkeypatch):
        monkeypatch.setattr(
            "factlog.runtime.installed_factlog_dist",
            lambda distributions=None: ("factlog-academic", "0.6.0", "/bundled/copy"),
        )
        rows = cli._factlog_runtime_checks()
        assert [r.severity for r in rows] == ["OK", "WARN"]
        assert "/bundled/copy" in rows[1].title
        assert rows[1].hints

    def test_the_skew_warn_is_a_different_axis_from_the_shadow_warn(self, monkeypatch):
        """Two WARNs about sys.path must not read as the same warning.

        ``_shadow_factlog_dir`` warns about a stray ``./factlog`` folder in the
        *current directory*. This one warns that the *installed distribution*
        disagrees with the *imported package*, wherever either lives. A reader who
        cannot tell them apart will apply the wrong remedy.
        """
        monkeypatch.setattr(
            "factlog.runtime.installed_factlog_dist",
            lambda distributions=None: ("factlog-academic", "0.6.0", "/bundled/copy"),
        )
        skew = cli._factlog_runtime_checks()[1]
        assert "설치된 배포판" in skew.title and "실행 중인" in skew.title
        assert "이 폴더에" not in skew.title
        assert "pip install -e ." in " ".join(skew.hints)

    def test_skew_warn_does_not_flip_the_doctor_exit_code(self, monkeypatch, capsys):
        """The contract this row must never break.

        WARN/INFO are advisory; only FAIL gates. A diagnostic added to make skew
        *observable* must not turn a working install into a failing one — smoke.sh
        and setup.sh both depend on a healthy environment exiting 0, and the skew
        state is the NORMAL state for anyone running from a checkout.
        """
        monkeypatch.setattr(
            "factlog.runtime.installed_factlog_dist",
            lambda distributions=None: ("factlog-academic", "0.6.0", "/bundled/copy"),
        )
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git")
        rows = cli._factlog_runtime_checks()
        assert cli._render_doctor(rows) is True
        assert cli._render_doctor(rows, gate="setup") is True
        assert "WARN" in capsys.readouterr().out

    def test_stale_metadata_after_a_version_bump_is_info_not_warn(self, monkeypatch):
        # Every maintainer's daily state: __version__ bumped, no reinstall. A WARN
        # here would fire in every clone and train people to ignore the row.
        import factlog

        monkeypatch.setattr(
            "factlog.runtime.installed_factlog_dist",
            lambda distributions=None: (
                "factlog-academic",
                "0.0.0-stale",
                str(Path(factlog.__file__).parent.parent),
            ),
        )
        rows = cli._factlog_runtime_checks()
        assert [r.severity for r in rows] == ["OK", "INFO"]
        assert cli._render_doctor(rows) is True

    def test_matching_install_adds_no_second_row(self, monkeypatch):
        import factlog

        monkeypatch.setattr(
            "factlog.runtime.installed_factlog_dist",
            lambda distributions=None: (
                "factlog-academic",
                factlog.__version__,
                str(Path(factlog.__file__).parent.parent),
            ),
        )
        assert len(cli._factlog_runtime_checks()) == 1

    def test_no_row_from_this_module_is_ever_fail(self):
        # Measured against the real environment, not an injected one.
        assert all(c.severity != "FAIL" for c in cli._factlog_runtime_checks())
