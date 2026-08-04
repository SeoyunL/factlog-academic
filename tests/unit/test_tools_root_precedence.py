# SPDX-License-Identifier: Apache-2.0
"""Root-resolution precedence for every bundled ``tools/`` entry point (#330).

The documented precedence (SKILL.md, ``factlog/config.py``, README) is

    --wiki/--target flag (or validate's positional)  >  $FACTLOG_ROOT
        >  active-KB config file  >  cwd

Seven tools used to skip the config step and fall straight through to cwd, so
``factlog use <kb>`` was honoured by the CLI but not by half of ``tools/``.
These tests run each script as a real subprocess and pin all four levels.

How a tool's resolved root is observed: every case runs against two roots — a
real ``factlog init`` KB and an EMPTY decoy directory — chosen so the tool's
output is unambiguous about which one it used. ``decoy`` is the message the tool
emits when it lands on the empty dir; ``kb`` is a message only reachable from
the initialised KB. Asserting both directions means an unrelated failure (e.g.
argparse rejecting a flag) cannot make a case pass by accident.

XDG_CONFIG_HOME and HOME are redirected per case, so a developer's real
~/.config/factlog/config.json is never read or written.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"


class ToolSpec:
    """How to point *name* at a KB, and how to tell which KB it used."""

    def __init__(self, name: str, flag: str | None, decoy: str, kb: str) -> None:
        self.name = name
        self.flag = flag  # None -> the root is a positional argument
        self.decoy = decoy
        self.kb = kb

    def point_at(self, root: Path) -> list[str]:
        return [self.flag, str(root)] if self.flag else [str(root)]

    def __repr__(self) -> str:  # pragma: no cover - pytest id only
        return self.name


TOOLS = [
    ToolSpec(
        "compile_facts",
        "--wiki",
        decoy="not a factlog KB root",
        kb="missing facts/candidates.csv",
    ),
    ToolSpec(
        "run_logic_check",
        "--wiki",
        decoy="not a factlog KB root",
        kb="missing facts/accepted.dl",
    ),
    ToolSpec(
        "review_candidates",
        "--wiki",
        decoy="not a factlog KB root",
        kb="missing facts/candidates.csv",
    ),
    ToolSpec(
        "generate_logic_policy",
        "--wiki",
        decoy="not a factlog KB root",
        kb="no compilable policies",
    ),
    ToolSpec(
        "validate",
        None,  # positional [root]
        decoy="missing directory: sources/",
        kb="decisions/open-questions.md should keep",
    ),
    ToolSpec(
        "finalize",
        "--target",
        decoy="is not a factlog KB (no sources/)",
        kb="candidate rows loaded",
    ),
    ToolSpec(
        "resolve_stale_refs",
        "--wiki",
        decoy="missing decisions/open-questions.md",
        kb="no stale_source records found",
    ),
]


@pytest.fixture(scope="session")
def kb_template(tmp_path_factory) -> Path:
    """A `factlog init` KB, built once and copied per test.

    ``factlog init`` itself records an active KB, so it runs under a throwaway
    XDG_CONFIG_HOME that no test ever reads.
    """
    base = tmp_path_factory.mktemp("kbtpl")
    template = base / "kb"
    env = _base_env(base / "xdg-init", base / "home-init")
    proc = subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(template)],
        cwd=str(base),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # resolve_stale_refs reads this file; an empty one is enough to distinguish
    # "landed on the KB" (no stale records) from "landed on the empty decoy"
    # (file missing).
    (template / "decisions").mkdir(parents=True, exist_ok=True)
    (template / "decisions" / "open-questions.md").write_text("", encoding="utf-8")
    return template


def _base_env(xdg: Path, home: Path) -> dict[str, str]:
    xdg.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(xdg),
        "PYTHONPATH": str(REPO_ROOT),
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
    }
    return env


@pytest.fixture
def sandbox(tmp_path, kb_template):
    """kb (real KB), decoy/decoy2 (empty dirs), and a tool runner."""
    kb = tmp_path / "kb"
    shutil.copytree(kb_template, kb)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    decoy2 = tmp_path / "decoy2"
    decoy2.mkdir()

    def run(spec: ToolSpec, *, args=(), cwd: Path, env_root: Path | None = None,
            config_root: Path | None = None) -> str:
        xdg = tmp_path / "xdg"
        if xdg.exists():
            shutil.rmtree(xdg)
        env = _base_env(xdg, tmp_path / "home")
        if config_root is not None:
            cfg = xdg / "factlog" / "config.json"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(json.dumps({"root": str(config_root)}), encoding="utf-8")
        if env_root is not None:
            env["FACTLOG_ROOT"] = str(env_root)
        proc = subprocess.run(
            [sys.executable, str(TOOLS_DIR / f"{spec.name}.py"), *args],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        return proc.stdout + proc.stderr

    return type(
        "Sandbox", (), {"kb": kb, "decoy": decoy, "decoy2": decoy2, "run": staticmethod(run)}
    )()


def _assert_used_kb(spec: ToolSpec, out: str) -> None:
    assert spec.kb in out, f"{spec.name}: expected KB marker {spec.kb!r} in output:\n{out}"
    assert spec.decoy not in out, f"{spec.name}: fell back to the decoy root:\n{out}"


def _assert_used_decoy(spec: ToolSpec, out: str) -> None:
    assert spec.decoy in out, f"{spec.name}: expected decoy marker {spec.decoy!r} in output:\n{out}"


@pytest.mark.parametrize("spec", TOOLS, ids=lambda s: s.name)
def test_flag_beats_env_and_config(spec, sandbox):
    """Level 1: an explicit --wiki/--target/positional wins over env and config."""
    out = sandbox.run(
        spec,
        args=spec.point_at(sandbox.kb),
        cwd=sandbox.decoy,
        env_root=sandbox.decoy,
        config_root=sandbox.decoy2,
    )
    _assert_used_kb(spec, out)


@pytest.mark.parametrize("spec", TOOLS, ids=lambda s: s.name)
def test_env_beats_config(spec, sandbox):
    """Level 2: $FACTLOG_ROOT wins over the active-KB config file."""
    out = sandbox.run(spec, cwd=sandbox.decoy, env_root=sandbox.kb, config_root=sandbox.decoy2)
    _assert_used_kb(spec, out)


@pytest.mark.parametrize("spec", TOOLS, ids=lambda s: s.name)
def test_config_used_when_env_unset(spec, sandbox):
    """Level 3: the active-KB config is honoured from a cwd that is not a KB.

    This is the #330 defect: without it the tools fell straight through to cwd.
    """
    out = sandbox.run(spec, cwd=sandbox.decoy, config_root=sandbox.kb)
    _assert_used_kb(spec, out)


@pytest.mark.parametrize("spec", TOOLS, ids=lambda s: s.name)
def test_config_beats_cwd(spec, sandbox):
    """Level 3 vs 4: a configured root wins even when cwd IS a valid KB."""
    out = sandbox.run(spec, cwd=sandbox.kb, config_root=sandbox.decoy)
    _assert_used_decoy(spec, out)


@pytest.mark.parametrize("spec", TOOLS, ids=lambda s: s.name)
def test_cwd_fallback_unchanged(spec, sandbox):
    """Level 4 regression guard (NOT a pin — this passed before the fix too).

    With no flag, no $FACTLOG_ROOT and no config file, cwd must still win.
    """
    out = sandbox.run(spec, cwd=sandbox.kb)
    _assert_used_kb(spec, out)
