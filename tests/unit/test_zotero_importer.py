# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Zotero import orchestration (phase 1, #11).

A fake client feeds preset items so the fetch->parse->write pipeline is exercised
deterministically against a temp KB.
"""
from __future__ import annotations

import pytest

from factlog.integrations.zotero.importer import fetch_items, import_items


def _item(key, title="T", **data):
    d = {"key": key, "itemType": "journalArticle", "title": title}
    d.update(data)
    return {"key": key, "data": d}


class FakeClient:
    def __init__(self, items):
        self._items = items
        self.calls = []

    def get_items_by_collection(self, name):
        self.calls.append(("collection", name))
        return list(self._items)

    def get_items_by_tag(self, tag):
        self.calls.append(("tag", tag))
        return list(self._items)

    def get_items_by_ids(self, ids):
        self.calls.append(("ids", tuple(ids)))
        return list(self._items)


class TestFetchRouting:
    def test_collection_routes(self):
        c = FakeClient([_item("A")])
        fetch_items(c, collection="X")
        assert c.calls == [("collection", "X")]

    def test_tag_routes(self):
        c = FakeClient([])
        fetch_items(c, tag="t")
        assert c.calls == [("tag", "t")]

    def test_ids_routes(self):
        c = FakeClient([])
        fetch_items(c, items=["A", "B"])
        assert c.calls == [("ids", ("A", "B"))]

    def test_requires_exactly_one_selector(self):
        c = FakeClient([])
        with pytest.raises(ValueError, match="exactly one"):
            fetch_items(c)
        with pytest.raises(ValueError, match="exactly one"):
            fetch_items(c, collection="X", tag="t")


class TestImport:
    def test_imports_and_counts(self, tmp_path):
        c = FakeClient([_item("K1", "One"), _item("K2", "Two")])
        report = import_items(c, target=tmp_path, collection="X", imported_at="2026-07-08T00:00:00Z")
        assert report.imported == 2
        assert report.skipped == 0
        assert report.errors == 0
        assert len(list((tmp_path / "sources").glob("*.md"))) == 2

    def test_reimport_is_skipped(self, tmp_path):
        items = [_item("K1", "One")]
        import_items(FakeClient(items), target=tmp_path, collection="X")
        report = import_items(FakeClient(items), target=tmp_path, collection="X")
        assert report.imported == 0 and report.skipped == 1

    def test_missing_key_is_error_not_abort(self, tmp_path):
        c = FakeClient([_item("K1", "Good"), _item("", "NoKey"), _item("K3", "Also")])
        report = import_items(c, target=tmp_path, tag="t")
        assert report.imported == 2
        assert report.errors == 1
        assert {o.status for o in report.outcomes} == {"imported", "error"}

    def test_deterministic_order_by_key(self, tmp_path):
        c = FakeClient([_item("Zeta", "z"), _item("Alpha", "a")])
        report = import_items(c, target=tmp_path, collection="X")
        assert [o.key for o in report.outcomes] == ["Alpha", "Zeta"]

    def test_empty_result(self, tmp_path):
        report = import_items(FakeClient([]), target=tmp_path, collection="X")
        assert report.outcomes == []
        assert report.imported == 0

    def test_write_oserror_is_error_outcome_not_abort(self, tmp_path, monkeypatch):
        from factlog.integrations.zotero import importer as imp

        real_write = imp.SourceWriter.write

        def flaky(self, parsed, target, imported_at=""):
            if parsed.get("zotero_key") == "BAD":
                raise OSError("disk full")
            return real_write(self, parsed, target, imported_at)

        monkeypatch.setattr(imp.SourceWriter, "write", flaky)
        c = FakeClient([_item("AAA", "ok"), _item("BAD", "boom")])
        report = import_items(c, target=tmp_path, collection="X")
        assert report.imported == 1
        assert report.errors == 1
        bad = next(o for o in report.outcomes if o.key == "BAD")
        assert bad.status == "error" and "disk full" in bad.reason

    def test_sort_uses_parsed_key_from_wrapper_fallback(self, tmp_path):
        # data has no key; parse_item falls back to the wrapper key. The sort must
        # honor that identity, not treat it as "".
        a = {"key": "AAA", "data": {"itemType": "journalArticle", "title": "a"}}
        z = {"key": "ZZZ", "data": {"itemType": "journalArticle", "title": "z"}}
        report = import_items(FakeClient([z, a]), target=tmp_path, collection="X")
        assert [o.key for o in report.outcomes] == ["AAA", "ZZZ"]
