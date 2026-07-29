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
# The `not in sys.path` guard is load-bearing, not defensive — but WHICH test proves that
# depends on the copy, and this block is byte-identical everywhere, so the sentence has to
# say which. Measured at 3e6aafc + #621 by replacing this line with `if True:` in one
# wrapper at a time and running `~/.factlog-venv/bin/python -m pytest tests/unit -q`
# (baseline 6313 passed, 1 skipped):
#
#   common.py          2 failed, 6311 passed -> test_prefer_installed.py::
#     TestWhichTreeValidatePyImports::test_a_fronted_tree_wins_over_validate_s_own_root
#     FAILS — the tools/validate.py path, which this copy decides alone (#621)
#   factlog_config.py  2 failed, 6311 passed -> test_report_factlog_provenance.py::
#     TestTwoTreesAreDistinguishable::test_two_trees_produce_two_different_lines FAILS
#   compile_facts.py   2 failed, 6311 passed -> no behaviour test moves
#   literal_types.py   1 failed, 6312 passed -> no behaviour test moves
#
# Every other failure in those runs is test_prefer_installed.py::
# TestTheFourBootstrapsDoNotDrift, which compares this block as TEXT and therefore kills
# every single-file mutant whatever it does. "It died" is not the reading; what killed it
# is. Drop the guard from all four at once and the byte-identical check goes quiet while
# both behaviour tests above still fail (measured: 3 failed, 6310 passed, 1 skipped — the
# third is test_the_block_is_the_real_one, which pins this line as a literal).
#
# The difference is import ORDER, not redundancy. run_logic_check.py — the script that
# writes the report that test reads — imports factlog_config (its line 34) before common
# (line 80), so `factlog` is already in sys.modules by the time the later copies run and a
# late sys.path.insert cannot move it. Measured directly (re-measured at 3e6aafc) with the
# guard dropped in common.py and PYTHONPATH fronting a second tree: importing `tools/common`
# FIRST resolves factlog to this tree, importing it after factlog_config still resolves the
# fronted tree. The copy that decides for tools/validate.py is common.py's — validate is the
# only tools/ script importing no factlog_config (measured: `grep -c factlog_config` gives
# validate 0, compile_facts 4, literal_types 4, merge_candidates 3, source_coverage 2) — and
# TestWhichTreeValidatePyImports measures that path through validate's own output. Do not
# drop it, and do not "always insert at 0".
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    if os.environ.get("FACTLOG_PREFER_INSTALLED") == "1":
        import importlib.util

        if importlib.util.find_spec("factlog") is None:
            sys.path.append(str(_ROOT))
    else:
        sys.path.insert(0, str(_ROOT))
# --- end factlog bootstrap ---

from factlog import literal_types as _literal_types  # noqa: E402

_WRAPPER_METADATA = {key: globals()[key] for key in ("__file__", "__name__", "__package__", "__spec__")}
globals().update(_literal_types.__dict__)
globals().update(_WRAPPER_METADATA)
