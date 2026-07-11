# SPDX-License-Identifier: Apache-2.0
"""KB-level PubMed check-log: "when did we last refresh this PMID" memory (#168).

``pubmed-refresh`` re-fetches every PubMed record a KB holds and reports how its
retraction status has drifted from what NCBI now serves. To answer ``--older-than``
it must remember, per PMID, *when it last looked*. This module is the PubMed-facing
reader/writer for that memory.

## Generalized, not duplicated (#83/#168)

The read/write boundary lives once in
:mod:`factlog.integrations.common.check_log` — schema_version, atomic write,
sort-on-write, duplicate-key rejection, missing-is-empty, corrupt-is-error. This
module supplies only the PubMed schema: the id key ``pmid`` and the filename
``pubmed.json``. There are **no extra fields**: the record is exactly
``{pmid, last_checked_at}``.

Like OpenAlex's log — and unlike arXiv's — there is no version to remember. A
PubMed refresh compares the live retraction markers directly against what the
ledger recorded, so the timestamp alone is all the log must hold; ``--older-than``
needs nothing more.

## Placement and invisibility (#58/#63)

The file is ``<kb>/check-log/pubmed.json`` — the same already-proven-invisible
``check-log/`` sibling of ``sources/`` that holds ``arxiv.json`` and
``openalex.json``. It is outside ``common.SOURCE_ROOTS`` and outside
``source-provenance/``, so ``factlog sources`` / ``status`` / ``export`` never see
it. That invisibility is enforced by the generic boundary and its guard test.
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

#: The PubMed check-log's filename inside :data:`CHECK_LOG_DIR`.
CHECK_LOG_NAME = "pubmed.json"

#: The PubMed schema descriptor handed to the generic boundary: the id key and the
#: filename, and no extra fields — the record is just ``{pmid, last_checked_at}``.
_SCHEMA = _base.CheckLogSchema(name=CHECK_LOG_NAME, id_key="pmid", fields=())


@dataclass(frozen=True)
class CheckRecord:
    """One PMID's last refresh: only the timestamp the tool looked. PubMed has no
    version to remember, so — unlike arXiv's record — there is nothing else here."""

    last_checked_at: str


@dataclass
class CheckLog:
    """The whole check-log: a schema version and ``pmid -> CheckRecord``.

    ``entries`` is a mapping so a lookup by ``pmid`` is O(1); its iteration order
    is not significant, because :func:`write_check_log` sorts on write.
    """

    schema_version: int = SCHEMA_VERSION
    entries: dict[str, CheckRecord] = field(default_factory=dict)


def check_log_path(kb_root: Path | str) -> Path:
    """Map a KB root to its PubMed check-log path: ``<kb>/check-log/pubmed.json``."""
    return _base.check_log_path(kb_root, _SCHEMA)


def record_check(log: CheckLog, pmid: str, last_checked_at: str) -> CheckLog:
    """Record that *pmid* was checked at *last_checked_at*. Mutates and returns
    *log*. An **upsert**: re-checking a PMID replaces its previous timestamp."""
    log.entries[pmid] = CheckRecord(last_checked_at=last_checked_at)
    return log


def read_check_log(path: Path | str) -> CheckLog:
    """Read the PubMed check-log at *path*. A missing file yields an empty
    :class:`CheckLog`; a malformed one raises :class:`CheckLogError` (never read as
    empty). The on-disk shape is validated by the generic boundary."""
    base = _base.read_check_log(path, _SCHEMA)
    entries = {
        pmid: CheckRecord(last_checked_at=record[_base.LAST_CHECKED_AT])
        for pmid, record in base.entries.items()
    }
    return CheckLog(schema_version=base.schema_version, entries=entries)


def write_check_log(path: Path | str, log: CheckLog) -> None:
    """Write *log* to *path* atomically via the generic boundary. Deterministic: two
    writes of the same data produce identical bytes."""
    base = _base.CheckLog(
        schema_version=log.schema_version,
        entries={
            pmid: {_base.LAST_CHECKED_AT: rec.last_checked_at}
            for pmid, rec in log.entries.items()
        },
    )
    _base.write_check_log(path, base, _SCHEMA)
