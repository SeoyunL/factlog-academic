# SPDX-License-Identifier: Apache-2.0
"""Who may change the active KB (#210).

`factlog init` used to write the active-KB config unconditionally, so scaffolding
a scratch KB anywhere — another shell, a test harness, an agent — retargeted the
user's accept/reject/amend/sync at it, silently.

These are the pure decision functions behind that behaviour. They are unit-tested
rather than driven through the CLI because `setup` installs dependencies before
it reaches the KB block: exercising it end-to-end in the deterministic shell job
would make that job run `pip install` and reach the network.
"""
from __future__ import annotations

import argparse
import tempfile

import pytest
from factlog import cli, config as factlog_config
from factlog.cli import (
    active_kb_is_usable,
    init_adopts_target,
    setup_active_kb_action,
    target_under_tempdir,
)


@pytest.fixture
def existing(tmp_path):
    kb = tmp_path / "my-kb"
    kb.mkdir()
    return kb


@pytest.fixture
def other(tmp_path):
    kb = tmp_path / "scratch-kb"
    kb.mkdir()
    return kb


class TestInitAdoption:
    def test_first_run_adopts(self, other):
        # Nothing configured yet: init is how you get your first active KB.
        assert init_adopts_target(None, other) is True

    def test_second_init_elsewhere_does_not_adopt(self, existing, other):
        # THE BUG: this used to silently retarget every mutating command.
        assert init_adopts_target(str(existing), other) is False

    def test_reinit_of_the_active_kb_adopts(self, existing):
        # Re-running init on the KB you are already using is a no-op, not a clash.
        assert init_adopts_target(str(existing), existing) is True

    def test_activate_flag_is_the_opt_in(self, existing, other):
        # Scripts that genuinely want the new KB have an explicit way to say so,
        # instead of depending on the old silent behaviour.
        assert init_adopts_target(str(existing), other, activate=True) is True

    def test_activate_over_a_different_kb_is_announced(self, existing, other):
        # Opting in is not a licence to be silent. `init --activate` shares
        # setup's wording, so it names the KB it displaced instead of moving the
        # active KB without a word — the very thing #210 is about.
        action = setup_active_kb_action(str(existing), other)
        assert action.startswith("CHANGED active KB")
        assert str(existing) in action

    def test_deleted_active_kb_does_not_trap_the_user(self, tmp_path, other):
        # A config pointing at a KB that no longer exists must not be defended —
        # otherwise init could never adopt anything again.
        gone = tmp_path / "deleted-kb"
        assert active_kb_is_usable(str(gone)) is False
        assert init_adopts_target(str(gone), other) is True

    def test_a_file_is_not_a_usable_kb(self, tmp_path):
        not_a_dir = tmp_path / "kb.txt"
        not_a_dir.write_text("", encoding="utf-8")
        assert active_kb_is_usable(str(not_a_dir)) is False


class TestSetupAction:
    def test_replacing_a_different_kb_is_announced(self, existing, other):
        action = setup_active_kb_action(str(existing), other)
        assert action.startswith("CHANGED active KB")
        assert str(existing) in action and str(other) in action

    def test_first_run_is_not_announced_as_a_change(self, other):
        assert setup_active_kb_action(None, other).startswith("set active KB")

    def test_resetting_up_the_same_kb_is_not_a_change(self, existing):
        assert setup_active_kb_action(str(existing), existing).startswith("set active KB")

    def test_replacing_a_deleted_kb_is_not_a_scary_change(self, tmp_path, other):
        gone = tmp_path / "deleted-kb"
        assert setup_active_kb_action(str(gone), other).startswith("set active KB")


