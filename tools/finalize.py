#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""One-shot deterministic finalize for `/factlog add`.

After the in-session extraction step writes runs/*.json, this chains the
deterministic engine steps into a single command so capturing knowledge is
low-friction:

    merge_candidates  ->  ensure policy/logic-policy.dl  ->  compile_facts  ->  run_logic_check

It is read-through to the bundled scripts (no logic duplicated here) and prints a
concise summary. The logic check needs pyrewire>=1.0.3; when that is absent the
check is skipped with a clear note (facts are still merged and compiled) so the
command degrades gracefully rather than hard-failing.

Usage:
    python3 finalize.py [--target <kb>]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from common import logic_policy_md_has_rules

_TOOLS = Path(__file__).parent


def _run(script: str, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_TOOLS / script), *args],
        env=env,
        capture_output=True,
        text=True,
    )


def _pyrewire_ok() -> bool:
    try:
        import pyrewire  # type: ignore

        # Robust parse (matches common.version_tuple): tolerate pre-release tags
        # like '1.0.1rc1' rather than treating them as absent.
        parts = re.findall(r"\d+", str(pyrewire.__version__))[:3]
        return tuple(int(part) for part in parts) >= (1, 0, 3)
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    # Windows console defaults to the legacy code page (cp949); force UTF-8 so
    # Korean output isn't mangled. No-op elsewhere. Files are always UTF-8.
    if sys.platform == "win32":
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(encoding="utf-8")
            except (AttributeError, ValueError, OSError):
                pass
    parser = argparse.ArgumentParser(prog="finalize", description="deterministic /factlog add finalize chain")
    parser.add_argument("--target", default=os.environ.get("FACTLOG_ROOT", "."), help="KB root")
    args = parser.parse_args(argv)

    root = Path(args.target).expanduser().resolve()
    if not (root / "sources").is_dir():
        print(f"finalize: {root} is not a factlog KB (no sources/).", file=sys.stderr)
        return 1
    env = {**os.environ, "FACTLOG_ROOT": str(root)}

    # 1. merge candidate rows (runs/*.json) into candidates.csv + pages + decisions
    merge = _run("merge_candidates.py", "--wiki", str(root), env=env)
    sys.stdout.write(merge.stdout)
    if merge.returncode != 0:
        sys.stderr.write(merge.stderr)
        print("finalize: merge_candidates failed.", file=sys.stderr)
        return 1

    # 2. ensure a loadable policy/logic-policy.dl exists (run_logic_check requires it).
    #    Generate from policy/logic-policy.md when it has compilable rules; otherwise
    #    write a no-op stub so the check can run with an empty policy.
    policy_dl = root / "policy" / "logic-policy.dl"
    if not policy_dl.is_file():
        gen = _run("generate_logic_policy.py", env=env)
        if not policy_dl.is_file():
            # generate produced nothing. Distinguish "no compilable rules" (the
            # benign fresh-KB case → stub) from a genuine generation failure when
            # the .md DOES define rules (do not silently drop the user's policy).
            policy_md = root / "policy" / "logic-policy.md"
            # Shared has-rules definition (factlog/common.py) so finalize and
            # _load_logic_policy_from never drift on what "defines rules" means (#190).
            if logic_policy_md_has_rules(policy_md):
                sys.stderr.write(gen.stderr)
                print(
                    "finalize: WARNING — policy/logic-policy.md appears to define rules but "
                    "generate_logic_policy did not produce logic-policy.dl. Proceeding with an "
                    "empty policy; fix the policy and re-run to apply it.",
                    file=sys.stderr,
                )
            policy_dl.parent.mkdir(parents=True, exist_ok=True)
            policy_dl.write_text("// no policy rules\n", encoding="utf-8")

    # 3. compile confirmed/accepted facts -> facts/accepted.dl
    compile_proc = _run("compile_facts.py", env=env)
    sys.stdout.write(compile_proc.stdout)
    if compile_proc.returncode != 0:
        sys.stderr.write(compile_proc.stderr)
        print("finalize: compile_facts failed.", file=sys.stderr)
        return 1

    # 4. detect single-valued contradictions (deterministic; no pyrewire needed)
    conflicts = _run("check_conflicts.py", "--wiki", str(root), env=env)
    sys.stdout.write(conflicts.stdout)
    conflict_found = conflicts.returncode != 0
    if conflict_found:
        sys.stderr.write(conflicts.stderr)

    # 5. run the deterministic logic check (needs pyrewire) — graceful skip if absent
    if _pyrewire_ok():
        check = _run("run_logic_check.py", env=env)
        sys.stdout.write(check.stdout)
        if check.returncode != 0:
            sys.stderr.write(check.stderr)
            print("finalize: run_logic_check failed.", file=sys.stderr)
            return 1
        checked = "logic-checked"
    else:
        print(
            "\nfinalize: Logic check SKIPPED — pyrewire>=1.0.3 not installed. "
            "Install it and run /factlog check to verify."
        )
        checked = "compiled (logic check skipped)"

    if conflict_found:
        print(
            f"\nfinalize: merged and {checked}, but CONTRADICTIONS were found "
            "(see CONFLICT lines above). Resolve them (mark outdated rows "
            "status='superseded') before trusting the KB.",
            file=sys.stderr,
        )
        return 1
    print(f"\nfinalize: done — merged, {checked}, no contradictions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
