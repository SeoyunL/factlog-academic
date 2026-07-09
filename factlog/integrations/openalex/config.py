#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""OpenAlex integration connection/import settings.

Mirrors :mod:`factlog.integrations.zotero.config`: a small TOML reader with the
precedence

    explicit path arg
        >  <kb>/policy/openalex-config.toml
        >  ${XDG_CONFIG_HOME:-~/.config}/factlog/openalex.toml
        >  built-in defaults

There is no secrets boundary here because OpenAlex has no credentials: the API
is unauthenticated and ``email`` is an identification courtesy that travels in
the query string of every request. It is therefore safe to read from a
KB-scoped policy file, unlike Zotero's ``web_api_key``.

``tomllib`` is stdlib on 3.11+, so this module has no third-party imports.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# OpenAlex rejects per_page > 200 with HTTP 400 (verified against the live API).
API_MAX_PER_PAGE = 200
DEFAULT_LIMIT = 25


class OpenAlexConfigError(Exception):
    """An OpenAlex settings file was named but could not be read/parsed/validated."""


@dataclass(frozen=True)
class OpenAlexConfig:
    """Resolved OpenAlex client + import settings.

    ``email`` is optional and unauthenticated; OpenAlex asks for it so it can
    contact heavy users. ``skip_duplicates`` is what makes re-import idempotent
    (P3), matching the Zotero importer's contract.
    """

    email: str = ""
    default_limit: int = DEFAULT_LIMIT
    max_limit: int = API_MAX_PER_PAGE
    default_target: str = ""
    skip_duplicates: bool = True
    include_abstract: bool = True


def xdg_config_path() -> Path:
    """The user-level OpenAlex settings path, next to factlog's own config."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "factlog" / "openalex.toml"


def default_config_paths(kb_root: Path | str | None = None) -> list[Path]:
    """Auto-discovery search order: KB-scoped policy file, then user-level file."""
    paths: list[Path] = []
    if kb_root is not None:
        paths.append(Path(kb_root) / "policy" / "openalex-config.toml")
    paths.append(xdg_config_path())
    return paths


def _as_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _as_bool(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_limit(value: object, default: int) -> int:
    # bool is an int subclass; a stray `true` must not read as limit 1. Values
    # outside the API's 1..200 window would only fail later as an HTTP 400, so
    # clamp back to the default here where the cause is still visible.
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= API_MAX_PER_PAGE:
        return value
    return default


def from_mapping(data: dict) -> OpenAlexConfig:
    """Build an :class:`OpenAlexConfig` from a parsed TOML mapping.

    Wrong-typed value fields fall back to defaults (graceful). A non-string
    ``client.email`` fails loud: it is echoed into every request URL, so a typo
    there should not be silently dropped into anonymous requests.
    """
    client = data.get("client", {})
    imp = data.get("import", {})
    if not isinstance(client, dict):
        client = {}
    if not isinstance(imp, dict):
        imp = {}

    raw_email = client.get("email", "")
    if not isinstance(raw_email, str):
        raise OpenAlexConfigError(f"client email must be a string, got {type(raw_email).__name__}")

    max_limit = _as_limit(imp.get("max_limit"), API_MAX_PER_PAGE)
    default_limit = _as_limit(imp.get("default_limit"), DEFAULT_LIMIT)
    # A default above the ceiling would make every un-flagged search fail at the
    # API; prefer the operator's ceiling over their default.
    if default_limit > max_limit:
        default_limit = max_limit

    return OpenAlexConfig(
        email=raw_email.strip(),
        default_limit=default_limit,
        max_limit=max_limit,
        default_target=_as_str(imp.get("default_target")),
        skip_duplicates=_as_bool(imp.get("skip_duplicates"), True),
        include_abstract=_as_bool(imp.get("include_abstract"), True),
    )


def _load_file(path: Path) -> OpenAlexConfig:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise OpenAlexConfigError(f"invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise OpenAlexConfigError(f"cannot read {path}: {exc}") from exc
    return from_mapping(data)


def load_config(
    path: Path | str | None = None,
    kb_root: Path | str | None = None,
) -> OpenAlexConfig:
    """Resolve OpenAlex settings following the module precedence.

    ``path`` names an explicit settings file (a missing one is an error, since
    the caller pointed at it). With no ``path``, auto-discovery walks
    :func:`default_config_paths`; if none exist, built-in defaults are returned.
    """
    if path is not None:
        explicit = Path(path).expanduser()
        if not explicit.is_file():
            raise OpenAlexConfigError(f"openalex config not found: {explicit}")
        return _load_file(explicit)

    for candidate in default_config_paths(kb_root):
        if candidate.is_file():
            return _load_file(candidate)
    return OpenAlexConfig()