class TestTargetUnderTempdir:
    """The pure predicate behind the #461 guard.

    Uses paths, not directories that must exist: the check is a lexical
    containment test after resolving symlinks, so it deliberately does not care
    whether the target is present on disk.
    """

    def test_pytest_tmp_path_is_temp(self, tmp_path):
        # pytest basetemps live under tempfile.gettempdir() — exactly the scratch
        # KBs the guard exists to catch.
        assert target_under_tempdir(tmp_path / "kb") is True

    def test_gettempdir_itself_is_temp(self):
        assert target_under_tempdir(tempfile.gettempdir()) is True

    def test_a_nonexistent_child_of_tmp_is_still_temp(self):
        # The path need not exist: a self-erasing scratch KB is precisely the
        # case where the directory is already gone.
        assert target_under_tempdir("/tmp/factlog-scratch-does-not-exist") is True

    def test_var_tmp_is_temp(self):
        assert target_under_tempdir("/var/tmp/factlog-scratch") is True

    def test_a_durable_kb_path_is_not_temp(self):
        # The first-run convenience must stay intact for a genuine, durable KB.
        # A fixed absolute path is used rather than ~ because the test suite
        # redirects HOME under a tmp dir (#454 isolation).
        assert target_under_tempdir("/opt/factlog/wiki") is False


class TestTempAdoptionGuard:
    """`target_is_temp` carves the scratch-KB refusal out of first-run adoption."""

    def test_temp_target_is_refused_on_first_run(self, other):
        # THE #461 GUARD: no active KB yet, but the target is a temp dir, so the
        # silent first-run adoption is refused — the user must opt in.
        assert init_adopts_target(None, other, target_is_temp=True) is False

    def test_nontemp_first_run_still_adopts(self, other):
        # The convenience is untouched for a real KB (target_is_temp False).
        assert init_adopts_target(None, other, target_is_temp=False) is True

    def test_activate_overrides_the_temp_guard(self, other):
        # Deliberate use is never blocked: --activate wins even for a temp KB.
        assert init_adopts_target(None, other, activate=True, target_is_temp=True) is True

    def test_reinit_of_an_active_temp_kb_is_still_a_noop_adoption(self, existing):
        # Re-running init on the temp KB you are ALREADY using is a no-op, not a
        # refusal: it does not retarget anything.
        assert init_adopts_target(str(existing), existing, target_is_temp=True) is True

    def test_temp_target_over_a_usable_kb_is_refused_as_before(self, existing, other):
        # With a usable active KB elsewhere the #210 refusal already applies; the
        # temp flag does not change that verdict.
        assert init_adopts_target(str(existing), other, target_is_temp=True) is False

    def test_temp_target_over_a_deleted_kb_is_refused(self, tmp_path, other):
        # A dead config normally escapes into first-run adoption; a temp target
        # does NOT get that escape, since it would just re-poison the config.
        gone = tmp_path / "deleted-kb"
        assert active_kb_is_usable(str(gone)) is False
        assert init_adopts_target(str(gone), other, target_is_temp=True) is False


