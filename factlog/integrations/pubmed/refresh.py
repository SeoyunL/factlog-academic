# SPDX-License-Identifier: Apache-2.0
"""Report-only (and, under ``--auto-update``, ledger-revising) refresh of the PubMed records
a KB already holds (issues #168, #169).

## What this does, and what it deliberately does not

For every PubMed record in a KB's provenance ledgers (``<kb>/source-provenance/**/*.json``)
— and every pre-provenance front-matter-only PubMed source — this re-fetches the record
by PMID (``efetch``), re-runs the two-marker retraction detector
(:func:`~factlog.integrations.pubmed.retraction.detect_retraction`) and compares the live
metadata with what the ledger recorded. It **reports** the divergence. Without
``--auto-update`` it writes nothing under ``sources/`` and nothing to a ledger; the only
thing it advances is the KB-level check-log's last-checked timestamps.

It is the PubMed counterpart of ``openalex-refresh`` (#83) and ``arxiv-check-versions``
(#78/#79). A merged or deleted PMID is handled (#170), but only to *surface* it: a merge is
reported with both PMIDs and offered — never followed, not even under ``--auto-update`` —
because a PMID is a ``CROSS_SOURCE_IDS`` identifier and re-keying is a human decision (P1);
a deletion is flagged for review and the KB entry is never dropped; a network failure stays
a network failure and is never mistaken for a deletion. See :func:`check_entries`.

## What a refresh compares — and what ``--auto-update`` may write (#169)

A refresh compares two categories, and they are handled by two different rules:

* The **narrow set of identifier/journal fields** in :data:`AUTO_UPDATE_FIELDS` —
  ``doi`` and ``journal``. These are *transcription* facts: a DOI absent at import and
  present now, a journal abbreviation NLM has since normalised. PubMed's answer is a
  correction to what the ledger transcribed, not a claim about the world, so under
  ``--auto-update`` they — and only they — are written back to the ledger. The enumeration
  lives in exactly one place (:data:`AUTO_UPDATE_FIELDS`) so the writer and the drift
  report can never disagree about its membership.
* **Retraction status.** PubMed owns it (§7.2), but it is a claim about the world and a
  **human-gate signal**, not a transcription. ``--auto-update`` **never** writes it: a
  newly-detected retraction is surfaced to a human and the recorded ``retracted`` value is
  copied through verbatim, so the signal keeps surfacing until a human records it via
  ``pubmed-acknowledge-retraction``. ``--auto-update`` is not an acknowledgement and is not
  a substitute for one. This is the same rule OpenAlex follows for ``is_retracted`` and
  arXiv for ``withdrawn_by``.

A record whose ``doi``/``journal`` both match what the ledger recorded is ``unchanged``;
one where either diverged, or whose retraction status drifted, is ``changed``. Retraction
drift is surfaced separately: ``newly_retracted`` (PubMed now reports a retraction the
record did not carry) and ``un_retracted`` (PubMed no longer reports one the record holds).

## "Unchanged" means one thing, for the report and for the writer (#121)

A retraction is recorded iff the ledger's ``retracted`` field is present **and** ``True``
(``fields.get("retracted") is True``), exactly as ``source_writer._provenance_record``
emits it — its absence *means* not-retracted, never not-checked. The drift report and
``--auto-update`` read that one definition **and** the one :data:`AUTO_UPDATE_FIELDS`
enumeration, so a record the report calls ``unchanged`` is one ``--auto-update`` leaves
byte-identical: an ``unchanged`` status means no ``AUTO_UPDATE_FIELDS`` diverged, and those
are the only fields the writer may touch. A test asserts that agreement rather than a
comment (#121 shipped a report that lied to its writer; this one cannot).

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
    SourceRecord,
    backfill_remedy,
    excluded_reason,
    excluded_sources_by_id,
    provenance_sources,
    read_provenance,
    update_source,
    write_provenance,
)
from factlog.integrations.pubmed.client import PubMedError
from factlog.integrations.pubmed.work_parser import (
    PubMedParseError,
    parse_efetch_response,
)

__all__ = [
    "LedgerEntry",
    "RefreshCheck",
    "LedgerRefresh",
    "Summary",
    "STATUS_UNCHANGED",
    "STATUS_CHANGED",
    "STATUS_ERROR",
    "STATUS_SKIPPED",
    "STATUS_MERGED",
    "STATUS_DELETED",
    "UPDATE_WRITTEN",
    "UPDATE_UNCHANGED",
    "UPDATE_NO_LEDGER",
    "UPDATE_ERROR",
    "AUTO_UPDATE_FIELDS",
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
    "apply_auto_update",
    "provenance_of",
    "retraction_note",
    "un_retraction_note",
    "merged_note",
    "deleted_note",
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

#: The deliberately narrow set of identifier/journal fields a refresh compares and, under
#: ``--auto-update``, may rewrite in a PubMed ledger record (#169). ``doi`` and ``journal``
#: are transcription facts PubMed is authoritative on for a published article — a DOI that
#: was absent at import and is present now, a journal abbreviation NLM has normalised. This
#: is the **one place** the enumeration lives: :func:`_diff` compares exactly these, and
#: :func:`apply_auto_update` writes exactly these, so the report and the writer can never
#: disagree about what "unchanged" means (#121). It never includes ``retracted`` (a
#: human-gate signal, copied through verbatim — see :func:`apply_auto_update`) nor the
#: ``pubmed_mesh_*`` topic fields (not identifiers, left untouched).
AUTO_UPDATE_FIELDS = ("doi", "journal")

STATUS_UNCHANGED = "unchanged"
STATUS_CHANGED = "changed"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"
#: A PMID PubMed answered under a *different* PMID (NCBI merged two records; #170).
#: Distinct from an error: efetch succeeded and named the survivor — a human decides
#: whether to follow the pointer, so it is surfaced (with both ids), never auto-followed,
#: not even under ``--auto-update`` (:func:`apply_auto_update` skips it).
STATUS_MERGED = "merged"
#: A PMID PubMed no longer serves at all — an empty (well-formed) response, not a network
#: failure (#170). Flagged for human review; the KB entry is never dropped, because a
#: deleted PMID is usually a signalled removal a fact-checking tool must surface.
STATUS_DELETED = "deleted"


@dataclass(frozen=True)
class LedgerEntry:
    """One PubMed record the KB holds, gathered from its provenance ledger(s) or its
    source front matter.

    ``recorded_doi`` and ``recorded_journal`` are the identifier/journal transcription
    facts a refresh measures the live record against — the fields :data:`AUTO_UPDATE_FIELDS`
    may rewrite. ``recorded_retracted`` is the retraction status a refresh measures the live
    record against — ``True`` iff a ledger record carried ``retracted: true`` (or the front
    matter carried a boolean ``pubmed_retracted``). ``recorded_notice_pmid`` is the notice
    PMID the ledger/front matter recorded, if any. ``sources`` are the ledger-relative
    paths that reference the record.
    """

    pmid: str
    recorded_doi: str | None = None
    recorded_journal: str | None = None
    recorded_retracted: bool = False
    recorded_notice_pmid: str | None = None
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class RefreshCheck:
    """The outcome of refreshing one PubMed record (or one corrupt/excluded source).

    ``status`` is one of the ``STATUS_*`` constants; it is :data:`STATUS_CHANGED` when any
    of :data:`AUTO_UPDATE_FIELDS` diverged (named in ``changed_fields``) **or** the
    retraction status drifted. ``changed_fields`` names which of ``doi``/``journal`` differ
    from the ledger. The two retraction drift flags are mutually exclusive and orthogonal to
    the field change: ``newly_retracted`` — PubMed now reports a retraction the record did
    not carry; ``un_retracted`` — PubMed no longer reports a retraction the record still
    holds. ``current_notice_pmid`` is the notice PMID a fresh retraction links to, if any.
    ``recorded_from`` is ``"ledger"`` or ``"front-matter"`` so a note never claims "the
    ledger recorded" a value that came from front matter. For an error result the ``pmid``
    may instead be a ledger/source path and ``reason`` explains it.

    ``returned_pmid`` is set only for :data:`STATUS_MERGED`: the PMID PubMed served the
    record under, so both the requested (:attr:`pmid`) and the survivor id are reported and
    a human can decide whether to follow the merge. The KB is never silently re-keyed.
    """

    pmid: str
    status: str
    recorded_doi: str | None = None
    current_doi: str | None = None
    recorded_journal: str | None = None
    current_journal: str | None = None
    recorded_retracted: bool = False
    current_retracted: bool = False
    current_notice_pmid: str | None = None
    newly_retracted: bool = False
    un_retracted: bool = False
    changed_fields: tuple[str, ...] = ()
    recorded_from: str = "ledger"
    reason: str = ""
    sources: tuple[str, ...] = ()
    returned_pmid: str | None = None


def _relative(path: Path, kb_root: Path) -> str:
    try:
        return str(path.relative_to(kb_root))
    except ValueError:
        return str(path)


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


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
    ``pmid``, keeping the first non-empty value of each :data:`AUTO_UPDATE_FIELDS` field,
    OR-ing the retraction flag (any ledger recording a retraction wins) and keeping the
    first non-empty notice PMID. A ledger, when present, is authoritative; front matter
    speaks only for a record no PubMed ledger record covers.
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
            for key in AUTO_UPDATE_FIELDS:
                if slot[key] is None:
                    slot[key] = _str_or_none(record.fields.get(key))
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
        scalars = read_scalars(
            path, ("pmid", "doi", "journal", RETRACTION_KEY, RETRACTION_NOTICE_KEY)
        )
        pmid = scalars.get("pmid", "")
        if not pmid or pmid in slots:
            continue
        slots[pmid] = {
            "doi": scalars.get("doi") or None,
            "journal": scalars.get("journal") or None,
            "retracted": parse_retraction_flag(scalars.get(RETRACTION_KEY, "")) is True,
            "notice_pmid": scalars.get(RETRACTION_NOTICE_KEY) or None,
            "sources": {_relative(path, root)},
        }

    entries = [
        LedgerEntry(
            pmid=pmid,
            recorded_doi=slot["doi"],
            recorded_journal=slot["journal"],
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
    return {"doi": None, "journal": None, "retracted": False, "notice_pmid": None,
            "sources": set()}


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
                    recorded_doi=entry.recorded_doi,
                    recorded_journal=entry.recorded_journal,
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
    current_doi = _str_or_none(work.doi)
    current_journal = _str_or_none(work.journal)
    # The narrow identifier/journal drift `--auto-update` may write — compared over exactly
    # `AUTO_UPDATE_FIELDS` so the report and the writer read the same enumeration (#121).
    recorded = {"doi": entry.recorded_doi, "journal": entry.recorded_journal}
    current = {"doi": current_doi, "journal": current_journal}
    changed_fields = tuple(k for k in AUTO_UPDATE_FIELDS if recorded[k] != current[k])

    current_retracted = bool(work.retracted)
    # A *value* comparison, not a presence test — so a retraction PubMed has since reversed
    # (recorded True, live False) surfaces as loudly as a fresh one. Retraction drift is
    # orthogonal to a field change and is never written by `--auto-update`.
    newly_retracted = current_retracted and not entry.recorded_retracted
    un_retracted = (not current_retracted) and entry.recorded_retracted
    # `unchanged` means no `AUTO_UPDATE_FIELDS` diverged — the exact condition under which
    # `--auto-update` leaves the ledger byte-identical (#121). Retraction drift is folded in
    # too so a reversed/fresh retraction still reads as `changed` in the report, but the
    # status is never `unchanged` while a writable field has moved.
    status = (
        STATUS_CHANGED
        if (changed_fields or newly_retracted or un_retracted)
        else STATUS_UNCHANGED
    )
    return RefreshCheck(
        pmid=entry.pmid,
        status=status,
        recorded_doi=entry.recorded_doi,
        current_doi=current_doi,
        recorded_journal=entry.recorded_journal,
        current_journal=current_journal,
        recorded_retracted=entry.recorded_retracted,
        current_retracted=current_retracted,
        current_notice_pmid=work.retraction_notice_pmid,
        newly_retracted=newly_retracted,
        un_retracted=un_retracted,
        changed_fields=changed_fields,
        recorded_from=provenance_of(entry.sources),
        sources=entry.sources,
    )


def _merged_record(outcome, requested_pmid: str):
    """The :class:`MergedRecord` for *requested_pmid*, or ``None`` if none merged it.

    ``check_entries`` fetches one PMID at a time, so the parser's single-request merge rule
    fires and pairs the merge unambiguously (``requested_pmid`` is set). We still tolerate a
    ``None`` (ambiguous) pairing — with a lone request there can be at most one merged
    record, and it is this entry's — so the match is by the requested id, falling back to a
    solitary unpaired merge.
    """
    for record in outcome.merged:
        if record.requested_pmid == requested_pmid:
            return record
    for record in outcome.merged:
        if record.requested_pmid is None:
            return record
    return None


def _merged_check(entry: LedgerEntry, merged) -> RefreshCheck:
    """A :data:`STATUS_MERGED` result carrying both the requested and the returned PMID.

    The retraction status of the survivor is read for the report, but the merge is only
    *offered*: nothing here re-keys the KB, and :func:`apply_auto_update` skips it entirely,
    so ``--auto-update`` never follows a merge either. ``reason`` carries the survivor id so
    the porcelain row exposes it without a new column."""
    return RefreshCheck(
        pmid=entry.pmid,
        status=STATUS_MERGED,
        returned_pmid=merged.returned_pmid,
        recorded_doi=entry.recorded_doi,
        recorded_journal=entry.recorded_journal,
        current_retracted=bool(merged.work.retracted),
        current_notice_pmid=merged.work.retraction_notice_pmid,
        recorded_retracted=entry.recorded_retracted,
        recorded_from=provenance_of(entry.sources),
        reason=f"merged into PMID {merged.returned_pmid}",
        sources=entry.sources,
    )


def _deleted_check(entry: LedgerEntry) -> RefreshCheck:
    """A :data:`STATUS_DELETED` result for a PMID PubMed no longer serves.

    Flagged for human review; the caller keeps the KB entry untouched (never dropped), and
    :func:`apply_auto_update` skips it (nothing to write for a record that is gone)."""
    return RefreshCheck(
        pmid=entry.pmid,
        status=STATUS_DELETED,
        recorded_doi=entry.recorded_doi,
        recorded_journal=entry.recorded_journal,
        recorded_retracted=entry.recorded_retracted,
        recorded_from=provenance_of(entry.sources),
        reason="deleted upstream (PubMed serves no record under this PMID)",
        sources=entry.sources,
    )


def _unparseable_check(entry: LedgerEntry, unparseable) -> RefreshCheck:
    """A :data:`STATUS_ERROR` result for a request whose returned record could not be
    reduced (no PMID).

    Distinct from a deletion: a record DID come back — it just could not be parsed — so
    this must never read as "deleted upstream". The per-record raise message(s) are carried
    through so the loss is inspectable, exactly as the parser's ``unparseable`` bucket keeps
    it visible rather than swallowed."""
    reasons = "; ".join(u.reason for u in unparseable) or "record could not be parsed"
    return RefreshCheck(
        pmid=entry.pmid,
        status=STATUS_ERROR,
        reason=(
            f"PubMed returned a record that could not be parsed ({reasons}); "
            "retraction status not confirmed (not a deletion)"
        ),
        recorded_doi=entry.recorded_doi,
        recorded_journal=entry.recorded_journal,
        recorded_retracted=entry.recorded_retracted,
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

    A PMID PubMed no longer serves is not collapsed into a generic error — the parser
    classifies it and #170 keeps the four cases apart:

    * **merged** — efetch returned the record under a *different* PMID (NCBI merged two
      records). Reported as :data:`STATUS_MERGED` carrying **both** the requested and the
      returned PMID, and the KB is *never* silently re-keyed: the PMID is a
      ``CROSS_SOURCE_IDS`` identifier, so changing it changes what a future import merges
      against — a human's decision (P1). The merge is *offered*, not followed, even under
      ``--auto-update`` (:func:`apply_auto_update` skips it), mirroring #106's "``--yes``
      may not clear a withdrawal".
    * **deleted** — efetch returned an empty (well-formed) response: the PMID is gone
      upstream. Reported as :data:`STATUS_DELETED` and **flagged for human review**; the KB
      entry is *never* dropped, because a deleted PMID is usually a signalled removal a
      fact-checking tool must surface, and erasing the entry would destroy the evidence.
    * **unparseable** — a record *did* come back but could not be reduced (no ``<PMID>``).
      A per-id :data:`STATUS_ERROR`, **not** a deletion: the parser lists the requested id
      in ``deleted`` (nothing matched) *and* the record in ``unparseable``, so this checks
      ``unparseable`` first — a deletion is inferred only from a genuinely empty response,
      never by elimination.

    Connection and rate-limit/service failures **propagate** (the caller decides the exit
    code) — a network failure is never mistaken for a deletion, so a flaky connection can
    never flag a live paper as gone.

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
                    recorded_doi=entry.recorded_doi,
                    recorded_journal=entry.recorded_journal,
                    recorded_retracted=entry.recorded_retracted,
                    recorded_from=provenance_of(entry.sources),
                    sources=entry.sources,
                )
            )
        else:
            # Consume the parser's FOUR signals explicitly — never collapse them or infer a
            # deletion by elimination. A returned record with no PMID lands in
            # `outcome.unparseable` while the requested id ALSO lands in `outcome.deleted`
            # (nothing matched it), so an "else -> deleted" would call a record that DID come
            # back "deleted upstream" — a false claim. Order: present -> merged ->
            # unparseable(error) -> deleted, so a genuine empty response is the ONLY path to
            # STATUS_DELETED. #170 keeps merged/deleted apart; neither rewrites or drops the
            # KB entry.
            present = {r.requested_pmid: r.work for r in outcome.present}
            work = present.get(entry.pmid)
            merged = _merged_record(outcome, entry.pmid)
            if work is not None:
                results.append(_diff(entry, work))
            elif merged is not None:
                results.append(_merged_check(entry, merged))
            elif outcome.unparseable:
                results.append(_unparseable_check(entry, outcome.unparseable))
            elif entry.pmid in outcome.deleted:
                results.append(_deleted_check(entry))
            else:
                # Defensive: a shape none of the four buckets claim for this id. Never guess
                # "deleted" — surface it as a per-id error.
                results.append(
                    RefreshCheck(
                        pmid=entry.pmid,
                        status=STATUS_ERROR,
                        reason=(
                            "PubMed returned a response this PMID could not be resolved "
                            "against (not present, merged, unparseable, or deleted); "
                            "retraction status not confirmed"
                        ),
                        recorded_doi=entry.recorded_doi,
                        recorded_journal=entry.recorded_journal,
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
    merged: int = 0
    deleted: int = 0
    errors: int = 0
    skipped: int = 0


def summarize(
    results: Iterable[RefreshCheck], skipped: Sequence[RefreshCheck]
) -> Summary:
    """Count outcomes. ``retracted`` counts newly-retracted records and ``un_retracted``
    counts records PubMed no longer flags but the KB still does; ``merged``/``deleted``
    count PMIDs PubMed re-keyed or removed upstream (#170). ``checked`` excludes skipped,
    errored, merged, and deleted — none of those confirmed a metadata state."""
    summary = Summary(skipped=len(skipped))
    for result in results:
        if result.status == STATUS_ERROR:
            summary.errors += 1
            continue
        if result.status == STATUS_MERGED:
            summary.merged += 1
            continue
        if result.status == STATUS_DELETED:
            summary.deleted += 1
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
# --auto-update: the PubMed sibling of openalex-refresh's update_source caller (#169)
# --------------------------------------------------------------------------- #

#: A ledger was rewritten (a doi/journal field genuinely moved).
UPDATE_WRITTEN = "updated"
#: The narrow fields already matched the ledger — nothing was written, so the file stays
#: byte- and ``mtime_ns``-identical.
UPDATE_UNCHANGED = "unchanged"
#: The record is known only from front matter (imported before provenance ledgers), so
#: there is no ledger to update. ``--auto-update`` does **not** create one (#110/#172).
UPDATE_NO_LEDGER = "no-ledger"
#: A ledger could not be read or written while updating it — that record's problem,
#: reported per-id, never a batch crash.
UPDATE_ERROR = "error"


@dataclass(frozen=True)
class LedgerRefresh:
    """What ``--auto-update`` did (or declined to do) for one record.

    ``status`` is one of the ``UPDATE_*`` constants. ``ledgers`` names the sidecars
    actually rewritten (empty unless :data:`UPDATE_WRITTEN`). ``fields`` names the
    :data:`AUTO_UPDATE_FIELDS` that moved. ``reason`` explains a non-write outcome.
    """

    pmid: str
    status: str
    ledgers: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    reason: str = ""


def _refreshed_fields(existing: SourceRecord, result: RefreshCheck) -> dict:
    """The record's fields with *only* :data:`AUTO_UPDATE_FIELDS` replaced.

    ``retracted`` (and its ``retraction_notice_pmid`` / ``retraction_verified_at``
    companions) and the ``pubmed_mesh_*`` topic fields are copied through **verbatim**: a
    refresh never writes the retraction human-gate signal, so it keeps surfacing until a
    human records it via ``pubmed-acknowledge-retraction``, and MeSH is not an identifier.
    A ``None`` incoming value (the DOI/journal disappeared upstream) is written as ``None``
    and dropped on serialization by :meth:`SourceRecord.to_dict`, so the ledger reflects the
    current upstream state rather than freezing a stale one. Everything else — ``imported_at``
    (top level, untouched) and any co-resident non-PubMed record — is left alone.
    """
    fields = dict(existing.fields)
    for key in AUTO_UPDATE_FIELDS:
        fields[key] = getattr(result, f"current_{key}")
    return fields


def apply_auto_update(
    results: Sequence[RefreshCheck],
    kb_root: Path | str,
) -> list[LedgerRefresh]:
    """Write the narrow identifier/journal fields (:data:`AUTO_UPDATE_FIELDS`) of each
    checked record into its provenance ledger(s), and nothing else.

    The PubMed sibling of ``openalex-refresh --auto-update``, and another legitimate caller
    of :func:`provenance.update_source`. The narrow contract:

    * Only ``doi`` and ``journal`` are rewritten. ``retracted`` (and its companions) is
      copied verbatim; every other ledger field, every non-PubMed record in the same
      ledger, and the original ``.md`` are untouched (the ``.md`` is never opened, so it
      stays byte- and ``mtime_ns``-identical).
    * A record whose narrow fields already match is a **byte-identical no-op** — the exact
      condition the report calls ``unchanged`` (#121).
    * A **retraction is surfaced but never absorbed**: a newly-detected retraction keeps
      the recorded (absent/False) ``retracted`` value, so it surfaces on every run until
      ``pubmed-acknowledge-retraction`` records a human's decision.
    * A **front-matter-only record gets no ledger** (#110/#172): reported
      :data:`UPDATE_NO_LEDGER`, left for ``pubmed-backfill-provenance`` to give it one.
    * A ledger that will not read **or write** is that record's problem — a per-id
      :data:`UPDATE_ERROR`, never a batch crash.

    A result that is not a confirmed diff is ignored — an error, a **merged** PMID, or a
    **deleted** PMID (#170). Merged is the load-bearing case: it is *surface-only*, so
    ``--auto-update`` **never** re-keys a merged record. Re-keying a PMID changes what a
    future import merges against (it is a ``CROSS_SOURCE_IDS`` identifier), a consequence
    beyond one sidecar and a human's decision (P1) — the same reason ``--yes`` may not clear
    a withdrawal (#106). A deleted record is gone upstream and has nothing to write. Only
    :data:`STATUS_UNCHANGED`/:data:`STATUS_CHANGED` results are eligible. Returns one
    :class:`LedgerRefresh` per record acted on, in ``pmid`` order.
    """
    root = Path(kb_root)
    outcomes: list[LedgerRefresh] = []
    for result in results:
        # Only a confirmed diff is writable. STATUS_ERROR, STATUS_MERGED, and STATUS_DELETED
        # are all skipped here — merged is surface-only (a re-key is a human's P1 decision,
        # #170), deleted is gone, and an error had nothing to confirm.
        if result.status not in (STATUS_UNCHANGED, STATUS_CHANGED):
            continue

        # A record spoken for only by `sources/*.md` has no ledger to update.
        sidecars = [s for s in result.sources if not str(s).startswith("sources/")]
        if not sidecars:
            outcomes.append(
                LedgerRefresh(
                    pmid=result.pmid,
                    status=UPDATE_NO_LEDGER,
                    reason=(
                        "front matter only, no provenance ledger to update. Run "
                        f"`factlog {BACKFILL_COMMAND}` to give it one; --auto-update will "
                        "not fabricate a ledger."
                    ),
                )
            )
            continue

        written: list[str] = []
        moved: set[str] = set()
        errors: list[str] = []
        for rel in sidecars:
            path = root / rel
            # Reading AND writing are that record's problem — guarding only the read ships a
            # batch crash, because `write_provenance` (and its `mkdir`) re-raise `OSError`.
            try:
                provenance = read_provenance(path)
            except (ProvenanceError, OSError) as exc:
                errors.append(f"{rel}: {exc}")
                continue
            existing = next(
                (
                    r
                    for r in provenance.records
                    if r.type == _PUBMED_TYPE and r.id == result.pmid
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
            # record leaves the ledger byte- and mtime_ns-identical.
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
                    pmid=result.pmid,
                    status=UPDATE_ERROR,
                    ledgers=tuple(sorted(written)),
                    fields=tuple(sorted(moved)),
                    reason="; ".join(errors),
                )
            )
        elif written:
            outcomes.append(
                LedgerRefresh(
                    pmid=result.pmid,
                    status=UPDATE_WRITTEN,
                    ledgers=tuple(sorted(written)),
                    fields=tuple(sorted(moved)),
                )
            )
        else:
            outcomes.append(LedgerRefresh(pmid=result.pmid, status=UPDATE_UNCHANGED))

    outcomes.sort(key=lambda u: u.pmid)
    return outcomes


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


def merged_note(result: RefreshCheck) -> str:
    """The line for a PMID PubMed merged into a different one (#170).

    Names **both** the requested PMID and the survivor it now serves the record under, and
    *offers* — does not perform — updating the KB's PMID. The PMID is a ``CROSS_SOURCE_IDS``
    identifier (``common/source_writer.py``): changing it changes what a future import
    merges against, a consequence beyond one sidecar, so following the merge is a human's
    decision (P1) — this command surfaces it and stops. ``--auto-update`` does not follow a
    merge either; surfacing is the safe default.
    """
    retracted = (
        f" The surviving record is currently reported RETRACTED by PubMed"
        f"{f' (notice PMID {result.current_notice_pmid})' if result.current_notice_pmid else ''}."
        if result.current_retracted
        else ""
    )
    where = "front matter" if result.recorded_from == "front-matter" else "ledger"
    return (
        f"PubMed no longer serves PMID {result.pmid} directly; it now returns the record "
        f"under PMID {result.returned_pmid} (the two were merged upstream). The KB still "
        f"records {result.pmid}; it is NOT rewritten, because a PMID is a cross-source "
        f"identifier and changing it changes what a future import merges against — a human "
        f"decision. Review both PMIDs and, if the merge is right, update the {where} by "
        f"hand.{retracted}"
    )


def deleted_note(result: RefreshCheck) -> str:
    """The line for a PMID PubMed no longer serves at all (#170).

    A deletion is an empty (well-formed) efetch response — not a network failure — so it is
    reported as its own signal and **flagged for human review**. The KB entry is kept: a
    deleted PMID is usually a signalled removal a fact-checking tool must surface, and
    dropping the entry because upstream did would destroy the evidence that something
    happened. Nothing under ``sources/`` or the sidecar is touched.
    """
    where = "front matter" if result.recorded_from == "front-matter" else "ledger"
    return (
        f"PubMed serves no record under PMID {result.pmid} (an empty response, not a "
        f"network error): the PMID has been deleted upstream. This is flagged for human "
        f"review; the KB entry is kept, not dropped — a deleted PMID is usually a signalled "
        f"removal, and erasing the {where} would destroy the evidence. Confirm what "
        f"happened before trusting any claim from this record."
    )


def _sources_suffix(result: RefreshCheck) -> str:
    return f"  (sources: {', '.join(result.sources)})" if result.sources else ""


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
    """The human-readable stdout report. Retractions lead; then reversals; then
    identifier/journal divergences; then per-id errors; then, under ``--auto-update``, what
    was written; then the tally."""
    total = len(results) + len(skipped)
    header = f"Checked {summary.checked} of {total} PubMed record(s) in KB: {target}"
    if skipped:
        header += (
            f"\n  ({len(skipped)} skipped: checked within the last {_days(older_than_days)}. "
            "A retraction or a field change that appeared since their last check is NOT "
            "detected — PubMed only says so when asked. Run with --older-than 0 to force a "
            "re-check.)"
        )
    lines = [header]

    retracted = [r for r in results if r.newly_retracted]
    un_retracted = [r for r in results if r.un_retracted]
    changed = [r for r in results if r.status == STATUS_CHANGED and r.changed_fields]
    merged = [r for r in results if r.status == STATUS_MERGED]
    deleted = [r for r in results if r.status == STATUS_DELETED]
    errors = [r for r in results if r.status == STATUS_ERROR]

    if retracted:
        lines.append("\nNewly reported as retracted by PubMed (unverified; confirm before trusting):")
        for result in retracted:
            lines.append(f"  ⚠ {retraction_note(result)}{_sources_suffix(result)}")

    if un_retracted:
        lines.append("\nNo longer reported as retracted (PubMed reversed a retraction this KB records):")
        for result in un_retracted:
            lines.append(f"  ↺ {un_retraction_note(result)}{_sources_suffix(result)}")

    if changed:
        lines.append(
            "\nIdentifier/journal diverged (PubMed now says X; the ledger recorded Y — a "
            "transcription correction, not a claim about the world):"
        )
        for result in changed:
            details = "; ".join(
                _field_change(name, recorded, current)
                for name, recorded, current in (
                    ("doi", result.recorded_doi, result.current_doi),
                    ("journal", result.recorded_journal, result.current_journal),
                )
                if name in result.changed_fields
            )
            lines.append(
                f"  ~ {result.pmid} ({_recorded_in(result)}): "
                f"{details}{_sources_suffix(result)}"
            )

    if merged:
        lines.append("\nMerged upstream (offered, not followed — a human decides whether to re-key):")
        for result in merged:
            lines.append(f"  ⤳ {merged_note(result)}{_sources_suffix(result)}")

    if deleted:
        lines.append("\nDeleted upstream (flagged for review; the KB entry is kept, never dropped):")
        for result in deleted:
            lines.append(f"  ⚑ {deleted_note(result)}{_sources_suffix(result)}")

    if errors:
        lines.append("\nCould not check:")
        for result in errors:
            lines.append(f"  ✗ {result.pmid}: {result.reason}{_sources_suffix(result)}")

    lines.extend(_auto_update_lines(updates))

    lines.append("\nSummary:")
    lines.append(f"  {'Checked:':<22}{summary.checked}")
    lines.append(f"  {'Up to date:':<22}{summary.unchanged}")
    lines.append(f"  {'Metadata changed:':<22}{summary.changed}")
    lines.append(f"  {'Retracted (new):':<22}{summary.retracted}")
    lines.append(f"  {'Retracted (reversed):':<22}{summary.un_retracted}")
    lines.append(f"  {'Merged upstream:':<22}{summary.merged}")
    lines.append(f"  {'Deleted upstream:':<22}{summary.deleted}")
    lines.append(f"  {'Errors:':<22}{summary.errors}")
    lines.append(f"  {'Skipped:':<22}{summary.skipped}")
    if updates:
        lines.append(
            f"  {'Ledgers updated:':<22}{sum(1 for u in updates if u.status == UPDATE_WRITTEN)}"
        )
    else:
        lines.append(
            "\nNothing was written: pubmed-refresh reports drift and stops. A new retraction "
            "is for a human to act on; run with --auto-update to record identifier/journal "
            "corrections (never retraction) in the ledger."
        )
    return lines


def _auto_update_lines(updates: Sequence[LedgerRefresh]) -> list[str]:
    """The ``--auto-update`` section: what was written, what has no ledger, and any per-id
    write error. Empty when off. A byte-identical no-op (:data:`UPDATE_UNCHANGED`) is
    intentionally silent — there is nothing to report about a record left untouched."""
    if not updates:
        return []
    written = [u for u in updates if u.status == UPDATE_WRITTEN]
    no_ledger = [u for u in updates if u.status == UPDATE_NO_LEDGER]
    errors = [u for u in updates if u.status == UPDATE_ERROR]
    lines: list[str] = []
    if written:
        lines.append(
            "\nLedger updated (identifier/journal fields only — doi, journal; retraction is "
            "never written):"
        )
        for u in written:
            ledgers = f"  ({', '.join(u.ledgers)})" if u.ledgers else ""
            moved = ", ".join(u.fields) if u.fields else "-"
            lines.append(f"  ✎ {u.pmid}: recorded {moved}{ledgers}")
    if no_ledger:
        lines.append(
            "\nNot auto-updated (no ledger; front matter only — run "
            f"`factlog {BACKFILL_COMMAND}` to create one):"
        )
        for u in no_ledger:
            lines.append(f"  · {u.pmid}")
    if errors:
        lines.append("\nCould not auto-update:")
        for u in errors:
            lines.append(f"  ✗ {u.pmid}: {u.reason}")
    return lines


def porcelain_lines(
    results: Sequence[RefreshCheck],
    skipped: Sequence[RefreshCheck],
    summary: Summary,
    *,
    target: Path,
    updates: Sequence[LedgerRefresh] = (),
) -> list[str]:
    """The machine contract on stdout: one tab-separated ``check`` row per record, then —
    only under ``--auto-update`` — one ``update`` row per acted-on record, then tallies.
    Parse by the first field.

    ``check\\t<pmid>\\t<status>\\t<retracted>\\t<un_retracted>\\t<reason>\\t<changed_fields>``
    with ``retracted``/``un_retracted`` as ``0``/``1`` and ``changed_fields`` comma-joined
    (empty for none). ``changed_fields`` is appended last so a parser keying on the earlier
    fixed columns is unaffected. A ``merged`` row carries the survivor PMID in ``<reason>``
    (``merged into PMID <id>``) so a consumer sees both ids without a new column; a
    ``deleted`` row's ``<reason>`` names the deletion. The ``update`` rows are
    ``update\\t<pmid>\\t<status>\\t<fields>\\t<ledgers>``. Progress and the estimate stay on
    stderr only.
    """
    rows: list[str] = []
    for result in sorted([*results, *skipped], key=lambda r: r.pmid):
        rows.append(
            "check\t{pmid}\t{status}\t{retracted}\t{un}\t{reason}\t{changed}".format(
                pmid=porcelain_field(result.pmid),
                status=result.status,
                retracted="1" if result.newly_retracted else "0",
                un="1" if result.un_retracted else "0",
                reason=porcelain_field(result.reason),
                changed=",".join(result.changed_fields),
            )
        )
    for u in updates:
        rows.append(
            "update\t{pmid}\t{status}\t{fields}\t{ledgers}".format(
                pmid=porcelain_field(u.pmid),
                status=u.status,
                fields=",".join(u.fields),
                ledgers=porcelain_field(",".join(u.ledgers)),
            )
        )
    rows.append(f"checked\t{summary.checked}")
    rows.append(f"unchanged\t{summary.unchanged}")
    rows.append(f"changed\t{summary.changed}")
    rows.append(f"retracted\t{summary.retracted}")
    rows.append(f"un_retracted\t{summary.un_retracted}")
    rows.append(f"merged\t{summary.merged}")
    rows.append(f"deleted\t{summary.deleted}")
    rows.append(f"errors\t{summary.errors}")
    rows.append(f"skipped\t{summary.skipped}")
    if updates:
        rows.append(f"updated\t{sum(1 for u in updates if u.status == UPDATE_WRITTEN)}")
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
