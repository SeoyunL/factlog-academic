# SPDX-License-Identifier: Apache-2.0
"""Report-only (and, under ``--auto-update``, ledger-revising) refresh of the OpenAlex
records a KB already holds (issue #83).

## What this does, and what it deliberately does not

For every OpenAlex record in a KB's provenance ledgers (``<kb>/source-provenance/*.json``)
this re-fetches the work by id (``GET /works/{id}``, **0 credits**, #51) and compares
the live metadata with what the ledger recorded. It **reports** divergences. Without
``--auto-update`` it writes nothing but the check-log's last-checked timestamps.

It is the counterpart of ``arxiv-check-versions`` (#78/#79) for OpenAlex, and it exists
because #73 had to leave ``OpenAlexSourceWriter._IDENTIFYING_FIELDS`` empty: with no
refresh command, an identifying field that drifted would raise a permanently
unclearable per-id error. This command is the legitimate ``update_source`` caller that
was missing.

## What a refresh compares (Decision 1)

Exactly the fields the ledger stores (``source_writer._provenance_record``):
``doi``, ``work_type``, ``journal`` and ``is_retracted``. ``cited_by_count`` is **not**
in the ledger (#84, a volatile metric) so it cannot become a divergence *structurally*
— #73's trap is closed by construction, not by discipline.

## ``--auto-update`` writes only three fields (Decision 2 / H1)

``AUTO_UPDATE_FIELDS = (doi, work_type, journal)`` — the mutable venue/identifier facts
a refresh actually learned. ``is_retracted`` is **copied through verbatim, never
rewritten**: writing it would flip "newly retracted" to false on the next run and
silence the human-gate signal (the same answer arXiv's ``withdrawn_by`` gets in #79;
#93 owns the shared acknowledge design). The original ``.md`` is never opened (P4 holds
byte- and ``mtime_ns``-identical), no other ledger field moves, ``imported_at`` is
preserved, and a no-op run is byte-identical.

## Retraction stays OpenAlex's opinion, and stays loud (Decision 3)

Surfaced under **both** modes, attributed to OpenAlex, never as a bare ``retracted:``
claim — OpenAlex flags the Lancet Commission dementia report as retracted while PubMed
records no retraction (#51). It is a different process from an arXiv preprint being
pulled by its authors; there is no shared downstream handling.

## An id that comes back different is reported, never followed (H3)

``get_work`` follows redirects and OpenAlex merges works, so a request for ``W_a`` can
return a work whose ``id`` is ``W_b``. That is an identity change, not a field change.
The refresh compares the returned ``id`` against the requested one, reports
``id superseded`` as its own signal, and ``--auto-update`` never rewrites the
``(type, id)`` key. (This case was *not* reproduced; it is a guard against an unverified
failure, not a measured one.)

## Failure isolation — guard BOTH the read and the write (Decision 6)

A corrupt ledger is that source's per-id error; a ``NotFound`` is that paper's per-id
error; an unwritable ``source-provenance/`` is that paper's per-id error. The per-id
write loop guards **both** ``read_provenance`` and ``write_provenance`` with
``(ProvenanceError, OSError)`` — #94 guarded only the read and shipped a batch crash,
because ``write_provenance`` (and its ``mkdir``) re-raise ``OSError``.

## A paper with no ledger is not given one (Decision 7 / #79)

Only pre-#84 OpenAlex imports are front-matter-only. Creating a ledger is an *import*'s
write (it invents ``imported_at`` and the provenance origin, which a refresh does not
know). Such a paper is reported ``NO_LEDGER`` and named the command that fixes it — but
it is still *read*, so a newly-appeared retraction on it surfaces.

## Determinism and the filesystem contract

This reads ``source-provenance/*.json`` and the KB-level check-log, and — via the caller
— writes only ``<kb>/check-log/openalex.json`` (report-only) plus, under
``--auto-update``, the ledgers. No original ``.md`` is opened for writing. Results are
produced in ``openalex_id`` order so output is reproducible.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from factlog.integrations.common.front_matter import read_scalars
from factlog.integrations.common.provenance import (
    SIDECAR_DIR,
    ProvenanceError,
    SourceRecord,
    backfill_remedy,
    excluded_reason,
    excluded_sources_by_id,
    provenance_sources,
    read_provenance,
    update_source,
    write_provenance,
)
from factlog.integrations.openalex.api_client import (
    OpenAlexConnectionError,
    OpenAlexError,
    OpenAlexNotFoundError,
    OpenAlexRateLimitError,
)
from factlog.integrations.openalex.check_log import CheckLog
from factlog.integrations.openalex.work_parser import parse_work

__all__ = [
    "LedgerEntry",
    "RefreshCheck",
    "LedgerRefresh",
    "STATUS_UNCHANGED",
    "STATUS_CHANGED",
    "STATUS_ERROR",
    "STATUS_SKIPPED",
    "UPDATE_WRITTEN",
    "UPDATE_UNCHANGED",
    "UPDATE_NO_LEDGER",
    "UPDATE_ID_SUPERSEDED",
    "UPDATE_ERROR",
    "AUTO_UPDATE_FIELDS",
    "COMPARED_FIELDS",
    "RETRACTION_KEY",
    "collect_ledger_entries",
    "excluded_checks",
    "parse_retraction_flag",
    "provenance_of",
    "partition_by_freshness",
    "check_entries",
    "summarize",
    "apply_auto_update",
    "retraction_note",
    "un_retraction_note",
    "report_lines",
    "porcelain_lines",
]

#: A provenance record's ``type`` for an OpenAlex contribution.
_OPENALEX_TYPE = "openalex"

#: The front-matter key ``OpenAlexSourceWriter`` emits for OpenAlex's retraction claim.
#: Source-scoped on purpose (#51): never a bare ``retracted:``.
RETRACTION_KEY = "openalex_is_retracted"

#: The boolean words :func:`parse_retraction_flag` recognises, matched case-insensitively.
#: YAML 1.2's core schema, and the only values ``OpenAlexSourceWriter`` ever emits. Widening
#: this to YAML 1.1's ``yes``/``on`` is a one-line change *here*, and every caller moves with
#: it — which is the whole reason the literal lives in exactly one place.
_RETRACTION_TRUE = "true"
_RETRACTION_FALSE = "false"

#: The venue/identifier fields a refresh compares and may learn. ``is_retracted`` is
#: compared too (below) but is a human-gate signal, never an auto-updatable field.
COMPARED_FIELDS = ("doi", "work_type", "journal")

STATUS_UNCHANGED = "unchanged"
STATUS_CHANGED = "changed"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"


@dataclass(frozen=True)
class LedgerEntry:
    """One OpenAlex work the KB records, gathered from its provenance ledger(s) or
    (pre-#84) its source front matter.

    ``recorded_*`` are what the ledger/front matter holds — the values a refresh
    measures the live work against. ``recorded_is_retracted`` is what a *newly*-set
    retraction is measured against. ``sources`` are the ledger-relative paths that
    reference the work (a ``source-provenance/*.json`` for a ledger-backed paper, a
    ``sources/*.md`` for a front-matter-only one).
    """

    openalex_id: str
    recorded_doi: str | None = None
    recorded_work_type: str | None = None
    recorded_journal: str | None = None
    recorded_is_retracted: bool = False
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class RefreshCheck:
    """The outcome of refreshing one work (or one corrupt ledger).

    ``status`` is one of the ``STATUS_*`` constants; it is :data:`STATUS_CHANGED` when
    any of :data:`COMPARED_FIELDS` diverged or the id was superseded. ``newly_retracted``,
    ``un_retracted`` and ``id_superseded`` are orthogonal flags surfaced regardless of
    ``status``: OpenAlex can flag (or unflag) a retraction with no field change, and a
    merged id is an identity change, not a field change. ``newly_retracted`` is a *value*
    comparison against the record, not a presence test — OpenAlex now flags a retraction
    the record did not carry; ``un_retracted`` is its mirror — OpenAlex has *reversed* a
    retraction the record still holds, so the record's flag should be cleared. The two are
    mutually exclusive. ``changed_fields`` names which of ``doi``/``work_type``/``journal``
    differ. ``returned_id`` is the id OpenAlex actually answered with (equal to
    ``openalex_id`` unless the work was merged upstream). ``recorded_from`` says where the
    recorded value came from — ``"ledger"`` (a provenance sidecar) or ``"front-matter"``
    (a pre-#84 work with no ledger) — so a note never claims "the ledger recorded" a value
    that came from front matter. For an error result the ``openalex_id`` may instead be a
    ledger path, and ``reason`` explains it.
    """

    openalex_id: str
    status: str
    returned_id: str | None = None
    recorded_doi: str | None = None
    current_doi: str | None = None
    recorded_work_type: str | None = None
    current_work_type: str | None = None
    recorded_journal: str | None = None
    current_journal: str | None = None
    recorded_is_retracted: bool = False
    current_is_retracted: bool = False
    newly_retracted: bool = False
    un_retracted: bool = False
    id_superseded: bool = False
    changed_fields: tuple[str, ...] = ()
    recorded_from: str = "ledger"
    reason: str = ""
    sources: tuple[str, ...] = ()


def _relative(path: Path, kb_root: Path) -> str:
    try:
        return str(path.relative_to(kb_root))
    except ValueError:
        return str(path)


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_retraction_flag(raw: str) -> bool | str:
    """What a front-matter ``openalex_is_retracted`` scalar says, or the scalar itself.

    Front matter has no booleans. ``front_matter.read_scalars`` is a line reader, not a YAML
    parser — it strips one optional layer of double quotes — so what reaches here is always a
    string, and ``openalex_is_retracted: true`` and ``: "true"`` are the *same document*.

    Returns ``True`` or ``False`` for a value this tool can read as a boolean, and the raw
    text **verbatim** for one it cannot. An absent key (``read_scalars`` yields ``""``) and an
    empty value are ``False``: they carry no claim, and the writer emits the key only when
    OpenAlex flags a retraction, so silence *means* not retracted. ``: ""`` cannot be
    distinguished from an absent key by ``_key_pattern`` (its capture group needs at least one
    non-quote character), and ``: ''`` reaches here as the two-character token ``''``; both
    are empty once unquoted, and both mean absence.

    The two callers must never disagree about a value. ``collect_ledger_entries`` narrows the
    result to a ``bool`` for the comparison a refresh needs, so a value this function will not
    read as a boolean is compared as *not retracted* and silently surfaces nothing. A backfill
    (#115) cannot narrow it: promoting an unreadable value into the ledger would either assert
    a retraction no source made or assert the absence of one the ``.md`` was trying to state,
    so it passes the verbatim text through and ``common/backfill.py`` refuses that paper.

    Both read this one function on purpose. Were the boolean words written down twice, widening
    one copy to YAML 1.1's ``yes``/``on`` would make ``openalex-refresh`` report a paper
    retracted that ``openalex-backfill-provenance`` still refuses a ledger — and a retraction
    that can never be acknowledged repeats forever, which is the failure #105 exists to end.
    That is #64, #98 and #111 in their exact shape.
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
    """Gather every OpenAlex record from ``<kb>/source-provenance/**/*.json`` and every
    pre-#84 front-matter-only OpenAlex source.

    Returns ``(entries, errors)``: the deduplicated works to refresh, and a per-file
    ``error`` :class:`RefreshCheck` for each ledger that would not parse. A corrupt
    ledger is *that source's* problem — it never aborts the enumeration (#65/#71/#94).

    One work may be referenced by several ledgers; they collapse to one entry keyed by
    ``openalex_id``, keeping the first non-empty value of each compared field, OR-ing
    the retraction flag, and retaining every referencing source path. A ledger, when
    present, is authoritative; front matter speaks only for a work no ledger covers.
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
                    openalex_id=_relative(path, root),
                    status=STATUS_ERROR,
                    reason=f"corrupt provenance ledger: {exc}",
                )
            )
            continue
        rel = _relative(path, root)
        for record in provenance.records:
            if record.type != _OPENALEX_TYPE:
                continue
            slot = slots.setdefault(record.id, _empty_slot())
            _fold(slot, {
                "doi": _str_or_none(record.fields.get("doi")),
                "work_type": _str_or_none(record.fields.get("work_type")),
                "journal": _str_or_none(record.fields.get("journal")),
                "is_retracted": record.fields.get("is_retracted") is True,
            })
            slot["sources"].add(rel)

    # A paper imported before #84 has front matter but no ledger. Reading only the
    # ledgers would answer "no OpenAlex records" for most of such a library — silently
    # wrong about all of its input. The front matter carries `openalex_id`, `type`
    # (the work type), `doi`, `journal` and `openalex_is_retracted`, which is what a
    # compare needs, and reading it writes nothing.
    # The KB's own enumeration (`rglob` under `sources/`, hidden paths excluded), not a
    # flat `glob` that would leave a nested paper out of this command's denominator while
    # `factlog sources` lists it (#112). A source outside the provenance root is reported
    # by `excluded_checks`, never dropped.
    for path in provenance_sources(root):
        scalars = read_scalars(
            path, ("openalex_id", "type", "doi", "journal", RETRACTION_KEY)
        )
        openalex_id = scalars.get("openalex_id", "")
        # A ledger, when one exists, is authoritative; front matter only speaks for a
        # work no ledger covers.
        if not openalex_id or openalex_id in slots:
            continue
        slots[openalex_id] = {
            "doi": scalars.get("doi") or None,
            "work_type": scalars.get("type") or None,
            "journal": scalars.get("journal") or None,
            # A compare needs a bool, so a value `parse_retraction_flag` will not read as
            # one is compared as "not retracted" — this command reports, it does not judge
            # the `.md`. The backfill shares the parser and refuses that value instead of
            # narrowing it; see `parse_retraction_flag` for why the two must not disagree.
            "is_retracted": parse_retraction_flag(scalars.get(RETRACTION_KEY, "")) is True,
            "sources": {_relative(path, root)},
        }

    entries = [
        LedgerEntry(
            openalex_id=openalex_id,
            recorded_doi=slot["doi"],
            recorded_work_type=slot["work_type"],
            recorded_journal=slot["journal"],
            recorded_is_retracted=slot["is_retracted"],
            sources=tuple(sorted(slot["sources"])),
        )
        for openalex_id, slot in slots.items()
    ]
    entries.sort(key=lambda e: e.openalex_id)
    errors.sort(key=lambda e: e.openalex_id)
    return entries, errors


