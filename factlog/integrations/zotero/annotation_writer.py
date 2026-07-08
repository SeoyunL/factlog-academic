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
block (not anywhere in the body), placed at the top so a long title cannot push
it out of the scanned head. Writes are atomic (temp + os.replace).
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from pathlib import Path

from factlog.integrations.zotero._textio import atomic_write_text, yaml_scalar

_MARKER = "source_kind: annotations"
_MARKER_LINE_RE = re.compile(r"^source_kind:\s*annotations\s*$", re.MULTILINE)
_HEAD_SCAN_BYTES = 4096

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


def render_annotations(parsed_bib: dict, annotations: list[dict], notes: list[dict]) -> str:
    """The full markdown (front matter + body), or "" if there is nothing to write."""
    highlight_blocks = [b for b in (_format_highlight(_ad(a)) for a in annotations) if b]
    note_texts = [t for t in (html_to_text(_ad(n).get("note")) for n in notes) if t]
    if not highlight_blocks and not note_texts:
        return ""

    title = _clean(parsed_bib.get("title")) or "Untitled"
    # Marker first so it is always near the top of the scanned head.
    lines = ["---", _MARKER]
    lines.append(f"zotero_key: {yaml_scalar(_clean(parsed_bib.get('zotero_key')))}")
    lines.append(f"title: {yaml_scalar(title)}")
    lines.append("imported_from: zotero")
    lines.append("---\n")
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


def _is_ours(path: Path) -> bool:
    """True only if the file's front-matter block carries the annotations marker."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            head = fh.read(_HEAD_SCAN_BYTES)
    except OSError:
        return False
    if not head.startswith("---"):
        return False
    rest = head[3:]
    end = rest.find("\n---")
    block = rest if end == -1 else rest[:end]
    return _MARKER_LINE_RE.search(block) is not None


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
        if not _is_ours(path):
            return AnnotationResult(path, "skipped", "target exists and is not a zotero notes file")
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
