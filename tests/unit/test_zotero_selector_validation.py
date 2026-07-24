# SPDX-License-Identifier: Apache-2.0
"""The three zotero-import selectors fail alike on a value the library lacks (#453).

`--collection` always resolved a name to a key, so a typo could not survive; the
other two selectors had no such forced lookup, and an unknown tag or item key
came back as an ordinary empty result — exit 0 on an import of nothing.

The distinction these tests pin is **absent vs. empty**: a selector the library
does not hold is an error (exit 1, with the available values named), while a
selector that exists and merely matches no bibliographic item stays a success
(exit 0), which is the behaviour scripts already depend on.

A fake backend stands in for pyzotero, so nothing here needs a running Zotero.
"""
from __future__ import annotations

import pytest

from factlog import cli
from factlog.integrations.zotero.api_client import (
    _SUGGEST_LIMIT,
    ZoteroClient,
    ZoteroConnectionError,
    ZoteroError,
)
from factlog.integrations.zotero.config import ZoteroConfig


def _item(key, title="T", tags=()):
    return {
        "key": key,
        "data": {
            "key": key,
            "itemType": "journalArticle",
            "title": title,
            "tags": [{"tag": t} for t in tags],
        },
    }


def _attachment(key):
    return {"key": key, "data": {"key": key, "itemType": "attachment", "title": "PDF"}}


def _col(name, key):
    return {"key": key, "data": {"key": key, "name": name}}


def _key_of(item) -> str:
    """How Zotero itself identifies an item: the key in ``data``, else the wrapper."""
    return item.get("data", {}).get("key") or item.get("key", "")


class FakeBackend:
    """pyzotero stand-in: an itemKey/tag-aware library with an explicit tag list."""

    def __init__(self, items=(), tags=None, tag_exc=None, collections=()):
        self._items = list(items)
        self._collections = list(collections)
        # Zotero's /tags is the union of the items' tags unless a test overrides it
        # (an unused tag, or a tag list that outlives the items carrying it).
        self._tags = list(tags) if tags is not None else [
            t["tag"] for i in self._items for t in i["data"].get("tags", [])
        ]
        self._tag_exc = tag_exc
        self.calls: list[tuple] = []

    def everything(self, page):
        return list(page)

    def tags(self):
        self.calls.append(("tags",))
        if self._tag_exc is not None:
            raise self._tag_exc
        return list(self._tags)

    def collections(self):
        self.calls.append(("collections",))
        return list(self._collections)

    def items(self, **kwargs):
        self.calls.append(("items", kwargs))
        if "itemKey" in kwargs:
            wanted = set(kwargs["itemKey"].split(","))
            return [i for i in self._items if _key_of(i) in wanted]
        if "tag" in kwargs:
            tag = kwargs["tag"]
            return [
                i for i in self._items
                if tag in [t["tag"] for t in i["data"].get("tags", [])]
            ]
        return list(self._items)

    def children(self, parent_key):
        return []


def _client(**kw):
    return ZoteroClient(ZoteroConfig(), backend=FakeBackend(**kw))


def _kb(tmp_path):
    (tmp_path / "sources").mkdir()
    return tmp_path


def _run(monkeypatch, argv, backend):
    client = ZoteroClient(ZoteroConfig(), backend=backend)
    monkeypatch.setattr(cli, "_make_zotero_client", lambda config: client)
    return cli.main(argv)


class TestListTags:
    def test_returns_library_tag_names(self):
        c = _client(items=[_item("A", tags=["alpha", "beta"])])
        assert c.list_tags() == ["alpha", "beta"]

    def test_accepts_raw_api_objects(self):
        # pyzotero yields plain strings; the raw Local API payload is {"tag": ...}.
        c = _client(tags=[{"tag": "alpha"}, "beta", {"nope": 1}, None])
        assert c.list_tags() == ["alpha", "beta"]

    def test_connection_failure_is_wrapped(self):
        c = _client(tag_exc=ConnectionError("simulated: Zotero not running"))
        with pytest.raises(ZoteroConnectionError, match="is Zotero running"):
            c.list_tags()


