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

from factlog.integrations.zotero.api_client import ZoteroClient
from factlog.integrations.zotero.config import ZoteroConfig
from factlog.integrations.zotero.item_parser import parse_item
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

    @property
    def imported(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "imported")

    @property
    def skipped(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "skipped")

    @property
    def errors(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "error")


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
) -> ImportReport:
    """Fetch the selected items and write each into ``<target>/sources/``."""
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
            result = writer.write(parsed, target, imported_at)
        except OSError as exc:
            report.outcomes.append(ItemOutcome(key, title, "error", None, str(exc)))
            continue
        report.outcomes.append(
            ItemOutcome(key, title, result.status, result.path, result.reason)
        )
    return report