def excluded_checks(kb_root: Path | str) -> list[RefreshCheck]:
    """One ``error`` :class:`RefreshCheck` per OpenAlex work named only by a source outside
    the provenance root, so no ledger can exist for it (#112).

    Keyed by **openalex_id**, not by path: the id is what every other row of this report and
    of ``--porcelain`` carries in that column, and what ``openalex-acknowledge-retraction
    --id`` takes. The paths go to the ``sources`` column and the reason.

    Deliberately a separate channel from :func:`collect_ledger_entries`' errors, which
    ``common/backfill.py`` treats as a poison that stops every write (#111): an unreadable
    ledger contaminates the front-matter-only classification, while an excluded source
    contaminates nothing. It is an *error* rather than a note because a retraction on this
    work will never be reported, and a command that exits 0 while that is true is the
    silent direction #112 closes. The arXiv twin is ``check_versions.excluded_checks``.
    """
    root = Path(kb_root)
    remedy = backfill_remedy("openalex-backfill-provenance")
    return [
        RefreshCheck(
            openalex_id=openalex_id,
            status=STATUS_ERROR,
            reason=excluded_reason(", ".join(refs), remedy),
            sources=refs,
        )
        for openalex_id, refs in sorted(excluded_sources_by_id(root, "openalex_id").items())
    ]


