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
from factlog.integrations.arxiv.work_parser import (
    WITHDRAWN_BY_ADMIN,
    WITHDRAWN_BY_AUTHOR,
)
from factlog.integrations.common.front_matter import read_scalars
from factlog.integrations.common.porcelain import porcelain_field
from factlog.integrations.common.provenance import (
    SIDECAR_DIR,
    ProvenanceError,
    SourceRecord,
    backfill_remedy,
    excluded_reason,
    excluded_sources_by_id,
    provenance_sources,
    read_provenance,
    sidecar_path,
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
    "STATUS_NO_VERSION",
    "STATUS_ERROR",
    "STATUS_SKIPPED",
    "SIDECAR_ABSENT",
    "SIDECAR_READABLE",
    "SIDECAR_UNREADABLE",
    "UPDATE_WRITTEN",
    "UPDATE_UNCHANGED",
    "UPDATE_NO_LEDGER",
    "UPDATE_ERROR",
    "AUTO_UPDATE_FIELDS",
    "collect_ledger_entries",
    "excluded_checks",
    "provenance_of",
    "partition_by_freshness",
    "check_entries",
    "summarize",
    "apply_auto_update",
    "no_version_note",
    "no_ledger_remedy",
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
#: The ledger (or front matter) carries **no** ``version`` at all. This is a state of
#: its own, not a flavour of ``unchanged`` and not a flavour of ``changed`` (#121).
#:
#: It is not ``unchanged``, because nothing was compared: whatever arXiv now serves,
#: the paper was silently excluded from the one signal this command exists to produce,
#: and an operator reading ``Version changed: 0`` had no way to learn either that the
#: paper needed repair or that ``--auto-update`` would repair it.
#:
#: It is not ``changed`` either, and must never be made so: "version changed from None
#: to 7" is the ``vNone`` of #116 wearing a new costume — ``None`` is a Python value,
#: not a version the ledger ever recorded. An absent value gets a distinct signal
#: rather than being collapsed into a normal one.
STATUS_NO_VERSION = "no-version"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"

#: What a front-matter paper's provenance sidecar is. Three states, not a bool: a file
#: that exists but will not parse is **not** a file whose contents we may describe. The
#: bool this replaced said ``True`` for it, and the report went on to assert what the
#: unparsed ledger held ("it holds no arXiv record") and to prescribe a command that
#: measurably fails on it. #128 made this newly reachable, by teaching ``read_provenance``
#: to raise on a non-bool ``is_retracted`` and an out-of-vocabulary ``withdrawn_by``.
SIDECAR_ABSENT = "absent"
SIDECAR_READABLE = "readable"
SIDECAR_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class LedgerEntry:
    """One arXiv paper the KB records, gathered from its provenance ledger(s).

    ``recorded_version`` is the version the ledger holds (an ``int``; ``None`` when a
    ledger carries an arXiv record without one — a hand-edit or an externally produced
    ledger, since #113 no importer writes one). A ``None`` here is not a version and is
    never compared as one: it produces :data:`STATUS_NO_VERSION`. ``recorded_withdrawn_by``
    is the withdrawal agent the ledger recorded, or ``None`` if it was not recorded
    as withdrawn — this is what a *newly* withdrawn paper is measured against.
    ``sources`` are the ledger-relative paths that reference the paper (one paper
    can be cited by several sources), for the report only.

    ``sidecar_state`` only speaks for a paper whose ``sources`` are front matter: one of
    :data:`SIDECAR_ABSENT`, :data:`SIDECAR_READABLE` (it exists and holds no arXiv record
    — an OpenAlex-primary import echoing ``arxiv_id``) or :data:`SIDECAR_UNREADABLE` (it
    exists and will not parse). "The ledger holds no arXiv record", "there is no ledger"
    and "the ledger could not be read" are three different papers with three different
    remedies, and collapsing any of them is how a report ends up asserting the contents
    of a file it never parsed, or prescribing a command that does nothing (#116, #121).
    For a ledger-backed paper it is trivially :data:`SIDECAR_READABLE`.

    ``per_source`` is one :class:`LedgerEntry` per ``.md`` that carries this paper's id,
    each speaking for exactly *its own* front matter (#117). It is empty for a ledger-backed
    paper, whose record belongs to a *ledger* and to no ``.md``; a backfill never touches
    such a paper, and an empty tuple here is that fact, not a missing value (#111).

    Deduplication and source aggregation are different concerns. A **check** needs the
    paper once, so the fields above answer for it — and they are the *first* source in
    sorted-path order, verbatim, never a fold across sources. A **backfill** writes one
    sidecar *per ``.md``*, from that ``.md``'s own front matter, and reads ``per_source``.
    Two ``.md`` can legitimately carry one ``arxiv_id`` (an arXiv deposit, and an OpenAlex
    import echoing it as a cross-reference without ever emitting ``arxiv_version``), and
    collapsing them made the backfill's outcome depend on a *filename*: the arXiv-authored
    file, the only one able to supply ``version``, was never consulted when it lost the
    sort. Adding the per-source detail beside the aggregate — rather than folding the
    aggregate, or deriving it by a second walk — is what fixes the backfill while leaving
    every byte of the check's report where it was.
    """

    arxiv_id: str
    recorded_version: int | None
    recorded_withdrawn_by: str | None
    sources: tuple[str, ...] = ()
    sidecar_state: str = SIDECAR_READABLE
    per_source: tuple["LedgerEntry", ...] = ()


@dataclass(frozen=True)
class VersionCheck:
    """The outcome of checking one paper (or one corrupt ledger).

    ``status`` is one of the ``STATUS_*`` constants. ``newly_withdrawn`` is set
    whenever arXiv's current withdrawing agent **differs** from the one the ledger
    recorded (a *value* comparison, not presence — #100): a fresh withdrawal, and
    also a *re-withdrawal by a different agent* (an acknowledged ``author``
    withdrawal that arXiv administrators later pull), which a presence test
    (``recorded is None``) silenced. It is *independent* of ``status`` — a withdrawn
    paper whose version did not change is still ``unchanged`` but ``newly_withdrawn``.
    ``un_withdrawn`` is its mirror: arXiv no longer reports a withdrawal the ledger
    still records, so the paper has come back and the ledger must be cleared. The two
    are mutually exclusive. ``withdrawn_by`` carries arXiv's current agent
    (``"author"``/``"admin"``) when the paper is withdrawn now; ``recorded_withdrawn_by``
    carries what the ledger held, so the note can name both sides of a divergence.
    ``recorded_from`` says where that recorded value came from — ``"ledger"`` (a
    provenance sidecar) or ``"front-matter"`` (a pre-#82 paper with no ledger, #98) —
    so the notes never claim "the ledger recorded" a value that came from front matter,
    nor assert an identifying-field divergence for a paper that has no provenance record
    to diverge. For an error result the ``arxiv_id`` may instead be a ledger path (a
    corrupt file), and ``reason`` explains it.

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
    un_withdrawn: bool = False
    withdrawn_by: str | None = None
    recorded_withdrawn_by: str | None = None
    recorded_from: str = "ledger"
    reason: str = ""
    sources: tuple[str, ...] = ()
    #: See :class:`LedgerEntry`. Discriminates "the ledger holds no arXiv record" from
    #: "there is no ledger" and from "the ledger would not parse", for a
    #: ``recorded_from="front-matter"`` paper.
    sidecar_state: str = SIDECAR_READABLE


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

    The front-matter fallback walks the KB's own enumeration (``provenance_sources``:
    ``rglob`` under ``sources/``), so a nested paper is seen exactly as a top-level one is.
    It used to walk ``sources/*.md`` flat while the sidecars above were walked with
    ``rglob``, and that asymmetry made a paper ``factlog sources`` lists absent from
    ``checked N/N`` and its withdrawal never reported (#112). A source outside the
    provenance root is reported by :func:`excluded_checks`, never dropped.

    Several ``sources/**/*.md`` may likewise carry one ``arxiv_id``. The entry a check reads is
    the **first in sorted-path order**, unchanged by #117 and unfolded — a paper is checked
    once, and no value is invented for it that no single source recorded. Each source's own
    front matter is kept *beside* that answer, verbatim, in :attr:`LedgerEntry.per_source`,
    which is what a backfill reads. Dedup and source aggregation are separate concerns
    (#117), and keeping them separate is what lets the backfill stop depending on a
    filename without the check's report moving at all.
    """
    root = Path(kb_root)
    slots: dict[str, dict] = {}
    errors: list[VersionCheck] = []
    #: Ledger-relative paths of the sidecars that would not parse. A `.md` whose sidecar
    #: is in here falls through to the front-matter loop below, and must not be described
    #: as though the file had been read.
    unreadable: set[str] = set()

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
            unreadable.add(_relative(path, root))
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
    # front matter carries `arxiv_id`, `arxiv_version` (#60) and `arxiv_withdrawn_by`
    # (the arXiv writer emits it whenever it emits `arxiv_withdrawn: true`,
    # `source_writer.py:165-167`), which is exactly what a check needs, and reading it
    # writes nothing, so report-only holds. A ledger, when present, is authoritative:
    # it is what a refresh updates.
    #: One view per `.md`, keyed by id, in sorted-path order. A second `.md` carrying an id
    #: is *not* dropped here (it was, before #117): it is a source of the same paper, and a
    #: backfill writes a sidecar next to each `.md`, from that `.md`'s own front matter.
    per_source: dict[str, list[LedgerEntry]] = {}
    # `provenance_sources` is the KB's own enumeration (rglob, hidden paths excluded),
    # narrowed to the one root a sidecar can describe. It replaces the flat
    # `sources_dir.glob("*.md")` that made a nested paper invisible to this command while
    # `factlog sources` listed it (#112). The papers it *cannot* cover are not dropped:
    # `excluded_checks` reports each one.
    for path in provenance_sources(root):
        scalars = read_scalars(path, ("arxiv_id", "arxiv_version", "arxiv_withdrawn_by"))
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
        # A paper imported while already withdrawn recorded the agent in its front
        # matter; reading it back is what keeps `_diff` from re-reporting that
        # withdrawal as new on every run (#98). `read_scalars` strips and omits an
        # empty value, so `or None` makes both absent and `arxiv_withdrawn_by: ""`
        # read as None rather than "" — line 320 tests `is None`, and "" would
        # silently suppress a genuinely new withdrawal. An unrecognised hand-typed
        # value (not "author"/"admin") is kept verbatim: like the ledger path above
        # (`withdrawn_by`), any non-empty string means "a withdrawal was recorded",
        # which is all the presence test needs; the batch never crashes over it.
        # A sidecar may exist and simply hold no arXiv record — an OpenAlex-primary
        # import that echoed `arxiv_id` into the front matter. That paper's remedy
        # (`arxiv-import`, which merges into the existing ledger) is not the remedy of a
        # paper with no sidecar at all (for which, version-less, there is none), nor of
        # one whose sidecar will not parse (about which nothing may be said at all: it
        # is in `errors` above, and we never read what it holds). Record which of the
        # three this is rather than making the report guess.
        # `provenance_sources` yields only paths under `sources/`, so sidecar_path cannot
        # refuse here today — but an unreachable refusal that escapes as a traceback would
        # abort the whole check (the #65/#71/#94 shape). Degrade it to this paper's per-id
        # error, exactly like the corrupt-ledger path above, and keep checking the rest (#142).
        try:
            sidecar = sidecar_path(path, root)
        except ProvenanceError as exc:
            errors.append(
                VersionCheck(
                    arxiv_id=arxiv_id,
                    status=STATUS_ERROR,
                    reason=f"cannot locate provenance sidecar for {_relative(path, root)}: {exc}",
                )
            )
            continue
        if _relative(sidecar, root) in unreadable:
            sidecar_state = SIDECAR_UNREADABLE
        elif sidecar.is_file():
            sidecar_state = SIDECAR_READABLE
        else:
            sidecar_state = SIDECAR_ABSENT
        per_source.setdefault(arxiv_id, []).append(
            LedgerEntry(
                arxiv_id=arxiv_id,
                recorded_version=version,
                recorded_withdrawn_by=scalars.get("arxiv_withdrawn_by") or None,
                sources=(_relative(path, root),),
                sidecar_state=sidecar_state,
            )
        )

    for arxiv_id, views in per_source.items():
        # The check's answer is the FIRST source in sorted-path order, exactly as before
        # #117 — every field of it, unfolded. This *is* first-wins dedup, and #117 says it
        # is correct for a check: a paper is checked once. `per_source` is added beside it,
        # never folded into it.
        #
        # It is tempting to fold the views instead (the highest version wins, as the ledger
        # loop above folds its sidecars). MEASURED, that silences the one signal this
        # command exists to produce: `a.md` recording v3 beside `b.md` recording v7, arXiv
        # serving v7, reports `unchanged` — the paper three versions behind vanishes from
        # the report. `max` is a guess, not a reading.
        #
        # Two sources disagreeing about a paper's `arxiv_version` is a *conflict*, and this
        # repo reports or refuses a conflict rather than resolve it silently (`add_source`
        # raises rather than overwrite; #113/#121 refuse rather than write a field they
        # cannot read). Making it a reportable state is right, and it is NOT this issue: the
        # ledger loop above folds the identical disagreement between two *sidecars* with the
        # same `max`, pinned by a test. Fixing only this loop would mean a paper reported as
        # conflicting before `arxiv-backfill-provenance` runs, and silently folded after —
        # the report's meaning changing because a ledger was created. It must move across
        # both consumers at once, like #112.
        first = views[0]
        slots[arxiv_id] = {
            "version": first.recorded_version,
            "withdrawn_by": first.recorded_withdrawn_by,
            "sources": {first.sources[0]},
            "sidecar_state": first.sidecar_state,
            "per_source": tuple(views),
        }

    entries = [
        LedgerEntry(
            arxiv_id=arxiv_id,
            recorded_version=slot["version"],
            recorded_withdrawn_by=slot["withdrawn_by"],
            sources=tuple(sorted(slot["sources"])),
            sidecar_state=slot.get("sidecar_state", SIDECAR_READABLE),
            per_source=slot.get("per_source", ()),
        )
        for arxiv_id, slot in slots.items()
    ]
    entries.sort(key=lambda e: e.arxiv_id)
    errors.sort(key=lambda e: e.arxiv_id)
    return entries, errors


def excluded_checks(kb_root: Path | str) -> list[VersionCheck]:
    """One ``error`` :class:`VersionCheck` per arXiv paper named only by a source outside
    the provenance root, so no ledger can exist for it (#112).

    Keyed by **arxiv_id**, not by path: the id is what every other row of this report and
    of ``--porcelain`` carries in that column, and it is what ``arxiv-acknowledge-withdrawal
    --id`` takes. A path in the id column would silently change the machine contract and
    hand a parser a value it cannot feed back to any command. The paths go where paths go —
    the ``sources`` column, and the reason.

    These are **not** returned by :func:`collect_ledger_entries`. Its second channel is the
    unreadable-ledger errors, and ``common/backfill.py`` treats any of those as a poison
    that stops every write, because an unread ledger contaminates the front-matter-only
    classification (#111). An excluded source contaminates nothing — it is simply a paper
    no command can act on — so it must be reported without stopping the papers that can be.

    Reported as an error rather than a note because the exit code is what a script reads: a
    withdrawal on this paper will never be detected, and a command that returns 0 while
    that is true is the silent direction #112 exists to close.
    """
    root = Path(kb_root)
    remedy = backfill_remedy("arxiv-backfill-provenance")
    return [
        VersionCheck(
            arxiv_id=arxiv_id,
            status=STATUS_ERROR,
            reason=excluded_reason(", ".join(refs), remedy),
            sources=refs,
        )
        for arxiv_id, refs in sorted(excluded_sources_by_id(root, "arxiv_id").items())
    ]


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
    #
    # This is a *value* comparison, not a presence test (#100). The old
    # `recorded is None` presence test lost three signals it has been MEASURED to
    # lose: (a) an acknowledged `author` withdrawal that arXiv administrators later
    # pull never re-surfaced, because `"admin"` is also not None; (b) an
    # *un-withdrawal* (`author -> None`) never surfaced at all; and (c) a hand-typed
    # garbage value in front matter (`arxiv_withdrawn_by: "typo"`) permanently
    # suppressed a real withdrawal. Comparing the values re-surfaces all three: a
    # withdrawal whose agent differs from the ledger's is `newly_withdrawn`, and a
    # withdrawal the ledger records that arXiv no longer reports is `un_withdrawn`.
    recorded_by = entry.recorded_withdrawn_by
    upstream_by = work.withdrawn_by
    newly_withdrawn = upstream_by is not None and upstream_by != recorded_by
    un_withdrawn = upstream_by is None and recorded_by is not None
    # Three states, not two (#121). A record carrying no version has nothing to
    # compare, so it is neither `changed` nor `unchanged` — it is
    # :data:`STATUS_NO_VERSION`, which the report surfaces on its own line with its
    # own count and its own remedy. Folding it into `unchanged` (the pre-#121
    # behaviour) silently excluded the paper from the only signal this command
    # produces, while `--auto-update` rewrote its `version` anyway: the report and
    # the write disagreed. Folding it into `changed` instead would print "ledger
    # records vNone" — #116.
    if recorded is None:
        status = STATUS_NO_VERSION
    elif current != recorded:
        status = STATUS_CHANGED
    else:
        status = STATUS_UNCHANGED
    return VersionCheck(
        arxiv_id=entry.arxiv_id,
        status=status,
        recorded_version=recorded,
        current_version=current,
        recorded_from=provenance_of(entry.sources),
        # The two other version-tracking values --auto-update writes. Dates are
        # serialized to ISO strings here (the ledger stores strings, not `date`,
        # for the same reason the arXiv writer does: `json` cannot serialize a
        # `date`, and `provenance` refuses to guess). `comment` is stored verbatim.
        current_last_updated=work.last_updated.isoformat() if work.last_updated else None,
        current_comment=work.comment,
        newly_withdrawn=newly_withdrawn,
        un_withdrawn=un_withdrawn,
        withdrawn_by=upstream_by,
        recorded_withdrawn_by=recorded_by,
        sources=entry.sources,
        sidecar_state=entry.sidecar_state,
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
    no_version: int = 0
    withdrawn: int = 0
    un_withdrawn: int = 0
    errors: int = 0
    skipped: int = 0


def summarize(
    results: Iterable[VersionCheck], skipped: Sequence[VersionCheck]
) -> Summary:
    """Count outcomes. ``withdrawn`` counts newly-withdrawn papers and ``un_withdrawn``
    counts papers arXiv no longer reports as withdrawn but the ledger still does, across
    every checked result whatever their version status. ``checked`` excludes skipped.

    ``no_version`` is its own tally, disjoint from ``unchanged`` and ``changed``: a
    record with no recorded version was never compared, so counting it as "up to date"
    is the report lying about what it looked at (#121)."""
    summary = Summary(skipped=len(skipped))
    for result in results:
        if result.status == STATUS_ERROR:
            summary.errors += 1
            continue
        summary.checked += 1
        if result.status == STATUS_CHANGED:
            summary.changed += 1
        elif result.status == STATUS_NO_VERSION:
            summary.no_version += 1
        elif result.status == STATUS_UNCHANGED:
            summary.unchanged += 1
        if result.newly_withdrawn:
            summary.withdrawn += 1
        if result.un_withdrawn:
            summary.un_withdrawn += 1
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
    #: See :class:`LedgerEntry`. Decides which :data:`UPDATE_NO_LEDGER` answer is true.
    sidecar_state: str = SIDECAR_READABLE


#: The four papers ``provenance_of`` calls "front-matter" and the command (if any) that
#: gives each one a usable arXiv ledger. Every message that speaks to such a paper — the
#: ``--auto-update`` remedy, the withdrawal note, the un-withdrawal note and the acknowledge
#: refusal — must agree on this classification, so it lives here once. Naming one remedy for
#: all four is #116 recreated at the prose layer, and #132 is that recreation caught a layer
#: up: the acknowledge/withdrawal messages branched on ``arxiv_version`` alone and so denied
#: ``arxiv-import`` to a paper it repairs and prescribed a backfill that errors on a corrupt
#: ledger. Each mapping below was measured, not reasoned (see ``no_ledger_remedy``).
LEDGER_FIX_REPAIR_BY_HAND = "repair-by-hand"  #: SIDECAR_UNREADABLE — no command; repair the file
LEDGER_FIX_IMPORT = "import"  #: SIDECAR_READABLE, holds no arXiv record — `arxiv-import` merges one
LEDGER_FIX_BACKFILL = "backfill"  #: SIDECAR_ABSENT, front matter has a version — backfill builds it
LEDGER_FIX_NONE = "none"  #: SIDECAR_ABSENT, no version — nothing repairs it (#135)


def ledger_fix(sidecar_state: str, *, has_recorded_version: bool) -> str:
    """Classify a front-matter paper by which command gives it a usable arXiv ledger.

    ``sidecar_state`` is read *first*: whether the front matter carries ``arxiv_version``
    only matters once we know there is no sidecar at all. A readable sidecar that holds no
    arXiv record is repaired by ``arxiv-import`` regardless of the front matter's version,
    and an unreadable one is repaired by nothing — reading the version would be answering
    the wrong question, the #132 defect. Returns one of the ``LEDGER_FIX_*`` constants.
    """
    if sidecar_state == SIDECAR_UNREADABLE:
        return LEDGER_FIX_REPAIR_BY_HAND
    if sidecar_state == SIDECAR_READABLE:
        return LEDGER_FIX_IMPORT
    if has_recorded_version:
        return LEDGER_FIX_BACKFILL
    return LEDGER_FIX_NONE


def no_ledger_remedy(
    arxiv_id: str, *, sidecar_state: str, has_recorded_version: bool
) -> str:
    """What actually repairs a paper ``--auto-update`` cannot write a ledger for.

    **Four** different papers reach :data:`UPDATE_NO_LEDGER`, and they have four different
    answers. Naming one remedy for all of them is how a report prescribes a command that
    does nothing — the failure #116 named and this issue repeats one layer up. Each branch
    below was measured, not reasoned:

    * **The sidecar will not parse** (:data:`SIDECAR_UNREADABLE`). We never read it, so
      nothing may be *asserted* about what it holds, and no command repairs it:
      ``arxiv-import`` answers ``error``, or — for a hand-written ``arxiv_id``-only
      ``.md`` — a silent ``skipped: already imported (arxiv_id match)``. The ledger is
      already reported under ``Could not check:``; the note points there and stops. #128
      made this branch newly reachable by teaching ``read_provenance`` to raise on a
      non-bool ``is_retracted`` and an out-of-vocabulary ``withdrawn_by``.
    * **A sidecar exists and holds no arXiv record** (an OpenAlex-primary import that
      echoed ``arxiv_id`` into the front matter). ``arxiv-import`` merges an arXiv record
      into that existing ledger: measured ``merged``.
    * **No sidecar, and the front matter records a version.** ``arxiv-backfill-provenance``
      builds a ledger from that front matter: measured ``backfilled``.
    * **No sidecar, and the front matter records no version.** *Nothing currently repairs
      this paper.* ``arxiv-import`` answers ``skipped: already imported (arxiv_id match)``
      and ``arxiv-backfill-provenance`` answers ``refused`` (``required=("version",)``,
      #113). Saying so is the honest answer #116 asked for; naming a command here would
      be the same lie in a new place.
    """
    fix = ledger_fix(sidecar_state, has_recorded_version=has_recorded_version)
    if fix == LEDGER_FIX_REPAIR_BY_HAND:
        # Say nothing about the contents of a file that never parsed, and prescribe
        # nothing: every command that touches it fails or silently no-ops.
        return (
            "Its provenance ledger could not be read (see `Could not check:`), so what it "
            "holds is unknown and no command can record a version for this paper until "
            "the ledger is repaired by hand."
        )
    if fix == LEDGER_FIX_IMPORT:
        return (
            "Its provenance ledger holds no arXiv record for this paper (another "
            "integration wrote the ledger and only echoed the id), so --auto-update has "
            f"no record to fill. Run `factlog arxiv-import --id {arxiv_id}` to add one."
        )
    if fix == LEDGER_FIX_BACKFILL:
        return (
            "This paper has no provenance ledger (imported before #82), so --auto-update "
            "has none to fill. Run `factlog arxiv-backfill-provenance` to build one from "
            "its front matter."
        )
    return (
        "This paper has no provenance ledger (imported before #82), and no command "
        "currently records a version for it: --auto-update has no ledger to write into, "
        "`factlog arxiv-import` answers `already imported (arxiv_id match)`, and "
        "`factlog arxiv-backfill-provenance` refuses a paper whose front matter carries "
        "no arxiv_version (#113). Backfilling a ledger for it is tracked in #135."
    )


def front_matter_acknowledge_refusal(
    arxiv_id: str, *, sidecar_state: str, has_recorded_version: bool
) -> str:
    """Why ``arxiv-acknowledge-withdrawal`` cannot yet record a decision for a front-matter
    paper, and the working next step — classified by :func:`ledger_fix` so it agrees with
    the check-versions notes and never denies a command that repairs the paper (#132).

    The UNREADABLE case is unreachable through the CLI (the command's ``ledger_errors``
    guard refuses before this branch, for zero API requests), but it is classified here so
    the one function answering this question answers it for all four papers, and a future
    caller that reaches it is not handed the #135 lie meant for a version-less paper.
    """
    fix = ledger_fix(sidecar_state, has_recorded_version=has_recorded_version)
    if fix == LEDGER_FIX_REPAIR_BY_HAND:
        return (
            f"{arxiv_id!r} has a provenance ledger that could not be read, so what it "
            "records is unknown and no command can record a decision for it until the "
            "ledger is repaired by hand."
        )
    if fix == LEDGER_FIX_IMPORT:
        # A sidecar exists (another integration echoed the id) but holds no arXiv record,
        # so "it has no provenance ledger" is false. `arxiv-import` merges an arXiv record
        # into that ledger: measured `merged`, after which the signal no longer surfaces.
        return (
            f"{arxiv_id!r} has a provenance ledger, but it holds no arXiv record (another "
            "integration wrote it and only echoed the id), so there is no arXiv record "
            f"here to acknowledge. Run `factlog arxiv-import --id {arxiv_id}` to add one."
        )
    if fix == LEDGER_FIX_BACKFILL:
        return (
            f"{arxiv_id!r} is known only from front matter (imported before #82), so it "
            "has no provenance ledger to record a decision in — and re-import will not "
            "create one. Run `factlog arxiv-backfill-provenance` to build one from its "
            "front matter, then re-run this command to acknowledge."
        )
    return (
        f"{arxiv_id!r} is known only from front matter (imported before #82), so it has "
        "no provenance ledger to record a decision in, and its front matter carries no "
        "arxiv_version, so `arxiv-backfill-provenance` refuses it (#113); no command can "
        "build a ledger for it, so this signal cannot be acknowledged here (#135)."
    )


def _withdrawal_ledger_suffix(result: VersionCheck) -> str:
    """The clause the front-matter :func:`withdrawal_note` appends: the missing-ledger fact
    and the command (if any) that lets the withdrawal be recorded, per :func:`ledger_fix`."""
    fix = ledger_fix(
        result.sidecar_state, has_recorded_version=result.recorded_version is not None
    )
    if fix == LEDGER_FIX_REPAIR_BY_HAND:
        return (
            "Its provenance ledger could not be read (see `Could not check:`), so what it "
            "holds is unknown and no command can record this withdrawal until the ledger "
            "is repaired by hand."
        )
    if fix == LEDGER_FIX_IMPORT:
        return (
            "Its provenance ledger holds no arXiv record for this paper (another "
            "integration wrote the ledger and only echoed the id), so there is no arXiv "
            f"record here to acknowledge; run `factlog arxiv-import --id {result.arxiv_id}` "
            "to add one."
        )
    if fix == LEDGER_FIX_BACKFILL:
        return (
            "This paper has no provenance ledger (imported before #82), so the withdrawal "
            "cannot be acknowledged until one exists; run `factlog arxiv-backfill-provenance` "
            "to build one from its front matter."
        )
    return (
        "This paper has no provenance ledger (imported before #82) and its front matter "
        "carries no arxiv_version, so `arxiv-backfill-provenance` refuses it (#113) and the "
        "withdrawal cannot be acknowledged and will keep surfacing until a ledger exists "
        "(#135)."
    )


def _un_withdrawal_ledger_suffix(result: VersionCheck) -> str:
    """The clause the front-matter :func:`un_withdrawal_note` appends: the command (if any)
    that gives the paper a ledger so the reversed withdrawal can be cleared, per
    :func:`ledger_fix`. The lead sentence ("the front-matter note is simply stale") is the
    caller's; this names only the next step."""
    fix = ledger_fix(
        result.sidecar_state, has_recorded_version=result.recorded_version is not None
    )
    if fix == LEDGER_FIX_REPAIR_BY_HAND:
        return (
            "Its provenance ledger could not be read (see `Could not check:`), so no "
            "command can give it a ledger to acknowledge this in until it is repaired by "
            "hand."
        )
    if fix == LEDGER_FIX_IMPORT:
        return (
            "Its provenance ledger holds no arXiv record for this paper (another "
            "integration wrote the ledger and only echoed the id); run "
            f"`factlog arxiv-import --id {result.arxiv_id}` to add one."
        )
    if fix == LEDGER_FIX_BACKFILL:
        return (
            "Run `factlog arxiv-backfill-provenance` to give it a ledger, after which this "
            "can be acknowledged."
        )
    return (
        "Its front matter carries no arxiv_version, so `arxiv-backfill-provenance` refuses "
        "it (#113) and no command can give it a ledger to acknowledge this in (#135)."
    )


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

    **The command never writes a field the report did not name** (#121). This function
    is deliberately *not* gated on :data:`STATUS_CHANGED`: a record carrying no
    ``version`` is missing a value a refresh legitimately learned, and filling it is a
    refresh's authority. What was wrong was doing it silently — the plain check called
    such a paper ``unchanged`` and ``--auto-update`` rewrote it anyway. The paper is now
    :data:`STATUS_NO_VERSION`, which both the plain report and the ``--auto-update``
    report surface, so the reported set and the written set are the same set.

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
                    sidecar_state=result.sidecar_state,
                    # `--auto-update` will not fabricate an import record whatever the
                    # answer is; which answer is true depends on the sidecar's state and
                    # on whether the front matter carries a version.
                    reason=no_ledger_remedy(
                        result.arxiv_id,
                        sidecar_state=result.sidecar_state,
                        has_recorded_version=result.recorded_version is not None,
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


#: The withdrawing agents the parser recognises. A recorded value outside this set
#: (a hand-typed garbage front-matter value, #100 case c) is shown verbatim rather
#: than run through ``withdrawal_agent``, whose fallback ("arXiv") would hide that it
#: was never a real agent in the first place.
_KNOWN_AGENTS = (WITHDRAWN_BY_AUTHOR, WITHDRAWN_BY_ADMIN)


def _recorded_phrase(recorded: str) -> str:
    """How the ledger's recorded withdrawal reads in a divergence note."""
    if recorded in _KNOWN_AGENTS:
        return f"a withdrawal by {withdrawal_agent(recorded)}"
    return f"the unrecognised value {recorded!r}"


def withdrawal_note(result: VersionCheck) -> str:
    """The prominent, retraction-free withdrawal line for a newly-withdrawn paper.

    Names both sides of the divergence: a fresh withdrawal the record did not carry,
    or a withdrawal whose agent differs from the one it recorded (an acknowledged
    ``author`` withdrawal arXiv administrators later pulled, or a garbage recorded
    value — #100). It attributes the recorded value to its true source — a ledger, or a
    pre-#82 paper's front matter (#98) — so it never says "the ledger recorded" a value
    that came from front matter. It never uses the word "retracted".

    For a **front-matter** paper (``recorded_from == "front-matter"``, one of four kinds,
    #98) the recorded value is not in an arXiv ledger, so ``arxiv-acknowledge-withdrawal``
    — which writes a sidecar — cannot record the operator's decision yet and would exit 1.
    The warning must stay loud (a withdrawal the KB never recorded is real news), but a loud
    warning that prescribes nothing is the exact wallpaper #93 exists to remove, so the note
    adds the missing-ledger fact and the working next step. That step is *not* a function of
    ``arxiv_version`` alone: :func:`ledger_fix` reads ``sidecar_state`` first. A readable
    sidecar that holds no arXiv record is repaired by ``arxiv-import`` (measured ``merged``),
    an unreadable one by nothing but a hand repair, an absent one by ``arxiv-backfill-
    provenance`` when the front matter has a version (#114) and by nothing when it does not
    (#113, tracked in #135). Branching on the version alone denied ``arxiv-import`` to the
    paper it fixes and prescribed a backfill that errors on a corrupt ledger — the #132
    defect. :func:`_withdrawal_ledger_suffix` names the one command that actually works.
    """
    agent = withdrawal_agent(result.withdrawn_by)
    version = f"v{result.current_version}" if result.current_version else "the current version"
    recorded = result.recorded_withdrawn_by
    where = "front matter" if result.recorded_from == "front-matter" else "ledger"
    if recorded is None:
        provenance = f"which the {where} did not record"
    else:
        provenance = f"where the {where} recorded {_recorded_phrase(recorded)}"
    # Build the shared body once. Restating it in both branches would let an edit to
    # one silently leave the other behind: only the ledger string is pinned byte-for-byte
    # by a test, so a maintainer who updates that literal would never learn the
    # front-matter note had kept the old prose. The ledger note is a prefix of the
    # front-matter note by construction, and a test asserts exactly that.
    body = (
        f"arXiv now reports {result.arxiv_id} ({version}) as WITHDRAWN by {agent}, "
        f"{provenance}. Withdrawal is not retraction; this "
        "unverified signal flags the paper for human review before any claim from "
        "it is trusted."
    )
    if result.recorded_from == "front-matter":
        return f"{body} {_withdrawal_ledger_suffix(result)}"
    return body


def un_withdrawal_note(result: VersionCheck) -> str:
    """The line for a paper arXiv no longer reports as withdrawn but the record does.

    This is **not** a withdrawal warning: a paper coming back is its own kind of news.

    For a **ledger**-recorded value, ``withdrawn_by`` is an *identifying* field
    (``arxiv/source_writer.py``), so a ledger left recording a withdrawal arXiv has
    reversed diverges from a fresh import (which parses ``None``) and makes a re-import
    error; a refresh may not clear it, so only a human's acknowledgement may, and the
    note prescribes it.

    For a **front-matter** value (``recorded_from == "front-matter"``, #98) the withdrawal
    lives only in front matter, not in an arXiv ledger record, so ``withdrawn_by`` (an
    identifying *ledger* field) does not diverge, a re-import does *not* error, and
    ``arxiv-acknowledge-withdrawal`` — which writes sidecars — cannot help until a ledger
    exists. The note must claim neither a divergence nor a re-import error, so it states
    plainly that the front-matter note is now stale, then names the working next step from
    :func:`ledger_fix` — ``arxiv-import`` for a readable sidecar with no arXiv record,
    ``arxiv-backfill-provenance`` for an absent sidecar with a version (#114), a hand repair
    for an unreadable one, and #135 for a version-less absent one. Branching on the version
    alone prescribed a backfill that errors on a corrupt ledger — the #132 defect.
    :func:`_un_withdrawal_ledger_suffix` names the one command that actually works.
    """
    recorded = result.recorded_withdrawn_by or ""
    if result.recorded_from == "front-matter":
        stale = (
            f"arXiv no longer reports {result.arxiv_id} as withdrawn, but its front "
            f"matter still records {_recorded_phrase(recorded)}. That withdrawal lives "
            "only in front matter, not in an arXiv ledger record, so nothing diverges and "
            "a re-import does not error; the front-matter note is simply stale."
        )
        return f"{stale} {_un_withdrawal_ledger_suffix(result)}"
    return (
        f"arXiv no longer reports {result.arxiv_id} as withdrawn, but the ledger still "
        f"records {_recorded_phrase(recorded)}. `withdrawn_by` is an identifying field: "
        "until this is acknowledged, the ledger diverges from a fresh import and "
        "re-import will error. Run "
        f"`factlog arxiv-acknowledge-withdrawal --id {result.arxiv_id}` to clear it."
    )


def no_version_note(result: VersionCheck, *, outcome: str | None = None) -> str:
    """The line for a record that carries no ``version`` at all (:data:`STATUS_NO_VERSION`).

    It never interpolates the recorded value — there is none, and ``v{None}`` is the
    ``vNone`` #116 removed. It says what is true (the record holds no version, arXiv
    serves vN) and it names the remedy that *works*, which is not the same command for
    every such paper:

    * A **ledger**-backed record is what ``--auto-update`` repairs: measured, it writes
      the version and the cross-source merge that errored then succeeds.
    * A **front-matter** paper has no arXiv ledger record for ``--auto-update`` to fill,
      so :func:`no_ledger_remedy` decides between ``arxiv-import``,
      ``arxiv-backfill-provenance``, and the honest admission that nothing repairs it.

    ``outcome`` is this paper's :class:`LedgerUpdate` status when ``--auto-update`` ran
    this very run, and ``None`` when it did not. A report that prescribes
    ``--auto-update`` inside the output of ``--auto-update`` is the report and the write
    disagreeing again, inverted — including for a paper whose write *failed*, which must
    point at the error rather than at the command that just produced it.
    """
    where = "front matter" if result.recorded_from == "front-matter" else "ledger"
    serves = f"arXiv now serves v{result.current_version}"
    head = f"{result.arxiv_id}: the {where} records no version, {serves}."

    if outcome == UPDATE_WRITTEN:
        return f"{head} --auto-update recorded it this run (see 'Ledger updated' below)."
    if outcome == UPDATE_ERROR:
        return (
            f"{head} --auto-update could not record it this run (see 'Could not "
            "auto-update' below); the version is still missing."
        )

    if result.recorded_from == "front-matter":
        remedy = no_ledger_remedy(
            result.arxiv_id,
            sidecar_state=result.sidecar_state,
            has_recorded_version=False,
        )
        return (
            f"{head} Version drift cannot be detected for this paper until a version is "
            f"recorded. {remedy}"
        )
    return (
        f"{head} Version drift cannot be detected for this paper until a version is "
        "recorded, and a cross-source merge import errors on the record until then "
        "(#116). Run `factlog arxiv-check-versions --auto-update` to record it."
    )


def _sources_suffix(result: VersionCheck) -> str:
    return f"  (sources: {', '.join(result.sources)})" if result.sources else ""


def provenance_of(sources) -> str:
    """``"front-matter"`` if *sources* are all ``sources/*.md`` (a pre-#82 paper with
    no ledger, #98), else ``"ledger"``. One paper's sources are never mixed:
    ``collect_ledger_entries`` fills a slot from the ledger *or*, only if no ledger
    covered it, from front matter — never both."""
    sources = sources or ()
    if sources and all(str(s).startswith("sources/") for s in sources):
        return "front-matter"
    return "ledger"


def _recorded_in(result) -> str:
    """Where the recorded version came from: a ledger, or a source's front matter.

    A paper imported before #82 has no ledger, and saying "ledger records v5" for it
    names a file that does not exist. Ledger sources live under
    ``source-provenance/``; front-matter ones are the ``sources/*.md`` themselves.
    """
    if provenance_of(getattr(result, "sources", ())) == "front-matter":
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
    version outcome; then version divergences; then records carrying no version at all;
    then per-id errors; then, under ``--auto-update``, what was written to the ledgers;
    then the tally."""
    total = len(results) + len(skipped)
    # `summary.checked`, not `len(results)`: `results` carries the per-file errors
    # (a corrupt ledger, a source outside the provenance root), and a paper this run
    # could not check has not been checked. `Checked 4 of 4` above `Errors: 1` was a
    # false statement about the run. The denominator keeps every record considered,
    # so the excluded paper is still counted, never dropped (#112).
    header = f"Checked {summary.checked} of {total} arXiv record(s) in KB: {target}"
    if skipped:
        header += (
            f"\n  ({len(skipped)} skipped: checked within the last "
            f"{_days(older_than_days)}. A withdrawal or a version bump that "
            "appeared since their last check is NOT detected — arXiv only says so "
            "when asked. Run with --older-than 0 to force a re-check.)"
        )
    lines = [header]

    withdrawn = [r for r in results if r.newly_withdrawn]
    un_withdrawn = [r for r in results if r.un_withdrawn]
    changed = [
        r
        for r in results
        if r.status == STATUS_CHANGED and not r.newly_withdrawn and not r.un_withdrawn
    ]
    # Unlike `changed`, a no-version result is NOT filtered out when the paper is also
    # newly withdrawn. The withdrawal note absorbs a version change ("Its version also
    # changed: ...") so listing it twice would repeat one fact; it says nothing about a
    # *missing* version, and that paper's remedy is its own. A distinct state keeps its
    # distinct signal even when a louder one fires alongside it.
    no_version = [r for r in results if r.status == STATUS_NO_VERSION]
    errors = [r for r in results if r.status == STATUS_ERROR]
    # What this run's --auto-update did to each paper. The no-version note reads
    # differently once the write has happened — or failed: prescribing `--auto-update`
    # underneath a line that says it just ran, or just errored, would be the report and
    # the write disagreeing again.
    outcomes = {u.arxiv_id: u.status for u in updates}

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

    if un_withdrawn:
        lines.append(
            "\nNo longer withdrawn (arXiv reversed a withdrawal this KB records):"
        )
        for result in un_withdrawn:
            lines.append(f"  ↺ {un_withdrawal_note(result)}{_sources_suffix(result)}")

    if changed:
        lines.append("\nVersion diverged (the source evolved; this is a report, not a verdict):")
        for result in changed:
            lines.append(
                f"  ~ {result.arxiv_id}: {_recorded_in(result)} "
                f"v{result.recorded_version}, arXiv now serves "
                f"v{result.current_version}{_sources_suffix(result)}"
            )

    if no_version:
        lines.append(
            "\nNo version recorded (nothing to compare — this paper is excluded from "
            "version checking until a version exists):"
        )
        for result in no_version:
            note = no_version_note(result, outcome=outcomes.get(result.arxiv_id))
            lines.append(f"  ? {note}{_sources_suffix(result)}")

    if errors:
        lines.append("\nCould not check:")
        for result in errors:
            lines.append(f"  ✗ {result.arxiv_id}: {result.reason}{_sources_suffix(result)}")

    lines.extend(_auto_update_lines(updates))

    # Labels are padded to the widest ("No longer withdrawn:") so every count lands in
    # one column; the un-withdrawal line no longer juts out of the block.
    lines.append("\nSummary:")
    lines.append(f"  {'Checked:':<21}{summary.checked}")
    lines.append(f"  {'Up to date:':<21}{summary.unchanged}")
    lines.append(f"  {'Version changed:':<21}{summary.changed}")
    lines.append(f"  {'No version recorded:':<21}{summary.no_version}")
    lines.append(f"  {'Newly withdrawn:':<21}{summary.withdrawn}")
    lines.append(f"  {'No longer withdrawn:':<21}{summary.un_withdrawn}")
    lines.append(f"  {'Errors:':<21}{summary.errors}")
    lines.append(f"  {'Skipped:':<21}{summary.skipped}")
    if updates:
        updated = sum(1 for u in updates if u.status == UPDATE_WRITTEN)
        lines.append(f"  {'Ledgers updated:':<21}{updated}")
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
            # `recorded_version` is None for the version-less record --auto-update just
            # filled. `v{None}` is the `vNone` of #116; say what was actually there.
            was = (
                "no version was recorded"
                if u.recorded_version is None
                else f"was v{u.recorded_version}"
            )
            lines.append(
                f"  ✎ {u.arxiv_id}: recorded v{u.current_version} ({was}){ledgers}"
            )
    if no_ledger:
        # The header prescribed `arxiv-import` for every paper here. That is true for a
        # paper whose sidecar simply holds no arXiv record, false for one with no sidecar
        # at all (`already imported (arxiv_id match)`). The remedy is per-paper, so it is
        # printed per-paper (#121); the header only states the shared fact.
        lines.append(
            "\nNot auto-updated (no arXiv record in a provenance ledger to update; "
            "--auto-update never fabricates an import record):"
        )
        for u in no_ledger:
            lines.append(
                f"  · {u.arxiv_id}: arXiv now serves v{u.current_version}. {u.reason}"
            )
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

    ``check\t<id>\t<status>\t<recorded>\t<current>\t<withdrawn_by>\t<newly_withdrawn>\t<reason>\t<un_withdrawn>``
    with empty fields for absent values, ``newly_withdrawn``/``un_withdrawn`` as
    ``0``/``1``, and versions as bare integers. ``status`` is one of
    ``unchanged``/``changed``/``no-version``/``error``/``skipped``: ``no-version`` is
    the record that carries no ``version`` (#121), whose ``<recorded>`` column is
    therefore empty. It is a *new value in an existing column*, never a new column and
    never the bare string ``None``, so a #78 parser keeping its column offsets keeps
    working; one that switched on ``status`` sees a value it must now handle rather
    than a version-less record mislabelled ``unchanged``. ``un_withdrawn`` distinguishes a paper
    arXiv no longer reports as withdrawn (but a record still does) from an unchanged one,
    whose row is otherwise byte-identical; it is appended last so a #78 parser keying on
    the earlier fixed columns is unaffected. The ``update`` rows are
    ``update\t<id>\t<status>\t<recorded>\t<current>\t<ledgers>`` where ``status`` is
    one of ``updated``/``unchanged``/``no-ledger``/``error`` and ``ledgers`` is a
    comma-joined list (empty unless ``updated``). They are absent when the flag is
    off, so an existing #78 parser is unaffected. The tally footer gains one row,
    ``no_version\t<n>``, after ``skipped``: rows ``checked``…``skipped`` keep their
    index, while ``updated`` and ``target`` shift down by one — as ``updated``'s
    conditional presence (it appears only under ``--auto-update``) already made
    positional tally parsing unsound. Parse the footer by its first field, as the rows
    above. The progress/ETA is stderr only.
    """
    rows: list[str] = []
    for result in sorted([*results, *skipped], key=lambda r: r.arxiv_id):
        recorded = "" if result.recorded_version is None else str(result.recorded_version)
        current = "" if result.current_version is None else str(result.current_version)
        rows.append(
            "check\t{id}\t{status}\t{recorded}\t{current}\t{by}\t{withdrawn}\t{reason}\t{un}".format(
                id=porcelain_field(result.arxiv_id),
                status=result.status,
                recorded=recorded,
                current=current,
                by=porcelain_field(result.withdrawn_by or ""),
                withdrawn="1" if result.newly_withdrawn else "0",
                reason=porcelain_field(result.reason),
                un="1" if result.un_withdrawn else "0",
            )
        )
    for u in updates:
        recorded = "" if u.recorded_version is None else str(u.recorded_version)
        current = "" if u.current_version is None else str(u.current_version)
        rows.append(
            "update\t{id}\t{status}\t{recorded}\t{current}\t{ledgers}".format(
                id=porcelain_field(u.arxiv_id),
                status=u.status,
                recorded=recorded,
                current=current,
                ledgers=porcelain_field(",".join(u.ledgers)),
            )
        )
    rows.append(f"checked\t{summary.checked}")
    rows.append(f"unchanged\t{summary.unchanged}")
    rows.append(f"changed\t{summary.changed}")
    rows.append(f"withdrawn\t{summary.withdrawn}")
    rows.append(f"un_withdrawn\t{summary.un_withdrawn}")
    rows.append(f"errors\t{summary.errors}")
    rows.append(f"skipped\t{summary.skipped}")
    # Appended after the #78/#100 tallies rather than slotted next to `changed`: that
    # keeps every pre-existing row at its own index, which is the most a footer can
    # promise. `updated` and `target` do move down by one — `updated` is conditional, so
    # nothing could have parsed this footer positionally to begin with. Key by field.
    rows.append(f"no_version\t{summary.no_version}")
    if updates:
        rows.append(f"updated\t{sum(1 for u in updates if u.status == UPDATE_WRITTEN)}")
    rows.append(f"target\t{target}")
    return rows


def _days(value: float) -> str:
    whole = int(value)
    label = "day" if whole == 1 else "days"
    return f"{whole} {label}" if value == whole else f"{value} days"
