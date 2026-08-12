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


class TestATargetThatIsAFileIsRefusedInWords:
    """A file where the KB should go must be reported, not crash the command.

    ``_init_kb`` calls ``mkdir`` on ``<target>/sources`` and a regular file at
    ``<target>`` makes that raise ``NotADirectoryError``, which reaches the top
    level as a stack trace. The implicit case is the worse one: the traceback
    prints *before* the "no --target given; using …" line, so the user sees a
    crash with nothing naming which source picked the path.

    The guard therefore sits where resolution ends and before the explicit
    target returns early — one placement covering both entry points and all four
    sources, rather than only the case the user could already see.
    """

    def test_an_explicit_file_target_is_refused(self, tmp_path, home):
        occupied = tmp_path / "notes.md"
        occupied.write_text("mine\n", encoding="utf-8")

        proc = run_init("--target", str(occupied), home=home)

        assert proc.returncode != 0, proc.stdout
        assert "Traceback" not in proc.stderr, f"crashed instead of reporting: {proc.stderr}"
        assert "NotADirectoryError" not in proc.stderr, proc.stderr
        assert "not a directory" in proc.stderr, f"the reason is not stated: {proc.stderr}"
        assert str(occupied) in proc.stderr, f"the offending path is not named: {proc.stderr}"

    def test_a_file_reached_through_factlog_root_names_its_source(self, tmp_path, home):
        """The implicit half — and the half that printed a trace with no clue."""
        occupied = tmp_path / "notes.md"
        occupied.write_text("mine\n", encoding="utf-8")

        proc = run_init(home=home, factlog_root=occupied)

        assert proc.returncode != 0, proc.stdout
        assert "Traceback" not in proc.stderr, f"crashed instead of reporting: {proc.stderr}"
        assert "not a directory" in proc.stderr, f"the reason is not stated: {proc.stderr}"
        assert "$FACTLOG_ROOT" in proc.stderr, (
            f"the user cannot tell which source chose this path: {proc.stderr}"
        )


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


class TestTheChainItselfIsShared:
    """The precedence must live in one place, not be re-implemented per command.

    ``factlog/config.py`` declares in its module docstring that it owns this
    order "so both factlog/cli.py and every tool's pre-import root resolver can
    share it". ``init``/``setup`` used to hand-roll their own copy of it, and a
    hand-rolled copy is invisible to every test above: those drive the chain from
    the outside, so a rank added to ``resolve_root`` and ignored by ``init``
    looks exactly like a rank that was never added.

    So this pins the *wiring*, not the order. It is the reviewer's drift
    experiment turned into a test: give ``resolve_root`` a rank nothing else
    knows about and require ``init``'s resolution to see it.
    """

    def test_init_resolution_goes_through_config_resolve_root(self, tmp_path, monkeypatch):
        """A rank only ``resolve_root`` knows about still reaches ``init``.

        Red before the delegation: the hand-rolled chain read ``$FACTLOG_ROOT``
        and ``read_root()`` itself, so it returned the environment KB and never
        called the patched resolver at all.
        """
        from factlog import cli as factlog_cli

        env_kb = tmp_path / "env-kb"
        newer_rank = tmp_path / "newer-rank-kb"
        monkeypatch.setenv("FACTLOG_ROOT", str(env_kb))
        calls: list[tuple] = []

        def resolve_root(cli_value=None, *, fallback=None):
            # Stands in for a rank added between env and config later — the shape
            # of change this test exists to keep honest.
            calls.append((cli_value, fallback))
            return str(newer_rank), "config"

        monkeypatch.setattr(factlog_cli.factlog_config, "resolve_root", resolve_root)

        target = factlog_cli._resolve_kb_target(None, "factlog init")

        assert calls == [(None, "~/wiki")], (
            f"init did not ask config.resolve_root for the target: {calls}"
        )
        assert target == newer_rank, (
            f"init resolved {target}, not the {newer_rank} the shared chain returned"
        )

    def test_an_unknown_rank_raises_instead_of_leaking_its_token(self, tmp_path, monkeypatch):
        """The point of sharing the chain is that a new rank cannot be ignored.

        A ``.get(origin, origin)`` fallthrough let it be ignored anyway, just
        more quietly: ``init`` would resolve the target correctly and then print
        the internal token — "(from localrc)" — inside a sentence written for
        human-readable source names, with nothing failing. Indexing turns the one
        thing this class exists to prevent into a crash at the point of the
        omission, which is where the person adding the rank is standing.
        """
        from factlog import cli as factlog_cli

        def resolve_root(cli_value=None, *, fallback=None):
            return str(tmp_path / "a-rank-nobody-labelled"), "localrc"

        monkeypatch.setattr(factlog_cli.factlog_config, "resolve_root", resolve_root)

        with pytest.raises(KeyError):
            factlog_cli._resolve_kb_target(None, "factlog init")

    def test_an_unknown_rank_pointing_at_a_file_raises_too(self, tmp_path, monkeypatch):
        """The label is read in two places; the test above only reaches one.

        Because that one resolves to a path which does not exist, it walks past
        the existing-file guard and trips the announce-line lookup. Reverting
        *only* the guard's lookup to a fallthrough therefore left the whole suite
        green — a surviving mutant, and on the branch that builds a `FactlogError`
        message, where a leaked token is user-facing text rather than a log line.
        """
        from factlog import cli as factlog_cli

        occupied = tmp_path / "notes.md"
        occupied.write_text("mine\n", encoding="utf-8")

        def resolve_root(cli_value=None, *, fallback=None):
            return str(occupied), "localrc"

        monkeypatch.setattr(factlog_cli.factlog_config, "resolve_root", resolve_root)

        with pytest.raises(KeyError):
            factlog_cli._resolve_kb_target(None, "factlog init")

    def test_the_fallback_is_passed_rather_than_applied_afterwards(self, tmp_path, monkeypatch):
        """``~/wiki`` is the chain's last resort, not a patch over its cwd result.

        Red before the delegation for the same reason, and separately worth
        pinning: applying the default *after* calling ``resolve_root`` would
        agree with the test above while still leaving two chains — the shared one
        would answer ``cwd`` and be overruled here.
        """
        from factlog import cli as factlog_cli

        seen: list[str | None] = []

        def resolve_root(cli_value=None, *, fallback=None):
            seen.append(fallback)
            return str(tmp_path / "answered"), "default"

        monkeypatch.setattr(factlog_cli.factlog_config, "resolve_root", resolve_root)

        factlog_cli._resolve_kb_target(None, "factlog init")

        assert seen == ["~/wiki"], f"the ~/wiki fallback did not reach the chain: {seen}"


