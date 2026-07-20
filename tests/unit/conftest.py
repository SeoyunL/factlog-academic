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
import sys
import tempfile
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

# Bind FACTLOG_ROOT to an isolated empty dir before any tool module is imported.
os.environ.setdefault("FACTLOG_ROOT", tempfile.mkdtemp(prefix="factlog-unit-"))


def vocabulary(constants: set[str]) -> "QueryVocabulary":  # noqa: F821
    """A ``QueryVocabulary`` that licenses `constants` in every query position.

    For the tests that vary something other than the position axis. The empty
    hierarchy and alias map keep them off the filesystem; a test that means to
    exercise a single position (subject vs relation object vs policy entity)
    builds the vocabulary itself with the sets it wants to tell apart.
    """
    from common import QueryVocabulary

    return QueryVocabulary(constants, constants, constants, hierarchy={}, aliases={})
