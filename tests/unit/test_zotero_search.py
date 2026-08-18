# SPDX-License-Identifier: Apache-2.0
"""Unit tests for Zotero discovery search (#455).

Two layers share this file: the client method :meth:`ZoteroClient.search_items`
(below) and the ``zotero-search`` CLI command (further down). Both use a fake
backend so they stay deterministic and pyzotero-free, mirroring
``test_zotero_client.py``'s convention — a minimal stand-in that records the
kwargs it was called with, so an assertion can prove ``q``/``qmode``/``limit``
reached the API unchanged.

The class at the bottom is the exception: it tests when the client is built
(#625), so it builds the real one and lets pyzotero be looked up. Still no
network — each case is decided inside ``_connect()``, from config or a failed
import, before a socket exists.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from factlog.integrations.zotero.api_client import (
    ZoteroClient,
    ZoteroConnectionError,
    ZoteroError,
)
from factlog.integrations.zotero.config import ZoteroConfig


PREPRINT = {
    "key": "KH78JUPE",
    "data": {
        "key": "KH78JUPE",
        "itemType": "preprint",
        "title": "Neurosymbolic Value-Inspired AI (Why, What, and How)",
    },
}
ARTICLE = {
    "key": "ABCD1234",
    "data": {"key": "ABCD1234", "itemType": "journalArticle", "title": "Protein folding"},
}
ATTACHMENT = {"key": "ATT1", "data": {"key": "ATT1", "itemType": "attachment", "title": "PDF"}}
NOTE = {"key": "NOTE1", "data": {"key": "NOTE1", "itemType": "note"}}


class ConnectError(Exception):  # httpx.ConnectError name — _classify routes by MRO name
    pass


class FakeBackend:
    """Minimal pyzotero stand-in for search: records the items() kwargs, or raises.

    pyzotero exposes the last response on ``backend.request``; ``total`` sets its
    ``Total-Results`` header so :func:`api_client._total_results` reads it. Left as
    ``None``, there is no ``request`` — the header-absent path the client tolerates.
    """

    def __init__(self, items=None, exc=None, total=None):
        self._items = items or []
        self._exc = exc
        self.calls: list[dict] = []
        self.request = None
        if total is not None:
            self.request = SimpleNamespace(headers={"Total-Results": str(total)})

    def items(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return list(self._items)


def _client(**kw):
    return ZoteroClient(ZoteroConfig(), backend=FakeBackend(**kw))


class TestSearchItems:
    def test_returns_bibliographic_items_only(self):
        client = _client(items=[PREPRINT, ATTACHMENT, NOTE, ARTICLE])
        results, _ = client.search_items("neurosymbolic")
        keys = [r["data"]["key"] for r in results]
        assert keys == ["KH78JUPE", "ABCD1234"]

    def test_passes_query_and_default_qmode(self):
        backend = FakeBackend(items=[PREPRINT])
        client = ZoteroClient(ZoteroConfig(), backend=backend)
        client.search_items("protein folding")
        assert backend.calls == [{"q": "protein folding", "qmode": "titleCreatorYear"}]

    def test_forwards_qmode_and_limit(self):
        backend = FakeBackend(items=[PREPRINT])
        client = ZoteroClient(ZoteroConfig(), backend=backend)
        client.search_items("folding", qmode="everything", limit=5)
        assert backend.calls == [{"q": "folding", "qmode": "everything", "limit": 5}]

    def test_omits_limit_when_none(self):
        # None must not become an explicit limit= kwarg — the API keeps its own default.
        backend = FakeBackend(items=[])
        client = ZoteroClient(ZoteroConfig(), backend=backend)
        client.search_items("anything", limit=None)
        assert "limit" not in backend.calls[0]

    def test_empty_result_is_returned_not_an_error(self):
        # A successful search that matched nothing is an honest empty list, distinct
        # from a connection failure (which raises) — the CLI relies on this split.
        items, _ = _client(items=[]).search_items("no such thing")
        assert items == []

    def test_total_results_reports_the_full_match_count(self):
        # --limit truncates the returned rows, but the header carries the real total,
        # so a caller can report "showing top N of M" instead of silently dropping M-N.
        client = _client(items=[PREPRINT, ARTICLE], total=10)
        items, total = client.search_items("neurosymbolic", limit=2)
        assert len(items) == 2 and total == 10

    def test_total_is_none_when_header_absent(self):
        # No Total-Results header (older server, or a backend that does not expose the
        # response) -> None, and the CLI falls back to the returned row count.
        client = _client(items=[PREPRINT, ARTICLE])  # total unset -> no request
        _, total = client.search_items("x")
        assert total is None

    def test_total_is_none_when_header_unparseable(self):
        backend = FakeBackend(items=[PREPRINT])
        backend.request = SimpleNamespace(headers={"Total-Results": "not-a-number"})
        client = ZoteroClient(ZoteroConfig(), backend=backend)
        _, total = client.search_items("x")
        assert total is None

    @pytest.mark.parametrize("q", ["", "   ", "\t\n"])
    def test_blank_query_is_rejected_before_any_request(self, q):
        backend = FakeBackend(items=[PREPRINT])
        client = ZoteroClient(ZoteroConfig(), backend=backend)
        with pytest.raises(ZoteroError):
            client.search_items(q)
        assert backend.calls == []  # nothing was sent

    def test_does_not_page_with_everything(self):
        # A search is a bounded top-N; it must issue exactly one items() call and
        # never follow pagination (which would return the whole library past --limit).
        backend = FakeBackend(items=[PREPRINT, ARTICLE])
        client = ZoteroClient(ZoteroConfig(), backend=backend)
        client.search_items("x", limit=2)
        assert len(backend.calls) == 1

    def test_connection_failure_raises_connection_error(self):
        client = _client(items=[PREPRINT], exc=ConnectError("refused"))
        with pytest.raises(ZoteroConnectionError):
            client.search_items("neurosymbolic")


# --- CLI layer (`factlog zotero-search`) ---------------------------------------
from factlog import cli  # noqa: E402 - kept next to the CLI tests that use it


def _kb(tmp_path):
    (tmp_path / "sources").mkdir()
    return tmp_path


class FakeSearchClient:
    """A client whose search_items returns ``(items, total)``, records args, or raises.

    ``total`` defaults to ``None`` — the header-absent path, where the command falls
    back to the shown row count — so a test that cares only about the row shape need
    not set it; the truncation tests pass a ``total`` larger than ``items``.
    """

    def __init__(self, items=None, raise_exc=None, total=None):
        self._items = items or []
        self._raise = raise_exc
        self._total = total
        self.calls: list[tuple] = []

    def search_items(self, q, qmode="titleCreatorYear", limit=None):
        self.calls.append((q, qmode, limit))
        if self._raise is not None:
            raise self._raise
        return list(self._items), self._total


def _run(monkeypatch, argv, client):
    monkeypatch.setattr(cli, "_make_zotero_client", lambda config: client)
    return cli.main(argv)


class TestParser:
    def test_query_is_positional_and_routes_to_the_command(self):
        args = cli.build_parser().parse_args(["zotero-search", "protein folding"])
        assert args.query == "protein folding" and args.func is cli.cmd_zotero_search

    def test_qmode_defaults_to_title_creator_year(self):
        args = cli.build_parser().parse_args(["zotero-search", "x"])
        assert args.qmode == "titleCreatorYear"

    def test_qmode_rejects_an_unknown_mode(self):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["zotero-search", "x", "--qmode", "fuzzy"])


class TestRun:
    def test_lists_key_itemtype_and_title(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeSearchClient([PREPRINT, ARTICLE])
        rc = _run(monkeypatch, ["zotero-search", "neurosymbolic", "--target", str(kb)], client)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Found 2 results" in out
        assert "[preprint] KH78JUPE" in out
        assert "[journalArticle] ABCD1234" in out
        assert "Protein folding" in out

    def test_forwards_qmode_and_limit_to_the_client(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeSearchClient([PREPRINT])
        _run(monkeypatch, ["zotero-search", "folding", "--qmode", "everything",
                           "--limit", "7", "--target", str(kb)], client)
        assert client.calls == [("folding", "everything", 7)]

    def test_default_limit_is_the_policy_default(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeSearchClient([PREPRINT])
        _run(monkeypatch, ["zotero-search", "x", "--target", str(kb)], client)
        assert client.calls == [("x", "titleCreatorYear", cli._ZOTERO_SEARCH_DEFAULT_LIMIT)]

    def test_zero_results_is_exit_0_not_an_error(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        rc = _run(monkeypatch, ["zotero-search", "no such thing", "--target", str(kb)],
                  FakeSearchClient([]))
        out = capsys.readouterr().out
        assert rc == 0
        assert "Found 0 results" in out

    def test_connection_failure_exits_2(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeSearchClient(raise_exc=ZoteroConnectionError("not running"))
        rc = _run(monkeypatch, ["zotero-search", "x", "--target", str(kb)], client)
        assert rc == 2
        assert "not running" in capsys.readouterr().err

    def test_other_zotero_error_exits_1(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeSearchClient(raise_exc=ZoteroError("request failed"))
        rc = _run(monkeypatch, ["zotero-search", "x", "--target", str(kb)], client)
        assert rc == 1
        assert "request failed" in capsys.readouterr().err

    def test_blank_query_is_rejected_before_touching_zotero(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeSearchClient([PREPRINT])
        rc = _run(monkeypatch, ["zotero-search", "   ", "--target", str(kb)], client)
        assert rc == 1
        assert client.calls == []  # never reached the client
        assert "non-empty" in capsys.readouterr().err

    def test_out_of_range_limit_is_rejected(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeSearchClient([PREPRINT])
        rc = _run(monkeypatch, ["zotero-search", "x", "--limit", "0", "--target", str(kb)], client)
        assert rc == 1
        assert client.calls == []
        assert "between 1 and" in capsys.readouterr().err

    def test_not_a_kb_exits_1(self, tmp_path, monkeypatch, capsys):
        # target with no sources/ -> _require_kb fails before touching Zotero.
        rc = _run(monkeypatch, ["zotero-search", "x", "--target", str(tmp_path)],
                  FakeSearchClient([PREPRINT]))
        assert rc == 1

    def test_porcelain_emits_result_and_found_rows(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeSearchClient([PREPRINT, ARTICLE])
        rc = _run(monkeypatch, ["zotero-search", "x", "--porcelain", "--target", str(kb)], client)
        lines = capsys.readouterr().out.splitlines()
        assert rc == 0
        assert lines == [
            "result\t1\tKH78JUPE\tpreprint\tNeurosymbolic Value-Inspired AI (Why, What, and How)",
            "result\t2\tABCD1234\tjournalArticle\tProtein folding",
            "found\t2",
        ]

    def test_porcelain_zero_results_emits_only_found_zero(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        rc = _run(monkeypatch, ["zotero-search", "x", "--porcelain", "--target", str(kb)],
                  FakeSearchClient([]))
        assert rc == 0
        assert capsys.readouterr().out.splitlines() == ["found\t0"]

    def test_human_heading_reports_total_when_limit_truncates(self, tmp_path, monkeypatch, capsys):
        # 2 rows shown of 10 matched: the heading must name the full total, not the
        # shown count — a silent "Found 2" would hide that --limit dropped eight.
        kb = _kb(tmp_path)
        client = FakeSearchClient([PREPRINT, ARTICLE], total=10)
        rc = _run(monkeypatch, ["zotero-search", "x", "--limit", "2", "--target", str(kb)], client)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Found 10 results, showing top 2:" in out

    def test_porcelain_found_is_the_total_not_the_shown_rows(self, tmp_path, monkeypatch, capsys):
        # The shared *-search contract: `found` is how many matched, so a consumer knows
        # it did not receive them all. Two result rows, but found\t10.
        kb = _kb(tmp_path)
        client = FakeSearchClient([PREPRINT, ARTICLE], total=10)
        rc = _run(monkeypatch, ["zotero-search", "x", "--limit", "2", "--porcelain",
                                "--target", str(kb)], client)
        lines = capsys.readouterr().out.splitlines()
        assert rc == 0
        assert len([ln for ln in lines if ln.startswith("result\t")]) == 2
        assert lines[-1] == "found\t10"

    def test_count_falls_back_to_shown_rows_when_total_is_none(self, tmp_path, monkeypatch, capsys):
        # No Total-Results header -> count is the shown row count, never below it.
        kb = _kb(tmp_path)
        client = FakeSearchClient([PREPRINT, ARTICLE], total=None)
        rc = _run(monkeypatch, ["zotero-search", "x", "--porcelain", "--target", str(kb)], client)
        assert rc == 0
        assert capsys.readouterr().out.splitlines()[-1] == "found\t2"


def _policy(kb, body):
    """Write the KB-scoped Zotero policy file the command will load.

    The ``[connection]`` header is not decoration: ``from_mapping`` reads that
    section only, so a top-level ``mode = ...`` parses fine and is then ignored.
    A test that dropped it would fall through to the built-in defaults — the
    user-level file is out of reach, since conftest's ``isolated_user_config``
    sandboxes ``$HOME``/``$XDG_CONFIG_HOME`` — and those defaults are local mode
    on port 23119, i.e. whatever Zotero is actually running on this machine.
    """
    (kb / "policy").mkdir(exist_ok=True)
    (kb / "policy" / "zotero-config.toml").write_text(body, encoding="utf-8")
    return kb


class TestPreconditionsDecideBeforeNarration:
    """`zotero-search`'s half of #625 — the same defect, a different string.

    The issue scoped this command out ("이 내레이션이 없어 해당 없음"), literally
    true of the words "Connecting to Zotero" and false of the behaviour: the line
    here reads "Searching Zotero (Local API)" and was printed before anything had
    checked whether the Local API was even the configured transport.

    These build the *real* client — `_make_zotero_client` is the seam under test,
    so patching it would test nothing. None of the three reaches the network.
    """

    def test_web_mode_never_claims_to_search_the_local_api(self, tmp_path, capsys):
        kb = _policy(_kb(tmp_path), '[connection]\nmode = "web"\n')
        rc = cli.main(["zotero-search", "x", "--target", str(kb)])
        cap = capsys.readouterr()
        assert rc == 1
        assert "Searching Zotero" not in cap.out
        assert "'web' mode is not supported" in cap.err

    def test_unsupported_local_port_never_claims_to_search(self, tmp_path, capsys):
        # 9999 is a valid TCP port, so it survives the config loader (an
        # out-of-range value would be clamped back to 23119 and check nothing)
        # and is rejected by `_connect()` as the unsupported port that it is.
        kb = _policy(_kb(tmp_path), '[connection]\nmode = "local"\nlocal_port = 9999\n')
        rc = cli.main(["zotero-search", "x", "--target", str(kb)])
        cap = capsys.readouterr()
        assert rc == 1
        assert "Searching Zotero" not in cap.out
        assert "local_port=9999 is not supported" in cap.err

    def test_missing_pyzotero_never_claims_to_search(self, tmp_path, monkeypatch, capsys):
        import sys

        # `None` in sys.modules is how the import system spells "this module is
        # not importable", so the guard fires whether or not pyzotero is really
        # installed in the running interpreter.
        kb = _policy(_kb(tmp_path), '[connection]\nmode = "local"\nlocal_port = 23119\n')
        monkeypatch.setitem(sys.modules, "pyzotero", None)
        rc = cli.main(["zotero-search", "x", "--target", str(kb)])
        cap = capsys.readouterr()
        assert rc == 1
        assert "Searching Zotero" not in cap.out
        assert "pyzotero is required" in cap.err
