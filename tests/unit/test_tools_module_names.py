# SPDX-License-Identifier: Apache-2.0
"""No module under tools/ may shadow an installed package.

conftest puts tools/ on sys.path so the harnesses can `import common` and friends
directly. That makes every tools/*.py a TOP-LEVEL module name, competing with
whatever is installed in the environment — and the loser is decided by import
order, not by us.

tools/coverage.py lost that race to coverage.py (the package pytest-cov imports
before conftest ever runs), which left `pytest --cov` unable to collect the suite
at all: `from coverage import coverage_rows` resolved to the wrong `coverage`.
CI does not pass --cov, so nothing went red; the person trying to measure this
repo's coverage just hit an ImportError they had no reason to attribute to us.

Guard the name space rather than that one collision: anything importable in a
normal test environment is a name tools/ must not take.
"""
from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools"

# Packages a contributor is likely to have alongside pytest. `coverage` is the
# one that actually bit (pytest-cov depends on it); the rest are its neighbours in
# any normal test env, and are just as unavailable as names for our own modules.
TEST_ENV_PACKAGES = frozenset(
    {"coverage", "pytest", "pluggy", "iniconfig", "packaging", "ruff", "py"}
)

RESERVED = frozenset(sys.stdlib_module_names) | TEST_ENV_PACKAGES


def test_no_tools_module_shadows_an_importable_package():
    stems = {path.stem for path in TOOLS.glob("*.py")}
    collisions = sorted(stems & RESERVED)
    assert collisions == [], (
        f"tools/{{{','.join(collisions)}}}.py shadow(s) an installed package. "
        "tools/ is on sys.path, so this name is taken from whatever is installed — "
        "rename the module (the CLI's own wording need not change)."
    )
