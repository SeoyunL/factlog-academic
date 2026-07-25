#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Report multi-source corroboration for engine-input facts.

For each accepted fact, how many DISTINCT sources back it (a trust signal a plain
notes wiki cannot give); and, for single-valued relations, the competing values
with their per-source support — the source-level view of a contradiction.

Informational: always exits 0.

Usage:
    python3 corroboration.py [--target <kb>]

--target ("--wiki" is an accepted alias) overrides $FACTLOG_ROOT, which overrides
the active-KB config, which overrides cwd.
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

# --target is the canonical spelling across the toolchain (the `factlog` CLI, the
# engine steps and finalize all take it); --wiki stays as an alias because SKILL.md
# and tests/*.sh spell it that way (#533). ONE tuple feeds BOTH the import-time
# pre-pass below and main()'s strict parser: a spelling only one of the two knew
# would be either read-but-unadvertised or accepted-but-ignored, and an ignored KB
# flag silently retargets the run at whatever the config/cwd tier resolved to.
_ROOT_FLAGS = ("--target", "--wiki")


def _peek_root_flag(argv: list[str] | None = None) -> str | None:
    """The KB root given on the command line, or None.

    ``parse_known_args`` because this runs at import time, before main()'s real
    parser exists: the peek must not reject an argument it is not responsible for.
    Rejecting a typo is main()'s job, once, through its own strict parse.
    """
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument(*_ROOT_FLAGS, dest="target", default=None)
    known, _ = pre.parse_known_args(sys.argv[1:] if argv is None else argv)
    return known.target


os.environ["FACTLOG_ROOT"] = factlog_config.resolve_root(_peek_root_flag())[0]

from common import (  # noqa: E402
    corroboration_counts,
    engine_facts,
    ensure_dirs,
    load_facts,
    single_valued_relations,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report multi-source corroboration of facts.")
    # The root flag was already consumed by the pre-pass above; declaring it again
    # here is what puts it in --help and what makes a misspelled `--targt /path`
    # exit 2 instead of being silently ignored and reading whatever the config/cwd
    # tier resolved to. No argparse `default=`: the default is not a constant but a
    # resolution over env -> config -> cwd, and the old
    # `os.environ.get("FACTLOG_ROOT", ".")` described a rule this tool has not
    # followed since the pre-pass landed (#531).
    parser.add_argument(
        *_ROOT_FLAGS,
        dest="target",
        default=None,
        metavar="PATH",
        help=(
            "KB root (--wiki is an alias). Overrides $FACTLOG_ROOT and the "
            "active-KB config; without it the root is resolved as "
            "$FACTLOG_ROOT > active-KB config > cwd."
        ),
    )
    args = parser.parse_args(argv)
    # An empty flag value is refused rather than dropped to the next tier (#546):
    # `--target "$FACTLOG_ROOT"` in a shell that never exported the variable is
    # exactly this shape, and falling through would target the configured KB while
    # the caller believes they named one.
    if args.target is not None and not args.target.strip():
        print(
            "corroboration: the KB-root flag (--target/--wiki) was empty; pass a KB path, "
            "or pass no flag at all to use the active KB.",
            file=sys.stderr,
        )
        return 1

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
        # (min), matching how the relation is handled above (#295). The subject axis
        # folds the same way (#310), so a value backed under one subject spelled two
        # ways is one row of the contest, not two.
        sources_by: dict[tuple[str, str], dict[str, set[str]]] = {}
        raw_rels: dict[tuple[str, str], set[str]] = {}
        raw_subjects: dict[tuple[str, str], set[str]] = {}
        raw_objs: dict[tuple[tuple[str, str], str], set[str]] = {}
        for row in engine_facts(facts):
            # single_valued is loaded NFC-normalized; the fact relation may be NFD.
            # Fold the membership probe so an NFD-authored fact still competes (#293).
            if unicodedata.normalize("NFC", row["relation"]) not in single_valued:
                continue
            bucket = (
                unicodedata.normalize("NFC", row["subject"]),
                unicodedata.normalize("NFC", row["relation"]),
            )
            fobj = unicodedata.normalize("NFC", row["object"])
            objs = sources_by.setdefault(bucket, {})
            objs.setdefault(fobj, set()).add(row["source"])
            raw_objs.setdefault((bucket, fobj), set()).add(row["object"])
            raw_rels.setdefault(bucket, set()).add(row["relation"])
            raw_subjects.setdefault(bucket, set()).add(row["subject"])
        contested = {b: objs for b, objs in sources_by.items() if len(objs) > 1}
        if contested:
            print(f"\ncorroboration: {len(contested)} single-valued relation(s) with competing values")
            for bucket, objs in sorted(contested.items()):
                subject = min(raw_subjects[bucket])
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
