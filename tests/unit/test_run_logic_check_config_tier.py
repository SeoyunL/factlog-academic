# SPDX-License-Identifier: Apache-2.0
"""``tools/run_logic_check.py`` honours the active-KB config tier (#528).

The skill's determinism gate names this script with no arguments — "Always run
``tools/run_logic_check.py`` and show the resulting ``facts/logic_report.txt``
verbatim" — so the documented form carries no KB flag and no ``FACTLOG_ROOT``.
Without a ``resolve_root_from_argv`` pre-pass the script bound its paths to cwd
and died with "not a factlog KB root" from anywhere else, which made the one
command the gate mandates unrunnable outside a KB directory.

The precedence pins below are engine-free on purpose: they read the root the
import-time pre-pass bound into ``common``, so they run identically on a machine
without pyrewire, where the end-to-end class skips. Resolution happens before
the engine is ever consulted, so that is the whole of what precedence means here.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools"
COMPILE = TOOLS / "compile_facts.py"
CHECK = TOOLS / "run_logic_check.py"
HEADER = "subject,relation,object,source,status,confidence,note"

# Import the script, then report the root ``common`` actually bound. The pre-pass
# runs at import of run_logic_check, so this is the value every path global in the
# module was derived from.
_PROBE = (
    "import run_logic_check;"  # imported for its import-time pre-pass, nothing else
    "import common;"
    "print(common.ROOT)"
)


def _env(root: Path | None = None, config_home: Path | None = None) -> dict[str, str]:
    """Child env with the repo importable and FACTLOG_ROOT under our control.

    The unit conftest pins FACTLOG_ROOT process-wide, so the config and cwd tiers
    are only reachable in a child that has it removed — inheriting it would make
    every tier below env untestable (and pass vacuously).
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(TOOLS), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    env.pop("FACTLOG_ROOT", None)
    if root is not None:
        env["FACTLOG_ROOT"] = str(root)
    if config_home is not None:
        env["XDG_CONFIG_HOME"] = str(config_home)
    return env


def _write_config(config_home: Path, root: Path) -> None:
    path = config_home / "factlog" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"root": str(root)}) + "\n", encoding="utf-8")


def _resolved_root(cwd: Path, *args: str, **env_kwargs) -> Path:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, *args],
        cwd=cwd, capture_output=True, text=True, check=True, env=_env(**env_kwargs),
    )
    return Path(proc.stdout.strip())


def _run_check(cwd: Path, *args: str, **env_kwargs) -> subprocess.CompletedProcess:
    """Run the script itself (not the import probe), so argparse gets a say.

    ``_resolved_root`` only imports the module, which exercises the pre-pass; the
    strict parse sits in the ``__main__`` guard and is reachable only by executing
    the script, which is also why an in-process ``main()`` never sees these flags.
    """
    return subprocess.run(
        [sys.executable, str(CHECK), *args],
        cwd=cwd, capture_output=True, text=True, env=_env(**env_kwargs),
    )


def _advertised_root_flags(tmp_path: Path) -> list[str]:
    """The long options ``--help`` advertises, less ``--help`` itself.

    Read out of the parser's own output rather than copied from a literal list, so
    a flag added to one tier and not the other surfaces here instead of in a user's
    silently-retargeted run.
    """
    proc = _run_check(tmp_path, "--help")
    assert proc.returncode == 0, proc.stderr
    return sorted(set(re.findall(r"--[a-z][a-z0-9-]*", proc.stdout)) - {"--help"})


def _init_kb(kb: Path, config_home: Path) -> Path:
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        check=True, capture_output=True, env=_env(root=kb.parent, config_home=config_home),
    )
    return kb


@pytest.fixture
def config_home(tmp_path, monkeypatch) -> Path:
    """A per-test XDG config home, layered over the session-wide sandbox.

    The autouse isolation fixture already keeps writes out of the developer's real
    config; this narrows it further so one test's active KB cannot decide another's.
    """
    home = tmp_path / "xdg"
    home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home