def _empty_slot() -> dict:
    return {"doi": None, "work_type": None, "journal": None, "is_retracted": False,
            "sources": set()}


def _fold(slot: dict, incoming: dict) -> None:
    for key in COMPARED_FIELDS:
        if slot[key] is None and incoming[key] is not None:
            slot[key] = incoming[key]
    if incoming["is_retracted"]:
        slot["is_retracted"] = True


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
    check_log: CheckLog,
    older_than_days: float,
    now: datetime,
) -> tuple[list[LedgerEntry], list[RefreshCheck]]:
    """Split *entries* into (to check now, skipped as recently checked).

    Freshness is read **only from the check-log** — never from a ``stat`` on the source
    files — so this touches nothing under ``sources/``. A work is skipped when its
    recorded ``last_checked_at`` is newer than ``now - older_than_days``. A work the
    check-log has never seen, or whose timestamp will not parse, is always checked
    (fail-open). ``GET /works/{id}`` costs 0 credits, so ``--older-than`` is a
    courtesy/reporting knob, not a budget one.
    """
    cutoff = now - timedelta(days=older_than_days)
    to_check: list[LedgerEntry] = []
    skipped: list[RefreshCheck] = []
    for entry in entries:
        record = check_log.entries.get(entry.openalex_id)
        last = _parse_iso(record.last_checked_at) if record else None
        if last is not None and last > cutoff:
            skipped.append(
                RefreshCheck(
                    openalex_id=entry.openalex_id,
                    status=STATUS_SKIPPED,
                    returned_id=entry.openalex_id,
                    recorded_doi=entry.recorded_doi,
                    recorded_work_type=entry.recorded_work_type,
                    recorded_journal=entry.recorded_journal,
                    recorded_is_retracted=entry.recorded_is_retracted,
                    reason=f"checked at {record.last_checked_at}",
                    sources=entry.sources,
                )
            )
        else:
            to_check.append(entry)
    return to_check, skipped


