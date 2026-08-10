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
output is path substitution — the pages use ``/Users/me/wiki`` and
``/tmp/scratch`` as illustrations, and a test cannot create those. Every other
byte, including the em dashes and the double space before ``(or re-run with
--activate)``, has to match what the page prints.

The blocks are excerpts: ``init`` also lists every scaffolded directory, and the
pages leave that out. Each scenario therefore names the prefixes it quotes, and
the assertion is that those lines, in that order, appear verbatim in both pages.
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

PAGES = (
    REPO_ROOT / "docs" / "reference" / "active-kb.md",
    REPO_ROOT / "docs" / "reference" / "active-kb.en.md",
)

# The illustrative paths the pages are written around.
DOC_WIKI = "/Users/me/wiki"
DOC_SCRATCH = "/tmp/scratch"
DOC_CONFIG = "/Users/me/.config/factlog/config.json"


@pytest.fixture()
def sandbox(tmp_path):
    """An isolated config home plus the two KB paths the pages illustrate."""
    cfg_home = tmp_path / "cfg"
    (cfg_home / "factlog").mkdir(parents=True)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    return {
        "cfg_home": cfg_home,
        "config": cfg_home / "factlog" / "config.json",
        "wiki": wiki,
        "scratch": tmp_path / "scratch",
    }


def run_init(sandbox, *args, cwd: Path | None = None) -> list[str]:
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(sandbox["cfg_home"])
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.pop("FACTLOG_ROOT", None)
    proc = subprocess.run(
        [sys.executable, "-m", "factlog", "init", *args],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return (proc.stdout + proc.stderr).splitlines()


def as_documented(sandbox, lines: list[str], *prefixes: str) -> list[str]:
    """Keep the quoted lines, in order, with the sandbox paths swapped for the
    page's illustrative ones."""
    kept = [line for line in lines if line.startswith(prefixes)]
    swapped = []
    for line in kept:
        for real, shown in (
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


def test_an_implicit_target_names_its_source(sandbox, tmp_path):
    """The line the earlier review already corrected in the code once."""
    sandbox["config"].write_text(
        json.dumps({"root": str(sandbox["wiki"].resolve())}, indent=2) + "\n", encoding="utf-8"
    )
    elsewhere = Path(tempfile.mkdtemp(dir=tmp_path))

    lines = run_init(sandbox, cwd=elsewhere)

    assert_pages_quote(as_documented(sandbox, lines, "factlog init: no --target given;"))
