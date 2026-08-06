#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Report multi-source corroboration for engine-input facts.

For each accepted fact, how many DISTINCT sources back it (a trust signal a plain
notes wiki cannot give); and, for single-valued relations, the competing values
with their per-source support — the source-level view of a contradiction.

Informational: always exits 0.

Usage:
    python3 corroboration.py [--wiki <kb>]
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

from common import (  # noqa: E402
    composed_spelling,
    engine_atom_key,
    engine_facts,
    ensure_dirs,
    fold_relation_name,
    folded_relation_names,
    load_facts,
    single_valued_relations,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report multi-source corroboration of facts.")
    # --wiki is resolved by the import-time prepass (it must set FACTLOG_ROOT
    # before common is imported); this declaration is only for --help/validation.
    parser.add_argument("--wiki", default=os.environ.get("FACTLOG_ROOT", "."), help="KB root")
    parser.parse_args(argv)

    ensure_dirs()
    facts = load_facts()
    # `common.engine_atom_key` — the same fold `common.corroboration_counts` now
    # uses, since #342 made engine atoms fold too and the raw triple stopped
    # being the right key anywhere. The loop stays here rather than calling that
    # helper only because it collects the per-axis spellings in the same pass.
    #
    # The competing-values clause below folds the subject and object axes; the
    # head line and this list used the raw triple, so one report answered "how
    # many facts, how many corroborated" on one equivalence and "which values
    # compete" on another. Two spellings of one fact backed by two different
    # sources were counted as two facts with one source each — the corroboration
    # signal this tool exists to give, under-reported in exactly the mixed KB
    # #325 is about. Sources are counted per folded fact, so a source backing
    # both spellings counts once (summing the raw counts would double it). The
    # relation axis stays raw, matching the gate's deferred #210 decision.
    backing: dict[tuple[str, str, str], set[str]] = {}
    triple_spellings: dict[tuple[str, str, str], tuple[set[str], set[str]]] = {}
    for row in engine_facts(facts):
        key = engine_atom_key(row)
        backing.setdefault(key, set()).add(row["source"])
        subjects, objects = triple_spellings.setdefault(key, (set(), set()))
        subjects.add(row["subject"])
        objects.add(row["object"])
    counts = {key: len(srcs) for key, srcs in backing.items()}
    if not counts:
        print("corroboration: no engine-input facts")
        return 0

    multi = sum(1 for n in counts.values() if n > 1)
    print(f"corroboration: {len(counts)} fact(s); {multi} backed by >1 source")
    for key, n in sorted(counts.items()):
        # Report spellings that were actually written, the same provenance rule
        # the competing-values clause follows.
        subjects, objects = triple_spellings[key]
        print(
            f"  {n} source(s): {composed_spelling(subjects)}, {key[1]}, "
            f"{composed_spelling(objects)}"
        )

    # Source-level view of single-valued competition: same (subject, relation)
    # given different objects (each with its own source support).
    single_valued = single_valued_relations()
    if single_valued:
        # (folded subject, relation) -> folded object -> distinct backing sources,
        # plus the raw spellings folded into each, so the report can name a row
        # that was actually written.
        competing: dict[tuple[str, str], dict[str, set[str]]] = {}
        subject_spellings: dict[tuple[str, str], set[str]] = {}
        object_spellings: dict[tuple[str, str], dict[str, set[str]]] = {}
        # Membership folded, matching the gate (check_conflicts): policy names are
        # stored verbatim, so a uniformly-NFD KB matches nothing raw and its
        # competing values are silently never surfaced. The subject and untyped
        # object axes fold too, for the same reason the gate folds them — left
        # raw, two spellings of one value are listed as two competing values
        # ("한국대 (1 src); 한국대 (1 src)", indistinguishable on screen), which
        # invites superseding a row that says the same thing as its twin. The
        # relation axis stays raw, matching the gate's deferred #210 decision.
        #
        # This is a source-level view, not the gate: like `factlog status` it does
        # not parse typed literals, so a #116 cross-notation pair
        # (`amount(5400,"억")` and `amount(0.54,"조")`, one value to
        # check_conflicts) is still listed here as two competing values while the
        # gate exits 0. Closing that needs the checker's grouping shared rather
        # than reimplemented — a follow-up, since `tools/` is not importable from
        # the installed package (pyproject packages = ["factlog"]).
        #
        # That follow-up owns an input #325 WIDENED, not only inherited: an
        # NFD-authored typed literal, where the gate folds the object before
        # parsing and this view does not. See the same note in
        # `factlog/cli.py`'s status block for the measured case.
        sv_folded = folded_relation_names(single_valued)
        for row in engine_facts(facts):
            if fold_relation_name(row["relation"]) not in sv_folded:
                continue
            # NFC, the same fold the gate applies to these two axes. Not
            # `common._canonical_value`, which layers amount-quote normalization on
            # top and would diverge from check_conflicts._fold.
            pair = (unicodedata.normalize("NFC", row["subject"]), row["relation"])
            obj = unicodedata.normalize("NFC", row["object"])
            # Sources are collected here rather than read out of `counts`
            # because this loop walks only the single-valued rows and wants the
            # set, not the total. Both partitions agree — `counts` is keyed on
            # `engine_atom_key`, and (pair, obj) above spells out the same
            # (NFC subject, raw relation, NFC object) — so a source backing two
            # spellings of one value counts once on either path.
            competing.setdefault(pair, {}).setdefault(obj, set()).add(row["source"])
            subject_spellings.setdefault(pair, set()).add(row["subject"])
            object_spellings.setdefault(pair, {}).setdefault(obj, set()).add(row["object"])
        contested = {k: v for k, v in competing.items() if len(v) > 1}
        if contested:
            print(f"\ncorroboration: {len(contested)} single-valued relation(s) with competing values")
            for pair, objs in sorted(contested.items()):
                subject = composed_spelling(subject_spellings[pair])
                detail = "; ".join(
                    f"{composed_spelling(object_spellings[pair][obj])} ({len(srcs)} src)"
                    for obj, srcs in sorted(objs.items())
                )
                print(f"  {subject} / {pair[1]}: {detail}")
    return 0


if __name__ == "__main__":
    from common import run_cli

    sys.exit(run_cli(main))
