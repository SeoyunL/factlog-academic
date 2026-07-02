#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Detect contradictions among engine-input facts.

A relation declared *single-valued* (functional) in policy/single-valued.md may
hold at most one object per subject. If two distinct objects are asserted for the
same (subject, relation) among engine-input facts (status confirmed/accepted;
'superseded' rows are ignored), that is a contradiction — the kind of silent rot
a plain notes wiki accumulates. This surfaces it deterministically.

Resolution is human-in-the-loop and non-destructive: mark the outdated row's
status as 'superseded' in facts/candidates.csv (it stays for audit, drops out of
engine input, and the conflict clears).

Exit code: 0 if no conflicts, 1 if any conflict is found.

Usage:
    python3 check_conflicts.py [--wiki <kb>]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


# Resolve the KB root and export it before importing common, which binds
# its module-level paths from FACTLOG_ROOT at import time.
import factlog_config  # noqa: E402

os.environ["FACTLOG_ROOT"] = factlog_config.resolve_root_from_argv("--wiki")

import literal_types  # noqa: E402
from common import (  # noqa: E402
    TypedRelSpec,
    engine_facts,
    ensure_dirs,
    load_facts,
    single_valued_relations,
    typed_relations,
)


def _group_key(obj: str, spec: TypedRelSpec | None) -> tuple:
    """Return the equivalence key an *object* string is grouped under.

    For a relation declared typed (#116), the object's canonical scalar
    (``literal_types.normalize``) is the key, so equivalent notations of the same
    value (e.g. ``amount(5400,"억")`` and ``amount(0.54,"조")`` -> 5.4e11) collapse
    to one value instead of firing a false CONFLICT. ``amount`` needs its unit
    table, so ``spec.units`` is passed through.

    Falls back to the raw object string when the relation is untyped OR the value
    does not parse (normalize -> None): backward-compatible, lossless degrade. The
    two key spaces are tagged (``"scalar"`` vs ``"raw"``) so a scalar never
    collides with an unrelated raw string. Total: never raises (normalize is
    total)."""
    if spec is not None:
        scalar = literal_types.normalize(spec.type, obj, spec.units)
        if scalar is not None:
            return ("scalar", scalar)
    return ("raw", obj)


def detect_conflicts(
    facts: list[dict[str, str]],
    single_valued: set[str],
    typed: dict[str, TypedRelSpec] | None = None,
) -> dict[tuple[str, str], list[str]]:
    """Map (subject, relation) -> sorted distinct *display* objects, for
    single-valued relations that hold more than one *distinct value* (a
    contradiction).

    Distinctness is judged on the canonical grouping key (typed scalar when
    available, else the raw string — see ``_group_key``), so equivalent typed
    notations do not false-positive. The reported values, however, preserve the
    original object strings (provenance): each distinct key contributes one
    deterministic representative (the lexicographically smallest raw object seen
    for it). Deterministic; never raises."""
    typed = typed or {}
    # (subject, relation) -> group key -> set of raw object strings under it.
    by_key: dict[tuple[str, str], dict[tuple, set[str]]] = {}
    for row in engine_facts(facts):
        relation = row["relation"]
        if relation not in single_valued:
            continue
        obj = row["object"]
        key = _group_key(obj, typed.get(relation))
        groups = by_key.setdefault((row["subject"], relation), {})
        groups.setdefault(key, set()).add(obj)
    return {
        key: sorted(min(raws) for raws in groups.values())
        for key, groups in by_key.items()
        if len(groups) > 1
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect single-valued-relation contradictions.")
    parser.add_argument("--wiki", default=os.environ.get("FACTLOG_ROOT", "."), help="KB root")
    parser.parse_args(argv)

    ensure_dirs()
    single_valued = single_valued_relations()
    if not single_valued:
        print("check_conflicts: no single-valued relations declared (policy/single-valued.md); nothing to check")
        return 0

    conflicts = detect_conflicts(load_facts(), single_valued, typed_relations())
    if not conflicts:
        print(f"check_conflicts: 0 conflicts across {len(single_valued)} single-valued relation(s)")
        return 0

    print(f"check_conflicts: {len(conflicts)} conflict(s) found", file=sys.stderr)
    for (subject, relation), objects in sorted(conflicts.items()):
        print(
            f"  CONFLICT: single-valued '{relation}' on '{subject}' has "
            f"{len(objects)} values: {', '.join(objects)}",
            file=sys.stderr,
        )
    print(
        "  Resolve by marking the outdated row(s) status='superseded' in "
        "facts/candidates.csv, then re-run.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    from common import run_cli

    sys.exit(run_cli(main))
