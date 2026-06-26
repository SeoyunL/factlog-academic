#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compatibility wrapper for direct ``tools/`` execution."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from factlog.compile_facts import main  # noqa: E402
from factlog.common import run_cli  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(run_cli(main))
