#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Value audit: surface OBJECT-value hygiene problems, per relation.

A KB's relation vocabulary is curated (policy declares attribute/typed/single-
valued relations). Its *value* vocabulary is not: values arrive one extraction at
a time, and nothing notices when the same thing lands twice under two spellings.
Observed in a real KB, both already `accepted`:

    염증지표: IL-10 (3 rows)   AND   기타(IL-10) (1 row)

`relation(P, "염증지표", "IL-10")?` returns 3. The fourth row is hiding behind a
different string. That is a silent omission — the one failure mode this KB exists
to prevent — and no existing check catches it. `entity_audit.py` reports entity
fragmentation, but it compares every entity against every other by shared token,
which on this KB yields 2275 candidates: the real split is in there, drowned.

So this audit is deliberately NARROW and deterministic, not a similarity
firehose. Values are only ever compared WITHIN THE SAME RELATION (a value of
`염증지표` has nothing to do with a value of `대상질환`), and every finding is a
rule, not a guess:

  1. Split wrapper — a value wraps another value of the SAME relation, e.g.
     `기타(IL-10)` beside `IL-10`. The same thing filed twice. Highest severity:
     queries are provably leaking today.
  2. Wrapper value — a wrapped value whose inner text is NOT (yet) a value on its
     own, e.g. `기타(INFLA-score)`. Not a split, but unqueryable by its own name:
     asking for `INFLA-score` returns nothing.
  3. Placeholder — a junk-drawer value that carries no information (`기타`,
     `불명`, `미상`, `N/A`, `unknown`, `-`). It cannot be queried usefully and
     hides whatever the source actually said.
  4. Spelling duplicate — two values of one relation that are equal after folding
     case, spaces, and punctuation (`IL-8` / `il 8`). Deterministic, not fuzzy.

