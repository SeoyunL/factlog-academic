#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compatibility wrapper for direct ``tools/`` execution."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# --- factlog bootstrap (#553): keep byte-identical across the tools/ wrappers ---
# A plugin ships its own copy of this tree, so a bundled wrapper prepending its own
# root makes the BUNDLED factlog package win over the one the contributor installed —
# the skew that got #208/#491/#527/#547 re-diagnosed as live bugs from reports no
# reader could tell apart. FACTLOG_PREFER_INSTALLED=1 appends instead of prepending:
# an installed package wins when there is one, and a bare checkout with nothing
# installed still imports this tree rather than raising ImportError, so the opt-out
# cannot break a working setup. Exactly "1" opts in; unset / 0 / "" / "true" stay on
# the default — no truthiness dialect to guess at.
# The `not in sys.path` guard is load-bearing, not defensive: tests/unit/
# test_report_factlog_provenance.py::test_two_trees_produce_two_different_lines fronts
# a second tree on PYTHONPATH that already lists _ROOT, and only this guard stops the
# insertion below from overtaking it. Do not drop it, and do not "always insert at 0".
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    if os.environ.get("FACTLOG_PREFER_INSTALLED") == "1":
        sys.path.append(str(_ROOT))
    else:
        sys.path.insert(0, str(_ROOT))
# --- end factlog bootstrap ---

from factlog.compile_facts import main  # noqa: E402
from factlog.common import run_cli  # noqa: E402
from factlog.runtime import script_tree_split  # noqa: E402


if __name__ == "__main__":
    # The split check lives HERE, not in ``factlog.compile_facts.main`` where the three
    # sibling tools put theirs, and the difference is not stylistic: inside the package
    # ``__file__`` IS the package file, so the comparison would be the package tree
    # against itself and could never fire (measured: ``script_tree_split`` returns None
    # for every input there). This wrapper is the only file on the compile path whose
    # ``__file__`` names the *script* — the bundled ``tools/compile_facts.py`` a plugin
    # actually executes. ``python -m factlog`` needs no check at all: there the script
    # and the package are the same tree by construction.
    #
    # stderr, not stdout: ``main`` prints ``factlog:`` and then
    # ``compile_facts: target KB`` as the first two stdout lines, pinned by position
    # (tests/unit/test_report_factlog_provenance.py).
    _tree_split = script_tree_split(__file__)
    if _tree_split:
        print(_tree_split, file=sys.stderr)
    raise SystemExit(run_cli(main))
