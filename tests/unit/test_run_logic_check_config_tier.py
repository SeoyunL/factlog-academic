# SPDX-License-Identifier: Apache-2.0
"""``tools/run_logic_check.py`` honours the active-KB config tier (#528).

The skill's determinism gate names this script with no arguments — "Always run
``tools/run_logic_check.py`` and show the resulting ``facts/logic_report.txt``
verbatim" — so the documented form carries no KB flag and no ``FACTLOG_ROOT``.
Without a ``resolve_root_from_argv`` pre-pass the script bound its paths to cwd
and died with "not a factlog KB root" from anywhere else, which made the one
command the gate mandates unrunnable outside a KB directory.

The precedence pins below are engine-free on purpose: they read the root the
import-time pre-pass bound into ``common``, so they run identically on a machine
without pyrewire, where the end-to-end class skips. Resolution happens before
the engine is ever consulted, so that is the whole of what precedence means here.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools"
COMPILE = TOOLS / "compile_facts.py"
CHECK = TOOLS / "run_logic_check.py"
HEADER = "subject,relation,object,source,status,confidence,note"

# Import the script, then report the root ``common`` actually bound. The pre-pass
# runs at import of run_logic_check, so this is the value every path global in the
# module was derived from.
_PROBE = (
    "import run_logic_check;"  # imported for its import-time pre-pass, nothing else
    "import common;"
    "print(common.ROOT)"
)


def _env(root: Path | None = None, config_home: Path | None = None) -> dict[str, str]:
    """Child env with the repo importable and FACTLOG_ROOT under our control.

    The unit conftest pins FACTLOG_ROOT process-wide, so the config and cwd tiers
    are only reachable in a child that has it removed — inheriting it would make
    every tier below env untestable (and pass vacuously).
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(TOOLS), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    env.pop("FACTLOG_ROOT", None)
    if root is not None:
        env["FACTLOG_ROOT"] = str(root)
    if config_home is not None:
        env["XDG_CONFIG_HOME"] = str(config_home)
    return env


def _write_config(config_home: Path, root: Path) -> None:
    path = config_home / "factlog" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"root": str(root)}) + "\n", encoding="utf-8")


def _resolved_root(cwd: Path, *args: str, **env_kwargs) -> Path:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, *args],
        cwd=cwd, capture_output=True, text=True, check=True, env=_env(**env_kwargs),
    )
    return Path(proc.stdout.strip())


def _init_kb(kb: Path, config_home: Path) -> Path:
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        check=True, capture_output=True, env=_env(root=kb.parent, config_home=config_home),
    )
    return kb


@pytest.fixture
def config_home(tmp_path, monkeypatch) -> Path:
    """A per-test XDG config home, layered over the session-wide sandbox.

    The autouse isolation fixture already keeps writes out of the developer's real
    config; this narrows it further so one test's active KB cannot decide another's.
    """
    home = tmp_path / "xdg"
    home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home


class TestRootPrecedence:
    """``--target`` > ``$FACTLOG_ROOT`` > config > cwd, engine-free."""

    def test_config_active_kb_is_used_from_an_unrelated_cwd(self, tmp_path, config_home):
        """The #528 repro: no flag, no env, cwd is not a KB."""
        kb = tmp_path / "config-kb"
        kb.mkdir()
        _write_config(config_home, kb)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        assert _resolved_root(elsewhere, config_home=config_home) == kb.resolve()

    def test_env_beats_config(self, tmp_path, config_home):
        env_kb = tmp_path / "env-kb"
        env_kb.mkdir()
        cfg_kb = tmp_path / "config-kb"
        cfg_kb.mkdir()
        _write_config(config_home, cfg_kb)

        root = _resolved_root(tmp_path, root=env_kb, config_home=config_home)
        assert root == env_kb.resolve()

    def test_flag_beats_env_and_config(self, tmp_path, config_home):
        flag_kb = tmp_path / "flag-kb"
        flag_kb.mkdir()
        env_kb = tmp_path / "env-kb"
        env_kb.mkdir()
        cfg_kb = tmp_path / "config-kb"
        cfg_kb.mkdir()
        _write_config(config_home, cfg_kb)

        root = _resolved_root(
            tmp_path, "--target", str(flag_kb), root=env_kb, config_home=config_home
        )
        assert root == flag_kb.resolve()

    def test_cwd_is_the_last_resort(self, tmp_path, config_home):
        """No flag, no env, no configured KB — the pre-#528 behaviour, unchanged."""
        kb = tmp_path / "cwd-kb"
        kb.mkdir()

        assert _resolved_root(kb, config_home=config_home) == kb.resolve()

    def test_config_does_not_override_a_kb_the_caller_stands_in(self, tmp_path, config_home):
        """cwd loses to config — the counter-example to "run it where you are".

        Pinned because the opposite reading is tempting and would silently write one
        KB's logic_report.txt while the operator reads another's.
        """
        cwd_kb = tmp_path / "cwd-kb"
        cwd_kb.mkdir()
        cfg_kb = tmp_path / "config-kb"
        cfg_kb.mkdir()
        _write_config(config_home, cfg_kb)

        assert _resolved_root(cwd_kb, config_home=config_home) == cfg_kb.resolve()


