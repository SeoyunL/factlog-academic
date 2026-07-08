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


def _pdf_att(key):
    return {"key": key, "data": {"key": key, "itemType": "attachment",
                                 "contentType": "application/pdf", "linkMode": "imported_url"}}


class FakeClient:
    def __init__(self, items=None, raise_exc=None, attachments=None, notes=None):
        self._items = items or []
        self._raise = raise_exc
        self._attachments = attachments or {}
        self._notes = notes or {}

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

    def get_pdf_attachments(self, item_key):
        return list(self._attachments.get(item_key, []))

    def fetch_file(self, key):
        return b"%PDF-1 fake"

    def get_notes(self, item_key):
        return list(self._notes.get(item_key, []))

    def get_annotations(self, attachment_key):
        return []


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

    def test_blank_collection_is_graceful_error(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        rc = _run(monkeypatch, ["zotero-import", "--collection", "   ", "--target", str(kb)], FakeClient([]))
        assert rc == 1
        assert "non-empty name" in capsys.readouterr().err

    def test_blank_tag_is_graceful_error(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        rc = _run(monkeypatch, ["zotero-import", "--tag", "", "--target", str(kb)], FakeClient([]))
        assert rc == 1
        assert "non-empty value" in capsys.readouterr().err

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


class TestDryRun:
    def test_dry_run_writes_no_files(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeClient([_item("K1", "One"), _item("K2", "Two")])
        rc = _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb), "--dry-run"], client)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Dry run: no files will be created." in out
        assert "Would import: 2" in out
        assert "Next step:" not in out  # no next-step on a dry run
        assert not list((kb / "sources").glob("*.md"))


class TestPorcelain:
    def test_porcelain_is_tab_separated_counts(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeClient([_item("K1", "One"), _item("", "NoKey")])
        rc = _run(monkeypatch, ["zotero-import", "--tag", "t", "--target", str(kb), "--porcelain"], client)
        out = capsys.readouterr().out
        assert rc == 1  # one error
        rows = dict(line.split("\t", 1) for line in out.splitlines() if "\t" in line)
        assert rows["imported"] == "1"
        assert rows["errors"] == "1"
        assert rows["dry_run"] == "0"
        assert rows["target"].endswith("sources")
        # No human narration leaked into porcelain output.
        assert "Connecting" not in out and "Summary" not in out

    def test_porcelain_dry_run_combo(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeClient([_item("K1", "One")])
        rc = _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb), "--porcelain", "--dry-run"], client)
        out = capsys.readouterr().out
        assert rc == 0
        lines = out.splitlines()
        counts = dict(line.split("\t", 1) for line in lines if line.split("\t", 1)[0] in
                      {"imported", "skipped", "errors", "dry_run", "target"})
        assert counts["imported"] == "1" and counts["dry_run"] == "1"
        # per-item plan row exposes the prospective filename.
        item_rows = [line for line in lines if line.startswith("item\t")]
        assert len(item_rows) == 1
        assert item_rows[0].split("\t")[1] == "imported"  # status
        assert item_rows[0].split("\t")[3].endswith(".md")  # would-be name
        assert not list((kb / "sources").glob("*.md"))

    def test_porcelain_connection_error_empty_stdout(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeClient(raise_exc=ZoteroConnectionError("not running"))
        rc = _run(monkeypatch, ["zotero-import", "--tag", "t", "--target", str(kb), "--porcelain"], client)
        cap = capsys.readouterr()
        assert rc == 2
        assert cap.out == ""  # porcelain stdout stays clean on hard error
        assert "not running" in cap.err

    def test_porcelain_empty_result_has_count_contract(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        rc = _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb), "--porcelain"], FakeClient([]))
        out = capsys.readouterr().out
        assert rc == 0
        rows = dict(line.split("\t", 1) for line in out.splitlines() if "\t" in line)
        assert rows == {"imported": "0", "skipped": "0", "errors": "0", "dry_run": "0",
                        "target": str(kb / "sources")}


