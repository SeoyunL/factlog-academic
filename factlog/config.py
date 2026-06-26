#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Persistent active-KB config.

Once a KB is set up, ingest/ask/sync should target it from any working
directory without a flag. This module records the active KB root in a small
config file and resolves the effective root with the precedence:

    --wiki/--target flag  >  $FACTLOG_ROOT  >  config file  >  cwd

The config lives at ${XDG_CONFIG_HOME:-~/.config}/factlog/config.json as
{"root": "<absolute kb path>"}. No third-party imports, so both factlog/cli.py
and every tool's pre-import root resolver can share it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "factlog" / "config.json"


def read_root() -> str | None:
    """Return the configured active-KB root (absolute), or None if unset/invalid.

    Any malformed config — bad JSON, non-object, missing/empty/non-string root —
    returns None so resolution falls back to cwd rather than crashing.
    """
    path = config_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    root = data.get("root") if isinstance(data, dict) else None
    if not isinstance(root, str) or not root:
        return None
    return str(Path(root).expanduser().resolve())


def write_root(path: str | os.PathLike) -> Path:
    """Record *path* as the active KB. Returns the config file path written."""
    cfg = config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(Path(path).expanduser().resolve())
    cfg.write_text(json.dumps({"root": resolved}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cfg


def resolve_root(cli_value: str | None = None) -> tuple[str, str]:
    """Resolve the effective KB root and where it came from.

    Returns (absolute_root, source) where source is one of
    'flag' | 'env' | 'config' | 'cwd', following the documented precedence.
    """
    if cli_value:
        return str(Path(cli_value).expanduser().resolve()), "flag"
    env = os.environ.get("FACTLOG_ROOT")
    if env:
        return str(Path(env).expanduser().resolve()), "env"
    cfg = read_root()
    if cfg:
        return str(Path(cfg).resolve()), "config"
    return str(Path(".").resolve()), "cwd"


def resolve_root_from_argv(flag: str = "--wiki") -> str:
    """Resolve the KB root from *flag* in argv (peeking past unrelated args),
    falling back to $FACTLOG_ROOT, the active-KB config, then cwd.

    Every engine tool must export ``FACTLOG_ROOT`` to this value *before* importing
    common, whose module-level paths capture it at import time. This centralises
    the prepass each tool used to duplicate. *flag* is the tool's KB-root option
    ("--wiki" or "--target").
    """
    import argparse

    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument(flag, default=None)
    known, _ = pre.parse_known_args()
    dest = flag.lstrip("-").replace("-", "_")
    return resolve_root(getattr(known, dest))[0]
