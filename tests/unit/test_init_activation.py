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
        assert "is not recorded in the config" in out, f"nothing says the new KB is not the recorded one: {out}"
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
        """Seeded in a format ``_write_config`` would not produce, plus mtime.

        The first version wrote the seed with the same serialiser the product
        uses — two-space indent, trailing newline — so a rewrite reproduced the
        file byte for byte and ``read_bytes()`` could not see it. Flipping this
        branch to write left the whole suite green. Compact JSON makes a rewrite
        visible in the bytes, and ``st_mtime_ns`` catches it even if a future
        serialiser change happens to match the seed again.
        """
        active = tmp_path / "wiki"
        active.mkdir()
        path = config_file(config_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Deliberately NOT write_pointer(): compact, no trailing newline, keys in
        # an order the writer would not emit.
        path.write_text('{"lang":"ko","root":"%s"}' % active, encoding="utf-8")
        before = path.read_bytes()
        before_mtime = path.stat().st_mtime_ns

        proc = run_init("--target", str(active), config_home=config_home)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert path.read_bytes() == before, f"the no-op rewrote the file: {proc.stdout}"
        assert path.stat().st_mtime_ns == before_mtime, "the file was rewritten with identical bytes"
        assert "already recorded" in proc.stdout, proc.stdout


class TestFirstRunStillActivates:
    """GUARDS, not evidence: every test here passes before and after the fix.

    They hold the experience `setup` exists for, which is what this change could
    most plausibly have broken. None of them demonstrates the new behaviour.
    """

    def test_no_config_at_all(self, tmp_path, config_home):
        kb = tmp_path / "wiki"

        proc = run_init("--target", str(kb), config_home=config_home)

        assert pointer(config_home) == resolved(kb), proc.stdout + proc.stderr
        assert "active-KB config set to" in proc.stdout

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


class TestDamagedConfigIsNotOverwritten:
    """A config that cannot be parsed is held, not treated as a fresh install.

    ``read_root`` folds bad JSON, a non-object, and an ``OSError`` all into
    ``None`` — correct for a reader that must degrade to cwd, fatal for a writer:
    ``None`` used to mean "first run, safe to write", so the #356 hijack survived
    on exactly the configs where it is *least* recoverable. A root pointing at an
    unmounted volume at least survives as text; a truncated
    ``{"root": "/real/kb",`` was rewritten to ``{"root": "/scratch"}`` and the
    original path was gone. An unreadable file took ``lang`` with it, because
    ``write_root`` rebuilds the file from a read that returned ``{}``.

    Byte-identity is the assertion, not "root is still X" — with the file
    unparseable there is no root to compare, and the bytes are the whole point.
    """

    @pytest.mark.parametrize(
        ("label", "content"),
        [
            ("truncated json", '{"root": "/real/kb", '),
            ("empty file", ""),
            ("not an object", "[1, 2, 3]"),
            ("not json at all", "root = /real/kb\n"),
        ],
    )
    def test_left_byte_identical(self, tmp_path, config_home, label, content):
        path = config_file(config_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        before = path.read_bytes()
        scratch = tmp_path / "scratch"

        proc = run_init("--target", str(scratch), config_home=config_home)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert scratch.joinpath("sources").is_dir(), "init must still create the KB"
        assert path.read_bytes() == before, f"{label}: init overwrote a config it could not read: {proc.stdout}"

    def test_says_it_could_not_read_the_file(self, tmp_path, config_home):
        path = config_file(config_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"root": "/real/kb", ', encoding="utf-8")

        out = run_init("--target", str(tmp_path / "scratch"), config_home=config_home).stdout

        assert "could not be read" in out, f"silently skipped instead of reporting: {out}"
        assert str(path) in out, f"the file to repair is not named: {out}"

    def test_an_unreadable_file_keeps_its_language(self, tmp_path, config_home):
        """``chmod 000``: the read fails, so a write would drop ``lang`` too."""
        path = config_file(config_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        before = write_pointer(config_home, tmp_path / "wiki", lang="ko")
        path.chmod(0o000)
        try:
            proc = run_init("--target", str(tmp_path / "scratch"), config_home=config_home)
        finally:
            path.chmod(0o644)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert path.read_bytes() == before, proc.stdout
        assert json.loads(path.read_text(encoding="utf-8"))["lang"] == "ko"

    def test_activate_replaces_it_and_says_what_it_replaced(self, tmp_path, config_home):
        """The escape hatch out of a corrupt config — explicit, and disclosed."""
        path = config_file(config_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"root": "/real/kb", ', encoding="utf-8")
        scratch = tmp_path / "scratch"

        proc = run_init("--target", str(scratch), "--activate", config_home=config_home)

        assert pointer(config_home) == resolved(scratch), proc.stdout
        assert "unreadable" in proc.stdout, f"replaced a corrupt config without saying so: {proc.stdout}"

    def test_a_readable_config_with_no_usable_root_still_activates(self, tmp_path, config_home):
        """GUARD, not evidence — deliberately on the *write* side of the line.

        It passes before and after, and that is the claim: the narrowing above
        must not have swept this case up with the damaged ones.

        ``{"root": ""}`` parses: it is understood, it holds no path to lose, and
        ``resolve_root`` already reports such a config as recording nothing. If
        this counted as held, anyone who ran ``factlog lang`` before their first
        ``init`` would be denied the first-run experience, and ``init`` would
        disagree with the precedence every other command follows. ``lang`` is
        preserved because the file was read, not because it was skipped.
        """
        path = config_file(config_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"root": "", "lang": "ko"}\n', encoding="utf-8")
        kb = tmp_path / "wiki"

        run_init("--target", str(kb), config_home=config_home)

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"root": resolved(kb), "lang": "ko"}


class TestEnvOverrideIsDisclosed:
    """These commands write the config, which is only rank 3 of the precedence.

    ``SKILL.md`` tells every flow to export ``$FACTLOG_ROOT`` first, so a message
    phrased as a claim about "the active KB" contradicts
    ``factlog where --porcelain`` — the one output the skill machine-reads — in
    the recommended state, not in an edge case.
    """

    def run_with_env(self, *args: str, config_home: Path, env_root: Path):
        env = dict(os.environ)
        env["XDG_CONFIG_HOME"] = str(config_home)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["FACTLOG_ROOT"] = str(env_root)
        return subprocess.run(
            [sys.executable, "-m", "factlog", *args],
            cwd=str(REPO_ROOT), capture_output=True, text=True, env=env, check=False,
        )

    def test_init_does_not_contradict_where_porcelain(self, tmp_path, config_home):
        cfg_kb = tmp_path / "wiki"
        cfg_kb.mkdir()
        write_pointer(config_home, cfg_kb)
        env_kb = tmp_path / "envkb"
        env_kb.mkdir()
        scratch = tmp_path / "scratch"

        out = self.run_with_env(
            "init", "--target", str(scratch), config_home=config_home, env_root=env_kb
        ).stdout
        porcelain = self.run_with_env(
            "where", "--porcelain", config_home=config_home, env_root=env_kb
        ).stdout.strip()

        assert porcelain == resolved(env_kb), "precondition: $FACTLOG_ROOT should win in `where`"
        assert "active KB unchanged" not in out, (
            f"claims the active KB is {cfg_kb} while `where` reports {porcelain}: {out}"
        )
        assert "$FACTLOG_ROOT" in out, f"nothing discloses that the environment outranks the config: {out}"
        assert str(env_kb) in out

    def test_no_note_when_the_environment_agrees_with_the_target(self, tmp_path, config_home):
        """GUARD: passes before and after — the note must not become noise.

        A flagless run resolves its target *from* `$FACTLOG_ROOT`, so nothing is
        being overridden. That is every SKILL.md flow, which is why it is worth
        holding.
        """
        env_kb = tmp_path / "envkb"

        out = self.run_with_env("init", config_home=config_home, env_root=env_kb).stdout

        assert "outranks" not in out, out

    def test_silent_when_the_environment_and_the_config_agree(self, tmp_path, config_home):
        """The predicate is env vs **config**, not env vs target.

        Comparing against the target fired here — env and config both name
        ``/wiki``, only the target differs — and announced an override that is
        not happening. `where --porcelain` returns the same ``/wiki`` the config
        holds, so there is nothing to disclose.
        """
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        write_pointer(config_home, wiki)

        out = self.run_with_env(
            "init", "--target", str(tmp_path / "scratch"), config_home=config_home, env_root=wiki
        ).stdout

        assert "outranks" not in out, f"announced an override while env and config agree: {out}"

    def test_no_reach_note_when_it_would_only_restate_the_summary(self, tmp_path, config_home):
        """#356's own reproduction path is the one most readers will meet.

        With no ``$FACTLOG_ROOT``, the summary already says the config records
        ``/wiki`` and not ``/scratch``, and the hint already gives
        ``factlog use /scratch``. A third line saying a flagless command reaches
        ``/wiki`` from the config adds nothing. ``setup``'s closing line still
        carries it, because there the fact has not been said yet — held by
        ``TestSetup::test_closing_line_names_the_target_when_it_is_not_recorded``.
        """
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        write_pointer(config_home, wiki)

        out = run_init("--target", str(tmp_path / "scratch"), config_home=config_home).stdout

        assert "a flagless command" not in out, f"restated the summary a third time: {out}"
        assert str(wiki) in out, "the untouched value must still be named once"

    def test_reach_note_survives_when_the_source_is_not_the_config(self, tmp_path, config_home):
        """The suppression is one source wide, not a blanket mute."""
        cfg_kb = tmp_path / "cfgkb"
        cfg_kb.mkdir()
        write_pointer(config_home, cfg_kb)
        env_kb = tmp_path / "envkb"
        env_kb.mkdir()

        out = self.run_with_env(
            "init", "--target", str(tmp_path / "scratch"), config_home=config_home, env_root=env_kb
        ).stdout

        assert "a flagless command" in out, f"muted a note that names a different KB: {out}"
        assert str(env_kb) in out

    def test_fires_when_the_environment_outranks_a_config_that_equals_the_target(
        self, tmp_path, config_home
    ):
        """The other direction of the same defect, and the worse one.

        env names ``/envkb``, the config names ``/cfgkb``, and the target *is*
        ``/envkb``. Comparing env against the target found them equal and stayed
        silent — exactly when the user most needs to know the config is not what
        is in force. The KB is reachable now only because the environment says
        so, and that lasts until the shell does.
        """
        cfg_kb = tmp_path / "cfgkb"
        cfg_kb.mkdir()
        write_pointer(config_home, cfg_kb)
        env_kb = tmp_path / "envkb"

        out = self.run_with_env(
            "init", "--target", str(env_kb), config_home=config_home, env_root=env_kb
        ).stdout

        assert "outranks" in out, f"stayed silent while $FACTLOG_ROOT overrode {cfg_kb}: {out}"
        assert str(env_kb) in out


class TestUseOwnsTheSameDisclosures:
    """`use` is where our own hint sends people, so it owes what `init` says.

    ``init`` printed "NOT recorded there / to record it: factlog use <kb>" plus
    the note that ``$FACTLOG_ROOT`` outranks the config — and then ``use`` said
    "active KB set to <kb>" with nothing, while ``where --porcelain`` still
    printed the environment's KB. The contradiction we fixed twice survived at
    the destination of the fix.
    """

    def run_use(self, *args: str, config_home: Path, env_root: Path | None = None):
        env = dict(os.environ)
        env["XDG_CONFIG_HOME"] = str(config_home)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env.pop("FACTLOG_ROOT", None)
        if env_root is not None:
            env["FACTLOG_ROOT"] = str(env_root)
        return subprocess.run(
            [sys.executable, "-m", "factlog", *args],
            cwd=str(REPO_ROOT), capture_output=True, text=True, env=env, check=False,
        )

    def test_use_discloses_that_the_environment_outranks_what_it_just_wrote(
        self, tmp_path, config_home
    ):
        newkb = tmp_path / "newkb"
        (newkb / "sources").mkdir(parents=True)
        env_kb = tmp_path / "envkb"
        env_kb.mkdir()

        proc = self.run_use("use", str(newkb), config_home=config_home, env_root=env_kb)
        porcelain = self.run_use(
            "where", "--porcelain", config_home=config_home, env_root=env_kb
        ).stdout.strip()

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert porcelain == resolved(env_kb), "precondition: the environment should win"
        assert "$FACTLOG_ROOT" in proc.stdout, (
            f"`use` claimed the KB was set with nothing to say `where` disagrees: {proc.stdout}"
        )
        assert str(env_kb) in proc.stdout

    def test_use_says_nothing_extra_when_it_is_the_whole_story(self, tmp_path, config_home):
        """GUARD: no environment, so `use` keeps its original two-line output."""
        newkb = tmp_path / "newkb"
        (newkb / "sources").mkdir(parents=True)

        proc = self.run_use("use", str(newkb), config_home=config_home)

        assert "$FACTLOG_ROOT" not in proc.stdout, proc.stdout
        assert "a flagless command" not in proc.stdout, proc.stdout

    def test_use_discloses_the_language_it_destroys(self, tmp_path, config_home):
        """`init --activate` says it replaced an unreadable config; `use` did not.

        `use` still goes ahead — re-pointing is the command, and it is the way
        out of a damaged config — but the `lang` in those unreadable bytes is
        gone, and that was silent.
        """
        path = config_file(config_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"root": "/real/kb", "lang": "ko"', encoding="utf-8")
        newkb = tmp_path / "newkb"
        (newkb / "sources").mkdir(parents=True)

        proc = self.run_use("use", str(newkb), config_home=config_home)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert pointer(config_home) == resolved(newkb)
        assert "unreadable" in proc.stdout, f"replaced a damaged config silently: {proc.stdout}"


class TestImplicitTargetNeverLandsInTheCurrentDirectory:
    """The body claims cwd is excluded; `$FACTLOG_ROOT` was a way back in.

    ``where --porcelain`` prints cwd when nothing is configured, and SKILL.md
    tells every flow to export that value — so a bare ``init`` in a directory of
    unrelated files scattered a KB layout across it. The hard-coded ``~/wiki``
    default this branch replaced made that impossible, so the branch introduced
    it.
    """

    def run_bare_init(self, cwd: Path, config_home: Path, env_root: Path | None = None):
        env = dict(os.environ)
        env["XDG_CONFIG_HOME"] = str(config_home)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env.pop("FACTLOG_ROOT", None)
        if env_root is not None:
            env["FACTLOG_ROOT"] = str(env_root)
        return subprocess.run(
            [sys.executable, "-m", "factlog", "init"],
            cwd=str(cwd), capture_output=True, text=True, env=env, check=False,
        )

    def test_refuses_a_directory_of_unrelated_files(self, tmp_path, config_home):
        work = tmp_path / "work"
        work.mkdir()
        (work / "README.md").write_text("mine\n", encoding="utf-8")

        proc = self.run_bare_init(work, config_home, env_root=work)

        assert proc.returncode != 0, proc.stdout
        assert sorted(p.name for p in work.iterdir()) == ["README.md"], (
            f"scaffolded into a directory the user never named: {sorted(p.name for p in work.iterdir())}"
        )
        assert "--target" in proc.stderr, proc.stderr

    def test_an_explicit_target_is_still_allowed(self, tmp_path, config_home):
        """Naming it *is* the consent the implicit path lacks."""
        work = tmp_path / "work"
        work.mkdir()
        (work / "README.md").write_text("mine\n", encoding="utf-8")

        env = dict(os.environ)
        env["XDG_CONFIG_HOME"] = str(config_home)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env.pop("FACTLOG_ROOT", None)
        proc = subprocess.run(
            [sys.executable, "-m", "factlog", "init", "--target", str(work)],
            cwd=str(work), capture_output=True, text=True, env=env, check=False,
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (work / "sources").is_dir()

    def test_an_empty_current_directory_is_fine(self, tmp_path, config_home):
        """GUARD: nothing to scatter over, so the refusal must not fire."""
        work = tmp_path / "work"
        work.mkdir()

        proc = self.run_bare_init(work, config_home, env_root=work)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (work / "sources").is_dir()

    def test_re_running_inside_an_existing_kb_is_fine(self, tmp_path, config_home):
        """GUARD: an idempotent re-scaffold of a real KB you are standing in."""
        kb = tmp_path / "kb"
        (kb / "sources").mkdir(parents=True)
        (kb / "notes.md").write_text("x\n", encoding="utf-8")

        proc = self.run_bare_init(kb, config_home, env_root=kb)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (kb / "facts").is_dir()

    def test_an_empty_target_is_an_error_not_a_synonym_for_omitted(self, tmp_path, config_home):
        """``--target ''`` printed "no --target given" and scaffolded elsewhere."""
        proc = subprocess.run(
            [sys.executable, "-m", "factlog", "init", "--target", ""],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            env={**os.environ, "XDG_CONFIG_HOME": str(config_home), "PYTHONPATH": str(REPO_ROOT)},
            check=False,
        )

        assert proc.returncode != 0, proc.stdout
        assert "empty" in proc.stderr, proc.stderr
        assert "no --target given" not in proc.stdout, proc.stdout


class TestADirectoryAtTheConfigPath:
    """Both advertised ways out of a damaged config died on the same exception.

    ``config_status`` asked ``is_file()``, so a directory classified as MISSING —
    breaking its own invariant that only MISSING means nothing is recorded — and
    every caller then took the write path, where ``os.replace`` raises
    ``IsADirectoryError``. The exception predates this branch; both exits failing
    at once does not, because this branch is what promised a way out.
    """

    @pytest.fixture()
    def blocked(self, config_home):
        path = config_file(config_home)
        path.mkdir(parents=True)
        return path

    def test_it_is_classified_unreadable_not_missing(self, blocked, config_home, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
        assert factlog_config.config_status() == factlog_config.UNREADABLE

    def test_plain_init_refuses_without_a_traceback(self, tmp_path, blocked, config_home):
        proc = run_init("--target", str(tmp_path / "kb"), config_home=config_home)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "Traceback" not in proc.stderr, proc.stderr
        assert "could not be read" in proc.stdout, proc.stdout

    @pytest.mark.parametrize("argv", [("init", "--activate"), ("use",)])
    def test_both_escape_hatches_explain_instead_of_crashing(
        self, tmp_path, blocked, config_home, argv
    ):
        kb = tmp_path / "kb"
        (kb / "sources").mkdir(parents=True)
        args = [sys.executable, "-m", "factlog", argv[0]]
        args += ["--target", str(kb), "--activate"] if argv[0] == "init" else [str(kb)]
        proc = subprocess.run(
            args,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            env={**os.environ, "XDG_CONFIG_HOME": str(config_home), "PYTHONPATH": str(REPO_ROOT)},
            check=False,
        )

        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "Traceback" not in proc.stderr, proc.stderr
        assert "IsADirectoryError" not in proc.stderr, proc.stderr
        assert str(factlog_config.config_path().name) in proc.stderr or "in the way" in proc.stderr, (
            proc.stderr
        )
        # The message ends "Nothing was changed", and the atomic writer stages a
        # `.tmp` sibling before the swap. Without cleanup on the failure path that
        # sentence was false and a stray config.json.tmp stayed next to the real
        # config for good.
        strays = [p.name for p in blocked.parent.iterdir() if p.name.endswith(".tmp")]
        assert not strays, f"a failed write left {strays} behind while claiming nothing changed"


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
        """GUARD, not evidence: passes before and after the fix.

        It is here because the first-run experience is the thing `setup` exists
        for and the thing this change could most easily have cost — not because
        it demonstrates anything about the change.
        """
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

    def test_closing_line_names_the_target_when_it_is_not_recorded(self, tmp_path, config_home, capsys):
        """The last line is the one a user (or an LLM) acts on.

        "run /factlog sync inside your knowledge base" reads as the KB setup just
        made, but a flagless `sync` follows the config — the *other* KB. So the
        closing line has to name the target and how to reach it.
        """
        active = tmp_path / "wiki"
        active.mkdir()
        write_pointer(config_home, active)
        scratch = tmp_path / "scratch"

        assert cli.main(["setup", "--target", str(scratch)]) == 0
        closing = capsys.readouterr().out.strip().splitlines()[-1]

        assert str(scratch) in closing, f"the closing line does not name the KB just created: {closing}"
        assert str(active) in closing, f"the closing line does not name where a flagless command goes: {closing}"
        assert "flagless" in closing, closing
        assert f"--target {scratch}" in closing or f"factlog use {scratch}" in closing, closing

    def test_summary_block_carries_the_hint(self, tmp_path, config_home, capsys):
        """The hint was printed twenty-odd lines above the summary, and lost.

        The block is cut *before* the closing line, and the assertion is on the
        ``→`` prefix the summary gives its notes. An earlier version sliced to
        the end of the output and asserted on ``factlog use <target>``, which the
        closing line also contains — so deleting the hint entirely left it green.
        It was matching the wrong line and duplicating
        ``test_closing_line_names_the_target_when_it_is_not_recorded``.
        """
        active = tmp_path / "wiki"
        active.mkdir()
        write_pointer(config_home, active)
        scratch = tmp_path / "scratch"

        assert cli.main(["setup", "--target", str(scratch)]) == 0
        out = capsys.readouterr().out

        summary_block = out.split("=== factlog setup: summary ===", 1)[-1]
        summary_block = summary_block.split("\nfactlog setup complete", 1)[0]
        assert f"→ to record it in the config: factlog use {scratch}" in summary_block, (
            f"the way to record it never reaches the summary block: {summary_block}"
        )

    def test_closing_line_stays_generic_when_the_target_is_recorded(self, tmp_path, config_home, capsys):
        """GUARD: the first-run wording must survive the change above."""
        kb = tmp_path / "wiki"

        assert cli.main(["setup", "--target", str(kb)]) == 0
        closing = capsys.readouterr().out.strip().splitlines()[-1]

        assert "inside your knowledge base" in closing, closing
        assert "flagless command would target" not in closing, closing

    def test_closing_line_is_generic_when_factlog_root_names_the_target(
        self, tmp_path, config_home, monkeypatch, capsys
    ):
        """`setup`'s whole environment dimension was untested (the fixture unset it).

        With ``$FACTLOG_ROOT`` naming the new KB and the config naming another,
        the config-based closing line said the new KB was NOT the active one and
        that a flagless sync would go elsewhere. Both were false:
        ``where --porcelain`` returns the target, because the environment
        outranks the config. The closing line has to ask ``resolve_root``.
        """
        other = tmp_path / "wiki"
        other.mkdir()
        write_pointer(config_home, other)
        scratch = tmp_path / "scratch"
        monkeypatch.setenv("FACTLOG_ROOT", str(scratch))

        assert cli.main(["setup", "--target", str(scratch)]) == 0
        closing = capsys.readouterr().out.strip().splitlines()[-1]

        assert "inside your knowledge base" in closing, (
            f"claims the KB is unreachable while $FACTLOG_ROOT names it: {closing}"
        )
        assert factlog_config.resolve_root()[0] == resolved(scratch), "precondition"

    def test_closing_line_names_the_target_on_a_damaged_config(self, tmp_path, config_home, capsys):
        """The UNREADABLE branch reaches the closing line too.

        Nothing pinned it: flipping that branch's decision left the suite green
        while `setup` closed with the generic "run /factlog sync inside your
        knowledge base" on a config it had just refused to touch — so a flagless
        sync would fall through to cwd, not the new KB.
        """
        path = config_file(config_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"root": "/real/kb", ', encoding="utf-8")
        scratch = tmp_path / "scratch"

        assert cli.main(["setup", "--target", str(scratch)]) == 0
        closing = capsys.readouterr().out.strip().splitlines()[-1]

        assert "inside your knowledge base" not in closing, (
            f"a damaged config still got the everything-is-fine closing line: {closing}"
        )
        assert str(scratch) in closing, closing
        assert "flagless" in closing, closing

    def test_lang_is_not_applied_to_a_config_that_could_not_be_read(
        self, tmp_path, config_home, capsys
    ):
        """The refusal has to cover ``--lang``, not just the root.

        ``_plan_activation`` guards the root write, but ``--lang`` reaches the
        same file by a sibling path: ``write_lang`` rebuilds it from
        ``_read_config()``, which is ``{}`` for a damaged file. So
        ``setup --target X --lang ko`` printed "could not be read — leaving it
        untouched" and then replaced the whole file with ``{"lang": "ko"}``,
        destroying the root it had just declined to touch. The output lying
        about it is what makes this worse than the loss it reintroduced.
        """
        path = config_file(config_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"root": "/real/kb", ', encoding="utf-8")
        before = path.read_bytes()

        rc = cli.main(["setup", "--target", str(tmp_path / "scratch"), "--lang", "ko"])
        captured = capsys.readouterr()
        out = captured.out + captured.err

        assert rc != 0, f"a declined --lang still exited 0: {out}"

        assert path.read_bytes() == before, f"--lang overwrote an unreadable config: {out}"
        assert "narration language NOT set" in out, f"dropped the language silently: {out}"

    def test_lang_still_applies_once_activate_has_replaced_the_damaged_config(
        self, tmp_path, config_home, capsys
    ):
        """The skip is conditional on the file *now*, not on how the run started.

        ``--activate`` replaces an unreadable config with a valid one earlier in
        the same run, so by the time ``--lang`` is applied there is nothing left
        to protect and refusing would drop a language for no reason.
        """
        path = config_file(config_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"root": "/real/kb", ', encoding="utf-8")
        scratch = tmp_path / "scratch"

        assert cli.main(["setup", "--target", str(scratch), "--activate", "--lang", "ko"]) == 0
        capsys.readouterr()

        assert json.loads(path.read_text(encoding="utf-8")) == {
            "root": resolved(scratch),
            "lang": "ko",
        }

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
