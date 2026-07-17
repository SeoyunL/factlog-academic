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
     In an IDENTITY relation (a title, a DOI — declared literal AND injective in
     the data) a collision across subjects means a possible duplicate RECORD, not
     a split; anywhere else it is a query leak. See `_identity_relations`.

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

import unicodedata  # noqa: E402

from common import CANDIDATES_CSV, ENGINE_STATUSES, ensure_dirs, identity_relations, load_facts  # noqa: E402

# A value that wraps another: `기타(X)`, `other(X)`, `misc (X)`, and the reversed
# `X(기타)`. The wrapper WORD is what makes it a junk-drawer rather than a
# legitimate parenthetical such as `hyperoxia-induced lung injury (HLI)`, where
# the parens carry an abbreviation.
#
# `etc` is deliberately NOT a wrapper word: `ETC (electron transport chain)` is a
# real value in a biomedical KB, and flagging it would make this audit the very
# noise it was built to replace.
_WRAPPER_WORD = r"기타|기타사항|그 ?외|그외|other|others|misc"
_WRAPPER_RE = re.compile(rf"^(?:{_WRAPPER_WORD})\s*[(（]\s*(?P<inner>.+?)\s*[)）]$", re.I)
_WRAPPER_SUFFIX_RE = re.compile(rf"^(?P<inner>.+?)\s*[(（]\s*(?:{_WRAPPER_WORD})\s*[)）]$", re.I)

# Values that carry no information on their own. Matched EXACTLY (after NFC +
# casefold), never through the fold: folding would swallow `Na` (sodium) into
# `n/a`, which is a real value in a biomedical KB.
_PLACEHOLDERS = {
    "기타", "그외", "그 외", "기타사항", "불명", "미상", "해당없음", "없음",
    "other", "others", "misc", "unknown", "n/a", "n.a.", "none", "tbd", "-", "?",
}

# Separators BETWEEN DIGITS are load-bearing: `1.5` is not `15`, `2023-01-05` is
# not `20230105`, `2507.03697` is not `250703697`. Folding them together reported
# distinct numbers as one value split — a false positive that failed --strict on
# perfectly good data.
_DIGIT_SEP_RE = re.compile(r"(?<=\d)[-._/](?=\d)")
# A thousands separator IS noise, unlike a decimal point: `1,000` and `1000` are
# the same number, and policy/typed-relations.md lists both as valid `number`
# spellings. Dropped before the digit-separator guard runs.
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_SEP_RE = re.compile(r"[\s\-_./·]+")
_KEEP = "\x00"


def _fold(value: str) -> str:
    """Case/space/punctuation-insensitive key. Deterministic, not a similarity score.

    NFC-normalised first: a policy-free audit that cannot see NFD text would stay
    silent on exactly the KBs (Korean values, authored on macOS) it exists for.
    """
    folded = unicodedata.normalize("NFC", value)
    folded = _THOUSANDS_RE.sub("", folded)
    folded = _DIGIT_SEP_RE.sub(_KEEP, folded)
    return _SEP_RE.sub("", folded).casefold()


def _match_wrapper(value: str) -> str | None:
    """The wrapped value inside a junk-drawer label, or None."""
    normalised = unicodedata.normalize("NFC", value)
    for pattern in (_WRAPPER_RE, _WRAPPER_SUFFIX_RE):
        match = pattern.match(normalised)
        if match:
            return match.group("inner")
    return None


