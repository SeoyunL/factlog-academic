# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the Python unit-test layer.

The bundled engine scripts live in ``tools/`` (not an installed package), so we
put that directory on ``sys.path`` to import ``common`` and friends directly.
We also pin ``FACTLOG_ROOT`` to a throwaway temp dir *before* ``common`` is
imported, so the module-level path globals never resolve to the developer's cwd
or a real knowledge base — the pure helpers under test never touch the filesystem.

``XDG_CONFIG_HOME`` is pinned for the same reason, and it is NOT redundant with
``FACTLOG_ROOT``: a test that shells out does not inherit this process's
``FACTLOG_ROOT`` unless it passes ``env=`` explicitly, and ``factlog init``
writes the active-KB config on every run. ``test_atomic_accepted_write.py``
does exactly that, so without this line one ``pytest tests/unit`` run rewrites
the developer's real ``~/.config/factlog/config.json`` to a pytest temp dir
that ceases to exist — leaving their ``factlog`` install pointed at nothing.
Every ``tests/*.sh`` harness already isolates this (see the ``#62`` comments);
the Python layer was the gap.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

# Bind FACTLOG_ROOT to an isolated empty dir before any tool module is imported.
os.environ.setdefault("FACTLOG_ROOT", tempfile.mkdtemp(prefix="factlog-unit-"))
# setdefault, so a CI job or developer who already isolated it keeps their value.
os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp(prefix="factlog-unit-cfg-"))
