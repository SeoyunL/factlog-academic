#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Zotero integration connection/import settings.

Phase 1 imports Zotero bibliographic metadata into a factlog KB. This module
loads the small TOML settings file that records how to reach Zotero (the Local
API by default) plus a few import preferences. Settings resolve with the
precedence:

    explicit path arg
        >  <kb>/policy/zotero-config.toml
        >  ${XDG_CONFIG_HOME:-~/.config}/factlog/zotero.toml
        >  built-in defaults

Aligned with :mod:`factlog.config`, the user-level file lives under the XDG
config dir (``factlog/zotero.toml``), not ``~/.factlog``. Missing files during
auto-discovery fall back to defaults; a *malformed* file (or an explicit path
that does not exist) raises :class:`ZoteroConfigError` so the operator sees the
problem instead of silently getting defaults.

Secrets boundary: web credentials (``web_user_id``/``web_api_key``) are read
only from the user-level XDG file or an explicitly passed path — never from a
KB-scoped ``policy/zotero-config.toml``. A KB is often its own version-controlled
repo (sources reproducibility), so honoring secrets there would invite an API
key into a committed KB. Secrets in a KB policy file are ignored.

Path expansion (``~`` in ``default_target``) and web-credential completeness are
*not* validated here; they are the caller's job at import-run time, so this
loader stays a pure settings reader with no filesystem/network side effects
beyond reading the one TOML file.

``tomllib`` is stdlib on 3.11+, so this module has no third-party imports;
``pyzotero`` is only needed when an import actually runs.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LOCAL_PORT = 23119
VALID_MODES = ("local", "web")


class ZoteroConfigError(Exception):
    """A Zotero settings file was named but could not be read/parsed/validated."""


@dataclass(frozen=True)
class ZoteroConfig:
    """Resolved Zotero connection + import settings.

    ``mode`` is the connection transport (``local`` Local API — the phase-1
    default — or ``web``). ``skip_duplicates`` is what makes re-import
    idempotent (P3): an item whose target source file already exists is skipped
    rather than rewritten, so the same collection imports to the same result.
    """

    mode: str = "local"
    local_port: int = DEFAULT_LOCAL_PORT
    web_user_id: str = ""
    web_api_key: str = ""
    default_target: str = ""
    skip_duplicates: bool = True
    include_abstract: bool = True


def xdg_config_path() -> Path:
    """The user-level Zotero settings path, next to factlog's own config."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "factlog" / "zotero.toml"


def default_config_paths(kb_root: Path | str | None = None) -> list[Path]:
    """Auto-discovery search order: KB-scoped policy file, then user-level file.

    The KB-scoped ``policy/zotero-config.toml`` wins so a per-KB setting can
    override the user default.
    """
    paths: list[Path] = []
    if kb_root is not None:
        paths.append(Path(kb_root) / "policy" / "zotero-config.toml")
    paths.append(xdg_config_path())
    return paths


def _as_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _as_bool(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_port(value: object, default: int) -> int:
    # bool is an int subclass; a stray `true` must not read as port 1. A port
    # outside the TCP range would only fail later at connect time, so clamp it
    # back to the default here where the cause is still visible.
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535:
        return value
    return default


def from_mapping(data: dict, *, allow_secrets: bool = True) -> ZoteroConfig:
    """Build a :class:`ZoteroConfig` from a parsed TOML mapping.

    Wrong-typed value fields fall back to defaults (graceful), but structural
    mistakes fail loud: an unsupported ``connection.mode``, or a ``mode`` that is
    not even a string, raises :class:`ZoteroConfigError` — a typo there would
    silently pick a transport the user did not intend.

    ``allow_secrets=False`` drops ``web_user_id``/``web_api_key`` regardless of
    the file's contents; callers pass it when the source is a KB-scoped policy
    file (see module docstring's secrets boundary).
    """
    conn = data.get("connection", {})
    imp = data.get("import", {})
    if not isinstance(conn, dict):
        conn = {}
    if not isinstance(imp, dict):
        imp = {}

    raw_mode = conn.get("mode", "local")
    if not isinstance(raw_mode, str):
        raise ZoteroConfigError(
            f"connection mode must be a string, got {type(raw_mode).__name__}"
        )
    mode = raw_mode.strip().lower() or "local"
    if mode not in VALID_MODES:
        raise ZoteroConfigError(
            f"unsupported connection mode {mode!r}; expected one of {', '.join(VALID_MODES)}"
        )

    return ZoteroConfig(
        mode=mode,
        local_port=_as_port(conn.get("local_port"), DEFAULT_LOCAL_PORT),
        web_user_id=_as_str(conn.get("web_user_id")) if allow_secrets else "",
        web_api_key=_as_str(conn.get("web_api_key")) if allow_secrets else "",
        default_target=_as_str(imp.get("default_target")),
        skip_duplicates=_as_bool(imp.get("skip_duplicates"), True),
        include_abstract=_as_bool(imp.get("include_abstract"), True),
    )


def _load_file(path: Path, *, allow_secrets: bool = True) -> ZoteroConfig:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ZoteroConfigError(f"invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ZoteroConfigError(f"cannot read {path}: {exc}") from exc
    return from_mapping(data, allow_secrets=allow_secrets)


def load_config(
    path: Path | str | None = None,
    kb_root: Path | str | None = None,
) -> ZoteroConfig:
    """Resolve Zotero settings following the module precedence.

    ``path`` names an explicit settings file (a missing one is an error, since
    the caller pointed at it). With no ``path``, auto-discovery walks
    :func:`default_config_paths`; if none exist, built-in defaults are returned.
    """
    if path is not None:
        explicit = Path(path).expanduser()
        if not explicit.is_file():
            raise ZoteroConfigError(f"zotero config not found: {explicit}")
        return _load_file(explicit)

    kb_policy = Path(kb_root) / "policy" / "zotero-config.toml" if kb_root is not None else None
    for candidate in default_config_paths(kb_root):
        if candidate.is_file():
            # Secrets are honored only from the user-level file, never from a
            # KB-scoped policy file (see module docstring's secrets boundary).
            return _load_file(candidate, allow_secrets=candidate != kb_policy)
    return ZoteroConfig()
