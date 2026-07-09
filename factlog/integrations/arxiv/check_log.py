# SPDX-License-Identifier: Apache-2.0
"""KB-level arXiv check-log: reader/writer for a single "when did we last look" file.

## What this is

``arxiv-check-versions`` (spec §11 Step 6) asks arXiv "is there a newer version of
this paper?" To answer ``--older-than`` without hammering the API it must remember,
per paper, *when it last checked* and *which version it saw then*. This module is the
arXiv-facing reader/writer for that memory: no command, no API call.

## Generalized, not duplicated (#83)

The read/write *boundary* — schema_version, atomic write, sort-on-write, duplicate-key
rejection, missing-is-empty, corrupt-is-error, and running each field's validator at
BOTH boundaries — lives once in :mod:`factlog.integrations.common.check_log`, so it
cannot drift from the OpenAlex check-log (#64's lesson: two guarded near-twins rot
apart). This module supplies only what is arXiv-specific: the id key ``arxiv_id``, the
filename ``arxiv.json``, and one extra field ``version`` with its validator. The public
names, error messages and on-disk bytes are unchanged — that is pinned by a golden
byte-identity test and the existing suites, which stay green.

## Where, and why not in ``sources/`` (decided in #58)

The obvious place — a ``last_checked_at`` field in the source's front matter — is
wrong. ``cli.py`` marks a logic report stale when any input's mtime exceeds the
report's, so writing a timestamp into an unchanged source *trips re-extraction of a
paper whose facts did not change*, and spends P4 (originals are byte-immutable) to
record a non-event. #58 settled that check-state lives in a single **KB-level
check-log** outside ``sources/``::

    <kb>/sources/foo.md          <- byte-immutable source (P4)
    <kb>/source-provenance/…     <- per-source provenance sidecars (#63)
    <kb>/check-log/arxiv.json    <- this file: one per KB, not per source

Placement was measured, not asserted — the mistake #58 made and #63 caught was
claiming invisibility to the source enumerators without running them. See the common
module for the four facts (all verified against the code) that justify the directory.

## Keyed by ``arxiv_id`` alone — per paper, not per source

"When did the tool last check arXiv for updates to paper X" is a property of the
*arXiv paper*, not of any source file. Two source files can reference the same paper;
the tool checks arXiv once per paper and records one timestamp. So the key is the
normalized ``arxiv_id``.

## Records exactly two things

``last_checked_at`` and the ``version`` observed at that time — the two values
``--older-than`` and change-detection need, and nothing more. ``version`` is an
**int**, matching ``ParsedArxivWork.version`` and the ``version`` field of an arXiv
provenance record. A string here would be a silent trap for the one consumer this log
exists for: ``arxiv-check-versions`` (#78) compares the logged version with the one
the API returns, and ``7 != "7"`` would report every paper as changed. arXiv numbers
versions from v1, so a value below 1 is corruption. The guard runs at **both**
boundaries (on write in :func:`record_check`, on read in the common boundary via the
:class:`~factlog.integrations.common.check_log.FieldSpec` below), preserving #77.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from factlog.integrations.common import check_log as _base
from factlog.integrations.common.check_log import (
    CHECK_LOG_DIR,
    SCHEMA_VERSION,
    CheckLogError,
)

__all__ = [
    "CHECK_LOG_DIR",
    "CHECK_LOG_NAME",
    "SCHEMA_VERSION",
    "CheckLog",
    "CheckLogError",
    "CheckRecord",
    "check_log_path",
    "read_check_log",
    "record_check",
    "write_check_log",
]

#: The arXiv check-log's filename inside :data:`CHECK_LOG_DIR`.
CHECK_LOG_NAME = "arxiv.json"


def _validate_version(value: object, where: str = "") -> int:
    """An arXiv version: an ``int`` >= 1. Enforced at *both* boundaries.

    ``provenance.py`` guarded its reader and left its writer open, and a value that
    only a read rejects is a value a write can still put on disk — one bad call bricks
    the file for every later read. So :func:`record_check` validates too, and rejects
    at the point of the mistake rather than at the next process's read.

    ``bool`` is an ``int`` subclass, so ``True`` would otherwise read as v1.

    This validator stays arXiv-owned: it is the arXiv schema descriptor's field
    validator, which the generic boundary runs on both write and read.
    """
    suffix = f": {where}" if where else ""
    if not isinstance(value, int) or isinstance(value, bool):
        raise CheckLogError(
            f"check-log version must be an integer, got {type(value).__name__}{suffix}"
        )
    if value < 1:
        raise CheckLogError(
            f"check-log version must be >= 1 (arXiv numbers versions from v1), "
            f"got {value}{suffix}"
        )
    return value


#: The arXiv schema descriptor handed to the generic boundary: the id key, the
#: filename, and the one extra field ``version`` with its arXiv-owned validator.
_SCHEMA = _base.CheckLogSchema(
    name=CHECK_LOG_NAME,
    id_key="arxiv_id",
    fields=(_base.FieldSpec("version", _validate_version),),
)


@dataclass(frozen=True)
class CheckRecord:
    """One paper's last check: the timestamp the tool looked, and the version it
    saw then.

    ``version`` is an **int**, matching ``ParsedArxivWork.version`` and the ``version``
    field of an arXiv provenance record — see the module docstring for why a string
    would silently mis-report every paper as changed.
    """

    last_checked_at: str
    version: int


@dataclass
class CheckLog:
    """The whole check-log: a schema version and ``arxiv_id -> CheckRecord``.

    ``entries`` is a mapping so a lookup by ``arxiv_id`` is O(1); its iteration order
    is not significant, because :func:`write_check_log` sorts on write, so equality and
    byte-output are independent of insertion order.
    """

    schema_version: int = SCHEMA_VERSION
    entries: dict[str, CheckRecord] = field(default_factory=dict)


def check_log_path(kb_root: Path | str) -> Path:
    """Map a KB root to its arXiv check-log path: ``<kb>/check-log/arxiv.json``."""
    return _base.check_log_path(kb_root, _SCHEMA)


def record_check(
    log: CheckLog, arxiv_id: str, last_checked_at: str, version: int
) -> CheckLog:
    """Record that *arxiv_id* was checked at *last_checked_at*, seeing *version*.
    Mutates and returns *log*.

    An **upsert** — the check-log records *the latest observation*, so re-checking a
    paper replaces its previous timestamp and version. ``version`` is validated here
    (the write boundary) so one bad call can never brick the file; the generic reader
    validates it again on the way back in.
    """
    log.entries[arxiv_id] = CheckRecord(
        last_checked_at=last_checked_at, version=_validate_version(version)
    )
    return log


def read_check_log(path: Path | str) -> CheckLog:
    """Read the arXiv check-log at *path*. A missing file yields an empty
    :class:`CheckLog`; a malformed one raises :class:`CheckLogError` (never read as
    empty). Validation of the on-disk shape and of ``version`` is the generic
    boundary's, run against the arXiv schema."""
    base = _base.read_check_log(path, _SCHEMA)
    entries = {
        arxiv_id: CheckRecord(
            last_checked_at=record[_base.LAST_CHECKED_AT], version=record["version"]
        )
        for arxiv_id, record in base.entries.items()
    }
    return CheckLog(schema_version=base.schema_version, entries=entries)


def write_check_log(path: Path | str, log: CheckLog) -> None:
    """Write *log* to *path* atomically via the generic boundary. Deterministic: two
    writes of the same data produce identical bytes."""
    base = _base.CheckLog(
        schema_version=log.schema_version,
        entries={
            arxiv_id: {_base.LAST_CHECKED_AT: rec.last_checked_at, "version": rec.version}
            for arxiv_id, rec in log.entries.items()
        },
    )
    _base.write_check_log(path, base, _SCHEMA)