def _diff(entry: LedgerEntry, parsed) -> RefreshCheck:
    current_doi = parsed.doi
    current_work_type = parsed.work_type
    current_journal = parsed.journal
    current_is_retracted = bool(parsed.openalex_is_retracted)
    # `get_work` follows redirects; a merged work answers under a different id (H3).
    id_superseded = parsed.openalex_id != entry.openalex_id
    changed_fields = tuple(
        name
        for name, recorded, current in (
            ("doi", entry.recorded_doi, current_doi),
            ("work_type", entry.recorded_work_type, current_work_type),
            ("journal", entry.recorded_journal, current_journal),
        )
        if recorded != current
    )
    # Retraction is measured against the record's own value, independent of any field
    # change: OpenAlex can flag (or unflag) a retraction with no venue/id change at all.
    #
    # This is a *value* comparison, not a presence test (mirrors arXiv #100). The old
    # `current and not recorded` form still names a fresh retraction, but on its own it
    # silently loses the *reverse*: an *un-retraction* (recorded True, upstream now False)
    # never surfaced, so a retraction OpenAlex has since reversed stayed recorded forever with
    # no way to learn it. Comparing the values surfaces both directions: a retraction the
    # record did not carry is `newly_retracted`, and a retraction the record holds that
    # OpenAlex no longer flags is `un_retracted`.
    newly_retracted = current_is_retracted and not entry.recorded_is_retracted
    un_retracted = (not current_is_retracted) and entry.recorded_is_retracted
    status = STATUS_CHANGED if (changed_fields or id_superseded) else STATUS_UNCHANGED
    return RefreshCheck(
        openalex_id=entry.openalex_id,
        status=status,
        returned_id=parsed.openalex_id,
        recorded_doi=entry.recorded_doi,
        current_doi=current_doi,
        recorded_work_type=entry.recorded_work_type,
        current_work_type=current_work_type,
        recorded_journal=entry.recorded_journal,
        current_journal=current_journal,
        recorded_is_retracted=entry.recorded_is_retracted,
        current_is_retracted=current_is_retracted,
        newly_retracted=newly_retracted,
        un_retracted=un_retracted,
        id_superseded=id_superseded,
        changed_fields=changed_fields,
        recorded_from=provenance_of(entry.sources),
        sources=entry.sources,
    )


