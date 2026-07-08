#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Read-only Zotero client for phase-1 import (Local API, personal library).

Wraps ``pyzotero`` to fetch bibliographic items by collection, tag, or item key.
Only GET requests are issued — Zotero originals are never modified (P4).

Two phase-1 boundaries are enforced here:

* **Bibliographic items only.** A Zotero collection also contains child
  attachments and notes; those are not standalone sources, so they are filtered
  out (:data:`_NON_BIBLIOGRAPHIC`). Collections are read top-level.
* **Local API only.** Web/group-library support is a non-goal for phase 1, so a
  ``web`` config mode is rejected with a clear message rather than half-working.

``pyzotero`` is imported lazily inside :meth:`_connect`, so importing this module
(and ``import factlog``) stays light for users without the extra. Tests inject a
fake ``backend`` to stay deterministic and pyzotero-free.
"""
from __future__ import annotations

from factlog.integrations.zotero.config import DEFAULT_LOCAL_PORT, ZoteroConfig

# Item types that are not standalone bibliographic sources in phase 1.
_NON_BIBLIOGRAPHIC = frozenset({"attachment", "note", "annotation"})

# Max itemKeys per Zotero API request; larger id lists are fetched in batches.
_ID_BATCH = 50


class ZoteroError(Exception):
    """A Zotero request could not be satisfied (bad collection, web mode, ...)."""


class ZoteroConnectionError(ZoteroError):
    """The Zotero Local API could not be reached (app not running / API off)."""


def _data(item: dict) -> dict:
    data = item.get("data") if isinstance(item, dict) else None
    return data if isinstance(data, dict) else {}


class ZoteroClient:
    """Fetch bibliographic items from a personal Zotero library over the Local API."""

    def __init__(self, config: ZoteroConfig | None = None, backend: object | None = None):
        self._config = config or ZoteroConfig()
        self._backend = backend

    # -- connection --------------------------------------------------------
    @property
    def backend(self):
        if self._backend is None:
            self._backend = self._connect()
        return self._backend

    def _connect(self):
        # Pure config validation first, so it works (and is tested) without the
        # pyzotero extra installed — a mis-set mode/port reports the real cause
        # instead of a misleading "install pyzotero" message.
        if self._config.mode != "local":
            raise ZoteroError(
                f"Zotero '{self._config.mode}' mode is not supported in phase 1; "
                "use the Local API (mode = 'local')."
            )
        # pyzotero's Local API endpoint is fixed at localhost:23119; a non-default
        # local_port cannot be forwarded, so surface that rather than silently
        # ignoring the setting.
        if self._config.local_port != DEFAULT_LOCAL_PORT:
            raise ZoteroError(
                f"Zotero Local API uses port {DEFAULT_LOCAL_PORT}; "
                f"local_port={self._config.local_port} is not supported."
            )
        try:
            from pyzotero import zotero
        except ImportError as exc:  # pragma: no cover - environment without the extra
            raise ZoteroError(
                "pyzotero is required for zotero-import: pip install 'factlog[zotero]'"
            ) from exc
        return zotero.Zotero("0", "user", local=True)

    def _fetch(self, thunk):
        """Run a backend call, mapping failures to the client's error contract.

        A request the server *rejected* (an HTTP 4xx/5xx from pyzotero) is a
        :class:`ZoteroError`; a request that could not *reach* the server (socket
        connection/timeout) is a :class:`ZoteroConnectionError`. These are
        distinguished by class-name (works across pyzotero/requests lazy imports)
        so a live-but-erroring server is not mislabelled "not running". An
        unrecognised exception propagates unchanged rather than being swallowed.
        """
        try:
            return thunk()
        except ZoteroError:
            raise
        except Exception as exc:
            mapped = self._classify(exc)
            if mapped is None:
                raise
            raise mapped from exc

    @staticmethod
    def _classify(exc: Exception) -> ZoteroError | None:
        names = {cls.__name__ for cls in type(exc).__mro__}
        # Server reachable but returned an error status, or pyzotero rejected the
        # request (bad key, unauthorised, rate limit, ...).
        if "HTTPError" in names or "PyZoteroError" in names or names & {
            "ResourceNotFound",
            "UserNotAuthorised",
            "UnsupportedParams",
            "TooManyRetries",
            "HTTPError",
        }:
            return ZoteroError(f"Zotero Local API request failed: {exc}")
        # Could not reach the server: dedicated connection/timeout types, or a
        # bare socket-level OSError.
        if names & {"ConnectionError", "ConnectTimeout", "Timeout", "ReadTimeout", "SSLError"} or (
            isinstance(exc, OSError)
        ):
            return ZoteroConnectionError(
                f"cannot reach the Zotero Local API on localhost:{DEFAULT_LOCAL_PORT} "
                f"({type(exc).__name__}) — is Zotero running with 'Allow other "
                "applications on this computer to communicate with Zotero' enabled? "
                "(Settings > Advanced)"
            )
        return None

    def _all(self, first_page):
        """Follow pagination so large collections import completely."""
        return self.backend.everything(first_page)

    def _bibliographic(self, items: list) -> list[dict]:
        return [it for it in items if _data(it).get("itemType") not in _NON_BIBLIOGRAPHIC]

    # -- queries -----------------------------------------------------------
    def list_collections(self) -> list[dict]:
        return self._fetch(lambda: self._all(self.backend.collections()))

    def _collection_key(self, name: str) -> str:
        if not isinstance(name, str) or not name.strip():
            raise ZoteroError("collection name must be a non-empty string.")
        collections = self.list_collections()
        exact = [c for c in collections if _data(c).get("name") == name]
        if len(exact) == 1:
            return exact[0]["key"]
        if len(exact) > 1:
            raise ZoteroError(f"collection name {name!r} is ambiguous ({len(exact)} matches).")
        insensitive = [c for c in collections if _data(c).get("name", "").lower() == name.lower()]
        if len(insensitive) == 1:
            return insensitive[0]["key"]
        if len(insensitive) > 1:
            raise ZoteroError(
                f"collection name {name!r} is ambiguous by case ({len(insensitive)} matches)."
            )
        available = ", ".join(sorted(_data(c).get("name", "") for c in collections)) or "(none)"
        raise ZoteroError(f"collection {name!r} not found. Available collections: {available}")

    def get_items_by_collection(self, name: str) -> list[dict]:
        key = self._collection_key(name)
        return self._fetch(
            lambda: self._bibliographic(self._all(self.backend.collection_items_top(key)))
        )

    def get_items_by_tag(self, tag: str) -> list[dict]:
        return self._fetch(lambda: self._bibliographic(self._all(self.backend.items(tag=tag))))

    def get_items_by_ids(self, ids: list[str]) -> list[dict]:
        keys = [i.strip() for i in ids if i and i.strip()]
        if not keys:
            return []
        # The Zotero API caps the itemKey list per request, so fetch in batches
        # and concatenate; each batch's connection/HTTP errors are mapped by _fetch.
        out: list[dict] = []
        for start in range(0, len(keys), _ID_BATCH):
            batch = keys[start : start + _ID_BATCH]
            out.extend(
                self._fetch(
                    lambda b=batch: self._bibliographic(self._all(self.backend.items(itemKey=",".join(b))))
                )
            )
        return out
