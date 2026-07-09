#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Coverage critic: which sources has the KB actually extracted facts from?

A plain notes wiki cannot tell you what it failed to capture. This reports, per
source file under sources/ and runs/sources/, how many *engine-input* facts
(status in {confirmed, accepted}) cite it, and flags the gaps:
  - a TEXT source with 0 facts      -> an extraction gap (run /factlog sync)
  - a BINARY source under sources/  -> needs conversion first (factlog ingest)
  - a BINARY source under runs/sources/ -> anomaly: ingest output should be text
A binary original is paired with its runs/sources/<stem> conversion: facts
attach to the conversion, so a binary whose conversion carries facts is
"covered via conversion" (NOT a binary gap). A binary is only flagged as needing
conversion when it has no conversion at all.
It also surfaces orphan citations: a fact citing a source file that no longer
exists on disk (a stale/typo'd reference).

Counts use engine facts only: a source backed solely by superseded or
needs_review rows contributes nothing to accepted.dl, so it is correctly a gap.

Always exits 0 by default (informational, never blocks the pipeline). With
--strict, exit non-zero when any TEXT source is uncovered, so automation can
surface silent extraction gaps.

Usage:
    python3 coverage.py [--wiki <kb>] [--strict]
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
    CANDIDATES_CSV,
    ensure_dirs,
    engine_facts,
    is_sync_ignored,
    is_text_source,
    load_facts,
    paired_conversion,
    source_files,
    source_rel_key,
    sync_ignore_patterns,
)


