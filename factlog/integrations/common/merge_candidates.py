# SPDX-License-Identifier: Apache-2.0
"""KB-level merge-candidate ledger: a record of *pairs a human should look at* (#75).

## What this is — a third kind of KB-level store

The title+author+year fallback (:mod:`factlog.integrations.common.matcher`) never
merges. When it fires it surfaces a *candidate*: two source records that look like
the same work but share no exact identifier. This module is the reader/writer for
the memory of those candidates, and nothing else — no matcher, no CLI, no merge.

It is deliberately **neither** of the two stores it resembles:

* Not the provenance sidecar (``common/provenance.py``): that is one file *per
  source*, keyed by ``(type, id)``, recording *where one source came from*. A
  candidate is a decision *about a pair* of sources — the wrong shape and the wrong
  key. A ``.json`` dropped under ``source-provenance/`` is misread as a sidecar by
  ``provenance.is_sidecar``.
* Not the arXiv check-log (``arxiv/check_log.py``): that records *when the tool last
  looked* at an arXiv paper. A candidate is not a timestamped observation.

So it lives in its own sibling directory of ``sources/``, exactly as the check-log
does::

    <kb>/sources/foo.md              <- byte-immutable original (P4)
    <kb>/source-provenance/…         <- per-source provenance sidecars
    <kb>/check-log/arxiv.json        <- KB-level arXiv check-log
    <kb>/merge-candidates/candidates.json  <- this file: KB-level pair ledger

Being outside ``common.SOURCE_ROOTS`` is what makes it invisible to source
enumeration — measured with the real CLI in the tests (the #58 mistake #63 caught
was asserting invisibility without running the enumerators), with a guard test that
adding the directory to ``SOURCE_ROOTS`` *does* surface it.

## Keyed by an order-normalized pair of the two sources' own identities

Each side of a pair is that source's own identity — ``("openalex", "W123")``,
``("arxiv", "2509.00891")``, ``("zotero", "K9")``. A :class:`CandidatePair` sorts
its two endpoints on construction, so the pair ``{A, B}`` and ``{B, A}`` are one
record and the ledger cannot hold both. ``state`` is ``"pending"`` or
``"rejected"`` only. ``"accepted"`` is #76's territory — accepting a candidate is a
*merge*, which this issue does not build — and is **never written here** (it is not
even a legal state at the read boundary).

## How suppression works, and how a human rejects

A pair present in **any** state is never surfaced again, which is what satisfies
"a rejected pairing is not re-proposed on every import" without waiting on human
action: the first surfacing records it ``pending``, and every later import of the
same paper reads it back and stays quiet.

**There is no ``reject`` command in this issue.** A human rejects a pair by
hand-editing this JSON: change that pair's ``"state"`` from ``"pending"`` to
``"rejected"``. That is a known rough edge, chosen deliberately (see #75 H4) until a
real KB shows how many candidates it produces and what a reject UI should look like;
#76 owns ``accept``. A ``rejected`` entry stays rejected and is never re-surfaced.

## On-disk shape and the read boundary

A list of records, each ``{a_type, a_id, b_type, b_id, state, score, recorded_at}``,
sorted by the pair key on write — the same list-of-records posture as
``common/provenance.py`` and ``arxiv/check_log.py``. Mirrors their proven contract:
stdlib ``json`` (zero new dependency), ``sort_keys=True`` + ``indent=2`` + trailing
newline for byte-deterministic, human-diffable, hand-editable output, atomic write
via ``_textio.atomic_write_text``, a ``schema_version`` from day one, and
``recorded_at`` supplied by the caller — **never** read from a clock inside this
module, so a write is a pure function of its data.

:func:`read_candidates` validates at the boundary rather than trusting JSON to be
well-shaped, because a corrupt shape read as an empty ledger would let the next
write erase real candidates. Every corrupt shape raises :class:`MergeCandidatesError`:
invalid JSON, a non-object top level (e.g. an array), a missing/future
``schema_version``, a missing ``candidates`` key, a ``candidates`` that is not a
list, a record that is not an object, a missing/mistyped field, an illegal
``state``, a duplicate pair, and — via ``object_pairs_hook`` — a duplicated key
inside any object. A missing file alone reads as an empty ledger, so a first run can
write one.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factlog.integrations.common._textio import atomic_write_text

#: Sibling directory of ``sources/`` that holds the KB-level merge-candidate ledger.
MERGE_CANDIDATES_DIR = "merge-candidates"

#: The ledger's filename inside :data:`MERGE_CANDIDATES_DIR`.
MERGE_CANDIDATES_NAME = "candidates.json"

#: Serialization schema. Bump when the on-disk layout changes incompatibly.
SCHEMA_VERSION = 1

#: A surfaced-but-unreviewed pair.
STATE_PENDING = "pending"
#: A pair a human has rejected by hand-editing the JSON (see the module docstring).
STATE_REJECTED = "rejected"
#: The only legal states. ``"accepted"`` is intentionally absent — accepting a
#: candidate is a merge (#76), never written by this issue.
LEGAL_STATES = frozenset({STATE_PENDING, STATE_REJECTED})

#: The exact keys an on-disk record must carry; anything more is a corrupt shape.
_RECORD_KEYS = frozenset({"a_type", "a_id", "b_type", "b_id", "state", "score", "recorded_at"})

#: Number of decimal places the score is rounded to before storage. Fixes the bytes
#: so two writes of the same match are identical regardless of float formatting.
_SCORE_PRECISION = 4


class MergeCandidatesError(ValueError):
    """A candidate ledger on disk is malformed and cannot be read as one.

    Raised rather than reading the file as empty: an empty read would let the next
    write erase real candidates (the failure #58/#63 warned about).
    """


def candidates_path(kb_root: Path | str) -> Path:
    """Map a KB root to its candidate ledger: ``<kb>/merge-candidates/candidates.json``.

    The one place that knows the naming rule. Takes the KB root directly (not a
    source path) because the ledger is per-KB, not per-source.
    """
    return Path(kb_root) / MERGE_CANDIDATES_DIR / MERGE_CANDIDATES_NAME


def normalize_pair(a: tuple[str, str], b: tuple[str, str]) -> tuple[tuple[str, str], tuple[str, str]]:
    """Order two ``(type, id)`` endpoints so ``{A, B}`` and ``{B, A}`` are one pair."""
    return (a, b) if a <= b else (b, a)


@dataclass(frozen=True)
class CandidatePair:
    """One surfaced pair: two source identities, a state, a score and a timestamp.

    The two endpoints are order-normalized on construction, so equality and the
    on-disk key are independent of which source was the incoming one.
    """

    a_type: str
    a_id: str
    b_type: str
    b_id: str
    state: str
    score: float
    recorded_at: str

    @classmethod
    def create(cls, incoming: tuple[str, str], existing: tuple[str, str], *,
               state: str, score: float, recorded_at: str) -> CandidatePair:
        (a_type, a_id), (b_type, b_id) = normalize_pair(incoming, existing)
        return cls(a_type=a_type, a_id=a_id, b_type=b_type, b_id=b_id,
                   state=state, score=round(float(score), _SCORE_PRECISION),
                   recorded_at=recorded_at)

    @property
    def key(self) -> tuple[str, str, str, str]:
        """The idempotency key: the order-normalized endpoint pair."""
        return (self.a_type, self.a_id, self.b_type, self.b_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "a_type": self.a_type, "a_id": self.a_id,
            "b_type": self.b_type, "b_id": self.b_id,
            "state": self.state, "score": self.score, "recorded_at": self.recorded_at,
        }


@dataclass
class MergeCandidates:
    """The whole ledger: a schema version and its pairs.

    In-memory order is not significant — :func:`write_candidates` sorts on write, so
    equality and byte-output are independent of insertion order.
    """

    schema_version: int = SCHEMA_VERSION
    pairs: list[CandidatePair] = field(default_factory=list)

    def has_pair(self, incoming: tuple[str, str], existing: tuple[str, str]) -> bool:
        """True when this pair is already recorded, in any state (so never re-surface)."""
        (a, ai), (b, bi) = normalize_pair(incoming, existing)
        key = (a, ai, b, bi)
        return any(p.key == key for p in self.pairs)


def add_candidate(ledger: MergeCandidates, pair: CandidatePair) -> MergeCandidates:
    """Append *pair* unless its endpoint pair is already present. Mutates and returns.

    Idempotent on the pair key across **every** state — a pair already recorded (even
    ``rejected``) is a no-op, so a human's rejection is never overwritten by a later
    ``pending`` surfacing and re-import stays byte-unchanged (mirrors
    ``provenance.add_source``'s idempotent no-op).
    """
    for existing in ledger.pairs:
        if existing.key == pair.key:
            return ledger  # already recorded in some state -> no-op
    ledger.pairs.append(pair)
    return ledger


def _serialize(ledger: MergeCandidates) -> str:
    records = sorted((p.to_dict() for p in ledger.pairs), key=lambda d: d["a_type"] + "\0" +
                     d["a_id"] + "\0" + d["b_type"] + "\0" + d["b_id"])
    payload = {"schema_version": ledger.schema_version, "candidates": records}
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` refusing a repeated key in any JSON object.

    ``json.loads`` silently keeps the last value for a duplicate key, so a record
    with two ``a_id`` keys would parse with one silently gone. Refuse it.
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MergeCandidatesError(f"candidate ledger has a duplicate key {key!r}")
        result[key] = value
    return result


def read_candidates(path: Path | str) -> MergeCandidates:
    """Read the ledger at *path*. A missing file yields an empty
    :class:`MergeCandidates` (so a first run can write one); a file that exists but
    is not a well-shaped ledger raises :class:`MergeCandidatesError` rather than
    reading as empty (which would let the next write erase real candidates)."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return MergeCandidates()

    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise MergeCandidatesError(f"candidate ledger is not valid JSON: {p}") from exc

    if not isinstance(data, Mapping):
        raise MergeCandidatesError(f"candidate ledger is not a JSON object: {p}")

    for required in ("schema_version", "candidates"):
        if required not in data:
            raise MergeCandidatesError(f"candidate ledger has no {required!r} key: {p}")

    schema_version = data["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise MergeCandidatesError(f"candidate ledger 'schema_version' is not an integer: {p}")
    if schema_version > SCHEMA_VERSION:
        raise MergeCandidatesError(
            f"candidate ledger schema_version {schema_version} is newer than this "
            f"factlog understands (max {SCHEMA_VERSION}): {p}"
        )

    raw_records = data["candidates"]
    if not isinstance(raw_records, list):
        raise MergeCandidatesError(f"candidate ledger 'candidates' is not a list: {p}")

    pairs: list[CandidatePair] = []
    seen: set[tuple[str, str, str, str]] = set()
    for raw in raw_records:
        if not isinstance(raw, Mapping):
            raise MergeCandidatesError(f"candidate record is not a JSON object: {raw!r} in {p}")
        missing = _RECORD_KEYS - set(raw)
        if missing:
            raise MergeCandidatesError(
                f"candidate record is missing {sorted(missing)}: {raw!r} in {p}")
        extra = set(raw) - _RECORD_KEYS
        if extra:
            raise MergeCandidatesError(
                f"candidate record has unexpected key(s) {sorted(extra)}: {raw!r} in {p}")
        for name in ("a_type", "a_id", "b_type", "b_id", "state", "recorded_at"):
            if not isinstance(raw[name], str):
                raise MergeCandidatesError(
                    f"candidate record field {name!r} must be a string, got "
                    f"{type(raw[name]).__name__}: {raw!r} in {p}")
        if raw["state"] not in LEGAL_STATES:
            raise MergeCandidatesError(
                f"candidate record has illegal state {raw['state']!r} (legal: "
                f"{sorted(LEGAL_STATES)}): {raw!r} in {p}")
        score = raw["score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise MergeCandidatesError(
                f"candidate record field 'score' must be a number, got "
                f"{type(score).__name__}: {raw!r} in {p}")
        pair = CandidatePair(
            a_type=raw["a_type"], a_id=raw["a_id"],
            b_type=raw["b_type"], b_id=raw["b_id"],
            state=raw["state"], score=float(score), recorded_at=raw["recorded_at"],
        )
        if pair.key in seen:
            raise MergeCandidatesError(
                f"candidate ledger has two records for pair {pair.key}: {p}")
        seen.add(pair.key)
        pairs.append(pair)

    return MergeCandidates(schema_version=schema_version, pairs=pairs)


def write_candidates(path: Path | str, ledger: MergeCandidates) -> None:
    """Write *ledger* to *path* atomically (temp file + ``os.replace`` via
    ``_textio.atomic_write_text``), creating ``merge-candidates/`` if needed.
    Deterministic: two writes of the same data produce identical bytes."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, _serialize(ledger))
