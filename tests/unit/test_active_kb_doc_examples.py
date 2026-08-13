# SPDX-License-Identifier: Apache-2.0
"""The active-KB reference pages must quote output the CLI can actually produce.

``docs/reference/active-kb.md`` and its English twin show ``text`` blocks of
``factlog init`` output, and both a user and an LLM read a reference page as a
contract. Three of those blocks in each page quoted wording the code had already
moved past — "active-KB config unchanged: … was created but is NOT recorded
there" when the tool says "active-KB root unchanged: … is not recorded in the
config", "leaving it untouched" for "could not be read — leaving its bytes
untouched", "(from the active KB config)" for "(from the active-KB config)". The
last of those had been corrected in the code by an earlier review and the new
page still carried the old spelling, which is the shape of the problem: the doc
was hand-written beside the code instead of taken from it.

So these run the command and compare. The only edit applied to the captured
output is path substitution — the pages use ``/Users/me/wiki``, ``/tmp/scratch``
and ``/Users/me/notes.md`` as illustrations, and a test cannot create those.
Every other byte, including the em dashes and the double space before ``(or
re-run with --activate)``, has to match what the page prints.

The blocks are excerpts: ``init`` also lists every scaffolded directory, and the
pages leave that out. Each scenario therefore names the prefixes it quotes, and
the assertion is that those lines, in that order, appear verbatim in both pages.

Two blocks are ``setup``'s, not ``init``'s, and they are driven through
``cli.main`` in-process with the doctor/pip stages stubbed — the same shape
``tests/unit/test_init_activation.py`` uses and for the same reason: a
subprocess ``setup`` would try to pip-install pyrewire when the engine is
absent, and its doctor output is machine- and locale-dependent noise.

The stub is not consequence-free for one of them, so state what it does: the
``--lang`` closing line is printed only under ``if final_ok and lang_deferred``,
so forcing the doctor to pass is what puts that branch in reach at all. With a
failing final doctor the run still exits 1, but that line is replaced by the
environment-problem one and the refusal survives only as the summary's
``narration language NOT set`` note — which is why the page states the premise.
What the stub does *not* do is compose the sentence or choose the exit code;
both come from unstubbed code, and those are what the page quotes. The
file-target block is a ``FactlogError`` raised before either stubbed stage is
reached.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT))
from factlog import cli  # noqa: E402

PAGES = (
    REPO_ROOT / "docs" / "reference" / "active-kb.md",
    REPO_ROOT / "docs" / "reference" / "active-kb.en.md",
)

# The illustrative paths the pages are written around.
DOC_WIKI = "/Users/me/wiki"
DOC_SCRATCH = "/tmp/scratch"
DOC_CONFIG = "/Users/me/.config/factlog/config.json"
DOC_FILE = "/Users/me/notes.md"


@pytest.fixture()
def sandbox(tmp_path):
    """An isolated config home plus the paths the pages illustrate.

    ``file`` is created for every scenario, not just the two that name it: it
    costs one write, and a substitution entry that matches nothing is a no-op
    for the blocks that do not mention it.
    """
    cfg_home = tmp_path / "cfg"
    (cfg_home / "factlog").mkdir(parents=True)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    notes = tmp_path / "notes.md"
    notes.write_text("a plain file, where a KB is about to be asked for\n", encoding="utf-8")
    return {
        "cfg_home": cfg_home,
        "config": cfg_home / "factlog" / "config.json",
        "wiki": wiki,
        "scratch": tmp_path / "scratch",
        "file": notes,
    }


def run_init(sandbox, *args, cwd: Path | None = None, env_root: str | None = None) -> list[str]:
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(sandbox["cfg_home"])
    env["PYTHONPATH"] = str(REPO_ROOT)
    # Dropped by default, and this pop is the actual defence rather than a
    # belt-and-braces one: the repo-root conftest *defers* to an inherited
    # FACTLOG_ROOT ("if not os.environ.get(...)") instead of overriding it, so
    # without this an inherited value would decide the target these captures are
    # choosing on purpose. *env_root* puts it back for the one block that is
    # about the environment.
    env.pop("FACTLOG_ROOT", None)
    if env_root is not None:
        env["FACTLOG_ROOT"] = env_root
    proc = subprocess.run(
        [sys.executable, "-m", "factlog", "init", *args],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return (proc.stdout + proc.stderr).splitlines()


def run_setup(sandbox, monkeypatch, capsys, *args) -> tuple[int, list[str]]:
    """Drive ``factlog setup`` in-process; only doctor/pip are stubbed.

    See the module docstring for what that stubbing does and does not decide.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(sandbox["cfg_home"]))
    monkeypatch.delenv("FACTLOG_ROOT", raising=False)
    monkeypatch.setattr(cli, "_pyrewire_ok", lambda: True)
    monkeypatch.setattr(cli, "_run_doctor_checks", lambda *a, **k: True)

    rc = cli.main(["setup", *args])

    captured = capsys.readouterr()
    return rc, (captured.out + captured.err).splitlines()


