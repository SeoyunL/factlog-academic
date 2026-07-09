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
from factlog.integrations.common.front_matter import read_scalars
from factlog.integrations.common.provenance import (
    SIDECAR_DIR,
    ProvenanceError,
    SourceRecord,
    read_provenance,
    update_source,
    write_provenance,
)

__all__ = [
    "BATCH_SIZE",
    "LedgerEntry",
    "VersionCheck",
    "LedgerUpdate",
    "STATUS_UNCHANGED",
    "STATUS_CHANGED",
    "STATUS_ERROR",
    "STATUS_SKIPPED",
    "UPDATE_WRITTEN",
    "UPDATE_UNCHANGED",
    "UPDATE_NO_LEDGER",
    "UPDATE_ERROR",
    "AUTO_UPDATE_FIELDS",
    "collect_ledger_entries",
    "partition_by_freshness",
    "check_entries",
    "summarize",
    "apply_auto_update",
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

    ``current_last_updated`` (an ISO date string) and ``current_comment`` carry the
    two *other* version-tracking values arXiv returned, alongside
    ``current_version``. They exist for ``--auto-update`` (#79), which writes exactly
    those three fields into the ledger. Report-only (#78) never reads them. They are
    ``None`` for a skipped/errored/missing result, which never reaches a write.
    """

    arxiv_id: str
    status: str
    recorded_version: int | None = None
    current_version: int | None = None
    current_last_updated: str | None = None
    current_comment: str | None = None
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
    slots: dict[str, dict] = {}
    errors: list[VersionCheck] = []

    sidecar_root = root / SIDECAR_DIR
    for path in sorted(sidecar_root.rglob("*.json")) if sidecar_root.is_dir() else ():
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

    # A paper imported before #82 has front matter but no ledger. Reading only the
    # ledgers would answer "no arXiv records in <kb>" and exit 0 for most of an
    # existing user's library — the command would be silently wrong about all of
    # its input, which is the failure this whole track exists to eliminate. The
    # front matter carries `arxiv_id` and `arxiv_version` (#60), which is exactly
    # what a check needs, and reading it writes nothing, so report-only holds.
    # A ledger, when present, is authoritative: it is what a refresh updates.
    sources_dir = root / "sources"
    for path in sorted(sources_dir.glob("*.md")) if sources_dir.is_dir() else ():
        scalars = read_scalars(path, ("arxiv_id", "arxiv_version"))
        arxiv_id = scalars.get("arxiv_id", "")
        # A ledger, when one exists, is authoritative — it is what a refresh
        # updates, and its `sources` name the ledgers a reader should open. Front
        # matter only speaks for a paper no ledger covers.
        if not arxiv_id or arxiv_id in slots:
            continue
        try:
            version = int(scalars.get("arxiv_version", ""))
        except ValueError:
            version = None
        slots[arxiv_id] = {
            "version": version,
            "withdrawn_by": None,
            "sources": {_relative(path, root)},
        }

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
        # The two other version-tracking values --auto-update writes. Dates are
        # serialized to ISO strings here (the ledger stores strings, not `date`,
        # for the same reason the arXiv writer does: `json` cannot serialize a
        # `date`, and `provenance` refuses to guess). `comment` is stored verbatim.
        current_last_updated=work.last_updated.isoformat() if work.last_updated else None,
        current_comment=work.comment,
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


# --------------------------------------------------------------------------- #
# --auto-update: the one legitimate caller of provenance.update_source (#79)
# --------------------------------------------------------------------------- #

#: The exact fields ``--auto-update`` may rewrite in an arXiv ledger record. This
#: is the whole of the narrow contract (#79): the version and the two other
#: values arXiv edits as a version evolves. It never includes the abstract, title,
#: authors, ``imported_at``, ``submitted``, ``primary_category`` — or
#: ``withdrawn_by`` (see :func:`apply_auto_update` for why a withdrawal is
#: surfaced but never absorbed).
AUTO_UPDATE_FIELDS = ("version", "last_updated", "comment")

#: A ledger was rewritten (a version-tracking field genuinely moved).
UPDATE_WRITTEN = "updated"
#: The upstream version-tracking fields already matched the ledger — nothing was
#: written, so the file stays byte- and ``mtime_ns``-identical.
UPDATE_UNCHANGED = "unchanged"
#: The paper is known only from front matter (imported before #82), so there is no
#: ledger to update. ``--auto-update`` does **not** create one (see below).
UPDATE_NO_LEDGER = "no-ledger"
#: A ledger could not be read while updating it — that paper's problem, reported
#: per-id, never a batch crash.
UPDATE_ERROR = "error"


@dataclass(frozen=True)
class LedgerUpdate:
    """What ``--auto-update`` did (or declined to do) for one paper.

    ``status`` is one of the ``UPDATE_*`` constants. ``ledgers`` names the sidecars
    actually rewritten (empty unless ``status`` is :data:`UPDATE_WRITTEN`).
    ``recorded_version``/``current_version`` mirror the check so the report can say
    "ledger v5 -> v7 recorded". ``reason`` explains a :data:`UPDATE_NO_LEDGER` or
    :data:`UPDATE_ERROR` outcome.
    """

    arxiv_id: str
    status: str
    ledgers: tuple[str, ...] = ()
    recorded_version: int | None = None
    current_version: int | None = None
    reason: str = ""


def _refreshed_fields(existing: SourceRecord, result: VersionCheck) -> dict:
    """The record's fields with *only* the three version-tracking values replaced.

    Everything else — ``imported_at`` (top level, untouched by construction),
    ``submitted``, ``primary_category``, and any recorded ``withdrawn_by`` — is
    copied through verbatim. A ``None`` incoming value (arXiv dropped a comment, or
    reports no ``updated`` date) is written as ``None`` and dropped on serialization
    by :meth:`SourceRecord.to_dict`, so it reflects the current upstream state
    rather than freezing a stale one.
    """
    fields = dict(existing.fields)
    fields["version"] = result.current_version
    fields["last_updated"] = result.current_last_updated
    fields["comment"] = result.current_comment
    return fields


def apply_auto_update(
    results: Sequence[VersionCheck],
    kb_root: Path | str,
) -> list[LedgerUpdate]:
    """Write the three version-tracking fields (:data:`AUTO_UPDATE_FIELDS`) of each
    checked paper into its provenance ledger(s), and nothing else.

    This is the sole legitimate caller of :func:`provenance.update_source` (#58/#79).
    An *import* calls ``add_source``, which refuses to revise a diverging entry
    because an import has no authority to; a *refresh* has gone to the upstream API
    to learn the new value, so replacing the entry is correct. The narrow contract:

    * Only ``version``, ``last_updated`` and ``comment`` are rewritten. Every other
      ledger field, every non-arXiv record in the same ledger, and — critically —
      the original ``sources/*.md`` are left untouched. The ``.md`` is never opened,
      so it stays byte- and ``mtime_ns``-identical (P4 needs no narrowing).
    * A paper whose three fields already match the ledger is a **byte-identical
      no-op**: the file is not rewritten, so its bytes and ``mtime_ns`` do not move.
    * **A withdrawal is surfaced but never absorbed.** ``withdrawn_by`` is not in
      :data:`AUTO_UPDATE_FIELDS`, so ``--auto-update`` never records it. That is
      deliberate: writing it would flip ``newly_withdrawn`` to ``False`` on the next
      run and silence the very signal the issue says must "never pass quietly". The
      withdrawal keeps surfacing in the report on every run until a human records
      it. ``--auto-update`` may still update the *version* of a withdrawn paper (the
      withdrawal often ships as a new version); the withdrawal itself stays for the
      P1 human gate.
    * **A front-matter-only paper gets no ledger.** A paper imported before #82 has
      front matter but no ledger. Creating one here would be an *import*'s write —
      it fabricates ``imported_at`` and the initial provenance record, which only
      the import path has the authority and the true values to write (#58). A
      refresh knows the new *version*, not where the source came from. So the paper
      is reported as :data:`UPDATE_NO_LEDGER` and left for a re-import/backfill to
      give it a ledger; its check-log timestamp still advances (that is KB-level
      housekeeping, not a source-provenance write).
    * A ledger that will not read is that paper's problem — a per-id
      :data:`UPDATE_ERROR`, never a batch crash (#65/#71).

    Results with no observed version (skipped, missing, corrupt at collection) are
    ignored: there is nothing to write. Returns one :class:`LedgerUpdate` per paper
    acted on, in ``arxiv_id`` order.
    """
    root = Path(kb_root)
    outcomes: list[LedgerUpdate] = []
    for result in results:
        # Only a paper the API actually answered has a version to record. Errors,
        # skips and missing ids carry no current version and are never written.
        if result.status == STATUS_ERROR or result.current_version is None:
            continue

        # A paper spoken for only by `sources/*.md` has no ledger (imported before
        # #82). `collect_ledger_entries` marks its sources with the `sources/`
        # prefix; a ledger-backed paper's sources are `source-provenance/*.json`.
        sidecars = [s for s in result.sources if not str(s).startswith("sources/")]
        if not sidecars:
            outcomes.append(
                LedgerUpdate(
                    arxiv_id=result.arxiv_id,
                    status=UPDATE_NO_LEDGER,
                    recorded_version=result.recorded_version,
                    current_version=result.current_version,
                    reason=(
                        "imported before #82: front matter only, no provenance "
                        "ledger to update. Run `factlog arxiv-import --id "
                        "<arxiv_id>` to create one; --auto-update "
                        "will not fabricate an import record."
                    ),
                )
            )
            continue

        written: list[str] = []
        errors: list[str] = []
        for rel in sidecars:
            path = root / rel
            # Reading AND writing are that paper's problem. Guarding only the read
            # is how #65 and #71 shipped a batch crash twice: an unwritable
            # `source-provenance/`, a full disk, or a permission error would abort
            # the whole run with a traceback, after earlier papers' ledgers were
            # already written. `write_provenance` re-raises `OSError`, and its
            # `mkdir` raises one too.
            try:
                provenance = read_provenance(path)
            except (ProvenanceError, OSError) as exc:
                errors.append(f"{rel}: {exc}")
                continue
            existing = next(
                (
                    r
                    for r in provenance.records
                    if r.type == _ARXIV_TYPE and r.id == result.arxiv_id
                ),
                None,
            )
            if existing is None:
                # The ledger named this paper at collection but no longer carries
                # the record: treat as nothing to do rather than inventing one.
                continue
            record = SourceRecord(
                type=existing.type,
                id=existing.id,
                imported_at=existing.imported_at,
                fields=_refreshed_fields(existing, result),
            )
            # A no-op must not touch the file: compare the serialized forms so an
            # unchanged paper leaves the ledger byte- and mtime_ns-identical.
            if record.to_dict() == existing.to_dict():
                continue
            try:
                update_source(provenance, record)
                write_provenance(path, provenance)
            except (ProvenanceError, OSError) as exc:
                errors.append(f"{rel}: {exc}")
                continue
            written.append(rel)

        if errors:
            # Any failure is an error, even when a sibling ledger was written. One
            # paper can be cited by two ledgers (an arXiv-primary original and an
            # OpenAlex-primary one referencing the same preprint); reporting the
            # pair as "updated" would bury the half that did not land, and only the
            # error status reaches the exit code.
            outcomes.append(
                LedgerUpdate(
                    arxiv_id=result.arxiv_id,
                    status=UPDATE_ERROR,
                    ledgers=tuple(sorted(written)),
                    recorded_version=result.recorded_version,
                    current_version=result.current_version,
                    reason="; ".join(errors),
                )
            )
        elif written:
            outcomes.append(
                LedgerUpdate(
                    arxiv_id=result.arxiv_id,
                    status=UPDATE_WRITTEN,
                    ledgers=tuple(sorted(written)),
                    recorded_version=result.recorded_version,
                    current_version=result.current_version,
                )
            )
        else:
            outcomes.append(
                LedgerUpdate(
                    arxiv_id=result.arxiv_id,
                    status=UPDATE_UNCHANGED,
                    recorded_version=result.recorded_version,
                    current_version=result.current_version,
                )
            )

    outcomes.sort(key=lambda u: u.arxiv_id)
    return outcomes


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


def _recorded_in(result) -> str:
    """Where the recorded version came from: a ledger, or a source's front matter.

    A paper imported before #82 has no ledger, and saying "ledger records v5" for it
    names a file that does not exist. Ledger sources live under
    ``source-provenance/``; front-matter ones are the ``sources/*.md`` themselves.
    """
    sources = getattr(result, "sources", ()) or ()
    if sources and all(str(s).startswith("sources/") for s in sources):
        return "front matter records"
    return "ledger records"


def report_lines(
    results: Sequence[VersionCheck],
    skipped: Sequence[VersionCheck],
    summary: Summary,
    *,
    target: Path,
    older_than_days: float,
    updates: Sequence[LedgerUpdate] = (),
) -> list[str]:
    """The human-readable stdout report. Withdrawals lead, prominently, whatever the
    version outcome; then version divergences; then per-id errors; then, under
    ``--auto-update``, what was written to the ledgers; then the tally."""
    total = len(results) + len(skipped)
    header = f"Checked {len(results)} of {total} arXiv record(s) in KB: {target}"
    if skipped:
        header += (
            f"\n  ({len(skipped)} skipped: checked within the last "
            f"{_days(older_than_days)}. A withdrawal or a version bump that "
            "appeared since their last check is NOT detected — arXiv only says so "
            "when asked. Run with --older-than 0 to force a re-check.)"
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
                f"  ~ {result.arxiv_id}: {_recorded_in(result)} "
                f"v{result.recorded_version}, arXiv now serves "
                f"v{result.current_version}{_sources_suffix(result)}"
            )

    if errors:
        lines.append("\nCould not check:")
        for result in errors:
            lines.append(f"  ✗ {result.arxiv_id}: {result.reason}{_sources_suffix(result)}")

    lines.extend(_auto_update_lines(updates))

    lines.append("\nSummary:")
    lines.append(f"  Checked:         {summary.checked}")
    lines.append(f"  Up to date:      {summary.unchanged}")
    lines.append(f"  Version changed: {summary.changed}")
    lines.append(f"  Newly withdrawn: {summary.withdrawn}")
    lines.append(f"  Errors:          {summary.errors}")
    lines.append(f"  Skipped:         {summary.skipped}")
    if updates:
        lines.append(
            f"  Ledgers updated: {sum(1 for u in updates if u.status == UPDATE_WRITTEN)}"
        )
    return lines


def _auto_update_lines(updates: Sequence[LedgerUpdate]) -> list[str]:
    """The ``--auto-update`` section: what was written, what already matched, what
    has no ledger, and any per-id write error. Empty when the flag is off."""
    if not updates:
        return []
    written = [u for u in updates if u.status == UPDATE_WRITTEN]
    no_ledger = [u for u in updates if u.status == UPDATE_NO_LEDGER]
    errors = [u for u in updates if u.status == UPDATE_ERROR]
    lines: list[str] = []
    if written:
        lines.append(
            "\nLedger updated (version-tracking fields only — "
            "version, last_updated, comment):"
        )
        for u in written:
            ledgers = f"  ({', '.join(u.ledgers)})" if u.ledgers else ""
            lines.append(
                f"  ✎ {u.arxiv_id}: recorded v{u.current_version} "
                f"(was v{u.recorded_version}){ledgers}"
            )
    if no_ledger:
        lines.append(
            "\nNot auto-updated (no ledger; front matter only, imported before #82 — "
            "run `factlog arxiv-import --id <arxiv_id>` to create one):"
        )
        for u in no_ledger:
            lines.append(f"  · {u.arxiv_id}: arXiv now serves v{u.current_version}")
    if errors:
        lines.append("\nCould not auto-update:")
        for u in errors:
            lines.append(f"  ✗ {u.arxiv_id}: {u.reason}")
    return lines


def porcelain_lines(
    results: Sequence[VersionCheck],
    skipped: Sequence[VersionCheck],
    summary: Summary,
    *,
    target: Path,
    updates: Sequence[LedgerUpdate] = (),
) -> list[str]:
    """The machine contract on stdout: one tab-separated ``check`` row per record
    (checked, skipped, or errored), then — only under ``--auto-update`` — one
    ``update`` row per acted-on paper, then the tallies. Parse by the first field.

    ``check\t<id>\t<status>\t<recorded>\t<current>\t<withdrawn_by>\t<newly_withdrawn>\t<reason>``
    with empty fields for absent values, ``newly_withdrawn`` as ``0``/``1``, and
    versions as bare integers. The ``update`` rows are
    ``update\t<id>\t<status>\t<recorded>\t<current>\t<ledgers>`` where ``status`` is
    one of ``updated``/``unchanged``/``no-ledger``/``error`` and ``ledgers`` is a
    comma-joined list (empty unless ``updated``). They are absent when the flag is
    off, so an existing #78 parser is unaffected. The progress/ETA is stderr only.
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
    for u in updates:
        recorded = "" if u.recorded_version is None else str(u.recorded_version)
        current = "" if u.current_version is None else str(u.current_version)
        rows.append(
            "update\t{id}\t{status}\t{recorded}\t{current}\t{ledgers}".format(
                id=u.arxiv_id,
                status=u.status,
                recorded=recorded,
                current=current,
                ledgers=",".join(u.ledgers),
            )
        )
    rows.append(f"checked\t{summary.checked}")
    rows.append(f"unchanged\t{summary.unchanged}")
    rows.append(f"changed\t{summary.changed}")
    rows.append(f"withdrawn\t{summary.withdrawn}")
    rows.append(f"errors\t{summary.errors}")
    rows.append(f"skipped\t{summary.skipped}")
    if updates:
        rows.append(f"updated\t{sum(1 for u in updates if u.status == UPDATE_WRITTEN)}")
    rows.append(f"target\t{target}")
    return rows


def _days(value: float) -> str:
    whole = int(value)
    label = "day" if whole == 1 else "days"
    return f"{whole} {label}" if value == whole else f"{value} days"
