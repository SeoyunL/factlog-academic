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
    corroboration_counts,
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
        sv_folded = folded_relation_names(single_valued)
        for row in engine_facts(facts):
            if fold_relation_name(row["relation"]) not in sv_folded:
                continue
            # NFC, the same fold the gate applies to these two axes. Not
            # `common._canonical_value`, which layers amount-quote normalization on
            # top and would diverge from check_conflicts._fold.
            pair = (unicodedata.normalize("NFC", row["subject"]), row["relation"])
            obj = unicodedata.normalize("NFC", row["object"])
            # Sources are re-counted here rather than read out of `counts`, which
            # is keyed on the raw triple: summing two spellings' counts would
            # double-count a source that backs both.
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
