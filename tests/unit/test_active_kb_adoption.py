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

import pytest
from factlog.cli import active_kb_is_usable, init_adopts_target, setup_active_kb_action


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
