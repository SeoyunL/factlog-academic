# SPDX-License-Identifier: Apache-2.0
"""Report-only refresh of the PubMed retraction status a KB already holds (issue #168).

## What this does, and what it deliberately does not

For every PubMed record in a KB's provenance ledgers (``<kb>/source-provenance/**/*.json``)
— and every pre-provenance front-matter-only PubMed source — this re-fetches the record
by PMID (``efetch``), re-runs the two-marker retraction detector
(:func:`~factlog.integrations.pubmed.retraction.detect_retraction`) and compares the live
retraction status with what the ledger recorded. It **reports** the divergence and writes
nothing under ``sources/`` and nothing to a ledger; the only thing it advances is the
KB-level check-log's last-checked timestamps.

It is the PubMed counterpart of ``openalex-refresh`` (#83) and ``arxiv-check-versions``
(#78), scoped to **report-only**: ``--auto-update`` (record the acknowledged status) is
#169 and merged/deleted-PMID following is #170. Neither is implemented here.

## The one thing a refresh compares: retraction status (Decision, #168)

PubMed owns retraction status (§7.2), and ``PubMedSourceWriter._IDENTIFYING_FIELDS`` is
**empty** on purpose (#166): nothing a PubMed record carries is auto-updatable, because a
retraction is a **human-gate signal**, not a field a tool silently rewrites. So the drift
this command reports is exactly the retraction status — the same value the writer emits
as the source-scoped ``retracted`` field (a bare top-level ``retracted:`` claim is never
written by either). A record whose retraction status matches what the ledger recorded is
``unchanged``; one that gained a retraction is ``newly_retracted``; one whose recorded
retraction PubMed no longer reports is ``un_retracted``.

## "Unchanged" means one thing, here and for a future writer (#121)

A retraction is recorded iff the ledger's ``retracted`` field is present **and** ``True``
(``fields.get("retracted") is True``), exactly as ``source_writer._provenance_record``
emits it — its absence *means* not-retracted, never not-checked. This report and any later
``--auto-update`` (#169) read that one definition, so a "changed" here can never disagree
with an "unchanged" there. That agreement is established now, before a writer exists.

## A front-matter-only PMID is read, never given a ledger (#110)

A PubMed record spoken for only by ``sources/*.md`` (imported before provenance ledgers,
or echoed as a ``pmid:`` on a paper imported from another database) has no PubMed ledger
record to acknowledge a retraction against. It is still **read**, so a newly-appeared
retraction on it surfaces — but the note points at ``pubmed-backfill-provenance`` (#172),
which builds the missing ledger, rather than a command that would refuse. This command
never fabricates a ledger.

## Determinism and the filesystem contract

This reads ``source-provenance/**/*.json`` (via the shared #112 walker) and the KB-level
check-log, and — via the caller — writes only ``<kb>/check-log/pubmed.json``. No original
``.md`` is opened. Results are produced in ``pmid`` order so output is reproducible.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from factlog.integrations.common.front_matter import read_scalars
from factlog.integrations.common.porcelain import porcelain_field
from factlog.integrations.common.provenance import (
    SIDECAR_DIR,
    ProvenanceError,
    backfill_remedy,
    excluded_reason,
    excluded_sources_by_id,
    provenance_sources,
    read_provenance,
)
from factlog.integrations.pubmed.client import PubMedError
from factlog.integrations.pubmed.work_parser import (
    PubMedParseError,
    parse_efetch_response,
)

__all__ = [
    "LedgerEntry",
    "RefreshCheck",
    "Summary",
    "STATUS_UNCHANGED",
    "STATUS_CHANGED",
    "STATUS_ERROR",
    "STATUS_SKIPPED",
    "RETRACTION_KEY",
    "RETRACTION_NOTICE_KEY",
    "BACKFILL_COMMAND",
    "parse_retraction_flag",
    "collect_ledger_entries",
    "excluded_checks",
    "flagged_only",
    "partition_by_freshness",
    "check_entries",
    "summarize",
    "provenance_of",
    "retraction_note",
    "un_retraction_note",
    "report_lines",
    "porcelain_lines",
    "estimate_lines",
    "format_eta",
]

#: A provenance record's ``type`` for a PubMed contribution.
_PUBMED_TYPE = "pubmed"

#: The command that gives a front-matter-only PubMed paper the ledger a retraction
#: needs to be acknowledged against (#172). Referenced by name only — this string is
#: stable whether or not that command has shipped, so #110's remedy is nameable today.
BACKFILL_COMMAND = "pubmed-backfill-provenance"

#: The front-matter key ``PubMedSourceWriter`` emits for PubMed's retraction signal.
#: Source-scoped on purpose (§7.2): never a bare ``retracted:`` claim.
RETRACTION_KEY = "pubmed_retracted"
#: The front-matter key carrying the retraction *notice* PMID, when one is linkable.
RETRACTION_NOTICE_KEY = "pubmed_retraction_notice_pmid"

#: The boolean words :func:`parse_retraction_flag` recognises, matched case-insensitively
#: — YAML 1.2's core schema, the only values ``PubMedSourceWriter`` emits.
_RETRACTION_TRUE = "true"
_RETRACTION_FALSE = "false"

STATUS_UNCHANGED = "unchanged"
STATUS_CHANGED = "changed"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"


@dataclass(frozen=True)
class LedgerEntry:
    """One PubMed record the KB holds, gathered from its provenance ledger(s) or its
    source front matter.

    ``recorded_retracted`` is the retraction status a refresh measures the live record
    against — ``True`` iff a ledger record carried ``retracted: true`` (or the front
    matter carried a boolean ``pubmed_retracted``). ``recorded_notice_pmid`` is the notice
    PMID the ledger/front matter recorded, if any. ``sources`` are the ledger-relative
    paths that reference the record.
    """

    pmid: str
    recorded_retracted: bool = False
    recorded_notice_pmid: str | None = None
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class RefreshCheck:
    """The outcome of refreshing one PubMed record (or one corrupt/excluded source).

    ``status`` is one of the ``STATUS_*`` constants; it is :data:`STATUS_CHANGED` when the
    retraction status diverged (``newly_retracted`` or ``un_retracted``). The two drift
    flags are mutually exclusive: ``newly_retracted`` — PubMed now reports a retraction the
    record did not carry; ``un_retracted`` — PubMed no longer reports a retraction the
    record still holds. ``current_notice_pmid`` is the notice PMID a fresh retraction links
    to, if any. ``recorded_from`` is ``"ledger"`` or ``"front-matter"`` so a note never
    claims "the ledger recorded" a value that came from front matter. For an error result
    the ``pmid`` may instead be a ledger/source path and ``reason`` explains it.
    """

    pmid: str
    status: str
    recorded_retracted: bool = False
    current_retracted: bool = False
    current_notice_pmid: str | None = None
    newly_retracted: bool = False
    un_retracted: bool = False
    recorded_from: str = "ledger"
    reason: str = ""
    sources: tuple[str, ...] = ()


def _relative(path: Path, kb_root: Path) -> str:
    try:
        return str(path.relative_to(kb_root))
    except ValueError:
        return str(path)


def parse_retraction_flag(raw: str) -> bool | str:
    """What a front-matter ``pubmed_retracted`` scalar says, or the scalar itself.

    Front matter has no booleans; ``read_scalars`` is a line reader, so what reaches here
    is a string. Returns ``True``/``False`` for a value this tool reads as a boolean, and
    the raw text verbatim for one it cannot. An absent or empty value is ``False``: the
    writer emits the key only when PubMed flags a retraction, so silence *means*
    not-retracted. The narrowing to ``bool`` for the compare happens in
    :func:`collect_ledger_entries`; a value that is not a boolean is compared as
    not-retracted (this command reports, it does not judge the ``.md``). Mirrors
    OpenAlex's ``parse_retraction_flag`` so the two integrations behave identically.
    """
    text = (raw or "").strip()
    if text in ("''", '""'):  # an explicitly empty scalar, unquoted by hand
        text = ""
    if not text:
        return False
    folded = text.lower()
    if folded == _RETRACTION_TRUE:
        return True
    if folded == _RETRACTION_FALSE:
        return False
    return text


def collect_ledger_entries(
    kb_root: Path | str,
) -> tuple[list[LedgerEntry], list[RefreshCheck]]:
    """Gather every PubMed record from ``<kb>/source-provenance/**/*.json`` and every
    front-matter-only PubMed source.

    Returns ``(entries, errors)``: the deduplicated records to refresh, and a per-file
    ``error`` :class:`RefreshCheck` for each ledger that would not parse. A corrupt ledger
    is *that source's* problem — it never aborts the enumeration (#65/#71/#94).

    One record may be referenced by several ledgers; they collapse to one entry keyed by
    ``pmid``, OR-ing the retraction flag (any ledger recording a retraction wins) and
    keeping the first non-empty notice PMID. A ledger, when present, is authoritative;
    front matter speaks only for a record no PubMed ledger record covers.
    """
    root = Path(kb_root)
    slots: dict[str, dict] = {}
    errors: list[RefreshCheck] = []

    sidecar_root = root / SIDECAR_DIR
    for path in sorted(sidecar_root.rglob("*.json")) if sidecar_root.is_dir() else ():
        if not path.is_file():
            continue
        try:
            provenance = read_provenance(path)
        except ProvenanceError as exc:
            errors.append(
                RefreshCheck(
                    pmid=_relative(path, root),
                    status=STATUS_ERROR,
                    reason=f"corrupt provenance ledger: {exc}",
                )
            )
            continue
        rel = _relative(path, root)
        for record in provenance.records:
            if record.type != _PUBMED_TYPE:
                continue
            slot = slots.setdefault(record.id, _empty_slot())
            if record.fields.get("retracted") is True:
                slot["retracted"] = True
            notice = record.fields.get("retraction_notice_pmid")
            if slot["notice_pmid"] is None and isinstance(notice, str) and notice:
                slot["notice_pmid"] = notice
            slot["sources"].add(rel)

    # A record with no PubMed ledger record (front-matter only) is still read: a retraction
    # that appeared since import is real news, and reading writes nothing. Its front matter
    # carries the pmid and the source-scoped retraction key. The KB's own #112 walker, not a
    # flat glob, so a nested paper is not silently dropped from this command's denominator.
    for path in provenance_sources(root):
        scalars = read_scalars(path, ("pmid", RETRACTION_KEY, RETRACTION_NOTICE_KEY))
        pmid = scalars.get("pmid", "")
        if not pmid or pmid in slots:
            continue
        slots[pmid] = {
            "retracted": parse_retraction_flag(scalars.get(RETRACTION_KEY, "")) is True,
            "notice_pmid": scalars.get(RETRACTION_NOTICE_KEY) or None,
            "sources": {_relative(path, root)},
        }

    entries = [
        LedgerEntry(
            pmid=pmid,
            recorded_retracted=slot["retracted"],
            recorded_notice_pmid=slot["notice_pmid"],
            sources=tuple(sorted(slot["sources"])),
        )
        for pmid, slot in slots.items()
    ]
    entries.sort(key=lambda e: e.pmid)
    errors.sort(key=lambda e: e.pmid)
    return entries, errors


def _empty_slot() -> dict:
    return {"retracted": False, "notice_pmid": None, "sources": set()}


def excluded_checks(kb_root: Path | str) -> list[RefreshCheck]:
    """One ``error`` :class:`RefreshCheck` per PubMed record named only by a source outside
    the provenance root, so no ledger can exist for it (#112).

    Keyed by **pmid**, not by path: the pmid is what every other row of this report carries
    in that column. It is an *error* rather than a note because a retraction on this record
    will never be reported, and a command that exits 0 while that is true is the silent
    direction #112 closes. The OpenAlex twin is ``refresh.excluded_checks``.
    """
    root = Path(kb_root)
    remedy = backfill_remedy(BACKFILL_COMMAND)
    return [
        RefreshCheck(
            pmid=pmid,
            status=STATUS_ERROR,
            reason=excluded_reason(", ".join(refs), remedy),
            sources=refs,
        )
        for pmid, refs in sorted(excluded_sources_by_id(root, "pmid").items())
    ]


def flagged_only(entries: Sequence[LedgerEntry]) -> list[LedgerEntry]:
    """The subset of *entries* the KB already records as retracted (``--only-flagged``).

    ``--only-flagged`` re-checks only records whose ledger/front matter carries the
    source-scoped ``retracted`` signal — the cheap way to catch a retraction PubMed has
    since *reversed* without re-fetching the whole library.
    """
    return [e for e in entries if e.recorded_retracted]


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def partition_by_freshness(
    entries: Sequence[LedgerEntry],
    check_log,
    older_than_days: float,
    now: datetime,
) -> tuple[list[LedgerEntry], list[RefreshCheck]]:
    """Split *entries* into (to check now, skipped as recently checked).

    Freshness is read **only from the check-log** — never from a ``stat`` on the source
    files — so this touches nothing under ``sources/``. A record is skipped when its
    recorded ``last_checked_at`` is newer than ``now - older_than_days``. A record the
    check-log has never seen, or whose timestamp will not parse, is always checked
    (fail-open).
    """
    cutoff = now - timedelta(days=older_than_days)
    to_check: list[LedgerEntry] = []
    skipped: list[RefreshCheck] = []
    for entry in entries:
        record = check_log.entries.get(entry.pmid)
        last = _parse_iso(record.last_checked_at) if record else None
        if last is not None and last > cutoff:
            skipped.append(
                RefreshCheck(
                    pmid=entry.pmid,
                    status=STATUS_SKIPPED,
                    recorded_retracted=entry.recorded_retracted,
                    recorded_from=provenance_of(entry.sources),
                    reason=f"checked at {record.last_checked_at}",
                    sources=entry.sources,
                )
            )
        else:
            to_check.append(entry)
    return to_check, skipped


def _diff(entry: LedgerEntry, work) -> RefreshCheck:
    current_retracted = bool(work.retracted)
    # A *value* comparison, not a presence test — so a retraction PubMed has since reversed
    # (recorded True, live False) surfaces as loudly as a fresh one.
    newly_retracted = current_retracted and not entry.recorded_retracted
    un_retracted = (not current_retracted) and entry.recorded_retracted
    status = STATUS_CHANGED if (newly_retracted or un_retracted) else STATUS_UNCHANGED
    return RefreshCheck(
        pmid=entry.pmid,
        status=status,
        recorded_retracted=entry.recorded_retracted,
        current_retracted=current_retracted,
        current_notice_pmid=work.retraction_notice_pmid,
        newly_retracted=newly_retracted,
        un_retracted=un_retracted,
        recorded_from=provenance_of(entry.sources),
        sources=entry.sources,
    )


def check_entries(
    entries: Sequence[LedgerEntry],
    client,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> list[RefreshCheck]:
    """Fetch each record by PMID (``efetch``) and diff its retraction status against the
    ledger. Results are keyed by the **requested** PMID, never response order.

    A PMID that no longer returns a record (deleted, or merged away under a different
    PMID) is a per-id ``error``, never a crash and never *followed* — following a merge is
    #170, out of scope here. A per-id parse problem is likewise isolated. Connection and
    rate-limit/service failures propagate (the caller decides the exit code); a partial
    batch cannot be trusted.

    ``progress`` is called with ``(records_done, records_total)`` after each record.
    """
    from factlog.integrations.pubmed.client import (
        PubMedConnectionError,
        PubMedServiceError,
    )

    results: list[RefreshCheck] = []
    total = len(entries)
    for index, entry in enumerate(entries, 1):
        try:
            xml = client.efetch([entry.pmid])
            outcome = parse_efetch_response(xml, [entry.pmid])
        except (PubMedConnectionError, PubMedServiceError):
            # Transport / rate-limit failure: not a per-id problem, and not trustable
            # partial. Let the caller abort with the right exit code.
            raise
        except (PubMedError, PubMedParseError) as exc:
            results.append(
                RefreshCheck(
                    pmid=entry.pmid,
                    status=STATUS_ERROR,
                    reason=str(exc),
                    recorded_retracted=entry.recorded_retracted,
                    recorded_from=provenance_of(entry.sources),
                    sources=entry.sources,
                )
            )
        else:
            present = {r.requested_pmid: r.work for r in outcome.present}
            work = present.get(entry.pmid)
            if work is not None:
                results.append(_diff(entry, work))
            else:
                # Absent from the response (deleted) or returned under a different PMID
                # (merged). Either way this run could not confirm the record's retraction
                # status; report it per-id and leave following the pointer to #170.
                results.append(
                    RefreshCheck(
                        pmid=entry.pmid,
                        status=STATUS_ERROR,
                        reason=(
                            "PubMed returned no record under this PMID (deleted, or merged "
                            "away under a different PMID); retraction status not confirmed"
                        ),
                        recorded_retracted=entry.recorded_retracted,
                        recorded_from=provenance_of(entry.sources),
                        sources=entry.sources,
                    )
                )
        if progress is not None:
            progress(index, total)
    results.sort(key=lambda r: r.pmid)
    return results


@dataclass
class Summary:
    """Tallies over a run's results, for the human and porcelain footers."""

    checked: int = 0
    unchanged: int = 0
    changed: int = 0
    retracted: int = 0
    un_retracted: int = 0
    errors: int = 0
    skipped: int = 0


def summarize(
    results: Iterable[RefreshCheck], skipped: Sequence[RefreshCheck]
) -> Summary:
    """Count outcomes. ``retracted`` counts newly-retracted records and ``un_retracted``
    counts records PubMed no longer flags but the KB still does; ``checked`` excludes
    skipped and errored."""
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
        if result.newly_retracted:
            summary.retracted += 1
        if result.un_retracted:
            summary.un_retracted += 1
    return summary


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def provenance_of(sources) -> str:
    """``"front-matter"`` if *sources* are all ``sources/*.md`` (no PubMed ledger record),
    else ``"ledger"``. One record's sources are never mixed."""
    sources = sources or ()
    if sources and all(str(s).startswith("sources/") for s in sources):
        return "front-matter"
    return "ledger"


def retraction_note(result: RefreshCheck) -> str:
    """The prominent retraction line for a newly-retracted record.

    Names PubMed as the source of the signal, states it is unverified and flags the paper
    for human review, and points at the retraction *notice* PMID when one is linkable. For
    a **front-matter**-only record there is no PubMed ledger to acknowledge the retraction
    against, so the note prescribes ``pubmed-backfill-provenance`` (#172), which builds one
    — the loud warning stays, and it names a command that has been measured to fix the
    paper rather than one that would refuse (#110).
    """
    where = f" See the retraction notice (PMID {result.current_notice_pmid})." if result.current_notice_pmid else ""
    body = (
        f"PubMed now reports {result.pmid} as RETRACTED, which the "
        f"{'front matter' if result.recorded_from == 'front-matter' else 'ledger'} did not "
        "record. This is PubMed's signal, not an absorbed fact: it is unverified and flags "
        f"the paper for human review before any claim from it is trusted.{where}"
    )
    if result.recorded_from == "front-matter":
        return (
            f"{body} This paper has no PubMed provenance ledger, so the retraction cannot "
            f"be acknowledged and will keep surfacing until one exists; run "
            f"`factlog {BACKFILL_COMMAND}` to give it one."
        )
    return body


def un_retraction_note(result: RefreshCheck) -> str:
    """The line for a record PubMed no longer reports as retracted but the KB still does.

    Not a retraction warning — a reversed retraction is its own news. For a
    front-matter-only record it prescribes ``pubmed-backfill-provenance`` (#172) so the
    reversal can eventually be recorded; ``retracted`` is not an identifying field, so
    nothing diverges and a re-import does not error — the recorded flag is simply stale.
    """
    where = "front matter" if result.recorded_from == "front-matter" else "ledger"
    note = (
        f"PubMed no longer reports {result.pmid} as retracted, but the {where} still "
        "records a retraction. `retracted` is not an identifying field, so nothing diverges "
        "and a re-import does not error — the recorded flag is simply stale."
    )
    if result.recorded_from == "front-matter":
        return (
            f"{note} This paper has no PubMed provenance ledger; run "
            f"`factlog {BACKFILL_COMMAND}` to give it one so the reversal can be recorded."
        )
    return note


def _sources_suffix(result: RefreshCheck) -> str:
    return f"  (sources: {', '.join(result.sources)})" if result.sources else ""


def _days(value: float) -> str:
    whole = int(value)
    label = "day" if whole == 1 else "days"
    return f"{whole} {label}" if value == whole else f"{value} days"


def report_lines(
    results: Sequence[RefreshCheck],
    skipped: Sequence[RefreshCheck],
    summary: Summary,
    *,
    target: Path,
    older_than_days: float,
) -> list[str]:
    """The human-readable stdout report. Retractions lead; then reversals; then per-id
    errors; then the tally. Nothing is written — this is a report."""
    total = len(results) + len(skipped)
    header = f"Checked {summary.checked} of {total} PubMed record(s) in KB: {target}"
    if skipped:
        header += (
            f"\n  ({len(skipped)} skipped: checked within the last {_days(older_than_days)}. "
            "A retraction that appeared since their last check is NOT detected — PubMed only "
            "says so when asked. Run with --older-than 0 to force a re-check.)"
        )
    lines = [header]

    retracted = [r for r in results if r.newly_retracted]
    un_retracted = [r for r in results if r.un_retracted]
    errors = [r for r in results if r.status == STATUS_ERROR]

    if retracted:
        lines.append("\nNewly reported as retracted by PubMed (unverified; confirm before trusting):")
        for result in retracted:
            lines.append(f"  ⚠ {retraction_note(result)}{_sources_suffix(result)}")

    if un_retracted:
        lines.append("\nNo longer reported as retracted (PubMed reversed a retraction this KB records):")
        for result in un_retracted:
            lines.append(f"  ↺ {un_retraction_note(result)}{_sources_suffix(result)}")

    if errors:
        lines.append("\nCould not check:")
        for result in errors:
            lines.append(f"  ✗ {result.pmid}: {result.reason}{_sources_suffix(result)}")

    lines.append("\nSummary:")
    lines.append(f"  {'Checked:':<22}{summary.checked}")
    lines.append(f"  {'Up to date:':<22}{summary.unchanged}")
    lines.append(f"  {'Retraction changed:':<22}{summary.changed}")
    lines.append(f"  {'Retracted (new):':<22}{summary.retracted}")
    lines.append(f"  {'Retracted (reversed):':<22}{summary.un_retracted}")
    lines.append(f"  {'Errors:':<22}{summary.errors}")
    lines.append(f"  {'Skipped:':<22}{summary.skipped}")
    lines.append(
        "\nNothing was written: pubmed-refresh reports drift and stops. A new retraction is "
        "for a human to act on."
    )
    return lines


def porcelain_lines(
    results: Sequence[RefreshCheck],
    skipped: Sequence[RefreshCheck],
    summary: Summary,
    *,
    target: Path,
) -> list[str]:
    """The machine contract on stdout: one tab-separated ``check`` row per record, then
    tallies. Parse by the first field.

    ``check\\t<pmid>\\t<status>\\t<retracted>\\t<un_retracted>\\t<reason>`` with
    ``retracted``/``un_retracted`` as ``0``/``1``. Progress and the estimate stay on
    stderr only.
    """
    rows: list[str] = []
    for result in sorted([*results, *skipped], key=lambda r: r.pmid):
        rows.append(
            "check\t{pmid}\t{status}\t{retracted}\t{un}\t{reason}".format(
                pmid=porcelain_field(result.pmid),
                status=result.status,
                retracted="1" if result.newly_retracted else "0",
                un="1" if result.un_retracted else "0",
                reason=porcelain_field(result.reason),
            )
        )
    rows.append(f"checked\t{summary.checked}")
    rows.append(f"unchanged\t{summary.unchanged}")
    rows.append(f"changed\t{summary.changed}")
    rows.append(f"retracted\t{summary.retracted}")
    rows.append(f"un_retracted\t{summary.un_retracted}")
    rows.append(f"errors\t{summary.errors}")
    rows.append(f"skipped\t{summary.skipped}")
    rows.append(f"target\t{target}")
    return rows


# --------------------------------------------------------------------------- #
# time estimate (§1.3): tell the operator the cost, and what a key would save
# --------------------------------------------------------------------------- #
def format_eta(requests: int, interval: float) -> str:
    """A human ETA for ``requests`` serial efetch calls at ``interval`` seconds each."""
    seconds = int(round(requests * max(interval, 0.0)))
    if seconds >= 60:
        return f"~{seconds // 60}m{seconds % 60:02d}s"
    return f"~{seconds}s"


def estimate_lines(
    requests: int,
    *,
    interval: float,
    keyed_interval: float,
    has_key: bool,
) -> list[str]:
    """The pre-wait estimate (§1.3), shown on stderr before any request is spent.

    Both intervals are the client's own cadence (``PubMedClient.request_interval`` and
    ``PubMedClient.min_interval(has_api_key=True)``) — no rate constant is copied here, so
    the estimate can never drift from the pacing. When no key is configured, the line shows
    what the same run would cost *with* one, which is the concrete reason a key is worth
    configuring — this command is where that value becomes visible.
    """
    eta = format_eta(requests, interval)
    lines = [f"Refreshing retraction status for {requests} PMID(s)..."]
    if has_key:
        lines.append(f"Estimated time: {eta}")
    else:
        keyed = format_eta(requests, keyed_interval)
        lines.append(f"Estimated time: {eta} (would be {keyed} with an NCBI API key)")
    return lines
