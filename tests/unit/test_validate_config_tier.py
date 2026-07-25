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

``validate.validate`` is stubbed throughout: what is under test is which directory
the tool aims at and whether it agrees to look, not the checks themselves.
"""
from __future__ import annotations

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
