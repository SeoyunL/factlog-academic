# SPDX-License-Identifier: Apache-2.0
"""CLI tests for `factlog zotero-import` (phase 1, #11).

The real Zotero client is replaced via _make_zotero_client so the command runs
without a live library. A temp KB (with sources/) is the import target.
"""
from __future__ import annotations

import pytest

from factlog import cli
from factlog.integrations.zotero.api_client import ZoteroConnectionError, ZoteroError


def _kb(tmp_path):
    (tmp_path / "sources").mkdir()
    return tmp_path


def _item(key, title="T"):
    return {"key": key, "data": {"key": key, "itemType": "journalArticle", "title": title}}


class FakeClient:
    def __init__(self, items=None, raise_exc=None):
        self._items = items or []
        self._raise = raise_exc

    def _maybe(self):
        if self._raise is not None:
            raise self._raise

    def get_items_by_collection(self, name):
        self._maybe()
        return list(self._items)

    def get_items_by_tag(self, tag):
        self._maybe()
        return list(self._items)

    def get_items_by_ids(self, ids):
        self._maybe()
        return list(self._items)


def _run(monkeypatch, argv, client):
    monkeypatch.setattr(cli, "_make_zotero_client", lambda config: client)
    return cli.main(argv)


class TestParserSelection:
    def test_requires_a_selector(self, capsys):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["zotero-import"])

    def test_selectors_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["zotero-import", "--collection", "A", "--tag", "b"])

    def test_single_selector_ok(self):
        args = cli.build_parser().parse_args(["zotero-import", "--collection", "A"])
        assert args.collection == "A" and args.func is cli.cmd_zotero_import


class TestRun:
    def test_import_collection_reports_and_writes(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeClient([_item("K1", "One"), _item("K2", "Two")])
        rc = _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb)], client)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Imported: 2" in out
        assert "Next step:" in out
        assert len(list((kb / "sources").glob("*.md"))) == 2

    def test_reimport_skips(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        items = [_item("K1", "One")]
        _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb)], FakeClient(items))
        rc = _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb)], FakeClient(items))
        out = capsys.readouterr().out
        assert rc == 0
        assert "Skipped:  1" in out

    def test_connection_error_exits_2(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeClient(raise_exc=ZoteroConnectionError("not running"))
        rc = _run(monkeypatch, ["zotero-import", "--tag", "t", "--target", str(kb)], client)
        assert rc == 2
        assert "not running" in capsys.readouterr().err

    def test_zotero_error_exits_1(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeClient(raise_exc=ZoteroError("collection 'Z' not found"))
        rc = _run(monkeypatch, ["zotero-import", "--collection", "Z", "--target", str(kb)], client)
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_not_a_kb_exits_1(self, tmp_path, monkeypatch, capsys):
        # target has no sources/ -> _require_kb fails before touching Zotero.
        rc = _run(monkeypatch, ["zotero-import", "--tag", "t", "--target", str(tmp_path)], FakeClient([]))
        assert rc == 1
        assert "not a factlog KB" in capsys.readouterr().err

    def test_item_error_makes_exit_nonzero(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeClient([_item("", "NoKey")])  # missing key -> error outcome
        rc = _run(monkeypatch, ["zotero-import", "--tag", "t", "--target", str(kb)], client)
        out = capsys.readouterr().out
        assert rc == 1
        assert "Errors:   1" in out

    def test_empty_items_is_graceful_error(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        rc = _run(monkeypatch, ["zotero-import", "--items", ",,", "--target", str(kb)], FakeClient([]))
        assert rc == 1
        assert "at least one item key" in capsys.readouterr().err

    def test_malformed_kb_config_is_graceful_error(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        (kb / "policy").mkdir()
        (kb / "policy" / "zotero-config.toml").write_text("this = = not toml", encoding="utf-8")
        rc = _run(monkeypatch, ["zotero-import", "--tag", "t", "--target", str(kb)], FakeClient([]))
        assert rc == 1
        assert "invalid TOML" in capsys.readouterr().err

    def test_non_ascii_title_in_output(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeClient([_item("K1", "한글 제목 논문")])
        rc = _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb)], client)
        assert rc == 0
        assert "한글 제목 논문" in capsys.readouterr().out
