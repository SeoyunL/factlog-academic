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
        competing: dict[tuple[str, str], dict[str, int]] = {}
        for row in engine_facts(facts):
            # single_valued is loaded NFC-normalized; the fact relation may be NFD.
            # Fold the membership probe so an NFD-authored fact still competes (#293).
            if unicodedata.normalize("NFC", row["relation"]) in single_valued:
                competing.setdefault((row["subject"], row["relation"]), {})
                key = row["object"]
                competing[(row["subject"], row["relation"])][key] = counts.get(
                    (row["subject"], row["relation"], key), 0
                )
        contested = {k: v for k, v in competing.items() if len(v) > 1}
        if contested:
            print(f"\ncorroboration: {len(contested)} single-valued relation(s) with competing values")
            for (subject, relation), objs in sorted(contested.items()):
                detail = "; ".join(f"{obj} ({src} src)" for obj, src in sorted(objs.items()))
                print(f"  {subject} / {relation}: {detail}")
    return 0


if __name__ == "__main__":
    from common import run_cli

    sys.exit(run_cli(main))
