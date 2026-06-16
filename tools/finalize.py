#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""One-shot deterministic finalize for `/factlog add`.

After the in-session extraction step writes runs/*.json, this chains the
deterministic engine steps into a single command so capturing knowledge is
low-friction:

    merge_candidates  ->  ensure policy/logic-policy.dl  ->  compile_facts  ->  run_logic_check

It is read-through to the bundled scripts (no logic duplicated here) and prints a
concise summary. The logic check needs pyrewire>=1.0.1; when that is absent the
check is skipped with a clear note (facts are still merged and compiled) so the
command degrades gracefully rather than hard-failing.

Usage:
    python3 finalize.py [--target <kb>]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

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

        version = tuple(int(part) for part in str(pyrewire.__version__).split(".")[:3])
        return version >= (1, 0, 1)
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
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
        _run("generate_logic_policy.py", env=env)
        if not policy_dl.is_file():
            policy_dl.parent.mkdir(parents=True, exist_ok=True)
            policy_dl.write_text("// no policy rules\n", encoding="utf-8")

    # 3. compile confirmed/accepted facts -> facts/accepted.dl
    compile_proc = _run("compile_facts.py", env=env)
    sys.stdout.write(compile_proc.stdout)
    if compile_proc.returncode != 0:
        sys.stderr.write(compile_proc.stderr)
        print("finalize: compile_facts failed.", file=sys.stderr)
        return 1

    # 4. run the deterministic logic check (needs pyrewire) — graceful skip if absent
    if _pyrewire_ok():
        check = _run("run_logic_check.py", env=env)
        sys.stdout.write(check.stdout)
        if check.returncode != 0:
            sys.stderr.write(check.stderr)
            print("finalize: run_logic_check failed.", file=sys.stderr)
            return 1
        print("\nfinalize: done — merged, compiled, and logic-checked.")
    else:
        print(
            "\nfinalize: facts merged and compiled. Logic check SKIPPED — pyrewire>=1.0.1 "
            "not installed. Install it and run /factlog check to verify."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
