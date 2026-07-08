# SPDX-License-Identifier: Apache-2.0
"""Shared text/IO helpers for the Zotero source writers.

Keeps the YAML scalar escaper and the atomic text write in one place so the
bibliographic writer and the annotation writer share one implementation instead
of copying it (or importing each other's private symbols).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Front-matter marker identifying a companion annotation source (<stem>-notes.md).
# Shared so the annotation writer (which stamps it) and the bibliographic writer
# (which must ignore such files when de-duplicating by zotero_key) agree, and so
# both match it line-anchored — a title/tag that merely contains the text is not
# mistaken for the marker.
ANNOTATION_SOURCE_MARKER = "source_kind: annotations"
ANNOTATION_MARKER_RE = re.compile(r"^source_kind:\s*annotations\s*$", re.MULTILINE)

# Backslash/quote plus the whitespace controls a double-quoted YAML scalar needs.
_YAML_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def yaml_scalar(value: str) -> str:
    """Double-quote a scalar, escaping backslash/quote/newline/tab and any other
    C0 control char as \\xNN — so an embedded newline/control cannot break the
    front matter onto a stray line."""
    out = []
    for ch in value:
        if ch in _YAML_ESCAPES:
            out.append(_YAML_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def atomic_write_text(path: Path, text: str) -> None:
    """Write text via a temp file + atomic replace; the temp is unlinked on a
    failed replace so no stray file lingers."""
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
