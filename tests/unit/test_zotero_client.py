# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Zotero Local API client (phase 1, #9).

A fake backend stands in for pyzotero so the tests are deterministic and need no
network or the extra dependency. Fixtures mirror the real "neurosymbolic AI"
collection (top-level preprints alongside child attachments/notes) so the
bibliographic filter is exercised against realistic shapes.
"""
from __future__ import annotations

import pytest

from factlog.integrations.zotero.api_client import (
    ZoteroClient,
    ZoteroConnectionError,
    ZoteroError,
)
from factlog.integrations.zotero.config import ZoteroConfig

# A real preprint item from the live library (abstract trimmed).
PREPRINT = {
    "key": "KH78JUPE",
    "data": {
        "key": "KH78JUPE",
        "itemType": "preprint",
        "title": "Neurosymbolic Value-Inspired AI (Why, What, and How)",
        "abstractNote": "The rapid progression of AI systems ...",
        "date": "2023-12-15",
        "DOI": "10.48550/arXiv.2312.09928",
        "extra": "arXiv:2312.09928 [cs.AI]",
        "creators": [
            {"firstName": "Amit", "lastName": "Sheth", "creatorType": "author"},
            {"firstName": "Kaushik", "lastName": "Roy", "creatorType": "author"},
        ],
        "tags": [
            {"tag": "Computer Science - Artificial Intelligence", "type": 1},
            {"tag": "neurosymbolic AI"},
        ],
        "dateModified": "2026-07-08T00:23:43Z",
    },
}
ATTACHMENT = {"key": "ATT1", "data": {"key": "ATT1", "itemType": "attachment", "title": "Preprint PDF"}}
NOTE = {"key": "NOTE1", "data": {"key": "NOTE1", "itemType": "note"}}


class FakeBackend:
    """Minimal pyzotero stand-in. Records calls; raises on demand."""

    def __init__(self, collections=None, items=None, raise_os=False):
        self._collections = collections or []
        self._items = items or []
        self._raise_os = raise_os
        self.calls: list[tuple] = []

    def _maybe_raise(self):
        if self._raise_os:
            raise ConnectionError("simulated: Zotero not running")

    def everything(self, page):
        return page  # no real pagination in the fake

    def collections(self):
        self.calls.append(("collections",))
        self._maybe_raise()
        return list(self._collections)

    def collection_items_top(self, key):
        self.calls.append(("collection_items_top", key))
        self._maybe_raise()
        return list(self._items)

    def items(self, **kwargs):
        self.calls.append(("items", kwargs))
        self._maybe_raise()
        return list(self._items)


def _col(name, key):
    return {"key": key, "data": {"key": key, "name": name}}


def _client(**kw):
    return ZoteroClient(ZoteroConfig(), backend=FakeBackend(**kw))


class TestBibliographicFilter:
    def test_collection_drops_attachments_and_notes(self):
        c = _client(
            collections=[_col("neurosymbolic AI", "8QBS9PK7")],
            items=[PREPRINT, ATTACHMENT, NOTE],
        )
        out = c.get_items_by_collection("neurosymbolic AI")
        assert [i["key"] for i in out] == ["KH78JUPE"]

    def test_tag_query_filters_too(self):
        c = _client(items=[PREPRINT, NOTE])
        assert [i["key"] for i in c.get_items_by_tag("neurosymbolic AI")] == ["KH78JUPE"]

    def test_ids_query_filters_too(self):
        c = _client(items=[ATTACHMENT, PREPRINT])
        assert [i["key"] for i in c.get_items_by_ids(["KH78JUPE", "ATT1"])] == ["KH78JUPE"]


class TestCollectionResolution:
    def test_exact_name_resolves_key(self):
        backend = FakeBackend(collections=[_col("A", "K1"), _col("neurosymbolic AI", "K2")],
                              items=[PREPRINT])
        c = ZoteroClient(ZoteroConfig(), backend=backend)
        c.get_items_by_collection("neurosymbolic AI")
        assert ("collection_items_top", "K2") in backend.calls

    def test_case_insensitive_fallback(self):
        backend = FakeBackend(collections=[_col("Neurosymbolic AI", "K2")], items=[PREPRINT])
        c = ZoteroClient(ZoteroConfig(), backend=backend)
        c.get_items_by_collection("neurosymbolic ai")
        assert ("collection_items_top", "K2") in backend.calls

    def test_unknown_collection_lists_available(self):
        c = _client(collections=[_col("Alpha", "K1"), _col("Beta", "K2")])
        with pytest.raises(ZoteroError, match="not found.*Alpha, Beta"):
            c.get_items_by_collection("Gamma")

    def test_ambiguous_exact_name_errors(self):
        c = _client(collections=[_col("Dup", "K1"), _col("Dup", "K2")])
        with pytest.raises(ZoteroError, match="ambiguous"):
            c.get_items_by_collection("Dup")


class TestConnectionErrors:
    def test_connection_failure_is_wrapped(self):
        c = _client(raise_os=True)
        with pytest.raises(ZoteroConnectionError, match="Local API"):
            c.list_collections()

    def test_collection_query_connection_failure(self):
        c = _client(collections=[_col("X", "K")], items=[], raise_os=True)
        with pytest.raises(ZoteroConnectionError):
            c.get_items_by_collection("X")


class TestModeGuard:
    def test_web_mode_rejected(self):
        # No backend injected -> _connect runs and rejects web mode.
        c = ZoteroClient(ZoteroConfig(mode="web", web_user_id="1", web_api_key="k"))
        with pytest.raises(ZoteroError, match="not supported in phase 1"):
            c.list_collections()

    def test_non_default_port_rejected(self):
        c = ZoteroClient(ZoteroConfig(mode="local", local_port=24000))
        with pytest.raises(ZoteroError, match="port 23119"):
            c.list_collections()


class TestIdsEdge:
    def test_empty_ids_returns_empty_without_backend_call(self):
        backend = FakeBackend(items=[PREPRINT])
        c = ZoteroClient(ZoteroConfig(), backend=backend)
        assert c.get_items_by_ids([" ", ""]) == []
        assert backend.calls == []
