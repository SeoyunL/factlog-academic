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

# Attachment content type imported as a full-text source in phase 2.
_PDF_CONTENT_TYPE = "application/pdf"

# Link modes whose bytes Zotero actually stores (and the Local API can serve).
# linked_file/linked_url point outside Zotero's storage, so file() cannot return
# them — they are skipped rather than surfaced as un-downloadable attachments.
_DOWNLOADABLE_LINK_MODES = frozenset({"imported_file", "imported_url"})

# Annotation types that carry no text (nothing to import as a source in phase 3).
_NON_TEXT_ANNOTATION_TYPES = frozenset({"image", "ink"})

# How many names a "not found" message lists before summarising the rest. A real
# library accumulates hundreds of auto-imported tags, so the whole set would bury
# the error it is meant to explain.
_SUGGEST_LIMIT = 20


class ZoteroError(Exception):
    """A Zotero request could not be satisfied (bad collection, web mode, ...)."""


class ZoteroConnectionError(ZoteroError):
    """The Zotero Local API could not be reached (app not running / API off)."""


def _data(item: dict) -> dict:
    data = item.get("data") if isinstance(item, dict) else None
    return data if isinstance(data, dict) else {}


def _unreachable_msg(exc: Exception) -> str:
    return (
        f"cannot reach the Zotero Local API on localhost:{DEFAULT_LOCAL_PORT} "
        f"({type(exc).__name__}) — is Zotero running with 'Allow other "
        "applications on this computer to communicate with Zotero' enabled? "
        "(Settings > Advanced)"
    )


