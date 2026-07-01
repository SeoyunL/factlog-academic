# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the structured doctor diagnostics (#180).

These exercise ``_collect_doctor_checks`` directly, monkeypatching the
environment so we can assert individual severities without shelling out or
touching the real machine. The contract that matters for exit codes: only
``FAIL`` rows may flip the doctor result — ``INFO``/``WARN`` are advisory.
"""
from __future__ import annotations

import factlog.cli as cli


def _by_title(checks, needle):
    """Return the first check whose title contains *needle* (or None)."""
    for c in checks:
        if needle in c.title:
            return c
    return None


class TestGitCheck:
    def test_missing_git_is_fail(self, monkeypatch):
        # `shutil` is imported inside the function, so patch the module directly.
        import shutil
        monkeypatch.setattr(shutil, "which", lambda name: None)
        checks = cli._collect_doctor_checks()
        git = _by_title(checks, "git")
        assert git is not None
        assert git.severity == "FAIL"
        assert git.hints, "a missing-git FAIL must carry an install hint"

    def test_present_git_is_ok(self, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git")
        checks = cli._collect_doctor_checks()
        git = _by_title(checks, "git")
        assert git is not None
        assert git.severity == "OK"


class TestFactlogPython:
    def test_unset_is_info(self, monkeypatch):
        monkeypatch.delenv("FACTLOG_PYTHON", raising=False)
        checks = cli._collect_doctor_checks()
        row = _by_title(checks, "FACTLOG_PYTHON")
        assert row is not None
        assert row.severity == "INFO"

    def test_set_and_existing_is_ok(self, monkeypatch, tmp_path):
        target = tmp_path / "python3"
        target.write_text("#!/bin/sh\n")
        monkeypatch.setenv("FACTLOG_PYTHON", str(target))
        checks = cli._collect_doctor_checks()
        row = _by_title(checks, "FACTLOG_PYTHON")
        assert row is not None
        assert row.severity == "OK"

    def test_set_but_missing_is_warn(self, monkeypatch, tmp_path):
        missing = tmp_path / "nope" / "python3"
        monkeypatch.setenv("FACTLOG_PYTHON", str(missing))
        checks = cli._collect_doctor_checks()
        row = _by_title(checks, "FACTLOG_PYTHON")
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
        row = _by_title(checks, "Python")
        assert row is not None
        # The interpreter path is surfaced in the title (issue #180 diag 2).
        assert cli.sys.executable in row.title


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