def as_documented(sandbox, lines: list[str], *prefixes: str) -> list[str]:
    """Keep the quoted lines, in order, with the sandbox paths swapped for the
    page's illustrative ones."""
    kept = [line for line in lines if line.startswith(prefixes)]
    swapped = []
    for line in kept:
        for real, shown in (
            (str(sandbox["file"].resolve()), DOC_FILE),
            (str(sandbox["scratch"].resolve()), DOC_SCRATCH),
            (str(sandbox["wiki"].resolve()), DOC_WIKI),
            (str(sandbox["config"]), DOC_CONFIG),
        ):
            line = line.replace(real, shown)
        swapped.append(line)
    return swapped


def assert_pages_quote(block: list[str]) -> None:
    assert block, "captured nothing — the prefixes no longer match any output line"
    text = "\n".join(block)
    for page in PAGES:
        assert text in page.read_text(encoding="utf-8"), (
            f"{page.relative_to(REPO_ROOT)} does not quote what the command prints:\n{text}"
        )


def test_another_kb_is_recorded(sandbox):
    """The page's headline example: a scratch KB created beside a recorded one."""
    sandbox["config"].write_text(
        json.dumps({"root": str(sandbox["wiki"].resolve())}, indent=2) + "\n", encoding="utf-8"
    )

    lines = run_init(sandbox, "--target", str(sandbox["scratch"]))

    assert_pages_quote(
        as_documented(
            sandbox,
            lines,
            "factlog init: created",
            "factlog init: active-KB root unchanged",
            "  to record it in the config:",
        )
    )


def test_a_damaged_config_is_left_alone(sandbox):
    """The "A damaged config file" section quotes the refusal verbatim."""
    sandbox["config"].write_text("{not json", encoding="utf-8")

    lines = run_init(sandbox, "--target", str(sandbox["scratch"]))

    assert_pages_quote(
        as_documented(
            sandbox,
            lines,
            "factlog init: active-KB config at",
            "  repair that file,",
        )
    )


def test_a_broken_symlink_config_is_described_as_a_link(sandbox):
    """The damaged-config section's second class: a link, not unreadable bytes.

    Every word differs from the block above it — "is a symlink whose target is
    not reachable right now" for "could not be read", "leaving the link in
    place" for "leaving its bytes untouched", "mount it or re-point the link"
    for "repair that file". Routing the new class through the old block's prose
    is exactly what an unpinned page drifts back into.
    """
    sandbox["config"].symlink_to(sandbox["cfg_home"] / "not-mounted" / "config.json")

    lines = run_init(sandbox, "--target", str(sandbox["scratch"]))

    assert_pages_quote(
        as_documented(
            sandbox,
            lines,
            "factlog init: active-KB config at",
            "  mount it or re-point the link,",
        )
    )


