#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Drive arXiv fetches into a factlog KB's ``sources/`` (spec §11 Step 3).

Sits between :mod:`~factlog.integrations.arxiv.client` and the CLI, mirroring
:mod:`factlog.integrations.openalex.importer`: parse, write, and report per-work
outcomes. Cross-source merging, the provenance sidecar, search, and version
checking are later steps and are not done here.

Imported works are ordinary sources. They still pass the usual
sync -> review -> accept gate before becoming facts (P1/P2), and arXiv is never
written to (P4).

**Determinism.** Works are written in ``(arxiv_id, version)`` order and error
outcomes in ``key`` order, so a re-run assigns the same collision suffixes to the
same files (P3) and the ``--porcelain`` output is reproducible. This module never
re-pairs a request to an entry by position — arXiv reorders responses (#57), so
it consumes the client's :class:`BatchResult` (already matched by id) only.

The client keeps three kinds of requested id apart, and so does this importer:

* a **work** that came back parses and writes as usual;
* a **missing** id (well-formed but unknown, or a pinned version that does not
  exist) becomes a per-id ``error`` outcome — never a hard batch failure;
* an **invalid** id (rejected by the normalizer before any request) is likewise a
  per-id ``error`` outcome, so one bad ``--id`` never kills the rest of the batch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from factlog.integrations.arxiv.config import ArxivConfig
from factlog.integrations.arxiv.source_writer import ArxivSourceWriter

__all__ = ["WorkOutcome", "ImportReport", "import_works"]


@dataclass(frozen=True)
class WorkOutcome:
    """What happened to one requested arXiv record."""

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


def import_works(
    works,
    missing=(),
    invalid=(),
    *,
    target: Path | str,
    config: ArxivConfig | None = None,
    imported_at: str = "",
    dry_run: bool = False,
) -> ImportReport:
    """Write each parsed work into ``<target>/sources/`` and report the outcome.

    ``missing`` is the requested ids the API silently declined (each an
    :class:`~factlog.integrations.arxiv.id_normalizer.ArxivId`); ``invalid`` is
    ``(raw_id, reason)`` pairs the normalizer rejected before the request. Both
    become ``error`` outcomes. With ``dry_run`` no file is created; the report
    still names the file each work *would* claim, collision suffixes included.

    Outcome order is deterministic: work outcomes first (sorted by
    ``(arxiv_id, version)``), then error outcomes (sorted by ``key``).
    """
    settings = config or ArxivConfig()
    writer = ArxivSourceWriter(
        skip_duplicates=settings.skip_duplicates,
        include_abstract=settings.include_abstract,
    )

    work_outcomes: list[WorkOutcome] = []
    for work in sorted(works, key=lambda w: (w.arxiv_id, w.version)):
        result = (
            writer.plan(work, target) if dry_run
            else writer.write(work, target, imported_at)
        )
        work_outcomes.append(
            WorkOutcome(
                status=result.status,
                # The versioned id is what a reader recognises; identity/dedup
                # still key on the base id inside the writer.
                key=work.versioned_id,
                title=work.title or "(untitled)",
                path=result.path,
                reason=result.reason,
            )
        )

    error_outcomes = [
        WorkOutcome("error", key, "", None, reason) for key, reason in invalid
    ]
    for identifier in missing:
        error_outcomes.append(
            WorkOutcome("error", str(identifier), "", None, "no entry returned by arXiv")
        )
    error_outcomes.sort(key=lambda o: o.key)

    return ImportReport(work_outcomes + error_outcomes)
