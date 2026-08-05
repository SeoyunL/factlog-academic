# SPDX-License-Identifier: Apache-2.0
"""Machine-state isolation for every Python test in this repo.

This lives at the repo root rather than under ``tests/unit/`` so that a test
added anywhere — ``tests/integration/``, a future package-level test — is
covered the moment it is collected. In a normal run pytest loads the rootdir
conftest before any test module, so the pins below are in force before the
first import; a run that cuts collection short of the root (``--confcutdir``)
is on its own, which is why the call sites that write also pin themselves.

``FACTLOG_ROOT`` is bound to a throwaway temp dir *before* any tool module is
imported, so the module-level path globals in ``tools/`` never resolve to the
developer's cwd or a real knowledge base.

``XDG_CONFIG_HOME`` is pinned for a different reason, and it is NOT redundant
with ``FACTLOG_ROOT``: a test that shells out does not inherit this process's
``FACTLOG_ROOT`` unless it passes ``env=`` explicitly, and ``factlog``'s
config-writing commands (``init``, ``use``, ``setup``, ``lang``) rewrite the
active-KB config on every run. So any test that shells out to one of them
without building its own ``env=`` writes to the developer's real
``~/.config/factlog/config.json``: ``init``/``use``/``setup`` repoint ``root``
at a pytest temp dir that ceases to exist, leaving their ``factlog`` install
aimed at nothing; ``lang`` edits the same file in place. Either way it happens
silently.
``tests/unit/test_atomic_accepted_write.py`` is the case that actually drilled
that hole; it now pins its own config home as well, so this file is the net
under the next one rather than under that one. Every ``tests/*.sh`` harness
isolates it per-harness (see the ``#62`` comments); the Python layer was the gap.

Note the asymmetry between the two: ``FACTLOG_ROOT`` defers to an inherited
value, ``XDG_CONFIG_HOME`` overwrites unconditionally. Whether a given path is
"already isolated" or is somebody's real config cannot be decided from the path
alone — pointing ``XDG_CONFIG_HOME`` at ``~/.dotfiles/config`` is the *normal*
use of the variable, not an isolation signal — and a test has no legitimate
reason to read the developer's config in the first place. CI shows why no
heuristic saves this: GitHub-hosted runners export
``XDG_CONFIG_HOME=/home/runner/.config`` in the runner environment (our own
workflow does not set it). The runner is disposable, so little is lost there —
the point is that a path test would have had to call the runner account's own
config dir "already isolated", and it cannot tell that path apart from a
developer's. Tests that need a specific ``XDG_CONFIG_HOME`` set it themselves
with ``monkeypatch.setenv``.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile


def _throwaway(prefix: str) -> str:
    """A temp dir this process owns and removes when it exits.

    Only directories created here are registered for removal — an inherited
    ``FACTLOG_ROOT`` belongs to whoever set it.
    """
    path = tempfile.mkdtemp(prefix=prefix)
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


# Deferring to an inherited value, but without the mkdtemp that setdefault would
# evaluate — and leave behind — even when the variable is already set.
if not os.environ.get("FACTLOG_ROOT"):
    os.environ["FACTLOG_ROOT"] = _throwaway("factlog-tests-")

# NOT setdefault, and no escape hatch for values that merely look isolated.
os.environ["XDG_CONFIG_HOME"] = _throwaway("factlog-tests-cfg-")
