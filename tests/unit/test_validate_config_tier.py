# SPDX-License-Identifier: Apache-2.0
"""validate.py resolves its target like its siblings, and refuses a non-KB (#530).

Two failures lived in the old ``root`` positional. It defaulted to ``"."``, so a run
with no argument from anywhere outside a KB validated the current directory — and
because a non-KB simply fails the ordinary checks, the operator got a full
``Fact sync validation failed: - missing directory: sources/ ...`` report about a
directory that was never their KB. That reads as "my KB is broken", which is worse
than the sibling tools' flat refusal. The second failure is that the active-KB config
was invisible here: ``factlog use`` pointed every other tool at a KB and this one
still looked at cwd.

So these pin the resolution tiers (flag > argument > $FACTLOG_ROOT > config > cwd),
all three target surfaces (``--target``, ``--wiki``, positional — the positional is
what tests/*.sh and merge_candidates' delegate call), and that a target which is not
a KB is refused by name instead of being reported on.

Two more shapes of the same misreading are pinned here. A *failing* report has to name
the KB it looked at (the success line always did): with the config tier in play a run
from inside KB-A can report on KB-B, and an unnamed failure list reads as A's. And an
**empty** target must be refused, not fall through to the next tier: the shell
harnesses call ``validate.py "$KB"``, so an unset ``$KB`` would otherwise stop meaning
"the sandbox cwd" and start meaning "the developer's active KB".

``validate.validate`` is stubbed in the in-process tests: what is under test there is
which directory the tool aims at and whether it agrees to look, not the checks
themselves. ``TestProcessLevel`` runs the real script in a subprocess, because a stub
cannot catch a regression in argparse wiring or in the exit code itself.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import validate

from factlog import config as factlog_config


def _kb(path):
    """A directory that reads as a KB to the guard (sources/ present)."""
    (path / "sources").mkdir(parents=True, exist_ok=True)
    return path


def _run(monkeypatch, argv, *, targets):
    """Run ``main()`` with *argv*, recording the root the validator was handed."""
    monkeypatch.setattr(validate, "validate", lambda root: targets.append(root) or [])
    monkeypatch.setattr(validate, "front_matter_warnings", lambda root: [])
    monkeypatch.setattr(validate, "review_section_warnings", lambda root: [])
    monkeypatch.setattr("sys.argv", ["validate.py", *argv])
    return validate.main()


class TestTargetSurfaces:
    """--target is the sibling spelling, --wiki its alias, positional the old one."""

    def test_target_flag(self, tmp_path, monkeypatch):
        kb = _kb(tmp_path / "kb")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        targets: list = []
        assert _run(monkeypatch, ["--target", str(kb)], targets=targets) == 0
        assert targets == [kb]

    def test_wiki_alias(self, tmp_path, monkeypatch):
        kb = _kb(tmp_path / "kb")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        targets: list = []
        assert _run(monkeypatch, ["--wiki", str(kb)], targets=targets) == 0
        assert targets == [kb]

    def test_positional_still_works(self, tmp_path, monkeypatch):
        """Back-compat: the shell harness and merge_candidates pass the KB this way."""
        kb = _kb(tmp_path / "kb")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        targets: list = []
        assert _run(monkeypatch, [str(kb)], targets=targets) == 0
        assert targets == [kb]


class TestConfigTier:
    """The active KB is adopted, and each tier out-ranks the one below it."""

    def test_config_kb_is_used_when_no_argument(self, tmp_path, monkeypatch):
        """The #530 headline: run bare from a non-KB and the *configured* KB is checked."""
        kb = _kb(tmp_path / "configured")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        factlog_config.write_root(kb)
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        targets: list = []
        assert _run(monkeypatch, [], targets=targets) == 0
        assert targets == [kb]

    def test_config_beats_cwd(self, tmp_path, monkeypatch):
        kb = _kb(tmp_path / "configured")
        cwd = _kb(tmp_path / "cwd")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        factlog_config.write_root(kb)
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        monkeypatch.chdir(cwd)
        targets: list = []
        assert _run(monkeypatch, [], targets=targets) == 0
        assert targets == [kb]

    def test_env_beats_config(self, tmp_path, monkeypatch):
        kb = _kb(tmp_path / "configured")
        env_kb = _kb(tmp_path / "env")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        factlog_config.write_root(kb)
        monkeypatch.setenv("FACTLOG_ROOT", str(env_kb))
        targets: list = []
        assert _run(monkeypatch, [], targets=targets) == 0
        assert targets == [env_kb]

    def test_argument_beats_env(self, tmp_path, monkeypatch):
        """The positional keeps out-ranking the environment, as callers relied on."""
        env_kb = _kb(tmp_path / "env")
        arg_kb = _kb(tmp_path / "arg")
        monkeypatch.setenv("FACTLOG_ROOT", str(env_kb))
        targets: list = []
        assert _run(monkeypatch, [str(arg_kb)], targets=targets) == 0
        assert targets == [arg_kb]

    def test_flag_beats_everything(self, tmp_path, monkeypatch):
        flag_kb = _kb(tmp_path / "flag")
        arg_kb = _kb(tmp_path / "arg")
        env_kb = _kb(tmp_path / "env")
        cfg_kb = _kb(tmp_path / "configured")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        factlog_config.write_root(cfg_kb)
        monkeypatch.setenv("FACTLOG_ROOT", str(env_kb))
        monkeypatch.chdir(_kb(tmp_path / "cwd"))
        targets: list = []
        assert _run(monkeypatch, ["--target", str(flag_kb), str(arg_kb)], targets=targets) == 0
        assert targets == [flag_kb]