def check_entries(
    entries: Sequence[LedgerEntry],
    client,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> list[RefreshCheck]:
    """Fetch each work by id (``GET /works/{id}``, 0 credits) and diff it against the
    ledger. Results are keyed by the **requested** id, never response order.

    A ``NotFound`` (the work was deleted/merged out of existence upstream) is a per-id
    ``error``, never a crash — the analogue of arXiv's ``missing`` list. A per-id API or
    payload problem is likewise isolated. Connection and rate-limit failures propagate
    (the caller decides the exit code); they cannot be trusted as partial.

    ``progress`` is called with ``(works_done, works_total)`` after each work so the
    caller can show progress on stderr.
    """
    results: list[RefreshCheck] = []
    total = len(entries)
    for index, entry in enumerate(entries, 1):
        try:
            raw = client.get_work(entry.openalex_id)
            parsed = parse_work(raw)
        except OpenAlexNotFoundError:
            results.append(
                RefreshCheck(
                    openalex_id=entry.openalex_id,
                    status=STATUS_ERROR,
                    reason="OpenAlex has no record for this id (deleted or merged away)",
                    recorded_doi=entry.recorded_doi,
                    recorded_work_type=entry.recorded_work_type,
                    recorded_journal=entry.recorded_journal,
                    recorded_is_retracted=entry.recorded_is_retracted,
                    sources=entry.sources,
                )
            )
        except (OpenAlexConnectionError, OpenAlexRateLimitError):
            # Transport / budget failure: not a per-id problem, and not trustable
            # partial. Let the caller abort with the right exit code.
            raise
        except OpenAlexError as exc:
            # A single work's API/payload problem is that work's problem, not the
            # batch's — the failure-isolation theme of this whole track.
            results.append(
                RefreshCheck(
                    openalex_id=entry.openalex_id,
                    status=STATUS_ERROR,
                    reason=str(exc),
                    recorded_doi=entry.recorded_doi,
                    recorded_work_type=entry.recorded_work_type,
                    recorded_journal=entry.recorded_journal,
                    recorded_is_retracted=entry.recorded_is_retracted,
                    sources=entry.sources,
                )
            )
        else:
            results.append(_diff(entry, parsed))
        if progress is not None:
            progress(index, total)
    results.sort(key=lambda r: r.openalex_id)
    return results


@dataclass
class Summary:
    """Tallies over a run's results, for the human and porcelain footers."""

    checked: int = 0
    unchanged: int = 0
    changed: int = 0
    retracted: int = 0
    un_retracted: int = 0
    superseded: int = 0
    errors: int = 0
    skipped: int = 0


def summarize(
    results: Iterable[RefreshCheck], skipped: Sequence[RefreshCheck]
) -> Summary:
    """Count outcomes. ``retracted`` counts newly-retracted works and ``un_retracted``
    counts works OpenAlex no longer flags but the record still does, across every checked
    result whatever their field status; ``superseded`` counts id changes. ``checked``
    excludes skipped."""
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
        if result.id_superseded:
            summary.superseded += 1
    return summary


# --------------------------------------------------------------------------- #
# --auto-update: the OpenAlex sibling of arxiv-check-versions' update_source caller
# --------------------------------------------------------------------------- #

#: The exact fields ``--auto-update`` may rewrite in an OpenAlex ledger record. The
#: whole of the narrow contract (Decision 2): the venue/identifier facts a refresh
#: learned. It never includes ``is_retracted`` (H1 — see :func:`apply_auto_update`).
AUTO_UPDATE_FIELDS = ("doi", "work_type", "journal")

#: A ledger was rewritten (a venue/id field genuinely moved).
UPDATE_WRITTEN = "updated"
#: The three fields already matched the ledger — nothing was written, so the file stays
#: byte- and ``mtime_ns``-identical.
UPDATE_UNCHANGED = "unchanged"
#: The work is known only from front matter (imported before #84), so there is no
#: ledger to update. ``--auto-update`` does **not** create one.
UPDATE_NO_LEDGER = "no-ledger"
#: The returned id differed from the requested one (H3). The ledger key is never
#: rewritten by a refresh; the divergence is reported for a human/import path.
UPDATE_ID_SUPERSEDED = "id-superseded"
#: A ledger could not be read or written while updating it — that work's problem,
#: reported per-id, never a batch crash.
UPDATE_ERROR = "error"


@dataclass(frozen=True)
class LedgerRefresh:
    """What ``--auto-update`` did (or declined to do) for one work.

    ``status`` is one of the ``UPDATE_*`` constants. ``ledgers`` names the sidecars
    actually rewritten (empty unless :data:`UPDATE_WRITTEN`). ``fields`` names the
    compared fields that moved. ``reason`` explains a non-write outcome.
    """

    openalex_id: str
    status: str
    ledgers: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    reason: str = ""


def _refreshed_fields(existing: SourceRecord, result: RefreshCheck) -> dict:
    """The record's fields with *only* the three venue/id values replaced.

    ``is_retracted`` is copied through **verbatim** (H1): a refresh never writes it, so
    a retraction keeps surfacing until a human records it. A ``None`` incoming value
    (the DOI/journal disappeared upstream) is written as ``None`` and dropped on
    serialization by :meth:`SourceRecord.to_dict`, so the ledger reflects the current
    upstream state rather than freezing a stale one. Everything else — ``imported_at``
    (top level, untouched) and any co-resident non-OpenAlex record — is left alone.
    """
    fields = dict(existing.fields)
    fields["doi"] = result.current_doi
    fields["work_type"] = result.current_work_type
    fields["journal"] = result.current_journal
    return fields


def apply_auto_update(
    results: Sequence[RefreshCheck],
    kb_root: Path | str,
) -> list[LedgerRefresh]:
    """Write the three venue/id fields (:data:`AUTO_UPDATE_FIELDS`) of each checked work
    into its provenance ledger(s), and nothing else.

    The OpenAlex sibling of ``arxiv-check-versions --auto-update``, and the second
    legitimate caller of :func:`provenance.update_source`. The narrow contract:

    * Only ``doi``, ``work_type`` and ``journal`` are rewritten. ``is_retracted`` is
      copied verbatim (H1); every other ledger field, every non-OpenAlex record in the
      same ledger, and the original ``.md`` are untouched (the ``.md`` is never opened,
      so it stays byte- and ``mtime_ns``-identical).
    * A work whose three fields already match is a **byte-identical no-op**.
    * A **retraction is surfaced but never absorbed** (H1).
    * A **superseded id is reported, never followed** (H3): the ``(type, id)`` key is
      never rewritten by a refresh.
    * A **front-matter-only work gets no ledger** (Decision 7): reported
      :data:`UPDATE_NO_LEDGER`, left for a re-import to give it a ledger.
    * A ledger that will not read **or write** is that work's problem — a per-id
      :data:`UPDATE_ERROR`, never a batch crash (#94 guarded only the read).

    Error results are ignored (nothing to write). Returns one :class:`LedgerRefresh`
    per work acted on, in ``openalex_id`` order.
    """
    root = Path(kb_root)
    outcomes: list[LedgerRefresh] = []
    for result in results:
        if result.status == STATUS_ERROR:
            continue

        # H3: the returned work is a *different* work than the ledger key names. Never
        # write its fields under the old key, and never rewrite the key itself.
        if result.id_superseded:
            outcomes.append(
                LedgerRefresh(
                    openalex_id=result.openalex_id,
                    status=UPDATE_ID_SUPERSEDED,
                    reason=(
                        f"OpenAlex answered under a different id {result.returned_id!r} "
                        "(the work was merged upstream). A refresh reports an identity "
                        "change but never rewrites the ledger key; re-import to follow it."
                    ),
                )
            )
            continue

        # A work spoken for only by `sources/*.md` has no ledger (imported before #84).
        sidecars = [s for s in result.sources if not str(s).startswith("sources/")]
        if not sidecars:
            outcomes.append(
                LedgerRefresh(
                    openalex_id=result.openalex_id,
                    status=UPDATE_NO_LEDGER,
                    reason=(
                        "imported before #84: front matter only, no provenance ledger "
                        "to update. Run `factlog openalex-import --work-id "
                        f"{result.openalex_id}` to create one; --auto-update will not "
                        "fabricate an import record."
                    ),
                )
            )
            continue

        written: list[str] = []
        moved: set[str] = set()
        errors: list[str] = []
        for rel in sidecars:
            path = root / rel
            # Reading AND writing are that work's problem. Guarding only the read is how
            # #94 shipped a batch crash: `write_provenance` re-raises `OSError`, and its
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
                    if r.type == _OPENALEX_TYPE and r.id == result.openalex_id
                ),
                None,
            )
            if existing is None:
                # Named at collection but no longer carries the record: nothing to do.
                continue
            record = SourceRecord(
                type=existing.type,
                id=existing.id,
                imported_at=existing.imported_at,
                fields=_refreshed_fields(existing, result),
            )
            # A no-op must not touch the file: compare serialized forms so an unchanged
            # work leaves the ledger byte- and mtime_ns-identical.
            if record.to_dict() == existing.to_dict():
                continue
            try:
                update_source(provenance, record)
                write_provenance(path, provenance)
            except (ProvenanceError, OSError) as exc:
                errors.append(f"{rel}: {exc}")
                continue
            written.append(rel)
            moved.update(result.changed_fields)

        if errors:
            # Any failure is an error, even when a sibling ledger was written; only the
            # error status reaches the exit code.
            outcomes.append(
                LedgerRefresh(
                    openalex_id=result.openalex_id,
                    status=UPDATE_ERROR,
                    ledgers=tuple(sorted(written)),
                    fields=tuple(sorted(moved)),
                    reason="; ".join(errors),
                )
            )
        elif written:
            outcomes.append(
                LedgerRefresh(
                    openalex_id=result.openalex_id,
                    status=UPDATE_WRITTEN,
                    ledgers=tuple(sorted(written)),
                    fields=tuple(sorted(moved)),
                )
            )
        else:
            outcomes.append(
                LedgerRefresh(
                    openalex_id=result.openalex_id,
                    status=UPDATE_UNCHANGED,
                )
            )

    outcomes.sort(key=lambda u: u.openalex_id)
    return outcomes


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def retraction_note(result: RefreshCheck) -> str:
    """The prominent, OpenAlex-attributed retraction line for a newly-retracted work.

    Never a bare ``retracted:`` claim: it names OpenAlex as the source, notes PubMed may
    disagree, and does not describe it as an arXiv preprint being pulled by its authors
    (a different process, no shared handling). It attributes the recorded value to its
    true source — a ledger, or a pre-#84 work's front matter — so it never says "the
    ledger did not record" a value that came from front matter.

    For a **front-matter**-only work (imported before #84) there is no provenance ledger,
    so ``openalex-acknowledge-retraction`` — which writes a sidecar — cannot record the
    operator's decision and would exit 1. The warning must stay loud (a retraction the KB
    never recorded is real news), but a loud warning that prescribes nothing is the exact
    wallpaper #93 exists to remove, so the note adds that the ledger is missing, that the
    retraction cannot be acknowledged until one exists, and prescribes
    ``openalex-backfill-provenance`` (#115), which builds one — not a command that would
    exit 1. The word stays OpenAlex's opinion throughout.
    """
    where = "front matter" if result.recorded_from == "front-matter" else "ledger"
    # Build the shared body once, so the two branches cannot drift. Restating it would
    # let an edit to one silently leave the other behind: only the ledger string is
    # pinned byte-for-byte by a test, so updating that literal would never reveal that
    # the front-matter note kept the old prose. `where` is what makes the body correct
    # for both — it says "front matter" or "ledger" according to the value's real source.
    body = (
        f"OpenAlex now flags {result.openalex_id} as RETRACTED, which the {where} did "
        "not record. This is OpenAlex's opinion — it has false positives, and PubMed "
        "(which owns retraction status) may disagree, as with the Lancet Commission "
        "dementia report. It is a different process from an arXiv preprint being pulled "
        "by its authors, with no shared handling. Confirm before trusting any claim "
        "from this work."
    )
    if result.recorded_from == "front-matter":
        return (
            f"{body} This work has no provenance ledger (imported before #84), so the "
            "retraction cannot be acknowledged and will keep surfacing until one exists; "
            "run `factlog openalex-backfill-provenance` to give it one."
        )
    return body