def audit(
    facts: list[dict[str, str]],
    identity_relations: set[str] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Group values by relation and apply the rules. Pure — no I/O.

    In an IDENTITY relation (declared in policy/identity-relations.md) the value
    identifies its subject, so two subjects whose values fold together are probably
    two records of one thing — not one value split in two. In every OTHER relation
    values are shared across subjects by design, and a folded collision between
    them IS the query leak: asking for `IL-8` misses the rows filed as `il 8`.

    Identity is DECLARED, never inferred. Judging it by subject count got the
    classification exactly backwards; deriving it from injectivity broke the moment
    a real duplicate record existed (which made the relation non-injective, flipped
    it to categorical, and failed the gate on the very thing the class describes).
    Undeclared ⇒ categorical, so a collision is reported as a leak — noisy rather
    than silent, which is the right way to be wrong here.
    """
    # Fold the declared identity set to NFC once so membership is decided on the
    # canonical form regardless of how the declaration was authored (#295), mirror
    # of the NFC-folded relation key below.
    identity = {unicodedata.normalize("NFC", r) for r in (identity_relations or set())}

    # Relations are bucketed on their NFC form so NFC- and NFD-authored spellings of
    # one relation share a bucket instead of splitting a cross-spelling duplicate
    # into two silent halves (#295). ``raw_rels`` keeps the spellings actually seen
    # so the report can name a deterministic representative (min) — provenance (#227).
    by_relation: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    subjects: dict[tuple[str, str], set[str]] = defaultdict(set)
    raw_rels: dict[str, set[str]] = defaultdict(set)
    for row in facts:
        frel = unicodedata.normalize("NFC", row["relation"])
        by_relation[frel][row["object"]] += 1
        subjects[(frel, row["object"])].add(row["subject"])
        raw_rels[frel].add(row["relation"])

    splits: list[dict[str, str]] = []
    wrappers: list[dict[str, str]] = []
    placeholders: list[dict[str, str]] = []
    duplicates: list[dict[str, str]] = []

    for frel in sorted(by_relation):
        # frel is the NFC bucket key; rep is the deterministic representative
        # spelling reported to a human (provenance stays a real occurrence, #227).
        rep = min(raw_rels[frel])
        values = by_relation[frel]
        folded: dict[str, list[str]] = defaultdict(list)
        for value in values:
            folded[_fold(value)].append(value)

        reported: set[str] = set()

        for value in sorted(values):
            if unicodedata.normalize("NFC", value).strip().casefold() in _PLACEHOLDERS:
                placeholders.append({"relation": rep, "value": value, "rows": str(values[value])})
                reported.add(value)
                continue
            inner = _match_wrapper(value)
            if inner is None:
                continue
            reported.add(value)
            # Does the wrapped value already exist on its own under this relation?
            twin = next((v for v in values if v != value and _fold(v) == _fold(inner)), None)
            if twin is not None:
                splits.append({
                    "relation": rep, "value": value, "rows": str(values[value]),
                    "twin": twin, "twin_rows": str(values[twin]),
                })
            else:
                wrappers.append({
                    "relation": rep, "value": value, "rows": str(values[value]),
                    "inner": inner,
                })

        # Spelling duplicates: same folded key, different surface. Values already
        # reported by the wrapper/placeholder rules are skipped so one problem is
        # not counted twice.
        for key in sorted(folded):
            distinct = sorted(set(folded[key]) - reported)
            if len(distinct) < 2:
                continue
            # Owners fold to NFC so ONE subject authored in a mix of NFC and NFD is a
            # single owner, not two — otherwise a categorical split is misread as a
            # cross-subject duplicate_record, exempting a real query leak from the
            # --strict gate (#314). ``subjects`` is keyed on the RAW object, and the
            # ``distinct`` values are raw object strings from the same source, so the
            # object axis stays raw-on-raw and needs no fold here. The stored subject
            # spellings are kept; the report shows the deterministic min per owner.
            folded_owners: dict[str, set[str]] = defaultdict(set)
            for v in distinct:
                for s in subjects[(frel, v)]:
                    folded_owners[unicodedata.normalize("NFC", s)].add(s)
            # See the docstring: policy decides, not the subject count. Both the
            # relation bucket (frel) and the identity set are NFC-folded, so an
            # NFD-authored relation still matches its identity declaration; the
            # reported name is the deterministic representative (rep).
            kind = "duplicate_record" if frel in identity and len(folded_owners) > 1 else "split"
            duplicates.append({
                "relation": rep,
                "values": " / ".join(f"{v} ({values[v]})" for v in distinct),
                "subjects": ", ".join(sorted(min(raws) for raws in folded_owners.values())),
                "kind": kind,
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

    # A brand-new KB has no candidates.csv. entity_audit prints a line and exits
    # 0 there; raising a traceback instead would make this unusable in the very
    # automation the --strict gate is for (and contradicts the documented
    # "always exits 0").
    ensure_dirs()
    if not CANDIDATES_CSV.is_file():
        print("value_audit: no candidate facts")
        return 0

    rows = load_facts()
    facts = rows if args.all_statuses else [r for r in rows if r["status"] in ENGINE_STATUSES]
    scope = "all candidate rows" if args.all_statuses else "engine facts"
    found = audit(facts, identity_relations())

    # A provable query leak is one value split across two strings. A folded value
    # shared by different subjects of an IDENTITY (attribute) relation is a
    # possible duplicate record instead — worth a human look, but not a leak, so
    # it must not fail a CI gate.
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
                print(f"      subjects: {f['subjects']} — queries for one spelling miss the other")
            else:
                print(f"      DIFFERENT subjects ({f['subjects']}) share this identifying value")
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

    # One general pointer, not a per-relation verdict. Guessing which relation is
    # an identity was the inference this tool deliberately dropped, and it came back
    # wrong through the advice channel: a marker shared by five subjects still got
    # "declare 염증지표 as an identity", which — if followed — would permanently
    # exempt the very leak this audit exists to catch. Name the file; let the human
    # decide which relations belong in it.
    if any(f["kind"] == "split" and len(f["subjects"].split(", ")) > 1 for f in found["duplicates"]):
        print(
            "\nnote: a spelling duplicate across DIFFERENT subjects is reported as a leak "
            "unless the relation is declared in policy/identity-relations.md. Declare only "
            "relations whose value names exactly one subject (a title, a DOI) — never a "
            "category many subjects share."
        )

    if not any(found.values()):
        print("  no value-hygiene problems found")

    if args.strict and leaks:
        print(f"\nvalue_audit: --strict — {leaks} provable query leak(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