class TestTagResolution:
    def test_unknown_tag_errors_and_lists_available(self):
        c = _client(items=[_item("A", tags=["neurosymbolic AI", "to-review"])])
        with pytest.raises(ZoteroError, match=r"not found.*neurosymbolic AI, to-review"):
            c.get_items_by_tag("protein folding")

    def test_no_tags_at_all_says_none(self):
        c = _client(tags=[])
        with pytest.raises(ZoteroError, match=r"Available tags: \(none\)"):
            c.get_items_by_tag("anything")

    def test_known_but_empty_tag_returns_empty_without_error(self):
        # The tag exists (something else in the library carries it) but no
        # bibliographic item matches: an empty result, not a failure.
        c = _client(items=[_item("A", tags=["kept"])], tags=["kept", "unused"])
        assert c.get_items_by_tag("unused") == []

    def test_case_insensitive_fallback_queries_the_library_spelling(self):
        backend = FakeBackend(items=[_item("A", tags=["Neurosymbolic AI"])])
        out = ZoteroClient(ZoteroConfig(), backend=backend).get_items_by_tag("neurosymbolic ai")
        assert [i["key"] for i in out] == ["A"]
        assert ("items", {"tag": "Neurosymbolic AI"}) in backend.calls

    def test_case_ambiguous_errors_not_not_found(self):
        c = _client(tags=["Dup", "dup"])
        with pytest.raises(ZoteroError, match="ambiguous by case"):
            c.get_items_by_tag("DUP")

    def test_exact_match_wins_over_case_variant(self):
        backend = FakeBackend(items=[_item("A", tags=["dup"])], tags=["Dup", "dup"])
        out = ZoteroClient(ZoteroConfig(), backend=backend).get_items_by_tag("dup")
        assert [i["key"] for i in out] == ["A"]

    def test_blank_tag_rejected_before_any_request(self):
        backend = FakeBackend(tags=["a"])
        with pytest.raises(ZoteroError, match="non-empty string"):
            ZoteroClient(ZoteroConfig(), backend=backend).get_items_by_tag("   ")
        assert backend.calls == []

    def test_long_tag_list_is_capped(self):
        c = _client(tags=[f"t{i:03d}" for i in range(30)])
        with pytest.raises(ZoteroError, match=r"\.\.\. \(10 more\)") as ei:
            c.get_items_by_tag("nope")
        assert "t019" in str(ei.value) and "t020" not in str(ei.value)

    def test_exactly_at_the_cap_is_not_truncated(self):
        # The boundary itself: _SUGGEST_LIMIT names list in full, one more
        # truncates. With only a far-past-the-cap case, an off-by-one in the
        # comparison passes unnoticed.
        c = _client(tags=[f"t{i:03d}" for i in range(_SUGGEST_LIMIT)])
        with pytest.raises(ZoteroError) as ei:
            c.get_items_by_tag("nope")
        assert "more)" not in str(ei.value)
        assert f"t{_SUGGEST_LIMIT - 1:03d}" in str(ei.value)

    def test_one_past_the_cap_truncates(self):
        c = _client(tags=[f"t{i:03d}" for i in range(_SUGGEST_LIMIT + 1)])
        with pytest.raises(ZoteroError, match=r"\.\.\. \(1 more\)") as ei:
            c.get_items_by_tag("nope")
        assert f"t{_SUGGEST_LIMIT:03d}" not in str(ei.value)


