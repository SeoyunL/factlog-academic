# SPDX-License-Identifier: Apache-2.0
"""Shared import path for the Python unit-test layer.

The bundled engine scripts live in ``tools/`` (not an installed package), so we
put that directory on ``sys.path`` to import ``common`` and friends directly.

The environment pins that keep the suite off the developer's real machine state
(``FACTLOG_ROOT``, ``XDG_CONFIG_HOME``) live in the repo-root ``conftest.py``,
which pytest loads first — they have to cover Python tests outside this
directory too. ``test_unit_suite_isolation.py`` asserts they are in force.
"""
from __future__ import annotations

import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))
