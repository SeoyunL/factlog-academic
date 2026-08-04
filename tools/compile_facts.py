#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compatibility wrapper for direct ``tools/`` execution.

Usage:
    python3 compile_facts.py [--wiki <kb>]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


# Resolve the KB root and export it before importing factlog.common (via
# factlog.compile_facts), which binds its module-level paths from FACTLOG_ROOT at
# import time. Without this the active-KB config (`factlog use`) was skipped and
# resolution fell straight through to cwd (#330).
import factlog_config  # noqa: E402

os.environ["FACTLOG_ROOT"] = factlog_config.resolve_root_from_argv("--wiki")

from factlog.compile_facts import main  # noqa: E402
from factlog.common import run_cli  # noqa: E402


if __name__ == "__main__":
    # main() takes no argv; parse here only so `--wiki` is a documented option
    # with --help, and a mistyped flag is rejected instead of silently ignored.
    parser = argparse.ArgumentParser(description="Compile confirmed facts into facts/accepted.dl.")
    parser.add_argument("--wiki", default=os.environ["FACTLOG_ROOT"], help="KB root")
    parser.parse_args()
    raise SystemExit(run_cli(main))
