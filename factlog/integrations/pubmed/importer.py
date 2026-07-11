#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Drive PubMed fetches into a factlog KB's ``sources/`` (#166, spec §6.4/§7).

Sits between :mod:`~factlog.integrations.pubmed.client` (raw transport) /
:mod:`~factlog.integrations.pubmed.work_parser` (the present/deleted/merged/
unparseable classification) and the CLI, mirroring the arXiv and OpenAlex
importers: write each parsed record, and report a per-PMID outcome.

A paper already in the KB via another database (an OpenAlex/Zotero record of the
same work, matched on a shared DOI or PMID) is *merged* into that original's
provenance sidecar (§7.3, ``merged`` outcome) rather than written twice; the
classification and sidecar write both live in
:class:`~factlog.integrations.pubmed.source_writer.PubMedSourceWriter`.

Imported records are ordinary sources. They still pass the usual
sync -> review -> accept gate before becoming facts (P1/P2), and NCBI is never
written to (P4). The retraction flag a record may carry is a **signal a human
must act on**, never an absorbed truth (§7.2).

**The four request outcomes are kept apart, never collapsed** (parser docstring):

* a **present** record parses and writes (or merges) as usual;
* a **merged** record — returned under a different PMID than requested (a merged
  lineage) — still writes, keyed on the PMID that actually arrived, its reason
  naming the requested->returned redirect so a reader can follow it;
* a **deleted** PMID (requested, absent from the response) becomes a per-id
  ``error`` — never a hard batch failure;
* an **unparseable** record (no PMID) becomes a per-id ``error`` carrying the
  reason, so one bad record never discards the batch.

**Determinism.** Records are written in ``pmid`` order and error outcomes in
``key`` order, so a re-run assigns the same collision suffixes to the same files
(P3) and the ``--porcelain`` output is reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from factlog.integrations.common.source_writer import CandidateMatch
from factlog.integrations.pubmed.config import PubMedConfig
from factlog.integrations.pubmed.source_writer import PubMedSourceWriter
from factlog.integrations.pubmed.work_parser import PubMedFetchOutcome

__all__ = ["WorkOutcome", "ImportReport", "import_outcome"]


@dataclass(frozen=True)
class WorkOutcome:
    """What happened to one requested PMID.

    ``status`` is ``"imported"`` | ``"skipped"`` | ``"error"`` | ``"merged"``.
    ``"merged"`` means the paper was already in the KB via another database and
    this PubMed view was folded into that original's provenance sidecar (§7.3)
    rather than written as a second file; ``path`` names that existing original. A
    merge is a success — it does not affect the exit code.

    ``candidate`` is a title+author+year match surfaced for a human (#75): the
    record still imported as a new file, but it resembles an existing source that
    shares no exact identifier. It is a field, never a status — the counters and
    exit code are untouched.
    """

    status: str  # "imported" | "skipped" | "error" | "merged"
    key: str
    title: str
    path: Path | None = None
    reason: str = ""
    candidate: CandidateMatch | None = None


@dataclass
class ImportReport:
    outcomes: list[WorkOutcome] = field(default_factory=list)
    #: Why the merge-candidate ledger could not be consulted, if it could not.
    #: The import still succeeded; the #75 fallback was disabled for this run.
    candidate_ledger_error: str | None = None

    def _count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)

    @property
    def imported(self) -> int:
        return self._count("imported")

    @property
    def skipped(self) -> int:
        return self._count("skipped")

    @property
    def merged(self) -> int:
        return self._count("merged")

    @property
    def errors(self) -> int:
        return self._count("error")

    @property
    def candidates(self) -> list[WorkOutcome]:
        """Imported records that surfaced a title+author+year candidate (#75), in
        report order — the CLI reports one line per entry."""
        return [o for o in self.outcomes if o.candidate is not None]


def import_outcome(
    outcome: PubMedFetchOutcome | None,
    invalid=(),
    *,
    target: Path | str,
    config: PubMedConfig | None = None,
    imported_at: str = "",
    dry_run: bool = False,
) -> ImportReport:
    """Write each parsed record in *outcome* into ``<target>/sources/`` and report.

    ``outcome`` is a :class:`PubMedFetchOutcome` (or ``None`` when no valid PMID
    reached the wire). ``invalid`` is ``(raw_pmid, reason)`` pairs the normalizer
    rejected before any request. With ``dry_run`` no file is created; the report
    still names the file each record *would* claim, collision suffixes included.

    Outcome order is deterministic: record outcomes first (sorted by ``pmid``),
    then error outcomes (deleted, unparseable, invalid), sorted by ``key``.
    """
    settings = config or PubMedConfig()
    writer = PubMedSourceWriter(
        include_abstract=getattr(settings, "include_abstract", True),
    )

    # A map from a merged record's returned PMID to the PMID that was requested,
    # so the outcome can name the redirect a downstream refresh (#170) follows.
    merged_redirect: dict[str, str | None] = {}
    works = []
    if outcome is not None:
        works = list(outcome.works)
        for merged in outcome.merged:
            merged_redirect[merged.returned_pmid] = merged.requested_pmid

    record_outcomes: list[WorkOutcome] = []
    for work in sorted(works, key=lambda w: w.pmid):
        result = (
            writer.plan(work, target) if dry_run
            else writer.write(work, target, imported_at)
        )
        reason = result.reason
        requested = merged_redirect.get(work.pmid, ...)
        if requested is not ...:
            # A merged lineage: the record arrived under a PMID other than the one
            # asked for. Name the redirect so it is not baffling, without letting
            # it change the write outcome (present/merged/skipped stand).
            note = (
                f"PubMed returned PMID {work.pmid} for requested PMID {requested}"
                if requested
                else f"PubMed returned PMID {work.pmid} under a different lineage"
            )
            reason = f"{reason}; {note}" if reason else note
        record_outcomes.append(
            WorkOutcome(
                status=result.status,
                key=work.pmid,
                title=work.title or "(untitled)",
                path=result.path,
                reason=reason,
                candidate=result.candidate,
            )
        )
    record_outcomes.sort(key=lambda o: o.key)

    error_outcomes: list[WorkOutcome] = []
    if outcome is not None:
        for pmid in outcome.deleted:
            error_outcomes.append(
                WorkOutcome("error", pmid, "", None, "no record returned by PubMed (deleted or nonexistent PMID)")
            )
        for bad in outcome.unparseable:
            error_outcomes.append(
                WorkOutcome("error", f"record #{bad.index}", "", None, bad.reason)
            )
    for raw, reason in invalid:
        error_outcomes.append(WorkOutcome("error", raw, "", None, reason))
    error_outcomes.sort(key=lambda o: o.key)

    return ImportReport(
        record_outcomes + error_outcomes,
        candidate_ledger_error=writer.candidate_ledger_error,
    )
