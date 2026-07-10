#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""PubMed (NCBI E-utilities) integration client settings.

Phase 4 imports biomedical metadata from NCBI E-utilities into a factlog KB.
This module loads the small TOML settings file that records how factlog should
identify itself to E-utilities. Settings resolve with the precedence:

    explicit path arg
        >  <kb>/policy/pubmed-config.toml
        >  ${XDG_CONFIG_HOME:-~/.config}/factlog/pubmed.toml
        >  built-in defaults

with one credential exception: the ``NCBI_API_KEY`` environment variable, when
set, overrides ``api_key`` from *any* file (see "Environment override" below).

Aligned with :mod:`factlog.config` and the sibling Zotero/OpenAlex loaders, the
user-level file lives under the XDG config dir (``factlog/pubmed.toml``), not
``~/.factlog``. Missing files during auto-discovery fall back to defaults; a
*malformed* file (or an explicit path that does not exist) raises
:class:`PubMedConfigError` so the operator sees the problem instead of silently
getting defaults.

Secrets boundary (mirrors Zotero, NOT OpenAlex): the E-utilities ``api_key`` is
a credential — it raises NCBI's per-key rate limit and is tied to an NCBI
account. It is read only from the user-level XDG file, an explicitly passed
path, or the environment — **never from a KB-scoped ``policy/pubmed-config.toml``**.
A KB is often its own version-controlled repo (sources reproducibility), so
honoring an ``api_key`` there would invite a credential into a committed KB. An
``api_key`` in a KB policy file is ignored. This differs from OpenAlex, whose
``email`` is not a credential and is safe to read from a KB policy file.

``email`` is required by factlog policy: NCBI expects a contact address that is
echoed into every request, and unidentified traffic risks being throttled or
blocked. A *non-string* ``email`` therefore fails loud (a typo must not be
silently dropped into anonymous requests) rather than falling back to a default.
Like the Zotero loader, completeness (a *present, non-empty* email) is the
caller's job at import-run time; this loader stays a pure settings reader with
no filesystem/network side effects beyond reading the one TOML file and one env
var.

Environment override: ``NCBI_API_KEY`` takes precedence over any file. NCBI's
own tooling reads this variable, and a CI runner supplies the key as a secret
env var rather than writing it to disk, so honoring env over file keeps the
credential off disk and matches operator expectations. The override is applied
last, so it wins even over an explicit path's ``api_key``.

``tomllib`` is stdlib on 3.11+, so this module has no third-party imports;
``httpx`` is only needed when an import actually runs.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

DEFAULT_TOOL = "factlog"
API_KEY_ENV = "NCBI_API_KEY"


class PubMedConfigError(Exception):
    """A PubMed settings file was named but could not be read/parsed/validated."""


@dataclass(frozen=True)
class PubMedConfig:
    """Resolved PubMed E-utilities client settings.

    ``email`` is the NCBI contact address echoed into every request (required at
    request time, see module docstring). ``api_key`` is an optional but strongly
    recommended credential that raises the per-request rate limit. ``tool`` is
    the E-utilities ``tool`` parameter identifying the client; it defaults to
    ``"factlog"`` and rarely needs changing.
    """

    email: str = ""
    api_key: str = ""
    tool: str = DEFAULT_TOOL


def xdg_config_path() -> Path:
    """The user-level PubMed settings path, next to factlog's own config."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "factlog" / "pubmed.toml"


def default_config_paths(kb_root: Path | str | None = None) -> list[Path]:
    """Auto-discovery search order: KB-scoped policy file, then user-level file.

    The KB-scoped ``policy/pubmed-config.toml`` wins so a per-KB setting can
    override the user default.
    """
    paths: list[Path] = []
    if kb_root is not None:
        paths.append(Path(kb_root) / "policy" / "pubmed-config.toml")
    paths.append(xdg_config_path())
    return paths


def _as_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def from_mapping(data: dict, *, allow_secrets: bool = True) -> PubMedConfig:
    """Build a :class:`PubMedConfig` from a parsed TOML mapping.

    A non-string ``client.email`` fails loud: it is echoed into every request, so
    a typo there should not be silently dropped into anonymous requests. Other
    wrong-typed value fields fall back to defaults (graceful).

    ``allow_secrets=False`` drops ``api_key`` regardless of the file's contents;
    callers pass it when the source is a KB-scoped policy file (see module
    docstring's secrets boundary).
    """
    client = data.get("client", {})
    if not isinstance(client, dict):
        client = {}

    raw_email = client.get("email", "")
    if not isinstance(raw_email, str):
        raise PubMedConfigError(f"client email must be a string, got {type(raw_email).__name__}")

    return PubMedConfig(
        email=raw_email.strip(),
        api_key=_as_str(client.get("api_key")) if allow_secrets else "",
        tool=_as_str(client.get("tool"), DEFAULT_TOOL) or DEFAULT_TOOL,
    )


def _load_file(path: Path, *, allow_secrets: bool = True) -> PubMedConfig:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise PubMedConfigError(f"invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise PubMedConfigError(f"cannot read {path}: {exc}") from exc
    return from_mapping(data, allow_secrets=allow_secrets)


def _apply_env_override(cfg: PubMedConfig) -> PubMedConfig:
    """Override ``api_key`` from ``NCBI_API_KEY`` when it is set to a value.

    Env wins over any file (see module docstring). An empty/unset variable is a
    no-op so a stray ``NCBI_API_KEY=`` does not silently wipe a file-supplied
    key; a genuine key is always non-empty.
    """
    env_key = os.environ.get(API_KEY_ENV)
    if env_key:
        return replace(cfg, api_key=env_key)
    return cfg


def load_config(
    path: Path | str | None = None,
    kb_root: Path | str | None = None,
) -> PubMedConfig:
    """Resolve PubMed settings following the module precedence.

    ``path`` names an explicit settings file (a missing one is an error, since
    the caller pointed at it). With no ``path``, auto-discovery walks
    :func:`default_config_paths`; if none exist, built-in defaults are used.
    In all cases a set ``NCBI_API_KEY`` env var overrides the file's ``api_key``.
    """
    if path is not None:
        explicit = Path(path).expanduser()
        if not explicit.is_file():
            raise PubMedConfigError(f"pubmed config not found: {explicit}")
        return _apply_env_override(_load_file(explicit))

    kb_policy = Path(kb_root) / "policy" / "pubmed-config.toml" if kb_root is not None else None
    for candidate in default_config_paths(kb_root):
        if candidate.is_file():
            # The api_key credential is honored only from the user-level file or
            # an explicit path, never from a KB-scoped policy file (see module
            # docstring's secrets boundary).
            return _apply_env_override(_load_file(candidate, allow_secrets=candidate != kb_policy))
    return _apply_env_override(PubMedConfig())