def un_retraction_note(result: RefreshCheck, *, prescribe: bool = True) -> str:
    """The line for a work OpenAlex no longer flags as retracted but the record still does.

    This is **not** a retraction warning: a retraction being reversed is its own news, and
    the word stays OpenAlex's opinion throughout.

    Unlike arXiv's ``withdrawn_by``, ``is_retracted`` is **not** an identifying field
    (``openalex/source_writer.py``): a stale retraction never makes a re-import error in
    either direction, so this note must not claim a divergence or a re-import error. The
    clear path exists anyway — a retraction can be reversed and the ledger must be able to
    say so. For a **ledger**-recorded value the note prescribes the acknowledge command;
    for a **front-matter**-only work (imported before #84) there is no sidecar to write, so
    it prescribes ``openalex-backfill-provenance`` (#115), which creates one, instead of a
    command that would exit 1.
    """
    if result.recorded_from == "front-matter":
        return (
            f"OpenAlex no longer flags {result.openalex_id} as retracted, but its front "
            "matter still records a retraction. This work has no provenance ledger "
            "(imported before #84); `is_retracted` is not an identifying field, so nothing "
            "diverges and a re-import does not error — the front-matter flag is simply "
            "stale. Run `factlog openalex-backfill-provenance` to give it a ledger, after "
            "which this can be acknowledged."
        )
    note = (
        f"OpenAlex no longer flags {result.openalex_id} as retracted, but the ledger still "
        "records a retraction. `is_retracted` is not an identifying field, so nothing "
        "diverges and a re-import does not error — but a reversed retraction should not "
        "keep surfacing, and the ledger should record that it was reversed."
    )
    if not prescribe:
        # The acknowledge command prints this note itself; telling the operator to run the
        # command they are already running is noise (#107 item 7).
        return note
    return (
        f"{note} Run `factlog openalex-acknowledge-retraction "
        f"--id {result.openalex_id}` to clear it."
    )