class TestTagMetacharacters:
    """A tag whose name uses Zotero's tag-search metacharacters can't be a literal.

    The ``tag`` parameter is a search expression: a leading ``-`` negates and
    ``||`` is OR (measured on the Local API, port 23119, which has no documented
    escape). Such a name is rejected before any query runs, so #460's negation
    ("-draft" pulls in every item *without* that tag) can't fire — even when the
    tag really exists and #453's existence check would otherwise certify it.
    """

    def test_leading_hyphen_tag_is_rejected_even_when_it_exists(self):
        # "-draft" is a real library tag, so resolution's existence check would
        # pass; the metacharacter guard must reject it before that, and no
        # negated items(tag=...) query may reach the backend.
        backend = FakeBackend(items=[_item("A", tags=["-draft"])])
        with pytest.raises(ZoteroError, match="tag-search syntax"):
            ZoteroClient(ZoteroConfig(), backend=backend).get_items_by_tag("-draft")
        assert not any(c[0] == "items" for c in backend.calls)

    def test_leading_hyphen_tag_is_rejected_when_absent(self):
        backend = FakeBackend(items=[_item("A", tags=["kept"])])
        with pytest.raises(ZoteroError, match="tag-search syntax"):
            ZoteroClient(ZoteroConfig(), backend=backend).get_items_by_tag("-missing")
        assert not any(c[0] == "items" for c in backend.calls)

    def test_or_tag_is_rejected_even_when_it_exists(self):
        backend = FakeBackend(items=[_item("A", tags=["a||b"])])
        with pytest.raises(ZoteroError, match="tag-search syntax"):
            ZoteroClient(ZoteroConfig(), backend=backend).get_items_by_tag("a||b")
        assert not any(c[0] == "items" for c in backend.calls)

    def test_or_tag_is_rejected_when_absent(self):
        backend = FakeBackend(items=[_item("A", tags=["kept"])])
        with pytest.raises(ZoteroError, match="tag-search syntax"):
            ZoteroClient(ZoteroConfig(), backend=backend).get_items_by_tag("x||y")
        assert not any(c[0] == "items" for c in backend.calls)

    def test_error_names_the_syntax_collision(self):
        # The message must explain the cause (negation / OR), not just "invalid".
        c = _client(tags=["kept"])
        with pytest.raises(ZoteroError, match="negation and OR"):
            c.get_items_by_tag("-draft")

    def test_interior_hyphen_tag_is_still_a_literal_lookup(self):
        # Only a *leading* '-' is a metacharacter; an interior hyphen is part of
        # the name and must still resolve and query literally (no regression).
        backend = FakeBackend(items=[_item("A", tags=["Computer Science - Performance"])])
        out = ZoteroClient(ZoteroConfig(), backend=backend).get_items_by_tag(
            "Computer Science - Performance"
        )
        assert [i["key"] for i in out] == ["A"]
        assert ("items", {"tag": "Computer Science - Performance"}) in backend.calls

    def test_leading_whitespace_hyphen_tag_is_rejected_even_when_it_exists(self):
        # " -draft" exists literally, so resolution's existence check would pass
        # and hand the leading-space name to items(tag=...). Zotero trims the
        # space and reads "-draft" as a negation, so the guard must judge the
        # *stripped* value and reject before any query reaches the backend.
        backend = FakeBackend(items=[_item("A", tags=[" -draft"])])
        with pytest.raises(ZoteroError, match="tag-search syntax"):
            ZoteroClient(ZoteroConfig(), backend=backend).get_items_by_tag(" -draft")
        assert not any(c[0] == "items" for c in backend.calls)

    def test_leading_whitespace_hyphen_tag_is_rejected_when_absent(self):
        # The stripped-value guard fires before resolution, so neither the tag
        # list nor an items query is consulted for a space-then-hyphen name.
        backend = FakeBackend(items=[_item("A", tags=["kept"])])
        with pytest.raises(ZoteroError, match="tag-search syntax"):
            ZoteroClient(ZoteroConfig(), backend=backend).get_items_by_tag(" -missing")
        assert not any(c[0] in ("tags", "items") for c in backend.calls)

    def test_leading_tab_hyphen_tag_is_rejected(self):
        # strip() covers every whitespace kind, not just the space character.
        backend = FakeBackend(items=[_item("A", tags=["\t-x"])])
        with pytest.raises(ZoteroError, match="tag-search syntax"):
            ZoteroClient(ZoteroConfig(), backend=backend).get_items_by_tag("\t-x")
        assert not any(c[0] == "items" for c in backend.calls)

    def test_leading_whitespace_or_tag_is_rejected(self):
        # An OR expression stays a metacharacter after the leading space is
        # stripped, so " a||b" is rejected the same as "a||b".
        backend = FakeBackend(items=[_item("A", tags=[" a||b"])])
        with pytest.raises(ZoteroError, match="tag-search syntax"):
            ZoteroClient(ZoteroConfig(), backend=backend).get_items_by_tag(" a||b")
        assert not any(c[0] == "items" for c in backend.calls)

    def test_leading_whitespace_plain_tag_still_resolves_literally(self):
        # Stripping only *decides* whether the name is a metacharacter; a benign
        # leading space (" planning") is not one, so the tag must still resolve
        # and query by its literal, space-carrying name (no over-rejection).
        backend = FakeBackend(items=[_item("A", tags=[" planning"])])
        out = ZoteroClient(ZoteroConfig(), backend=backend).get_items_by_tag(" planning")
        assert [i["key"] for i in out] == ["A"]
        assert ("items", {"tag": " planning"}) in backend.calls


