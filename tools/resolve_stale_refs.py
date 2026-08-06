#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Remove stale source references from wiki pages after human review.

Usage:
    python3 resolve_stale_refs.py [--wiki <kb>] [--apply]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


# Resolve the KB root once, up front, so --wiki's default is the shared
# resolver's answer ($FACTLOG_ROOT > active-KB config > cwd) rather than the
# literal "." this script used to hard-code (#330). This module imports no
# path-binding helpers, so the export is for the --wiki default alone.
import factlog_config  # noqa: E402

os.environ["FACTLOG_ROOT"] = factlog_config.resolve_root_from_argv("--wiki")


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
    parser.add_argument(
        "--wiki",
        default=os.environ.get("FACTLOG_ROOT", "."),
        help="KB root, default: $FACTLOG_ROOT, else the active KB, else the current directory",
    )
    parser.add_argument("--apply", action="store_true", help="write changes; default only prints what would change")
    args = parser.parse_args()

    root = Path(args.wiki).expanduser().resolve()
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