class TestNonKbIsRefused:
    """A target without sources/ is named as "not a KB", never reported on."""

    @staticmethod
    def _refuse(monkeypatch, capsys, argv):
        targets: list = []
        code = _run(monkeypatch, argv, targets=targets)
        captured = capsys.readouterr()
        # The validator is never reached: a refusal must not double as a verdict on
        # a directory the operator never called a KB.
        assert targets == []
        assert code == 1
        assert "is not a factlog KB" in captured.err
        assert "Fact sync validation failed" not in captured.out
        assert "missing directory" not in captured.out
        return captured.err

    def test_bare_run_outside_a_kb_refuses_instead_of_reporting(self, tmp_path, monkeypatch, capsys):
        """The reproduction from the issue: cd somewhere else, run it bare."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)
        err = self._refuse(monkeypatch, capsys, [])
        assert str(tmp_path.resolve()) in err
        # Which tier produced the target, because that is what has to change.
        assert "the current directory" in err

    def test_a_non_kb_target_flag_is_refused(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        monkeypatch.chdir(_kb(tmp_path / "cwd"))
        err = self._refuse(monkeypatch, capsys, ["--target", str(tmp_path)])
        assert str(tmp_path.resolve()) in err
        assert "--target" in err

    def test_a_non_kb_wiki_flag_quotes_wiki(self, tmp_path, monkeypatch, capsys):
        """The message quotes the spelling the caller typed, not its sibling's.

        ``--wiki`` is an alias sharing ``--target``'s dest, so "the --target option"
        used to be the only wording available — telling a merge_candidates-style
        caller to fix an option they never passed.
        """
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        monkeypatch.chdir(_kb(tmp_path / "cwd"))
        err = self._refuse(monkeypatch, capsys, ["--wiki", str(tmp_path)])
        assert "the --wiki option" in err
        assert "--target option" not in err

    def test_a_non_kb_positional_is_refused(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        monkeypatch.chdir(_kb(tmp_path / "cwd"))
        err = self._refuse(monkeypatch, capsys, [str(tmp_path)])
        assert "command-line argument" in err

    def test_a_stale_configured_kb_is_refused_by_name(self, tmp_path, monkeypatch, capsys):
        """A config pointing at a directory that is no longer a KB says so."""
        stale = tmp_path / "gone"
        stale.mkdir()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        factlog_config.write_root(stale)
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        monkeypatch.chdir(_kb(tmp_path / "cwd"))
        err = self._refuse(monkeypatch, capsys, [])
        assert str(stale.resolve()) in err
        assert "active KB" in err


class TestBlankTargetIsRefused:
    """An empty value is a caller bug, never a request for the next tier.

    ``if target:`` read an empty string as "not given", so ``validate.py ""`` skipped
    the surface the caller *did* use and landed on the config tier. This tool is where
    that is dangerous: the shell harnesses call ``"$PYTHON" "$VALIDATE" "$KB"``, so an
    unset ``$KB`` used to mean "validate the sandbox cwd" and would now mean "validate
    whatever KB the developer last ran ``factlog use`` on".
    """

    @staticmethod
    def _refuse_blank(monkeypatch, capsys, tmp_path, argv):
        """Run with a configured KB in reach; the blank value must not reach it."""
        configured = _kb(tmp_path / "configured")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        factlog_config.write_root(configured)
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        monkeypatch.chdir(_kb(tmp_path / "cwd"))
        targets: list = []
        code = _run(monkeypatch, argv, targets=targets)
        captured = capsys.readouterr()
        # Nothing was validated: not the configured KB, not the cwd, not anything.
        assert targets == []
        assert code == 1
        assert "was empty" in captured.err
        assert str(configured) not in captured.err + captured.out
        return captured.err

    def test_empty_target_flag(self, tmp_path, monkeypatch, capsys):
        err = self._refuse_blank(monkeypatch, capsys, tmp_path, ["--target", ""])
        assert "--target was empty" in err

    def test_empty_wiki_flag_quotes_wiki(self, tmp_path, monkeypatch, capsys):
        err = self._refuse_blank(monkeypatch, capsys, tmp_path, ["--wiki", ""])
        assert "--wiki was empty" in err

    def test_empty_positional(self, tmp_path, monkeypatch, capsys):
        """``validate.py ""`` — the shape an unset ``$KB`` gives the shell harness."""
        err = self._refuse_blank(monkeypatch, capsys, tmp_path, [""])
        assert "the root argument was empty" in err

    def test_whitespace_only_positional(self, tmp_path, monkeypatch, capsys):
        """A path of spaces is the same mistake wearing a disguise."""
        err = self._refuse_blank(monkeypatch, capsys, tmp_path, ["   "])
        assert "the root argument was empty" in err

    def test_resolve_target_raises_with_the_surface(self):
        """The refusal names a surface because resolve_target carries it up."""
        with pytest.raises(validate.BlankTarget) as excinfo:
            validate.resolve_target("", None, flag_spelling="--wiki")
        assert excinfo.value.surface == "--wiki"
        with pytest.raises(validate.BlankTarget) as excinfo:
            validate.resolve_target(None, "")
        assert excinfo.value.surface == "the root argument"

    def test_a_real_path_is_still_resolved(self, tmp_path):
        """Counter-case: only *blank* is refused, not falsy-looking real input."""
        kb = _kb(tmp_path / "kb")
        assert validate.resolve_target(str(kb), None) == (kb, "flag")
        assert validate.resolve_target(None, str(kb)) == (kb, "argument")


class TestFailureReportNamesItsTarget:
    """A failing report says which KB failed, and how that KB was chosen.

    The passing line printed the root all along; the failing one did not, so the
    report a config-tier run produces about KB-B is indistinguishable from a report
    about the KB-A the reader is standing in — the #530 misreading again.
    """

    @staticmethod
    def _report(monkeypatch, capsys, argv):
        monkeypatch.setattr(validate, "validate", lambda root: ["missing directory: facts/"])
        monkeypatch.setattr(validate, "front_matter_warnings", lambda root: [])
        monkeypatch.setattr(validate, "review_section_warnings", lambda root: [])
        monkeypatch.setattr("sys.argv", ["validate.py", *argv])
        code = validate.main()
        assert code == 1
        return capsys.readouterr().out

    def test_config_tier_failure_names_the_kb_and_the_tier(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path / "configured")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        factlog_config.write_root(kb)
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        monkeypatch.chdir(_kb(tmp_path / "standing-here"))
        out = self._report(monkeypatch, capsys, [])
        assert f"Fact sync validation failed: {kb}" in out
        assert "the target came from the active KB" in out
        assert "- missing directory: facts/" in out

    def test_named_target_failure_skips_the_tier_note(self, tmp_path, monkeypatch, capsys):
        """The caller who typed the path does not need to be told where it came from."""
        kb = _kb(tmp_path / "kb")
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)
        out = self._report(monkeypatch, capsys, ["--target", str(kb)])
        assert f"Fact sync validation failed: {kb}" in out
        assert "the target came from" not in out


class TestProcessLevel:
    """The same guarantees through argparse and the exit code, in a real process.

    Everything above stubs ``validate`` and calls ``main()`` in-process, which cannot
    see a broken argparse wiring (the ``--wiki`` alias, the positional's ``nargs``) or
    an exit code that never reaches the shell. These run ``tools/validate.py`` the way
    ``tests/*.sh`` and merge_candidates' delegate do.
    """

    @staticmethod
    def _script(argv, *, cwd, config_home):
        env = {k: v for k, v in os.environ.items() if k != "FACTLOG_ROOT"}
        # The developer's real active KB must be unreachable from here: the config
        # tier is exactly what these exercise.
        env["XDG_CONFIG_HOME"] = str(config_home)
        return subprocess.run(
            [sys.executable, str(Path(validate.__file__).resolve()), *argv],
            cwd=str(cwd), env=env, capture_output=True, text=True, check=False,
        )

    def test_non_kb_target_exits_1_with_two_stderr_lines(self, tmp_path):
        done = self._script([str(tmp_path)], cwd=tmp_path, config_home=tmp_path / "config")
        assert done.returncode == 1
        lines = [line for line in done.stderr.strip().splitlines() if line]
        assert len(lines) == 2, done.stderr
        assert "is not a factlog KB" in lines[0]
        assert "the target came from the command-line argument" in lines[1]
        assert "Fact sync validation failed" not in done.stdout

    def test_empty_positional_exits_1_and_never_reaches_the_config_kb(self, tmp_path):
        """``$KB`` unset in the shell harness: refuse, do not validate the active KB."""
        configured = _kb(tmp_path / "configured")
        config_home = tmp_path / "config"
        (config_home / "factlog").mkdir(parents=True)
        (config_home / "factlog" / "config.json").write_text(
            f'{{"root": "{configured}"}}\n', encoding="utf-8"
        )
        done = self._script([""], cwd=tmp_path, config_home=config_home)
        assert done.returncode == 1
        assert "the root argument was empty" in done.stderr
        assert str(configured) not in done.stdout + done.stderr

    def test_bare_run_reports_against_the_configured_kb_by_name(self, tmp_path):
        """End to end: the config tier is adopted *and* the report says whose it is."""
        configured = _kb(tmp_path / "configured")
        config_home = tmp_path / "config"
        (config_home / "factlog").mkdir(parents=True)
        (config_home / "factlog" / "config.json").write_text(
            f'{{"root": "{configured}"}}\n', encoding="utf-8"
        )
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        done = self._script([], cwd=elsewhere, config_home=config_home)
        assert done.returncode == 1
        assert f"Fact sync validation failed: {configured}" in done.stdout
        assert "the target came from the active KB" in done.stdout
