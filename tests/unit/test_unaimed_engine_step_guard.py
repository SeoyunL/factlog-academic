# SPDX-License-Identifier: Apache-2.0
"""The engine steps say which KB they act on, and an unaimed one may not disarm it (#547).

#527/#528 taught ``compile_facts`` and ``run_logic_check`` the active-KB config tier, and
#532/#529 gave ``merge_candidates``/``finalize`` a guard against a root that came ONLY
from that config while the caller stands outside the KB. The two engine steps were left
out on the argument that they write only their own derived output. Measurement weakened
that: on a single-valued contradiction ``compile_facts`` DELETES ``facts/accepted.dl``, so
a run from an unrelated directory left the configured KB with no engine input — /factlog
ask answers nothing — until a human resolved the conflict.

What this file pins:

* both steps announce ``target KB <root> (from <source>)``, the siblings' line verbatim;
* the deletion is refused on an unaimed (config-tier, cwd outside) run — and ONLY there:
  every aimed shape still removes the file, which is the #212/#327 invariant;
* an unaimed compile with no contradiction still compiles (#527's pins are extensions,
  not casualties, of this change);
* ``run_logic_check`` is not refused at all, so the determinism gate's mandated no-flag
  form keeps working, and the gate's own documented flow (export FACTLOG_ROOT, then call
  both steps bare) passes end to end.

Everything drives the real scripts in subprocesses: the root is bound by a *pre-import*
pass, so an in-process call would resolve once for the whole session and prove nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPILE = REPO_ROOT / "tools" / "compile_facts.py"
CHECK = REPO_ROOT / "tools" / "run_logic_check.py"
HEADER = "subject,relation,object,source,status,confidence,note"

CONFLICT_ROWS = [
    "Acme,founded_in,1999,sources/a.md,confirmed,0.9,",
    "Acme,founded_in,2001,sources/a.md,confirmed,0.9,",
]
CLEAN_ROWS = ["Acme,founded_in,1999,sources/a.md,confirmed,0.9,"]


@pytest.fixture
def config_home(tmp_path, monkeypatch) -> Path:
    """A per-test ``$XDG_CONFIG_HOME``, layered over the session-wide sandbox.

    The autouse isolation fixture already keeps writes out of the developer's real
    config; this narrows it further so one test's active KB cannot decide another's.
    """
    home = tmp_path / "xdg"
    (home / "factlog").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home


def _env(config_home: Path, root: Path | None = None) -> dict[str, str]:
    """Child env with the worktree importable and FACTLOG_ROOT under our control.

    The unit conftest pins FACTLOG_ROOT process-wide, so the config and cwd tiers are
    only reachable in a child that has it removed — inheriting it would make every tier
    below 'env' untestable, and the guard under test keys on exactly those tiers.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["XDG_CONFIG_HOME"] = str(config_home)
    env.pop("FACTLOG_ROOT", None)
    if root is not None:
        env["FACTLOG_ROOT"] = str(root)
    return env


def _activate(config_home: Path, root: Path | None) -> None:
    cfg = config_home / "factlog" / "config.json"
    if root is None:
        cfg.unlink(missing_ok=True)
        return
    cfg.write_text(json.dumps({"root": str(root)}) + "\n", encoding="utf-8")


def _seed_kb(path: Path, rows: list[str]) -> Path:
    """A minimal KB declaring ``founded_in`` single-valued, holding *rows*."""
    for name in ("sources", "pages", "facts", "decisions", "policy"):
        (path / name).mkdir(parents=True, exist_ok=True)
    (path / "sources" / "a.md").write_text("a\n", encoding="utf-8")
    (path / "policy" / "single-valued.md").write_text(
        "# single-valued relations\n\n- founded_in\n", encoding="utf-8"
    )
    (path / "facts" / "candidates.csv").write_text(
        "\n".join([HEADER, *rows]) + "\n", encoding="utf-8"
    )
    return path


SNAPSHOT = 'relation("Acme", "founded_in", "1999").\n'


def _snapshot(kb: Path) -> Path:
    """A previously compiled accepted.dl — the file an unaimed run must not remove."""
    accepted = kb / "facts" / "accepted.dl"
    accepted.write_text(SNAPSHOT, encoding="utf-8")
    return accepted


def _run(script: Path, *args: str, cwd: Path, config_home: Path, root: Path | None = None):
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(cwd), capture_output=True, text=True, env=_env(config_home, root),
    )