Findings are reported for HUMAN judgement; nothing is merged. Fix with
`factlog amend <subject> <relation> <object> --set-object <canonical>`, which
rewrites the row durably (candidates.csv AND the backing runs/*.json).

Exit 0 by default (informational, like entity_audit). `--strict` exits non-zero
when a split wrapper or a spelling duplicate is found — those are provable
query leaks, suitable for a CI gate.

Usage:
    python3 value_audit.py [--wiki <kb>] [--strict] [--all-statuses]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

_TOOLS_DIR = Path(__file__).parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import factlog_config  # noqa: E402

os.environ["FACTLOG_ROOT"] = factlog_config.resolve_root_from_argv("--wiki")

from common import ENGINE_STATUSES, load_facts  # noqa: E402

# A value that wraps another: `기타(X)`, `other(X)`, `misc (X)`. The wrapper word
# is what makes it a junk-drawer rather than a legitimate parenthetical such as
# `hyperoxia-induced lung injury (HLI)`, where the parens carry an abbreviation.
_WRAPPER_RE = re.compile(r"^(?:기타|기타사항|그 ?외|other|misc|etc)\s*[(（]\s*(?P<inner>.+?)\s*[)）]$", re.I)

# Values that carry no information on their own.
_PLACEHOLDERS = {
    "기타", "그외", "그 외", "기타사항", "불명", "미상", "해당없음", "없음",
    "other", "others", "misc", "unknown", "n/a", "na", "none", "tbd", "-", "?",
}


def _fold(value: str) -> str:
    """Case/space/punctuation-insensitive key. Deterministic, not a similarity score."""
    return re.sub(r"[\s\-_./·]+", "", value).casefold()


def audit(facts: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    """Group values by relation and apply the four rules. Pure — no I/O."""
    by_relation: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    subjects: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in facts:
        by_relation[row["relation"]][row["object"]] += 1
        subjects[(row["relation"], row["object"])].add(row["subject"])

    splits: list[dict[str, str]] = []
    wrappers: list[dict[str, str]] = []
    placeholders: list[dict[str, str]] = []
    duplicates: list[dict[str, str]] = []

    for relation in sorted(by_relation):
        values = by_relation[relation]
        folded: dict[str, list[str]] = defaultdict(list)
        for value in values:
            folded[_fold(value)].append(value)

        for value in sorted(values):
            if value.strip().casefold() in _PLACEHOLDERS or _fold(value) in {_fold(p) for p in _PLACEHOLDERS}:
                placeholders.append({"relation": relation, "value": value, "rows": str(values[value])})
                continue
            match = _WRAPPER_RE.match(value)
            if not match:
                continue
            inner = match.group("inner")
            # Does the wrapped value already exist on its own under this relation?
            twin = next((v for v in values if v != value and _fold(v) == _fold(inner)), None)
            if twin is not None:
                splits.append({
                    "relation": relation, "value": value, "rows": str(values[value]),
                    "twin": twin, "twin_rows": str(values[twin]),
                })
            else:
                wrappers.append({
                    "relation": relation, "value": value, "rows": str(values[value]),
                    "inner": inner,
                })

        # Spelling duplicates: same folded key, different surface. Skip pairs the
        # wrapper rules already reported, so one problem is not counted twice.
        reported = {f["value"] for f in splits + wrappers if f["relation"] == relation}
        for surfaces in folded.values():
            distinct = sorted(set(surfaces) - reported)
            if len(distinct) > 1:
                # Whether the spellings sit on ONE subject or on several changes
                # what the finding means, so say which. One subject spelled two
                # ways is a value split (queries leak). Several subjects sharing a
                # folded value is a possible DUPLICATE RECORD — two entries for
                # one real thing — which is a different repair and a different
                # conversation with the human.
                owners = {s for v in distinct for s in subjects[(relation, v)]}
                duplicates.append({
                    "relation": relation,
                    "values": " / ".join(f"{v} ({values[v]})" for v in distinct),
                    "subjects": ", ".join(sorted(owners)),
                    "kind": "split" if len(owners) == 1 else "duplicate_record",
                })

    return {
        "splits": splits,
        "wrappers": wrappers,
        "placeholders": placeholders,
        "duplicates": duplicates,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit relation OBJECT values for splits, wrappers, and placeholders.")
    parser.add_argument("--wiki", default=os.environ.get("FACTLOG_ROOT", "."), help="KB root")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when a split wrapper or spelling duplicate is found (provable query leaks)",
    )
    parser.add_argument(
        "--all-statuses",
        action="store_true",
        help="audit every candidate row, not just engine input (confirmed/accepted)",
    )
    args = parser.parse_args(argv)

    rows = load_facts()
    facts = rows if args.all_statuses else [r for r in rows if r["status"] in ENGINE_STATUSES]
    scope = "all candidate rows" if args.all_statuses else "engine facts"
    found = audit(facts)

    # A provable query leak is one value split across two strings. A folded value
    # shared by DIFFERENT subjects is a possible duplicate record — worth a human
    # look, but not a leak, so it must not fail a CI gate.
    leaks = len(found["splits"]) + sum(1 for f in found["duplicates"] if f["kind"] == "split")
    print(
        f"value_audit ({scope}): {len(found['splits'])} split wrapper(s), "
        f"{len(found['wrappers'])} wrapper value(s), {len(found['placeholders'])} placeholder(s), "
        f"{len(found['duplicates'])} spelling duplicate(s)"
    )

    if found["splits"]:
        print("\nSPLIT — the same value filed under two strings; queries are leaking NOW:")
        for f in found["splits"]:
            print(f"  • {f['relation']}: '{f['value']}' ({f['rows']}) == '{f['twin']}' ({f['twin_rows']})")
            print(f"      asking for '{f['twin']}' misses the {f['rows']} row(s) under '{f['value']}'")
            print(f"      fix: factlog amend <subject> {f['relation']} '{f['value']}' --set-object '{f['twin']}'")

    if found["duplicates"]:
        print("\nSPELLING DUPLICATE — equal after folding case/space/punctuation:")
        for f in found["duplicates"]:
            print(f"  • {f['relation']}: {f['values']}")
            if f["kind"] == "split":
                print(f"      one subject ({f['subjects']}) spelled two ways — queries are leaking")
            else:
                print(f"      DIFFERENT subjects ({f['subjects']}) share this value")
                print("      → not a spelling split: check whether these are duplicate records")

    if found["wrappers"]:
        print("\nWRAPPER — wrapped value is not queryable by its own name:")
        for f in found["wrappers"]:
            print(f"  • {f['relation']}: '{f['value']}' ({f['rows']}) — asking for '{f['inner']}' returns nothing")
            print(f"      fix: factlog amend <subject> {f['relation']} '{f['value']}' --set-object '{f['inner']}'")

    if found["placeholders"]:
        print("\nPLACEHOLDER — carries no information; hides what the source said:")
        for f in found["placeholders"]:
            print(f"  • {f['relation']}: '{f['value']}' ({f['rows']} row(s))")

    if not any(found.values()):
        print("  no value-hygiene problems found")

    if args.strict and leaks:
        print(f"\nvalue_audit: --strict — {leaks} provable query leak(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
