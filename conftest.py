# SPDX-License-Identifier: Apache-2.0
"""Machine-state isolation for every Python test in this repo.

This lives at the repo root rather than under ``tests/unit/`` so that a test
added anywhere — ``tests/integration/``, a future package-level test — is
covered the moment it is collected. pytest loads the rootdir conftest before
any test module, so the pins below are in force before the first import.

``FACTLOG_ROOT`` is bound to a throwaway temp dir *before* any tool module is
imported, so the module-level path globals in ``tools/`` never resolve to the
developer's cwd or a real knowledge base.

``XDG_CONFIG_HOME`` is pinned for a different reason, and it is NOT redundant
with ``FACTLOG_ROOT``: a test that shells out does not inherit this process's
``FACTLOG_ROOT`` unless it passes ``env=`` explicitly, and ``factlog init``
rewrites the active-KB config on every run. ``tests/unit/test_atomic_accepted_write.py``
does exactly that, so without this pin one ``pytest`` run rewrites the
developer's real ``~/.config/factlog/config.json`` to a pytest temp dir that
ceases to exist — leaving their ``factlog`` install pointed at nothing. Every
``tests/*.sh`` harness isolates it per-harness (see the ``#62`` comments); the
Python layer was the gap.

Note the asymmetry between the two: ``FACTLOG_ROOT`` defers to an inherited
value, ``XDG_CONFIG_HOME`` overwrites unconditionally. Whether a given path is
"already isolated" or is somebody's real config cannot be decided from the path
alone — pointing ``XDG_CONFIG_HOME`` at ``~/.dotfiles/config`` is the *normal*
use of the variable, not an isolation signal — and a test has no legitimate
reason to read the developer's config in the first place. Deferring to an
inherited value would also reinstate the bug on CI: GitHub-hosted runners
export ``XDG_CONFIG_HOME=/home/runner/.config`` in the runner environment (our
own workflow does not set it), i.e. the real one. Tests that need a specific
``XDG_CONFIG_HOME`` set it themselves with ``monkeypatch.setenv``.
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("FACTLOG_ROOT", tempfile.mkdtemp(prefix="factlog-tests-"))

# NOT setdefault, and no escape hatch for values that merely look isolated.
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="factlog-tests-cfg-")
