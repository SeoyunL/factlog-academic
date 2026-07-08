#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Place a Zotero item's PDF attachments into a KB's ``sources/`` (phase 2).

Downloads each PDF attachment of an imported bibliographic item and writes it as
a ``sources/<stem>.pdf`` original, deterministically named so it pairs with the
item's bibliographic ``<stem>.md``. This module does NOT convert PDFs to text —
that is delegated to factlog's existing ``ingest`` pipeline (wired in the CLI),
which turns ``sources/*.pdf`` into ``runs/sources/*.txt`` and is read by ``sync``.

Invariants:

* **P4 (immutability).** An existing target file is never overwritten or
  re-downloaded — placement skips it. Writes are atomic (temp + os.replace).
* **P3 (idempotent).** Filenames derive from a stable base stem and the stable
  attachment key, so re-running an import re-derives the same names and skips
  what is already present.
* **Partial failure.** A single attachment that fails to download/write is
  recorded as an error; the rest continue.

The caller passes ``base_stem`` — the stem of the bibliographic source file that
was actually written (collision suffix included) — so the PDF pairs with the
exact ``.md`` even when the slug was disambiguated.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from factlog.integrations.zotero.api_client import ZoteroError


@dataclass(frozen=True)
class PdfOutcome:
    """What happened to one PDF attachment: placed | skipped | error."""

    attachment_key: str
    path: Path
    status: str
    reason: str = ""


def _att_key(attachment: dict) -> str:
    data = attachment.get("data") if isinstance(attachment, dict) else None
    if isinstance(data, dict) and data.get("key"):
        return str(data["key"])
    return str(attachment.get("key", "")) if isinstance(attachment, dict) else ""


def pdf_filename(base_stem: str, attachment_key: str, single: bool) -> str:
    """Deterministic PDF filename paired with the bibliographic ``<base_stem>.md``.

    A lone PDF gets the clean ``<base_stem>.pdf``; when an item has several, each
    is disambiguated by its stable attachment key so re-import re-derives the same
    names regardless of attachment order.
    """
    if single:
        return f"{base_stem}.pdf"
    return f"{base_stem}-{attachment_key}.pdf"


def place_pdfs(
    client,
    *,
    item_key: str,
    base_stem: str,
    target: Path | str,
    dry_run: bool = False,
) -> list[PdfOutcome]:
    """Download and place all PDF attachments of ``item_key`` under ``sources/``."""
    attachments = client.get_pdf_attachments(item_key)
    single = len(attachments) == 1
    sources_dir = Path(target) / "sources"
    outcomes: list[PdfOutcome] = []
    for attachment in attachments:
        akey = _att_key(attachment)
        path = sources_dir / pdf_filename(base_stem, akey, single)
        if path.exists():
            outcomes.append(PdfOutcome(akey, path, "skipped", "already present"))
            continue
        if dry_run:
            outcomes.append(PdfOutcome(akey, path, "placed"))  # would place
            continue
        try:
            data = client.fetch_file(akey)
            sources_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(path, data)
        except (ZoteroError, OSError) as exc:
            outcomes.append(PdfOutcome(akey, path, "error", str(exc)))
            continue
        outcomes.append(PdfOutcome(akey, path, "placed"))
    return outcomes


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
