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
# reader could tell apart. FACTLOG_PREFER_INSTALLED=1 opts out of that precedence.
# Exactly "1" opts in; unset / 0 / "" / "true" stay on the default — no truthiness
# dialect to guess at.
#
# The opt-out ASKS whether an installed factlog exists instead of merely appending,
# and that is the whole difference between working and not. `pip install -e .` on this
# pyproject makes setuptools emit a _TopLevelFinder: the checkout is reachable only
# through a finder appended to sys.meta_path, BEHIND the builtin PathFinder (measured —
# the shape this repo's own editable installs actually take). Appending _ROOT to
# sys.path still leaves it on sys.path, so PathFinder answers with the bundle and the
# editable finder is never consulted; the opt-out would be a silent no-op, and the split
# warning would go quiet too because both trees then agree. Leaving sys.path untouched
# when an installed factlog is findable is what lets the finder win. The append survives
# for the case it was chosen for: a bare checkout with nothing installed anywhere still
# imports this tree instead of raising ImportError, so the opt-out cannot break a
# working setup.
#
# find_spec, NOT `try: import factlog / except ImportError`. ImportError is wider than
# "there is no factlog": an installed package whose __init__ imports a missing
# dependency raises ModuleNotFoundError (an ImportError subclass), the fallback would
# swallow it, and the bundle would win with =1 set and NOTHING printed — the exact
# no-signal state this issue exists to remove, rebuilt inside its own fallback. A
# lookup also answers without executing the package, so a broken install fails later,
# loudly, on its own terms instead of being silently routed around.
#
# The `not in sys.path` guard is load-bearing, not defensive — but the named test proves
# that for exactly ONE of the four copies, and this block is byte-identical everywhere, so
# the sentence has to say which. Measured at bb3909c by replacing this line with `if True:`
# in one wrapper at a time and running `~/.factlog-venv/bin/python -m pytest tests/unit -q`:
#
#   factlog_config.py                          -> test_report_factlog_provenance.py::
#     TestTwoTreesAreDistinguishable::test_two_trees_produce_two_different_lines FAILS
#   common.py / compile_facts.py / literal_types.py
#     -> that test passes and the suite stays 6288 passed, 1 skipped apart from
#        test_prefer_installed.py::TestTheFourBootstrapsDoNotDrift, which compares this
#        block as TEXT and therefore kills every single-file mutant whatever it does
#
# The difference is import ORDER, not redundancy. run_logic_check.py — the script that
# writes the report that test reads — imports factlog_config (its line 34) before common
# (line 80), so `factlog` is already in sys.modules by the time the later copies run and a
# late sys.path.insert cannot move it. Measured directly with the guard dropped in
# common.py and PYTHONPATH fronting a second tree: importing `tools/common` FIRST resolves
# factlog to this tree, importing it after factlog_config still resolves the fronted tree.
# The copy that decides for tools/validate.py is common.py's (validate imports no
# factlog_config), and no test measures that path today. Do not drop it, and do not
# "always insert at 0".
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    if os.environ.get("FACTLOG_PREFER_INSTALLED") == "1":
        import importlib.util

        if importlib.util.find_spec("factlog") is None:
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
    #
    # Unlike its three siblings this one fires BEFORE argument parsing, because
    # ``run_cli`` swallows the parse and leaves no seam between "arguments accepted" and
    # "work begins". Two visible consequences, both deliberate: ``--help`` and a rejected
    # argument can carry this line on stderr, and on a terminal it appears above the
    # ``factlog:`` line stdout opens with. Neither breaks a contract — argparse's own
    # output is untouched and the stdout ORDER is unchanged — and deferring it would mean
    # changing ``main``'s signature to accept the script path, a wider change than #553.
    _tree_split = script_tree_split(__file__)
    if _tree_split:
        print(_tree_split, file=sys.stderr)
    raise SystemExit(run_cli(main))
