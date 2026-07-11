# SPDX-License-Identifier: Apache-2.0
"""Source-file converters for `factlog ingest`.

Fact extraction reads sources/ files as text, so binary/office formats (docx,
pdf, hwp/hwpx, pptx, ...) must be converted to text first. This module holds the
per-extension converter chains and the two built-in (in-process) converters for
formats no common PATH tool can read (hwpx, pptx) or that need orchestration
(hwp). ``factlog.cli`` drives them; the public surface is:

    INGEST_CONVERTERS  — {ext: [(tool, out_suffix, builder), ...]}
    BUILTIN_CONVERTERS — tool names that run in-process (skip the PATH check)
    INGEST_HINTS       — message for a recognised-but-unconvertible format
    INSTALL_HINTS      — install hint per PATH tool
    MissingTool        — raised by a built-in when a required external tool is absent
"""

from __future__ import annotations


def _conv_pandoc(src, dst) -> list[str]:
    return ["pandoc", str(src), "-t", "gfm", "--wrap=none", "-o", str(dst)]


def _conv_textutil(src, dst) -> list[str]:
    return ["textutil", "-convert", "txt", str(src), "-output", str(dst)]


def _conv_pdftotext(src, dst) -> list[str]:
    return ["pdftotext", "-layout", str(src), str(dst)]


