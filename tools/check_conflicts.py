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
    python3 check_conflicts.py [--target <kb>]

--target ("--wiki" is an accepted alias) overrides $FACTLOG_ROOT, which overrides
the active-KB config, which overrides cwd.
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
    detect_conflicts,
    ensure_dirs,
    load_facts,
    relation_aliases,
    single_valued_relations,
    typed_relations,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect single-valued-relation contradictions.")
    # The root flag was already consumed by the pre-pass above; declaring it again
    # here is what puts it in --help and what makes a misspelled `--targt /path`
    # exit 2 instead of being silently ignored and checking whatever the
    # config/cwd tier resolved to. No argparse `default=`: the default is not a
    # constant but a resolution over env → config → cwd, and the old
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
            "check_conflicts: the KB-root flag (--target/--wiki) was empty; pass a KB path, "
            "or pass no flag at all to use the active KB.",
            file=sys.stderr,
        )
        return 1

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