class TestPdf:
    def test_pdf_registered_in_parser(self):
        args = cli.build_parser().parse_args(["zotero-import", "--collection", "X", "--pdf"])
        assert args.pdf is True

    def test_pdf_places_and_triggers_conversion(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        calls = []
        monkeypatch.setattr(cli, "_convert_placed_pdfs",
                            lambda target, paths, *, quiet: calls.append((list(paths), quiet)) or 0)
        client = FakeClient([_item("K1", "One")], attachments={"K1": [_pdf_att("A1")]})
        rc = _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb), "--pdf"], client)
        out = capsys.readouterr().out
        assert rc == 0
        assert "placed 1" in out
        assert "Converting PDFs to text" in out
        assert len(calls) == 1 and calls[0][1] is False  # invoked, not quiet
        assert [p.name for p in calls[0][0]] == [f"{list((kb / 'sources').glob('*.pdf'))[0].name}"]

    def test_pdf_porcelain_rows_and_quiet_conversion(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        calls = []
        monkeypatch.setattr(cli, "_convert_placed_pdfs",
                            lambda target, paths, *, quiet: calls.append(quiet) or 0)
        client = FakeClient([_item("K1", "One")], attachments={"K1": [_pdf_att("A1")]})
        rc = _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb), "--pdf", "--porcelain"], client)
        out = capsys.readouterr().out
        assert rc == 0
        rows = dict(line.split("\t", 1) for line in out.splitlines() if "\t" in line)
        assert rows["pdf_placed"] == "1" and rows["pdf_skipped"] == "0" and rows["pdf_errors"] == "0"
        assert calls == [True]  # conversion suppressed in porcelain

    def test_pdf_dry_run_no_conversion(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        called = []
        monkeypatch.setattr(cli, "_convert_placed_pdfs",
                            lambda target, paths, *, quiet: called.append(1) or 0)
        client = FakeClient([_item("K1")], attachments={"K1": [_pdf_att("A1")]})
        rc = _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb), "--pdf", "--dry-run"], client)
        assert rc == 0
        assert called == []  # no conversion on dry run
        assert not list((kb / "sources").glob("*.pdf"))

    def test_conversion_failure_makes_exit_nonzero(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        monkeypatch.setattr(cli, "_convert_placed_pdfs", lambda target, paths, *, quiet: 1)
        client = FakeClient([_item("K1")], attachments={"K1": [_pdf_att("A1")]})
        rc = _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb), "--pdf"], client)
        assert rc == 1

    def test_conversion_retried_for_already_placed_pdf(self, tmp_path, monkeypatch, capsys):
        # Re-run: bib skipped, PDF already present (skipped) -> conversion still
        # runs on that existing PDF (retry path), passing its path.
        kb = _kb(tmp_path)
        items = [_item("K1", "One")]
        atts = {"K1": [_pdf_att("A1")]}
        monkeypatch.setattr(cli, "_convert_placed_pdfs", lambda target, paths, *, quiet: 0)
        _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb), "--pdf"],
             FakeClient(items, attachments=atts))
        calls = []
        monkeypatch.setattr(cli, "_convert_placed_pdfs",
                            lambda target, paths, *, quiet: calls.append([p.name for p in paths]) or 0)
        _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb), "--pdf"],
             FakeClient(items, attachments=atts))
        assert len(calls) == 1 and len(calls[0]) == 1  # skipped PDF still handed to convert

    def test_convert_reentry_builds_ingest_args_and_porcelain_suppresses(self, tmp_path, monkeypatch, capsys):
        # Do NOT stub _convert_placed_pdfs: exercise the real build_parser/cmd_ingest
        # reentry and the porcelain redirect. Stub cmd_ingest to emit noise.
        kb = _kb(tmp_path)
        seen = {}

        def fake_ingest(args):
            seen["paths"] = list(args.paths)
            seen["scan"] = args.scan
            print("INGEST_NOISE_SHOULD_BE_SUPPRESSED")
            return 0

        monkeypatch.setattr(cli, "cmd_ingest", fake_ingest)
        client = FakeClient([_item("K1")], attachments={"K1": [_pdf_att("A1")]})
        rc = _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb), "--pdf", "--porcelain"], client)
        out = capsys.readouterr().out
        assert rc == 0
        assert "INGEST_NOISE_SHOULD_BE_SUPPRESSED" not in out  # redirected away in porcelain
        assert seen["scan"] is False and len(seen["paths"]) == 1  # explicit path, not --scan


def _note(html="<p>a note</p>"):
    return {"data": {"itemType": "note", "note": html}}


class TestAnnotations:
    def test_annotations_registered_in_parser(self):
        args = cli.build_parser().parse_args(["zotero-import", "--collection", "X", "--annotations"])
        assert args.annotations is True

    def test_annotations_written_and_reported(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeClient([_item("K1", "One")], notes={"K1": [_note("<p>my note</p>")]})
        rc = _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb), "--annotations"], client)
        out = capsys.readouterr().out
        assert rc == 0
        assert "written 1" in out
        assert list((kb / "sources").glob("*-notes.md"))

    def test_annotations_porcelain_rows(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeClient([_item("K1", "One")], notes={"K1": [_note()]})
        rc = _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb), "--annotations", "--porcelain"], client)
        out = capsys.readouterr().out
        assert rc == 0
        rows = dict(line.split("\t", 1) for line in out.splitlines() if "\t" in line)
        assert rows["annotations_written"] == "1"
        assert rows["annotations_skipped"] == "0"
        assert rows["annotation_errors"] == "0"

    def test_annotations_dry_run_no_file(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        client = FakeClient([_item("K1")], notes={"K1": [_note()]})
        rc = _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb), "--annotations", "--dry-run"], client)
        assert rc == 0
        assert not list((kb / "sources").glob("*-notes.md"))

    def test_annotation_error_makes_exit_nonzero(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)

        class Boom(FakeClient):
            def get_notes(self, item_key):
                raise ZoteroError("boom")

        rc = _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb), "--annotations"],
                  Boom([_item("K1")]))
        out = capsys.readouterr().out
        assert rc == 1
        assert "errors 1" in out


class TestDryRunSkip:
    def test_dry_run_would_skip_existing(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        items = [_item("K1", "One")]
        _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb)], FakeClient(items))
        rc = _run(monkeypatch, ["zotero-import", "--collection", "X", "--target", str(kb), "--dry-run"], FakeClient(items))
        out = capsys.readouterr().out
        assert rc == 0
        assert "would skip" in out
        assert "Would skip:  1" in out