@pytest.fixture
def outside(tmp_path) -> Path:
    """A cwd that is deliberately not the KB — the #547 reproduction directory."""
    d = tmp_path / "outside"
    d.mkdir()
    return d


class TestUnaimedRunKeepsTheEngineInput:
    """The measured symptom: an unaimed compile deleted the active KB's accepted.dl."""

    def test_the_repro_leaves_accepted_dl_on_disk(self, tmp_path, outside, config_home):
        kb = _seed_kb(tmp_path / "kb", CONFLICT_ROWS)
        accepted = _snapshot(kb)
        _activate(config_home, kb)

        proc = _run(COMPILE, cwd=outside, config_home=config_home)

        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert accepted.is_file(), "an unaimed run deleted the configured KB's engine input"
        assert accepted.read_text(encoding="utf-8") == SNAPSHOT
        assert "REFUSING to remove" in proc.stderr

    def test_the_refusal_names_both_ways_to_aim_the_run(self, tmp_path, outside, config_home):
        # A refusal that does not say how to proceed just moves the dead end.
        kb = _seed_kb(tmp_path / "kb", CONFLICT_ROWS)
        _snapshot(kb)
        _activate(config_home, kb)

        proc = _run(COMPILE, cwd=outside, config_home=config_home)

        assert f"--target {kb}" in proc.stderr
        assert f"FACTLOG_ROOT={kb}" in proc.stderr

    def test_the_error_says_the_file_was_kept_not_removed(self, tmp_path, outside, config_home):
        # The old text claimed the removal unconditionally. A reader who is told the
        # engine input is gone, when it is not, resolves the wrong problem.
        kb = _seed_kb(tmp_path / "kb", CONFLICT_ROWS)
        _snapshot(kb)
        _activate(config_home, kb)

        proc = _run(COMPILE, cwd=outside, config_home=config_home)

        assert "was KEPT because this run did not aim at that KB" in proc.stderr
        assert "was removed" not in proc.stderr

    def test_an_unaimed_run_still_compiles_nothing(self, tmp_path, outside, config_home):
        # Withholding the deletion is not permission to write: the contradiction gate
        # still refuses to put contradictory rows into the engine input.
        kb = _seed_kb(tmp_path / "kb", CONFLICT_ROWS)
        accepted = _snapshot(kb)
        _activate(config_home, kb)

        _run(COMPILE, cwd=outside, config_home=config_home)

        assert "2001" not in accepted.read_text(encoding="utf-8")

    def test_nothing_to_protect_is_not_reported_as_a_refusal(self, tmp_path, outside, config_home):
        # No accepted.dl on disk: the unlink is a no-op, so an unaimed run has taken
        # nothing away and must not claim it withheld anything.
        kb = _seed_kb(tmp_path / "kb", CONFLICT_ROWS)
        _activate(config_home, kb)

        proc = _run(COMPILE, cwd=outside, config_home=config_home)

        assert proc.returncode == 1
        assert not (kb / "facts" / "accepted.dl").exists()
        assert "REFUSING" not in proc.stderr
        assert "was KEPT" not in proc.stderr


class TestAimedRunsStillHeal:
    """#212/#327 verbatim for every shape that names a target — the guard's other side."""

    def test_env_tier_removes_the_stale_accepted_dl(self, tmp_path, outside, config_home):
        # The shape every documented flow uses: SKILL.md exports FACTLOG_ROOT, finalize
        # and `factlog amend/eject` hand the child an explicit FACTLOG_ROOT.
        kb = _seed_kb(tmp_path / "kb", CONFLICT_ROWS)
        accepted = _snapshot(kb)
        _activate(config_home, kb)

        proc = _run(COMPILE, cwd=outside, config_home=config_home, root=kb)

        assert proc.returncode == 1
        assert not accepted.exists(), "an aimed run left a contradictory KB's engine input in place"
        assert "was removed" in proc.stderr

    @pytest.mark.parametrize("flag", ["--target", "--wiki"])
    def test_flag_tier_removes_the_stale_accepted_dl(self, flag, tmp_path, outside, config_home):
        kb = _seed_kb(tmp_path / "kb", CONFLICT_ROWS)
        accepted = _snapshot(kb)
        _activate(config_home, kb)

        proc = _run(COMPILE, flag, str(kb), cwd=outside, config_home=config_home)

        assert proc.returncode == 1
        assert not accepted.exists()

    def test_standing_in_the_configured_kb_removes_it(self, tmp_path, config_home):
        # The documented no-flag workflow: run it from the KB. 'config' is the tier, but
        # the caller is standing in the target, so the run is aimed.
        kb = _seed_kb(tmp_path / "kb", CONFLICT_ROWS)
        accepted = _snapshot(kb)
        _activate(config_home, kb)

        proc = _run(COMPILE, cwd=kb, config_home=config_home)

        assert proc.returncode == 1
        assert not accepted.exists()

    def test_standing_in_a_subdirectory_of_it_removes_it(self, tmp_path, config_home):
        kb = _seed_kb(tmp_path / "kb", CONFLICT_ROWS)
        accepted = _snapshot(kb)
        _activate(config_home, kb)

        proc = _run(COMPILE, cwd=kb / "sources", config_home=config_home)

        assert proc.returncode == 1
        assert not accepted.exists()

    def test_cwd_tier_removes_it(self, tmp_path, config_home):
        # No config at all: the root IS the cwd, so there is no unnamed target to guard.
        kb = _seed_kb(tmp_path / "kb", CONFLICT_ROWS)
        accepted = _snapshot(kb)
        _activate(config_home, None)

        proc = _run(COMPILE, cwd=kb, config_home=config_home)

        assert proc.returncode == 1
        assert not accepted.exists()


