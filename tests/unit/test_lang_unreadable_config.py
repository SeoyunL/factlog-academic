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


def test_invalid_code_is_still_rejected_before_the_config_is_consulted(cfg, capsys):
    """rc 2 (invalid input) outranks rc 1 (unwritable config): the value is wrong
    whatever the config says, and reporting the config instead would send the
    user to repair a file that is not the problem."""
    seed_truncated(cfg)
    before = snapshot(cfg)
    assert run_lang("x" * 100) == 2
    assert snapshot(cfg) == before
    assert "too long" in capsys.readouterr().err