def coverage_rows(root: Path, facts: list[dict[str, str]]) -> tuple[list[dict[str, object]], list[str]]:
    """Return (per-source rows, orphan citations).

    Each row: {file, dir, text, facts, ignored, conversion, conv_facts} where
    facts is how many engine-input rows cite it (source path before any '#') and
    ignored marks a source excluded by policy/sync-ignore.md. For a binary
    original under sources/, conversion is its runs/sources/<stem> text
    conversion (if any) and conv_facts how many facts cite that conversion — so a
    binary whose conversion carries facts is "covered via conversion", not a gap
    (facts attach to the conversion, never to the binary original). Orphans are
    cited paths with no file on disk.
    """
    # NFC-normalise both sides: macOS stores filenames as NFD but candidate
    # sources are NFC, so an un-normalised compare would mis-report a Korean-named
    # source as 0-facts + orphan (see merge_candidates' matching).
    cited: dict[str, int] = {}
    for row in engine_facts(facts):
        ref = unicodedata.normalize("NFC", row.get("source", "").partition("#")[0])
        if ref:
            cited[ref] = cited.get(ref, 0) + 1

    patterns = sync_ignore_patterns()
    rows: list[dict[str, object]] = []
    on_disk: set[str] = set()
    for path in source_files(root):
        # source_files() already drops hidden paths (any dot-prefixed component
        # under the source root), so every enumerator shares one rule (#67).
        ref = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
        on_disk.add(ref)
        rows.append({
            "file": ref,
            "dir": "runs/sources" if ref.startswith("runs/sources/") else "sources",
            "text": is_text_source(path),
            "facts": cited.get(ref, 0),
            "ignored": is_sync_ignored(ref, patterns),
            "conversion": "",
            "conv_facts": 0,
        })

    # Pair a binary original under sources/ with its runs/sources/<rel>
    # conversion using the subdir-aware rel key (ingest mirrors the original's
    # subtree), so sources/a/x.pdf pairs with runs/sources/a/x.md and a same-stem
    # file in another subtree does NOT mispair. Only *text* conversions count —
    # a stray binary under runs/sources/ is an anomaly, not a usable conversion,
    # so it must not mask a real "needs conversion" gap on the original.
    conv_by_key: dict[str, str] = {}
    conv_rows: dict[str, dict[str, object]] = {}
    for r in rows:
        if r["dir"] == "runs/sources" and r["text"]:
            cref = str(r["file"])
            conv_by_key.setdefault(source_rel_key(cref), cref)
            conv_rows[cref] = r
    for r in rows:
        if r["dir"] == "sources" and not r["text"]:
            # Match on the full-name key (#213), with a provenance-verified legacy
            # stem-key fallback (paired_conversion) so a pre-#213 conversion still
            # pairs — but never mispairs a same-stem/different-extension sibling.
            cref = paired_conversion(str(r["file"]), conv_by_key, lambda ref: root / ref)
            if cref is not None:
                conv = conv_rows[cref]
                r["conversion"] = conv["file"]
                r["conv_facts"] = conv["facts"]

    orphans = sorted(set(cited) - on_disk)
    return rows, orphans


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report source coverage (extraction gaps).")
    parser.add_argument("--wiki", default=os.environ.get("FACTLOG_ROOT", "."), help="KB root")
    parser.add_argument("--strict", action="store_true", help="exit non-zero if any text source has 0 facts")
    args = parser.parse_args(argv)

    ensure_dirs()
    facts = load_facts() if CANDIDATES_CSV.is_file() else []
    rows, orphans = coverage_rows(Path(os.environ["FACTLOG_ROOT"]), facts)

    if not rows:
        print("coverage: no source files")
        if orphans:
            for ref in orphans:
                print(f"  ORPHAN citation (source file missing): {ref}", file=sys.stderr)
        return 0

    # A binary original is "covered via conversion" when its runs/sources/<stem>
    # conversion carries facts (facts attach to the conversion, not the binary).
    covered_direct = [r for r in rows if r["facts"]]
    covered_via_conv = [r for r in rows if not r["facts"] and r["conv_facts"]]
    excluded = [r for r in rows if r["ignored"]]
    # Sources on the sync-ignore list are never gaps: they're excluded on purpose.
    text_gaps = [r for r in rows if not r["facts"] and r["text"] and not r["ignored"]]
    # A binary original with ANY conversion has been ingested — not a "needs
    # conversion" gap (if its conversion has 0 facts, that surfaces as the
    # conversion's own text gap). Only an unconverted binary is a binary gap.
    binary_gaps = [
        r for r in rows
        if not r["facts"] and not r["text"] and not r["ignored"] and not r["conversion"]
    ]
    n_covered = len(covered_direct) + len(covered_via_conv)
    via_note = f" ({len(covered_via_conv)} via conversion)" if covered_via_conv else ""
    excluded_note = f", {len(excluded)} excluded (sync-ignored)" if excluded else ""
    print(
        f"coverage: {len(rows)} source(s); {n_covered} covered{via_note}, "
        f"{len(text_gaps)} text gap(s), {len(binary_gaps)} binary needing conversion, "
        f"{len(orphans)} orphan citation(s){excluded_note}"
    )
    for r in rows:
        if r["ignored"]:
            tag = "  [excluded]"
        elif not r["facts"] and r["conv_facts"]:
            tag = f"  [covered via {r['conversion']}: {r['conv_facts']} fact(s)]"
        elif not r["facts"] and r["conversion"]:
            tag = f"  [converted → {r['conversion']} (0 facts — re-run /factlog sync)]"
        else:
            tag = ""
        print(f"  {r['facts']} fact(s): {r['file']}{tag}")
    for r in text_gaps:
        print(f"  GAP (text, run /factlog sync): {r['file']}", file=sys.stderr)
    for r in binary_gaps:
        if r["dir"] == "runs/sources":
            print(f"  GAP (binary under runs/sources — ingest output should be text): {r['file']}", file=sys.stderr)
        else:
            print(f"  GAP (binary, run factlog ingest): {r['file']}", file=sys.stderr)
    for ref in orphans:
        print(f"  ORPHAN citation (source file missing): {ref}", file=sys.stderr)

    if args.strict and text_gaps:
        print(f"--strict: {len(text_gaps)} text source(s) with no extracted facts", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    from common import run_cli

    sys.exit(run_cli(main))