class TestCollectionSuggestionCap:
    """The shared _suggest() caps the collection listing too (a contract change)."""

    def test_short_collection_list_is_unabridged(self):
        c = _client(collections=[_col("Alpha", "K1"), _col("Beta", "K2")])
        with pytest.raises(ZoteroError, match=r"not found.*Alpha, Beta$"):
            c.get_items_by_collection("Gamma")

    def test_long_collection_list_is_capped(self):
        c = _client(collections=[_col(f"C{i:03d}", f"K{i}") for i in range(_SUGGEST_LIMIT + 5)])
        with pytest.raises(ZoteroError, match=r"\.\.\. \(5 more\)") as ei:
            c.get_items_by_collection("nope")
        assert f"C{_SUGGEST_LIMIT:03d}" not in str(ei.value)


class TestItemKeyResolution:
    def test_unresolved_key_is_named_in_the_error(self):
        c = _client(items=[_item("KH78JUPE")])
        with pytest.raises(ZoteroError, match="not found in Zotero: ZZZZZZZZ"):
            c.get_items_by_ids(["KH78JUPE", "ZZZZZZZZ"])

    def test_every_missing_key_is_listed_once_in_order(self):
        c = _client(items=[])
        with pytest.raises(ZoteroError, match=r"not found in Zotero: AAAA, BBBB$"):
            c.get_items_by_ids(["AAAA", "BBBB", "AAAA"])

    def test_resolved_keys_pass(self):
        c = _client(items=[_item("AAAA"), _item("BBBB")])
        assert [i["key"] for i in c.get_items_by_ids(["BBBB", "AAAA"])] == ["AAAA", "BBBB"]

    def test_resolved_but_non_bibliographic_key_is_not_reported_missing(self):
        # An attachment key resolves; it is filtered out as a non-source, and that
        # is an empty result rather than a "key not found" failure.
        c = _client(items=[_attachment("ATT1")])
        assert c.get_items_by_ids(["ATT1"]) == []

    def test_key_read_from_data_when_the_wrapper_has_none(self):
        # parse_item reads the key out of `data`, preferring it over the wrapper,
        # so resolution has to read it the same way: an item carrying its key only
        # in `data` did come back and must not be reported missing.
        c = _client(items=[{"data": {"key": "DATAONLY", "itemType": "journalArticle"}}])
        assert len(c.get_items_by_ids(["DATAONLY"])) == 1

    def test_key_read_from_the_wrapper_when_data_has_none(self):
        # The other half of the same fallback.
        c = _client(items=[{"key": "WRAPONLY", "data": {"itemType": "journalArticle"}}])
        assert len(c.get_items_by_ids(["WRAPONLY"])) == 1

    def test_data_key_wins_over_a_disagreeing_wrapper_key(self):
        # If the two placements ever disagree, the resolved key must be the one
        # the import files the source under (parse_item's), not the wrapper's.
        c = _client(items=[{"key": "WRAPPER", "data": {"key": "REAL", "itemType": "book"}}])
        assert len(c.get_items_by_ids(["REAL"])) == 1
        with pytest.raises(ZoteroError, match="not found in Zotero: WRAPPER"):
            c.get_items_by_ids(["WRAPPER"])

    def test_missing_key_detected_across_batches(self):
        keys = [f"K{i:03d}" for i in range(120)]
        c = _client(items=[_item(k) for k in keys if k != "K099"])
        with pytest.raises(ZoteroError, match="not found in Zotero: K099"):
            c.get_items_by_ids(keys)


