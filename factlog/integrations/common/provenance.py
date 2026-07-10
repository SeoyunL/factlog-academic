# SPDX-License-Identifier: Apache-2.0
"""Per-source provenance sidecar: reader/writer for a machine audit ledger.

## What this is

When one paper is imported from more than one database (OpenAlex, arXiv,
Zotero), the union of *where it came from* cannot live in the source's front
matter: ``factlog.bibtex.parse_front_matter`` is line-oriented, so a nested
list collapses and the identity is destroyed (see #58). The provenance of a
source therefore lives in a separate, mutable file *beside the KB*, not inside
the original.

This is **not** front matter. ``_textio.py`` states factlog never machine-parses
a source's front matter; a sidecar is an explicit machine ledger, read and
written by this module with a real ``json`` parser, so that principle does not
constrain it. It is JSON (stdlib, zero new dependency) rather than YAML/TOML:
PyYAML would have to become a *core* runtime dependency (the sidecar serves every
merging integration, not just arXiv) and this project keeps exactly one
(``pyrewire``); ``tomllib`` has no ``dumps``. ``json`` round-trips exactly and,
with ``sort_keys=True`` + ``indent=2`` + a trailing newline, is byte-deterministic
and human-diffable.

## Placement (decided in #63)

The sidecar is a sibling *directory* of ``sources/``, never a file inside it::

    <kb>/sources/foo.md              <- byte-immutable original (P4), never reopened
    <kb>/source-provenance/foo.json  <- mutable provenance ledger

Placing it outside ``sources/`` is what makes it invisible to source
enumeration. Every walker either resolves ``common.SOURCE_ROOTS`` (``source_files()``,
used by ``factlog status``/coverage and ``tools/coverage.py``) or hardcodes the
same two names (``factlog sources`` at ``cli.py:745``, the stale-ref audit at
``cli.py:2422``). Either way none of them descends into a sibling directory, so a
sidecar cannot be picked up as a source document — structurally, rather than by a
promise each call site must keep. A sidecar placed *inside* ``sources/`` is
counted as a source and the user is told to run ``sync`` on it; that was measured,
and it is why this directory exists.

It is deliberately not named ``provenance/``: ``factlog provenance <TERM>``
already answers "which source did this fact come from", and a directory of
source-record ledgers under the same word would collide.

``sidecar_path`` owns the naming rule; it is the only place that knows a sidecar
lives under ``source-provenance/``. It preserves any subdirectory a source sits
in, because the enumerators above use ``rglob`` and ``ingest`` mirrors an
original's subtree — a stem-only mapping would silently merge the ledgers of
``sources/a/x.md`` and ``sources/b/x.md``.

## Determinism contract

``imported_at`` is supplied by the caller and is **never** read from a clock
inside this module — the same rationale as ``BaseSourceWriter``: writers stay
pure and unit-testable, and the CLI controls the single batch timestamp.
Byte-determinism ("two writes of the same data produce identical bytes") depends
on it. Records serialize sorted by ``(type, id)``; keys within each record are
sorted by ``json.dumps(sort_keys=True)``; ``None``-valued source-specific fields
are dropped rather than written as ``null``. There is no file-level
``updated_at``: it would be housekeeping, not provenance about the paper, and it
would break byte-determinism (same reasoning that moved ``last_checked_at`` out
of ``sources/`` in #58).

``schema_version`` is written from day one so a future reader has a single hook
to judge a format it does not recognise.

## Which values the read boundary judges (#109)

``from_dict`` type-checks the three reserved keys because a non-string ``id``
survives ``json`` and dies far from the corrupt file that caused it. Two of the
*source-specific* fields need the same boundary for the same reason, and one
stronger one: they are read as **signals**, and a corrupt value does not merely
crash late — it is read as "no signal". ``refresh.py`` tests
``fields.get("is_retracted") is True``, so ``"is_retracted": "true"`` reads as
*not retracted*; the retraction direction then self-heals (upstream retracted ->
still loud) while the *un*-retraction direction is permanently invisible: nothing
surfaces, ``openalex-acknowledge-retraction`` exits 0 with "nothing to
acknowledge", and the string stays in the ledger forever. ``withdrawn_by`` has
the same shape, where any non-empty string is a truthy "some withdrawal was
recorded".

Coercing ``"true"`` to ``True`` is not the answer: ``"1"``, ``"yes"``, ``"on"``
and ``"false"`` would each need a rule, and a ledger records what a source said —
inventing a value for a corrupt one is the write this project forbids. So the
value space is enforced here, at the one boundary every consumer passes through,
and a bad value raises :class:`ProvenanceError`. Every reader already guards
``read_provenance`` with it, so strictness lands in a path that already exists:
``openalex-acknowledge-retraction`` and ``arxiv-acknowledge-withdrawal`` refuse
before their live query (zero API requests, no prompt), while ``openalex-refresh``
and ``arxiv-check-versions`` report the unreadable ledger as a per-file error and
keep going. ``write_provenance`` enforces the same rule, so this module can never
create a ledger it would then refuse to read (``common/backfill.py``, which
promotes a front-matter value into a record, refuses in its own words first).
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factlog.integrations.arxiv.work_parser import (
    WITHDRAWN_BY_ADMIN,
    WITHDRAWN_BY_AUTHOR,
)
from factlog.integrations.common._textio import atomic_write_text

#: Sibling directory of ``sources/`` that holds provenance sidecars. Exported so
#: later steps (#64 matching, #65 merge) reference the name in exactly one place.
SIDECAR_DIR = "source-provenance"

#: File extension of a sidecar. A sidecar is a ``.json`` file directly inside a
#: ``source-provenance/`` directory.
SIDECAR_SUFFIX = ".json"

#: Serialization schema. Bump when the on-disk layout changes incompatibly.
SCHEMA_VERSION = 1

#: The three record fields every source carries; everything else is a
#: source-specific flat field stored in :attr:`SourceRecord.fields`.
_RESERVED_KEYS = ("type", "id", "imported_at")


def _is_bool(value: object) -> bool:
    # `isinstance(True, int)` is True, so an int check would admit booleans; the
    # mirror of that trap is what this must NOT do — admit `1`/`0` as booleans.
    # `isinstance(value, bool)` is false for `1`, which is the whole point.
    return isinstance(value, bool)


def _is_withdrawal_agent(value: object) -> bool:
    # `True in (WITHDRAWN_BY_AUTHOR, ...)` is False, so a bool cannot sneak through this
    # membership test.
    return value in (WITHDRAWN_BY_AUTHOR, WITHDRAWN_BY_ADMIN)


#: The value space of the source-specific fields that are read as **signals**, keyed
#: by ``(record type, field name)`` -> ``(predicate, what a valid value is)``. A ``None``
#: never reaches a predicate: it is the field's absence (:func:`signal_field_error`).
#:
#: The vocabulary is deliberately *not* this module's. ``withdrawn_by`` is arXiv's word
#: and ``is_retracted`` is OpenAlex's (#57 §6.3, #93 Q2); the two integrations own their
#: fields' meanings, and the names are repeated here only because the *read boundary* is
#: shared. The alternative — a registry each integration populates at import time — would
#: make a corrupt ledger's fate depend on which modules a process happened to import, so a
#: reader that never imported arXiv would read a bad ``withdrawn_by`` silently. That is the
#: failure this table exists to remove, so the table is always present. The two allowed
#: agents are imported from their owner rather than re-typed, so a third agent is added in
#: one place. Generalizing this into a per-integration schema (the shape ``BackfillSchema``
#: / ``AcknowledgeSchema`` take) is a real seam, but those objects are constructed by the
#: *caller* of a command and are not in reach of a low-level reader; building that seam is
#: not this issue's job.
_FIELD_VALUE_SPACE: dict[tuple[str, str], tuple[Callable[[object], bool], str]] = {
    # owner: factlog/integrations/openalex
    ("openalex", "is_retracted"): (_is_bool, "a boolean (true or false)"),
    # owner: factlog/integrations/arxiv
    ("arxiv", "withdrawn_by"): (
        _is_withdrawal_agent,
        f"{WITHDRAWN_BY_AUTHOR!r} or {WITHDRAWN_BY_ADMIN!r} (or absent)",
    ),
}


class ProvenanceError(ValueError):
    """A sidecar on disk is malformed and cannot be read as provenance."""


class ProvenanceConflict(ValueError):
    """A record with an existing ``(type, id)`` but different field values.

    Raised by :func:`add_source`. See its docstring for why divergence is loud
    rather than silently overwritten or dropped.
    """


def is_sidecar(path: Path | str) -> bool:
    """True when *path* is a provenance sidecar — a ``.json`` file directly
    inside a ``source-provenance/`` directory.

    Exported for #64/#65; nothing in this step consumes it. The check is on the
    path shape only (it does not touch the filesystem), so callers can use it to
    filter a listing without a stat.
    """
    p = Path(path)
    return p.suffix == SIDECAR_SUFFIX and SIDECAR_DIR in p.parts[:-1]


def sidecar_path(source_path: Path | str) -> Path:
    """Map a source file to its provenance sidecar. The one place that knows the
    naming rule.

    ``<kb>/sources/foo.md`` -> ``<kb>/source-provenance/foo.json``. The KB root
    is the parent of the source's directory (i.e. the parent of ``sources/``);
    the sidecar keeps the source's stem and takes a ``.json`` extension. A source
    with no ``.md`` extension, or a multi-dot stem, is handled the same way
    (only the final extension is replaced)::

        sources/foo.md              -> source-provenance/foo.json
        sources/foo.provenance.md   -> source-provenance/foo.provenance.json
        sources/report.pdf          -> source-provenance/report.json
    """
    src = Path(source_path)
    parts = src.parts
    # Anchor on the innermost `sources/` component rather than assuming the file
    # sits directly inside it. `src.parent.parent` would place the sidecar for
    # `sources/a/x.md` at `sources/source-provenance/x.json` — *inside* the very
    # directory the sidecar must stay out of — and would collide with
    # `sources/b/x.md`, silently merging two papers' ledgers into one file.
    for index in range(len(parts) - 2, -1, -1):
        if parts[index] == "sources":
            break
    else:
        raise ValueError(
            f"{src} is not under a 'sources/' directory; there is no KB root to "
            "anchor a provenance sidecar to."
        )
    kb_root = Path(*parts[:index]) if index else Path()
    # The path *below* sources/ is preserved, so nested sources cannot collide.
    relative = Path(*parts[index + 1:]).with_suffix(SIDECAR_SUFFIX)
    return kb_root / SIDECAR_DIR / relative


@dataclass(frozen=True)
class SourceRecord:
    """One database's contribution to a source's provenance.

    ``type`` is ``"openalex"`` | ``"arxiv"`` | ``"zotero"``; ``id`` is that
    database's identifier for the paper; ``imported_at`` is the caller-supplied
    batch timestamp. ``fields`` holds source-specific flat values merged to the
    record's top level on serialization — for arXiv: ``version``, ``submitted``,
    ``last_updated``, ``comment``, ``primary_category``. A ``None`` value in
    ``fields`` is dropped on write (deterministic absence), so callers may pass a
    record's optional fields through without pre-filtering.
    """

    type: str
    id: str
    imported_at: str
    fields: Mapping[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        """The idempotency key. Two databases may share an id string, so the key
        is ``(type, id)``, not ``id`` alone."""
        return (self.type, self.id)

    def to_dict(self) -> dict[str, Any]:
        """Flat mapping for serialization; ``None``-valued extras are dropped."""
        out: dict[str, Any] = {"type": self.type, "id": self.id, "imported_at": self.imported_at}
        for name, value in self.fields.items():
            if value is not None:
                out[name] = value
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SourceRecord:
        try:
            type_, id_, imported_at = data["type"], data["id"], data["imported_at"]
        except (KeyError, TypeError) as exc:
            raise ProvenanceError(f"record missing a required field: {data!r}") from exc
        # Types are enforced at the read boundary. A non-string `id` survives a
        # round-trip through json, then makes `_serialize`'s sort compare an int
        # against a str and die with a bare TypeError at *write* time — far from
        # the corrupt file that caused it.
        for name, value in (("type", type_), ("id", id_), ("imported_at", imported_at)):
            if not isinstance(value, str):
                raise ProvenanceError(
                    f"record field {name!r} must be a string, got "
                    f"{type(value).__name__}: {data!r}"
                )
        extras = {k: v for k, v in data.items() if k not in _RESERVED_KEYS}
        return cls(type=type_, id=id_, imported_at=imported_at, fields=extras)


@dataclass
class Provenance:
    """A source's full provenance: a schema version and its source records.

    The record order held in memory is not significant — :func:`write_provenance`
    sorts by ``(type, id)`` on write, so equality and byte-output are independent
    of insertion order.
    """

    schema_version: int = SCHEMA_VERSION
    records: list[SourceRecord] = field(default_factory=list)


def add_source(provenance: Provenance, record: SourceRecord) -> Provenance:
    """Add *record* to *provenance*, idempotent on ``(type, id)``. Mutates and
    returns *provenance*.

    Conflict semantics (chosen for an *audit* ledger):

    * A record whose ``(type, id)`` is not present is appended.
    * A record identical to one already present is a **no-op** — this keeps
      re-import idempotent (P3), so re-running an import leaves the file
      byte-unchanged.
    * A record with an existing ``(type, id)`` but **different** field values
      raises :class:`ProvenanceConflict`.

    Why raise rather than overwrite or keep-first: an audit ledger must not lie.
    *Overwriting* would silently discard the earlier record of where the source
    came from — the thing the ledger exists to preserve. *Keeping the first*
    would silently hide that the upstream metadata diverged, so an auditor could
    never tell the ledger is stale. Raising surfaces the divergence to the caller
    (the import/merge path in #65), which is the only place that can decide
    whether the new value supersedes the old or the two records are genuinely in
    conflict. Idempotent re-import stays quiet; a real change is never swallowed.
    """
    incoming = record.to_dict()
    for existing in provenance.records:
        if existing.key == record.key:
            if existing.to_dict() != incoming:
                raise ProvenanceConflict(
                    f"provenance already has a different {record.type} record "
                    f"for id {record.id!r}; refusing to overwrite an audit entry. "
                    "Use update_source() if the upstream record legitimately changed."
                )
            return provenance  # identical -> idempotent no-op
    provenance.records.append(record)
    return provenance


def update_source(provenance: Provenance, record: SourceRecord) -> Provenance:
    """Replace the record with *record*'s ``(type, id)``, or append it if absent.
    Mutates and returns *provenance*.

    The deliberate counterpart to :func:`add_source`. An arXiv paper's version,
    ``last_updated`` and ``comment`` change upstream over time, and recording
    that change is the entire purpose of ``arxiv-check-versions --auto-update``
    (#58). That command must not have to catch :class:`ProvenanceConflict` to do
    its normal job, and it must not be tempted to delete-then-add.

    The split is the point. :func:`add_source` is what an *import* calls: it has
    no authority to revise an existing entry, so divergence is an error. This is
    what a *refresh* calls: it has explicitly gone to the upstream API to learn
    the new value, so replacement is the correct outcome. Which function the
    caller reaches for records what kind of write it believes it is making.

    Replacing is safe for the audit story because the ledger records *where a
    source came from*, not a history of the tool's observations of it. A version
    bump does not invalidate the earlier import (#57 §6.1) — it means the source
    evolved, and the ledger should describe the source as it is now. Whether the
    KB entry should change remains a human decision (P1).
    """
    for index, existing in enumerate(provenance.records):
        if existing.key == record.key:
            provenance.records[index] = record
            return provenance
    provenance.records.append(record)
    return provenance


def _serialize(provenance: Provenance) -> str:
    records = sorted((r.to_dict() for r in provenance.records), key=lambda d: (d["type"], d["id"]))
    payload = {"schema_version": provenance.schema_version, "records": records}
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def signal_field_error(record: SourceRecord) -> str | None:
    """The reason *record* carries a signal field outside its integration's value space,
    or ``None`` when every field it carries is in range (see :data:`_FIELD_VALUE_SPACE`).

    A field the record does not carry is not judged: absence is a value (an absent
    ``is_retracted`` *means* not retracted). ``None`` **is** that absence — ``to_dict``
    drops it on write, and callers pass a record's optional fields through unfiltered — so
    a ``None`` is skipped rather than rejected, in either direction. Everything else is
    judged: ``"true"``, ``1`` and ``0`` are not booleans, and ``"maintainer"`` is not a
    withdrawal agent.

    Exported so a caller that *promotes* a value into the ledger from a looser medium can
    refuse in its own vocabulary before building a record. ``common/backfill.py`` copies
    arXiv's ``withdrawn_by`` out of front matter, where an unrecognised hand-typed value is
    kept verbatim on purpose (#98): that boundary is loud, not fatal, and it must not
    become a ledger the reader below then rejects.
    """
    for name, value in record.fields.items():
        rule = _FIELD_VALUE_SPACE.get((record.type, name))
        if rule is None or value is None:
            continue
        predicate, expected = rule
        if not predicate(value):
            return (
                f"record field {name!r} must be {expected}, got "
                f"{type(value).__name__} {value!r} (record {record.type} {record.id!r})"
            )
    return None


def _validate_signal_fields(record: SourceRecord, path: Path) -> None:
    """Raise :class:`ProvenanceError` for a record :func:`signal_field_error` rejects,
    naming the file. Guards both ends: nothing this module writes can be a ledger it
    would refuse to read back."""
    reason = signal_field_error(record)
    if reason is not None:
        raise ProvenanceError(
            f"sidecar {reason}: {path}. The value is not coerced — a ledger records what "
            "a source said, and inventing a value for a corrupt one is a lie. Repair it "
            "by hand."
        )


def read_provenance(path: Path | str) -> Provenance:
    """Read a sidecar. A missing file yields an empty :class:`Provenance` (not an
    error), so a caller can read-modify-write a source that has none yet. A file
    that exists but is not valid provenance JSON raises :class:`ProvenanceError`
    — a corrupt ledger is surfaced, never silently treated as empty (which would
    let the next write erase real provenance)."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Provenance()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProvenanceError(f"sidecar is not valid JSON: {p}") from exc
    if not isinstance(data, Mapping):
        raise ProvenanceError(f"sidecar is not a JSON object: {p}")

    # A file this module wrote always carries both keys. Their absence means the
    # file is not provenance, and reading it as an empty ledger would let the
    # next write erase whatever it really was.
    for required in ("schema_version", "records"):
        if required not in data:
            raise ProvenanceError(f"sidecar has no {required!r} key: {p}")

    schema_version = data["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise ProvenanceError(f"sidecar 'schema_version' is not an integer: {p}")
    # A newer factlog may lay records out incompatibly. Reading such a file as if
    # it were v1 would misparse it, and the next write would overwrite it with
    # the misparse. Refusing is the only safe move.
    if schema_version > SCHEMA_VERSION:
        raise ProvenanceError(
            f"sidecar schema_version {schema_version} is newer than this factlog "
            f"understands (max {SCHEMA_VERSION}): {p}"
        )

    raw_records = data["records"]
    if not isinstance(raw_records, list):
        raise ProvenanceError(f"sidecar 'records' is not a list: {p}")

    # The path is named in the error, so the signal fields are judged here rather than in
    # `from_dict` (which never sees the file it came from).
    records: list[SourceRecord] = []
    for raw in raw_records:
        record = SourceRecord.from_dict(raw)
        _validate_signal_fields(record, p)
        records.append(record)
    # `add_source` guarantees one record per (type, id). A file that violates it
    # was not written by this module, and a read-modify-write of it would pick an
    # arbitrary one of the duplicates to compare against.
    seen: set[tuple[str, str]] = set()
    for record in records:
        if record.key in seen:
            raise ProvenanceError(
                f"sidecar has two {record.type} records for id {record.id!r}: {p}"
            )
        seen.add(record.key)

    return Provenance(schema_version=schema_version, records=records)


def write_provenance(path: Path | str, provenance: Provenance) -> None:
    """Write *provenance* to *path* atomically (temp file + ``os.replace`` via
    ``_textio.atomic_write_text``), creating the ``source-provenance/`` directory
    if needed. Deterministic: two writes of the same data produce identical
    bytes.

    A record whose signal field is outside its integration's value space raises
    :class:`ProvenanceError` **before** anything is created — the symmetric half of the
    read boundary. Without it the stricter reader would only mean this module can write a
    ledger it then refuses to read, which is a worse failure than the one it fixes. Every
    writer already guards this call with ``(ProvenanceError, OSError)``, so the refusal is
    that record's per-id problem, never a batch crash."""
    p = Path(path)
    for record in provenance.records:
        _validate_signal_fields(record, p)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, _serialize(provenance))