class TestInitDeadPathWarning:
    """`init` keeps the dead-path adoption (#210) but no longer does it silently.

    When the configured active KB has vanished, `init` still adopts a fresh target
    — refusing would trap the user forever. But that silent retarget is the outer
    half of the #454 self-perpetuating loop: a KB is written, its directory is
    removed, and the next run quietly re-adopts. So the adoption now emits a single
    stderr line naming the reason, the displaced path, and the recovery command.

    These drive `cmd_init` (not the pure decision function) because the warning is
    a property of the command's output, not of `init_adopts_target`. `tmp_path` is
    under a temp dir, which would otherwise trip the #461 guard and refuse the
    adoption entirely, so `target_under_tempdir` is neutralised: the durable
    dead-path case is what is under test, and its harness cousin (temp targets) is
    pinned in tests/test_init_active_kb.sh.
    """

    @pytest.fixture
    def durable_kb(self, tmp_path, monkeypatch):
        # Isolate the active-KB config from the dev machine, and treat targets as
        # durable so the dead-path adoption actually runs.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        monkeypatch.setattr(cli, "target_under_tempdir", lambda _target: False)

    def _init(self, target, *, activate=False):
        args = argparse.Namespace(target=str(target), activate=activate)
        return cli.cmd_init(args)

    def test_dead_path_replacement_warns_on_stderr(self, tmp_path, durable_kb, capsys):
        # (1) THE CASE: config points at a KB that no longer exists. Adoption is
        # kept, but a stderr warning names the reason, the previous path, and the
        # recovery command — and stdout stays clean.
        gone = tmp_path / "gone-kb"
        factlog_config.write_root(gone)  # config now points at a path we never create
        assert active_kb_is_usable(str(gone)) is False

        fresh = tmp_path / "fresh-kb"
        assert self._init(fresh) == 0

        out, err = capsys.readouterr()
        assert str(fresh) == factlog_config.read_root()  # adoption still happened
        # Reason, previous path, and recovery hint are all present on stderr.
        assert "warning" in err
        assert "no longer exists" in err
        assert str(gone.resolve()) in err
        assert f"factlog use {gone.resolve()}" in err
        # The recovery hint is conditioned on the KB coming back: `factlog use`
        # rejects a still-missing path (rc=1), so the message says "Once ... is
        # back" rather than telling the user to run it immediately.
        assert "is back" in err
        # stdout carries no warning: the porcelain/stdout contract is unpolluted.
        assert "warning" not in out

    def test_first_run_does_not_warn(self, tmp_path, durable_kb, capsys):
        # (2) Nothing configured yet: adoption is the whole point of first-run and
        # there is no displaced KB to warn about.
        assert factlog_config.read_root() is None
        fresh = tmp_path / "fresh-kb"
        assert self._init(fresh) == 0

        out, err = capsys.readouterr()
        assert str(fresh) == factlog_config.read_root()
        assert "warning" not in err
        assert "warning" not in out

    def test_reinit_of_same_path_does_not_warn(self, tmp_path, durable_kb, capsys):
        # (3) Re-init of the already-active KB is a no-op adoption, not a displaced
        # dead path, so it stays quiet.
        kb = tmp_path / "kb"
        kb.mkdir()
        factlog_config.write_root(kb)
        capsys.readouterr()

        assert self._init(kb) == 0
        out, err = capsys.readouterr()
        assert "warning" not in err
        assert "warning" not in out

    def test_activate_keeps_changed_warning_not_the_dead_path_one(self, tmp_path, durable_kb, capsys):
        # (4) --activate over a *usable* different KB: the existing CHANGED warning
        # still fires (opting in is not a licence to be silent), and the dead-path
        # warning does NOT, since the previous KB was alive and this was a
        # deliberate switch rather than an involuntary replacement.
        prev = tmp_path / "prev-kb"
        prev.mkdir()
        factlog_config.write_root(prev)
        capsys.readouterr()

        fresh = tmp_path / "fresh-kb"
        assert self._init(fresh, activate=True) == 0
        out, err = capsys.readouterr()
        assert "CHANGED active KB" in err
        assert "no longer exists" not in err

    def test_stdout_is_never_polluted_by_the_warning(self, tmp_path, durable_kb, capsys):
        # (5) The whole warning is stderr-only, so a caller parsing stdout (or a
        # future --porcelain reader) sees exactly the adoption summary and nothing
        # of the warning — the stdout contract the harness relies on is intact.
        gone = tmp_path / "gone-kb"
        factlog_config.write_root(gone)
        capsys.readouterr()

        fresh = tmp_path / "fresh-kb"
        assert self._init(fresh) == 0
        out, err = capsys.readouterr()
        assert "no longer exists" not in out
        assert "factlog use" not in out
        assert "no longer exists" in err

    def test_dead_path_with_activate_still_warns(self, tmp_path, durable_kb, capsys):
        # (F) --activate over a *dead* different KB still warns. Opting in is not a
        # licence to be silent (#210): the CHANGED notice fires on --activate over a
        # usable KB, so the dead-path displacement — which `setup_active_kb_action`
        # would otherwise report as a plain "set active KB" first-run — must be
        # named too. This is the sibling of (4): there the previous KB was alive so
        # CHANGED covered it; here it is dead, so the dead-path branch does.
        gone = tmp_path / "gone-kb"
        factlog_config.write_root(gone)
        assert active_kb_is_usable(str(gone)) is False
        capsys.readouterr()

        fresh = tmp_path / "fresh-kb"
        assert self._init(fresh, activate=True) == 0
        out, err = capsys.readouterr()
        assert str(fresh) == factlog_config.read_root()
        assert "no longer exists" in err
        assert str(gone.resolve()) in err
        # Still stderr-only, even on the deliberate opt-in.
        assert "no longer exists" not in out
