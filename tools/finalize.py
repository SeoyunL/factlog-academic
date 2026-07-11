#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""One-shot deterministic finalize for `/factlog add`.

After the in-session extraction step writes runs/*.json, this chains the
deterministic engine steps into a single command so capturing knowledge is
low-friction:

    merge_candidates  ->  ensure policy/logic-policy.dl  ->  check_conflicts
        ->  compile_facts  ->  run_logic_check

The single-valued contradiction gate (check_conflicts) runs BEFORE compile_facts so
a detected contradiction never reaches facts/accepted.dl, the engine's trusted input
that ask/check read directly without recompiling (#212).

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

# Exact content of the empty-policy stub finalize writes for a benign no-rules KB.
# Matched byte-for-byte to recognise (and self-heal) a stub left by a pre-#194
# finalize that wrote it OVER an uncompilable policy.
POLICY_STUB = "// no policy rules\n"


# Defensive upper bound so a wedged child (e.g. an engine call that never returns)
# can't hang finalize forever. Generous — the deterministic steps finish in
# well under a second on real KBs; this only trips on a genuine hang.
_RUN_TIMEOUT_SEC = 300


def _run(script: str, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [sys.executable, str(_TOOLS / script), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=_RUN_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        # Surface a timeout as an ordinary non-zero result so every caller handles
        # it exactly like any other failure (rc != 0), rather than raising through
        # finalize. Preserve whatever output was captured before the timeout.
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + (
            f"\n{script}: timed out after {_RUN_TIMEOUT_SEC}s\n"
        )
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return subprocess.CompletedProcess(exc.cmd, returncode=124, stdout=stdout, stderr=stderr)


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
    policy_md = root / "policy" / "logic-policy.md"
    # Shared has-rules definition (factlog/common.py) so finalize and
    # _load_logic_policy_from never drift on what "defines rules" means (#190).
    policy_uncompiled = False  # md defines rules but nothing compiled → NOT applied
    # Self-heal a KB poisoned by a pre-#194 finalize: that version wrote the empty
    # stub OVER an uncompilable policy, and the leftover stub then (a) made every
    # later run skip regeneration (the `not policy_dl.is_file()` guard below) and
    # (b) fooled /factlog check (#190 keys on the .dl being ABSENT). If the .dl is
    # exactly that stub yet the .md defines rules, drop it so this run regenerates
    # (and, if generation still fails, leaves it absent to fail loud) instead of
    # inheriting the silent-ignore. A benign stub (no rules in .md) is left alone.
    if (
        policy_dl.is_file()
        and policy_dl.read_text(encoding="utf-8") == POLICY_STUB
        and logic_policy_md_has_rules(policy_md)
    ):
        policy_dl.unlink()
    # #217: a real (non-stub) .dl that already exists must NOT be trusted blindly.
    # The old `if not policy_dl.is_file(): generate` guard skipped regeneration
    # whenever a .dl was present, so edits to logic-policy.md after the first
    # finalize were silently ignored and the engine kept applying the OLD compiled
    # rules (stale policy). When the .md defines rules, reuse the compiler's own
    # byte-identity verification (generate_logic_policy.py --check) to detect drift
    # between the .md rules and the compiled .dl; on drift the .dl is stale and we
    # fall through to regenerate so the current rules are actually applied. In sync
    # → --check exits 0 and we leave everything untouched (deterministic, idempotent,
    # no output). When the .md has NO rules but a real compiled .dl is still on disk,
    # --check is skipped (it would hard-error on a ruleless .md) so the symmetric
    # rules→empty reset below handles that case instead. Only the generated
    # logic-policy.dl is inspected here; hand-authored logic-policy.extra.dl is a
    # separate file and is never touched.
    stale_dl = False
    if policy_dl.is_file() and logic_policy_md_has_rules(policy_md):
        check = _run("generate_logic_policy.py", "--check", env=env)
        stale_dl = check.returncode != 0
    elif (
        policy_dl.is_file()
        and not logic_policy_md_has_rules(policy_md)
        and policy_dl.read_text(encoding="utf-8") != POLICY_STUB
    ):
        # #217 (symmetric transition rules→empty): the .md previously had rules that
        # compiled into a real .dl, but the user has since REMOVED all rules.
        # has_rules is now False so the stale-check above is skipped, yet the real
        # compiled .dl (old rules, e.g. requires_review(X,"c1") :- relation(X,"uses",_))
        # is still on disk and the engine keeps applying the OLD policy — the same
        # silent stale-apply this issue removes, just in the rules→empty direction.
        # Reset the .dl to the empty-policy stub so it matches the now-ruleless .md.
        # A benign stub is already POLICY_STUB so this never fires for it (no-op),
        # and the #194 stub-over-rules self-heal above ran first, so a stub is never
        # left masking real rules.
        policy_dl.write_text(POLICY_STUB, encoding="utf-8")
        print(
            "finalize: policy/logic-policy.dl was stale; reset to empty policy "
            "(logic-policy.md defines no rules)."
        )
    if stale_dl or not policy_dl.is_file():
        gen = _run("generate_logic_policy.py", env=env)
        if stale_dl and policy_dl.is_file() and gen.returncode != 0:
            # The .dl drifted from the .md but regeneration also failed (e.g. the
            # edited .md no longer compiles). Do NOT keep applying the stale .dl
            # silently — remove it so this run surfaces the uncompiled state the same
            # way the absent-.dl path does below (#194 invariant): run_logic_check
            # fails loud with pyrewire, and /factlog check's loud detection (#190)
            # keys on the .dl being ABSENT.
            policy_dl.unlink(missing_ok=True)
        if not policy_dl.is_file():
            # generate produced nothing. Distinguish "no compilable rules" (the
            # benign fresh-KB case → stub) from a genuine generation failure when
            # the .md DOES define rules (do not silently drop the user's policy).
            if logic_policy_md_has_rules(policy_md):
                # The policy defines rules but did NOT compile. Deliberately do
                # NOT write a stub here (#194): a "// no policy rules" .dl would
                # (a) satisfy the `not policy_dl.is_file()` guard above so the NEXT
                # finalize skips regeneration — permanently ignoring the policy —
                # and (b) mask the uncompiled state from /factlog check, whose loud
                # detection (#190) keys on the .dl being ABSENT. Leaving it absent
                # means every re-run retries generation and re-warns, and
                # run_logic_check below still fails loud via _load_logic_policy_from.
                policy_uncompiled = True
                sys.stderr.write(gen.stderr)
                print(
                    "finalize: WARNING — policy/logic-policy.md defines rules but "
                    "generate_logic_policy did not produce logic-policy.dl (see the "
                    "error above), so the policy is NOT applied. Fix the policy and "
                    "re-run — no empty-policy stub was written, so re-running retries "
                    "generation.",
                    file=sys.stderr,
                )
            else:
                # Benign: no compilable rules → a no-op stub lets the check run
                # with an empty policy (fresh-KB case).
                policy_dl.parent.mkdir(parents=True, exist_ok=True)
                policy_dl.write_text(POLICY_STUB, encoding="utf-8")
        elif stale_dl:
            # Stale .dl was regenerated from the current .md rules. Surface this on
            # stdout (only in the drift path — the in-sync path stays silent) so the
            # behaviour change is visible rather than a silent recompile.
            print("finalize: policy/logic-policy.dl was stale; regenerated from logic-policy.md.")

    # 3. detect single-valued contradictions BEFORE compiling (deterministic; no
    #    pyrewire needed). #212: the pre-fix order compiled facts/accepted.dl FIRST
    #    (step 3) and only then checked for conflicts (step 4), so when a
    #    contradiction was found finalize returned 1 but left the two contradictory
    #    facts sitting in accepted.dl — the engine's trusted input file, which
    #    ask_router (and /factlog check) read directly from disk WITHOUT recompiling.
    #    A failed finalize therefore silently poisoned the KB: the very next
    #    `factlog ask` could answer from contradictory facts, defeating factlog's
    #    deterministic contradiction gate. check_conflicts reads ONLY
    #    facts/candidates.csv (never accepted.dl), so gating here — before any
    #    compile — is correct and means contradictory facts never enter the engine
    #    input in the first place (option (a)).
    conflicts = _run("check_conflicts.py", "--wiki", str(root), env=env)
    sys.stdout.write(conflicts.stdout)
    if conflicts.returncode != 0:
        sys.stderr.write(conflicts.stderr)
        # Defensive heal (option (c)): a KB poisoned by a PRE-FIX finalize can still
        # have a facts/accepted.dl on disk holding the contradictory pair. Gating
        # before compile prevents NEW pollution, but it would leave that stale
        # poisoned file untouched — so a downstream reader could keep answering from
        # the contradiction. Since we are refusing to (re)compile while the
        # contradiction stands, remove accepted.dl so no reader can trust it. On a
        # clean-history KB it is either absent or a prior consistent snapshot;
        # removing it here is the fail-safe the message already implies ("resolve
        # them before trusting the KB"). This makes the invariant unconditional:
        # after a conflict-failing finalize, accepted.dl never contains the
        # contradictory facts.
        accepted_dl = root / "facts" / "accepted.dl"
        removed = accepted_dl.is_file()
        try:
            accepted_dl.unlink(missing_ok=True)
        except OSError as exc:  # never crash finalize on a cleanup failure
            print(f"finalize: could not remove facts/accepted.dl ({exc}).", file=sys.stderr)
            removed = False
        print(
            "\nfinalize: CONTRADICTIONS were found (see CONFLICT lines above); "
            "facts were NOT compiled to facts/accepted.dl"
            + (
                " and the existing facts/accepted.dl was removed, so /factlog ask "
                "returns nothing until the conflict is resolved"
                if removed
                else ""
            )
            + ". Resolve them through the human gate — factlog eject --fact SUBJECT "
            "RELATION OBJECT to retire a row, or factlog amend ... --set-object to "
            "correct one — not by hand-editing facts/candidates.csv. If the values are "
            "a supertype and its subtype, neither is wrong: declare the relationship in "
            "policy/value-hierarchy.md and both rows are kept. Then re-run before "
            "trusting the KB.",
            file=sys.stderr,
        )
        return 1

    # 4. compile confirmed/accepted facts -> facts/accepted.dl (only when consistent)
    compile_proc = _run("compile_facts.py", env=env)
    sys.stdout.write(compile_proc.stdout)
    if compile_proc.returncode != 0:
        sys.stderr.write(compile_proc.stderr)
        print("finalize: compile_facts failed.", file=sys.stderr)
        return 1

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

    if policy_uncompiled:
        # Reached only when the logic check was skipped (no pyrewire); with the
        # engine present, run_logic_check above already failed loud on the absent
        # .dl. Keep the summary honest — facts are compiled but the policy is not.
        print(
            f"\nfinalize: done — merged, {checked}, no contradictions, "
            "but the policy is NOT applied (see the WARNING above). Install "
            "pyrewire and run /factlog check to gate on the policy."
        )
        return 0
    print(f"\nfinalize: done — merged, {checked}, no contradictions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
