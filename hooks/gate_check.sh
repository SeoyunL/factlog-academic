#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# factlog PreToolUse gate — deny writes to engine inputs when logic_report.txt
# is absent or stale, EXCEPT for the first (bootstrap) creation of an input.
#
# Fires BEFORE Write|Edit. If the tool is about to touch facts/accepted.dl or
# facts/query.dl, this script checks that facts/logic_report.txt exists and is
# newer than both files. If the predicate fails it exits 2, which Claude Code
# interprets as a permissionDecision=deny and blocks the tool call.
#
# FALSIFIABLE predicate (per CRITIC M4 + bootstrap fix):
#   Let TARGET be the tool target path, read from tool_input.file_path in the
#   PreToolUse envelope (top level kept as a legacy fallback — see #591). TARGET
#   is an "engine input" iff it resolves to <KB_ROOT>/facts/accepted.dl OR
#   <KB_ROOT>/facts/query.dl. If TARGET cannot be read at all but the raw payload
#   names one of those two files, it counts as an engine input of unknown
#   identity: branch B cannot apply and only branch C can allow.
#
#   ALLOW (exit 0) iff any of:
#     A. TARGET is not an engine input; OR
#     B. BOOTSTRAP: facts/logic_report.txt does NOT exist AND TARGET does NOT
#        yet exist on disk (this is the first creation of an engine input in a
#        fresh KB, where no report can possibly exist yet); OR
#     C. FRESH: facts/logic_report.txt EXISTS and is newer than (>=) the most
#        recently modified existing engine input (accepted.dl / query.dl).
#
#   DENY (exit 2) otherwise, i.e. TARGET is an engine input AND NOT bootstrap
#   AND (report absent OR report stale).
#
# This predicate is falsifiable in both directions:
#   - Bootstrap is allowed: creating facts/query.dl in a freshly `factlog init`
#     KB (no logic_report.txt, no pre-existing query.dl) returns exit 0.
#   - Stale-guard still denies: once a logic_report.txt exists, any edit that
#     would supersede it (report absent due to deletion, or report older than
#     an existing input) returns exit 2. Running /factlog check (which calls
#     run_logic_check.py and writes a fresh logic_report.txt) re-satisfies (C).
#
# KB root resolution: FACTLOG_ROOT > active-KB config > cwd. This matches the
# engine/CLI resolver (factlog.config.resolve_root(None)) so the gate guards the
# same KB the slash-skill and tools operate on.
#
# SCOPE: the gate protects the *active* KB (the one resolved above). Directly
# editing a NON-active KB's facts/accepted.dl or facts/query.dl — e.g. when an
# active KB is configured but cwd is a different KB-B — is NOT the gate's target:
# that write does not match the active KB_ROOT and is allowed. This is
# intentional and consistent with the tools, which also resolve to the active KB.
#
# That scope rule is a statement about RESOLVED targets, and it has exactly one
# exception, which follows from its own reasoning: when the target path cannot be
# read out of the payload at all (see the extraction block below), there is no
# path to compare against KB_ROOT, so which KB is meant is not merely unknown but
# unknowable. The scope rule has no input to apply. The fallback probe matches on
# filename alone and is therefore KB-blind: a payload we cannot parse that names
# facts/accepted.dl is judged against the ACTIVE KB's report even if it meant
# some other KB's copy. That errs toward denying, never toward allowing, so it
# takes nothing away from the guarantee above — and it stays escapable, since a
# fresh report in the active KB lets the write through. Making the probe
# KB-scoped instead would mean pattern-matching KB_ROOT against text we have
# already failed to parse, and would silently miss relative paths, which is the
# one direction this branch must not fail in.
#
# If the resolver cannot run (e.g. the factlog package is unavailable), KB_ROOT
# safely degrades to the prior ${FACTLOG_ROOT:-.} behaviour (usually cwd). This
# is a fail-to-previous-behaviour, NOT a fail-closed: it opens no new hole beyond
# what existed before this resolver, but it is permissive for cross-KB writes.
# That degrade is made OBSERVABLE: when Python is available but the resolver
# returns empty (package import failure), a one-line stderr note is emitted so
# the silent permissive fallback is visible to an operator (see below).
# The only true fail-closed here is the python-availability check below (which
# DENYs when no usable Python 3.11+ is present, since the predicate cannot then
# be evaluated). Target-path extraction failures for engine-input-shaped payloads
# likewise deny — a claim this header made from #239 onward while the code below
# allowed them unconditionally; #591 turned that gap into the live defect and the
# extraction block now implements what is written here.