def test_setup_says_why_it_withheld_the_language(sandbox, monkeypatch, capsys):
    """``setup``'s rc-1 closing line, the other half of the same refusal.

    Driven in-process for the reason in the module docstring. The exit code is
    asserted alongside the wording because the page states it: a ``--lang`` that
    was declined must not report success, and this sentence is where the run
    says why.

    The page's paragraph also claims the refusal covers the ``--lang`` and not
    only the activation, so the summary note that carries that half is asserted
    too — an earlier draft said "only the activation is refused" while the block
    right underneath it read ``--lang was not applied``.
    """
    sandbox["config"].symlink_to(sandbox["cfg_home"] / "not-mounted" / "config.json")

    rc, lines = run_setup(
        sandbox, monkeypatch, capsys, "--target", str(sandbox["scratch"]), "--lang", "ko"
    )

    assert rc == 1, f"a declined --lang exited {rc}, which the page says is 1"
    assert any("narration language NOT set" in line for line in lines), (
        "the page says the --lang is refused too; nothing in the run says so:\n"
        + "\n".join(lines)
    )
    assert_pages_quote(as_documented(sandbox, lines, "factlog setup: the KB at"))


def test_setup_refuses_a_file_target_under_its_own_name(sandbox, monkeypatch, capsys):
    """The file-target check belongs to the shared target-picking step.

    ``_resolve_kb_target`` is called by ``cmd_init`` and ``cmd_setup`` alike, so
    the page writes this guard as ``init``/``setup`` — its convention for shared
    behaviour. Only the command name differs in the sentence, which is exactly
    the kind of claim that is cheap to assert and easy to let rot: prose saying
    "``setup`` says the same" stays on the page unchanged if someone gives
    ``setup`` its own wording.
    """
    rc, lines = run_setup(sandbox, monkeypatch, capsys, "--target", str(sandbox["file"]))

    assert rc == 1, f"a file target exited {rc}, which the page says is 1"
    assert_pages_quote(
        as_documented(sandbox, lines, "factlog setup: refusing to scaffold a KB at")
    )


def test_a_named_file_target_is_refused_in_a_sentence(sandbox):
    """A regular file where a KB was asked for, named with the flag.

    ``_init_kb`` mkdirs ``<target>/sources``, so this used to be a bare
    ``NotADirectoryError`` traceback. The page quotes the sentence that replaced
    it, and the advice half is what distinguishes this from the implicit block
    below.
    """
    lines = run_init(sandbox, "--target", str(sandbox["file"]))

    assert_pages_quote(
        as_documented(sandbox, lines, "factlog init: refusing to scaffold a KB at")
    )


def test_an_implicit_file_target_names_the_rank_it_came_from(sandbox):
    """The same refusal, for a target the user never typed.

    This is the worse crash of the two: the traceback fired *before* the "no
    --target given" line, so nothing said whether ``$FACTLOG_ROOT`` or the
    config had chosen the path. The config is used here because it is the rank
    the page's block names.
    """
    sandbox["config"].write_text(
        json.dumps({"root": str(sandbox["file"])}, indent=2) + "\n", encoding="utf-8"
    )

    lines = run_init(sandbox)

    assert_pages_quote(
        as_documented(sandbox, lines, "factlog init: refusing to scaffold a KB at")
    )


def test_a_flagless_command_would_go_elsewhere(sandbox):
    """The "would not reach" block — the one block no test pinned, and so the one
    that drifted: the page wrapped a single line of output across two, with a
    continuation indent the command never prints.

    ``$FACTLOG_ROOT`` is set to the *same* KB the config records, deliberately.
    Nothing is being overridden, so ``_env_override_note`` stays silent and the
    capture is this block alone — which is how the page frames it.
    """
    sandbox["config"].write_text(
        json.dumps({"root": str(sandbox["wiki"].resolve())}, indent=2) + "\n", encoding="utf-8"
    )

    lines = run_init(
        sandbox,
        "--target",
        str(sandbox["scratch"]),
        env_root=str(sandbox["wiki"].resolve()),
    )

    assert_pages_quote(
        as_documented(sandbox, lines, "  a flagless command would target")
    )


def test_an_implicit_target_names_its_source(sandbox, tmp_path):
    """The line the earlier review already corrected in the code once."""
    sandbox["config"].write_text(
        json.dumps({"root": str(sandbox["wiki"].resolve())}, indent=2) + "\n", encoding="utf-8"
    )
    elsewhere = Path(tempfile.mkdtemp(dir=tmp_path))

    lines = run_init(sandbox, cwd=elsewhere)

    assert_pages_quote(as_documented(sandbox, lines, "factlog init: no --target given;"))