def _tag_name(entry) -> str:
    """The tag string of one ``tags()`` entry.

    pyzotero hands back plain strings, but the raw Local API payload is a list of
    ``{"tag": ...}`` objects — accept both so a backend that skips pyzotero's
    processing (including the test fakes) still resolves.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        name = entry.get("tag")
        if isinstance(name, str):
            return name
    return ""


def _item_key(item) -> str:
    """The item key, read the way :func:`parse_item` reads it.

    The key sits on the item wrapper *and* inside ``data`` in a Local API
    response, and ``data`` wins here for the same reason it wins in the parser:
    the two must not disagree about which key an item has, or the "did the
    request resolve?" check below would clear a key that the import then files
    under a different ``zotero_key``.
    """
    if not isinstance(item, dict):
        return ""
    key = _data(item).get("key") or item.get("key")
    return key if isinstance(key, str) else ""


def _suggest(names) -> str:
    """A deterministic, length-capped ``a, b, c`` listing for a not-found error."""
    ordered = sorted(set(names))
    if not ordered:
        return "(none)"
    if len(ordered) <= _SUGGEST_LIMIT:
        return ", ".join(ordered)
    shown = ordered[:_SUGGEST_LIMIT]
    return f"{', '.join(shown)}, ... ({len(ordered) - _SUGGEST_LIMIT} more)"


def _is_downloadable_pdf(data: dict) -> bool:
    if data.get("itemType") != "attachment":
        return False
    content_type = data.get("contentType")
    if not isinstance(content_type, str):
        return False
    if content_type.split(";")[0].strip().lower() != _PDF_CONTENT_TYPE:
        return False
    return data.get("linkMode") in _DOWNLOADABLE_LINK_MODES


class ZoteroClient:
    """Fetch bibliographic items from a personal Zotero library over the Local API."""

    def __init__(self, config: ZoteroConfig | None = None, backend: object | None = None):
        self._config = config or ZoteroConfig()
        self._backend = backend
        # All library annotations, fetched once (annotations aren't served via an
        # attachment's /children on the Local API), reused across attachments.
        self._annotations: list[dict] | None = None

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
                "pyzotero is required for zotero-import: "
                "pip install 'factlog-academic[zotero] @ git+https://github.com/SeoyunL/factlog-academic'"
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
        # Order matters: a transport failure must be tested BEFORE HTTP status,
        # because modern pyzotero uses httpx whose ConnectError/timeouts derive
        # from RequestError -> HTTPError — i.e. a connection failure also carries
        # "HTTPError" in its MRO. Classify "could not reach/complete the request"
        # first (httpx RequestError family, requests ConnectionError/Timeout,
        # builtin ConnectionError). httpx exceptions are NOT OSError subclasses,
        # so we match by name, keeping a bare-OSError socket fallback last.
        if names & {
            "RequestError",       # httpx base for every transport-level failure
            "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
            "PoolTimeout", "NetworkError", "TransportError", "ProxyError",
            "ConnectionError", "Timeout", "SSLError",  # requests + builtins
        }:
            return ZoteroConnectionError(_unreachable_msg(exc))
        # Server responded with an error status, or pyzotero rejected the request.
        if names & {
            "HTTPStatusError",    # httpx: a real 4xx/5xx response
            "HTTPError",          # requests HTTPError (status) / pyzotero
            "PyZoteroError", "ResourceNotFound", "UserNotAuthorised",
            "UnsupportedParams", "TooManyRetries",
        }:
            return ZoteroError(f"Zotero Local API request failed: {exc}")
        # Bare socket-level OSError not otherwise named.
        if isinstance(exc, OSError):
            return ZoteroConnectionError(_unreachable_msg(exc))
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
        available = _suggest(_data(c).get("name", "") for c in collections)
        raise ZoteroError(f"collection {name!r} not found. Available collections: {available}")

    def get_items_by_collection(self, name: str) -> list[dict]:
        key = self._collection_key(name)
        return self._fetch(
            lambda: self._bibliographic(self._all(self.backend.collection_items_top(key)))
        )

    def list_tags(self) -> list[str]:
        """Every tag name in the library (read-only), in Zotero's order."""
        raw = self._fetch(lambda: self._all(self.backend.tags()))
        return [name for name in (_tag_name(entry) for entry in raw) if name]

    def _resolve_tag(self, tag: str) -> str:
        """The library's own spelling of ``tag``, or a ZoteroError naming what exists.

        A tag needs no name->key lookup, so nothing forced an existence check here
        the way ``_collection_key`` forces one for collections — and an unknown tag
        used to come back as an ordinary empty result (#453). Resolution mirrors
        the collection rules (exact, then a unique case-insensitive match) so the
        two selectors fail the same way, and the resolved spelling is what gets
        queried because the API matches tags exactly.

        The Zotero ``tag`` parameter is a search expression, not a literal: a
        leading ``-`` negates and ``||`` is OR (measured against the Local API,
        which offers no documented escape — a backslash is matched literally and
        finds nothing). So a tag whose own name starts with ``-`` or contains
        ``||`` cannot be looked up literally; querying it would run a *negation*
        or a *union* instead, and #453's existence check would even certify the
        wrong result as real. Such a tag is rejected here rather than silently
        mis-queried. An interior hyphen (``Computer Science - Performance``) is
        literal, so only a leading ``-`` is treated as the metacharacter.
        """
        if not isinstance(tag, str) or not tag.strip():
            raise ZoteroError("tag must be a non-empty string.")
        stripped = tag.strip()
        if stripped.startswith("-") or "||" in stripped:
            raise ZoteroError(
                f"tag {tag!r} cannot be looked up literally: a leading '-' or '||' "
                "collides with Zotero's tag-search syntax (negation and OR), so the "
                "query would run a search instead of matching the tag by name, and "
                "the Local API offers no escape for these characters."
            )
        tags = self.list_tags()
        if tag in tags:
            return tag
        insensitive = sorted({t for t in tags if t.lower() == tag.lower()})
        if len(insensitive) == 1:
            return insensitive[0]
        if len(insensitive) > 1:
            raise ZoteroError(f"tag {tag!r} is ambiguous by case ({len(insensitive)} matches).")
        raise ZoteroError(f"tag {tag!r} not found. Available tags: {_suggest(tags)}")

    def get_items_by_tag(self, tag: str) -> list[dict]:
        resolved = self._resolve_tag(tag)
        return self._fetch(lambda: self._bibliographic(self._all(self.backend.items(tag=resolved))))

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
                self._fetch(lambda b=batch: self._all(self.backend.items(itemKey=",".join(b))))
            )
        # A key the library does not hold is dropped from the response with no
        # other signal, so an all-typo request used to look like a successful
        # import of nothing (#453). Checked against the *raw* response: a key that
        # does resolve but names an attachment or note is filtered below as a
        # non-bibliographic item, not reported as missing.
        found = {_item_key(item) for item in out}
        missing = [k for k in dict.fromkeys(keys) if k not in found]
        if missing:
            raise ZoteroError(f"item key(s) not found in Zotero: {', '.join(missing)}")
        return self._bibliographic(out)

    # -- attachments (phase 2) ---------------------------------------------
    def get_pdf_attachments(self, parent_key: str) -> list[dict]:
        """Downloadable PDF child attachments of an item, in Zotero's order.

        Kept only if the child is an ``attachment`` whose content type is PDF
        (compared case-insensitively, ignoring any ``; charset=…`` parameter) and
        whose ``linkMode`` is one Zotero actually stores. Snapshots, notes,
        non-PDF files, and linked (not stored) files are skipped.
        """
        children = self._fetch(lambda: self._all(self.backend.children(parent_key)))
        return [child for child in children if _is_downloadable_pdf(_data(child))]

    def fetch_file(self, key: str) -> bytes:
        """Download an attachment's bytes over the Local API (read-only).

        The whole file is loaded into memory; streaming large attachments is out
        of scope for phase 2.
        """
        if not isinstance(key, str) or not key.strip():
            raise ZoteroError("fetch_file needs a non-empty attachment key.")
        return self._fetch(lambda: self.backend.file(key))

    # -- notes & annotations (phase 3) -------------------------------------
    def get_notes(self, item_key: str) -> list[dict]:
        """Note *children* of a bibliographic item, in Zotero's order (read-only).

        Only child notes are returned; standalone (parent-less) notes are out of
        scope for phase 3 and are not reachable through this method.
        """
        children = self._fetch(lambda: self._all(self.backend.children(item_key)))
        return [c for c in children if _data(c).get("itemType") == "note"]

    def get_annotations(self, attachment_key: str) -> list[dict]:
        """Text-bearing annotations of a PDF attachment, in order (read-only).

        The Zotero **Local API does not return annotations via an attachment's
        ``/children``** (unlike notes, which are children of the parent item), so
        annotations are fetched with ``items(itemType="annotation")`` and matched
        by ``parentItem``. This also works on the Web API. The full annotation set
        is fetched once per client instance and reused across attachments.

        A denylist keeps everything except image/ink types (matched
        case-insensitively; a missing/unknown type is kept). Empty-text pruning is
        the annotation writer's job (#M), so this stays a thin filter.
        """
        if self._annotations is None:
            self._annotations = self._fetch(
                lambda: self._all(self.backend.items(itemType="annotation"))
            )
        return [
            a
            for a in self._annotations
            if _data(a).get("parentItem") == attachment_key
            and _data(a).get("itemType") == "annotation"
            and str(_data(a).get("annotationType", "")).lower() not in _NON_TEXT_ANNOTATION_TYPES
        ]
