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
    python3 finalize.py [--target|--wiki <kb>]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

import factlog_config
from common import (
    EMPTY_POLICY_DL,
    logic_policy_md_has_rejected_items,
    logic_policy_md_has_rules,
)

_TOOLS = Path(__file__).parent

# Exact content of the empty-policy stub finalize writes for a benign no-rules KB.
# Matched byte-for-byte to recognise (and self-heal) a stub left by a pre-#194
# finalize that wrote it OVER an uncompilable policy. Aliased to the shared constant
# (factlog/common.py) since #491: generate_logic_policy now emits the same bytes for a
# ruleless .md, so a literal here could drift from what the compiler writes and turn
# every finalize into a "stale" verdict on its own output.
POLICY_STUB = EMPTY_POLICY_DL


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


def resolve_kb_root(cli_value: str | None) -> tuple[Path, str]:
    """The KB root finalize operates on **and where that choice came from** (#529).

    Precedence is the documented one (factlog/config.py):

        --target/--wiki  >  $FACTLOG_ROOT  >  active-KB config  >  cwd

    The flag default used to be ``os.environ.get("FACTLOG_ROOT", ".")``, which skipped
    the config tier entirely: run outside a KB with neither the flag nor the env var —
    the normal shape for a user who set an active KB once with `factlog use` — finalize
    resolved to cwd and refused with "is not a factlog KB (no sources/)" instead of
    finalizing the KB every other command was already targeting.

    The second answer — 'flag' | 'env' | 'config' | 'cwd' — is returned rather than
    dropped because consulting the config tier is exactly what makes an *unaimed* run
    possible, and only the provenance can tell one apart from an aimed one. It is what
    ``implicit_target_refusal`` decides on and what ``main`` announces; nothing
    downstream can recover it, since every chained step is handed an explicit
    ``FACTLOG_ROOT`` and would report 'env' for a root that came from anywhere.

    Sibling tools (tools/merge_candidates.py and friends) do this as a pre-pass that
    exports FACTLOG_ROOT *before* importing common, because common binds its path
    globals at import time. finalize does not need that: it imports only a constant and
    two path-taking predicates from common, and hands every chained step an explicit
    FACTLOG_ROOT in ``env``. Resolving here instead keeps the root a function of
    ``main``'s argv rather than of module import order. That holds only while finalize
    imports nothing ROOT-bound from common — adding such an import means moving this
    to a pre-pass.
    """
    root, source = factlog_config.resolve_root(cli_value)
    return Path(root), source