def _conv_hwpx(src, dst) -> bool:
    """In-process converter for Hancom HWPX (OWPML: a zip of XML).

    pandoc/textutil/pdftotext cannot read hwpx, but the format is a zip whose
    Contents/section*.xml hold the body text as <hp:t> runs inside <hp:p>
    paragraphs. Extract per paragraph (inline tags stripped, entities
    unescaped), one line per non-empty paragraph, across all sections. Writes
    *dst* and returns True on success; a corrupt zip or empty extraction returns
    False (the caller reports a failure). Standard library only.
    """
    import html
    import re
    import zipfile

    try:
        with zipfile.ZipFile(src) as z:
            sections = sorted(
                n for n in z.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)
            )
            if not sections:
                return False
            lines: list[str] = []
            for name in sections:
                xml = z.read(name).decode("utf-8", "ignore")
                for para in re.split(r"<hp:p\b", xml):
                    # tolerate attributes on the run element (<hp:t charPrIDRef="..">);
                    # OWPML permits them, so a bare-tag-only match would silently drop text.
                    runs = re.findall(r"<hp:t\b[^>]*>(.*?)</hp:t>", para, flags=re.S)
                    if not runs:
                        continue
                    line = html.unescape("".join(re.sub(r"<[^>]+>", "", r) for r in runs)).strip()
                    if line:
                        lines.append(line)
    except (zipfile.BadZipFile, OSError, KeyError):
        return False
    if not lines:
        return False
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def _conv_pptx(src, dst) -> bool:
    """In-process converter for PowerPoint .pptx (OOXML: a zip of XML).

    pandoc can *write* pptx but cannot *read* it, so there is no PATH tool for
    this; the format is a zip whose ppt/slides/slideN.xml hold the slide text as
    <a:t> runs inside <a:p> paragraphs. Extract per paragraph (inline tags
    stripped, entities unescaped), one line per non-empty paragraph, slides in
    numeric order (slide10 after slide9, not lexicographic), each slide block
    separated by a blank line. Writes *dst* and returns True on success; a
    corrupt zip or empty extraction returns False (the caller reports a
    failure). Standard library only.

    Scope (deliberate, like the hwpx built-in): only *on-slide* text body is
    read — speaker notes (ppt/notesSlides/) are excluded. Table cells are each
    their own <a:p>, so a table flattens to one line per cell (row/column
    grouping is not preserved). The DrawingML element prefix is matched
    prefix-agnostically (<*:p>/<*:t>) so non-PowerPoint exporters that alias the
    namespace differently still extract.
    """
    import html
    import re
    import zipfile

    try:
        with zipfile.ZipFile(src) as z:
            slides = [n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
            # slideN.xml: order by the embedded number so slide10 follows slide9
            # (plain sort would place slide10 before slide2).
            slides.sort(key=lambda n: int(re.search(r"slide(\d+)", n).group(1)))
            if not slides:
                return False
            blocks: list[str] = []
            for name in slides:
                xml = z.read(name).decode("utf-8", "ignore")
                lines: list[str] = []
                # Split on the paragraph tag with any namespace prefix; \b after
                # ":p" keeps <*:pPr>/<*:prstGeom> from being treated as paragraphs.
                for para in re.split(r"<\w+:p\b", xml):
                    # tolerate attributes on the run element (<*:t ...>); strip
                    # inline tags inside the run, then unescape XML entities.
                    runs = re.findall(r"<\w+:t\b[^>]*>(.*?)</\w+:t>", para, flags=re.S)
                    if not runs:
                        continue
                    line = html.unescape("".join(re.sub(r"<[^>]+>", "", r) for r in runs)).strip()
                    if line:
                        lines.append(line)
                if lines:
                    blocks.append("\n".join(lines))
    except (zipfile.BadZipFile, OSError, KeyError):
        return False
    if not blocks:
        return False
    dst.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return True


class MissingTool(Exception):
    """Raised by a built-in converter when a required external tool is absent.

    Treated like a missing PATH converter: a soft skip under --scan, a failure
    when the file was named explicitly. Carries an install hint as its message.
    """


def _conv_hwp(src, dst) -> bool:
    """Convert a legacy Hancom .hwp (HWP 5.x, an OLE binary) to markdown.

    LibreOffice's HWP import filter only handles old HWP (<=3.x), so the
    soffice->PDF route fails on modern HWP 5.x files. Instead use pyhwp's
    `hwp5html` to extract structure-preserving HTML, then pandoc to markdown
    (tables survive). Raises MissingTool if hwp5html or pandoc is unavailable.
    """
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    if not shutil.which("hwp5html"):
        raise MissingTool("install pyhwp for .hwp support (`pip install pyhwp`); provides hwp5html")
    if not shutil.which("pandoc"):
        raise MissingTool("install pandoc for the HTML->markdown step (e.g. `brew install pandoc`)")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "html"
        h = subprocess.run(
            ["hwp5html", "--output", str(out), str(src)],
            capture_output=True, text=True, timeout=180,
        )
        index = out / "index.xhtml"
        if h.returncode != 0 or not index.is_file():
            return False
        p = subprocess.run(
            ["pandoc", str(index), "-t", "gfm", "--wrap=none", "-o", str(dst)],
            capture_output=True, text=True, timeout=180,
        )
    return p.returncode == 0 and dst.is_file() and dst.stat().st_size > 0


# Converters that run in-process (a Python callable writing dst) rather than by
# shelling out to a single PATH tool; they skip the shutil.which availability
# check (a built-in may itself orchestrate external tools and report MissingTool).
BUILTIN_CONVERTERS: frozenset[str] = frozenset({"factlog-hwpx", "factlog-hwp", "factlog-pptx"})

# Per-extension converter chains, tried in order until one's tool is available
# (on PATH, or always for a built-in). Each entry: (tool_name, output_suffix,
# builder) where builder is an argv-list builder for PATH tools, or a
# (src, dst) -> bool callable for built-ins.
# Formats whose BYTES are text but whose text is not the document: RTF control
# words, HTML tags. `--scan` sniffs content to avoid converting a mislabelled .pdf
# that is really plain text, and these tripped that sniff — so `--scan` skipped
# them forever. Since /factlog sync's first step IS --scan, their markup went into
# extraction as if it were prose, and the "no conversion" warning never fired
# because they were not classified as binary (#222). The sniff exists for formats
# that MUST be binary; these are not among them.
# Markup whose bytes are text but whose content is NOT prose. Extraction must never
# read the raw original -- .xhtml/.html/.htm/.rtf have a converter; .xml/.svg do not,
# so those are reported unconvertible (INGEST_HINTS) rather than fed as prose (#238).
TEXT_CONTAINER_EXTS: frozenset[str] = frozenset(
    {".html", ".htm", ".xhtml", ".rtf", ".xml", ".svg"}
)

INGEST_CONVERTERS: dict[str, list[tuple]] = {
    ".docx": [("pandoc", ".md", _conv_pandoc), ("textutil", ".txt", _conv_textutil)],
    ".odt": [("pandoc", ".md", _conv_pandoc), ("textutil", ".txt", _conv_textutil)],
    ".epub": [("pandoc", ".md", _conv_pandoc)],
    ".html": [("pandoc", ".md", _conv_pandoc), ("textutil", ".txt", _conv_textutil)],
    ".htm": [("pandoc", ".md", _conv_pandoc), ("textutil", ".txt", _conv_textutil)],
    # pandoc reads XHTML with its html reader (verified); textutil does not, so no fallback.
    ".xhtml": [("pandoc", ".md", _conv_pandoc)],
    # pandoc first, like every other entry, with textutil as the macOS fallback.
    # Order matters beyond preference: the chain picks the OUTPUT EXTENSION, so a
    # platform-dependent first choice makes the same original convert to .md on
    # Linux and .txt on macOS. Both land in runs/sources/, both get extracted, and
    # the same document then yields duplicate facts under two source refs. Putting
    # the portable converter first makes every platform converge on .md.
    # (textutil was first on the claim that it handles legacy code pages; measured
    # on a cp949 RTF it mangles them exactly as pandoc does, so the claim bought
    # nothing and cost the divergence.) With textutil alone, a Linux/Windows user
    # got a permanent "run factlog ingest --scan" warning for a command that could
    # never convert anything, and the RTF control words kept going into extraction
    # (#222).
    ".rtf": [("pandoc", ".md", _conv_pandoc), ("textutil", ".txt", _conv_textutil)],
    ".pdf": [("pdftotext", ".txt", _conv_pdftotext)],
    ".hwpx": [("factlog-hwpx", ".md", _conv_hwpx)],
    ".hwp": [("factlog-hwp", ".md", _conv_hwp)],
    ".pptx": [("factlog-pptx", ".md", _conv_pptx)],
}

# Formats recognised as needing conversion but with no bundled converter.
INGEST_HINTS: dict[str, str] = {
    ".xlsx": "no built-in converter; export sheets to .csv and place those in sources/",
    ".png": "images need OCR (out of scope); transcribe to text manually",
    ".jpg": "images need OCR (out of scope); transcribe to text manually",
    ".jpeg": "images need OCR (out of scope); transcribe to text manually",
    ".xml": "no general converter; extract the text you need into a .md/.txt in sources/",
    ".svg": "an SVG is XML markup, not prose; transcribe any caption/text into sources/",
}

INSTALL_HINTS: dict[str, str] = {
    "pandoc": "install pandoc (e.g. `brew install pandoc`, https://pandoc.org)",
    "pdftotext": "install poppler (e.g. `brew install poppler`)",
    "textutil": "textutil ships with macOS",
}
