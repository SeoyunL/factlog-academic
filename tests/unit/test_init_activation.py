# SPDX-License-Identifier: Apache-2.0
"""Creating a KB must not silently re-point the global active KB (#356).

``cmd_init``/``cmd_setup`` called ``factlog_config.write_root(target)``
unconditionally, so a single ``factlog init --target /tmp/scratch`` replaced
whichever KB the user had been working in — no confirmation, no record of the
old value, and nothing in the output that named it. Creating a KB and choosing
which KB is active are two different intents; only the second one owns the
global pointer.

The contract these pin: the pointer moves when nothing holds it yet (the
first-run experience ``setup`` exists for), when the target already *is* the
active KB (a no-op), or when the user asks with ``--activate``. Otherwise the
KB is created and the pointer is left alone, with the untouched value and the
command that would switch printed on stdout. ``--no-activate`` declines even
the first-run write.

Everything here drives the real entry point — ``python -m factlog`` in a
subprocess for ``init``, ``factlog.cli.main`` for ``setup`` (whose doctor/pip
stages are stubbed; the pointer decision itself is the un-stubbed code under
test). No test re-implements the decision it is checking.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT))
from factlog import cli  # noqa: E402
from factlog import config as factlog_config  # noqa: E402


def run_init(*args: str, config_home: Path):
    """Run ``python -m factlog init ...`` with an isolated config home.

    ``FACTLOG_ROOT`` is dropped: the repo-root conftest pins it for every test
    process, and an inherited value would silently decide the target these
    tests are choosing on purpose.
    """
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(config_home)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.pop("FACTLOG_ROOT", None)
    return subprocess.run(
        [sys.executable, "-m", "factlog", "init", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def config_file(config_home: Path) -> Path:
    return config_home / "factlog" / "config.json"


def write_pointer(config_home: Path, root: Path | str, **extra) -> bytes:
    """Seed the active-KB config the way ``factlog use`` leaves it."""
    path = config_file(config_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"root": str(root), **extra}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path.read_bytes()


def resolved(path: Path) -> str:
    """The absolute form ``write_root`` stores, so a comparison is not testing
    whether the temp dir happened to arrive already symlink-free."""
    return str(path.resolve())


def pointer(config_home: Path) -> str | None:
    path = config_file(config_home)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("root")


@pytest.fixture()
def config_home(tmp_path):
    home = tmp_path / "cfg"
    home.mkdir()
    return home


class TestInitDoesNotHijackAnExistingActiveKb:
    """The reproduction from the issue, at the CLI boundary."""

    def test_existing_pointer_survives_init_of_another_kb(self, tmp_path, config_home):
        active = tmp_path / "wiki"
        active.mkdir()
        write_pointer(config_home, active)

        scratch = tmp_path / "scratch"
        proc = run_init("--target", str(scratch), config_home=config_home)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert scratch.joinpath("sources").is_dir(), "init must still create the KB"
        assert pointer(config_home) == str(active), (
            "init re-pointed the active KB at the scratch KB: " + proc.stdout
        )

    def test_config_file_is_not_rewritten_at_all(self, tmp_path, config_home):
        """Byte-identical, not merely equal-after-parsing.

        ``write_root`` round-trips the whole config, so a re-write that happens
        to preserve ``root`` would still touch ``lang`` formatting and mask the
        very "something wrote here" signal this issue is about.
        """
        active = tmp_path / "wiki"
        active.mkdir()
        before = write_pointer(config_home, active, lang="ko")

        run_init("--target", str(tmp_path / "scratch"), config_home=config_home)

        assert config_file(config_home).read_bytes() == before

    def test_output_names_the_untouched_pointer_and_the_way_to_switch(self, tmp_path, config_home):
        active = tmp_path / "wiki"
        active.mkdir()
        write_pointer(config_home, active)
        scratch = tmp_path / "scratch"

        out = run_init("--target", str(scratch), config_home=config_home).stdout

        assert str(active) in out, f"the KB that stayed active is not named: {out}"
        assert "NOT active" in out, f"nothing says the new KB is inactive: {out}"
        assert f"factlog use {scratch}" in out, f"no command to switch: {out}"

    def test_dangling_pointer_is_not_silently_replaced(self, tmp_path, config_home):
        """A configured root that does not exist right now is still the user's choice.

        Adopting the new KB "because the old one is missing" would hand the
        pointer to ``init`` again the first time an external volume is
        unmounted. The hint tells the user how to move it deliberately.
        """
        gone = tmp_path / "unmounted"
        write_pointer(config_home, gone)

        proc = run_init("--target", str(tmp_path / "scratch"), config_home=config_home)

        assert pointer(config_home) == str(gone), proc.stdout


class TestReInitOfTheActiveKb:
    def test_is_a_no_op_that_says_so(self, tmp_path, config_home):
        active = tmp_path / "wiki"
        active.mkdir()
        before = write_pointer(config_home, active)

        proc = run_init("--target", str(active), config_home=config_home)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert config_file(config_home).read_bytes() == before
        assert "already active" in proc.stdout, proc.stdout


class TestFirstRunStillActivates:
    """The experience `setup` exists for, which the fix must not cost."""

    def test_no_config_at_all(self, tmp_path, config_home):
        kb = tmp_path / "wiki"

        proc = run_init("--target", str(kb), config_home=config_home)

        assert pointer(config_home) == resolved(kb), proc.stdout + proc.stderr
        assert "active KB set to" in proc.stdout

    def test_config_that_holds_only_a_language(self, tmp_path, config_home):
        """No ``root`` means nothing to lose — and the language must survive."""
        path = config_file(config_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"lang": "ko"}\n', encoding="utf-8")
        kb = tmp_path / "wiki"

        run_init("--target", str(kb), config_home=config_home)

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data.get("root") == resolved(kb)
        assert data.get("lang") == "ko"


class TestExplicitFlags:
    def test_activate_moves_the_pointer_and_prints_both_ends(self, tmp_path, config_home):
        old = tmp_path / "wiki"
        old.mkdir()
        write_pointer(config_home, old)
        new = tmp_path / "scratch"

        proc = run_init("--target", str(new), "--activate", config_home=config_home)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert pointer(config_home) == resolved(new)
        assert str(old) in proc.stdout, f"the replaced value is unrecoverable from the output: {proc.stdout}"
        assert str(new) in proc.stdout

    def test_activate_preserves_the_narration_language(self, tmp_path, config_home):
        old = tmp_path / "wiki"
        old.mkdir()
        write_pointer(config_home, old, lang="ko")
        new = tmp_path / "scratch"

        run_init("--target", str(new), "--activate", config_home=config_home)

        data = json.loads(config_file(config_home).read_text(encoding="utf-8"))
        assert data == {"root": resolved(new), "lang": "ko"}

    def test_no_activate_declines_even_the_first_run_write(self, tmp_path, config_home):
        kb = tmp_path / "wiki"

        proc = run_init("--target", str(kb), "--no-activate", config_home=config_home)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert kb.joinpath("sources").is_dir()
        assert not config_file(config_home).exists(), (
            "--no-activate still created an active-KB config: " + proc.stdout
        )
        assert f"factlog use {kb}" in proc.stdout, proc.stdout

    def test_the_two_flags_conflict(self, tmp_path, config_home):
        proc = run_init(
            "--target", str(tmp_path / "wiki"), "--activate", "--no-activate", config_home=config_home
        )

        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "not allowed with" in proc.stderr


class TestSetup:
    """`setup` shares the decision; its doctor/pip stages are stubbed away.

    Stubbing is limited to ``_pyrewire_ok``/``_run_doctor_checks`` so the test
    neither installs packages nor depends on the engine being present. The
    target resolution, the pointer decision and the write all run for real,
    through ``main`` and its argument parser.
    """

    @pytest.fixture(autouse=True)
    def _stub_environment_stages(self, monkeypatch, config_home):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        monkeypatch.setattr(cli, "_pyrewire_ok", lambda: True)
        monkeypatch.setattr(cli, "_run_doctor_checks", lambda *a, **k: True)

    def test_first_run_activates(self, tmp_path, config_home, capsys):
        kb = tmp_path / "wiki"

        assert cli.main(["setup", "--target", str(kb)]) == 0
        out = capsys.readouterr().out

        assert pointer(config_home) == resolved(kb), out
        assert kb.joinpath("sources").is_dir()

    def test_does_not_hijack_an_existing_pointer(self, tmp_path, config_home, capsys):
        active = tmp_path / "wiki"
        active.mkdir()
        before = write_pointer(config_home, active)
        scratch = tmp_path / "scratch"

        assert cli.main(["setup", "--target", str(scratch)]) == 0
        out = capsys.readouterr().out

        assert config_file(config_home).read_bytes() == before, out
        assert scratch.joinpath("sources").is_dir()
        assert f"factlog use {scratch}" in out, out

    def test_activate_flag_moves_the_pointer(self, tmp_path, config_home, capsys):
        active = tmp_path / "wiki"
        active.mkdir()
        write_pointer(config_home, active)
        scratch = tmp_path / "scratch"

        assert cli.main(["setup", "--target", str(scratch), "--activate"]) == 0
        out = capsys.readouterr().out

        assert pointer(config_home) == resolved(scratch), out
        assert str(active) in out

    def test_lang_flag_still_applies_without_activating(self, tmp_path, config_home, capsys):
        """``--lang`` edits the same file; declining the root must not decline it."""
        active = tmp_path / "wiki"
        active.mkdir()
        write_pointer(config_home, active)

        assert cli.main(["setup", "--target", str(tmp_path / "scratch"), "--lang", "ko"]) == 0
        capsys.readouterr()

        data = json.loads(config_file(config_home).read_text(encoding="utf-8"))
        assert data == {"root": str(active), "lang": "ko"}


class TestUseIsUnaffected:
    """`use` exists to move the pointer, so it keeps moving it unconditionally.

    A guard, not evidence: this passes before and after the fix. It is here
    because the obvious over-correction — refusing to overwrite a configured
    root anywhere in the CLI — would leave the user no way to switch at all.
    """

    def test_use_still_repoints(self, tmp_path, config_home, monkeypatch, capsys):
        old = tmp_path / "wiki"
        old.mkdir()
        new = tmp_path / "other"
        new.mkdir()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
        write_pointer(config_home, old)

        assert cli.main(["use", str(new)]) == 0
        capsys.readouterr()

        assert factlog_config.read_root() == str(new.resolve())
