# SPDX-License-Identifier: Apache-2.0
"""Generic KB-level check-log: the read/write boundary shared by every integration.

## What this is, and why it is not per-integration

A check-log answers "when did the tool last look upstream for this paper, and what
did it see then?" ``arxiv-check-versions`` (#58/#78) needed it first; ``openalex-refresh``
(#83) needs the same memory. The obvious move — copy the arXiv module and adjust it
— is exactly the failure #64 named: two guarded near-twins that drift, so a boundary
hardened on one side (#77's both-boundary ``version`` guard) silently rots on the
other.

So the *boundary* lives here, once, and each integration supplies a small
:class:`CheckLogSchema` describing what is integration-specific. What the boundary
owns, identically for every caller:

* a ``schema_version`` written from day one;
* atomic write via ``_textio.atomic_write_text`` (temp file + ``os.replace``);
* deterministic bytes — records sorted by the id key on write, ``sort_keys=True`` +
  ``indent=2`` + a trailing newline, so two writes of the same data are byte-identical
  and the output is human-diffable;
* an in-memory ``dict[id -> record]`` for O(1) lookup;
* a missing file reads as an *empty* log (so a first run can write one) while a file
  that exists but is not a well-shaped check-log raises :class:`CheckLogError` rather
  than reading as empty — an empty read would let the next write erase a real log
  (the failure ``provenance.py`` shipped without guarding, #58/#63);
* every corrupt shape that still parses as JSON is refused at the read boundary: a
  non-object top level, a missing/future ``schema_version``, a missing ``entries``,
  an ``entries`` that is not a list, an entry that is not an object, a missing or
  wrong-typed id/``last_checked_at``, an unexpected extra key, two records for one id,
  and — via an ``object_pairs_hook`` — a duplicated key *inside* any object.

## What a :class:`CheckLogSchema` supplies

The id-key name (``arxiv_id`` / ``openalex_id``), the filename inside ``check-log/``,
and the set of extra fields beyond ``last_checked_at`` together with each field's
validator. arXiv carries one extra field, ``version``, whose validator (an ``int``
>= 1 that rejects ``bool``) stays arXiv-owned; the boundary runs it at **both** write
(:func:`record_check`) and read (:func:`read_check_log`), preserving #77's guarantee
at one implementation and two call sites. OpenAlex carries no extra field: its record
is just ``{openalex_id, last_checked_at}``, because ``--older-than`` needs only the
timestamp.

## Placement (unchanged, #58/#63)

The check-log is a file inside a ``check-log/`` sibling directory of ``sources/``,
one file per KB per integration (``check-log/arxiv.json``, ``check-log/openalex.json``),
keyed by the paper's id, never per source. It sits *outside* ``common.SOURCE_ROOTS``
and outside ``source-provenance/``, so it is invisible to source enumeration — a
property that is *measured* against the real CLI, never merely asserted (the mistake
#58 made and #63 caught).

The on-disk record is a **list of records**, each ``{<id_key>, last_checked_at, ...}``,
sorted by the id key on write — the same list-of-records posture as
``common/provenance.py``, and chosen for the same reason: a JSON *object* keyed by
the id would force the id to be a string that could never be *constructed* corrupt to
be caught, whereas the list form makes the id a value the read boundary validates.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factlog.integrations.common._textio import atomic_write_text

#: Sibling directory of ``sources/`` that holds the KB-level check-log(s). One level
#: so each integration's log (``check-log/<name>.json``) has a home without renaming.
CHECK_LOG_DIR = "check-log"

#: Serialization schema. Bump when the on-disk layout changes incompatibly.
SCHEMA_VERSION = 1

#: The key every record carries besides its id. Validated as a string at the boundary.
LAST_CHECKED_AT = "last_checked_at"


class CheckLogError(ValueError):
    """A check-log on disk is malformed and cannot be read as a check-log.

    Raised rather than reading the file as empty: an empty read would let the next
    write erase a real log (the failure #58/#63 warned about, and the one
    ``provenance.py`` shipped without guarding).
    """


@dataclass(frozen=True)
class FieldSpec:
    """One extra record field beyond ``last_checked_at`` and its validator.

    ``validate`` is called with ``(value, where)`` at both the write and the read
    boundary and must return the validated value or raise :class:`CheckLogError`.
    ``where`` is a context suffix (the offending record and file) for the message.
    Owning the validator here — in the integration's schema, not in this module — is
    what keeps a guard like arXiv's ``version: int >= 1`` integration-owned while the
    generic boundary still enforces it on both sides.
    """

    name: str
    validate: Callable[[object, str], Any]


@dataclass(frozen=True)
class CheckLogSchema:
    """What is integration-specific about a check-log: the id key, the filename, and
    the extra fields.

    ``fields`` is empty for OpenAlex (record = ``{openalex_id, last_checked_at}``) and
    holds one :class:`FieldSpec` for arXiv (``version``). The id key is the front-matter
    identity of the paper (``arxiv_id`` / ``openalex_id``).
    """

    name: str
    id_key: str
    fields: tuple[FieldSpec, ...] = ()

    @property
    def entry_keys(self) -> frozenset[str]:
        """The exact keys an on-disk entry object must carry — nothing more, nothing
        less. Anything else is a corrupt shape (or an unbumped schema change)."""
        return frozenset({self.id_key, LAST_CHECKED_AT, *(f.name for f in self.fields)})


@dataclass
class CheckLog:
    """The whole check-log: a schema version and ``id -> record``.

    A record is a flat mapping of ``last_checked_at`` plus the schema's extra fields
    (the id key is the mapping's key, not stored in the record). ``entries`` is a
    mapping so a lookup by id is O(1); its iteration order is not significant, because
    :func:`write_check_log` sorts on write, so equality and byte-output are independent
    of insertion order.
    """

    schema_version: int = SCHEMA_VERSION
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)


def check_log_path(kb_root: Path | str, schema: CheckLogSchema) -> Path:
    """Map a KB root to its check-log path: ``<kb>/check-log/<schema.name>``.

    The one place that knows the naming rule. It takes the KB root directly (not a
    source path) because the check-log is per-KB, not per-source.
    """
    return Path(kb_root) / CHECK_LOG_DIR / schema.name


def record_check(
    log: CheckLog,
    entry_id: str,
    last_checked_at: str,
    extra: Mapping[str, Any],
    schema: CheckLogSchema,
) -> CheckLog:
    """Upsert: record that *entry_id* was checked at *last_checked_at* with *extra*.
    Mutates and returns *log*.

    Deliberately unlike ``provenance.add_source`` (an audit ledger that refuses to
    overwrite): a check-log records *the latest observation*, so re-checking a paper
    replaces its previous record — the whole point of ``--older-than``. Recording the
    identical values again is a byte-level no-op.

    The schema's field validators run **here**, on write, so a value only a read would
    reject can never be put on disk in the first place (#77: ``provenance.py`` guarded
    its reader and left its writer open, and one bad call bricked the file for every
    later read). ``extra`` must carry exactly the schema's extra fields.
    """
    expected = {f.name for f in schema.fields}
    provided = set(extra)
    if provided != expected:
        raise CheckLogError(
            f"check-log record for {entry_id!r} must carry exactly {sorted(expected)}, "
            f"got {sorted(provided)}"
        )
    record: dict[str, Any] = {LAST_CHECKED_AT: last_checked_at}
    for spec in schema.fields:
        record[spec.name] = spec.validate(extra[spec.name], f"record for {entry_id!r}")
    log.entries[entry_id] = record
    return log


def _serialize(log: CheckLog, schema: CheckLogSchema) -> str:
    entries = [
        {schema.id_key: entry_id, **record} for entry_id, record in log.entries.items()
    ]
    entries.sort(key=lambda e: e[schema.id_key])
    payload = {"schema_version": log.schema_version, "entries": entries}
    # sort_keys orders each record's keys; the explicit sort above orders the records
    # — together the bytes are a pure function of the data, independent of insertion
    # order.
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` that refuses a repeated key in any JSON object.

    ``json.loads`` silently keeps the last value for a duplicate key, so a record
    written with the id key twice would parse with one id and the other silently gone.
    Refuse it. (Two *records* for one paper are caught separately while building the
    entry map.)
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CheckLogError(f"check-log has a duplicate key {key!r}")
        result[key] = value
    return result


def read_check_log(path: Path | str, schema: CheckLogSchema) -> CheckLog:
    """Read the check-log at *path* against *schema*. A missing file yields an empty
    :class:`CheckLog` (so a first run can write one); a file that exists but is not a
    well-shaped check-log raises :class:`CheckLogError` rather than reading as empty
    (which would let the next write erase a real log)."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CheckLog()

    try:
        # The hook raises CheckLogError on a duplicate key; that propagates unwrapped,
        # which is what we want — only a JSON *syntax* error is remapped.
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise CheckLogError(f"check-log is not valid JSON: {p}") from exc

    if not isinstance(data, Mapping):
        raise CheckLogError(f"check-log is not a JSON object: {p}")

    # A file this module wrote always carries both keys. Their absence means it is not
    # a check-log, and reading it as empty would erase whatever it really was.
    for required in ("schema_version", "entries"):
        if required not in data:
            raise CheckLogError(f"check-log has no {required!r} key: {p}")

    schema_version = data["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise CheckLogError(f"check-log 'schema_version' is not an integer: {p}")
    # A newer factlog may lay entries out incompatibly; reading it as v1 would misparse
    # it and the next write would persist the misparse. Refusing is safe.
    if schema_version > SCHEMA_VERSION:
        raise CheckLogError(
            f"check-log schema_version {schema_version} is newer than this factlog "
            f"understands (max {SCHEMA_VERSION}): {p}"
        )

    raw_entries = data["entries"]
    if not isinstance(raw_entries, list):
        raise CheckLogError(f"check-log 'entries' is not a list: {p}")

    entry_keys = schema.entry_keys
    entries: dict[str, dict[str, Any]] = {}
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            raise CheckLogError(f"check-log entry is not a JSON object: {raw!r} in {p}")
        missing = entry_keys - set(raw)
        if missing:
            raise CheckLogError(
                f"check-log entry is missing {sorted(missing)}: {raw!r} in {p}"
            )
        extra = set(raw) - entry_keys
        if extra:
            raise CheckLogError(
                f"check-log entry has unexpected key(s) {sorted(extra)}: {raw!r} in {p}"
            )
        # A wrong-typed value survives json and later breaks a comparison or the sort
        # far from the corrupt file that caused it; reject it at the boundary.
        for name in (schema.id_key, LAST_CHECKED_AT):
            if not isinstance(raw[name], str):
                raise CheckLogError(
                    f"check-log entry field {name!r} must be a string, got "
                    f"{type(raw[name]).__name__}: {raw!r} in {p}"
                )
        record: dict[str, Any] = {LAST_CHECKED_AT: raw[LAST_CHECKED_AT]}
        for spec in schema.fields:
            record[spec.name] = spec.validate(raw[spec.name], f"{raw!r} in {p}")
        entry_id = raw[schema.id_key]
        # One record per paper. A file with two entries for one id was not written by
        # this module, and folding it into the dict would drop one silently.
        if entry_id in entries:
            raise CheckLogError(
                f"check-log has two records for {schema.id_key} {entry_id!r}: {p}"
            )
        entries[entry_id] = record

    return CheckLog(schema_version=schema_version, entries=entries)


def write_check_log(path: Path | str, log: CheckLog, schema: CheckLogSchema) -> None:
    """Write *log* to *path* atomically (temp file + ``os.replace`` via
    ``_textio.atomic_write_text``), creating ``check-log/`` if needed. Deterministic:
    two writes of the same data produce identical bytes."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, _serialize(log, schema))