class TestTheConfigTierStillWorks:
    """#527's pins are extended by the guard, not reversed by it."""

    def test_an_unaimed_run_with_no_contradiction_still_compiles(self, tmp_path, outside, config_home):
        kb = _seed_kb(tmp_path / "kb", CLEAN_ROWS)
        _activate(config_home, kb)

        proc = _run(COMPILE, cwd=outside, config_home=config_home)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "Acme" in (kb / "facts" / "accepted.dl").read_text(encoding="utf-8")

    def test_the_compile_log_still_says_what_it_wrote(self, tmp_path, outside, config_home):
        kb = _seed_kb(tmp_path / "kb", CLEAN_ROWS)
        _activate(config_home, kb)

        proc = _run(COMPILE, cwd=outside, config_home=config_home)

        assert "engine facts: 1 / 1" in proc.stdout
        assert f"written: {kb / 'facts' / 'accepted.dl'}" in proc.stdout


class TestAnnouncedTarget:
    """Both steps name the KB and the tier it came from, before touching it."""

    @pytest.mark.parametrize("script,tool", [(COMPILE, "compile_facts"), (CHECK, "run_logic_check")])
    def test_config_tier_is_announced_as_config(self, script, tool, tmp_path, outside, config_home):
        kb = _seed_kb(tmp_path / "kb", CLEAN_ROWS)
        _activate(config_home, kb)

        proc = _run(script, cwd=outside, config_home=config_home)

        assert f"{tool}: target KB {kb} (from config)" in proc.stdout

    @pytest.mark.parametrize("script,tool", [(COMPILE, "compile_facts"), (CHECK, "run_logic_check")])
    def test_env_tier_is_announced_as_env(self, script, tool, tmp_path, outside, config_home):
        kb = _seed_kb(tmp_path / "kb", CLEAN_ROWS)
        _activate(config_home, tmp_path / "other-kb")

        proc = _run(script, cwd=outside, config_home=config_home, root=kb)

        assert f"{tool}: target KB {kb} (from env)" in proc.stdout

    @pytest.mark.parametrize("script,tool", [(COMPILE, "compile_facts"), (CHECK, "run_logic_check")])
    def test_flag_tier_is_announced_as_flag(self, script, tool, tmp_path, outside, config_home):
        kb = _seed_kb(tmp_path / "kb", CLEAN_ROWS)
        _activate(config_home, tmp_path / "other-kb")

        proc = _run(script, "--target", str(kb), cwd=outside, config_home=config_home)

        assert f"{tool}: target KB {kb} (from flag)" in proc.stdout

    @pytest.mark.parametrize("script,tool", [(COMPILE, "compile_facts"), (CHECK, "run_logic_check")])
    def test_cwd_tier_is_announced_as_cwd(self, script, tool, tmp_path, config_home):
        kb = _seed_kb(tmp_path / "kb", CLEAN_ROWS)
        _activate(config_home, None)

        proc = _run(script, cwd=kb, config_home=config_home)

        assert f"{tool}: target KB {kb} (from cwd)" in proc.stdout

    @pytest.mark.parametrize("script", [COMPILE, CHECK])
    def test_help_output_stays_pure_argparse(self, script, tmp_path, config_home):
        # --help resolves a root as a side effect of the pre-pass; announcing one for a
        # run that will not happen would put a KB path in front of every reader of the
        # usage text.
        proc = _run(script, "--help", cwd=tmp_path, config_home=config_home)

        assert proc.returncode == 0, proc.stderr
        assert "target KB" not in proc.stdout


