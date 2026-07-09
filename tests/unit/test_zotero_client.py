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


# Fake exceptions whose class names mirror the real pyzotero/requests hierarchy,
# so _classify (which inspects the MRO names) routes them like the real ones.
class HTTPError(Exception):  # requests.exceptions.HTTPError / pyzotero HTTPError
    pass


class ConnectTimeout(OSError):  # requests.exceptions timeout family
    pass


class ResourceNotFound(Exception):  # pyzotero.zotero_errors.ResourceNotFound (404)
    pass


class FakeBackend:
    """Minimal pyzotero stand-in. Records calls; raises/pages on demand."""

    def __init__(self, collections=None, items=None, raise_os=False, exc=None, extra_pages=None,
                 children=None, files=None, annotations=None):
        self._collections = collections or []
        self._items = items or []
        self._raise_os = raise_os
        self._exc = exc
        self._extra_pages = extra_pages or []
        self._children = children or []
        self._files = files or {}
        self._annotations = annotations or []
        self.calls: list[tuple] = []

    def _maybe_raise(self):
        if self._exc is not None:
            raise self._exc
        if self._raise_os:
            raise ConnectionError("simulated: Zotero not running")

    def everything(self, page):
        # Simulate link-following pagination: append the queued extra pages.
        result = list(page)
        for extra in self._extra_pages:
            result.extend(extra)
        return result

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
        if kwargs.get("itemType") == "annotation":
            return list(self._annotations)
        return list(self._items)

    def children(self, parent_key):
        self.calls.append(("children", parent_key))
        self._maybe_raise()
        return list(self._children)

    def file(self, key):
        self.calls.append(("file", key))
        self._maybe_raise()
        if key not in self._files:
            raise ResourceNotFound(f"404: {key}")  # mirror pyzotero's not-found
        return self._files[key]


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

    def test_ids_are_joined_into_itemkey(self):
        backend = FakeBackend(items=[PREPRINT])
        ZoteroClient(ZoteroConfig(), backend=backend).get_items_by_ids(["A", "B", "C"])
        assert backend.calls == [("items", {"itemKey": "A,B,C"})]

    def test_ids_batched_over_fifty(self):
        backend = FakeBackend(items=[])
        ids = [f"K{i}" for i in range(120)]
        ZoteroClient(ZoteroConfig(), backend=backend).get_items_by_ids(ids)
        item_calls = [c for c in backend.calls if c[0] == "items"]
        assert len(item_calls) == 3  # 50 + 50 + 20
        assert item_calls[0][1]["itemKey"].count(",") == 49


class TestPagination:
    def test_collection_follows_extra_pages(self):
        second = {"key": "P2", "data": {"key": "P2", "itemType": "preprint", "title": "t2"}}
        backend = FakeBackend(
            collections=[_col("C", "K")], items=[PREPRINT], extra_pages=[[second]]
        )
        c = ZoteroClient(ZoteroConfig(), backend=backend)
        out = c.get_items_by_collection("C")
        assert {i["key"] for i in out} == {"KH78JUPE", "P2"}


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

    def test_case_ambiguous_errors_not_not_found(self):
        c = _client(collections=[_col("Dup", "K1"), _col("dup", "K2")])
        with pytest.raises(ZoteroError, match="ambiguous by case"):
            c.get_items_by_collection("DUP")

    def test_empty_name_rejected(self):
        c = _client(collections=[_col("A", "K1")])
        with pytest.raises(ZoteroError, match="non-empty string"):
            c.get_items_by_collection("   ")


class TestConnectionErrors:
    def test_connection_failure_is_wrapped(self):
        c = _client(raise_os=True)
        with pytest.raises(ZoteroConnectionError, match="Local API"):
            c.list_collections()

    def test_collection_query_connection_failure(self):
        c = _client(collections=[_col("X", "K")], items=[], raise_os=True)
        with pytest.raises(ZoteroConnectionError):
            c.get_items_by_collection("X")

    def test_timeout_is_connection_error(self):
        c = _client(exc=ConnectTimeout("slow"))
        with pytest.raises(ZoteroConnectionError, match="ConnectTimeout"):
            c.list_collections()

    def test_http_error_is_not_misreported_as_unreachable(self):
        # A live-but-erroring server (HTTP 4xx/5xx) must be a ZoteroError, not a
        # "Zotero not running" ZoteroConnectionError.
        c = _client(exc=HTTPError("404 Not Found"))
        with pytest.raises(ZoteroError, match="request failed") as ei:
            c.list_collections()
        assert not isinstance(ei.value, ZoteroConnectionError)

    def test_unknown_exception_propagates_unchanged(self):
        c = _client(exc=ValueError("weird bug"))
        with pytest.raises(ValueError, match="weird bug"):
            c.list_collections()


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


