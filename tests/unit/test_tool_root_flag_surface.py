# SPDX-License-Identifier: Apache-2.0
"""One KB-root flag surface across ``tools/`` (#533), with honest help (#531).

The scripts under ``tools/`` had four different ways of naming a KB — ``--wiki``,
``--target``, a positional, and nothing at all — so a caller had to read ``--help``
before every invocation and ``validate.py --wiki <kb>`` failed with "unrecognized
arguments". Every KB-taking script now accepts ``--target`` with ``--wiki`` as an
alias, resolves the documented four tiers, rejects a misspelled flag instead of
ignoring it, and refuses a blank one instead of dropping it to the next tier (#546).

The script list is DATA, not a set of hand-written cases: a script added to
``tools/`` later is covered the moment its name is added here, and — more to the
point — a script that quietly loses one half of the surface fails in the axis that
lost it rather than in whichever test happened to mention it.

Driven as subprocesses. What is under test is resolution that happens at *import*
time for most of these scripts (the pre-pass that exports ``FACTLOG_ROOT`` before
``common`` binds its module-level path globals), plus the exit codes the shell
harness and ``finalize`` read; neither survives an in-process ``main()`` call.
Every run gets its own ``$XDG_CONFIG_HOME`` and ``$HOME``, so no test here can read
or write the developer's real active KB.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools"


@dataclass(frozen=True)
class Tool:
    """A ``tools/`` script that takes a KB root, and how to invoke it minimally.

    *lead_args* are the arguments that must precede the root flag for the parser to
    accept the line at all (ask_router's subcommand and its positional). *resolves
    at import* distinguishes the scripts whose pre-pass exports ``FACTLOG_ROOT``
    before importing ``common`` from the ones that resolve inside ``main`` because
    they bind no path globals — the precedence axis observes those two differently.
    """

    module: str
    lead_args: tuple[str, ...] = ()
    resolves_at_import: bool = True

    @property
    def path(self) -> Path:
        return TOOLS / f"{self.module}.py"

    def __str__(self) -> str:  # readable pytest ids
        return self.module


# Every tools/ script that names a KB and that #533 unified. finalize and validate
# are here too although they already carried both spellings: the point of the issue
# is the surface as a whole, and a regression in the two that were right first is
# exactly as bad as one in the eight that were not.
KB_TOOLS = (
    Tool("ask_router", lead_args=("validate", "sibling(a, b).")),
    Tool("check_conflicts"),
    Tool("corroboration"),
    Tool("entity_audit"),
    Tool("finalize", resolves_at_import=False),
    Tool("generate_logic_policy"),
    Tool("merge_candidates"),
    Tool("resolve_stale_refs", resolves_at_import=False),
    Tool("review_candidates"),
    Tool("source_coverage"),
    Tool("validate", resolves_at_import=False),
    Tool("value_audit"),
)

# The scripts #533 gave the blank-value contract to. finalize and validate answer a
# blank flag differently (validate with its own "--target was empty", finalize by
# resolving on and letting the #532 guard refuse), so they are excluded from this
# axis rather than having their wording restated here.
BLANK_REFUSING = tuple(t for t in KB_TOOLS if t.module not in {"finalize", "validate"})

IMPORT_RESOLVERS = tuple(t for t in KB_TOOLS if t.resolves_at_import)


def run(tool: Tool, *args: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(tool.path), *tool.lead_args, *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )


def make_kb(root: Path) -> Path:
    """A structurally complete-enough KB: every tool here gets past its root gate."""
    for name in ("sources", "pages", "facts", "decisions", "runs", "policy"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "policy" / "prompts").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def sandbox(tmp_path: Path) -> dict[str, object]:
    """Four candidate KBs (one per precedence tier) plus an isolated config home.

    Returned as a dict rather than four fixtures because every test here needs the
    same set and the interesting assertions are about which of the four won.
    """
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    roots = {tier: make_kb(tmp_path / f"kb-{tier}") for tier in ("flag", "env", "config", "cwd")}
    env = {**os.environ}
    env.pop("FACTLOG_ROOT", None)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    return {"roots": roots, "env": env, "config_home": home / ".config"}


def set_active_kb(sandbox: dict, root: Path | None) -> None:
    cfg = Path(sandbox["config_home"]) / "factlog" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    if root is None:
        cfg.unlink(missing_ok=True)
        return
    cfg.write_text(json.dumps({"root": str(root)}) + "\n", encoding="utf-8")


class TestBothSpellingsAreAdvertised:
    """``--help`` is where a caller looks; both spellings have to be in it."""

    @pytest.mark.parametrize("tool", KB_TOOLS, ids=str)
    def test_help_lists_target_and_wiki(self, tool: Tool, sandbox: dict):
        # ask_router's root flag lives on the subcommands, so ask the subcommand.
        args = (*tool.lead_args[:1], "--help") if tool.module == "ask_router" else ("--help",)
        done = subprocess.run(
            [sys.executable, str(tool.path), *args],
            cwd=str(sandbox["roots"]["cwd"]),
            env=sandbox["env"],
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0, done.stderr
        assert "--target" in done.stdout, done.stdout
        assert "--wiki" in done.stdout, done.stdout

    @pytest.mark.parametrize("tool", KB_TOOLS, ids=str)
    def test_help_does_not_promise_a_cwd_default(self, tool: Tool, sandbox: dict):
        """#531: the help said the default was ``$FACTLOG_ROOT`` or ``'.'``.

        It never was, once the pre-pass landed: with neither flag nor env var the
        root is the active KB from ``factlog use``, and a caller who trusted the
        old sentence and ran from an unrelated directory got that KB instead of the
        one they were standing in. Pinned as the absence of the claim, not as the
        presence of one wording, so the sentence stays editable.
        """
        args = (*tool.lead_args[:1], "--help") if tool.module == "ask_router" else ("--help",)
        done = subprocess.run(
            [sys.executable, str(tool.path), *args],
            cwd=str(sandbox["roots"]["cwd"]),
            env=sandbox["env"],
            capture_output=True,
            text=True,
        )
        collapsed = " ".join(done.stdout.split())
        assert "$FACTLOG_ROOT or '.'" not in collapsed, collapsed


class TestTypoIsRejected:
    @pytest.mark.parametrize("tool", KB_TOOLS, ids=str)
    def test_misspelled_flag_exits_2(self, tool: Tool, sandbox: dict):
        """A near-miss must not be silently ignored.

        Both halves matter. argparse's exit 2 is the loud part; the quiet part is
        that a pre-pass reading ``--target`` while the strict parser did not declare
        it (or the reverse) would let ``--targt /path`` through as "no flag given"
        and run against whatever the config tier resolved to — the intended KB
        untouched, an unrelated one rewritten, exit 0.
        """
        roots = sandbox["roots"]
        set_active_kb(sandbox, roots["config"])
        done = run(tool, "--targt", str(roots["flag"]), cwd=roots["cwd"], env=sandbox["env"])
        assert done.returncode == 2, (done.returncode, done.stdout, done.stderr)
        assert "--targt" in done.stderr


class TestBlankFlagIsRefused:
    """``--target ""`` is refused, not dropped to the next tier (#546).

    This is the shape SKILL.md produces: it documents
    ``merge_candidates.py --wiki "$FACTLOG_ROOT"``, and in a shell that never
    exported the variable that is exactly ``--wiki ""``. Truthiness in
    ``factlog.config.resolve_root`` then made the blank value skip its own tier,
    so a caller who believed they had named a KB silently got the configured one.
    """

    @pytest.mark.parametrize("tool", BLANK_REFUSING, ids=str)
    @pytest.mark.parametrize("spelling", ("--target", "--wiki"))
    def test_blank_value_exits_1(self, tool: Tool, spelling: str, sandbox: dict):
        roots = sandbox["roots"]
        set_active_kb(sandbox, roots["config"])
        done = run(tool, spelling, "", cwd=roots["cwd"], env=sandbox["env"])
        assert done.returncode == 1, (done.returncode, done.stdout, done.stderr)
        combined = done.stdout + done.stderr
        # One sentence for all of them: a caller who hits this in one script should
        # not have to learn a second phrasing for the next.
        assert "the KB-root flag (--target/--wiki) was empty" in combined, combined
        # The refusal has to name a way out, or it is just a failure.
        assert "pass a KB path" in combined, combined

    @pytest.mark.parametrize("tool", BLANK_REFUSING, ids=str)
    def test_blank_value_does_not_reach_the_configured_kb(self, tool: Tool, sandbox: dict):
        """The refusal happens before the tool does anything to a KB.

        The bug was not only the exit code: ``merge_candidates --wiki ""`` wrote
        ``facts/candidates.csv`` under cwd while announcing the *config* KB as its
        target. Nothing may be created in either KB on this path.
        """
        roots = sandbox["roots"]
        set_active_kb(sandbox, roots["config"])
        run(tool, "--wiki", "", cwd=roots["cwd"], env=sandbox["env"])
        for tier in ("config", "cwd"):
            assert not (roots[tier] / "facts" / "candidates.csv").exists()
            assert not (roots[tier] / "policy" / "logic-policy.dl").exists()


class TestPrecedence:
    """flag > $FACTLOG_ROOT > active-KB config > cwd, for every script.

    Observed through the ``FACTLOG_ROOT`` the pre-pass exports before importing
    ``common`` — the value every path global in the run is derived from — rather
    than through each tool's own report, which would only pin the tools that
    happen to print their target.
    """

    @staticmethod
    def resolved_root(tool: Tool, argv: list[str], cwd: Path, env: dict[str, str]) -> str:
        code = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(TOOLS)!r})\n"
            f"sys.argv = {[f'{tool.module}.py', *argv]!r}\n"
            f"import {tool.module}\n"
            "print(os.environ['FACTLOG_ROOT'])\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", code], cwd=str(cwd), env=env, capture_output=True, text=True
        )
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    @pytest.mark.parametrize("tool", IMPORT_RESOLVERS, ids=str)
    @pytest.mark.parametrize("spelling", ("--target", "--wiki"))
    def test_flag_wins_over_every_other_tier(self, tool: Tool, spelling: str, sandbox: dict):
        roots, env = sandbox["roots"], dict(sandbox["env"])
        set_active_kb(sandbox, roots["config"])
        env["FACTLOG_ROOT"] = str(roots["env"])
        got = self.resolved_root(tool, [spelling, str(roots["flag"])], roots["cwd"], env)
        assert got == str(roots["flag"])

    @pytest.mark.parametrize("tool", IMPORT_RESOLVERS, ids=str)
    def test_env_wins_over_config_and_cwd(self, tool: Tool, sandbox: dict):
        roots, env = sandbox["roots"], dict(sandbox["env"])
        set_active_kb(sandbox, roots["config"])
        env["FACTLOG_ROOT"] = str(roots["env"])
        assert self.resolved_root(tool, [], roots["cwd"], env) == str(roots["env"])

    @pytest.mark.parametrize("tool", IMPORT_RESOLVERS, ids=str)
    def test_config_wins_over_cwd(self, tool: Tool, sandbox: dict):
        roots = sandbox["roots"]
        set_active_kb(sandbox, roots["config"])
        assert self.resolved_root(tool, [], roots["cwd"], sandbox["env"]) == str(roots["config"])

    @pytest.mark.parametrize("tool", IMPORT_RESOLVERS, ids=str)
    def test_cwd_is_the_last_resort(self, tool: Tool, sandbox: dict):
        roots = sandbox["roots"]
        set_active_kb(sandbox, None)
        assert self.resolved_root(tool, [], roots["cwd"], sandbox["env"]) == str(roots["cwd"])

    @pytest.mark.parametrize("spelling", ("--target", "--wiki"))
    def test_resolve_stale_refs_follows_the_same_tiers(self, spelling: str, sandbox: dict):
        """The one script that resolves in ``main`` instead of a pre-pass.

        It binds no path globals from ``common``, so there is no ``FACTLOG_ROOT``
        export to observe. Each candidate KB is given a stale-ref record naming a
        different page, and the reported page says which KB was read.
        """
        roots, env = sandbox["roots"], dict(sandbox["env"])
        for tier, root in roots.items():
            (root / "decisions" / "open-questions.md").write_text(
                f"- stale_source: pages/{tier}.md references removed source sources/x.md\n",
                encoding="utf-8",
            )
        set_active_kb(sandbox, roots["config"])
        env["FACTLOG_ROOT"] = str(roots["env"])
        tool = Tool("resolve_stale_refs", resolves_at_import=False)

        flagged = run(tool, spelling, str(roots["flag"]), cwd=roots["cwd"], env=env)
        assert "pages/flag.md" in flagged.stdout, flagged.stdout

        env_only = run(tool, cwd=roots["cwd"], env=env)
        assert "pages/env.md" in env_only.stdout, env_only.stdout

        del env["FACTLOG_ROOT"]
        config_only = run(tool, cwd=roots["cwd"], env=env)
        assert "pages/config.md" in config_only.stdout, config_only.stdout

        set_active_kb(sandbox, None)
        cwd_only = run(tool, cwd=roots["cwd"], env=env)
        assert "pages/cwd.md" in cwd_only.stdout, cwd_only.stdout


class TestGenerateLogicPolicyOutsideAKb:
    """#533's concrete failure: the skill calls this script with no arguments.

    ``skills/factlog/SKILL.md`` and ``tools/finalize.py`` both invoke
    ``generate_logic_policy.py`` bare. Without the config tier it resolved to cwd,
    so the documented form died with "not a factlog KB root" for exactly the user
    the active-KB config exists for — one who ran ``factlog use`` once and is no
    longer standing in the KB.
    """

    def test_bare_run_outside_a_kb_uses_the_active_kb(self, sandbox: dict, tmp_path: Path):
        roots, env = sandbox["roots"], sandbox["env"]
        kb = roots["config"]
        (kb / "policy" / "logic-policy.md").write_text("# Logic policy\n\nNo rules yet.\n", encoding="utf-8")
        set_active_kb(sandbox, kb)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        done = subprocess.run(
            [sys.executable, str(TOOLS / "generate_logic_policy.py"), "--check"],
            cwd=str(elsewhere),
            env=env,
            capture_output=True,
            text=True,
        )
        assert "not a factlog KB root" not in done.stdout + done.stderr
        assert done.returncode == 0, (done.stdout, done.stderr)


class TestMergeCandidatesResolvesOnce:
    """#546: the pre-pass and ``main`` must agree on the target.

    ``main`` re-read ``args.wiki`` and resolved it a second time. A blank value is
    where the two answers came apart: the pre-pass (truthiness) took the config
    tier, ``Path("").resolve()`` took cwd — so the guard judged 'config' against a
    cwd path, let the write through, and the announcement printed the cwd path
    labelled ``(from config)``.
    """

    def test_blank_flag_neither_writes_cwd_nor_claims_config(self, sandbox: dict):
        roots = sandbox["roots"]
        set_active_kb(sandbox, roots["config"])
        done = run(
            Tool("merge_candidates"), "--wiki", "", cwd=roots["cwd"], env=sandbox["env"]
        )
        assert done.returncode == 1, (done.stdout, done.stderr)
        assert not (roots["cwd"] / "facts" / "candidates.csv").exists()
        # The false provenance is the second half of the bug: a cwd path may never
        # be announced as having come from the config tier.
        assert f"{roots['cwd']} (from config)" not in done.stdout

    def test_announced_target_is_the_one_written(self, sandbox: dict):
        """The positive case the blank one broke: label and path describe one KB."""
        roots = sandbox["roots"]
        set_active_kb(sandbox, roots["config"])
        done = run(
            Tool("merge_candidates"), "--target", str(roots["flag"]), cwd=roots["cwd"], env=sandbox["env"]
        )
        assert f"merge_candidates: target KB {roots['flag']} (from flag)" in done.stdout
        assert (roots["flag"] / "facts" / "candidates.csv").is_file()
        assert not (roots["cwd"] / "facts" / "candidates.csv").exists()
        assert not (roots["config"] / "facts" / "candidates.csv").exists()
