# SPDX-License-Identifier: Apache-2.0
"""Backfill a provenance ledger for a paper that has front matter but none (issue #113).

## Why a fourth reader/writer of the ledger exists

``common/provenance.py`` splits *who* may write the ledger. ``add_source`` is what an
*import* calls: it materializes a record the KB already believes and refuses to revise a
diverging one (#58, #63). ``update_source`` is what a *refresh* calls, having gone to the
upstream API to learn a new value (#83). Acknowledgement is a *human* revising an
existing record (#93, ``common/acknowledge.py``).

A **backfill** is none of those, and needs no new primitive. A paper imported before the
ledger existed has front matter and no sidecar, so a re-import short-circuits on the
front-matter identity match before it ever reaches the sidecar writer, and its ledger is
never created — a signal that fires on every run can never be acknowledged (both
acknowledge commands refuse a paper with no ledger, and point here). The fix has no
record to revise: it materializes what the ``.md`` already asserts, which is exactly
``add_source``'s contract. So this is ``add_source`` into a fresh sidecar, and nothing
more.

## No network, ever

Querying upstream would make this a *refresh*, whose write is ``update_source`` — an
authority to revise, earned by having gone upstream. A backfill has no such authority; it
records what the import recorded, not what is true *now*, so it can never silently absorb
a change that appeared after the import (the signal the import/refresh split exists to
protect). This module imports no API client, and a test asserts that.

## What is integration-specific stays in the schema

Written once per integration, the two answers drift — the shape #64 named. This module
holds the read-modify-write boundary once; each integration supplies a small
:class:`BackfillSchema`. The schema is the *only* thing that knows a field's meaning, so
this module never names one (vocabulary neutrality, #57 §6.3, #93 Q2). Membership — which
papers have front matter but no ledger — is decided by the integration's *own*
``collect_ledger_entries`` and its exported ``provenance_of`` predicate, never a second
copy: two copies of one predicate is how #64, #98 and the empty-tuple divergence fixed in
#111 all happened.

## The flat walk is inherited on purpose (#112)

``collect_ledger_entries`` walks ``sources/*.md`` **flat**, while the KB's canonical
enumeration is ``rglob`` over ``SOURCE_ROOTS``. That asymmetry is a real defect (#112),
and it must move across every consumer at once, not be patched here. Backfill reuses
``collect_ledger_entries``, so it covers exactly the set the consuming commands can read
and act on. A ledger nothing reads is worse than no ledger, so the coverage of backfill
is deliberately tied to the coverage of the commands that would consume it.

## The traps this is built around

* **``imported_at`` is read from front matter, never invented.** A pre-ledger paper this
  tool wrote carries a truthful ``imported_at``. A paper whose front matter lacks it
  (hand-written, or older than the import command) is refused per-id — reported, nothing
  written. No sentinel is written: ``read_provenance`` validates ``imported_at`` is a
  *string*, not a timestamp, so a value like ``"unknown"`` would pass validation and
  become a trap for the next reader. A refusal is honest; a sentinel is a lie.
* **A field it cannot read is omitted, never nulled.** The schema maps each ledger field
  to a value drawn from the entry the integration's ``collect_ledger_entries`` already
  parsed from front matter; a value of ``None`` (the front matter did not carry it) is
  dropped, exactly as an import drops it. This is what keeps a backfill from writing an
  identifying field as absent-then-``None`` and manufacturing a false divergence against a
  later re-import that carries the real value.
* **A no-op is byte- and ``mtime_ns``-identical.** A second backfill (or a paper whose
  fresh record equals one already in the sidecar) compares the serialized record and does
  not open the file, so the sidecar stays byte- and ``mtime_ns``-identical.
* **Both boundaries are guarded**, per id, with ``(ProvenanceError, OSError)`` — the read
  and the write. ``write_provenance`` re-raises ``OSError`` and its ``mkdir`` raises one
  too; guarding only the read is the batch crash shipped in #65, #71 and #94. One paper's
  failure is that paper's problem, never an abort of the rest.
* **A neighbour is left alone.** A paper cross-source-merged before the ledger existed may
  already have a sidecar carrying another integration's record; ``add_source`` appends
  this integration's record without disturbing the neighbour or its ``imported_at``.
* **No ``.md`` is opened for write** (P4). This module only *reads* front matter
  (``read_scalars``); every original stays byte- and ``mtime_ns``-identical.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factlog.integrations.common.front_matter import read_scalars
from factlog.integrations.common.provenance import (
    ProvenanceError,
    SourceRecord,
    add_source,
    read_provenance,
    sidecar_path,
    write_provenance,
)

__all__ = [
    "BackfillSchema",
    "BackfillResult",
    "BACKFILL_WRITTEN",
    "BACKFILL_UNCHANGED",
    "BACKFILL_REFUSED",
    "BACKFILL_ERROR",
    "backfill",
]

#: The front-matter key every source writer emits for the import timestamp. Read
#: verbatim; never invented. A source that lacks it is refused (see the module docstring).
IMPORTED_AT_KEY = "imported_at"

#: A fresh sidecar was created (or this integration's record appended to a neighbour's).
BACKFILL_WRITTEN = "backfilled"
#: The record the ledger would receive is already present, identical — nothing was
#: written, so the sidecar stays byte- and ``mtime_ns``-identical.
BACKFILL_UNCHANGED = "unchanged"
#: The source's front matter has no ``imported_at`` to read. A refusal, not a no-op:
#: nothing is written and no timestamp is invented.
BACKFILL_REFUSED = "refused"
#: A sidecar could not be read or written while backfilling — that paper's problem,
#: reported per-id, never a batch crash.
BACKFILL_ERROR = "error"


@dataclass(frozen=True)
class BackfillSchema:
    """What is integration-specific about a backfill.

    ``type`` is the provenance record type (``"arxiv"`` / ``"openalex"``).

    ``collect_entries`` is the integration's own ``collect_ledger_entries`` — the *one*
    place per integration that reads the front-matter scalar keys and maps them to ledger
    fields (#112's flat walk is inherited through it). ``provenance_of`` is the same
    integration's exported predicate; the pair decides which papers are front-matter-only
    without a second copy of either the walk or the predicate.

    ``id_of`` extracts the record id from one entry (integrations name it ``arxiv_id`` /
    ``openalex_id``). ``fields`` maps each ledger field name to a callable that reads its
    already-parsed value off the entry. A callable that returns ``None`` (the front matter
    did not carry the field) contributes nothing to the record — so a ledger field can
    only ever be populated from a value the entry actually holds.

    ``required`` names the ``fields`` a truthful ledger cannot be built without: the
    *identifying* fields whose absence from front matter is not a legitimate value but a
    sign the front matter cannot express this integration's identity at all. If any of them
    reads ``None``, the paper is refused per-id and no sidecar is written — the same honesty
    as a missing ``imported_at``.

    This is the #73/#84 identifying-field trap, and a schema-*shape* guard cannot catch it:
    the reader for arXiv's ``version`` *exists* on the entry, it just holds ``None`` when an
    OpenAlex-authored ``.md`` (which echoes ``arxiv_id`` but never emits ``arxiv_version``)
    is collected front-matter-only. Writing that record leaves ``version`` absent, and a
    later arXiv merge import then reads it as ``{version: None}``, diverges from the live
    ``{version: N}`` and errors permanently — a false conflict the backfill manufactured.
    ``required`` refuses to build such a record.

    ``required`` is only the identifying fields whose ``None`` means *unreadable*, never
    those whose ``None`` is a legitimate value an import would record too. For arXiv it is
    ``("version",)`` — an authentic deposit ``.md`` always carries ``arxiv_version`` —
    while arXiv's other identifying field is optional in front matter and its absence is a
    real, recordable state, so it is *not* required. For OpenAlex, whose writer declares no
    identifying fields and whose id key is emitted only by its own writer, it is ``()``.
    """

    type: str
    collect_entries: Callable[[Path | str], tuple[Sequence[Any], Sequence[Any]]]
    provenance_of: Callable[[Sequence[str]], str]
    id_of: Callable[[Any], str]
    fields: Mapping[str, Callable[[Any], Any]]
    required: tuple[str, ...] = ()


@dataclass(frozen=True)
class BackfillResult:
    """What backfilling one front-matter-only paper did.

    ``status`` is one of the ``BACKFILL_*`` constants. ``ledger`` names the sidecar
    written (empty unless :data:`BACKFILL_WRITTEN`, and empty on a no-op). ``reason``
    explains a refusal or a per-id error.
    """

    entry_id: str
    status: str
    ledger: str = ""
    reason: str = ""


def _relative(path: Path, kb_root: Path) -> str:
    try:
        return str(path.relative_to(kb_root))
    except ValueError:
        return str(path)


def _record_fields(entry: Any, schema: BackfillSchema) -> dict[str, Any]:
    """The ledger fields for *entry*, drawn from front matter via the schema.

    A field whose value is ``None`` (the front matter did not carry it) is omitted, not
    written as ``None`` — the same absence an import produces, and the guard that keeps a
    backfill from manufacturing an identifying-field divergence.
    """
    out: dict[str, Any] = {}
    for name, reader in schema.fields.items():
        value = reader(entry)
        if value is not None:
            out[name] = value
    return out


def _backfill_source(
    kb_root: Path,
    entry_id: str,
    source_rel: str,
    fields: Mapping[str, Any],
    missing_required: Sequence[str],
    schema: BackfillSchema,
) -> BackfillResult:
    """Materialize one front-matter-only paper's ledger into its sidecar.

    Refuses per-id, writing nothing, when an identifying field the ledger's identity turns
    on cannot be read from front matter (*missing_required*) — the front matter cannot
    supply this integration's identity, so a ledger built from it would manufacture a false
    divergence against a later import (the #73/#84 trap). Otherwise reads ``imported_at``
    from the source's front matter (refusing per-id if absent), builds the record, and
    ``add_source``s it into ``sidecar_path(source)``. Both the read and the write are
    guarded with ``(ProvenanceError, OSError)`` so one paper's failure is that paper's
    problem, and a record already present identically is a byte- and ``mtime_ns``-identical
    no-op.
    """
    if missing_required:
        return BackfillResult(
            entry_id=entry_id,
            status=BACKFILL_REFUSED,
            reason=(
                f"{source_rel} front matter cannot supply the {schema.type} identifying "
                f"field(s) {', '.join(missing_required)} (this looks like another "
                "integration's record echoing the id, not this integration's own deposit); "
                "a backfill will not build a ledger whose identity it cannot read, so this "
                "paper is left for a re-import."
            ),
        )
    source_path = kb_root / source_rel
    # Only read: read_scalars opens the .md read-only (P4), and swallows an OSError into an
    # empty mapping, so an unreadable source reads as "no imported_at" — a refusal, below.
    imported_at = read_scalars(source_path, (IMPORTED_AT_KEY,)).get(IMPORTED_AT_KEY)
    if not imported_at:
        return BackfillResult(
            entry_id=entry_id,
            status=BACKFILL_REFUSED,
            reason=(
                f"{source_rel} has no {IMPORTED_AT_KEY} in its front matter; a backfill "
                "reads the import timestamp verbatim and never invents one, so this "
                "paper is left for a re-import to give it a ledger."
            ),
        )

    record = SourceRecord(
        type=schema.type, id=entry_id, imported_at=imported_at, fields=fields
    )
    sidecar = sidecar_path(source_path)
    rel = _relative(sidecar, kb_root)

    # The READ is guarded: a corrupt or unreadable sidecar (a partially-populated one that
    # will not parse, or a permission fault) is this paper's per-id problem, never a crash.
    try:
        provenance = read_provenance(sidecar)
    except (ProvenanceError, OSError) as exc:
        return BackfillResult(entry_id, BACKFILL_ERROR, reason=f"{rel}: {exc}")

    existing = next(
        (r for r in provenance.records if r.key == record.key), None
    )
    if existing is not None:
        # Already backfilled (or an import beat us to it): an identical record is a byte-
        # and mtime_ns-identical no-op — do not open the file. A *different* record for the
        # same (type, id) means the sidecar disagrees with front matter; refuse per-id
        # rather than overwrite an audit entry (the same stance add_source takes).
        if existing.to_dict() == record.to_dict():
            return BackfillResult(entry_id, BACKFILL_UNCHANGED)
        return BackfillResult(
            entry_id,
            BACKFILL_ERROR,
            reason=(
                f"{rel}: a different {schema.type} record for id {entry_id!r} is already "
                "in the sidecar; refusing to overwrite an audit entry"
            ),
        )

    # The WRITE is guarded too: write_provenance re-raises OSError and its mkdir raises one
    # — guarding only the read is the crash shipped in #65, #71 and #94. add_source appends
    # this record beside any neighbour without disturbing it.
    try:
        add_source(provenance, record)
        write_provenance(sidecar, provenance)
    except (ProvenanceError, OSError) as exc:
        return BackfillResult(entry_id, BACKFILL_ERROR, reason=f"{rel}: {exc}")

    return BackfillResult(entry_id, BACKFILL_WRITTEN, ledger=rel)


def backfill(kb_root: Path | str, schema: BackfillSchema) -> list[BackfillResult]:
    """Give every front-matter-only paper a provenance ledger, returning one result each.

    Membership is decided by the integration's own ``collect_ledger_entries`` and exported
    ``provenance_of`` — never a second copy — so backfill covers exactly the set the
    consuming commands can read (the #112 flat walk is inherited, not fixed here). A paper
    that already has a ledger is ``ledger``-classified by ``provenance_of`` and skipped
    entirely, so it stays byte- and ``mtime_ns``-identical.

    Each front-matter-only paper is materialized with :func:`_backfill_source`: its
    ``imported_at`` is read from front matter (refused per-id if absent), a record is built
    from the schema's fields, and it is ``add_source``d into a fresh sidecar. Both the read
    and write are guarded per id, so one paper's failure never aborts the rest. Results are
    returned in ``entry_id`` order for reproducibility.
    """
    root = Path(kb_root)
    entries, _errors = schema.collect_entries(root)

    results: list[BackfillResult] = []
    for entry in entries:
        # The integration's own predicate: a paper whose sources are all `sources/*.md` is
        # front-matter-only; anything the ledgers cover is left untouched.
        sources = getattr(entry, "sources", ()) or ()
        if schema.provenance_of(sources) != "front-matter":
            continue
        entry_id = schema.id_of(entry)
        fields = _record_fields(entry, schema)
        # An identifying field whose reader returns None is unreadable from this front
        # matter (e.g. an OpenAlex-authored .md echoing arxiv_id but carrying no
        # arxiv_version). Refuse rather than write a record with an absent identifying
        # field, which a later import would call a divergence and error on (#73/#84).
        missing_required = tuple(
            name for name in schema.required if schema.fields[name](entry) is None
        )
        for source_rel in sources:
            results.append(
                _backfill_source(
                    root, entry_id, source_rel, fields, missing_required, schema
                )
            )

    results.sort(key=lambda r: (r.entry_id, r.ledger))
    return results
