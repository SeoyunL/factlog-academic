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
    corroboration_counts,
    engine_facts,
    ensure_dirs,
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
    counts = corroboration_counts(facts)
    if not counts:
        print("corroboration: no engine-input facts")
        return 0

    multi = sum(1 for n in counts.values() if n > 1)
    print(f"corroboration: {len(counts)} fact(s); {multi} backed by >1 source")
    for (subject, relation, object_), n in sorted(counts.items()):
        print(f"  {n} source(s): {subject}, {relation}, {object_}")

    # Source-level view of single-valued competition: same (subject, relation)
    # given different objects (each with its own source support).
    single_valued = single_valued_relations()
    if single_valued:
        # Bucket the competition on the NFC-folded relation so NFC- and NFD-authored
        # spellings of one relation share a bucket instead of splitting the contest
        # (#295). ``raw_rels`` keeps the spellings seen for a deterministic reported
        # representative (min). A value's source support is the UNION of the sources
        # backing it across spellings, so one source that happens to back both an NFC
        # and an NFD row for the same value is still counted once — the distinct-
        # sources contract corroboration_counts guarantees. Union is order-independent.
        # The object is ALSO keyed on its NFC form (#307), so one value authored in a
        # mix of NFC and NFD is a single competitor rather than a false two-way
        # contest; ``raw_objs`` keeps the spellings for a deterministic representative
        # (min), matching how the relation is handled above (#295).
        sources_by: dict[tuple[str, str], dict[str, set[str]]] = {}
        raw_rels: dict[tuple[str, str], set[str]] = {}
        raw_objs: dict[tuple[tuple[str, str], str], set[str]] = {}
        for row in engine_facts(facts):
            # single_valued is loaded NFC-normalized; the fact relation may be NFD.
            # Fold the membership probe so an NFD-authored fact still competes (#293).
            if unicodedata.normalize("NFC", row["relation"]) not in single_valued:
                continue
            bucket = (row["subject"], unicodedata.normalize("NFC", row["relation"]))
            fobj = unicodedata.normalize("NFC", row["object"])
            objs = sources_by.setdefault(bucket, {})
            objs.setdefault(fobj, set()).add(row["source"])
            raw_objs.setdefault((bucket, fobj), set()).add(row["object"])
            raw_rels.setdefault(bucket, set()).add(row["relation"])
        contested = {b: objs for b, objs in sources_by.items() if len(objs) > 1}
        if contested:
            print(f"\ncorroboration: {len(contested)} single-valued relation(s) with competing values")
            for bucket, objs in sorted(contested.items()):
                subject = bucket[0]
                relation = min(raw_rels[bucket])
                reps = sorted(
                    (min(raw_objs[(bucket, fobj)]), len(srcs)) for fobj, srcs in objs.items()
                )
                detail = "; ".join(f"{obj} ({n} src)" for obj, n in reps)
                print(f"  {subject} / {relation}: {detail}")
    return 0


if __name__ == "__main__":
    from common import run_cli

    sys.exit(run_cli(main))
