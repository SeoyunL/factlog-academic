# SPDX-License-Identifier: Apache-2.0
"""compile_facts honours the active-KB config tier (#527).

`factlog where` documents one precedence for every entry point —
``--flag > $FACTLOG_ROOT > config > cwd`` — but compile_facts implemented only
the last two tiers: it had no root flag at all and never read the config, so a
bare run from outside the KB took cwd as the root and exited 1 with "not a
factlog KB root". SKILL.md's `/factlog check` Step 1 runs exactly that command
with no arguments, which made following the documentation fail anywhere but
inside the KB directory.

Everything below drives the real script in a subprocess rather than importing
it: the fix is a *pre-import* pass (common binds its path globals from
FACTLOG_ROOT at import time), so an in-process call would resolve the root once
for the whole pytest session and prove nothing about a fresh run. ``PYTHONPATH``
pins the worktree ahead of any editable install, and the env is built without
FACTLOG_ROOT (conftest sets one for the suite) so the tiers under test are the
ones actually exercised.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_SCRIPT = REPO_ROOT / "tools" / "compile_facts.py"


def _seed_kb(path: Path, subject: str = "Alpha") -> Path:
    """A minimal but complete KB with exactly one confirmed fact."""
    for name in ("sources", "pages", "facts", "decisions", "policy"):
        (path / name).mkdir(parents=True, exist_ok=True)
    (path / "sources" / "a.md").write_text("a\n", encoding="utf-8")
    (path / "facts" / "candidates.csv").write_text(
        "subject,relation,object,source,status,confidence,note\n"
        f"{subject},uses,Beta,sources/a.md,confirmed,0.9,\n",
        encoding="utf-8",
    )
    return path


def _write_config(root: Path | None) -> Path:
    """Point the sandboxed active-KB config at *root* (None clears it).

    The autouse ``isolated_user_config`` fixture already redirects
    ``$XDG_CONFIG_HOME`` into a sandbox home and removes this file on teardown,
    so writing it here can never touch the developer's real config.
    """
    cfg = Path(os.environ["XDG_CONFIG_HOME"]) / "factlog" / "config.json"
    if root is None:
        cfg.unlink(missing_ok=True)
        return cfg
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"root": str(root)}) + "\n", encoding="utf-8")
    return cfg


def _run(*args: str, cwd: Path, env_root: Path | None = None, script: bool = False):
    env = {k: v for k, v in os.environ.items() if k != "FACTLOG_ROOT"}
    env["PYTHONPATH"] = str(REPO_ROOT)
    if env_root is not None:
        env["FACTLOG_ROOT"] = str(env_root)
    argv = (
        [sys.executable, str(TOOLS_SCRIPT), *args]
        if script
        else [sys.executable, "-m", "factlog.compile_facts", *args]
    )
    return subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True)


def _compiled(kb: Path) -> bool:
    return (kb / "facts" / "accepted.dl").is_file()


@pytest.fixture
def outside(tmp_path) -> Path:
    """A cwd that is deliberately not a KB — the #527 reproduction directory."""
    d = tmp_path / "outside"
    d.mkdir()
    return d


class TestConfigTier:
    def test_configured_kb_is_used_from_outside_any_kb(self, tmp_path, outside):
        """THE BUG: this run used to exit 1 on "not a factlog KB root"."""
        kb = _seed_kb(tmp_path / "kb")
        _write_config(kb)
        proc = _run(cwd=outside)
        assert proc.returncode == 0, proc.stderr
        assert _compiled(kb)
        assert "Alpha" in (kb / "facts" / "accepted.dl").read_text(encoding="utf-8")

    def test_stdout_format_is_unchanged(self, tmp_path, outside):
        # The compile log is read by finalize and shown by the skill; resolving a
        # root differently must not restyle it.
        kb = _seed_kb(tmp_path / "kb")
        _write_config(kb)
        proc = _run(cwd=outside)
        assert proc.returncode == 0, proc.stderr
        assert "engine facts: 1 / 1" in proc.stdout
        assert f"written: {kb / 'facts' / 'accepted.dl'}" in proc.stdout

    def test_tools_wrapper_resolves_the_same_root(self, tmp_path, outside):
        # SKILL.md Step 1 invokes tools/compile_facts.py, not the module, so the
        # documented entry point has to pick the config tier up too.
        kb = _seed_kb(tmp_path / "kb")
        _write_config(kb)
        proc = _run(cwd=outside, script=True)
        assert proc.returncode == 0, proc.stderr
        assert _compiled(kb)