def _pdf(key, **over):
    data = {"key": key, "itemType": "attachment", "parentItem": "KH78JUPE",
            "contentType": "application/pdf", "linkMode": "imported_url", "filename": f"{key}.pdf"}
    data.update(over)
    return {"key": key, "data": data}


# A stored PDF, a snapshot, a note, and a *linked* (non-downloadable) PDF.
PDF_ATT = _pdf("NZ4XXMUR")
SNAPSHOT = {"key": "SNAP1", "data": {"key": "SNAP1", "itemType": "attachment",
                                     "contentType": "text/html", "linkMode": "imported_url"}}
CHILD_NOTE = {"key": "CN1", "data": {"key": "CN1", "itemType": "note", "parentItem": "KH78JUPE"}}
LINKED_PDF = _pdf("LINK1", linkMode="linked_url")


class TestPdfAttachments:
    def test_filters_to_pdf_attachments_only(self):
        backend = FakeBackend(children=[SNAPSHOT, PDF_ATT, CHILD_NOTE, LINKED_PDF])
        c = ZoteroClient(ZoteroConfig(), backend=backend)
        out = c.get_pdf_attachments("KH78JUPE")
        assert [a["key"] for a in out] == ["NZ4XXMUR"]  # linked PDF excluded too
        assert ("children", "KH78JUPE") in backend.calls

    def test_no_pdf_children_returns_empty(self):
        c = ZoteroClient(ZoteroConfig(), backend=FakeBackend(children=[SNAPSHOT, CHILD_NOTE]))
        assert c.get_pdf_attachments("KH78JUPE") == []

    def test_content_type_case_and_charset_normalised(self):
        a = _pdf("A", contentType="Application/PDF")
        b = _pdf("B", contentType="application/pdf; charset=binary")
        c = ZoteroClient(ZoteroConfig(), backend=FakeBackend(children=[a, b]))
        assert [x["key"] for x in c.get_pdf_attachments("P")] == ["A", "B"]

    def test_linked_modes_excluded(self):
        c = ZoteroClient(ZoteroConfig(), backend=FakeBackend(children=[
            _pdf("F", linkMode="linked_file"), _pdf("U", linkMode="linked_url")]))
        assert c.get_pdf_attachments("P") == []

    def test_order_preserved_across_pages(self):
        backend = FakeBackend(children=[PDF_ATT], extra_pages=[[_pdf("PDF2")]])
        c = ZoteroClient(ZoteroConfig(), backend=backend)
        assert [a["key"] for a in c.get_pdf_attachments("P")] == ["NZ4XXMUR", "PDF2"]

    def test_connection_failure_wrapped(self):
        c = ZoteroClient(ZoteroConfig(), backend=FakeBackend(raise_os=True))
        with pytest.raises(ZoteroConnectionError):
            c.get_pdf_attachments("P")


class TestFetchFile:
    def test_returns_bytes(self):
        backend = FakeBackend(files={"NZ4XXMUR": b"%PDF-1.7 ..."})
        c = ZoteroClient(ZoteroConfig(), backend=backend)
        assert c.fetch_file("NZ4XXMUR") == b"%PDF-1.7 ..."
        assert ("file", "NZ4XXMUR") in backend.calls

    def test_blank_key_rejected_without_backend_call(self):
        backend = FakeBackend(files={"K": b"x"})
        c = ZoteroClient(ZoteroConfig(), backend=backend)
        with pytest.raises(ZoteroError, match="non-empty attachment key"):
            c.fetch_file("  ")
        assert backend.calls == []

    def test_not_found_key_maps_to_zotero_error(self):
        c = ZoteroClient(ZoteroConfig(), backend=FakeBackend(files={"K": b"x"}))
        with pytest.raises(ZoteroError) as ei:
            c.fetch_file("MISSING")
        assert not isinstance(ei.value, ZoteroConnectionError)

    def test_connection_failure_wrapped(self):
        c = ZoteroClient(ZoteroConfig(), backend=FakeBackend(raise_os=True))
        with pytest.raises(ZoteroConnectionError):
            c.fetch_file("K")


def _note(key, parent="KH78JUPE"):
    return {"key": key, "data": {"key": key, "itemType": "note", "parentItem": parent,
                                 "note": "<p>Comment: under review</p>"}}


def _annotation(key, atype="highlight", text="passage", comment="", page="3", parent="A"):
    return {"key": key, "data": {"key": key, "itemType": "annotation", "annotationType": atype,
                                 "annotationText": text, "annotationComment": comment,
                                 "annotationPageLabel": page, "parentItem": parent}}


