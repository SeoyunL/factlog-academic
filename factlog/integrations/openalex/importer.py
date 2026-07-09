#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Drive OpenAlex fetches into a factlog KB's ``sources/`` (spec §5.2).

Sits between :mod:`~factlog.integrations.openalex.api_client` and the CLI,
mirroring :mod:`factlog.integrations.zotero.importer`: fetch, parse, write, and
report per-work outcomes.

Imported works are ordinary sources. They still pass the usual
sync -> review -> accept gate before becoming facts (P1/P2), and OpenAlex is
never written to (P4).

Works are imported in ``openalex_id`` order, not API-response order, so a
re-run assigns the same collision suffixes to the same files (P3).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from factlog.integrations.common.front_matter import read_scalar
from factlog.integrations.openalex.api_client import OpenAlexClient, OpenAlexError
from factlog.integrations.openalex.config import OpenAlexConfig
from factlog.integrations.openalex.source_writer import OpenAlexSourceWriter
from factlog.integrations.openalex.work_parser import ParsedWork, parse_work


@dataclass(frozen=True)
class WorkOutcome:
    """What happened to one work."""

    status: str  # "imported" | "skipped" | "error"
    key: str
    title: str
    path: Path | None = None
    reason: str = ""


@dataclass
class ImportReport:
    outcomes: list[WorkOutcome] = field(default_factory=list)

    def _count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)

    @property
    def imported(self) -> int:
        return self._count("imported")

    @property
    def skipped(self) -> int:
        return self._count("skipped")

    @property
    def errors(self) -> int:
        return self._count("error")


def parse_works(raw_works) -> list[ParsedWork]:
    """Parse an API page, dropping records too malformed to address."""
    parsed = []
    for raw in raw_works:
        try:
            parsed.append(parse_work(raw))
        except OpenAlexError:
            continue
    return parsed


def import_works(
    works,
    *,
    target: Path | str,
    config: OpenAlexConfig | None = None,
    imported_at: str = "",
    dry_run: bool = False,
) -> ImportReport:
    """Write each parsed work into ``<target>/sources/`` and report the outcome.

    With ``dry_run`` no file is created; the report still names the file each
    work *would* claim, collision suffixes included.
    """
    settings = config or OpenAlexConfig()
    writer = OpenAlexSourceWriter(
        skip_duplicates=settings.skip_duplicates,
        include_abstract=settings.include_abstract,
    )
    report = ImportReport()

    for work in sorted(works, key=lambda w: w.openalex_id):
        result = (
            writer.plan(work, target) if dry_run
            else writer.write(work, target, imported_at)
        )
        report.outcomes.append(
            WorkOutcome(
                status=result.status,
                key=work.openalex_id,
                title=work.title or "(untitled)",
                path=result.path,
                reason=result.reason,
            )
        )
    return report


def resolve_work_id(target: Path | str, slug: str) -> str:
    """The ``openalex_id`` recorded by a source in the KB, for ``--for <SLUG>``.

    Accepts the slug with or without the ``.md`` suffix. Raises
    :class:`OpenAlexError` when the source is absent or was not imported from
    OpenAlex — ``openalex-cite`` has nothing to traverse from otherwise.
    """
    if not isinstance(slug, str) or not slug.strip():
        raise OpenAlexError("--for needs a non-empty source slug.")
    name = slug.strip()
    if not name.endswith(".md"):
        name += ".md"

    path = Path(target) / "sources" / name
    if not path.is_file():
        raise OpenAlexError(f"no source {name} in {Path(target) / 'sources'}")

    work_id = read_scalar(path, "openalex_id")
    if not work_id:
        raise OpenAlexError(
            f"{name} records no openalex_id; import it with 'factlog openalex-import' first."
        )
    return work_id


def fetch_work(client: OpenAlexClient, *, work_id: str = "", doi: str = "") -> ParsedWork:
    """Fetch and parse one work by OpenAlex id or DOI. Costs 0 credits."""
    if bool(work_id) == bool(doi):
        raise OpenAlexError("specify exactly one of --work-id or --doi.")
    raw = client.get_work(work_id) if work_id else client.get_work_by_doi(doi)
    return parse_work(raw)