def implicit_target_refusal(root: Path, source: str, cwd: Path) -> str | None:
    """Why this run may not finalize *root*, or None if it may (#529/#532).

    ``source`` is ``factlog_config.resolve_root``'s second answer. Only 'config' names a
    target nobody chose — not the command line, not the environment, not the directory
    the caller is standing in. Before the config tier was consulted here, a bare
    ``finalize.py`` outside a KB resolved to cwd and the ``sources/`` gate below turned
    it away; with the tier consulted it instead chains merge_candidates against the
    configured KB, rewriting facts/candidates.csv, pages/ and
    decisions/open-questions.md and recompiling facts/accepted.dl. Measured: a run from
    an unrelated directory rewrote the active KB's candidate table.

    Same criterion as tools/merge_candidates.py's guard for the same accident (#532),
    and finalize needs its own copy rather than inheriting that one: it hands the child
    ``--wiki <root>``, so the child's resolver answers 'flag' and its guard passes. A
    guard one caller can turn off by being explicit on the child's behalf is not a
    guard for that caller — finalize would be the way around #532 rather than a step
    covered by it.

    Announcing the target (main does that too) is not a substitute: merge_candidates
    already printed ``wiki=<root>`` before every write and the incident happened anyway.
    A line on stdout makes an intended write auditable; it does not stop an unintended
    one.

    'flag' and 'env' are aimed by definition. 'cwd' needs no refusal either: the root IS
    the cwd, so the ``sources/`` gate catches a non-KB there exactly as before. And a
    config root the caller is standing inside (or at) is not implicit — running finalize
    from the KB with no flag is the documented workflow.
    """
    if source != "config":
        return None
    if cwd == root or root in cwd.parents:
        return None
    return (
        f"finalize: REFUSING to finalize {root} — that KB comes from the active-KB config, "
        f"not from this command, and the current directory ({cwd}) is not inside it.\n"
        f"  finalize chains merge_candidates, which rewrites facts/candidates.csv, pages/ and "
        f"decisions/open-questions.md, and then recompiles facts/accepted.dl — so a run nobody "
        f"aimed would silently rewrite the configured KB and invalidate its logic report (#532).\n"
        f"  Name the target explicitly:\n"
        f"    python3 tools/finalize.py --target {root}\n"
        f"  or export FACTLOG_ROOT={root}"
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
    # `--wiki` is accepted as an alias of `--target` so the KB-root flag is spelled the
    # same way everywhere in the toolchain (merge_candidates/check_conflicts take
    # `--wiki`, ask_router takes `--target`); both land in the same dest.
    #
    # One dest, so passing BOTH is not an error and not a conflict argparse resolves in
    # any special way: each occurrence overwrites the dest, so the spelling that comes
    # LAST on the command line wins. Said in the help because a caller that builds argv
    # by appending (a wrapper adding `--wiki` after a user's `--target`) otherwise has
    # no way to know which of the two it is actually finalizing.
    #
    # No argparse `default=` here: the default is not a constant but a resolution over
    # env → config → cwd, and computing it in resolve_kb_root keeps the help text and
    # the behaviour describing one rule instead of two (#529).
    parser.add_argument(
        "--target",
        "--wiki",
        dest="target",
        default=None,
        help="KB root (authoritative; sets FACTLOG_ROOT for the chained steps; "
        "default: $FACTLOG_ROOT, then the active KB from `factlog use`, then '.'). "
        "--target and --wiki are one option: given both, the last one wins.",
    )
    parser.add_argument(
        "--allow-unverified",
        action="store_true",
        help="exit 0 even when the engine logic check is skipped (pyrewire>=1.0.3 absent). "
        "By default that skip exits 3 so automation can distinguish an unverified compile "
        "from an engine-verified pass (#336). A policy that did not compile is NOT accepted "
        "by this flag — it always exits non-zero (#356/#496), because the flag tolerates "
        "engine absence, not a KB policy defect. That covers every way generation can fail "
        "to produce logic-policy.dl: rules that would not compile, bullets naming no "
        "backtick relation, and a missing or empty logic-policy.md.",
    )
    args = parser.parse_args(argv)

    root, root_source = resolve_kb_root(args.target)
    # Name the KB about to be finalized and where that choice came from, before anything
    # is read, written or chained — the same line `factlog ingest`/`status` print
    # (cli.py: "target KB {target} (from {source})"). The provenance is the part that
    # tells a reader this run picked the KB up from config rather than from their
    # command.
    print(f"finalize: target KB {root} (from {root_source})")
    if not (root / "sources").is_dir():
        print(f"finalize: {root} is not a factlog KB (no sources/).", file=sys.stderr)
        return 1
    # After the KB gate, not before: a root with no sources/ is refused either way and
    # nothing is written on that path, so the reader gets the more specific diagnosis of
    # the two. Before every write, which is what the guard is for.
    refusal = implicit_target_refusal(root, root_source, Path.cwd().resolve())
    if refusal is not None:
        print(refusal, file=sys.stderr)
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
    policy_uncompiled = False  # md attempts rules but nothing compiled → NOT applied
    # "Every tagged bullet was REJECTED" (an [id] with no backtick relation): has_rules is
    # False, yet generate_logic_policy exits non-zero on exactly this shape rather than
    # compiling an empty policy (#491). Keying the three decisions below on has_rules
    # ALONE therefore read this authoring defect as a ruleless KB and papered it over with
    # the empty-policy stub — silently, with rc 0, and (worst) destroying a real compiled
    # .dl the moment an author dropped the backticks from a working rule (#496). The .md is
    # never written by finalize, so one read-time verdict is used throughout.
    #
    # The `not has_rules` conjunct is a redundant guard, not the thing that protects a
    # MIXED policy: every site below is already inside a has-rules branch or ordered after
    # one, so dropping it leaves the whole suite green (a true equivalent mutant, measured).
    # Re-measured at bb3909c by cutting it down to
    # `rejected_only = logic_policy_md_has_rejected_items(policy_md)`: tests/test_finalize.sh
    # 74 passed / 0 failed and `pytest tests/unit -q` 6288 passed / 1 skipped, both identical
    # to baseline. That command is the whole content of "equivalent mutant" here — if a later
    # change gives the conjunct work to do, that run is what stops reporting a survivor.
    # It is kept because it makes the name honest — "rejected_only" should not be True for
    # a .md that also compiled a rule — and reads as the same verdict generation reaches.
    rejected_only = not logic_policy_md_has_rules(policy_md) and logic_policy_md_has_rejected_items(
        policy_md
    )
    # Self-heal a KB poisoned by a pre-#194 finalize: that version wrote the empty
    # stub OVER an uncompilable policy, and the leftover stub then (a) made every
    # later run skip regeneration (the `not policy_dl.is_file()` guard below) and
    # (b) fooled /factlog check (#190 keys on the .dl being ABSENT). If the .dl is
    # exactly that stub yet the .md defines rules, drop it so this run regenerates
    # (and, if generation still fails, leaves it absent to fail loud) instead of
    # inheriting the silent-ignore. A benign stub (no rules in .md) is left alone.
    #
    # rejected_only is healed too (#496): a pre-fix finalize wrote this same stub over a
    # .md whose every bullet was rejected, and because has_rules is False there the heal
    # never fired — the stub sat forever, the skip guard below kept generation from
    # retrying, and /factlog check saw a present .dl and reported 0 findings. Widening the
    # condition is the migration for KBs already in that state; a genuinely ruleless .md
    # has nothing rejected, so the benign stub is still left alone (#491).
    if (
        policy_dl.is_file()
        and policy_dl.read_text(encoding="utf-8") == POLICY_STUB
        and (logic_policy_md_has_rules(policy_md) or rejected_only)
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
    # --check is skipped and the symmetric rules→empty reset below handles that case
    # instead. That branch predates #491, when --check hard-errored on a ruleless .md;
    # --check now reports exactly this state as stale, so the two agree on the verdict
    # and the local reset is kept only because it names the transition in its message
    # ("reset to empty policy") instead of asking the operator to re-run. Only the generated
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
        if rejected_only:
            # #496: the .md did NOT go ruleless — its bullets are still there, they just
            # stopped naming a relation (the usual cause: the backticks were dropped while
            # editing a working rule). Resetting here destroyed the compiled .dl and said
            # "logic-policy.md defines no rules", which is false, at rc 0. Remove the .dl
            # instead — it no longer matches the .md either way — and fall through to the
            # regeneration path, where generate fails loud and the uncompiled state is
            # reported (and, with the engine present, run_logic_check refuses to pass).
            policy_dl.unlink()
        else:
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
            # generate produced nothing. Distinguish "no compilable rules" (→ stub) from
            # a genuine generation failure when the .md DOES define rules (do not
            # silently drop the user's policy).
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
            elif rejected_only:
                # #496: the .md whose every bullet was REJECTED used to land in the stub
                # branch below, because has_rules is False for it. That turned generate's
                # own fatal verdict ("no compilable policies", rc 1) into a stub .dl and
                # rc 0 — the policy was dropped in silence, and the stub then masked the
                # state from /factlog check and from every later finalize (the .dl is
                # present, so nothing regenerates). It is the SAME defect #194 named for
                # the has-rules half, just reached with the tag typed and the backticks
                # missing, so it gets the same treatment: no stub, warn, exit non-zero.
                #
                # What this branch actually OWNS is the wording. Its rc and stub effects
                # are identical to the general failure branch below, which would catch the
                # same .md (generation exits non-zero on it), so deleting this branch
                # changes nothing an operator can measure EXCEPT the diagnosis — from
                # "quote the relation name in backticks" to generation's raw stderr. The
                # message is therefore the branch's only observable contract, which is why
                # tests/test_finalize.sh pins its exact phrasing rather than just rc and
                # the absent .dl (#496 review). Keep the predicate honest for the same
                # reason: it is what licenses this remediation.
                policy_uncompiled = True
                sys.stderr.write(gen.stderr)
                print(
                    "finalize: WARNING — every policy bullet in policy/logic-policy.md was "
                    "REJECTED (an [id] tag with no backtick relation name), so "
                    "generate_logic_policy produced no logic-policy.dl (see the error "
                    "above) and the policy is NOT applied. This is an authoring defect, "
                    "not an empty policy: quote the relation name in backticks and re-run "
                    "— no empty-policy stub was written, so re-running retries generation.",
                    file=sys.stderr,
                )
            elif gen.returncode != 0:
                # generate failed for a reason that is neither of the two policy shapes
                # above: a malformed `{...}` marker, an undecodable relation name, a
                # missing or empty logic-policy.md, an OS-level failure. Whatever it was,
                # no .dl exists and the .md is not a benign ruleless one we can honestly
                # stub — say so instead of writing bytes that claim the KB has no policy.
                #
                # This widens finalize beyond #496's literal report (main wrote the stub
                # and exited 0 for a missing/empty .md; this exits non-zero), which is
                # deliberate: writing "// no policy rules" because generation FAILED is
                # the exact masking this issue removes, and both generate
                # ("missing or empty policy/logic-policy.md") and tools/validate.py
                # already treat that KB as invalid. The read-side loader stays graceful on
                # it by #190's design, so finalize and /factlog check answer differently
                # here on purpose — authoring pipeline vs. read gate. tests/test_finalize.sh
                # pins the absent- and empty-.md shapes; without them this branch was
                # deletable with the whole suite still green (#496 review, WARNING 1).
                policy_uncompiled = True
                sys.stderr.write(gen.stderr)
                print(
                    "finalize: WARNING — generate_logic_policy failed (see the error "
                    "above) and produced no logic-policy.dl, so the policy is NOT "
                    "applied. Fix the policy and re-run — no empty-policy stub was "
                    "written, so re-running retries generation.",
                    file=sys.stderr,
                )
            else:
                # No compilable rules → a no-op stub lets the check run with an empty
                # policy. This was the fresh-KB path until #491; a prose-only .md no
                # longer arrives here at all, because generate now succeeds on it and
                # writes these same bytes itself. Since #496 the two ways a FAILED
                # generate reached this branch — an all-rejected .md and an OS-level
                # failure — are handled above, so what is left is a successful generate
                # that produced nothing, and the stub is written for a KB that genuinely
                # has no policy to apply (#491's invariant: rules-free is not an error).
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

    # 5. run the deterministic logic check (needs pyrewire).
    logic_skipped = not _pyrewire_ok()
    if not logic_skipped:
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
        checked = "compiled (logic check SKIPPED — engine verification NOT run)"

    # #336: a skipped logic check exits 3 — distinct from a verified pass (0), argparse
    # misuse (2), a real failure (1) and a timeout (124) — so automation can tell an
    # unverified compile from an engine-checked one (run_logic_check hard-fails on absent
    # pyrewire; finalize used to exit 0 and look identical to a checked pass). The
    # explicit --allow-unverified opt-out keeps rc 0 for callers that accept it. The engine
    # present → unchanged (rc 0, "logic-checked").
    unverified = logic_skipped and not args.allow_unverified
    exit_code = 3 if unverified else 0
    # #356: --allow-unverified accepts an ENGINE-ABSENT skip, not a KB policy defect.
    # policy_uncompiled means logic-policy.md ATTEMPTED rules that did NOT compile —
    # bullets with relations that the compiler rejected, or (since #496) bullets that named
    # no relation at all — so the policy is silently NOT applied: a correctness fault
    # independent of pyrewire's
    # presence. Keep it non-zero regardless of the flag; otherwise CI running finalize
    # with --allow-unverified for no-pyrewire tolerance would also wave through a broken
    # policy (warning on stderr only).
    #
    # Which non-zero code depends on whether the ENGINE RAN, because that is the only
    # thing rc 3 claims (#336: "compiled but not engine-verified"). The rejected-only and
    # has-rules shapes leave no .dl, so with pyrewire present run_logic_check already
    # failed loud and returned 1 far above; what reaches here with the engine running is a
    # generation failure the loader stays graceful about (a missing or empty
    # logic-policy.md, #190). Reporting 3 for that would tell automation the engine was
    # skipped when it ran and passed — a false claim about verification, not about the
    # policy. So: engine skipped → 3 (unverified), engine ran → 1 (a real failure).
    if policy_uncompiled:
        exit_code = 3 if logic_skipped else 1
    # Keep the closing claim honest: when the engine ran, "no contradictions" is
    # engine-backed; when it was skipped, only the single-valued check_conflicts ran, so
    # say exactly that rather than "no contradictions" (which reads as engine-verified).
    contradiction_clause = (
        "single-valued contradiction check passed (engine logic NOT run)"
        if logic_skipped
        else "no contradictions"
    )
    if policy_uncompiled:
        # Reachable with the engine either skipped OR running: the shapes that leave no
        # .dl fail loud in run_logic_check above, but a generation failure the loader
        # treats as an empty policy (missing/empty logic-policy.md) gets all the way here
        # with the check having run and passed. The remedy line has to follow that, or the
        # summary claims "logic-checked" and "Install pyrewire" in the same breath — which
        # it did until #496's review caught it.
        remedy = (
            "Install pyrewire and run /factlog check to gate on the policy."
            if logic_skipped
            else "The logic check ran WITHOUT the policy, so its pass says nothing about "
            "policy findings. Fix the policy and re-run."
        )
        print(
            f"\nfinalize: done — merged, {checked}, {contradiction_clause}, "
            f"but the policy is NOT applied (see the WARNING above). {remedy}"
        )
        return exit_code
    print(f"\nfinalize: done — merged, {checked}, {contradiction_clause}.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
