# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the extracted ingest converter module (#108)."""
from __future__ import annotations

import io
import zipfile

from factlog import ingest


class TestRegistry:
    def test_every_chain_entry_is_well_formed(self):
        for ext, chain in ingest.INGEST_CONVERTERS.items():
            assert ext.startswith(".")
            for tool, out_suffix, builder in chain:
                assert isinstance(tool, str) and tool
                assert out_suffix in (".md", ".txt")
                assert callable(builder)

    def test_builtins_are_registered_first_for_their_format(self):
        for ext in (".hwpx", ".hwp", ".pptx"):
            tool = ingest.INGEST_CONVERTERS[ext][0][0]
            assert tool in ingest.BUILTIN_CONVERTERS

    def test_hint_formats_have_no_converter(self):
        for ext in ingest.INGEST_HINTS:
            assert ext not in ingest.INGEST_CONVERTERS


class TestArgvBuilders:
    def test_pandoc_argv(self):
        argv = ingest._conv_pandoc("a.docx", "b.md")
        assert argv[0] == "pandoc" and "a.docx" in argv and "b.md" in argv

    def test_pdftotext_argv(self):
        argv = ingest._conv_pdftotext("a.pdf", "b.txt")
        assert argv[0] == "pdftotext" and argv[-1] == "b.txt"


class TestPptxBuiltin:
    def _pptx(self, tmp_path, *slide_texts):
        path = tmp_path / "deck.pptx"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for i, text in enumerate(slide_texts, start=1):
                xml = f'<p:sld><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:sld>'
                z.writestr(f"ppt/slides/slide{i}.xml", xml)
        path.write_bytes(buf.getvalue())
        return path

    def test_extracts_slide_text(self, tmp_path):
        src = self._pptx(tmp_path, "Hello", "World")
        dst = tmp_path / "out.md"
        assert ingest._conv_pptx(src, dst) is True
        body = dst.read_text(encoding="utf-8")
        assert "Hello" in body and "World" in body

    def test_corrupt_zip_returns_false(self, tmp_path):
        bad = tmp_path / "bad.pptx"
        bad.write_bytes(b"not a zip")
        assert ingest._conv_pptx(bad, tmp_path / "out.md") is False