class TestReportUnchanged:
    """End-to-end: the report and the rc rule survive the new pre-pass."""

    def _seed(self, kb: Path, config_home: Path, query: str = "") -> None:
        _init_kb(kb, config_home)
        (kb / "sources" / "a.md").write_text("a\n", encoding="utf-8")
        (kb / "facts" / "candidates.csv").write_text(
            f"{HEADER}\nA,uses,B,sources/a.md,confirmed,0.90,\n", encoding="utf-8"
        )
        if query:
            (kb / "facts" / "query.dl").write_text(query, encoding="utf-8")
        subprocess.run(
            [sys.executable, str(COMPILE)], cwd=kb, check=True, capture_output=True,
            env=_env(root=kb, config_home=config_home),
        )

    def test_config_kb_report_is_written_outside_it(self, tmp_path, config_home):
        pytest.importorskip("pyrewire", reason="run_logic_check needs the engine")
        kb = tmp_path / "kb"
        self._seed(kb, config_home, query='relation("A", "uses", O)?\n')
        _write_config(config_home, kb)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        proc = subprocess.run(
            [sys.executable, str(CHECK)], cwd=elsewhere, capture_output=True, text=True,
            env=_env(config_home=config_home),
        )

        assert proc.returncode == 0, proc.stderr
        report = (kb / "facts" / "logic_report.txt").read_text(encoding="utf-8")
        assert "Logic Check Report" in report
        assert "engine facts: 1" in report
        assert "errors: 0" in report
        # The report belongs to the KB, not to wherever the operator happened to stand.
        assert not (elsewhere / "facts").exists()

    def test_inside_the_kb_the_report_is_identical(self, tmp_path, config_home):
        """Same KB, run from inside with FACTLOG_ROOT — byte-for-byte the same report."""
        pytest.importorskip("pyrewire", reason="run_logic_check needs the engine")
        kb = tmp_path / "kb"
        self._seed(kb, config_home, query='relation("A", "uses", O)?\n')
        report_path = kb / "facts" / "logic_report.txt"

        inside = subprocess.run(
            [sys.executable, str(CHECK)], cwd=kb, capture_output=True, text=True,
            env=_env(root=kb, config_home=config_home),
        )
        assert inside.returncode == 0, inside.stderr
        from_inside = report_path.read_text(encoding="utf-8")

        _write_config(config_home, kb)
        report_path.unlink()
        outside = subprocess.run(
            [sys.executable, str(CHECK)], cwd=tmp_path, capture_output=True, text=True,
            env=_env(config_home=config_home),
        )
        assert outside.returncode == 0, outside.stderr

        assert report_path.read_text(encoding="utf-8") == from_inside

    def test_an_error_still_exits_nonzero_through_the_config_tier(self, tmp_path, config_home):
        """rc != 0 only on errors — and reaching the KB indirectly must not soften it."""
        pytest.importorskip("pyrewire", reason="run_logic_check needs the engine")
        kb = tmp_path / "kb"
        self._seed(kb, config_home, query='count("A")?\n')
        _write_config(config_home, kb)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        proc = subprocess.run(
            [sys.executable, str(CHECK)], cwd=elsewhere, capture_output=True, text=True,
            env=_env(config_home=config_home),
        )

        assert proc.returncode == 1
        report = (kb / "facts" / "logic_report.txt").read_text(encoding="utf-8")
        assert "Errors:" in report
