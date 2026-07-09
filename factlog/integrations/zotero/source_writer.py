#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Write a parsed Zotero item into a factlog ``sources/<slug>.md`` original.

Consumes the standard dict from :mod:`factlog.integrations.zotero.item_parser`
and produces one markdown source file carrying a YAML provenance front matter
plus a readable body (abstract + the original Zotero/DOI/PMID pointers).

The write machinery — atomic writes, slug construction, globally-unique
filenames, the batch index, and duplicate detection — lives in
:mod:`factlog.integrations.common.source_writer`, shared with the OpenAlex
importer. This module supplies only what is Zotero-specific: the ``zotero_key``
identity, the front matter, and the body.
"""
from __future__ import annotations

from pathlib import Path

from factlog.integrations.common.source_writer import (
    BaseSourceWriter,
    WriteResult,
)
from factlog.integrations.common._textio import yaml_list as _yaml_list
from factlog.integrations.common._textio import yaml_scalar as _yaml_str
from factlog.integrations.common.front_matter import read_scalar
from factlog.integrations.zotero._textio import ANNOTATION_MARKER_RE

__all__ = ["SourceWriter", "WriteResult", "read_zotero_key"]


def _author_display(author: dict) -> str:
    """Front-matter author string. "Family, Given" when both are known (the
    standard bibliographic form export can split unambiguously), else the single
    field available — so a compound surname is never mis-split downstream."""
    last = (author.get("last") or "").strip()
    first = (author.get("first") or "").strip()
    if last and first:
        return f"{last}, {first}"
    return last or (author.get("name") or "").strip() or first


def _first_author_name(parsed: dict) -> str:
    authors = parsed.get("authors") or []
    if authors:
        first = authors[0]
        return (first.get("last") or first.get("name") or "").strip()
    return ""


def read_zotero_key(path: Path) -> str:
    """Return the ``zotero_key`` recorded in a source file's front matter, or "".

    An annotation source (``source_kind: annotations``, i.e. a ``<stem>-notes.md``)
    returns "" even though it carries a ``zotero_key`` — it is a companion file,
    not the bibliographic import, so it must not be picked as the existing source
    for that key (which would mis-pair the item on re-import).
    """
    return read_scalar(path, "zotero_key", ANNOTATION_MARKER_RE)


class SourceWriter(BaseSourceWriter):
    """Render parsed Zotero items into ``sources/`` markdown originals."""

    identity_key = "zotero_key"
    source_name = "zotero"
    ignore_re = ANNOTATION_MARKER_RE
    # Zotero stays OUT of §7.3 merging, unlike arXiv and OpenAlex. A Zotero item is
    # the same paper as seen by THE USER, not by a DATABASE; §7.3 merges the views
    # different DATABASES hold of one work, and a personal library entry is
    # curation, not upstream bibliographic authority. Folding a Zotero item into a
    # source's provenance ledger would assert that the user's own record is a
    # database that observed the paper. So Zotero writes its own original and never
    # a sidecar. Do not flip this for symmetry.
    merges_cross_source = False

    def identity_of(self, parsed: dict) -> str:
        return parsed.get("zotero_key", "")

    def slug_fields(self, parsed: dict) -> tuple[str, str, str]:
        return (
            _first_author_name(parsed),
            parsed.get("year") or "",
            parsed.get("title") or "",
        )

    def cross_ids(self, parsed: dict) -> dict[str, str]:
        return {
            kind: value
            for kind in ("doi", "pmid")
            if (value := (parsed.get(kind) or "").strip())
        }

    def render(self, parsed: dict, imported_at: str = "") -> str:
        """The full markdown text (front matter + body) for a parsed item."""
        return self._front_matter(parsed, imported_at) + self._body(parsed)

    def _front_matter(self, parsed: dict, imported_at: str) -> str:
        lines = ["---"]
        lines.append(f"zotero_key: {_yaml_str(parsed.get('zotero_key', ''))}")
        if parsed.get("item_type"):
            lines.append(f"item_type: {_yaml_str(parsed['item_type'])}")
        lines.append(f"title: {_yaml_str(parsed.get('title', ''))}")
        authors = [_author_display(a) for a in (parsed.get("authors") or []) if _author_display(a)]
        if authors:
            lines.append(f"authors: {_yaml_list(authors)}")
        if parsed.get("year"):
            lines.append(f"year: {_yaml_str(parsed['year'])}")
        if parsed.get("journal"):
            lines.append(f"journal: {_yaml_str(parsed['journal'])}")
        if parsed.get("doi"):
            lines.append(f"doi: {_yaml_str(parsed['doi'])}")
        if parsed.get("pmid"):
            lines.append(f"pmid: {_yaml_str(parsed['pmid'])}")
        if parsed.get("tags"):
            lines.append(f"tags: {_yaml_list(parsed['tags'])}")
        lines.append("imported_from: zotero")
        if imported_at:
            lines.append(f"imported_at: {_yaml_str(imported_at)}")
        if parsed.get("retracted"):
            lines.append("retracted: true")
        lines.append("---")
        return "\n".join(lines) + "\n"

    def _body(self, parsed: dict) -> str:
        title = parsed.get("title") or "Untitled"
        parts = [f"\n# {title}\n"]
        if self.include_abstract:
            abstract = (parsed.get("abstract") or "").strip()
            parts.append("\n## Abstract\n")
            parts.append(f"\n{abstract or '_No abstract available._'}\n")
        parts.append("\n## Original source\n")
        key = parsed.get("zotero_key", "")
        if key:
            parts.append(f"\n- Zotero item: `zotero://select/library/items/{key}`")
        if parsed.get("doi"):
            parts.append(f"\n- DOI: {parsed['doi']}")
        if parsed.get("pmid"):
            parts.append(f"\n- PMID: {parsed['pmid']}")
        return "".join(parts) + "\n"
