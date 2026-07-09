# SPDX-License-Identifier: Apache-2.0
"""Report-only version drift for the arXiv records a KB already holds (spec §11
Step 6, part 2; issue #78).

## What this does, and what it deliberately does not

For every arXiv record in a KB's provenance ledgers (``<kb>/source-provenance/*.json``),
this asks arXiv "what is the latest version now?" and compares it with the version
the ledger recorded. It **reports** divergences. It never judges them, never writes
to ``sources/``, never rewrites a ledger, and never diffs the text of two versions —
all of that is out of scope (``--auto-update`` is #79).

A version bump does **not** invalidate the earlier import (#57 §6.1): it means the
source evolved, and whether the KB entry should change is a human decision (P1). So
the command's whole job is to surface "your KB records vN, arXiv now serves vM" and
"a paper you did not record as withdrawn is now withdrawn", and to remember when it
last looked so ``--older-than`` can skip fresh papers.

## The five measured API traps this file works around (#57)

* **A nonexistent id — or a nonexistent version — answers HTTP 200 with zero
  entries, not an error.** :meth:`ArxivClient.fetch_works` already turns that
  absence into a ``missing`` list, and this module renders each missing id as a
  per-id *error* result, never as "unchanged".
* **The response is not in request order.** ``fetch_works`` matches on
  ``(base, version)``; this module keys results by base id, never by position.
* **``max_results`` defaults to 10**, silently dropping a larger batch.
  ``fetch_works`` sets it; this module batches at :data:`BATCH_SIZE` (arXiv's
  100-id ceiling) so it never asks for more than one page can hold.
* **``<arxiv:comment>`` is unstructured prose.** It is never parsed for meaning;
  this command does not read it at all.
* **The 3s courtesy delay is the client's job**, enforced by unit test. This
  module does not touch the wire — it drives :class:`ArxivClient` — so the delay
  cannot be bypassed here.

## Withdrawal is not a version change, and not retraction

If the ledger did not record a paper as withdrawn and arXiv now does, that is
surfaced **prominently regardless of ``--older-than`` or any other flag**, naming
the agent (the author vs arXiv administrators, #57). Withdrawal can arrive *without*
a version bump, so it is tracked as a signal orthogonal to the version comparison.
Withdrawal is not retraction — arXiv has no peer-reviewed retraction process — so
the word "retracted" is never used for it.

## Robustness: one paper's problem is never the batch's

A corrupt ledger is isolated to the source it belongs to and reported as a per-id
error; enumeration continues. (This class of bug had to be fixed twice already,
#65/#71.) A corrupt *check-log* is a single KB-level file: it is a clear failure the
caller reports and stops on, never a traceback.

## Determinism and the filesystem contract

This module reads ``source-provenance/*.json`` and the KB-level check-log, and — via
the caller — writes only the check-log (``<kb>/check-log/arxiv.json``), which lives
outside ``sources/``. No original ``.md`` and no ledger is opened for writing, so
every source stays byte- and ``mtime_ns``-identical across a run (P4). Results are
produced in ``arxiv_id`` order so output is reproducible.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from factlog.integrations.arxiv.client import MAX_ID_LIST
from factlog.integrations.arxiv.check_log import CheckLog
from factlog.integrations.arxiv.source_writer import withdrawal_agent
from factlog.integrations.common.provenance import (
    SIDECAR_DIR,
    ProvenanceError,
    read_provenance,
)

__all__ = [
    "BATCH_SIZE",
    "LedgerEntry",
    "VersionCheck",
    "STATUS_UNCHANGED",
    "STATUS_CHANGED",
    "STATUS_ERROR",
    "STATUS_SKIPPED",
    "collect_ledger_entries",
    "partition_by_freshness",
    "check_entries",
    "summarize",
]

#: A provenance record's ``type`` for an arXiv contribution.
_ARXIV_TYPE = "arxiv"

#: arXiv's ceiling of ids per ``id_list`` request; the batch size this module
#: sends. Sourced from the client so the two never drift.
BATCH_SIZE = MAX_ID_LIST

#: The version comparison outcomes. Withdrawal is tracked as an *orthogonal* flag
#: on :class:`VersionCheck`, not a status, because a paper can be newly withdrawn
#: with or without a version change.
STATUS_UNCHANGED = "unchanged"
STATUS_CHANGED = "changed"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"


@dataclass(frozen=True)
class LedgerEntry:
    """One arXiv paper the KB records, gathered from its provenance ledger(s).

    ``recorded_version`` is the version the ledger holds (an ``int``; ``None`` only
    if a ledger somehow carries an arXiv record without one). ``recorded_withdrawn_by``
    is the withdrawal agent the ledger recorded, or ``None`` if it was not recorded
    as withdrawn — this is what a *newly* withdrawn paper is measured against.
    ``sources`` are the ledger-relative paths that reference the paper (one paper
    can be cited by several sources), for the report only.
    """

    arxiv_id: str
    recorded_version: int | None
    recorded_withdrawn_by: str | None
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class VersionCheck:
    """The outcome of checking one paper (or one corrupt ledger).

    ``status`` is one of the ``STATUS_*`` constants. ``newly_withdrawn`` is set
    whenever arXiv now reports a withdrawal the ledger did not record, *independent*
    of ``status`` — a withdrawn paper whose version did not change is still
    ``unchanged`` but ``newly_withdrawn``. ``withdrawn_by`` carries arXiv's current
    agent (``"author"``/``"admin"``) when the paper is withdrawn now. For an error
    result the ``arxiv_id`` may instead be a ledger path (a corrupt file), and
    ``reason`` explains it.
    """

    arxiv_id: str
    status: str
    recorded_version: int | None = None
    current_version: int | None = None
    newly_withdrawn: bool = False
    withdrawn_by: str | None = None
    reason: str = ""
    sources: tuple[str, ...] = ()


def _relative(path: Path, kb_root: Path) -> str:
    try:
        return str(path.relative_to(kb_root))
    except ValueError:
        return str(path)


def collect_ledger_entries(
    kb_root: Path | str,
) -> tuple[list[LedgerEntry], list[VersionCheck]]:
    """Gather every arXiv record from ``<kb>/source-provenance/**/*.json``.

    Returns ``(entries, errors)``: the deduplicated papers to check, and a per-file
    ``error`` :class:`VersionCheck` for each ledger that would not parse. A corrupt
    ledger is *that source's* problem — it never aborts the enumeration (#65/#71).

    One arXiv paper may be referenced by several ledgers (an arXiv-primary original
    and an OpenAlex-primary one that cites the same preprint). They collapse to one
    entry keyed by ``arxiv_id``; the highest recorded version wins, any recorded
    withdrawal agent is kept, and every referencing source path is retained.
    """
    root = Path(kb_root)
    sidecar_root = root / SIDECAR_DIR
    if not sidecar_root.is_dir():
        return [], []

    slots: dict[str, dict] = {}
    errors: list[VersionCheck] = []
    for path in sorted(sidecar_root.rglob("*.json")):
        if not path.is_file():
            continue
        try:
            provenance = read_provenance(path)
        except ProvenanceError as exc:
            errors.append(
                VersionCheck(
                    arxiv_id=_relative(path, root),
                    status=STATUS_ERROR,
                    reason=f"corrupt provenance ledger: {exc}",
                )
            )
            continue
        rel = _relative(path, root)
        for record in provenance.records:
            if record.type != _ARXIV_TYPE:
                continue
            version = record.fields.get("version")
            if not isinstance(version, int) or isinstance(version, bool):
                version = None
            withdrawn_by = record.fields.get("withdrawn_by")
            if not isinstance(withdrawn_by, str) or not withdrawn_by:
                withdrawn_by = None
            slot = slots.setdefault(
                record.id,
                {"version": None, "withdrawn_by": None, "sources": set()},
            )
            if version is not None and (
                slot["version"] is None or version > slot["version"]
            ):
                slot["version"] = version
            if withdrawn_by and not slot["withdrawn_by"]:
                slot["withdrawn_by"] = withdrawn_by
            slot["sources"].add(rel)

    entries = [
        LedgerEntry(
            arxiv_id=arxiv_id,
            recorded_version=slot["version"],
            recorded_withdrawn_by=slot["withdrawn_by"],
            sources=tuple(sorted(slot["sources"])),
        )
        for arxiv_id, slot in slots.items()
    ]
    entries.sort(key=lambda e: e.arxiv_id)
    errors.sort(key=lambda e: e.arxiv_id)
    return entries, errors


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    # A naive timestamp is compared as UTC; the caller always writes tz-aware.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def partition_by_freshness(
    entries: Sequence[LedgerEntry],
    check_log: CheckLog,
    older_than_days: float,
    now: datetime,
) -> tuple[list[LedgerEntry], list[VersionCheck]]:
    """Split *entries* into (to check now, skipped as recently checked).

    Freshness is read **only from the check-log** — never from a ``stat`` on the
    source files — so this touches nothing under ``sources/``. A paper is skipped
    when its recorded ``last_checked_at`` is newer than ``now - older_than_days``.
    An entry the check-log has never seen, or whose timestamp will not parse, is
    always checked (fail-open: better to re-check than to silently never look).
    """
    cutoff = now - timedelta(days=older_than_days)
    to_check: list[LedgerEntry] = []
    skipped: list[VersionCheck] = []
    for entry in entries:
        record = check_log.entries.get(entry.arxiv_id)
        last = _parse_iso(record.last_checked_at) if record else None
        if last is not None and last > cutoff:
            skipped.append(
                VersionCheck(
                    arxiv_id=entry.arxiv_id,
                    status=STATUS_SKIPPED,
                    recorded_version=entry.recorded_version,
                    current_version=record.version,
                    reason=f"checked at {record.last_checked_at}",
                    sources=entry.sources,
                )
            )
        else:
            to_check.append(entry)
    return to_check, skipped


def _diff(entry: LedgerEntry, work) -> VersionCheck:
    current = work.version
    recorded = entry.recorded_version
    # Withdrawal is measured against the ledger's own record, independently of the
    # version: a paper can be withdrawn without a new version (#57).
    newly_withdrawn = bool(work.withdrawn) and entry.recorded_withdrawn_by is None
    changed = recorded is not None and current != recorded
    return VersionCheck(
        arxiv_id=entry.arxiv_id,
        status=STATUS_CHANGED if changed else STATUS_UNCHANGED,
        recorded_version=recorded,
        current_version=current,
        newly_withdrawn=newly_withdrawn,
        withdrawn_by=work.withdrawn_by,
        sources=entry.sources,
    )


def check_entries(
    entries: Sequence[LedgerEntry],
    client,
    *,
    batch_size: int = BATCH_SIZE,
    progress: Callable[[int, int], None] | None = None,
) -> list[VersionCheck]:
    """Query arXiv for each entry's latest version and diff it against the ledger.

    Entries are batched (at most ``batch_size`` per request, arXiv's ceiling) and
    the results matched back **by base id**, never by response order (#57). A
    requested id arXiv silently declined — a nonexistent id, or a base whose only
    recorded version is gone — comes back in the client's ``missing`` list and
    becomes a per-id ``error`` result rather than "unchanged".

    ``progress`` is called with ``(papers_done, papers_total)`` after each batch so
    the caller can show progress and an ETA on stderr. Transport failures propagate
    (``ArxivError`` / ``ArxivConnectionError``); the caller decides the exit code.
    """
    results: list[VersionCheck] = []
    total = len(entries)
    done = 0
    for start in range(0, total, batch_size):
        batch = entries[start : start + batch_size]
        result = client.fetch_works([entry.arxiv_id for entry in batch])
        by_id = {work.arxiv_id: work for work in result.works}
        for entry in batch:
            work = by_id.get(entry.arxiv_id)
            if work is None:
                results.append(
                    VersionCheck(
                        arxiv_id=entry.arxiv_id,
                        status=STATUS_ERROR,
                        recorded_version=entry.recorded_version,
                        reason="no entry returned by arXiv (nonexistent id or version)",
                        sources=entry.sources,
                    )
                )
            else:
                results.append(_diff(entry, work))
        done += len(batch)
        if progress is not None:
            progress(done, total)
    results.sort(key=lambda r: r.arxiv_id)
    return results


@dataclass
class Summary:
    """Tallies over a run's results, for the human and porcelain footers."""

    checked: int = 0
    unchanged: int = 0
    changed: int = 0
    withdrawn: int = 0
    errors: int = 0
    skipped: int = 0


def summarize(
    results: Iterable[VersionCheck], skipped: Sequence[VersionCheck]
) -> Summary:
    """Count outcomes. ``withdrawn`` counts newly-withdrawn papers across every
    checked result, whatever their version status. ``checked`` excludes skipped."""
    summary = Summary(skipped=len(skipped))
    for result in results:
        if result.status == STATUS_ERROR:
            summary.errors += 1
            continue
        summary.checked += 1
        if result.status == STATUS_CHANGED:
            summary.changed += 1
        elif result.status == STATUS_UNCHANGED:
            summary.unchanged += 1
        if result.newly_withdrawn:
            summary.withdrawn += 1
    return summary


def format_eta(papers: int, batch_size: int, delay: float) -> str:
    """A human ETA for ``papers`` in ``ceil(papers/batch_size)`` delayed requests."""
    batches = (papers + batch_size - 1) // batch_size
    seconds = int(round(batches * max(delay, 0.0)))
    if seconds >= 60:
        return f"~{seconds // 60}m{seconds % 60:02d}s"
    return f"~{seconds}s"


def withdrawal_note(result: VersionCheck) -> str:
    """The prominent, retraction-free withdrawal line for a newly-withdrawn paper."""
    agent = withdrawal_agent(result.withdrawn_by)
    version = f"v{result.current_version}" if result.current_version else "the current version"
    return (
        f"arXiv now reports {result.arxiv_id} ({version}) as WITHDRAWN by {agent}, "
        "which the ledger did not record. Withdrawal is not retraction; this "
        "unverified signal flags the paper for human review before any claim from "
        "it is trusted."
    )


def _sources_suffix(result: VersionCheck) -> str:
    return f"  (sources: {', '.join(result.sources)})" if result.sources else ""


def report_lines(
    results: Sequence[VersionCheck],
    skipped: Sequence[VersionCheck],
    summary: Summary,
    *,
    target: Path,
    older_than_days: float,
) -> list[str]:
    """The human-readable stdout report. Withdrawals lead, prominently, whatever the
    version outcome; then version divergences; then per-id errors; then the tally."""
    total = len(results) + len(skipped)
    header = f"Checked {len(results)} of {total} arXiv record(s) in KB: {target}"
    if skipped:
        header += (
            f"\n  ({len(skipped)} skipped: checked within the last "
            f"{_days(older_than_days)}; run with --older-than 0 to force a re-check)"
        )
    lines = [header]

    withdrawn = [r for r in results if r.newly_withdrawn]
    changed = [
        r for r in results if r.status == STATUS_CHANGED and not r.newly_withdrawn
    ]
    errors = [r for r in results if r.status == STATUS_ERROR]

    if withdrawn:
        lines.append("\nWithdrawn since import (not a version change):")
        for result in withdrawn:
            note = withdrawal_note(result)
            if result.status == STATUS_CHANGED:
                note += (
                    f" Its version also changed: ledger records "
                    f"v{result.recorded_version}, arXiv now serves "
                    f"v{result.current_version}."
                )
            lines.append(f"  ⚠ {note}{_sources_suffix(result)}")

    if changed:
        lines.append("\nVersion diverged (the source evolved; this is a report, not a verdict):")
        for result in changed:
            lines.append(
                f"  ~ {result.arxiv_id}: ledger records v{result.recorded_version}, "
                f"arXiv now serves v{result.current_version}{_sources_suffix(result)}"
            )

    if errors:
        lines.append("\nCould not check:")
        for result in errors:
            lines.append(f"  ✗ {result.arxiv_id}: {result.reason}{_sources_suffix(result)}")

    lines.append("\nSummary:")
    lines.append(f"  Checked:         {summary.checked}")
    lines.append(f"  Up to date:      {summary.unchanged}")
    lines.append(f"  Version changed: {summary.changed}")
    lines.append(f"  Newly withdrawn: {summary.withdrawn}")
    lines.append(f"  Errors:          {summary.errors}")
    lines.append(f"  Skipped:         {summary.skipped}")
    return lines


def porcelain_lines(
    results: Sequence[VersionCheck],
    skipped: Sequence[VersionCheck],
    summary: Summary,
    *,
    target: Path,
) -> list[str]:
    """The machine contract on stdout: one tab-separated ``check`` row per record
    (checked, skipped, or errored), then the tallies. Parse by the first field.

    ``check\t<id>\t<status>\t<recorded>\t<current>\t<withdrawn_by>\t<newly_withdrawn>\t<reason>``
    with empty fields for absent values, ``newly_withdrawn`` as ``0``/``1``, and
    versions as bare integers. The progress/ETA never appears here — it is stderr.
    """
    rows: list[str] = []
    for result in sorted([*results, *skipped], key=lambda r: r.arxiv_id):
        recorded = "" if result.recorded_version is None else str(result.recorded_version)
        current = "" if result.current_version is None else str(result.current_version)
        rows.append(
            "check\t{id}\t{status}\t{recorded}\t{current}\t{by}\t{withdrawn}\t{reason}".format(
                id=result.arxiv_id,
                status=result.status,
                recorded=recorded,
                current=current,
                by=result.withdrawn_by or "",
                withdrawn="1" if result.newly_withdrawn else "0",
                reason=result.reason,
            )
        )
    rows.append(f"checked\t{summary.checked}")
    rows.append(f"unchanged\t{summary.unchanged}")
    rows.append(f"changed\t{summary.changed}")
    rows.append(f"withdrawn\t{summary.withdrawn}")
    rows.append(f"errors\t{summary.errors}")
    rows.append(f"skipped\t{summary.skipped}")
    rows.append(f"target\t{target}")
    return rows


def _days(value: float) -> str:
    whole = int(value)
    label = "day" if whole == 1 else "days"
    return f"{whole} {label}" if value == whole else f"{value} days"
