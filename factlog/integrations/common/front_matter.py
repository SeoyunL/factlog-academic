# SPDX-License-Identifier: Apache-2.0
"""Read scalar fields out of a source file's YAML front matter.

Only the leading front-matter block (between the opening ``---`` and its closing
``---``) is consulted, and only the first bytes are read — so a plain user
source, or the literal text ``zotero_key:`` inside a body, is never mistaken for
a prior import.

This is a *reader*, not a YAML parser: it recognises the ``key: value`` and
``key: "value"`` forms the source writers emit, which is all the de-duplication
index needs.
"""
from __future__ import annotations

import re
from pathlib import Path

# How many leading bytes of a source file to scan for its front matter.
FRONT_MATTER_SCAN_BYTES = 2048


def _key_pattern(key: str) -> re.Pattern[str]:
    return re.compile(rf'^{re.escape(key)}:\s*"?([^"\n]+?)"?\s*$', re.MULTILINE)


def front_matter_block(path: Path) -> str | None:
    """The text between the opening ``---`` and the closing fence, or None.

    Returns None for an unreadable file or one with no opening fence.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            head = fh.read(FRONT_MATTER_SCAN_BYTES)
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    rest = head[3:]
    end = rest.find("\n---")
    return rest if end == -1 else rest[:end]


def read_scalars(path: Path, keys, ignore_re: re.Pattern[str] | None = None) -> dict[str, str]:
    """Map each of ``keys`` found in ``path``'s front matter to its value.

    Absent keys are omitted. When ``ignore_re`` matches anywhere in the block the
    file is treated as carrying nothing — callers use it to skip companion files
    (e.g. Zotero's ``<stem>-notes.md`` annotation sources), which would otherwise
    be picked as the existing source for their parent item.
    """
    block = front_matter_block(path)
    if block is None or (ignore_re is not None and ignore_re.search(block)):
        return {}

    found: dict[str, str] = {}
    for key in keys:
        match = _key_pattern(key).search(block)
        if match:
            value = match.group(1).strip()
            if value:
                found[key] = value
    return found


def read_scalar(path: Path, key: str, ignore_re: re.Pattern[str] | None = None) -> str:
    """The single front-matter value for ``key``, or ""."""
    return read_scalars(path, (key,), ignore_re).get(key, "")
