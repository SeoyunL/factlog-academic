# SPDX-License-Identifier: Apache-2.0
"""The unit suite never writes the developer's real active-KB config (#454).

Tests that scaffold a KB shell out to ``factlog init``, which adopts its target
whenever the configured active KB is not a live directory. Inherited env sent
that write to the real ``~/.config/factlog/config.json``, pointing sync/accept
and the import commands at a pytest temp dir that is deleted moments later.

The pins below are deliberately independent of machine state. A "run the suite
and diff the real config" check passes even with the bug present whenever the
developer's KB happens to exist, because then adoption never fires. So instead
we plant the *dead* root that arms adoption inside the sandbox and pin that the
write landed there, and we pin what a subprocess actually resolves for ``~``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_RESOLVE = (
    "import json, os;"
    "from pathlib import Path;"
    "from factlog.config import config_path;"
    "print(json.dumps({'home': os.path.expanduser('~'), 'config': str(config_path())}))"
)


def _resolved_paths(**popen_kwargs) -> dict:
    """What a child process sees as ``~`` and as the active-KB config path."""
    proc = subprocess.run(
        [sys.executable, "-c", _RESOLVE],
        capture_output=True, text=True, check=True, cwd=os.getcwd(), **popen_kwargs,
    )
    return json.loads(proc.stdout)


def _assert_sandboxed(paths: dict, sandbox: Path, real_config: Path) -> None:
    home = Path(paths["home"]).resolve()
    config = Path(paths["config"]).resolve()
    assert home == sandbox.resolve(), f"child resolved ~ to {home}"
    assert config.is_relative_to(sandbox.resolve()), f"child would write {config}"
    assert config != real_config.resolve()


def test_inherited_env_keeps_a_child_out_of_the_real_home(
    isolated_user_config, real_user_config_path
):
    """The ``_seed_kb`` convention: ``subprocess.run`` with no ``env`` at all.

    This is the pattern that actually invokes ``init``, so patching os.environ
    (not the call sites) is what makes it safe.
    """
    _assert_sandboxed(_resolved_paths(), isolated_user_config, real_user_config_path)


def test_explicit_env_copy_keeps_a_child_out_of_the_real_home(
    isolated_user_config, real_user_config_path, tmp_path
):
    """The ``_compile`` convention: ``env={**os.environ, ...}``.

    A copy of ``os.environ`` inherits the patched values too, so the same single
    fixture covers both conventions.
    """
    env = {**os.environ, "FACTLOG_ROOT": str(tmp_path / "kb"), "PYTHONPATH": os.getcwd()}
    _assert_sandboxed(_resolved_paths(env=env), isolated_user_config, real_user_config_path)


def test_init_adopting_a_dead_root_writes_only_inside_the_sandbox(isolated_user_config, tmp_path):
    """Arm the adoption path, then pin where the write lands.

    A config already pointing at a dead root is the precondition for
    ``init_adopts_target`` to return True; with a live root nothing is written
    and the test would pass vacuously. Since the sandbox config is the only file
    ``init`` can rewrite, asserting it now names the new KB proves the adoption
    fired *here* rather than in the developer's home.

    ``--activate`` forces that adoption: ``tmp_path`` is a temp dir, which the
    #461 guard refuses to adopt silently even over a dead root, so the opt-in is
    what makes the write fire. This test is about *where* the write lands, not
    whether the silent path fires, so opting in is exactly right.
    """
    config = isolated_user_config / ".config" / "factlog" / "config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"root": "/nonexistent/dead-kb"}) + "\n", encoding="utf-8")
    # Guard before spawning: a broken fixture must fail the test, not the config.
    # Read with .get so a fixture that never set XDG_CONFIG_HOME fails the
    # assertion with a legible message instead of raising a bare KeyError.
    xdg = os.environ.get("XDG_CONFIG_HOME")
    assert xdg == str(config.parent.parent), (
        f"isolation fixture did not point XDG_CONFIG_HOME at the sandbox (got {xdg!r}); "
        "aborting before init could reach a real config"
    )

    kb = tmp_path / "kb"
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb), "--activate"],
        capture_output=True, check=True, cwd=os.getcwd(),
    )

    assert json.loads(config.read_text(encoding="utf-8"))["root"] == str(kb)
