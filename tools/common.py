# SPDX-License-Identifier: Apache-2.0
"""Compatibility wrapper for direct ``tools/`` imports."""

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

from factlog import common as _common  # noqa: E402

_WRAPPER_METADATA = {key: globals()[key] for key in ("__file__", "__name__", "__package__", "__spec__")}
globals().update(_common.__dict__)
globals().update(_WRAPPER_METADATA)
