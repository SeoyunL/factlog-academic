# SPDX-License-Identifier: Apache-2.0
"""Read scalar fields out of a source file's YAML front matter.

Only the leading front-matter block (between the opening ``---`` and its closing
``---``) is consulted, and the read stops at that closing fence — so a plain user
source, or the literal text ``zotero_key:`` inside a body, is never mistaken for
a prior import.

A block whose closing fence is never found carries *nothing*. Its extent is
unknowable, so any key read out of it might be a body line; see
:func:`front_matter_block`.

This is a *reader*, not a YAML parser: it recognises the ``key: value`` and
``key: "value"`` forms the source writers emit, which is all the de-duplication
index needs.
"""
from __future__ import annotations

import re
from pathlib import Path

# How much to pull per read while looking for the closing fence, and the point at
# which the search for one gives up. A well-formed block stops at its own fence,
# however long it is; the cap bounds the search on a file that has no closing fence
# to find.
#
# The cap is a *limit on the search*, not only on the pathological file: a block
# that is genuinely closed but longer than the cap is cut off before its fence is
# reached, and is then indistinguishable from an unclosed one — so it reads as no
# front matter at all. At ~40 bytes per author that needs roughly 26,000 authors in
# one ``authors:`` line, which no record approaches (the largest real
# collaborations run to a few thousand), so the cap buys bounded reads at a price
# nothing pays. Raising it costs only memory on malformed files.
#
# The chunk size is not a free performance knob — it is load-bearing twice over:
#
# * below 3 it breaks correctness outright. The opening-fence test runs on the
#   first read alone, so a 1- or 2-char chunk makes ``startswith("---")`` false
#   for a perfectly well-formed file and every source reads as empty.
# * it quantises the cap. The loop checks the length *before* reading, so the
#   effective ceiling is ``ceil(FRONT_MATTER_MAX_CHARS / chunk) * chunk``, and
#   changing the chunk moves where the search is actually cut. Powers of two that
#   divide the cap keep that boundary put; other values shift it.
FRONT_MATTER_CHUNK_CHARS = 8192
FRONT_MATTER_MAX_CHARS = 1 << 20


def _key_pattern(key: str) -> re.Pattern[str]:
    return re.compile(rf'^{re.escape(key)}:\s*"?([^"\n]+?)"?\s*$', re.MULTILINE)


def front_matter_block(path: Path) -> str | None:
    """The text between the opening ``---`` and the closing fence, or None.

    Reads to the block's **closing fence**, not to a fixed byte count. A fixed
    2048-byte window truncated the block mid-way and silently dropped every key
    past it: the arXiv writer emits one long ``authors:`` line ahead of ``year``/
    ``journal``/``imported_from``, so 50 authors (2104-byte block) already lost
    ``imported_from`` and 60 lost ``year`` and ``journal`` too, leaving
    ``arxiv_id``/``title`` alone (#409). The ID-keyed paths survived — the writers
    emit their identity keys first — but the title+author+year fallback, which
    needs ``year`` and ``imported_from``, did not.

    The window was never a read budget either: the read stops at the fence, so a
    well-formed source costs its front matter and nothing more, whatever the body
    weighs. Returning early when there is no opening fence keeps that true for the
    ingest conversions that carry an HTML provenance comment instead of YAML.

    Returns None for an unreadable file, one with no opening fence, **and one whose
    closing fence is never found**. That last case used to return everything read so
    far, which meant the body: a user's own note that opens with ``---`` and never
    closes it would hand its ``arxiv_id:``/``doi:``/``title:`` body lines to the caller as
    front matter, and the writers' caches would register them as that file's
    identity (``by_identity``/``by_cross_id``/``match_rows`` in
    ``common/source_writer``). An unrelated note then matches a real paper and the
    import is skipped or paired wrongly. Reading to the cap rather than to 2048
    bytes widened how much body could be absorbed, but the defect predates that —
    the fixed window only made it smaller, and where the offending line sat in the
    body decided whether it appeared at all.

    So an unclosed block yields nothing: its extent is unknowable, and a key read
    out of it cannot be told from a body line. The cost is the other direction —
    a genuinely tool-written source whose fence a human deleted now reads as "not
    imported", so re-importing writes a second ``.md`` instead of updating the
    first — measured, not inferred: an import that reports ``skipped`` against the
    intact source reports ``imported`` once the closing fence is deleted, leaving a
    ``…-2.md`` beside it. That is a visible, recoverable duplicate; silently binding
    a stranger's note to a paper's identity is neither, which is why the trade is
    taken in this direction. Restoring the old value for such a file is not the goal — the old
    reader answered from where the offending line happened to sit, and an answer
    decided by an offset is the defect, not the behaviour to preserve.

    The tempting middle road — trust an unclosed block only up to where the body
    seems to start — is the one thing not to do here. This is a reader, not a YAML
    parser, and guessing where front matter ends reintroduces exactly the defect
    above under a different knob.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            head = fh.read(FRONT_MATTER_CHUNK_CHARS)
            if not head.startswith("---"):
                # No opening fence: nothing to find, and no reason to read the body.
                return None
            # Stop as soon as the accumulated text holds a fence. Scanning the
            # accumulation rather than the latest chunk is a *read budget*, not a
            # correctness property: the extraction below searches all of ``head``
            # either way, so missing a straddling fence here would not lose it —
            # it would just keep reading to the cap. On a file with a large body
            # that is the difference between two chunks and a megabyte.
            while "\n---" not in head[3:] and len(head) < FRONT_MATTER_MAX_CHARS:
                chunk = fh.read(FRONT_MATTER_CHUNK_CHARS)
                if not chunk:
                    break
                head += chunk
    except (OSError, UnicodeDecodeError):
        # Undecodable bytes are "no front matter", like an unreadable file. Reading
        # further than the old window put more of a file's bytes through the codec,
        # so a source that is valid UTF-8 in its head and mojibake in its body used
        # to decode and now raises — at the caller, which does not expect it.
        return None
    rest = head[3:]
    end = rest.find("\n---")
    return None if end == -1 else rest[:end]


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
