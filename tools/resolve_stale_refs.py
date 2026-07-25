#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Remove stale source references from wiki pages after human review.

Usage:
    python3 resolve_stale_refs.py [--target <kb>] [--apply]

--target ("--wiki" is an accepted alias) overrides $FACTLOG_ROOT, which overrides
the active-KB config, which overrides cwd.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Ensure tools/ is importable when this is run directly as a script.
_TOOLS_DIR = Path(__file__).parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import factlog_config  # noqa: E402

# --target is the canonical spelling across the toolchain; --wiki stays as an alias
# because that is the only spelling this script ever accepted (#533). One tuple, so
# the flag set can never differ between the declaration and the resolution.
_ROOT_FLAGS = ("--target", "--wiki")

# Unlike the sibling engine tools this needs no import-time pre-pass: nothing here
# reads common's ROOT-bound path globals — every helper takes *root* as a parameter —
# so the root can be resolved inside main() from its own parsed arguments, the way
# tools/finalize.py and tools/validate.py do.


# The source group accepts the optional runs/ prefix because validate.py records
# refs as (?:runs/)?sources/...; without it, a removed runs/sources/ conversion's
# stale record could never be matched and would linger forever.
STALE_RE = re.compile(r"^- stale_source: (?P<page>pages/\S+) references removed source (?P<source>(?:runs/)?sources/\S+)$")


@dataclass(frozen=True)
class StaleRef:
    page: str
    source: str


def load_stale_refs(root: Path) -> list[StaleRef]:
    decisions = root / "decisions" / "open-questions.md"
    if not decisions.is_file():
        raise SystemExit("missing decisions/open-questions.md")
    refs: list[StaleRef] = []
    for line in decisions.read_text(encoding="utf-8").splitlines():
        match = STALE_RE.match(line.strip())
        if match:
            refs.append(StaleRef(match.group("page"), match.group("source")))
    return refs


def remove_source_ref(text: str, source_ref: str) -> tuple[str, int]:
    patterns = [
        rf"\s*\({re.escape(source_ref)}\)",
        rf"\s*\[{re.escape(source_ref)}\]",
        rf"\s*`{re.escape(source_ref)}`",
        re.escape(source_ref),
    ]
    changed = 0
    updated = text
    for pattern in patterns:
        updated, count = re.subn(pattern, "", updated)
        changed += count
    return updated, changed


def main() -> int:
    # Windows console defaults to the legacy code page (cp949); force UTF-8 so
    # Korean output isn't mangled. No-op elsewhere. Files are always UTF-8.
    if sys.platform == "win32":
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(encoding="utf-8")
            except (AttributeError, ValueError, OSError):
                pass
    parser = argparse.ArgumentParser(description="Remove stale source references listed in decisions/open-questions.md.")
    # No argparse `default=`: the default is not a constant but a resolution over
    # env → config → cwd. The old `default="."` skipped the $FACTLOG_ROOT and
    # active-KB tiers entirely, so this was the one tool in the set that ignored the
    # KB every other command was already targeting (#533) and the one whose help text
    # was right only because it did (#531).
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
    parser.add_argument("--apply", action="store_true", help="write changes; default only prints what would change")
    args = parser.parse_args()

    # An empty flag value is refused rather than dropped to the next tier (#546):
    # `--target "$FACTLOG_ROOT"` in a shell that never exported the variable is
    # exactly this shape, and falling through would edit pages/ in the configured KB
    # while the caller believes they named one.
    if args.target is not None and not args.target.strip():
        print(
            "resolve_stale_refs: the KB-root flag (--target/--wiki) was empty; pass a KB path, "
            "or pass no flag at all to use the active KB.",
            file=sys.stderr,
        )
        return 1

    root = Path(factlog_config.resolve_root(args.target)[0])
    refs = load_stale_refs(root)
    if not refs:
        print("no stale_source records found")
        return 0

    total_changes = 0
    for ref in refs:
        page = root / ref.page
        if not page.is_file():
            print(f"skip missing page: {ref.page}")
            continue
        text = page.read_text(encoding="utf-8")
        updated, count = remove_source_ref(text, ref.source)
        if count == 0:
            print(f"already clean: {ref.page} does not contain {ref.source}")
            continue
        total_changes += count
        action = "remove" if args.apply else "would remove"
        print(f"{action}: {ref.source} from {ref.page} ({count} occurrence(s))")
        if args.apply:
            page.write_text(updated, encoding="utf-8")

    if not args.apply:
        print("dry run only; rerun with --apply after reviewing the page diff")
    else:
        print(f"updated pages: {total_changes} stale reference occurrence(s) removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
