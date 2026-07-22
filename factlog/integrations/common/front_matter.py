# SPDX-License-Identifier: Apache-2.0
"""Read scalar fields out of a source file's YAML front matter.

Only the leading front-matter block (between the opening ``---`` and its closing
``---``) is consulted, and the read stops at that closing fence — so a plain user
source, or the literal text ``zotero_key:`` inside a body, is never mistaken for
a prior import.

A block whose closing fence is never found carries *nothing*. Its extent is
unknowable, so any key read out of it might be a body line. Locating the block —
and that rule — belongs to :mod:`factlog.front_matter_scan`, which
:mod:`factlog.bibtex` shares (#419); what is left here is the reading.

This is a *reader*, not a YAML parser: it recognises the ``key: value`` and
``key: "value"`` forms the source writers emit, which is all the de-duplication
index needs.
"""
from __future__ import annotations

import re
from pathlib import Path

from factlog.front_matter_scan import front_matter_block

# ``front_matter_block`` is imported to be *used* below, not re-exported: import it
# from :mod:`factlog.front_matter_scan`, which owns it. This documents the intended
# surface; it does not block an explicit import, which still resolves here (#419).
__all__ = ["read_first_author", "read_scalar", "read_scalars"]


def _key_pattern(key: str) -> re.Pattern[str]:
    return re.compile(rf'^{re.escape(key)}:\s*"?([^"\n]+?)"?\s*$', re.MULTILINE)


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


# Reverse of ``_textio.yaml_scalar``'s single-character escapes. ``\xNN`` is handled
# separately below because it consumes two further hex digits.
_YAML_UNESCAPE = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}


def _read_double_quoted(text: str, start: int) -> tuple[str, int]:
    """Decode one double-quoted YAML scalar beginning at ``text[start] == '"'``.

    Reverses the escaping ``_textio.yaml_scalar`` emits (``\\\\``, ``\\"``, ``\\n``,
    ``\\r``, ``\\t`` and ``\\xNN`` for other control chars). Returns the decoded
    value and the index just past the closing quote (or end of string if the quote
    is unterminated — a truncated front matter degrades to a best-effort value
    rather than raising, since this reader is tolerant of hand-edited files).
    """
    out: list[str] = []
    i = start + 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            return "".join(out), i + 1
        if ch == "\\" and i + 1 < n:
            esc = text[i + 1]
            if esc == "x" and i + 3 < n:
                try:
                    out.append(chr(int(text[i + 2:i + 4], 16)))
                    i += 4
                    continue
                except ValueError:
                    pass
            out.append(_YAML_UNESCAPE.get(esc, esc))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out), n


def _decode_scalar(raw: str) -> str:
    """Decode a possibly-quoted YAML scalar (the value after ``key:`` or a list item).

    Recognises the double-quoted form the writers emit and a bare unquoted token;
    a single-quoted form (which the writers never produce but a human might) is
    unwrapped with YAML's ``''`` -> ``'`` rule.
    """
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] == '"':
        return _read_double_quoted(raw, 0)[0]
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return raw


def _first_flow_item(text: str) -> str:
    """The first element of a ``[...]`` flow sequence, decoded. ``""`` when empty."""
    i = 1  # past the '['
    n = len(text)
    while i < n and text[i] in " \t":
        i += 1
    if i >= n or text[i] == "]":
        return ""
    if text[i] == '"':
        return _read_double_quoted(text, i)[0]
    if text[i] == "'":
        j = text.find("'", i + 1)
        end = j if j != -1 else n
        return _decode_scalar(text[i:end + 1])
    j = i
    while j < n and text[j] not in ",]":
        j += 1
    return _decode_scalar(text[i:j])


def read_first_author(path: Path, ignore_re: re.Pattern[str] | None = None) -> str:
    """The first author's raw name from ``path``'s front matter, or ``""``.

    ``authors`` is a YAML **list**, which :func:`read_scalars` cannot read — it is
    scalar-only and drops ``authors`` entirely, so a naive ``read_scalars`` reader
    silently yields no author and disables the title+author+year fallback for every
    paper (the biggest implementation surface of #75). This reader handles both
    serializations:

    * the flow form the writers emit, ``authors: ["Ada Lovelace", "Alan Turing"]``
      (via ``_textio.yaml_list``), decoding the double-quote escapes; and
    * a block form a hand-written or legacy file may carry::

          authors:
            - Ada Lovelace
            - Alan Turing

    A file with no ``authors`` key, an empty list, or no readable first item yields
    ``""`` — the matcher then fails closed (no surname, no match), which is the safe
    direction. It never raises on a malformed value.
    """
    block = front_matter_block(path)
    if block is None or (ignore_re is not None and ignore_re.search(block)):
        return ""
    match = re.search(r"^authors:[ \t]*(.*)$", block, re.MULTILINE)
    if match is None:
        return ""
    inline = match.group(1).strip()
    if inline.startswith("["):
        return _first_flow_item(inline)
    if inline:
        # A single-author scalar (``authors: "Ada Lovelace"``) — not what the writers
        # emit, but decode it rather than mistake it for an empty list.
        return _decode_scalar(inline)
    # Block form: the first ``- item`` line under the key.
    item = re.search(r"^[ \t]*-[ \t]*(.+?)[ \t]*$", block[match.end():], re.MULTILINE)
    return _decode_scalar(item.group(1)) if item else ""