class TestCliExitCodes:
    def test_unknown_tag_exits_1_and_explains(self, tmp_path, monkeypatch, capsys):
        backend = FakeBackend(items=[_item("A", tags=["neurosymbolic AI"])])
        rc = _run(
            monkeypatch,
            ["zotero-import", "--tag", "protein folding",
             "--target", str(_kb(tmp_path)), "--dry-run"],
            backend,
        )
        err = capsys.readouterr().err
        assert rc == 1
        assert "protein folding" in err and "neurosymbolic AI" in err

    def test_known_but_empty_tag_still_exits_0(self, tmp_path, monkeypatch, capsys):
        # Back-compat: "the tag exists and matched nothing" is a success.
        backend = FakeBackend(items=[_item("A", tags=["kept"])], tags=["kept", "unused"])
        rc = _run(
            monkeypatch,
            ["zotero-import", "--tag", "unused",
             "--target", str(_kb(tmp_path)), "--dry-run"],
            backend,
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert 'Found tag "unused": 0 item(s)' in out

    def test_unresolved_item_key_exits_1(self, tmp_path, monkeypatch, capsys):
        backend = FakeBackend(items=[_item("KH78JUPE")])
        rc = _run(
            monkeypatch,
            ["zotero-import", "--items", "KH78JUPE,ZZZZZZZZ",
             "--target", str(_kb(tmp_path)), "--dry-run"],
            backend,
        )
        err = capsys.readouterr().err
        assert rc == 1
        assert "ZZZZZZZZ" in err

    def test_resolved_item_keys_still_exit_0(self, tmp_path, monkeypatch, capsys):
        backend = FakeBackend(items=[_item("KH78JUPE")])
        rc = _run(
            monkeypatch,
            ["zotero-import", "--items", "KH78JUPE",
             "--target", str(_kb(tmp_path)), "--dry-run"],
            backend,
        )
        assert rc == 0
        assert "Would import: 1" in capsys.readouterr().out

    def test_porcelain_writes_no_rows_for_an_unknown_tag(self, tmp_path, monkeypatch, capsys):
        # The porcelain contract: a hard error leaves stdout empty and exits non-zero,
        # so a script cannot read "imported 0" off a typo.
        backend = FakeBackend(items=[_item("A", tags=["kept"])])
        rc = _run(
            monkeypatch,
            ["zotero-import", "--tag", "typo",
             "--target", str(_kb(tmp_path)), "--dry-run", "--porcelain"],
            backend,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.out == ""
        assert "typo" in captured.err

    def test_porcelain_still_reports_zero_for_a_known_empty_tag(
        self, tmp_path, monkeypatch, capsys
    ):
        backend = FakeBackend(items=[_item("A", tags=["kept"])], tags=["kept", "unused"])
        rc = _run(
            monkeypatch,
            ["zotero-import", "--tag", "unused",
             "--target", str(_kb(tmp_path)), "--dry-run", "--porcelain"],
            backend,
        )
        rows = dict(
            line.split("\t", 1) for line in capsys.readouterr().out.splitlines() if "\t" in line
        )
        assert rc == 0
        assert rows["imported"] == "0" and rows["errors"] == "0"
