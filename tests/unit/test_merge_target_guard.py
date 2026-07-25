# SPDX-License-Identifier: Apache-2.0
"""Which KB a bare ``merge_candidates.py`` is allowed to write (#532).

The script resolves its root through the active-KB config, which its sibling write
steps (``compile_facts.py``, ``run_logic_check.py``, ``finalize.py``) do not — so a
bare run from an unrelated directory rewrote the configured KB's
``facts/candidates.csv`` and ``pages/`` and exited 0, flipping that KB's logic
report to STALE. The siblings all exit 1 there.

Driven as a subprocess rather than by calling ``main()``: what is under test is the
resolution that happens at *import* time (the ``--wiki`` prepass and the
``FACTLOG_ROOT`` export before ``common`` binds its path globals), and the exit code
the shell harness and ``finalize`` see. Every run gets its own
``$XDG_CONFIG_HOME``, so no test can read — or write — the developer's real active
KB.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MERGE = REPO_ROOT / "tools" / "merge_candidates.py"

FACT_ROW = {
    "subject": "인공지능",
    "relation": "포함",
    "object": "기계학습",
    "source": "sources/a.md",
    "status": "candidate",
    "confidence": "0.90",
    "note": "",
}


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    """A minimal populated KB: enough structure for a merge to complete."""
    root = tmp_path / "kb"
    for name in ("sources", "pages", "facts", "decisions", "policy", "runs"):
        (root / name).mkdir(parents=True)
    (root / "sources" / "a.md").write_text("인공지능은 기계학습을 포함한다.\n", encoding="utf-8")
    (root / "runs" / "r.json").write_text(json.dumps([FACT_ROW], ensure_ascii=False), encoding="utf-8")
    return root


@pytest.fixture
def elsewhere(tmp_path: Path) -> Path:
    """A directory that is emphatically not a KB — the ``/tmp`` of the report."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    return other


@pytest.fixture
def active_kb(kb: Path, tmp_path: Path, monkeypatch) -> Path:
    """Point this test's ``$XDG_CONFIG_HOME`` at a config naming *kb* as active."""
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    cfg = config_home / "factlog" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"root": str(kb)}) + "\n", encoding="utf-8")
    return kb


def run_merge(*args: str, cwd: Path, root_env: Path | None = None) -> subprocess.CompletedProcess:
    """Run the script with a clean ``$FACTLOG_ROOT`` (conftest pins one for imports)."""
    env = {**os.environ}
    env.pop("FACTLOG_ROOT", None)
    if root_env is not None:
        env["FACTLOG_ROOT"] = str(root_env)
    return subprocess.run(
        [sys.executable, str(MERGE), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )


def candidates(root: Path) -> str | None:
    path = root / "facts" / "candidates.csv"
    return path.read_text(encoding="utf-8") if path.is_file() else None


class TestImplicitConfigTarget:
    def test_bare_run_outside_the_kb_refuses(self, active_kb: Path, elsewhere: Path):
        # THE BUG: rc=0 and the configured KB rewritten from an unrelated cwd.
        result = run_merge(cwd=elsewhere)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "REFUSING" in result.stderr

    def test_refusal_writes_nothing(self, active_kb: Path, elsewhere: Path):
        # The exit code is only half of it — the damage was the write itself, and
        # re-writing an identical candidates.csv still invalidates the logic report.
        assert candidates(active_kb) is None
        run_merge(cwd=elsewhere)
        assert candidates(active_kb) is None
        assert list((active_kb / "pages").iterdir()) == []

    def test_refusal_names_the_target_and_both_escapes(self, active_kb: Path, elsewhere: Path):
        result = run_merge(cwd=elsewhere)
        assert f"merge_candidates: target KB {active_kb} (from config)" in result.stdout
        assert f"--wiki {active_kb}" in result.stderr
        assert f"FACTLOG_ROOT={active_kb}" in result.stderr

    def test_bare_run_outside_a_kb_with_no_config_still_refuses(self, elsewhere: Path, tmp_path: Path, monkeypatch):
        # Sibling parity, from the other direction: with nothing configured the root
        # is the cwd, and a cwd that is not a KB is rejected as it always was.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
        result = run_merge(cwd=elsewhere)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "not a factlog KB root" in result.stderr


class TestNamedTargetsStillRun:
    def test_bare_run_inside_the_kb_passes(self, active_kb: Path):
        # The documented workflow: stand in the KB, run the merge with no flags.
        result = run_merge(cwd=active_kb)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "기계학습" in (candidates(active_kb) or "")

    def test_bare_run_in_a_subdirectory_of_the_kb_passes(self, active_kb: Path):
        # Still inside the configured KB, so the target is not one nobody named.
        result = run_merge(cwd=active_kb / "sources")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "기계학습" in (candidates(active_kb) or "")

    def test_explicit_wiki_flag_from_outside_passes(self, active_kb: Path, elsewhere: Path):
        # How SKILL.md, finalize.py and every shell harness call this script.
        result = run_merge("--wiki", str(active_kb), cwd=elsewhere)
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"merge_candidates: target KB {active_kb} (from flag)" in result.stdout
        assert "기계학습" in (candidates(active_kb) or "")

    def test_factlog_root_env_from_outside_passes(self, active_kb: Path, elsewhere: Path):
        result = run_merge(cwd=elsewhere, root_env=active_kb)
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"merge_candidates: target KB {active_kb} (from env)" in result.stdout
        assert "기계학습" in (candidates(active_kb) or "")

    def test_wiki_flag_wins_over_a_different_configured_kb(self, active_kb: Path, tmp_path: Path, elsewhere: Path):
        # An explicit target is authoritative even when a *different* KB is active:
        # the guard must gate on provenance, not on "is this the configured root".
        other = tmp_path / "other-kb"
        for name in ("sources", "pages", "facts", "decisions", "policy", "runs"):
            (other / name).mkdir(parents=True)
        result = run_merge("--wiki", str(other), cwd=elsewhere)
        assert result.returncode == 0, result.stdout + result.stderr
        assert candidates(other) is not None
        assert candidates(active_kb) is None