class TestPrecedence:
    def test_flag_beats_env_and_config(self, tmp_path, outside):
        flag_kb = _seed_kb(tmp_path / "flag-kb")
        env_kb = _seed_kb(tmp_path / "env-kb")
        cfg_kb = _seed_kb(tmp_path / "config-kb")
        _write_config(cfg_kb)
        proc = _run("--target", str(flag_kb), cwd=outside, env_root=env_kb)
        assert proc.returncode == 0, proc.stderr
        assert _compiled(flag_kb)
        assert not _compiled(env_kb) and not _compiled(cfg_kb)

    def test_env_beats_config(self, tmp_path, outside):
        env_kb = _seed_kb(tmp_path / "env-kb")
        cfg_kb = _seed_kb(tmp_path / "config-kb")
        _write_config(cfg_kb)
        proc = _run(cwd=outside, env_root=env_kb)
        assert proc.returncode == 0, proc.stderr
        assert _compiled(env_kb) and not _compiled(cfg_kb)

    def test_config_beats_cwd(self, tmp_path):
        # cwd is itself a perfectly good KB here, so only the tier order can
        # decide which one gets compiled.
        cwd_kb = _seed_kb(tmp_path / "cwd-kb")
        cfg_kb = _seed_kb(tmp_path / "config-kb")
        _write_config(cfg_kb)
        proc = _run(cwd=cwd_kb)
        assert proc.returncode == 0, proc.stderr
        assert _compiled(cfg_kb) and not _compiled(cwd_kb)

    def test_wiki_is_an_accepted_alias(self, tmp_path, outside):
        # The sibling engine scripts spell the flag --wiki; both names must land
        # on the same root so a mixed invocation cannot compile the wrong KB.
        flag_kb = _seed_kb(tmp_path / "flag-kb")
        cfg_kb = _seed_kb(tmp_path / "config-kb")
        _write_config(cfg_kb)
        proc = _run("--wiki", str(flag_kb), cwd=outside)
        assert proc.returncode == 0, proc.stderr
        assert _compiled(flag_kb) and not _compiled(cfg_kb)


class TestBackwardCompatibility:
    def test_cwd_kb_still_compiles_with_no_config_and_no_flag(self, tmp_path):
        kb = _seed_kb(tmp_path / "kb")
        _write_config(None)
        proc = _run(cwd=kb)
        assert proc.returncode == 0, proc.stderr
        assert _compiled(kb)
        assert "engine facts: 1 / 1" in proc.stdout

    def test_outside_a_kb_with_no_config_still_fails_loudly(self, outside):
        # Without a configured KB there is nothing to fall back to, so the cwd
        # tier — and its error — must survive the fix.
        _write_config(None)
        proc = _run(cwd=outside)
        assert proc.returncode == 1
        assert "not a factlog KB root" in proc.stderr

    def test_a_dead_configured_root_reports_that_root(self, tmp_path, outside):
        # A stale config must not silently fall through to cwd: the failure names
        # the root that was actually resolved, which is what makes it fixable.
        _write_config(tmp_path / "deleted-kb")
        proc = _run(cwd=outside)
        assert proc.returncode == 1
        assert "not a factlog KB root" in proc.stderr


class TestFlagTypos:
    def test_a_misspelled_root_flag_is_rejected(self, tmp_path, outside):
        # Silently ignoring it would compile into the config/cwd tier instead —
        # the wrong KB, with a success exit code.
        kb = _seed_kb(tmp_path / "kb")
        cfg_kb = _seed_kb(tmp_path / "config-kb")
        _write_config(cfg_kb)
        proc = _run("--targt", str(kb), cwd=outside)
        assert proc.returncode == 2
        assert not _compiled(kb) and not _compiled(cfg_kb)
