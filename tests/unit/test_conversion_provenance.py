# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ingest-conversion provenance helpers (#214, #229).

conversion_origin() must reduce both header formats — a legacy bare basename
and the #214 sources/-relative path — to the same basename, so every
basename-keyed consumer (paired_conversion, eject) is unaffected by the change.

conversion_body_is_empty() must flag a factlog conversion that has only its
provenance header (a scanned/image PDF -> silent 0-facts, #229) while never
flagging a plain source or a hand-placed file.
"""
from __future__ import annotations

import common


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


class TestConversionOrigin:
    def test_legacy_basename_header(self, tmp_path):
        # Pre-#214 header recorded the bare basename.
        conv = _write(
            tmp_path / "data.hwpx.md",
            "<!-- ingested-by-factlog | source: data.hwpx | converter: x | date: y -->\n\nbody\n",
        )
        assert common.conversion_origin(conv) == "data.hwpx"

    def test_relative_path_header_reduces_to_basename(self, tmp_path):
        # #214: a sources/-relative header still yields the basename, so legacy
        # basename-comparing consumers keep working unchanged.
        conv = _write(
            tmp_path / "data.hwpx.md",
            "<!-- ingested-by-factlog | source: sub_a/data.hwpx | converter: x | date: y -->\n\nbody\n",
        )
        assert common.conversion_origin(conv) == "data.hwpx"

    def test_deeply_nested_relative_path(self, tmp_path):
        conv = _write(
            tmp_path / "x.pdf.txt",
            "[ingested-by-factlog] source: a/b/c/report.pdf | converter: x | date: y\n\nbody\n",
        )
        assert common.conversion_origin(conv) == "report.pdf"

    def test_no_header_returns_none(self, tmp_path):
        conv = _write(tmp_path / "hand.md", "just some text, no header\n")
        assert common.conversion_origin(conv) is None

    def test_empty_source_returns_none(self, tmp_path):
        conv = _write(
            tmp_path / "x.md",
            "<!-- ingested-by-factlog | source:  | converter: x | date: y -->\n\nbody\n",
        )
        assert common.conversion_origin(conv) is None


class TestConversionBodyIsEmpty:
    def test_header_only_is_empty(self, tmp_path):
        # A scanned PDF converts to header-only output (#229).
        conv = _write(
            tmp_path / "scan.pdf.txt",
            "[ingested-by-factlog] source: scan.pdf | converter: pdftotext | date: y\n\n",
        )
        assert common.conversion_body_is_empty(conv) is True

    def test_header_plus_whitespace_is_empty(self, tmp_path):
        conv = _write(
            tmp_path / "scan.pdf.txt",
            "[ingested-by-factlog] source: scan.pdf | converter: pdftotext | date: y\n\n  \n\n",
        )
        assert common.conversion_body_is_empty(conv) is True

    def test_header_with_text_is_not_empty(self, tmp_path):
        conv = _write(
            tmp_path / "doc.pdf.txt",
            "[ingested-by-factlog] source: doc.pdf | converter: pdftotext | date: y\n\nHello world\n",
        )
        assert common.conversion_body_is_empty(conv) is False

    def test_plain_source_without_header_is_not_flagged(self, tmp_path):
        # A blank *plain* source (no factlog header) is not an ingest conversion,
        # so this helper must not judge it as "converted-but-empty".
        conv = _write(tmp_path / "notes.md", "   \n")
        assert common.conversion_body_is_empty(conv) is False

    def test_missing_file_is_not_empty(self, tmp_path):
        assert common.conversion_body_is_empty(tmp_path / "nope.md") is False


class TestConversionConverter:
    def test_reads_the_markdown_header(self, tmp_path):
        conv = _write(
            tmp_path / "page.html.md",
            "<!-- ingested-by-factlog | source: page.html | converter: pandoc | date: y -->\n\n",
        )
        assert common.conversion_converter(conv) == "pandoc"

    def test_reads_the_plain_text_header(self, tmp_path):
        conv = _write(
            tmp_path / "scan.pdf.txt",
            "[ingested-by-factlog] source: scan.pdf | converter: pdftotext | date: y\n\n",
        )
        assert common.conversion_converter(conv) == "pdftotext"

    def test_trailing_field_without_date(self, tmp_path):
        # The `converter:` field can be last in the header; the comment closer
        # must not be swallowed into the tool name.
        conv = _write(
            tmp_path / "report.pdf.md",
            "<!-- ingested-by-factlog | source: report.pdf | converter: pandoc -->\n\nText\n",
        )
        assert common.conversion_converter(conv) == "pandoc"

    def test_header_without_converter_field_is_unknown(self, tmp_path):
        # A header predating the field: unknown, never guessed.
        conv = _write(
            tmp_path / "old.md",
            "<!-- ingested-by-factlog | source: old.pdf -->\n\nText\n",
        )
        assert common.conversion_converter(conv) is None

    def test_non_conversion_is_unknown(self, tmp_path):
        conv = _write(tmp_path / "notes.md", "converter: pdftotext\n\nhand-written\n")
        assert common.conversion_converter(conv) is None

    def test_missing_file_is_unknown(self, tmp_path):
        assert common.conversion_converter(tmp_path / "nope.md") is None


class TestEmptyConversionHint:
    def test_pdftotext_keeps_the_ocr_next_step(self):
        # pdftotext reads a PDF's text layer; a scan has none, so OCR is a real
        # next step and #229's original wording stays for this converter (#620).
        hint = common.empty_conversion_hint("pdftotext")
        assert "OCR" in hint
        assert "scanned" in hint

    def test_markup_converters_do_not_instruct_ocr(self):
        # pandoc/textutil produce an empty conversion when the markup held no
        # text. OCR recovers nothing there, so it must not be named (#620).
        for tool in ("pandoc", "textutil"):
            hint = common.empty_conversion_hint(tool)
            assert "OCR" not in hint
            assert "scanned" not in hint
            assert hint == "no extractable text in the original"

    def test_unknown_converter_gets_the_neutral_wording(self):
        # A conversion whose header names no converter must not be asserted to
        # be a scan — err toward the wording that is true of every converter.
        assert common.empty_conversion_hint(None) == "no extractable text in the original"

    def test_aggregate_wording_holds_for_every_converter(self):
        # The mixed-bucket counter in `status` / the ingest summary may only say
        # what every member's per-item hint also says.
        for tool in ("pdftotext", "pandoc", "textutil", None):
            assert common.EMPTY_CONVERSION_AGGREGATE in common.empty_conversion_hint(tool)
