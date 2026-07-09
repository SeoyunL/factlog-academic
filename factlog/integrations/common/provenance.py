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
enumeration: every walker in the repo is rooted at
``common.SOURCE_ROOTS = ("sources", "runs/sources")``, so a file under
``source-provenance/`` cannot be picked up as a source by ``factlog sources``,
``factlog status`` (coverage), ``factlog export`` or any future enumerator —
structurally, not by a promise each call site must keep. It is deliberately not
named ``provenance/``: ``factlog provenance <TERM>`` already answers "which
source did this fact come from", and a directory of source-record ledgers under
the same word would collide.

``sidecar_path`` owns the naming rule; it is the only place that knows a sidecar
lives under ``source-provenance/``.

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
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    return p.suffix == SIDECAR_SUFFIX and p.parent.name == SIDECAR_DIR


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
    kb_root = src.parent.parent  # parent of sources/ == KB root
    return kb_root / SIDECAR_DIR / (src.stem + SIDECAR_SUFFIX)


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
                    f"for id {record.id!r}; refusing to overwrite an audit entry"
                )
            return provenance  # identical -> idempotent no-op
    provenance.records.append(record)
    return provenance


def _serialize(provenance: Provenance) -> str:
    records = sorted((r.to_dict() for r in provenance.records), key=lambda d: (d["type"], d["id"]))
    payload = {"schema_version": provenance.schema_version, "records": records}
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


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
    raw_records = data.get("records", [])
    if not isinstance(raw_records, list):
        raise ProvenanceError(f"sidecar 'records' is not a list: {p}")
    schema_version = data.get("schema_version", SCHEMA_VERSION)
    return Provenance(
        schema_version=schema_version,
        records=[SourceRecord.from_dict(r) for r in raw_records],
    )


def write_provenance(path: Path | str, provenance: Provenance) -> None:
    """Write *provenance* to *path* atomically (temp file + ``os.replace`` via
    ``_textio.atomic_write_text``), creating the ``source-provenance/`` directory
    if needed. Deterministic: two writes of the same data produce identical
    bytes."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, _serialize(provenance))
