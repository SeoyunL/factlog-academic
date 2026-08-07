# SPDX-License-Identifier: Apache-2.0
"""A bare ``factlog init``/``setup`` must not ignore the KB you are using (#356).

``--target`` defaulted to the literal string ``"~/wiki"`` in the parser, so
``init`` and ``setup`` were the only commands that did not follow factlog's
documented root precedence: with ``$FACTLOG_ROOT`` exported, or an active KB
configured, a bare ``factlog init`` still scaffolded a stray ``~/wiki``. That is
the other half of the accident in the issue — one command reached past both
signals of "the KB I am working in" and then (before the activation fix) made
the directory it had just invented the global default.

Resolution now follows the same order as every other command, minus the cwd
fallback: ``--target`` > ``$FACTLOG_ROOT`` > active-KB config > ``~/wiki``.
cwd is deliberately not in the chain — a bare ``init`` scattering a KB layout
into whatever directory the user happens to stand in would be a worse default
than the one being fixed.

These drive ``python -m factlog`` in a subprocess with ``HOME`` redirected, so
nothing can create a real ``~/wiki`` even when the test is wrong.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def run_init(*args: str, home: Path, config_home: Path | None = None, factlog_root: Path | None = None):
    """``python -m factlog init ...`` under a throwaway ``HOME``.

    ``HOME`` is always redirected: the ``~/wiki`` fallback is one of the paths
    under test, and a bug in either the test or the code must not be able to
    write into the developer's home directory.
    """
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.pop("FACTLOG_ROOT", None)
    env["XDG_CONFIG_HOME"] = str(config_home if config_home is not None else home / "unused-config")
    if factlog_root is not None:
        env["FACTLOG_ROOT"] = str(factlog_root)
    return subprocess.run(
        [sys.executable, "-m", "factlog", "init", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def write_pointer(config_home: Path, root: Path) -> None:
    path = config_home / "factlog" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"root": str(root)}) + "\n", encoding="utf-8")


@pytest.fixture()
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture()
def config_home(tmp_path):
    path = tmp_path / "cfg"
    path.mkdir()
    return path


def scaffolded(root: Path) -> bool:
    return (root / "sources").is_dir()


class TestBareInitFollowsThePrecedence:
    def test_factlog_root_is_used(self, tmp_path, home):
        env_kb = tmp_path / "env-kb"

        proc = run_init(home=home, factlog_root=env_kb)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert scaffolded(env_kb), proc.stdout
        assert not (home / "wiki").exists(), f"created a stray ~/wiki anyway: {proc.stdout}"

    def test_active_kb_config_is_used_when_no_env(self, tmp_path, home, config_home):
        cfg_kb = tmp_path / "config-kb"
        cfg_kb.mkdir()
        write_pointer(config_home, cfg_kb)

        proc = run_init(home=home, config_home=config_home)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert scaffolded(cfg_kb), proc.stdout
        assert not (home / "wiki").exists(), f"created a stray ~/wiki anyway: {proc.stdout}"

    def test_home_wiki_remains_the_last_resort(self, home):
        """GUARD, not evidence: passes before and after.

        Nothing exported, nothing configured — the documented first-run target,
        which reordering the chain must not have moved.
        """
        proc = run_init(home=home)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert scaffolded(home / "wiki"), proc.stdout

    def test_the_flag_still_wins_over_the_environment(self, tmp_path, home):
        """GUARD, not evidence: passes before and after (rank 1 was never in doubt)."""
        env_kb = tmp_path / "env-kb"
        flag_kb = tmp_path / "flag-kb"

        proc = run_init("--target", str(flag_kb), home=home, factlog_root=env_kb)

        assert scaffolded(flag_kb), proc.stdout
        assert not env_kb.exists(), proc.stdout


class TestTheResolvedTargetIsAnnounced:
    """An implicit target is only safe if the user can see which one it was."""

    def test_env_sourced_target_is_named_with_its_source(self, tmp_path, home):
        env_kb = tmp_path / "env-kb"

        out = run_init(home=home, factlog_root=env_kb).stdout

        assert str(env_kb) in out
        assert "FACTLOG_ROOT" in out, f"the target's origin is not reported: {out}"

    def test_an_explicit_target_needs_no_such_line(self, tmp_path, home):
        """GUARD, not evidence: passes before and after — no announcement existed
        to suppress. It holds the new line to implicit targets only."""
        flag_kb = tmp_path / "flag-kb"

        out = run_init("--target", str(flag_kb), home=home).stdout

        assert "no --target given" not in out, out
