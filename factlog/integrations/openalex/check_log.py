# SPDX-License-Identifier: Apache-2.0
"""KB-level OpenAlex check-log: "when did we last refresh this work" memory (#83).

``openalex-refresh`` re-fetches every OpenAlex record a KB holds and reports (and,
under ``--auto-update``, records) how the upstream metadata has drifted. To answer
``--older-than`` it must remember, per work, *when it last looked*. This module is the
OpenAlex-facing reader/writer for that memory.

## Generalized, not duplicated (#83)

The read/write boundary lives once in
:mod:`factlog.integrations.common.check_log` — schema_version, atomic write,
sort-on-write, duplicate-key rejection, missing-is-empty, corrupt-is-error. This
module supplies only the OpenAlex schema: the id key ``openalex_id`` and the filename
``openalex.json``. There are **no extra fields**: the record is exactly
``{openalex_id, last_checked_at}``.

Unlike arXiv's log there is no ``version``. arXiv logs the last-seen version so a
*skipped* paper can still display it; ``--older-than`` never needed it, and OpenAlex
has no version to remember — a refresh compares the live ledger fields directly, so the
timestamp alone is all the log must hold.

## Placement and invisibility (#58/#63)

The file is ``<kb>/check-log/openalex.json`` — the same already-proven-invisible
``check-log/`` sibling of ``sources/`` that holds ``arxiv.json``. It is outside
``common.SOURCE_ROOTS`` and outside ``source-provenance/``, so ``factlog sources`` /
``status`` / ``export`` never see it. That invisibility is *measured* against the real
CLI (the mistake #58 made and #63 caught was asserting it), and a guard test fails if
``check-log`` is ever added to ``SOURCE_ROOTS``.
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

#: The OpenAlex check-log's filename inside :data:`CHECK_LOG_DIR`.
CHECK_LOG_NAME = "openalex.json"

#: The OpenAlex schema descriptor handed to the generic boundary: the id key and the
#: filename, and no extra fields — the record is just ``{openalex_id, last_checked_at}``.
_SCHEMA = _base.CheckLogSchema(name=CHECK_LOG_NAME, id_key="openalex_id", fields=())


@dataclass(frozen=True)
class CheckRecord:
    """One work's last refresh: only the timestamp the tool looked. OpenAlex has no
    version to remember, so — unlike arXiv's record — there is nothing else here."""

    last_checked_at: str


@dataclass
class CheckLog:
    """The whole check-log: a schema version and ``openalex_id -> CheckRecord``.

    ``entries`` is a mapping so a lookup by ``openalex_id`` is O(1); its iteration
    order is not significant, because :func:`write_check_log` sorts on write.
    """

    schema_version: int = SCHEMA_VERSION
    entries: dict[str, CheckRecord] = field(default_factory=dict)


def check_log_path(kb_root: Path | str) -> Path:
    """Map a KB root to its OpenAlex check-log path: ``<kb>/check-log/openalex.json``."""
    return _base.check_log_path(kb_root, _SCHEMA)


def record_check(log: CheckLog, openalex_id: str, last_checked_at: str) -> CheckLog:
    """Record that *openalex_id* was checked at *last_checked_at*. Mutates and returns
    *log*. An **upsert**: re-checking a work replaces its previous timestamp."""
    log.entries[openalex_id] = CheckRecord(last_checked_at=last_checked_at)
    return log


def read_check_log(path: Path | str) -> CheckLog:
    """Read the OpenAlex check-log at *path*. A missing file yields an empty
    :class:`CheckLog`; a malformed one raises :class:`CheckLogError` (never read as
    empty). The on-disk shape is validated by the generic boundary."""
    base = _base.read_check_log(path, _SCHEMA)
    entries = {
        openalex_id: CheckRecord(last_checked_at=record[_base.LAST_CHECKED_AT])
        for openalex_id, record in base.entries.items()
    }
    return CheckLog(schema_version=base.schema_version, entries=entries)


def write_check_log(path: Path | str, log: CheckLog) -> None:
    """Write *log* to *path* atomically via the generic boundary. Deterministic: two
    writes of the same data produce identical bytes."""
    base = _base.CheckLog(
        schema_version=log.schema_version,
        entries={
            openalex_id: {_base.LAST_CHECKED_AT: rec.last_checked_at}
            for openalex_id, rec in log.entries.items()
        },
    )
    _base.write_check_log(path, base, _SCHEMA)