def _sources_suffix(result: RefreshCheck) -> str:
    return f"  (sources: {', '.join(result.sources)})" if result.sources else ""


def provenance_of(sources) -> str:
    """``"front-matter"`` if *sources* are all ``sources/*.md`` (a pre-#84 work with no
    ledger), else ``"ledger"``. One work's sources are never mixed:
    ``collect_ledger_entries`` fills a slot from the ledger *or*, only if no ledger covered
    it, from front matter — never both."""
    sources = sources or ()
    if sources and all(str(s).startswith("sources/") for s in sources):
        return "front-matter"
    return "ledger"


def _recorded_in(result: RefreshCheck) -> str:
    """Where the recorded values came from: a ledger, or a source's front matter."""
    return "front matter" if provenance_of(result.sources) == "front-matter" else "ledger"


def _field_change(name: str, recorded: str | None, current: str | None) -> str:
    was = recorded if recorded is not None else "(none)"
    now = current if current is not None else "(none)"
    return f"{name}: {was} -> {now}"


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
    updates: Sequence[LedgerRefresh] = (),
) -> list[str]:
    """The human-readable stdout report. Retractions lead, prominently, whatever the
    field outcome; then id supersedes; then field divergences; then per-id errors; then,
    under ``--auto-update``, what was written; then the tally."""
    total = len(results) + len(skipped)
    # `summary.checked`, not `len(results)`: `results` carries the per-file errors
    # (a corrupt ledger, a source outside the provenance root), and a paper this run
    # could not check has not been checked. `Checked 4 of 4` above `Errors: 1` was a
    # false statement about the run. The denominator keeps every record considered,
    # so the excluded paper is still counted, never dropped (#112).
    header = f"Checked {summary.checked} of {total} OpenAlex record(s) in KB: {target}"
    if skipped:
        header += (
            f"\n  ({len(skipped)} skipped: checked within the last "
            f"{_days(older_than_days)}. A retraction or a field change that appeared "
            "since their last check is NOT detected — OpenAlex only says so when asked. "
            "Run with --older-than 0 to force a re-check.)"
        )
    lines = [header]

    retracted = [r for r in results if r.newly_retracted]
    un_retracted = [r for r in results if r.un_retracted]
    superseded = [r for r in results if r.id_superseded]
    changed = [
        r
        for r in results
        if r.status == STATUS_CHANGED and r.changed_fields and not r.id_superseded
    ]
    errors = [r for r in results if r.status == STATUS_ERROR]

    if retracted:
        lines.append("\nNewly flagged as retracted by OpenAlex (its opinion; PubMed may disagree):")
        for result in retracted:
            lines.append(f"  ⚠ {retraction_note(result)}{_sources_suffix(result)}")

    if un_retracted:
        lines.append(
            "\nNo longer flagged as retracted (OpenAlex reversed a retraction this KB records):"
        )
        for result in un_retracted:
            lines.append(f"  ↺ {un_retraction_note(result)}{_sources_suffix(result)}")

    if superseded:
        lines.append("\nId superseded upstream (OpenAlex merged the work; not followed):")
        for result in superseded:
            lines.append(
                f"  ↔ {result.openalex_id}: OpenAlex now answers under "
                f"{result.returned_id}. The ledger key is left alone; re-import to "
                f"follow it.{_sources_suffix(result)}"
            )

    if changed:
        lines.append(
            "\nMetadata diverged (upstream now says X; the ledger recorded Y — a signal, "
            "not a verdict):"
        )
        for result in changed:
            details = "; ".join(
                _field_change(name, recorded, current)
                for name, recorded, current in (
                    ("doi", result.recorded_doi, result.current_doi),
                    ("work_type", result.recorded_work_type, result.current_work_type),
                    ("journal", result.recorded_journal, result.current_journal),
                )
                if name in result.changed_fields
            )
            lines.append(
                f"  ~ {result.openalex_id} ({_recorded_in(result)}): "
                f"{details}{_sources_suffix(result)}"
            )

    if errors:
        lines.append("\nCould not check:")
        for result in errors:
            lines.append(f"  ✗ {result.openalex_id}: {result.reason}{_sources_suffix(result)}")

    lines.extend(_auto_update_lines(updates))

    # Labels are padded to the widest ("Retracted (reversed):") so every count lands in one
    # column. The "reversed" label parallels "Retracted (new):" and, like it, is never a
    # bare `retracted:` claim (retraction stays OpenAlex's opinion, not a fact).
    lines.append("\nSummary:")
    lines.append(f"  {'Checked:':<22}{summary.checked}")
    lines.append(f"  {'Up to date:':<22}{summary.unchanged}")
    lines.append(f"  {'Metadata changed:':<22}{summary.changed}")
    lines.append(f"  {'Retracted (new):':<22}{summary.retracted}")
    lines.append(f"  {'Retracted (reversed):':<22}{summary.un_retracted}")
    lines.append(f"  {'Id superseded:':<22}{summary.superseded}")
    lines.append(f"  {'Errors:':<22}{summary.errors}")
    lines.append(f"  {'Skipped:':<22}{summary.skipped}")
    if updates:
        lines.append(
            f"  {'Ledgers updated:':<22}{sum(1 for u in updates if u.status == UPDATE_WRITTEN)}"
        )
    return lines