class TestRootPrecedence:
    """``--target`` > ``$FACTLOG_ROOT`` > config > cwd, engine-free."""

    def test_config_active_kb_is_used_from_an_unrelated_cwd(self, tmp_path, config_home):
        """The #528 repro: no flag, no env, cwd is not a KB."""
        kb = tmp_path / "config-kb"
        kb.mkdir()
        _write_config(config_home, kb)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        assert _resolved_root(elsewhere, config_home=config_home) == kb.resolve()

    def test_env_beats_config(self, tmp_path, config_home):
        env_kb = tmp_path / "env-kb"
        env_kb.mkdir()
        cfg_kb = tmp_path / "config-kb"
        cfg_kb.mkdir()
        _write_config(config_home, cfg_kb)

        root = _resolved_root(tmp_path, root=env_kb, config_home=config_home)
        assert root == env_kb.resolve()

    def test_flag_beats_env_and_config(self, tmp_path, config_home):
        flag_kb = tmp_path / "flag-kb"
        flag_kb.mkdir()
        env_kb = tmp_path / "env-kb"
        env_kb.mkdir()
        cfg_kb = tmp_path / "config-kb"
        cfg_kb.mkdir()
        _write_config(config_home, cfg_kb)

        root = _resolved_root(
            tmp_path, "--target", str(flag_kb), root=env_kb, config_home=config_home
        )
        assert root == flag_kb.resolve()

    def test_cwd_is_the_last_resort(self, tmp_path, config_home):
        """No flag, no env, no configured KB — the pre-#528 behaviour, unchanged."""
        kb = tmp_path / "cwd-kb"
        kb.mkdir()

        assert _resolved_root(kb, config_home=config_home) == kb.resolve()

    def test_config_does_not_override_a_kb_the_caller_stands_in(self, tmp_path, config_home):
        """cwd loses to config — the counter-example to "run it where you are".

        Pinned because the opposite reading is tempting and would silently write one
        KB's logic_report.txt while the operator reads another's.
        """
        cwd_kb = tmp_path / "cwd-kb"
        cwd_kb.mkdir()
        cfg_kb = tmp_path / "config-kb"
        cfg_kb.mkdir()
        _write_config(config_home, cfg_kb)

        assert _resolved_root(cwd_kb, config_home=config_home) == cfg_kb.resolve()


class TestReportUnchanged:
    """End-to-end: the report and the rc rule survive the new pre-pass."""

    def _seed(self, kb: Path, config_home: Path, query: str = "") -> None:
        _init_kb(kb, config_home)
        (kb / "sources" / "a.md").write_text("a\n", encoding="utf-8")
        (kb / "facts" / "candidates.csv").write_text(
            f"{HEADER}\nA,uses,B,sources/a.md,confirmed,0.90,\n", encoding="utf-8"
        )
        if query:
            (kb / "facts" / "query.dl").write_text(query, encoding="utf-8")
        subprocess.run(
            [sys.executable, str(COMPILE)], cwd=kb, check=True, capture_output=True,
            env=_env(root=kb, config_home=config_home),
        )

    def test_config_kb_report_is_written_outside_it(self, tmp_path, config_home):
        pytest.importorskip("pyrewire", reason="run_logic_check needs the engine")
        kb = tmp_path / "kb"
        self._seed(kb, config_home, query='relation("A", "uses", O)?\n')
        _write_config(config_home, kb)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        proc = subprocess.run(
            [sys.executable, str(CHECK)], cwd=elsewhere, capture_output=True, text=True,
            env=_env(config_home=config_home),
        )

        assert proc.returncode == 0, proc.stderr
        report = (kb / "facts" / "logic_report.txt").read_text(encoding="utf-8")
        assert "Logic Check Report" in report
        assert "engine facts: 1" in report
        assert "errors: 0" in report
        # The report belongs to the KB, not to wherever the operator happened to stand.
        assert not (elsewhere / "facts").exists()

    def test_inside_the_kb_the_report_is_identical(self, tmp_path, config_home):
        """Same KB, run from inside with FACTLOG_ROOT — byte-for-byte the same report."""
        pytest.importorskip("pyrewire", reason="run_logic_check needs the engine")
        kb = tmp_path / "kb"
        self._seed(kb, config_home, query='relation("A", "uses", O)?\n')
        report_path = kb / "facts" / "logic_report.txt"

        inside = subprocess.run(
            [sys.executable, str(CHECK)], cwd=kb, capture_output=True, text=True,
            env=_env(root=kb, config_home=config_home),
        )
        assert inside.returncode == 0, inside.stderr
        from_inside = report_path.read_text(encoding="utf-8")

        _write_config(config_home, kb)
        report_path.unlink()
        outside = subprocess.run(
            [sys.executable, str(CHECK)], cwd=tmp_path, capture_output=True, text=True,
            env=_env(config_home=config_home),
        )
        assert outside.returncode == 0, outside.stderr

        assert report_path.read_text(encoding="utf-8") == from_inside

    def test_an_error_still_exits_nonzero_through_the_config_tier(self, tmp_path, config_home):
        """rc != 0 only on errors — and reaching the KB indirectly must not soften it."""
        pytest.importorskip("pyrewire", reason="run_logic_check needs the engine")
        kb = tmp_path / "kb"
        self._seed(kb, config_home, query='count("A")?\n')
        _write_config(config_home, kb)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        proc = subprocess.run(
            [sys.executable, str(CHECK)], cwd=elsewhere, capture_output=True, text=True,
            env=_env(config_home=config_home),
        )

        assert proc.returncode == 1
        report = (kb / "facts" / "logic_report.txt").read_text(encoding="utf-8")
        assert "Errors:" in report


