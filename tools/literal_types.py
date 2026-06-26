# SPDX-License-Identifier: Apache-2.0
"""Compatibility wrapper for direct ``tools/`` imports."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from factlog import literal_types as _literal_types  # noqa: E402

_WRAPPER_METADATA = {key: globals()[key] for key in ("__file__", "__name__", "__package__", "__spec__")}
globals().update(_literal_types.__dict__)
globals().update(_WRAPPER_METADATA)
