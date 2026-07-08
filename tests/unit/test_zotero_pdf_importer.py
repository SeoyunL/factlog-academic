# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Zotero PDF placement module (phase 2, #20).

A fake client supplies attachments and bytes so placement is exercised against a
temp KB without network. Covers deterministic filenames, idempotent skip, P4
non-overwrite, partial failure, dry-run, and atomic bytes.
"""
from __future__ import annotations

from factlog.integrations.zotero.api_client import ZoteroError
from factlog.integrations.zotero.pdf_importer import PdfOutcome, pdf_filename, place_pdfs


def _att(key):
    return {"key": key, "data": {"key": key, "itemType": "attachment",
                                 "contentType": "application/pdf", "linkMode": "imported_url"}}


class FakeClient:
    def __init__(self, attachments=None, files=None, fetch_error=None):
        self._attachments = attachments or []
        self._files = files or {}
        self._fetch_error = fetch_error
        self.fetched: list[str] = []

    def get_pdf_attachments(self, item_key):
        return list(self._attachments)

    def fetch_file(self, key):
        self.fetched.append(key)
        if self._fetch_error is not None and key in self._fetch_error:
            raise self._fetch_error[key]
        return self._files.get(key, b"%PDF-1.7 fake")


def _sources(tmp_path):
    return tmp_path / "sources"


class TestFilename:
    def test_name_uses_stem_and_attkey(self):
        assert pdf_filename("faronius-2025-x", "AKEY") == "faronius-2025-x-AKEY.pdf"

    def test_name_independent_of_sibling_count(self):
        # Same attachment -> same name whether or not the item has other PDFs.
        assert pdf_filename("s", "A1") == "s-A1.pdf"


class TestPlace:
    def test_single_pdf_placed(self, tmp_path):
        client = FakeClient([_att("A1")], files={"A1": b"%PDF-1 one"})
        out = place_pdfs(client, item_key="I", base_stem="paper-2025", target=tmp_path)
        assert [o.status for o in out] == ["placed"]
        p = _sources(tmp_path) / "paper-2025-A1.pdf"
        assert p.read_bytes() == b"%PDF-1 one"
        assert out[0].path == p

    def test_multiple_pdfs_disambiguated(self, tmp_path):
        client = FakeClient([_att("A1"), _att("A2")], files={"A1": b"a", "A2": b"b"})
        out = place_pdfs(client, item_key="I", base_stem="s", target=tmp_path)
        names = sorted(o.path.name for o in out)
        assert names == ["s-A1.pdf", "s-A2.pdf"]

    def test_name_stable_when_sibling_added(self, tmp_path):
        # First run: item has one PDF. Second run: a sibling PDF was added.
        # The original attachment must keep its name and be skipped (no orphan,
        # no re-download).
        place_pdfs(FakeClient([_att("A1")], files={"A1": b"x"}),
                   item_key="I", base_stem="s", target=tmp_path)
        client2 = FakeClient([_att("A1"), _att("A2")], files={"A1": b"x", "A2": b"y"})
        out = place_pdfs(client2, item_key="I", base_stem="s", target=tmp_path)
        by_key = {o.attachment_key: o for o in out}
        assert by_key["A1"].status == "skipped"  # unchanged, not re-fetched
        assert "A1" not in client2.fetched
        assert by_key["A2"].status == "placed"
        assert {p.name for p in _sources(tmp_path).glob("*.pdf")} == {"s-A1.pdf", "s-A2.pdf"}

    def test_idempotent_skip_existing(self, tmp_path):
        client = FakeClient([_att("A1")], files={"A1": b"x"})
        place_pdfs(client, item_key="I", base_stem="s", target=tmp_path)
        client2 = FakeClient([_att("A1")], files={"A1": b"x"})
        out = place_pdfs(client2, item_key="I", base_stem="s", target=tmp_path)
        assert [o.status for o in out] == ["skipped"]
        assert client2.fetched == []  # no re-download

    def test_never_overwrites_existing_file(self, tmp_path):
        sources = _sources(tmp_path)
        sources.mkdir()
        squatter = sources / "s-A1.pdf"
        squatter.write_bytes(b"USER OWNED")
        client = FakeClient([_att("A1")], files={"A1": b"NEW"})
        out = place_pdfs(client, item_key="I", base_stem="s", target=tmp_path)
        assert out[0].status == "skipped"
        assert squatter.read_bytes() == b"USER OWNED"

    def test_partial_existing_among_multiple(self, tmp_path):
        # A2 already placed, A1 new -> A1 placed, A2 skipped.
        sources = _sources(tmp_path)
        sources.mkdir()
        (sources / "s-A2.pdf").write_bytes(b"old")
        client = FakeClient([_att("A1"), _att("A2")], files={"A1": b"new", "A2": b"x"})
        out = place_pdfs(client, item_key="I", base_stem="s", target=tmp_path)
        by_key = {o.attachment_key: o for o in out}
        assert by_key["A1"].status == "placed" and by_key["A2"].status == "skipped"
        assert client.fetched == ["A1"]

    def test_invalid_attachment_key_is_error(self, tmp_path):
        bad = {"key": "", "data": {"itemType": "attachment", "contentType": "application/pdf"}}
        out = place_pdfs(FakeClient([bad]), item_key="I", base_stem="s", target=tmp_path)
        assert out[0].status == "error" and out[0].path is None
        assert not _sources(tmp_path).exists()

    def test_non_bytes_payload_is_error_not_crash(self, tmp_path):
        client = FakeClient([_att("A1")], files={"A1": None})
        out = place_pdfs(client, item_key="I", base_stem="s", target=tmp_path)
        assert out[0].status == "error" and "bytes" in out[0].reason

    def test_unclassified_exception_is_isolated(self, tmp_path):
        client = FakeClient([_att("A1"), _att("A2")],
                            files={"A2": b"ok"}, fetch_error={"A1": ValueError("weird")})
        out = place_pdfs(client, item_key="I", base_stem="s", target=tmp_path)
        by_key = {o.attachment_key: o for o in out}
        assert by_key["A1"].status == "error" and by_key["A2"].status == "placed"

    def test_partial_failure_continues(self, tmp_path):
        client = FakeClient(
            [_att("A1"), _att("A2")],
            files={"A2": b"ok"},
            fetch_error={"A1": ZoteroError("boom")},
        )
        out = place_pdfs(client, item_key="I", base_stem="s", target=tmp_path)
        by_key = {o.attachment_key: o for o in out}
        assert by_key["A1"].status == "error" and "boom" in by_key["A1"].reason
        assert by_key["A2"].status == "placed"
        assert (_sources(tmp_path) / "s-A2.pdf").exists()

    def test_dry_run_writes_nothing(self, tmp_path):
        client = FakeClient([_att("A1")], files={"A1": b"x"})
        out = place_pdfs(client, item_key="I", base_stem="s", target=tmp_path, dry_run=True)
        assert out[0].status == "placed" and out[0].reason == "would place"
        assert client.fetched == []
        assert not _sources(tmp_path).exists() or not list(_sources(tmp_path).glob("*.pdf"))

    def test_dry_run_reports_existing_as_skipped(self, tmp_path):
        sources = _sources(tmp_path)
        sources.mkdir()
        (sources / "s-A1.pdf").write_bytes(b"old")
        out = place_pdfs(FakeClient([_att("A1")]), item_key="I", base_stem="s",
                         target=tmp_path, dry_run=True)
        assert out[0].status == "skipped"

    def test_no_attachments_is_empty(self, tmp_path):
        assert place_pdfs(FakeClient([]), item_key="I", base_stem="s", target=tmp_path) == []

    def test_returns_pdf_outcome_type(self, tmp_path):
        out = place_pdfs(FakeClient([_att("A1")]), item_key="I", base_stem="s", target=tmp_path)
        assert isinstance(out[0], PdfOutcome)