class TestArgumentParsing:
    """The strict parse of the command line, and its agreement with the pre-pass.

    The pre-pass has to run before ``common`` is imported, so it can only ever be a
    ``parse_known_args`` peek — it cannot reject anything. With nothing else parsing
    the command line, ``--targt /intended-kb`` was dropped without a word and the
    check ran against the config's active KB while the operator read the output as
    a verdict on the KB they had named. Engine-free: argument handling is settled
    before pyrewire is consulted, so these run where the end-to-end class skips.
    """

    def test_a_misspelled_root_flag_is_rejected(self, tmp_path, config_home):
        """The #528 repro: a typo must exit 2, not retarget the run in silence."""
        cfg_kb = tmp_path / "config-kb"
        cfg_kb.mkdir()
        _write_config(config_home, cfg_kb)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        proc = _run_check(
            elsewhere, "--targt", str(tmp_path / "intended-kb"), config_home=config_home
        )

        assert proc.returncode == 2, proc.stdout
        assert "unrecognized arguments: --targt" in proc.stderr
        # The give-away of the old behaviour: it got far enough to touch a KB.
        assert not (cfg_kb / "facts").exists()

    def test_an_unsupported_flag_is_rejected(self, tmp_path, config_home):
        """Not only near-misses: anything the tool does not implement exits 2."""
        proc = _run_check(tmp_path, "--strict", config_home=config_home)

        assert proc.returncode == 2
        assert "unrecognized arguments: --strict" in proc.stderr

    def test_help_lists_the_root_flag(self, tmp_path, config_home):
        proc = _run_check(tmp_path, "--help", config_home=config_home)

        assert proc.returncode == 0, proc.stderr
        assert "--target" in proc.stdout
        assert "run_logic_check" in proc.stdout

    def test_the_advertised_flags_are_exactly_the_pair_the_prepass_reads(self, tmp_path):
        """Pin the set on both sides at once.

        ``--target`` is canonical (ask_router and the CLI subcommands spell it that
        way); ``--wiki`` is the alias the sibling engine scripts use. Adding a third
        spelling to the parser without teaching the pre-pass would leave it accepted
        and ignored, so the set is pinned rather than merely non-empty.
        """
        assert _advertised_root_flags(tmp_path) == ["--target", "--wiki"]

    @pytest.mark.parametrize("flag", ["--target", "--wiki"])
    def test_every_advertised_flag_actually_moves_the_root(self, flag, tmp_path, config_home):
        """The parser tier and the pre-pass tier land on the same KB.

        Accepted-by-argparse is not the property that matters: what matters is that
        the root ``common`` binds is the one the operator named, from the tier that
        runs first.
        """
        flag_kb = tmp_path / "flag-kb"
        flag_kb.mkdir()
        cfg_kb = tmp_path / "config-kb"
        cfg_kb.mkdir()
        _write_config(config_home, cfg_kb)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        # pre-pass tier: the root bound into common is the flag's, not the config's.
        assert _resolved_root(
            elsewhere, flag, str(flag_kb), config_home=config_home
        ) == flag_kb.resolve()
        # strict tier: the same spelling survives main()'s parser.
        proc = _run_check(elsewhere, flag, str(flag_kb), config_home=config_home)
        assert proc.returncode != 2, proc.stderr
        assert "unrecognized" not in proc.stderr

    def test_the_no_argument_form_the_gate_mandates_still_parses(self, tmp_path, config_home):
        """SKILL.md's determinism gate runs this script bare — argparse must not object.

        rc 1 here is the missing-accepted.dl failure from an empty directory, i.e. the
        run got past argument handling and into the check. rc 2 would mean the strict
        parse had broken the one invocation the gate mandates.
        """
        proc = _run_check(tmp_path, config_home=config_home)

        assert proc.returncode != 2, proc.stderr
        assert "unrecognized" not in proc.stderr

    def test_an_in_process_main_does_not_parse_the_hosts_argv(self, tmp_path, config_home):
        """``main()`` is a callable, not an entry point — the argv is not its to reject.

        Several suites call ``run_logic_check.main()`` inside another process (pytest,
        `python -c` harnesses) whose ``sys.argv`` carries that host's own arguments.
        Parsing strictly from inside ``main()`` turned every one of those into exit 2,
        so the strict parse lives in the ``__main__`` guard instead. The error here
        must be the missing engine input, never an argparse rejection.
        """
        code = (
            "import sys; sys.argv = ['pytest', 'tests/unit', '-q']\n"
            "import run_logic_check as rlc\n"
            "try:\n"
            "    rlc.main()\n"
            "except SystemExit as exc:\n"
            "    print('SYSTEMEXIT', exc.code)\n"
            "except Exception as exc:\n"  # FactlogError: no accepted.dl in an empty dir
            "    print('RAISED', type(exc).__name__)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True,
            env=_env(config_home=config_home),
        )

        assert "SYSTEMEXIT" not in proc.stdout, proc.stdout
        assert "RAISED FactlogError" in proc.stdout, proc.stdout + proc.stderr