set -euo pipefail

payload="$(</dev/stdin)"

# Determine the KB root: FACTLOG_ROOT > active-KB config > cwd.
# Fail-safe fallback used until the config-aware resolver (below) succeeds.
KB_ROOT="${FACTLOG_ROOT:-.}"

HOOK_DIR="$(cd "${BASH_SOURCE[0]%/*}" && pwd)"
PYTHON_RUNNER_SCRIPT="${FACTLOG_PYTHON_RUNNER:-"$HOOK_DIR/../tools/factlog_python.sh"}"
PYTHON_RUNNER=( "${BASH:-bash}" "$PYTHON_RUNNER_SCRIPT" )

# Python 3.11+ is required for JSON parsing and portable path/mtime handling.
# Fail closed: without it we cannot evaluate the predicate safely.
#
# stdout only is discarded here, deliberately. This is the first runner exec of a
# gate evaluation — and, when it fails closed just below, the ONLY one. Past it
# the evaluation reaches three, five, or six, decided by the target alone (see the
# exec-count table in tools/factlog_python.sh, pinned by CASE 19 of
# tests/test_gate_check.sh). The runner writes one stderr line
# when it selects an interpreter from outside PATH (#578). Swallowing stderr at
# every call site would make that disclosure zero-per-evaluation, which is the
# silent selection it exists to prevent; letting it through exactly here surfaces
# it once, alongside the deny reason a user actually reads.
if ! "${PYTHON_RUNNER[@]}" -c 'import sys' >/dev/null; then
  echo "[factlog GATE] DENIED: usable Python 3.11+ is required to evaluate the gate predicate." >&2
  echo "  Set FACTLOG_PYTHON to a venv/system python if python3 is unavailable or is a Windows Store stub." >&2
  exit 2
fi

# Resolve the KB root config-aware, matching the engine/CLI resolver so the gate
# guards the same KB the tools write to: FACTLOG_ROOT > active-KB config > cwd.
# factlog.config.resolve_root(None) implements exactly that precedence (no flag).
# The factlog package lives beside this hook in the plugin root ($HOOK_DIR/..).
# If resolution fails for any reason, KB_ROOT safely degrades to the prior
# ${FACTLOG_ROOT:-.} behaviour (fail-to-previous-behaviour, no new hole); it is
# not fail-closed — the python-availability check above owns that.
resolved_root="$(FACTLOG_HOOK_PLUGIN_ROOT="$HOOK_DIR/.." "${PYTHON_RUNNER[@]}" -c \
  'import os, sys; sys.path.insert(0, os.path.abspath(os.environ["FACTLOG_HOOK_PLUGIN_ROOT"])); from factlog import config; print(config.resolve_root(None)[0])' \
  2>/dev/null || true)"
if [ -n "$resolved_root" ]; then
  KB_ROOT="$resolved_root"
else
  # Python IS available (the fail-closed check above passed) yet the resolver
  # returned nothing. resolve_root(None) always yields a non-empty absolute path
  # (its final fallback is cwd), so the only way to reach here is the factlog
  # package failing to import in the child (corrupt/missing package under the
  # plugin root). That silent, permissive degrade to ${FACTLOG_ROOT:-cwd} is
  # intentional (fail-to-previous-behaviour, protects bootstrap/first-run UX and
  # opens no new hole) — but make it OBSERVABLE with a one-line stderr note so an
  # operator can see the resolver was bypassed. This does NOT change the
  # exit-code contract or path matching.
  echo "[factlog GATE] note: factlog config resolver unavailable; freshness gate falling back to \${FACTLOG_ROOT:-cwd} (KB_ROOT=$KB_ROOT)" >&2
fi

# Extract the tool target from the hook payload.
#
# Claude Code sends an ENVELOPE on stdin, not the bare tool input (#591):
#   {"session_id":..., "cwd":..., "hook_event_name":"PreToolUse",
#    "tool_name":"Write", "tool_input":{"file_path":..., "content":...},
#    "tool_use_id":...}
# so the target lives at tool_input.file_path — for Edit exactly as for Write,
# both tools name it "file_path". Reading only the TOP level (the pre-#591
# behaviour) found nothing in every real invocation and fell through to the
# fail-open below, which is why this gate never once fired in production.
# The top-level lookup is kept as a LEGACY fallback for callers that pipe a bare
# tool input (older harnesses, manual `echo '{"file_path":...}' | gate_check.sh`).
#
# tool_name is deliberately NOT consulted. Routing is hooks.json's job (its
# matcher is Write|Edit); this hook's predicate is about the TARGET, so whatever
# tool call is routed here is judged by the path it carries and nothing else.
# Gating on tool_name would re-add a fail-open on an unrecognised/renamed field —
# the exact failure mode #591 was.
target_path="$(printf '%s' "$payload" | "${PYTHON_RUNNER[@]}" -c \
  "import json,sys; d=json.load(sys.stdin); d=d if isinstance(d,dict) else {}; ti=d.get('tool_input'); ti=ti if isinstance(ti,dict) else {}; print(ti.get('file_path') or ti.get('path') or d.get('file_path') or d.get('path') or '')" \
  2>/dev/null || true)"

# No path extracted. Two causes hide behind one symptom, and #591 proved they
# must NOT share a verdict:
#
#   (a) the payload genuinely names no engine input (a pathless probe, a shape
#       this hook is not meant to judge, plain garbage on stdin), or
#   (b) the payload DOES carry an engine input but under a key/shape we failed
#       to read — a future schema change, a tool form we did not anticipate.
#
# The pre-#591 comment asserted "an empty/unparseable payload cannot target an
# engine input" and allowed both. That premise was FALSE for (b): the path was
# in the payload all along, we looked in the wrong place, and the gate SKILL.md
# calls "do not skip" silently permitted every engine-input write for its whole
# existence. An assumption that a payload we cannot read is harmless is not
# something this gate gets to make about its own two files.
#
# So separate them by evidence rather than by assumption: scan the RAW payload
# text for an engine-input filename (the shape-agnostic probe gate_reminder.sh
# already relies on, so it survives envelope changes). Present → treat the write
# as targeting an unknown engine input and fall through to the freshness
# predicate (fail-closed-ish). Absent → allow, and now the premise is actually
# checked instead of assumed.
#
# Deliberately NOT a blanket deny-on-unreadable: this hook fires on every Write
# and Edit in a session, so denying whenever a path cannot be parsed would brick
# all file editing over a contract that covers exactly two files.
#
# The residual false positive is a write whose CONTENT mentions facts/accepted.dl
# while its own target path could not be read. #591 dismissed that as "a state
# that should not occur"; #596 asked what pins it, since any tool naming its
# target under another key (NotebookEdit uses notebook_path) would land here on
# its content alone. What pins it is hooks.json's matcher AND Claude Code's
# matcher semantics — and the second half is a property of a program this repo
# does not ship, so it is a measurement, not a guarantee:
#
#   Claude Code 2.1.220 does not apply a matcher of only [A-Za-z0-9_|, -] as a
#   regex at all. It splits on | and , and compares each token to the tool name by
#   EXACT equality; the regex path is taken only by matchers carrying some other
#   character. A live session pinned it end to end: with "Write|Edit",
#   "NotebookEdit" and "Notebook.*" registered side by side, one NotebookEdit call
#   fired the latter two and NOT "Write|Edit", and one Write call fired only
#   "Write|Edit".
#
# So "Write|Edit" routes exactly Write and Edit, both of which carry
# tool_input.file_path (CASE 20 pins both shapes) — NotebookEdit never arrives
# here, and a failed extraction means a payload from outside that routing.
#
# Two ways that stops holding, one of which this repo can watch: a matcher edited
# to carry a regex metacharacter ("Notebook.*|Write|Edit") switches to the regex
# path, which is unanchored, and routes tools whose target key is not read here —
# CASE 23 of tests/test_gate_check.sh fails if hooks.json's matcher stops being a
# plain list of tools this gate can read a path out of. The other is Claude Code
# changing matcher semantics (it last did at 2.1.195); nothing here pins that.
#
# The extraction keys are deliberately NOT widened to cover such a tool. Every
# added key is a new payload-controlled way to produce a RESOLVED, non-engine
# target, which can only move verdicts from deny toward allow — paid for a routing
# path that does not exist at the measured version. And the branch is not a wall:
# the predicate, not a hard deny, decides the verdict (see below), so running
# /factlog check clears a false positive.
#
# The probe is a shell `case`, not grep. An external command here would decide a
# security verdict on whether PATH happens to contain it: with grep missing, the
# `if` condition simply failed (set -e does not fire inside an `if`) and the gate
# ALLOWED — a second fail-open with a second trigger, reached without touching
# the payload logic at all. Everything this branch needs is glob matching, which
# the shell already has, so the probe now depends on nothing but bash. The
# separator appears three ways in raw payload text: "/" on POSIX, a lone "\" in
# text JSON never escaped, and "\\" once JSON has escaped a Windows backslash.
target_unresolved=false
if [ -z "$target_path" ]; then
  case "$payload" in
    *facts/accepted.dl*     | *facts/query.dl*     | \
    *'facts\accepted.dl'*   | *'facts\query.dl'*   | \
    *'facts\\accepted.dl'*  | *'facts\\query.dl'*)
      target_unresolved=true
      echo "[factlog GATE] note: could not extract a target path from the hook payload, but it names an engine input; evaluating the freshness predicate conservatively." >&2
      ;;
    *)
      exit 0
      ;;
  esac
fi

# Normalise: check whether the target is facts/accepted.dl or facts/query.dl
# under the KB root. Match both absolute and relative paths.
#
# Use Python for portable path canonicalisation — realpath -m is GNU-only and
# is not available on macOS/BSD. os.path.realpath resolves symlinks and
# normalises . / .. segments on all platforms without requiring the path to
# exist (matching realpath -m semantics).
_canon() {
  "${PYTHON_RUNNER[@]}" -c "import os,sys; print(os.path.realpath(os.path.abspath(os.path.expanduser(sys.argv[1]))))" "$1" 2>/dev/null || printf '%s' "$1"
}

is_engine_input=false
abs_target=""
if [ "$target_unresolved" = true ]; then
  # Path unreadable but the payload names an engine input: assume it is one.
  # abs_target stays empty on purpose — we do not know WHICH file, so no on-disk
  # test may be run against it (see the bootstrap branch below).
  is_engine_input=true
else
  abs_target="$(_canon "$target_path")"
  for engine_file in "${KB_ROOT}/facts/accepted.dl" "${KB_ROOT}/facts/query.dl"; do
    abs_engine="$(_canon "$engine_file")"
    if [ "$abs_target" = "$abs_engine" ]; then
      is_engine_input=true
      break
    fi
  done
fi

# If the target is not an engine input file, allow the tool to proceed.
if [ "$is_engine_input" = false ]; then
  exit 0
fi

report="${KB_ROOT}/facts/logic_report.txt"
accepted="${KB_ROOT}/facts/accepted.dl"
query="${KB_ROOT}/facts/query.dl"

# BOOTSTRAP (predicate branch B): a fresh KB has neither facts/logic_report.txt
# nor the engine input being created. `factlog init` seeds neither file, so the
# FIRST creation of facts/query.dl (or facts/accepted.dl) cannot possibly be
# preceded by a report. Allow it; the stale-guard takes over once a report
# exists. We test the on-disk existence of the *target* (not the path string)
# so this only relaxes the genuine first-write case.
#
# When the target could not be resolved (target_unresolved), abs_target is empty
# and this branch is skipped: bootstrap is a claim about ONE specific file being
# absent, and we do not know which file was named. Skipping it costs nothing —
# running /factlog check in a fresh KB writes a report that no existing input
# post-dates, so the predicate below then allows. The gate stays escapable
# instead of deadlocking.
if [ "$target_unresolved" = false ] && [ ! -f "$report" ] && [ ! -e "$abs_target" ]; then
  exit 0
fi

# Predicate: report must exist and be newer than the most recently modified
# engine input file (accepted.dl or query.dl).
if [ ! -f "$report" ]; then
  echo "[factlog GATE] DENIED: facts/logic_report.txt does not exist." >&2
  echo "  An engine input already exists but no report supersedes it." >&2
  echo "  Run /factlog check (\"\${CLAUDE_PLUGIN_ROOT}\"/tools/factlog_python.sh \"\${CLAUDE_PLUGIN_ROOT}\"/tools/run_logic_check.py)" >&2
  echo "  to produce a fresh report before editing engine inputs." >&2
  exit 2
fi

# FRESHNESS (predicate branch C): deny unless the report post-dates every engine
# input that exists. `-ot` is bash's own mtime comparison, so this needs no
# external command — the whole predicate below runs on builtins.
#
# It used to materialise the numbers with `stat` (-c %Y for GNU, then -f %m for
# BSD) and, when neither form produced one, deny with "could not read mtime".
# Two things were wrong with that, and #600 separates them:
#
#   COVERAGE — no row in tests/test_gate_check.sh ever executed that deny.
#   Instrumenting the branch and running the file scored 0 hits across all 66
#   rows. CASE 22's comment claimed its empty-PATH row reached `stat`
#   "incidentally"; it does not — that KB has no report, so the deny just above
#   returns first, and the same instrumentation scores 0 on that fixture alone.
#
#   VERDICT — the deny was wrong on its own terms. "stat is not on PATH" is a
#   fact about the environment, not about the KB. By that point the gate has
#   established the report EXISTS; it then denied a write whose report may well
#   have been fresh, because a coreutil was out of reach. Measured: a KB with an
#   existing accepted.dl and a newer logic_report.txt, targeted by a Write, exits
#   0 normally and exited 2 "could not read mtime" with PATH emptied and the
#   interpreter reached by absolute path (so the fail-closed python check above
#   still passes and the deny provably came from here).
#
# That is the defect #591 removed from the engine-input probe one branch up, in
# the same shape: an external command deciding a security verdict on whether
# PATH happens to carry it. #591's answer was not to pick a safer direction for
# the missing-command case but to stop needing the command, and the answer here
# is the same. With `-ot` the state "cannot read mtime because stat is absent"
# no longer exists, so there is no longer a verdict to get wrong — which is also
# why this ships without a case pinning that deny: the branch is gone, not
# merely untested.
#
# Deleting a deny moves verdicts deny -> allow, which #596 treats as widening the
# trust boundary, so name the set that moves: exactly {report exists AND report
# is genuinely fresh AND stat unreachable}. Nothing in it is payload-controlled
# — PATH is not an input this gate is a boundary against, since whoever sets it
# also chose the bash and the interpreter running this file — and the
# complementary set {report exists AND genuinely stale AND stat unreachable}
# still denies. CASE 24 pins both halves as a pair in one environment.
#
# Resolution, measured rather than assumed. On bash 3.2.57 (macOS /bin/bash)
# `-ot` compares whole seconds, identical to `stat -f %m`: two files 0.10s and
# 0.90s into the SAME second compare as not-older. GNU bash compares finer. That
# divergence cannot open the guard in either direction, because sec(a) < sec(b)
# implies ns(a) < ns(b) — a finer comparison denies a superset of what a coarser
# one denies, never a different set. Equal mtimes are not "older" at either
# resolution, which keeps the >= in the header exact (CASE 24 pins that row).
# The sub-second window is the one part that moves with the bash build and is
# deliberately left unasserted; where it moves is toward factlog's other copy of
# this predicate, `factlog status`, which reads st_mtime as a float.
#
# Residual, stated rather than dismissed: `-ot` re-stats paths that `[ -f ]` just
# stated, and reports a failed stat as a boolean rather than as an error. A file
# that vanishes between the two calls is judged "not older", i.e. allowed. That
# window is not measured here and is not claimed to be impossible.
report_stale=false
for f in "$accepted" "$query"; do
  if [ -f "$f" ] && [ "$report" -ot "$f" ]; then
    report_stale=true
    break
  fi
done

if [ "$report_stale" = true ]; then
  echo "[factlog GATE] DENIED: facts/logic_report.txt is stale." >&2
  echo "  The report predates the last modification to facts/accepted.dl or facts/query.dl." >&2
  echo "  Run /factlog check (\"\${CLAUDE_PLUGIN_ROOT}\"/tools/factlog_python.sh \"\${CLAUDE_PLUGIN_ROOT}\"/tools/run_logic_check.py)" >&2
  echo "  to refresh the report before editing engine inputs." >&2
  exit 2
fi

# Report is fresh — allow the write/edit to proceed.
exit 0
