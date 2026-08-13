# SPDX-License-Identifier: Apache-2.0
"""`factlog lang` must not rebuild a config it could not read (#366).

``write_lang`` re-emits the whole config from ``_read_config()``, which folds
truncated JSON, an unreadable mode, an empty file and a non-object top level
alike into ``{}``. So every one of those classes came back as a file holding the
new language and nothing else, and a truncated ``{"root": "/…/kb",`` — where the
user's KB root is still there *as text* — was destroyed with rc 0 and a
confirmation line. #356 closed the same hole for ``init``/``setup``; these pin
the sibling path, the escape hatch that replaced it, and the two states that must
keep writing (a readable config, and no config at all).

Every test isolates ``XDG_CONFIG_HOME`` into ``tmp_path``: without that, running
this file overwrites the developer's real active-KB config, which is the exact
loss under test.
"""

from __future__ import annotations

import json
import os

import pytest

from factlog import cli
from factlog import config as factlog_config


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    """Point the config at an isolated XDG_CONFIG_HOME and hand back its path."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = factlog_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def run_lang(code, *, force=False):
    """Invoke ``cmd_lang`` the way argparse would, and return its exit code."""
    import argparse

    return cli.cmd_lang(argparse.Namespace(code=code, force=force))


# The four classes #366 measured. Each seeder returns the bytes on disk, so the
# assertion is byte identity and not a re-derivation of what the seeder meant.
def seed_truncated(path):
    path.write_bytes(b'{"root": "/Users/real/kb",')


def seed_unreadable_mode(path):
    path.write_bytes(b'{"root": "/Users/real/kb"}')
    path.chmod(0o000)


def seed_empty(path):
    path.write_bytes(b"")


def seed_non_object(path):
    path.write_bytes(b'["/Users/real/kb"]')


def seed_broken_symlink(path):
    path.symlink_to("/nonexistent/volume/factlog-config.json")


DAMAGED = [
    pytest.param(seed_truncated, id="truncated-json"),
    pytest.param(seed_unreadable_mode, id="unreadable-mode"),
    pytest.param(seed_empty, id="empty-file"),
    pytest.param(seed_non_object, id="non-object-top-level"),
    pytest.param(seed_broken_symlink, id="broken-symlink"),
]


def snapshot(path):
    """What must survive: the link itself for a symlink, else the exact bytes.

    ``read_bytes`` is no use for the ``chmod 000`` and broken-symlink classes —
    the first cannot be read by this process either, the second has no far end —
    so identity is taken from whatever is observable for the class: the link
    target, or the bytes plus the mode.
    """
    if path.is_symlink():
        return ("symlink", os.readlink(path))
    st = path.lstat()
    if not (st.st_mode & 0o400):
        return ("opaque", st.st_size, st.st_mode & 0o777)
    return ("bytes", path.read_bytes())


@pytest.mark.parametrize("seed", DAMAGED)
class TestRefusesToRebuildADamagedConfig:
    def test_bytes_survive_a_set(self, cfg, seed, capsys):
        seed(cfg)
        before = snapshot(cfg)
        rc = run_lang("ko")
        assert rc == 1
        assert snapshot(cfg) == before

    def test_bytes_survive_a_clear(self, cfg, seed, capsys):
        """Clearing rebuilds the file too — ``write_lang(None)`` writes ``{}``.

        Guarding only the set would have left the identical destruction one
        argument away, and ``factlog lang ''`` is the documented unset action.
        """
        seed(cfg)
        before = snapshot(cfg)
        assert run_lang("") == 1
        assert snapshot(cfg) == before

    def test_refusal_names_the_file_and_an_escape_hatch(self, cfg, seed, capsys):
        seed(cfg)
        run_lang("ko")
        err = capsys.readouterr().err
        assert "narration language NOT set" in err
        assert str(cfg) in err
        assert "factlog lang ko --force" in err

    def test_refusal_does_not_claim_the_language_was_set(self, cfg, seed, capsys):
        """The whole point of #366's sibling #356 is output that does not lie.

        A refusal that still printed the confirmation phrase on stdout would let
        a caller reading stdout believe the setting took.
        """
        seed(cfg)
        run_lang("ko")
        out = capsys.readouterr().out
        assert "narration language set to" not in out

    def test_force_writes_and_says_what_it_replaced(self, cfg, seed, capsys):
        seed(cfg)
        assert run_lang("ko", force=True) == 0
        out = capsys.readouterr().out
        assert "narration language set to ko" in out
        assert "replaced an unreadable config" in out
        cfg.chmod(0o600)
        assert json.loads(cfg.read_text(encoding="utf-8")) == {"lang": "ko"}


class TestForceLeavesNoRoot:
    """`--force` writes only the language, so the config comes out recording no KB.

    `factlog use` — the other advertised exit — cannot reach this state: it
    writes a replacement root in the same breath. This one does not, and SKILL.md
    opens every flow with `export FACTLOG_ROOT="$(factlog where --porcelain)"`,
    so a `--force` run from an arbitrary directory quietly promotes that
    directory to the active KB on the next sync. The escape hatch was more
    destructive than the one it was modelled on, and said less about it.
    """

    def test_the_config_really_is_left_without_a_root(self, cfg):
        seed_truncated(cfg)
        run_lang("ko", force=True)
        assert json.loads(cfg.read_text(encoding="utf-8")) == {"lang": "ko"}
        assert factlog_config.read_root() is None

    def test_it_says_the_root_is_gone_and_how_to_record_one(self, cfg, capsys):
        seed_truncated(cfg)
        run_lang("ko", force=True)
        out = capsys.readouterr().out
        assert "records no KB root" in out
        assert "factlog use <kb>" in out

    def test_it_names_the_directory_a_flagless_command_would_fall_back_to(
        self, cfg, capsys, monkeypatch, tmp_path
    ):
        """cwd is the trap SKILL.md walks into, so the fallback is named, not
        merely implied by 'no root'."""
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        elsewhere = tmp_path / "some-unrelated-dir"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        seed_truncated(cfg)
        run_lang("ko", force=True)
        out = capsys.readouterr().out
        assert str(elsewhere.resolve()) in out
        assert "the current directory" in out

    def test_it_names_the_environment_when_that_is_what_wins(
        self, cfg, capsys, monkeypatch, tmp_path
    ):
        """With $FACTLOG_ROOT exported the fallback is not cwd, and saying cwd
        would be the same false claim `_reach_note` exists to prevent."""
        kb = tmp_path / "env-kb"
        kb.mkdir()
        monkeypatch.setenv("FACTLOG_ROOT", str(kb))
        seed_truncated(cfg)
        run_lang("ko", force=True)
        out = capsys.readouterr().out
        assert "$FACTLOG_ROOT" in out
        assert str(kb.resolve()) in out

    def test_a_forced_clear_says_it_too(self, cfg, capsys):
        """`factlog lang '' --force` writes `{}` — no language and no root."""
        seed_truncated(cfg)
        run_lang("", force=True)
        assert json.loads(cfg.read_text(encoding="utf-8")) == {}
        assert "records no KB root" in capsys.readouterr().out


def test_force_names_the_root_as_the_loss_not_the_language(cfg, capsys):
    """``lost``, reused from the root-writing sites, named the wrong field.

    On a truncated config `factlog lang --force` destroys the root and sets the
    language, so "any narration language in it is gone" reports the one field the
    command was replacing anyway and stays silent about the path it dropped.
    """
    seed_truncated(cfg)
    run_lang("ko", force=True)
    out = capsys.readouterr().out
    assert "KB root" in out
    assert "any narration language in it is gone" not in out


def test_force_on_a_symlink_reports_the_link_not_the_root(cfg, capsys):
    """``os.replace`` swaps the link and leaves the far end intact, so the root
    in the far-end file survives and the indirection is the whole loss."""
    seed_broken_symlink(cfg)
    run_lang("ko", force=True)
    out = capsys.readouterr().out
    assert "the symlink is gone" in out
    assert not cfg.is_symlink()


def test_readable_config_still_sets_lang_and_keeps_root(cfg, capsys):
    """The control: nothing about the healthy path moves."""
    cfg.write_text(json.dumps({"root": "/Users/real/kb", "lang": "en"}), encoding="utf-8")
    assert run_lang("ko") == 0
    assert json.loads(cfg.read_text(encoding="utf-8")) == {"root": "/Users/real/kb", "lang": "ko"}
    assert "narration language set to ko" in capsys.readouterr().out


def test_missing_config_is_written_not_refused(cfg, capsys):
    """MISSING is not UNREADABLE. A first run — before any ``init`` — must still
    be able to set a narration language, which is why ``config_status`` splits
    the two in the first place."""
    assert not cfg.exists()
    assert run_lang("ko") == 0
    assert json.loads(cfg.read_text(encoding="utf-8")) == {"lang": "ko"}


def test_readable_config_without_a_root_is_written(cfg, capsys):
    """A config that parses but records no root has no path to lose, so it is
    READABLE and is written — matching ``_plan_activation``'s deliberate
    non-holding of the same state."""
    cfg.write_text(json.dumps({"lang": "en"}), encoding="utf-8")
    assert run_lang("ko") == 0
    assert json.loads(cfg.read_text(encoding="utf-8")) == {"lang": "ko"}


class TestForceWithoutACode:
    """`factlog lang --force` alone did nothing and said nothing.

    `code is None` fell straight through to the query branch, which never reads
    `force`. So the destructive flag typed on its own — the plausible slip is
    dropping the CODE and keeping the flag — printed the *old* language on stdout
    and exited 0, which reads exactly like a successful set.
    """

    def test_it_is_rejected_rather_than_treated_as_a_query(self, cfg, capsys):
        cfg.write_text(json.dumps({"lang": "en"}), encoding="utf-8")
        assert run_lang(None, force=True) == 2
        captured = capsys.readouterr()
        assert "--force applies to setting a language" in captured.err
        assert captured.out == "", "a rejected command must not emit the porcelain line"

    def test_it_changes_nothing(self, cfg, capsys):
        cfg.write_text(json.dumps({"root": "/Users/real/kb", "lang": "en"}), encoding="utf-8")
        before = cfg.read_bytes()
        run_lang(None, force=True)
        assert cfg.read_bytes() == before

    def test_a_bare_query_is_untouched(self, cfg, capsys):
        """The flag is what is rejected, not the query."""
        cfg.write_text(json.dumps({"lang": "en"}), encoding="utf-8")
        assert run_lang(None) == 0
        assert capsys.readouterr().out == "en\n"


def test_query_mode_leaves_a_damaged_config_alone_and_stays_porcelain(cfg, capsys):
    """A read must not become a write, and must not grow a second line: the skill
    parses exactly one bare line here."""
    seed_truncated(cfg)
    before = snapshot(cfg)
    assert run_lang(None) == 0
    assert snapshot(cfg) == before
    captured = capsys.readouterr()
    assert captured.out == "\n"
    assert captured.err == ""


class TestTheWriteBoundary:
    """`factlog lang` must reach the same OSError boundary the root write does.

    ``_write_root_or_explain`` exists because a *directory* at the config path
    made every advertised way out of a damaged config die on
    ``IsADirectoryError`` — pinned as ``TestADirectoryAtTheConfigPath`` in
    test_init_activation.py, and promised in that function's own docstring.
    ``factlog lang --force`` is a third advertised exit, and it was added outside
    that boundary: the refusal named the command (``factlog lang ko --force``)
    and the command then crashed.

    The unwritable-config-directory case is here for the sharper reason. That
    config is perfectly *readable*, so the UNREADABLE guard never fires and no
    ``--force`` is involved — a plain ``factlog lang ko`` crashed. So the
    boundary cannot live inside the damaged-config branch; it belongs to the act
    of writing the file, which is why ``_apply_lang`` now goes through it and all
    three entry points are covered at once.

    Subprocess rather than in-process: "no traceback" is a claim about what the
    process prints to stderr, and only a real process can be asked.
    """

    @pytest.fixture
    def blocked_by_a_directory(self, cfg):
        cfg.mkdir()
        return cfg

    @pytest.fixture
    def blocked_by_an_unwritable_dir(self, cfg):
        cfg.write_bytes(b'{"root": "/Users/real/kb"}')
        cfg.parent.chmod(0o500)
        yield cfg
        # Restore before teardown, or pytest cannot clean up tmp_path.
        cfg.parent.chmod(0o700)

    def run_cli(self, cfg, *argv):
        import subprocess
        import sys

        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return subprocess.run(
            [sys.executable, "-m", "factlog", *argv],
            cwd=repo_root,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "XDG_CONFIG_HOME": str(cfg.parent.parent),
                "PYTHONPATH": repo_root,
            },
            check=False,
        )

    def assert_explained(self, proc, cfg):
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "Traceback" not in proc.stderr, proc.stderr
        assert "IsADirectoryError" not in proc.stderr, proc.stderr
        assert "PermissionError" not in proc.stderr, proc.stderr
        assert "cannot write the active-KB config" in proc.stderr, proc.stderr
        # The message ends "Nothing at that path was changed", and the atomic
        # writer stages a `.tmp` sibling before the swap. Without cleanup on the
        # failure path that sentence is false and a stray config.json.tmp stays
        # next to the real config for good.
        strays = [p.name for p in cfg.parent.iterdir() if p.name.endswith(".tmp")]
        assert not strays, f"a failed write left {strays} behind while claiming nothing changed"

    def test_force_explains_instead_of_crashing_on_a_directory(self, blocked_by_a_directory):
        proc = self.run_cli(blocked_by_a_directory, "lang", "ko", "--force")
        self.assert_explained(proc, blocked_by_a_directory)

    def test_the_refusal_it_prints_names_a_command_that_works(self, blocked_by_a_directory):
        """The refusal and the crash were one command apart: whatever `--force`
        does here, it must not be a traceback."""
        refusal = self.run_cli(blocked_by_a_directory, "lang", "ko")
        assert refusal.returncode == 1
        assert "factlog lang ko --force" in refusal.stderr, refusal.stderr
        forced = self.run_cli(blocked_by_a_directory, "lang", "ko", "--force")
        assert "Traceback" not in forced.stderr, forced.stderr

    def test_a_readable_config_under_an_unwritable_dir_explains(self, blocked_by_an_unwritable_dir):
        proc = self.run_cli(blocked_by_an_unwritable_dir, "lang", "ko")
        self.assert_explained(proc, blocked_by_an_unwritable_dir)
        # The diagnosis must name the directory, not the config file: deleting
        # config.json drops the root and lang without unblocking the write.
        assert "not writable" in proc.stderr, proc.stderr

    def test_the_unwritable_dir_case_keeps_the_config_intact(self, blocked_by_an_unwritable_dir):
        before = blocked_by_an_unwritable_dir.read_bytes()
        self.run_cli(blocked_by_an_unwritable_dir, "lang", "ko")
        assert blocked_by_an_unwritable_dir.read_bytes() == before

    def test_the_message_names_the_command_that_could_not_write(self, blocked_by_a_directory):
        """`_apply_lang` is shared by three commands, so the boundary is told
        which one is speaking rather than hard-coding `factlog use`."""
        proc = self.run_cli(blocked_by_a_directory, "lang", "ko", "--force")
        assert "factlog lang: cannot write" in proc.stderr, proc.stderr


class TestEverySiteThatDeclinesKnowsTheWayOut:
    """Three sentences decline to set a language; only one knew `--force`.

    `setup --lang` ended both of its refusals with "then set the language with
    `factlog lang`" — a command that, on the same damaged config, refuses in
    turn. The user learned the escape hatch existed one wasted rc 1 later. That
    is the drift `_Unreadable` was built to prevent, so the fragment is shared
    and this asserts the sharing by behaviour, not by grepping the source.
    """

    def test_the_hint_quotes_the_code_including_the_clear_action(self):
        assert cli._lang_force_hint("ko") == "factlog lang ko --force"
        assert cli._lang_force_hint("") == "factlog lang '' --force"

    def test_lang_refusal_carries_it(self, cfg, capsys):
        seed_truncated(cfg)
        run_lang("ko")
        assert cli._lang_force_hint("ko") in capsys.readouterr().err

    def test_setup_carries_it_in_both_its_notes_and_its_closing_line(
        self, cfg, capsys, tmp_path, monkeypatch
    ):
        """`setup --lang` produces two of the three sentences: a summary note and
        the rc-1 closing line. Both must name it."""
        import argparse

        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        seed_truncated(cfg)
        kb = tmp_path / "kb"
        rc = cli.cmd_setup(argparse.Namespace(target=str(kb), lang="ko", activate=False))
        captured = capsys.readouterr()

        assert rc == 1, "a --lang that was not applied must not exit 0"
        # Spelled out rather than taken from `_lang_force_hint`, so this fails on
        # a tree where the two setup sites simply never learned the flag —
        # asserting via the helper would only prove the helper exists.
        assert "factlog lang ko --force" in captured.out, captured.out
        assert "factlog lang ko --force" in captured.err, captured.err
        # And the refusal it belongs to is still intact: nothing was written.
        assert cfg.read_bytes() == b'{"root": "/Users/real/kb",'


def test_skill_md_tells_the_assistant_what_the_new_rc_means(cfg):
    """SKILL.md is what an assistant acts on, so a new refusal it does not
    mention is a refusal the assistant will meet with no instruction.

    Paired with the live rc below rather than asserted alone: a doc pin that only
    greps prose passes on a tree where the prose is right and the code changed.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    text = open(
        os.path.join(repo_root, "skills", "factlog", "SKILL.md"), encoding="utf-8"
    ).read()
    assert "exits 1" in text, "SKILL.md does not tell the assistant the setter can refuse"
    assert "--force" in text, "SKILL.md does not warn the assistant off --force"

    seed_truncated(cfg)
    assert run_lang("ko") == 1, "SKILL.md documents an rc the code no longer returns"


def test_invalid_code_is_still_rejected_before_the_config_is_consulted(cfg, capsys):
    """rc 2 (invalid input) outranks rc 1 (unwritable config): the value is wrong
    whatever the config says, and reporting the config instead would send the
    user to repair a file that is not the problem."""
    seed_truncated(cfg)
    before = snapshot(cfg)
    assert run_lang("x" * 100) == 2
    assert snapshot(cfg) == before
    assert "too long" in capsys.readouterr().err
