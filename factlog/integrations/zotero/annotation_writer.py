#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Format a Zotero item's highlights + notes into a ``sources/<stem>-notes.md``.

Phase 3 brings a researcher's PDF highlights and item notes into the KB as an
ordinary source, so the existing ``sync`` step (LLM extraction + the human accept
gate) turns them into candidate facts. The agent never writes candidates itself
(P1) — annotations are just richer source text.

The file pairs with the item's bibliographic ``<stem>.md`` by sharing the stem
and carries the same ``zotero_key`` in its front matter. Its content is a pure
function of the Zotero annotations/notes (no import timestamp), which lets it be
both idempotent and fresh:

* target absent            -> write it
* target is ours & same    -> skip (unchanged)
* target is ours & differs -> overwrite (a highlight was added/changed)
* target is NOT ours       -> skip (never clobber a user's own file — P4)

"ours" is detected by a ``source_kind: annotations`` line inside the front-matter
block (not anywhere in the body). Deciding that needs two things inside the
scanned head: we write the *marker* on the line right after the opening fence,
and we cap the emitted title so the *closing fence* cannot be pushed out of the
head by a long title. A file whose block does not close inside the head is never
ours, however early its marker appears — that still holds for hand-edited front
matter. Writes are atomic (temp+os.replace).
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from pathlib import Path

from factlog.integrations.zotero._textio import (
    ANNOTATION_MARKER_RE as _MARKER_LINE_RE,
)
from factlog.integrations.zotero._textio import (
    ANNOTATION_SOURCE_MARKER as _MARKER,
)
from factlog.integrations.zotero._textio import (
    atomic_write_text,
    yaml_scalar,
)

# Widening this window is not the safe direction it looks like. It would let a
# user file whose *own front matter* carries the marker line close inside the head
# and so be claimed as ours and overwritten — measured at 65536, a file with the
# fence at 4844 goes from "skipped" back to "updated". The narrow window costs an
# over-rejection for absurdly long front matter instead (see _not_ours_reason).
# Characters, not bytes: the head is read in text mode, so a multi-byte title is
# measured the same way render_annotations budgets for it.
_HEAD_SCAN_CHARS = 4096

# Appended to a title we had to cut so the cut is visible in the file itself.
_TRUNCATED_SUFFIX = "…"

_NOT_OURS = "target exists and is not a zotero notes file"
_UNTERMINATED = "target exists and its front matter does not close inside the scanned head"

# Strip script/style/comment *contents* (not just the tags) before removing tags.
_DROP_CONTENT_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>|<!--.*?-->")
_BR_RE = re.compile(r"(?i)<\s*br\s*/?>")
_BLOCK_CLOSE_RE = re.compile(r"(?i)</\s*(p|div|li|h[1-6]|tr)\s*>")
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class AnnotationResult:
    path: Path | None
    status: str  # "written" | "updated" | "skipped"
    reason: str = ""


def _clean(value: object) -> str:
    """Stripped string with C0 control chars removed (tabs/newlines kept)."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch for ch in value if ch in "\t\n" or ord(ch) >= 0x20 and ord(ch) != 0x7F)
    return cleaned.strip()


def html_to_text(value: object) -> str:
    """Flatten Zotero note HTML to plain text.

    Script/style/comment contents are dropped, block tags become line breaks, the
    remaining tags are stripped, entities are unescaped, and control characters
    are removed so nothing hostile leaks into the source file.
    """
    if not isinstance(value, str):
        return ""
    text = _DROP_CONTENT_RE.sub("", value)
    text = _BR_RE.sub("\n", text)
    text = _BLOCK_CLOSE_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = _html.unescape(text)
    lines = [_clean(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _ad(item: dict) -> dict:
    data = item.get("data") if isinstance(item, dict) else None
    return data if isinstance(data, dict) else {}


def _format_highlight(data: dict) -> str:
    """One highlight block, or "" when it carries no text at all."""
    text = _clean(data.get("annotationText"))
    comment = _clean(data.get("annotationComment"))
    if not text and not comment:
        return ""
    page = _clean(data.get("annotationPageLabel"))
    parts = [f"### p. {page}" if page else "###"]
    if text:
        # Quote the highlighted passage; a multi-line passage stays in the quote.
        parts.append("\n".join(f"> {line}" for line in text.splitlines()))
    if comment:
        parts.append(comment)
    return "\n\n".join(parts)


def _fit_scalar(value: str, budget: int) -> str:
    """Longest prefix of ``value`` whose *emitted* scalar stays within ``budget``.

    Counted on the emitted width, not the input length: yaml_scalar turns one
    backslash/quote/tab into two characters, so a cap on input characters would
    still let the front matter grow past the budget.
    """
    if len(yaml_scalar(value)) - 2 <= budget:
        return value
    budget -= len(yaml_scalar(_TRUNCATED_SUFFIX)) - 2  # the marker itself has to fit
    if budget < 0:
        return ""
    used = 0
    kept = 0
    for ch in value:
        width = len(yaml_scalar(ch)) - 2
        if used + width > budget:
            break
        used += width
        kept += 1
    return value[:kept] + _TRUNCATED_SUFFIX


def render_annotations(parsed_bib: dict, annotations: list[dict], notes: list[dict]) -> str:
    """The full markdown (front matter + body), or "" if there is nothing to write."""
    highlight_blocks = [b for b in (_format_highlight(_ad(a)) for a in annotations) if b]
    note_texts = [t for t in (html_to_text(_ad(n).get("note")) for n in notes) if t]
    if not highlight_blocks and not note_texts:
        return ""

    key = _clean(parsed_bib.get("zotero_key"))
    title = _clean(parsed_bib.get("title")) or "Untitled"
    # Marker first so it is always near the top of the scanned head. EVERY variable
    # length field is capped, not just the title: whatever is left uncapped becomes
    # the next thing that pushes the closing fence out of the head and makes us
    # disown our own file for good (#430). The budget is derived from the block we
    # are about to emit rather than hardcoded, so adding a field narrows what the
    # values may spend instead of silently reopening that cliff.
    skeleton = ["---", _MARKER, 'zotero_key: ""', 'title: ""', "imported_from: zotero"]
    budget = _HEAD_SCAN_CHARS - len("\n---") - len("\n".join(skeleton))
    # The key identifies the item, so it is served first and the title lives on
    # what remains. Both are for a human reading the file — nothing reads either
    # back (front_matter.read_scalars skips annotation sources by their marker).
    key = _fit_scalar(key, budget)
    title = _fit_scalar(title, budget - (len(yaml_scalar(key)) - 2))
    lines = ["---", _MARKER, f"zotero_key: {yaml_scalar(key)}"]
    lines.append(f"title: {yaml_scalar(title)}")
    lines.append("imported_from: zotero")
    lines.append("---")
    lines.append("")
    lines.append(f"# Annotations — {title}\n")

    if highlight_blocks:
        lines.append("## Highlights\n")
        lines.append("\n\n".join(highlight_blocks))
        lines.append("")
    if note_texts:
        lines.append("## Notes\n")
        lines.append("\n\n".join(note_texts))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _not_ours_reason(path: Path) -> str:
    """Empty if the file's front-matter block carries our marker, else why it does not.

    The two refusals are reported apart because they mean different things to
    whoever reads the skip. One is a file we can read and can tell is not ours.
    The other is a file whose ownership is undecidable: it may be ours with front
    matter hand-grown past the head, someone else's unterminated block, or a plain
    document that merely opens with ``---``. Reporting the second as "not a zotero
    notes file" asserted more than we know, and was false for our own files (#430).
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            head = fh.read(_HEAD_SCAN_CHARS)
    except OSError:
        return _NOT_OURS
    if not head.startswith("---"):
        return _NOT_OURS
    rest = head[3:]
    end = rest.find("\n---")
    if end == -1:
        # The closing fence is missing or past the scanned head, so we cannot tell
        # front matter from body. Claiming ownership here would let a marker line
        # in a user's *body* pass the overwrite gate and destroy their file. The
        # opposite error costs a silent skip, which is bad but recoverable. Not
        # knowing where the block ends means not ours. Our own writes stay clear of
        # this by capping every variable field (see render_annotations), so reaching
        # it now takes front matter someone lengthened by hand.
        return _UNTERMINATED
    block = rest[:end]
    return "" if _MARKER_LINE_RE.search(block) else _NOT_OURS


def write_annotations(
    parsed_bib: dict,
    annotations: list[dict],
    notes: list[dict],
    base_stem: str,
    target: Path | str,
    dry_run: bool = False,
) -> AnnotationResult:
    """Write ``sources/<base_stem>-notes.md`` from the item's highlights/notes.

    With ``dry_run`` the same decision (written/updated/skipped) is returned but no
    file is created — a "written"/"updated" outcome is what *would* happen.
    """
    if not _clean(parsed_bib.get("zotero_key")):
        return AnnotationResult(None, "skipped", "missing zotero_key")
    if not base_stem or "/" in base_stem or "\\" in base_stem or ".." in base_stem:
        return AnnotationResult(None, "skipped", "unsafe base stem")

    content = render_annotations(parsed_bib, annotations, notes)
    if not content:
        return AnnotationResult(None, "skipped", "no annotations or notes")

    sources_dir = Path(target) / "sources"
    path = sources_dir / f"{base_stem}-notes.md"

    if path.exists():
        refusal = _not_ours_reason(path)
        if refusal:
            return AnnotationResult(path, "skipped", refusal)
        try:
            if path.read_text(encoding="utf-8") == content:
                return AnnotationResult(path, "skipped", "unchanged")
        except OSError:
            pass  # unreadable -> fall through and rewrite
        if not dry_run:
            sources_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, content)
        return AnnotationResult(path, "updated")

    if not dry_run:
        sources_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, content)
    return AnnotationResult(path, "written")
