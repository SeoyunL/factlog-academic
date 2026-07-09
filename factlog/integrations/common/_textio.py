# SPDX-License-Identifier: Apache-2.0
"""Text/IO helpers shared by every source writer.

factlog never machine-parses a source's front matter — sources are read as text
by the extraction step — so a minimal hand-rolled YAML emitter (no PyYAML
dependency) is sufficient; it double-quotes every string with the escapes YAML
needs.
"""
from __future__ import annotations

import os
from pathlib import Path

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


def yaml_list(items) -> str:
    """A flow-style YAML sequence of double-quoted scalars."""
    return "[" + ", ".join(yaml_scalar(item) for item in items) + "]"


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