class TestRunLogicCheckIsNotRefused:
    """The determinism gate names this script bare; the guard must not be able to stop it."""

    def test_an_unaimed_config_tier_run_is_not_refused(self, tmp_path, outside, config_home):
        kb = _seed_kb(tmp_path / "kb", CLEAN_ROWS)
        _activate(config_home, kb)

        proc = _run(CHECK, cwd=outside, config_home=config_home)

        assert "REFUSING" not in proc.stderr, proc.stderr
        # It gets as far as the check itself: rc 1 here is the missing accepted.dl of a
        # KB that was never compiled, not a guard turning the run away.
        assert proc.returncode != 2
        assert f"target KB {kb} (from config)" in proc.stdout

    def test_an_unaimed_run_writes_the_report_into_the_kb(self, tmp_path, outside, config_home):
        pytest.importorskip("pyrewire", reason="run_logic_check needs the engine")
        kb = _seed_kb(tmp_path / "kb", CLEAN_ROWS)
        _activate(config_home, kb)
        assert _run(COMPILE, cwd=outside, config_home=config_home).returncode == 0

        proc = _run(CHECK, cwd=outside, config_home=config_home)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        report = (kb / "facts" / "logic_report.txt").read_text(encoding="utf-8")
        assert "Logic Check Report" in report
        # The announcement belongs to stdout, never to the artifact the gate makes you
        # show verbatim.
        assert "target KB" not in report

    def test_an_in_process_main_announces_nothing(self, tmp_path, config_home):
        """``main()`` is a callable, not an entry point (the reason _parse_args sits in
        the ``__main__`` guard): several suites call it inside a host process that chose
        the KB itself, so the announcement is not its to print."""
        kb = _seed_kb(tmp_path / "kb", CLEAN_ROWS)
        _activate(config_home, kb)
        code = (
            "import sys; sys.path.insert(0, %r)\n" % str(REPO_ROOT / "tools")
            + "import run_logic_check as rlc\n"
            "try:\n"
            "    rlc.main()\n"
            "except Exception as exc:\n"  # FactlogError: nothing compiled in this KB
            "    print('RAISED', type(exc).__name__)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(tmp_path), capture_output=True, text=True,
            env=_env(config_home),
        )

        assert "target KB" not in proc.stdout, proc.stdout


class TestTheDeterminismGateFlow:
    """SKILL.md's prescribed shape, end to end: export the root, then call both bare."""

    def test_exported_root_lets_both_steps_run_and_heal_from_outside(self, tmp_path, outside, config_home):
        kb = _seed_kb(tmp_path / "kb", CONFLICT_ROWS)
        accepted = _snapshot(kb)
        _activate(config_home, kb)

        # `export FACTLOG_ROOT="$(factlog where --porcelain)"`
        where = subprocess.run(
            [sys.executable, "-m", "factlog", "where", "--porcelain"],
            cwd=str(outside), capture_output=True, text=True, env=_env(config_home),
        )
        assert where.returncode == 0, where.stderr
        exported = Path(where.stdout.strip())
        assert exported == kb.resolve()

        # Step 1 of /factlog check, no arguments — the contradiction gate applies in
        # full, deletion included, because the exported root aims the run.
        compile_proc = _run(COMPILE, cwd=outside, config_home=config_home, root=exported)
        assert compile_proc.returncode == 1
        assert not accepted.exists()
        assert "(from env)" in compile_proc.stdout

        # Resolve the contradiction the way the message says, then re-run both bare.
        (kb / "facts" / "candidates.csv").write_text(
            "\n".join([HEADER, *CLEAN_ROWS]) + "\n", encoding="utf-8"
        )
        compile_proc = _run(COMPILE, cwd=outside, config_home=config_home, root=exported)
        assert compile_proc.returncode == 0, compile_proc.stdout + compile_proc.stderr
        assert accepted.is_file()

        pytest.importorskip("pyrewire", reason="run_logic_check needs the engine")
        check = _run(CHECK, cwd=outside, config_home=config_home, root=exported)
        assert check.returncode == 0, check.stdout + check.stderr
        assert "Logic Check Report" in (kb / "facts" / "logic_report.txt").read_text(encoding="utf-8")
