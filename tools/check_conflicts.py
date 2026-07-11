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
import unicodedata
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
    canonical_value,
    engine_facts,
    ensure_dirs,
    hierarchy_ancestors,
    load_facts,
    relation_aliases,
    single_valued_relations,
    typed_relations,
    value_hierarchy,
)


def _canonicalize(relation: str, aliases: dict[str, str]) -> str:
    """Return the canonical relation name when *relation* participates in the
    alias map; otherwise return *relation* verbatim (NFD-preserving).

    Participation mirrors ``common.canonical_atoms``:

    * relation is an alias **key** (raw predicate) → ``aliases[NFC(relation)]``
    * relation **is** a canonical value (stored literally) → its NFC form
    * relation is not in the alias map → verbatim (no normalization)

    When *aliases* is empty the function short-circuits and returns *relation*
    unchanged, preserving byte-identical behaviour for KBs without a
    relation-aliases.md file.
    """
    if not aliases:
        return relation
    rn = unicodedata.normalize("NFC", relation)
    if rn in aliases:
        return aliases[rn]
    if rn in set(aliases.values()):
        return rn
    return relation


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
    total).

    **ordinal unit loss (#218 / #224 A):** ``normalize("ordinal", …)`` keeps only
    the integer *rank* — the ordinal-class unit (호/위/번/차/등/째) is dropped at
    parse time (``literal_types.parse_ordinal``), so it never enters the key. A
    cross-unit pair therefore collapses onto one scalar: ``제3호`` and ``3위`` both
    key as ``("scalar", 3)``. This is **by design** and consistent with the engine,
    which likewise compares ordinals on rank alone (``_TYPED_COL["ordinal"]`` is a
    bare int64, no unit column). ordinal is a *rank-only* contract: same rank =
    same value. If two notations denote genuinely different domains (a rank vs a
    house number), that distinction belongs in the model — declare them as
    **separate relations**, not one single-valued ordinal relation. (Contrast
    ``amount``, where 억↔조 equivalence is the intended collapse.)

    **int64 divergence note (#224 C):** ``normalize`` can return a scalar wider
    than int64 (mainly ``number`` via ``parse_number_scaled``, and unbounded
    ``ordinal`` ranks — both lack a range guard; ``amount`` already degrades to raw
    when ``parse_amount`` overflows, #205). The engine, by contrast, **skips insertion** of an out-of-int64-range
    scalar (see ``insert_typed_facts`` in ``common.py`` ~ the ``-(2**63) <= scalar
    < 2**63`` guard). So this checker may group under a scalar the engine would
    drop. That affects **grouping only** (never insertion) and is harmless: the
    checker is strictly more willing to merge equivalents, never less. No behaviour
    change here — note only."""
    if spec is not None:
        scalar = literal_types.normalize(spec.type, obj, spec.units)
        if scalar is not None:
            return ("scalar", scalar)
    return ("raw", obj)


def detect_conflicts(
    facts: list[dict[str, str]],
    single_valued: set[str],
    typed: dict[str, TypedRelSpec] | None = None,
    aliases: dict[str, str] | None = None,
    hierarchy: dict[str, dict[str, set[str]]] | None = None,
) -> dict[tuple[str, str], list[str]]:
    """Map (subject, canonical_relation) -> sorted distinct *display* objects,
    for single-valued relations that hold more than one *distinct value* (a
    contradiction).

    Distinctness is judged on the canonical grouping key (typed scalar when
    available, else the raw string — see ``_group_key``), so equivalent typed
    notations do not false-positive. The reported values, however, preserve the
    original object strings (provenance): each distinct key contributes one
    deterministic representative (the lexicographically smallest raw object seen
    for it). Deterministic; never raises.

    Two grouping subtleties documented on ``_group_key``: ordinal collapses
    cross-unit notations onto the shared rank (rank-only contract, #218/#224 A),
    and a scalar wider than int64 groups here even though the engine skips its
    insertion (harmless grouping-only divergence, #224 C).

    **Alias canonicalization (#227):** when *aliases* is provided (non-empty),
    each row's relation is canonicalized via ``_canonicalize`` before the
    single-valued membership test and before grouping.  This causes surface
    variants that map to the same canonical name (e.g. ``게재연도`` and ``발행년도``
    both aliased to ``published_year``) to collide under one key, so a cross-
    variant contradiction is detected as a single conflict on the canonical
    name.  Relations that do **not** participate in the alias map are passed
    through verbatim (no normalization), preserving byte-identical behaviour for
    those predicates.

    When *aliases* is ``None`` or ``{}`` the function is byte-identical to the
    pre-#227 behaviour: the raw relation string is used throughout, and an NFD-
    authored relation name is reported exactly as written (no silent NFC coercion
    for non-participating relations).

    **Typed-spec lookup (#210):** the ``typed`` dict is keyed by NFC-normalized
    names (``typed_relations`` normalizes at ``common._parse_typed_relations``).
    The lookup first tries the canonical relation name (already NFC when it came
    from the alias map), then falls back to the NFC form of the raw relation
    string.  This ensures that an NFD-authored relation that also participates in
    the alias map still reaches its typed spec, so equivalent notations (억↔조)
    collapse correctly."""
    typed = typed or {}
    aliases = aliases or {}
    hierarchy = value_hierarchy() if hierarchy is None else hierarchy
    # Precompute the set of canonical single-valued relation names so the
    # per-row membership test is O(1).
    sv = {_canonicalize(r, aliases) for r in single_valued}
    # (subject, canonical_relation) -> group key -> set of raw object strings.
    by_key: dict[tuple[str, str], dict[tuple, set[str]]] = {}
    for row in engine_facts(facts):
        relation = row["relation"]
        canon = _canonicalize(relation, aliases)
        if canon not in sv:
            continue
        obj = row["object"]
        # Typed-spec lookup: try canonical name first (NFC by construction when
        # it came from the alias map), then NFC of the raw relation (#210).
        spec = typed.get(canon) or typed.get(unicodedata.normalize("NFC", relation))
        key = _group_key(obj, spec)
        groups = by_key.setdefault((row["subject"], canon), {})
        groups.setdefault(key, set()).add(obj)
    conflicts: dict[tuple[str, str], list[str]] = {}
    for (subject, canon), groups in by_key.items():
        if len(groups) <= 1:
            continue
        values = sorted(min(raws) for raws in groups.values())
        if _is_specialisation_chain(values, canon, hierarchy):
            continue
        conflicts[(subject, canon)] = values
    return conflicts


def _is_specialisation_chain(
    values: list[str],
    relation: str,
    hierarchy: dict[str, dict[str, set[str]]] | None,
) -> bool:
    """True when every value sits on ONE declared ancestor chain for *relation*.

    `연구유형: 코호트연구 ⊂ 관찰연구` means a cohort study IS an observational study,
    so a paper carrying both is not contradicting itself -- it is being described at
    two levels of precision, and both rows are true. Reporting that as a conflict
    made finalize refuse to compile, and the resolution text then told the user to
    retire one of them: retire the subtype and you lose the more precise fact, retire
    the supertype and you delete something the source states outright. Either way a
    human-checked fact is discarded on no evidence (#219).

    A chain, not merely "some pair is related": with 관찰연구 / 코호트연구 / 실험연구 the
    first two are on a chain but 실험연구 is a genuine sibling, and that is still a
    contradiction. So require a single most-specific value that has every other value
    among its declared ancestors.

    *hierarchy* is what value_hierarchy() returns, i.e. already transitively closed,
    so a grandparent is reachable in one lookup.
    """
    if not hierarchy or len(values) < 2:
        return False
    for candidate in values:
        ancestors = hierarchy_ancestors(hierarchy, relation, candidate)
        if not ancestors:
            continue
        others = {canonical_value(v) for v in values if v != candidate}
        if others <= ancestors:
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect single-valued-relation contradictions.")
    parser.add_argument("--wiki", default=os.environ.get("FACTLOG_ROOT", "."), help="KB root")
    parser.parse_args(argv)

    ensure_dirs()
    single_valued = single_valued_relations()
    if not single_valued:
        print("check_conflicts: no single-valued relations declared (policy/single-valued.md); nothing to check")
        return 0

    conflicts = detect_conflicts(load_facts(), single_valued, typed_relations(), relation_aliases())
    if not conflicts:
        print(f"check_conflicts: 0 conflicts across {len(single_valued)} single-valued relation(s)")
        return 0

    print(f"check_conflicts: {len(conflicts)} conflict(s) found", file=sys.stderr)
    aliases = relation_aliases()
    for (subject, relation), objects in sorted(conflicts.items()):
        suffix = " (canonical; incl. surface variants)" if aliases and relation in set(aliases.values()) else ""
        print(
            f"  CONFLICT: single-valued '{relation}'{suffix} on '{subject}' has "
            f"{len(objects)} values: {', '.join(objects)}",
            file=sys.stderr,
        )
    print(
        "  Resolve with the human gate, not by hand-editing facts/candidates.csv:\n"
        "    factlog eject --fact SUBJECT RELATION OBJECT   retire an accepted row\n"
        "    factlog amend SUBJECT RELATION OBJECT --set-object NEW   correct a value\n"
        "  Retire a row only when it is genuinely outdated or wrong. If the values are\n"
        "  a supertype and its subtype (a cohort study IS an observational study),\n"
        "  neither is wrong: declare the relationship in policy/value-hierarchy.md\n"
        "  (e.g. `- RELATION: SUBTYPE ⊂ SUPERTYPE`) and both rows are kept.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    from common import run_cli

    sys.exit(run_cli(main))
