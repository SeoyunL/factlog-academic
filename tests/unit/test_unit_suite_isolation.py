# SPDX-License-Identifier: Apache-2.0
"""The unit suite must not touch the developer's real machine state.

``tests/unit/conftest.py`` pins ``FACTLOG_ROOT`` and ``XDG_CONFIG_HOME`` to
throwaway dirs. These assert that the pins are actually in force, because the
damage is silent: a test that shells out to ``factlog init`` writes the
active-KB config wherever ``XDG_CONFIG_HOME`` points, and if that is the real
``~/.config`` the developer's install ends up aimed at a pytest temp dir that
no longer exists. Nothing fails, and they find out the next time they run
``factlog``.

Asserting the *environment* rather than the writing test's behaviour is
deliberate: it holds no matter which test shells out, including ones added
later. ``tests/*.sh`` already isolate this per-harness (see the ``#62``
comments) — the Python layer is the one that was missing it.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def _is_under(child: str, parent: Path) -> bool:
    try:
        Path(child).resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


@pytest.mark.parametrize("var", ["FACTLOG_ROOT", "XDG_CONFIG_HOME"])
def test_env_is_pinned(var):
    assert os.environ.get(var), f"{var} must be pinned by conftest before any tool import"


def test_config_home_is_not_the_real_one():
    assert not _is_under(os.environ["XDG_CONFIG_HOME"], Path.home() / ".config"), (
        "XDG_CONFIG_HOME resolves inside the real ~/.config — a test that runs "
        "`factlog init` would overwrite the developer's active-KB config"
    )


def test_resolved_config_path_is_not_the_real_one():
    """The env var is the mechanism; this pins the thing that actually matters."""
    import factlog_config

    assert not _is_under(str(factlog_config.config_path()), Path.home() / ".config")


def test_factlog_root_is_not_a_real_kb():
    """An empty temp dir, not a directory that happens to hold someone's facts."""
    root = Path(os.environ["FACTLOG_ROOT"])
    assert not (root / "facts").exists(), f"FACTLOG_ROOT points at a populated KB: {root}"
