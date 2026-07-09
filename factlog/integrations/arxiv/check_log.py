# SPDX-License-Identifier: Apache-2.0
"""KB-level arXiv check-log: reader/writer for a single "when did we last look" file.

## What this is

``arxiv-check-versions`` (spec §11 Step 6, not built here) asks arXiv "is there a
newer version of this paper?" To answer ``--older-than`` without hammering the
API it must remember, per paper, *when it last checked* and *which version it saw
then*. This module is the reader/writer for that memory and nothing else: no
command, no API call, no ``arxiv-check-versions``.

## Where, and why not in ``sources/`` (decided in #58)

The obvious place — a ``last_checked_at`` field in the source's front matter — is
wrong. ``cli.py:1666-1669`` marks a logic report stale when any input's mtime
exceeds the report's, so writing a timestamp into an unchanged source *trips
re-extraction of a paper whose facts did not change*, and spends P4 (originals are
byte-immutable) to record a non-event. #58 settled that check-state lives in a
single **KB-level check-log** outside ``sources/`` instead.

Placement was then measured, not asserted — the mistake #58 made and #63 caught
was claiming invisibility to the source enumerators without running them. The
check-log is a file inside its own sibling directory of ``sources/``::

    <kb>/sources/foo.md          <- byte-immutable source (P4)
    <kb>/source-provenance/…     <- per-source provenance sidecars (#63)
    <kb>/check-log/arxiv.json    <- this file: one per KB, not per source

Four facts justify the directory, all verified against the code:

1. **Outside ``SOURCE_ROOTS``.** ``common.source_files()`` (``common.py:368``, four
   callers incl. ``factlog status``/coverage) walks ``("sources", "runs/sources")``
   with ``rglob("*")``; ``cli.py:745`` (``factlog sources``) and ``cli.py:2422``
   hardcode ``sources``/``runs/sources``. None descends into a ``check-log/`` sibling.
   The accompanying test proves ``factlog sources``/``status``/``export`` are
   byte-identical with and without the log, and that adding ``"check-log"`` to
   ``SOURCE_ROOTS`` *does* surface it — so the invisibility is structural, not a
   promise each call site keeps.
2. **Not in ``source-provenance/``.** A ``.json`` there is classified as a
   per-source provenance sidecar by ``provenance.is_sidecar`` (``provenance.py:104``);
   #64/#65 will enumerate sidecars by that predicate. A KB-level check-log dropped
   beside them would be mis-read as one source's provenance ledger and fail to
   parse. The two are different in kind: the sidecar is one file *per source* keyed
   by ``(type, id)``; the check-log is one file *per KB* keyed by ``arxiv_id``.
3. **Not at the KB root loose.** A dedicated directory keeps the root uncluttered
   and gives the guard test a concrete "the log's directory" to add to
   ``SOURCE_ROOTS``.
4. **Its own directory, not ``provenance/``**, mirroring #63's care to avoid
   colliding with the ``factlog provenance <TERM>`` command name.

## Keyed by ``arxiv_id`` alone — per paper, not per source

"When did the tool last check arXiv for updates to paper X" is a property of the
*arXiv paper*, not of any source file. Two source files can reference the same
paper (an arXiv-primary ``.md`` and an OpenAlex-primary one that also cites the
preprint); the tool checks arXiv once per paper and records one timestamp.
``--older-than`` asks "which papers have not been checked since T" — a per-paper
question. Keying per source file would re-check the same paper once per file and
store redundant, divergeable timestamps. So the key is the normalized ``arxiv_id``.

## Records exactly two things

``last_checked_at`` and the ``version`` observed at that time — the two values
``--older-than`` and change-detection need, and nothing more. There is no
``updated_at``, no source path, no run id: those would be housekeeping, not the
answer to "when did we last look, and at what."

## On-disk shape and the read boundary

Entries are a **list of records**, each ``{arxiv_id, last_checked_at, version}``,
sorted by ``arxiv_id`` on write — the same list-of-records posture as
``common/provenance.py``. A JSON *object* keyed by ``arxiv_id`` was rejected for
one concrete reason: JSON object keys are always strings, so a corrupt non-string
``arxiv_id`` could never be *constructed* to be caught, whereas the list form makes
the id a value the read boundary validates like provenance does. In memory the log
is still a ``dict[arxiv_id -> CheckRecord]`` for O(1) lookup.

Mirrors ``common/provenance.py``, the proven posture: stdlib ``json`` (zero new
dependency; ``tomllib`` has no ``dumps`` and PyYAML would become a core dep),
``sort_keys=True`` + ``indent=2`` + trailing newline for byte-deterministic,
human-diffable output, atomic write via ``_textio.atomic_write_text``, a
``schema_version`` from day one, ``last_checked_at`` supplied by the caller and
**never** read from a clock inside this module.

``read_check_log`` validates at the boundary rather than trusting JSON to be
well-shaped. ``provenance.py`` shipped without this and review found corrupt shapes
that parse as valid JSON yet are not the format — reading them as an empty log
would let the next write erase the real log. Every such shape raises
:class:`CheckLogError`: a non-object top level (e.g. an array), a missing/future
``schema_version``, a missing ``entries``, an ``entries`` that is not a list, an
entry that is not an object, a missing or non-string
``arxiv_id``/``last_checked_at``/``version``, an unexpected extra key, two records
for one ``arxiv_id``, and — caught with an ``object_pairs_hook`` because
``json.loads`` would otherwise silently keep the last — a duplicated key *inside*
any object. A missing file alone reads as an empty log, so a first run can write one.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factlog.integrations.common._textio import atomic_write_text

#: Sibling directory of ``sources/`` that holds the KB-level check-log(s). One
#: level so a future per-database check-log (``check-log/<db>.json``) has a home
#: without renaming anything.
CHECK_LOG_DIR = "check-log"

#: The arXiv check-log's filename inside :data:`CHECK_LOG_DIR`.
CHECK_LOG_NAME = "arxiv.json"

#: Serialization schema. Bump when the on-disk layout changes incompatibly.
SCHEMA_VERSION = 1

#: The exact keys an on-disk entry object must carry. Anything more is a corrupt
#: shape (or an unbumped schema change) and is refused at the read boundary.
_ENTRY_KEYS = frozenset({"arxiv_id", "last_checked_at", "version"})


class CheckLogError(ValueError):
    """A check-log on disk is malformed and cannot be read as a check-log.

    Raised rather than reading the file as empty: an empty read would let the next
    write erase a real log (the failure #58/#63 warned about, and the one
    ``provenance.py`` shipped without guarding).
    """


def check_log_path(kb_root: Path | str) -> Path:
    """Map a KB root to its arXiv check-log path: ``<kb>/check-log/arxiv.json``.

    The one place that knows the naming rule. It takes the KB root directly (not a
    source path) because the check-log is per-KB, not per-source — there is no
    source to anchor on, and no subtree to preserve.
    """
    return Path(kb_root) / CHECK_LOG_DIR / CHECK_LOG_NAME


@dataclass(frozen=True)
class CheckRecord:
    """One paper's last check: the timestamp the tool looked, and the version it
    saw then.

    ``version`` is an **int**, matching ``ParsedArxivWork.version`` and the
    ``version`` field of an arXiv provenance record. A string here would be a
    silent trap for the one consumer this log exists for: ``arxiv-check-versions``
    (#78) compares the logged version against the one the API returns, and
    ``7 != "7"`` would report every paper as changed. arXiv numbers versions from
    v1, so a value below 1 is corruption.
    """

    last_checked_at: str
    version: int


@dataclass
class CheckLog:
    """The whole check-log: a schema version and ``arxiv_id -> CheckRecord``.

    ``entries`` is a mapping so a lookup by ``arxiv_id`` is O(1); its iteration
    order is not significant, because :func:`write_check_log` sorts on write, so
    equality and byte-output are independent of insertion order.
    """

    schema_version: int = SCHEMA_VERSION
    entries: dict[str, CheckRecord] = field(default_factory=dict)


def record_check(
    log: CheckLog, arxiv_id: str, last_checked_at: str, version: str
) -> CheckLog:
    """Record that *arxiv_id* was checked at *last_checked_at*, seeing *version*.
    Mutates and returns *log*.

    This is an **upsert**, deliberately unlike ``provenance.add_source`` (which is
    an audit ledger and refuses to overwrite a diverging entry). The check-log
    records *the latest observation*, so re-checking a paper must replace its
    previous timestamp and version — that is the whole point of ``--older-than``,
    which reads the most recent check. Recording the identical values again is a
    byte-level no-op; recording a newer check overwrites. The module does not judge
    whether *last_checked_at* moves forward or *version* changed: the caller
    (``arxiv-check-versions``, #58) owns those decisions and supplies the values.
    """
    log.entries[arxiv_id] = CheckRecord(last_checked_at=last_checked_at, version=version)
    return log


def _serialize(log: CheckLog) -> str:
    entries = [
        {
            "arxiv_id": arxiv_id,
            "last_checked_at": rec.last_checked_at,
            "version": rec.version,
        }
        for arxiv_id, rec in log.entries.items()
    ]
    entries.sort(key=lambda e: e["arxiv_id"])
    payload = {"schema_version": log.schema_version, "entries": entries}
    # sort_keys orders each record's keys; the explicit sort above orders the
    # records — together the bytes are a pure function of the data, independent of
    # insertion order.
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` that refuses a repeated key in any JSON object.

    ``json.loads`` silently keeps the last value for a duplicate key, so a record
    written as ``{"arxiv_id": "a", "arxiv_id": "b", …}`` would parse with one id and
    the other silently gone. Refuse it. (Two *records* for one paper — the more
    obvious "duplicate ids" — are caught separately while building the entry map.)
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CheckLogError(f"check-log has a duplicate key {key!r}")
        result[key] = value
    return result


def read_check_log(path: Path | str) -> CheckLog:
    """Read the check-log at *path*. A missing file yields an empty
    :class:`CheckLog` (so a first run can write one); a file that exists but is not
    a well-shaped check-log raises :class:`CheckLogError` rather than reading as
    empty (which would let the next write erase a real log)."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CheckLog()

    try:
        # The hook raises CheckLogError on a duplicate key; that propagates
        # unwrapped, which is what we want — only a JSON *syntax* error is remapped.
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise CheckLogError(f"check-log is not valid JSON: {p}") from exc

    if not isinstance(data, Mapping):
        raise CheckLogError(f"check-log is not a JSON object: {p}")

    # A file this module wrote always carries both keys. Their absence means it is
    # not a check-log, and reading it as empty would erase whatever it really was.
    for required in ("schema_version", "entries"):
        if required not in data:
            raise CheckLogError(f"check-log has no {required!r} key: {p}")

    schema_version = data["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise CheckLogError(f"check-log 'schema_version' is not an integer: {p}")
    # A newer factlog may lay entries out incompatibly; reading it as v1 would
    # misparse it and the next write would persist the misparse. Refusing is safe.
    if schema_version > SCHEMA_VERSION:
        raise CheckLogError(
            f"check-log schema_version {schema_version} is newer than this factlog "
            f"understands (max {SCHEMA_VERSION}): {p}"
        )

    raw_entries = data["entries"]
    if not isinstance(raw_entries, list):
        raise CheckLogError(f"check-log 'entries' is not a list: {p}")

    entries: dict[str, CheckRecord] = {}
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            raise CheckLogError(f"check-log entry is not a JSON object: {raw!r} in {p}")
        missing = _ENTRY_KEYS - set(raw)
        if missing:
            raise CheckLogError(
                f"check-log entry is missing {sorted(missing)}: {raw!r} in {p}"
            )
        extra = set(raw) - _ENTRY_KEYS
        if extra:
            raise CheckLogError(
                f"check-log entry has unexpected key(s) {sorted(extra)}: {raw!r} in {p}"
            )
        # A wrong-typed value survives json and later breaks a comparison or the
        # sort far from the corrupt file that caused it; reject it at the boundary.
        for name in ("arxiv_id", "last_checked_at"):
            if not isinstance(raw[name], str):
                raise CheckLogError(
                    f"check-log entry field {name!r} must be a string, got "
                    f"{type(raw[name]).__name__}: {raw!r} in {p}"
                )
        # bool is an int subclass; `true` must not read as version 1.
        version = raw["version"]
        if not isinstance(version, int) or isinstance(version, bool):
            raise CheckLogError(
                f"check-log entry field 'version' must be an integer, got "
                f"{type(version).__name__}: {raw!r} in {p}"
            )
        if version < 1:
            raise CheckLogError(
                f"check-log entry field 'version' must be >= 1 (arXiv numbers "
                f"versions from v1), got {version}: {raw!r} in {p}"
            )
        arxiv_id = raw["arxiv_id"]
        # One record per paper. A file with two entries for one id was not written
        # by this module, and folding it into the dict would drop one silently.
        if arxiv_id in entries:
            raise CheckLogError(f"check-log has two records for arxiv_id {arxiv_id!r}: {p}")
        entries[arxiv_id] = CheckRecord(
            last_checked_at=raw["last_checked_at"], version=raw["version"]
        )

    return CheckLog(schema_version=schema_version, entries=entries)


def write_check_log(path: Path | str, log: CheckLog) -> None:
    """Write *log* to *path* atomically (temp file + ``os.replace`` via
    ``_textio.atomic_write_text``), creating ``check-log/`` if needed.
    Deterministic: two writes of the same data produce identical bytes."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, _serialize(log))
