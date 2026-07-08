#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Orchestrate a Zotero import: fetch -> parse -> write, with a per-item report.

Ties together the client (#9), parser (#5), and writer (#7). Kept separate from
the CLI so it is unit-testable with a fake client and a temp target — the CLI
module is then a thin adapter (arg parsing + printing).

Determinism (P3): items are processed in ``zotero_key`` order so the writer's
collision suffixes are stable across runs. Partial failure is tolerated — a
single item that fails to write is recorded as an error and the rest continue,
so one bad item never aborts a whole collection import.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from factlog.integrations.zotero.annotation_writer import AnnotationResult, write_annotations
from factlog.integrations.zotero.api_client import ZoteroClient
from factlog.integrations.zotero.config import ZoteroConfig
from factlog.integrations.zotero.item_parser import parse_item
from factlog.integrations.zotero.pdf_importer import PdfOutcome, _att_key, place_pdfs
from factlog.integrations.zotero.source_writer import SourceWriter


@dataclass(frozen=True)
class ItemOutcome:
    """What happened to one item: status is imported | skipped | error."""

    key: str
    title: str
    status: str
    path: Path | None = None
    reason: str = ""


@dataclass
class ImportReport:
    outcomes: list[ItemOutcome] = field(default_factory=list)
    pdf_outcomes: list[PdfOutcome] = field(default_factory=list)
    annotation_outcomes: list[AnnotationResult] = field(default_factory=list)

    @property
    def imported(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "imported")

    @property
    def skipped(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "skipped")

    @property
    def errors(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "error")

    @property
    def pdf_placed(self) -> int:
        return sum(1 for o in self.pdf_outcomes if o.status == "placed")

    @property
    def pdf_skipped(self) -> int:
        return sum(1 for o in self.pdf_outcomes if o.status == "skipped")

    @property
    def pdf_errors(self) -> int:
        return sum(1 for o in self.pdf_outcomes if o.status == "error")

    @property
    def annotations_written(self) -> int:
        # Newly created notes files only (see annotations_updated for refreshes).
        return sum(1 for o in self.annotation_outcomes if o.status == "written")

    @property
    def annotations_updated(self) -> int:
        # Existing notes files rewritten because a highlight/note changed.
        return sum(1 for o in self.annotation_outcomes if o.status == "updated")

    @property
    def annotations_skipped(self) -> int:
        return sum(1 for o in self.annotation_outcomes if o.status == "skipped")

    @property
    def annotation_errors(self) -> int:
        return sum(1 for o in self.annotation_outcomes if o.status == "error")


def fetch_items(
    client: ZoteroClient,
    *,
    collection: str | None = None,
    tag: str | None = None,
    items: list[str] | None = None,
) -> list[dict]:
    """Fetch raw items for exactly one selector (collection / tag / items)."""
    selectors = [collection is not None, tag is not None, items is not None]
    if sum(selectors) != 1:
        raise ValueError("exactly one of collection, tag, items must be given")
    if collection is not None:
        return client.get_items_by_collection(collection)
    if tag is not None:
        return client.get_items_by_tag(tag)
    return client.get_items_by_ids(items or [])


def import_items(
    client: ZoteroClient,
    *,
    target: Path | str,
    config: ZoteroConfig | None = None,
    collection: str | None = None,
    tag: str | None = None,
    items: list[str] | None = None,
    imported_at: str = "",
    dry_run: bool = False,
    pdf: bool = False,
    annotations: bool = False,
) -> ImportReport:
    """Fetch the selected items and write each into ``<target>/sources/``.

    With ``dry_run`` the same decision is computed (including collision suffixes)
    but no file is created — the report's "imported" outcomes are what *would* be
    written.

    With ``pdf``, each item's PDF attachments are also placed under ``sources/``
    (paired with the bibliographic file by stem). Placement runs for imported
    *and* skipped items — a previously-imported item may still be missing its
    PDFs — but not for an item that failed (no bibliographic file to pair with).
    Conversion to text is left to the caller's ingest step.
    """
    config = config or ZoteroConfig()
    raw = fetch_items(client, collection=collection, tag=tag, items=items)
    writer = SourceWriter(
        skip_duplicates=config.skip_duplicates,
        include_abstract=config.include_abstract,
    )
    report = ImportReport()
    # Sort by the *parsed* identity key (parse_item's own fallback logic) so the
    # order matches the writer's collision-suffix basis regardless of where the
    # key sat in the raw item — keeping re-import deterministic (P3).
    parsed_items = sorted((parse_item(item) for item in raw), key=lambda p: p.get("zotero_key", ""))
    for parsed in parsed_items:
        title = parsed.get("title") or "(untitled)"
        key = parsed.get("zotero_key", "")
        try:
            result = writer.plan(parsed, target) if dry_run else writer.write(parsed, target, imported_at)
        except OSError as exc:
            report.outcomes.append(ItemOutcome(key, title, "error", None, str(exc)))
            continue
        report.outcomes.append(
            ItemOutcome(key, title, result.status, result.path, result.reason)
        )
        pairable = key and result.path is not None and result.status in ("imported", "skipped")
        if pdf and pairable:
            report.pdf_outcomes.extend(
                place_pdfs(
                    client,
                    item_key=key,
                    base_stem=result.path.stem,
                    target=target,
                    dry_run=dry_run,
                )
            )
        if annotations and pairable:
            ann = _import_annotations(client, parsed, result.path.stem, target, dry_run)
            # Skip the no-op "item has no notes/highlights" case so the report
            # reflects only items that actually had annotations.
            if ann.path is not None or ann.status == "error":
                report.annotation_outcomes.append(ann)
    return report


def _import_annotations(client, parsed, base_stem, target, dry_run) -> AnnotationResult:
    """Collect an item's notes + PDF-attachment annotations and write the source.

    Per-item isolation: any client/write failure (including an unclassified
    exception the client re-raises) becomes an error outcome so one bad item never
    aborts the whole import — annotations are a side feature, like PDF placement.
    """
    key = parsed.get("zotero_key", "")
    try:
        notes = client.get_notes(key)
        anns: list[dict] = []
        for attachment in client.get_pdf_attachments(key):
            att_key = _att_key(attachment)
            if att_key:
                anns.extend(client.get_annotations(att_key))
        return write_annotations(parsed, anns, notes, base_stem, target, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 — deliberate per-item isolation
        return AnnotationResult(None, "error", str(exc) or type(exc).__name__)
