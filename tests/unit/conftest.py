# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the Python unit-test layer.

The bundled engine scripts live in ``tools/`` (not an installed package), so we
put that directory on ``sys.path`` to import ``common`` and friends directly.
We also pin ``FACTLOG_ROOT`` to a throwaway temp dir *before* ``common`` is
imported, so the module-level path globals never resolve to the developer's cwd
or a real knowledge base — the pure helpers under test never touch the filesystem.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

# Bind FACTLOG_ROOT to an isolated empty dir before any tool module is imported.
os.environ.setdefault("FACTLOG_ROOT", tempfile.mkdtemp(prefix="factlog-unit-"))

# Where the developer's own active-KB config lives, resolved at import time —
# before any test relocates $HOME. Only for tests that must assert a sandboxed
# subprocess never resolved back to it; exposed as a fixture rather than read
# across modules, so the test layer never depends on the import mode.
_REAL_CONFIG_BASE = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
    os.environ.get("HOME") or os.path.expanduser("~"), ".config"
)
_REAL_CONFIG_PATH = Path(_REAL_CONFIG_BASE) / "factlog" / "config.json"


@pytest.fixture(scope="session")
def real_user_config_path() -> Path:
    """The active-KB config path this machine would use without isolation."""
    return _REAL_CONFIG_PATH


@pytest.fixture(scope="session")
def _user_config_sandbox(tmp_path_factory) -> Path:
    """One sandbox home for the whole session (see `isolated_user_config`).

    Session-scoped because the per-test alternative created a directory pair for
    all ~4.7k tests to serve the handful that shell out, which measured as a
    ~75% wall-clock regression on the suite (system time 2.3x).
    """
    sandbox = tmp_path_factory.mktemp("home", numbered=False)
    (sandbox / ".config").mkdir(exist_ok=True)
    return sandbox


@pytest.fixture(autouse=True)
def isolated_user_config(_user_config_sandbox, monkeypatch) -> Path:
    """Point ``$HOME``/``$XDG_CONFIG_HOME`` at a sandbox home (#454).

    Tests that scaffold a KB run ``factlog init`` in a subprocess, and `init`
    adopts its target as the active KB whenever the configured one is not a live
    directory (`init_adopts_target`). Without isolation that write lands in the
    developer's real ``~/.config/factlog/config.json``, retargeting every later
    sync/accept/import at a pytest temp dir that is about to be deleted — and
    because the recorded root is then dead, the next run adopts again.

    Patching ``os.environ`` (rather than each call site) covers both subprocess
    conventions in the suite: the ones that omit ``env`` and inherit, and the
    ones that pass ``env={**os.environ, ...}``. Autouse so a new test file
    cannot forget it.

    The sandbox directory is shared, but the config *inside* it is not: teardown
    removes it so a KB adopted by one test cannot decide whether a later test's
    `init` adopts. Removing one small tree is far cheaper than minting a home per
    test. A test that needs its own ``$XDG_CONFIG_HOME`` can still monkeypatch it
    on top of this one.
    """
    monkeypatch.setenv("HOME", str(_user_config_sandbox))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(_user_config_sandbox / ".config"))
    try:
        yield _user_config_sandbox
    finally:
        shutil.rmtree(_user_config_sandbox / ".config" / "factlog", ignore_errors=True)


def vocabulary(constants: set[str]) -> "QueryVocabulary":  # noqa: F821
    """A ``QueryVocabulary`` that licenses `constants` in every query position.

    For the tests that vary something other than the position axis. The empty
    hierarchy and alias map keep them off the filesystem; a test that means to
    exercise a single position (subject vs relation object vs policy entity)
    builds the vocabulary itself with the sets it wants to tell apart.
    """
    from common import QueryVocabulary

    return QueryVocabulary(constants, constants, constants, hierarchy={}, aliases={})