def _auto_update_lines(updates: Sequence[LedgerRefresh]) -> list[str]:
    """The ``--auto-update`` section: what was written, what already matched, what has
    no ledger, what was superseded, and any per-id write error. Empty when off."""
    if not updates:
        return []
    written = [u for u in updates if u.status == UPDATE_WRITTEN]
    no_ledger = [u for u in updates if u.status == UPDATE_NO_LEDGER]
    superseded = [u for u in updates if u.status == UPDATE_ID_SUPERSEDED]
    errors = [u for u in updates if u.status == UPDATE_ERROR]
    lines: list[str] = []
    if written:
        lines.append(
            "\nLedger updated (venue/identifier fields only — doi, work_type, journal; "
            "retraction is never written):"
        )
        for u in written:
            ledgers = f"  ({', '.join(u.ledgers)})" if u.ledgers else ""
            moved = ", ".join(u.fields) if u.fields else "-"
            lines.append(f"  ✎ {u.openalex_id}: recorded {moved}{ledgers}")
    if no_ledger:
        lines.append(
            "\nNot auto-updated (no ledger; front matter only, imported before #84 — "
            "run `factlog openalex-import --work-id <id>` to create one):"
        )
        for u in no_ledger:
            lines.append(f"  · {u.openalex_id}")
    if superseded:
        lines.append(
            "\nNot auto-updated (id superseded upstream; a refresh never rewrites the "
            "ledger key):"
        )
        for u in superseded:
            lines.append(f"  · {u.openalex_id}")
    if errors:
        lines.append("\nCould not auto-update:")
        for u in errors:
            lines.append(f"  ✗ {u.openalex_id}: {u.reason}")
    return lines


def _porcelain_field(text: str) -> str:
    """Neutralize a free-text field so it cannot shift the columns after it.

    ``reason`` interpolates an exception string, and an ``OSError``'s message carries a
    path — a path may contain a tab. ``un_retracted`` is appended after ``reason``, so an
    unescaped tab there silently moves the last column. Newlines would break the row.
    """
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ")


def porcelain_lines(
    results: Sequence[RefreshCheck],
    skipped: Sequence[RefreshCheck],
    summary: Summary,
    *,
    target: Path,
    updates: Sequence[LedgerRefresh] = (),
) -> list[str]:
    """The machine contract on stdout: one tab-separated ``check`` row per record, then
    — only under ``--auto-update`` — one ``update`` row per acted-on work, then tallies.
    Parse by the first field.

    ``check\t<id>\t<status>\t<returned_id>\t<changed_fields>\t<retracted>\t<superseded>\t<reason>\t<un_retracted>``
    with ``changed_fields`` comma-joined (empty for none), ``retracted``/``superseded``/
    ``un_retracted`` as ``0``/``1``. ``un_retracted`` distinguishes a work OpenAlex no
    longer flags (but a record still does) from an unchanged one, whose row is otherwise
    byte-identical — a count without an id is useless. It is appended last so a parser
    keying on the earlier fixed columns is unaffected. The ``update`` rows are
    ``update\t<id>\t<status>\t<fields>\t<ledgers>``. Progress stays on stderr only.
    """
    rows: list[str] = []
    for result in sorted([*results, *skipped], key=lambda r: r.openalex_id):
        rows.append(
            "check\t{id}\t{status}\t{returned}\t{changed}\t{retracted}\t{superseded}\t{reason}\t{un}".format(
                id=result.openalex_id,
                status=result.status,
                returned=result.returned_id or "",
                changed=",".join(result.changed_fields),
                retracted="1" if result.newly_retracted else "0",
                superseded="1" if result.id_superseded else "0",
                reason=_porcelain_field(result.reason),
                un="1" if result.un_retracted else "0",
            )
        )
    for u in updates:
        rows.append(
            "update\t{id}\t{status}\t{fields}\t{ledgers}".format(
                id=u.openalex_id,
                status=u.status,
                fields=",".join(u.fields),
                ledgers=",".join(u.ledgers),
            )
        )
    rows.append(f"checked\t{summary.checked}")
    rows.append(f"unchanged\t{summary.unchanged}")
    rows.append(f"changed\t{summary.changed}")
    rows.append(f"retracted\t{summary.retracted}")
    rows.append(f"un_retracted\t{summary.un_retracted}")
    rows.append(f"superseded\t{summary.superseded}")
    rows.append(f"errors\t{summary.errors}")
    rows.append(f"skipped\t{summary.skipped}")
    if updates:
        rows.append(f"updated\t{sum(1 for u in updates if u.status == UPDATE_WRITTEN)}")
    rows.append(f"target\t{target}")
    return rows


def format_eta(works: int, delay: float) -> str:
    """A human ETA for ``works`` sequential 0-credit lookups at ``delay`` seconds each."""
    seconds = int(round(works * max(delay, 0.0)))
    if seconds >= 60:
        return f"~{seconds // 60}m{seconds % 60:02d}s"
    return f"~{seconds}s"
