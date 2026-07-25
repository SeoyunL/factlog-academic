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

from factlog import literal_types as _literal_types  # noqa: E402

_WRAPPER_METADATA = {key: globals()[key] for key in ("__file__", "__name__", "__package__", "__spec__")}
globals().update(_literal_types.__dict__)
globals().update(_WRAPPER_METADATA)