class TestNotes:
    def test_filters_to_notes(self):
        backend = FakeBackend(children=[_note("N1"), PDF_ATT, _annotation("A1")])
        c = ZoteroClient(ZoteroConfig(), backend=backend)
        out = c.get_notes("KH78JUPE")
        assert [n["key"] for n in out] == ["N1"]
        assert ("children", "KH78JUPE") in backend.calls

    def test_no_notes_returns_empty(self):
        c = ZoteroClient(ZoteroConfig(), backend=FakeBackend(children=[PDF_ATT]))
        assert c.get_notes("K") == []

    def test_order_and_pagination(self):
        backend = FakeBackend(children=[_note("N1")], extra_pages=[[_note("N2")]])
        c = ZoteroClient(ZoteroConfig(), backend=backend)
        assert [n["key"] for n in c.get_notes("K")] == ["N1", "N2"]

    def test_connection_failure_wrapped(self):
        c = ZoteroClient(ZoteroConfig(), backend=FakeBackend(raise_os=True))
        with pytest.raises(ZoteroConnectionError):
            c.get_notes("K")


class TestAnnotations:
    # Annotations are fetched via items(itemType="annotation") and matched by
    # parentItem — the Local API does NOT return them via an attachment's children.
    def test_filters_to_text_annotations_of_parent(self):
        backend = FakeBackend(annotations=[
            _annotation("H", "highlight", parent="A"),
            _annotation("U", "underline", parent="A"),
            _annotation("N", "note", parent="A"),
            _annotation("IMG", "image", parent="A"),   # excluded (no text type)
            _annotation("INK", "ink", parent="A"),      # excluded
            _annotation("OTHER", "highlight", parent="B"),  # different attachment
        ])
        c = ZoteroClient(ZoteroConfig(), backend=backend)
        out = c.get_annotations("A")
        assert [a["key"] for a in out] == ["H", "U", "N"]
        assert ("items", {"itemType": "annotation"}) in backend.calls

    def test_uses_items_not_children(self):
        backend = FakeBackend(annotations=[_annotation("H", parent="ATTKEY")])
        ZoteroClient(ZoteroConfig(), backend=backend).get_annotations("ATTKEY")
        # Local API doesn't serve annotations via /children, so we must not call it.
        assert not any(call[0] == "children" for call in backend.calls)

    def test_no_annotations_returns_empty(self):
        c = ZoteroClient(ZoteroConfig(), backend=FakeBackend(annotations=[]))
        assert c.get_annotations("A") == []

    def test_order_and_pagination(self):
        backend = FakeBackend(annotations=[_annotation("A1", parent="A")],
                              extra_pages=[[_annotation("A2", parent="A")]])
        c = ZoteroClient(ZoteroConfig(), backend=backend)
        assert [a["key"] for a in c.get_annotations("A")] == ["A1", "A2"]

    def test_fetched_once_and_reused_across_attachments(self):
        backend = FakeBackend(annotations=[_annotation("H1", parent="A"), _annotation("H2", parent="B")])
        c = ZoteroClient(ZoteroConfig(), backend=backend)
        assert [a["key"] for a in c.get_annotations("A")] == ["H1"]
        assert [a["key"] for a in c.get_annotations("B")] == ["H2"]
        # Only one library-wide annotation fetch, reused for both attachments.
        assert sum(1 for call in backend.calls if call == ("items", {"itemType": "annotation"})) == 1

    def test_case_variant_image_ink_excluded(self):
        c = ZoteroClient(ZoteroConfig(), backend=FakeBackend(annotations=[
            _annotation("H", "highlight"), _annotation("IMG", "Image"), _annotation("INK", "INK")]))
        assert [a["key"] for a in c.get_annotations("A")] == ["H"]

    def test_missing_type_is_kept_fail_open(self):
        typeless = {"key": "T", "data": {"key": "T", "itemType": "annotation",
                                         "parentItem": "A", "annotationText": "x"}}
        c = ZoteroClient(ZoteroConfig(), backend=FakeBackend(annotations=[typeless]))
        assert [a["key"] for a in c.get_annotations("A")] == ["T"]

    def test_empty_text_annotation_kept_at_client_level(self):
        # Pruning empty-text annotations is the writer's job (#M), not the client's.
        empty = _annotation("E", "highlight", text="", comment="")
        c = ZoteroClient(ZoteroConfig(), backend=FakeBackend(annotations=[empty]))
        assert [a["key"] for a in c.get_annotations("A")] == ["E"]

    def test_connection_failure_wrapped(self):
        c = ZoteroClient(ZoteroConfig(), backend=FakeBackend(raise_os=True))
        with pytest.raises(ZoteroConnectionError):
            c.get_annotations("A")
