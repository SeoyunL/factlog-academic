#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Detect contradictions among engine-input facts.

A relation declared *single-valued* (functional) in policy/single-valued.md may
hold at most one object per subject. If two distinct objects are asserted for the
same (subject, relation) among engine-input facts (status confirmed/accepted;
'superseded' rows are ignored), that is a contradiction — the kind of silent rot
a plain notes wiki accumulates. This surfaces it deterministically.

Resolution is human-in-the-loop and non-destructive, through the gate rather than by
hand-editing facts/candidates.csv: `factlog eject --fact SUBJECT RELATION OBJECT`
retires a row (it stays for audit, drops out of engine input, and the conflict clears)
and `factlog amend ... --set-object` corrects one. If the two values are a supertype
and its subtype, neither is wrong -- declare the relationship in
policy/value-hierarchy.md and both rows are kept.

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

from common import (  # noqa: E402
    detect_conflicts,
    ensure_dirs,
    load_facts,
    relation_aliases,
    single_valued_relations,
    typed_relations,
)


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