class TestResolveRootFallbackIsOptIn:
    """``fallback`` must not change what existing callers get."""

    def test_without_fallback_the_last_resort_is_still_cwd(self, tmp_path, monkeypatch):
        """GUARD, not evidence: passes before and after. Every other command
        relies on the cwd fallback, so opting one pair out must not move it."""
        from factlog import config as factlog_config

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-cfg"))
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)

        assert factlog_config.resolve_root()[1] == "cwd"

    def test_with_fallback_the_last_resort_is_named_default(self, tmp_path, monkeypatch):
        from factlog import config as factlog_config

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-cfg"))
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)
        fallback = tmp_path / "fallback-kb"

        root, source = factlog_config.resolve_root(fallback=str(fallback))

        assert source == "default"
        assert root == str(fallback.resolve())

    def test_fallback_does_not_outrank_the_config(self, tmp_path, monkeypatch):
        """The new argument is a *last* resort, not a new rank above the config."""
        from factlog import config as factlog_config

        cfg_home = tmp_path / "cfg"
        configured = tmp_path / "configured-kb"
        configured.mkdir()
        write_pointer(cfg_home, configured)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)

        root, source = factlog_config.resolve_root(fallback=str(tmp_path / "fallback-kb"))

        assert (root, source) == (str(configured.resolve()), "config")


class TestSkillMdQuotesTheRealFallback:
    """SKILL.md tells an assistant what these commands do; it must stay true.

    The line it pins is a statement about a constant rather than a transcript of
    output, so it cannot drift as the code around it moves — with one exception,
    which is the whole reason this exists: changing ``_DEFAULT_KB`` makes the
    skill file silently false, and silently false is exactly what this branch
    spent a commit closing on the ``.get(origin, origin)`` fallthrough. The
    human-facing page states the same chain in prose; this is the machine-facing
    half, and the half an LLM acts on.
    """

    def test_the_skill_file_names_the_fallback_the_code_uses(self):
        from factlog import cli as factlog_cli

        text = (REPO_ROOT / "skills" / "factlog" / "SKILL.md").read_text(encoding="utf-8")

        assert f"their fallback is `{factlog_cli._DEFAULT_KB}`, not cwd" in text, (
            "skills/factlog/SKILL.md no longer names the fallback init/setup actually use "
            f"({factlog_cli._DEFAULT_KB})"
        )
