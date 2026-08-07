#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Persistent active-KB config.

Once a KB is set up, ingest/ask/sync should target it from any working
directory without a flag. This module records the active KB root in a small
config file and resolves the effective root with the precedence:

    --wiki/--target flag  >  $FACTLOG_ROOT  >  config file  >  cwd

The config lives at ${XDG_CONFIG_HOME:-~/.config}/factlog/config.json as
{"root": "<absolute kb path>", "lang": "<code>"}. The optional ``lang`` field
records the language for the assistant's human-facing narration/summaries only
(never engine reports, CLI stdout, or fact data — see SKILL.md). No third-party
imports, so both factlog/cli.py and every tool's pre-import root resolver can
share it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "factlog" / "config.json"


def _write_config(cfg: Path, data: dict) -> None:
    """Serialise *data* to *cfg* atomically (temp file + ``os.replace``).

    A direct ``write_text`` can leave a truncated config if the process is killed
    mid-write, and a corrupt config silently degrades root resolution to cwd. The
    temp+replace pattern makes the swap atomic on POSIX and Windows, keeping this
    module free of third-party imports.
    """
    cfg.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = cfg.with_name(cfg.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.replace(tmp, cfg)
    except OSError:
        # The swap can fail for reasons the caller cannot foresee — a directory
        # sitting at the config path raises IsADirectoryError here. Leaving the
        # temp file behind turned a failed write into a permanent stray
        # ``config.json.tmp`` next to the real config, and made "nothing was
        # changed" untrue for the callers that report it. Best-effort, because
        # the original error is the one worth propagating.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


MISSING = "missing"
READABLE = "readable"
UNREADABLE = "unreadable"


def config_status() -> str:
    """Classify the config file as ``MISSING``, ``READABLE`` or ``UNREADABLE``.

    ``read_root``/``read_lang`` fold every failure into None so *resolution*
    degrades to cwd instead of crashing. That is right for a reader and wrong for
    a **writer**: "I could not read it" is not "there is nothing there". The bytes
    a writer is about to replace may hold the user's KB root and narration
    language, and once replaced they are unrecoverable — a strictly worse loss
    than the one #356 is about, because a root that merely points at an unmounted
    volume at least survives as text.

    So callers that write ask this first, and only ``MISSING`` means "nothing is
    recorded yet". A file that parses but records no usable ``root`` is
    ``READABLE``: it is understood, there is no path in it to lose, and
    ``resolve_root`` already reports such a config as holding nothing.
    """
    path = config_path()
    if not path.exists():
        return MISSING
    # ``exists()``, not ``is_file()``: a *directory* at the config path is not
    # "nothing is recorded yet". Classifying it MISSING broke this function's own
    # invariant and sent callers down the write path, where every write raises
    # ``IsADirectoryError``. It is unreadable, which is exactly what it is.
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return UNREADABLE
    return READABLE if isinstance(data, dict) else UNREADABLE


def _read_config() -> dict:
    """Return the parsed config object, or {} for a missing/malformed file.

    Central safe reader so both ``read_root`` and ``read_lang`` degrade the same
    way (bad JSON / non-object / unreadable → {}) instead of crashing. Preserving
    the full dict also lets ``write_root``/``write_lang`` keep sibling fields they
    do not own.

    ``ValueError`` rather than ``json.JSONDecodeError`` so a config that is not
    valid UTF-8 degrades here too: ``read_text`` raises ``UnicodeDecodeError``
    (a ``ValueError``, not an ``OSError``), which used to escape this handler and
    crash every command that resolves a root.
    """
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def read_root() -> str | None:
    """Return the configured active-KB root (absolute), or None if unset/invalid.

    Any malformed config — bad JSON, non-object, missing/empty/non-string root —
    returns None so resolution falls back to cwd rather than crashing.
    """
    root = _read_config().get("root")
    if not isinstance(root, str) or not root:
        return None
    return str(Path(root).expanduser().resolve())


def read_lang() -> str | None:
    """Return the configured narration language code, or None if unset/invalid.

    The language is advisory metadata for the assistant's human-facing prose only;
    it never affects root resolution. A missing/empty/non-string ``lang`` (or a
    malformed config) returns None so a root-only config — and every pre-``lang``
    KB — behaves exactly as before.
    """
    lang = _read_config().get("lang")
    if not isinstance(lang, str):
        return None
    lang = lang.strip()
    return lang or None


def write_root(path: str | os.PathLike) -> Path:
    """Record *path* as the active KB, preserving any configured ``lang``.

    Returns the config file path written. Root and language are independent
    settings, so re-pointing the active KB must never drop a language the user
    already set.
    """
    cfg = config_path()
    data = _read_config()
    data["root"] = str(Path(path).expanduser().resolve())
    _write_config(cfg, data)
    return cfg


def write_lang(code: str | None) -> Path:
    """Set (or clear) the narration language, preserving the configured ``root``.

    A non-empty *code* is stored trimmed; passing None or an empty/whitespace
    string removes the field (reverting to conversation-language behaviour).
    Returns the config file path written. The ``root`` field is read back and
    re-emitted untouched so setting a language never disturbs the active KB.
    """
    cfg = config_path()
    data = _read_config()
    normalized = code.strip() if isinstance(code, str) else ""
    if normalized:
        data["lang"] = normalized
    else:
        data.pop("lang", None)
    _write_config(cfg, data)
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
