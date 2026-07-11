# SPDX-License-Identifier: Apache-2.0
"""The third authorized writer of the provenance ledger: a human acknowledgement.

## Why a third writer exists

``common/provenance.py`` deliberately splits who may write the ledger. ``add_source``
is what an *import* calls and refuses to revise an existing ``(type, id)`` — an import
has no authority to rewrite the ledger (#58, #63). ``update_source`` is what a *refresh*
calls, having gone to the upstream API to learn a new value (#83).

Acknowledgement is neither. It is a **human** recording a decision — "I saw this, I have
decided" — about a signal the ledger already carries. arXiv's ``withdrawn_by`` and
OpenAlex's ``is_retracted`` are both such signals: a refresh may never absorb them
(doing so would silence the human gate the import/refresh split exists to protect —
arXiv #79, OpenAlex ``refresh.py`` H1), yet a warning that fires on every run forever is
how an operator learns to skim past the alarm the KB most needs read (#93).

## Why it is shared, not written twice

Written once per integration, the two answers drift — the shape #64 named and PR #97
avoided by generalizing ``common/check_log.py`` behind a per-integration schema. This
module follows that precedent: the read-modify-write boundary lives here once, and each
integration supplies a small :class:`AcknowledgeSchema` naming the record ``type``
(``"arxiv"`` / ``"openalex"``) and the ledger field it owns (``withdrawn_by`` /
``is_retracted``).

The vocabulary stays per-integration and this module is neutral about both words:
"withdrawn" belongs to arXiv, "retracted" to OpenAlex (#57 §6.3, #93 Q2), and neither
appears here. This is one writing primitive, not one concept.

## What the primitive does, and the traps it is built around

Given a KB root, a record ``(type, id)`` and a value, it writes that value into the
record's ``fields`` in **every** provenance sidecar that carries the ``(type, id)``, via
``update_source``. The value may be ``None``, which — because
:meth:`SourceRecord.to_dict` drops ``None`` on serialization — **removes** the field.

* **Set and clear are both this primitive's job.** ``withdrawn_by`` is an *identifying*
  field (``arxiv/source_writer.py:110``); an un-withdrawn paper whose ledger still reads
  ``withdrawn_by: "author"`` diverges from a fresh import that parses ``None``, so
  re-import errors permanently and a refresh may not clear it (``AUTO_UPDATE_FIELDS``
  excludes it). Only the human's write may. Adding a separate "acknowledged" field would
  leave the identifying field stale and re-import still broken, so the clear must land in
  the same field — this primitive owns it.
* **Both boundaries are guarded**, per id, with ``(ProvenanceError, OSError)`` — the read
  *and* the write. ``write_provenance`` re-raises ``OSError`` and its ``mkdir`` raises one
  too; guarding only the read is the batch crash shipped in #65, #71 and #94. A failure on
  one sidecar is that record's problem, never an abort of the rest, and a partial write is
  an :data:`ACK_ERROR`, not a success.
* **A paper may be named by several ledgers.** Every sidecar carrying the ``(type, id)``
  is updated; the match is on the id, never on position.
* **A no-op is byte- and ``mtime_ns``-identical.** Acknowledging a value the ledger
  already holds compares the serialized record and does not open the file, so the sidecar
  is left untouched to the byte and to the mtime.
* **Blast radius of one record.** Only the target ``(type, id)``'s named field moves;
  every other field of that record (``imported_at`` included) and every other record in
  the same sidecar are left exactly as they were.
* **No ledger is ever created.** Creating a ledger invents ``imported_at`` and the
  provenance origin — an *import*'s write (#58, #63). A ``(type, id)`` that no ledger
  carries is a reported :data:`ACK_NO_LEDGER`, never a silent no-op and never a new file.
* **No ``.md`` is opened, ever** (P4). This module touches only ``source-provenance/``.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from factlog.integrations.common.provenance import (
    SIDECAR_DIR,
    ProvenanceError,
    SourceRecord,
    read_provenance,
    update_source,
    write_provenance,
)

__all__ = [
    "AcknowledgeSchema",
    "AcknowledgeResult",
    "AcknowledgeLookup",
    "ACK_WRITTEN",
    "ACK_UNCHANGED",
    "ACK_NO_LEDGER",
    "ACK_ERROR",
    "acknowledge",
    "acknowledge_all",
    "lookup",
]

#: The named field was set or cleared in at least one ledger (a value moved).
ACK_WRITTEN = "acknowledged"
#: Every ledger carrying the record already held the value — nothing was written, so each
#: sidecar stays byte- and ``mtime_ns``-identical.
ACK_UNCHANGED = "unchanged"
#: No provenance ledger carries this ``(type, id)``. A refusal, not a no-op: this
#: primitive never fabricates a ledger (that is an import's write).
ACK_NO_LEDGER = "no-ledger"
#: A ledger could not be read or written while acknowledging — that record's problem,
#: reported per-id, never a batch crash. Set even if a sibling ledger was written: a
#: partial write is an error, not a success.
ACK_ERROR = "error"


@dataclass(frozen=True)
class AcknowledgeSchema:
    """What is integration-specific about an acknowledgement: the record ``type`` and the
    ledger field the human's decision is recorded in.

    ``type`` is the provenance record type (``"arxiv"`` / ``"openalex"``); ``field`` is
    the ledger field name (``withdrawn_by`` / ``is_retracted``). The vocabulary — which
    of "withdrawn" / "retracted" the field means — stays entirely on the integration
    side; this module never inspects the name's meaning.
    """

    type: str
    field: str


@dataclass(frozen=True)
class AcknowledgeLookup:
    """What a **read-only** scan found about one ``(type, id)`` across the KB's ledgers.

    The read-only companion to :func:`acknowledge`, for a caller that must decide —
    *before* spending an upstream request — whether a record can be acknowledged at all.
    This is the shape #107 named: verify the ledger exists *before* writing the request
    (never spend a request on a paper this command could only ever fail to write), and
    never assert "the ledger did not record X" on a ledger that could not be read. It
    shares :func:`acknowledge`'s scan (same ``rglob``, same match on the id and never on
    position) so a pre-flight refusal and the write that follows can never drift about
    which sidecars carry the id.

    ``found`` is True iff at least one sidecar carries a ``(schema.type, entry_id)``
    record. ``values`` are the distinct values the ``schema.field`` holds across those
    records, in first-seen order, and only where the field is present — an absent field
    contributes nothing, mirroring the write side where a dropped ``None`` *means* the
    field is unset. ``unreadable`` names the sidecars that would not read (same
    ``(ProvenanceError, OSError)`` guard as the write); one of them may carry the id, so a
    caller with a non-empty ``unreadable`` must refuse rather than trust an incomplete view.
    """

    entry_id: str
    found: bool
    values: tuple[object, ...] = ()
    unreadable: tuple[str, ...] = ()


@dataclass(frozen=True)
class AcknowledgeResult:
    """What acknowledging one ``(type, id)`` did across the KB's ledgers.

    ``status`` is one of the ``ACK_*`` constants. ``ledgers`` names the sidecars actually
    rewritten (empty unless :data:`ACK_WRITTEN`, and empty on a byte-identical no-op).
    ``reason`` explains a non-write outcome (the per-sidecar failures for
    :data:`ACK_ERROR`, or why there was nothing to write).
    """

    entry_id: str
    status: str
    ledgers: tuple[str, ...] = ()
    reason: str = ""


def _relative(path: Path, kb_root: Path) -> str:
    try:
        return str(path.relative_to(kb_root))
    except ValueError:
        return str(path)


def acknowledge(
    kb_root: Path | str,
    entry_id: str,
    value: object,
    schema: AcknowledgeSchema,
) -> AcknowledgeResult:
    """Write *value* into the ``schema.field`` of the ``(schema.type, entry_id)`` record
    in every provenance sidecar that carries it. Returns one :class:`AcknowledgeResult`.

    *value* of ``None`` removes the field (``to_dict`` drops ``None`` on serialization),
    which is how the human clears an identifying signal a refresh may not touch. Any other
    value is stored verbatim; this primitive is neutral about what the field means.

    The whole contract lives in the module docstring; in short: every ledger naming the id
    is updated (match on the id, never position); a value the ledger already holds is a
    byte- and ``mtime_ns``-identical no-op; only the named field of the target record
    moves (``imported_at`` and every neighbouring record and record-field untouched); no
    ``.md`` is opened; no ledger is created; and both the read and the write are guarded
    per sidecar with ``(ProvenanceError, OSError)`` so one bad sidecar is that record's
    error, not a batch crash, and a partial write is an error, not a success.
    """
    root = Path(kb_root)
    sidecar_root = root / SIDECAR_DIR

    found = False
    written: list[str] = []
    errors: list[str] = []

    paths = sorted(sidecar_root.rglob("*.json")) if sidecar_root.is_dir() else ()
    for path in paths:
        if not path.is_file():
            continue
        rel = _relative(path, root)
        # The READ is guarded: a corrupt or unreadable sidecar is this record's per-id
        # problem, never an abort of the scan across the KB's other ledgers.
        try:
            provenance = read_provenance(path)
        except (ProvenanceError, OSError) as exc:
            errors.append(f"{rel}: {exc}")
            continue

        existing = next(
            (
                r
                for r in provenance.records
                if r.type == schema.type and r.id == entry_id
            ),
            None,
        )
        if existing is None:
            continue
        found = True

        # Only the named field moves; every other field (imported_at included) is carried
        # through unchanged, and every co-resident record is left where it is.
        fields = dict(existing.fields)
        fields[schema.field] = value
        record = SourceRecord(
            type=existing.type,
            id=existing.id,
            imported_at=existing.imported_at,
            fields=fields,
        )
        # A no-op must not open the file: compare the serialized forms so a ledger that
        # already holds the value stays byte- and mtime_ns-identical. This also makes
        # clearing an already-absent field a no-op (None drops out of both).
        if record.to_dict() == existing.to_dict():
            continue

        # The WRITE is guarded too: write_provenance re-raises OSError and its mkdir
        # raises one — guarding only the read is the crash shipped in #65, #71 and #94.
        try:
            update_source(provenance, record)
            write_provenance(path, provenance)
        except (ProvenanceError, OSError) as exc:
            errors.append(f"{rel}: {exc}")
            continue
        written.append(rel)

    if errors:
        # A partial write is an error, not a success: the error status is returned even
        # when a sibling ledger was rewritten.
        return AcknowledgeResult(
            entry_id=entry_id,
            status=ACK_ERROR,
            ledgers=tuple(sorted(written)),
            reason="; ".join(errors),
        )
    if not found:
        # A record no ledger carries is a refusal, not a no-op: this primitive never
        # fabricates a ledger, because inventing imported_at and the origin is an import.
        return AcknowledgeResult(
            entry_id=entry_id,
            status=ACK_NO_LEDGER,
            reason=(
                # No article before the interpolated type: "a"/"an" cannot be chosen
                # for an arbitrary schema type (`arxiv`, `openalex`, a future
                # consonant-initial `pubmed`/`crossref`), so the sentence is worded to
                # need none (#107 item 6).
                f"no provenance ledger carries a record of type {schema.type!r} for id "
                f"{entry_id!r}; acknowledgement records a decision about an existing "
                "record and never creates one."
            ),
        )
    if written:
        return AcknowledgeResult(
            entry_id=entry_id,
            status=ACK_WRITTEN,
            ledgers=tuple(sorted(written)),
        )
    return AcknowledgeResult(entry_id=entry_id, status=ACK_UNCHANGED)


def acknowledge_all(
    kb_root: Path | str,
    entry_ids: Sequence[str],
    value: object,
    schema: AcknowledgeSchema,
) -> list[AcknowledgeResult]:
    """Acknowledge each of *entry_ids* in turn, returning one result per id in id order.

    A convenience over :func:`acknowledge` for a batch. Because the primitive captures
    every ``(ProvenanceError, OSError)`` into an :data:`ACK_ERROR` result rather than
    raising, one failing id can never abort the others — the isolation the read/write
    guards exist to provide.
    """
    return [
        acknowledge(kb_root, entry_id, value, schema)
        for entry_id in sorted(entry_ids)
    ]


def lookup(
    kb_root: Path | str,
    entry_id: str,
    schema: AcknowledgeSchema,
) -> AcknowledgeLookup:
    """Read-only scan for the ``(schema.type, entry_id)`` record across the KB's ledgers.

    The pre-flight companion to :func:`acknowledge`: it answers whether the record exists
    to be acknowledged, what value its field already holds, and which sidecars could not
    be read — *without writing anything and without opening any ``.md`` (P4)*. A caller
    uses it to refuse a paper it cannot write (or an unreadable ledger it cannot trust)
    **before** spending an upstream request (#107), so the write that follows is never a
    request-then-refuse. See :class:`AcknowledgeLookup` for the fields.

    The scan is byte-for-byte the same shape as :func:`acknowledge`'s (same ``rglob``,
    same ``(ProvenanceError, OSError)`` guard, same match on the id and never on position)
    so the two never disagree about which sidecars carry the id.
    """
    root = Path(kb_root)
    sidecar_root = root / SIDECAR_DIR

    found = False
    values: list[object] = []
    unreadable: list[str] = []

    paths = sorted(sidecar_root.rglob("*.json")) if sidecar_root.is_dir() else ()
    for path in paths:
        if not path.is_file():
            continue
        rel = _relative(path, root)
        try:
            provenance = read_provenance(path)
        except (ProvenanceError, OSError):
            # An unreadable sidecar might be the one that carries this id, so its recorded
            # value is unknown. Name it; the caller refuses rather than assert absence.
            unreadable.append(rel)
            continue
        for record in provenance.records:
            if record.type != schema.type or record.id != entry_id:
                continue
            found = True
            # An absent field contributes no value: absence is how the write side records
            # "not set" (a dropped None), so a reader treats it the same way.
            if schema.field in record.fields:
                value = record.fields[schema.field]
                if value not in values:
                    values.append(value)

    return AcknowledgeLookup(
        entry_id=entry_id,
        found=found,
        values=tuple(values),
        unreadable=tuple(sorted(unreadable)),
    )
