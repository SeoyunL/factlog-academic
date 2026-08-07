#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# factlog PreToolUse gate — deny writes to engine inputs when logic_report.txt
# is absent, stale, or records a run the engine never completed, EXCEPT for the
# first (bootstrap) creation of an input.
#
# Fires BEFORE Write|Edit. If the tool is about to touch facts/accepted.dl or
# facts/query.dl, this script checks that facts/logic_report.txt exists, records
# a completed engine run, and is newer than both files. If the predicate fails
# it exits 2, which Claude Code interprets as a permissionDecision=deny and
# blocks the tool call.
#
# FALSIFIABLE predicate (per CRITIC M4 + bootstrap fix + #338):
#   Let TARGET be the tool target path. TARGET is an "engine input" iff it
#   resolves to <KB_ROOT>/facts/accepted.dl OR <KB_ROOT>/facts/query.dl.
#
#   Call a report FAILED iff it contains the whole line
#   `status: engine-did-not-run` — the line run_logic_check.py writes when the
#   engine could not run at all (pyrewire missing, facts/accepted.dl absent, a
#   program the engine refuses). Such a report states the cause and NOTHING
#   about the KB, so for this predicate it counts as no report at all.
#
#   ALLOW (exit 0) iff any of:
#     A. TARGET is not an engine input; OR
#     B. BOOTSTRAP: facts/logic_report.txt does NOT exist, or exists and is
#        FAILED, AND TARGET does NOT yet exist on disk (this is the first
#        creation of an engine input in a fresh KB, where no report of a
#        completed run can possibly exist yet); OR
#     C. FRESH: facts/logic_report.txt EXISTS, is NOT FAILED, and is newer than
#        (>=) the most recently modified existing engine input (accepted.dl /
#        query.dl).
#
#   DENY (exit 2) otherwise, i.e. TARGET is an engine input AND NOT bootstrap
#   AND (report absent OR report FAILED OR report stale).
#
#   TARGET itself is read from the hook payload; when it cannot be read at all
#   the predicate above is undefined, and the narrow fail-closed rule described
#   under "fail-closed branches" below decides instead.
#
# This predicate is falsifiable in both directions:
#   - Bootstrap is allowed: creating facts/query.dl in a freshly `factlog init`
#     KB (no logic_report.txt, no pre-existing query.dl) returns exit 0. It stays
#     allowed after a failed /factlog check in that same KB: the failure report
#     that run now writes must not turn the first creation of an input into a
#     deny it never was (#338).
#   - Stale-guard still denies: once a logic_report.txt exists, any edit that
#     would supersede it (report absent due to deletion, or report older than
#     an existing input) returns exit 2. Running /factlog check (which calls
#     run_logic_check.py and writes a fresh logic_report.txt) re-satisfies (C).
#   - A failed check does NOT open the gate: /factlog check writes a report even
#     when the engine cannot run, and that report is fresh by mtime, so on the
#     mtime test alone it would satisfy (C) and hand out edit rights on engine
#     inputs precisely when the engine is broken. FAILED is what keeps the deny;
#     what changes is that the deny now names the cause instead of pointing at
#     the command that just failed. There is deliberately no escape hatch — the
#     Write|Edit matcher leaves Bash open, and docs/guide/determinism.md walks
#     through the recovery.
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
# If the resolver cannot run (e.g. the factlog package is unavailable), KB_ROOT
# safely degrades to the prior ${FACTLOG_ROOT:-.} behaviour (usually cwd). This
# is a fail-to-previous-behaviour, NOT a fail-closed: it opens no new hole beyond
# what existed before this resolver, but it is permissive for cross-KB writes.
# That degrade is made OBSERVABLE: when Python is available but the resolver
# returns empty (package import failure), a one-line note is emitted so the
# silent permissive fallback is visible to an operator (see below).
# THREE branches DENY without evaluating the freshness predicate:
#   1. The python-availability check below DENYs when no usable Python 3.11+ is
#      present, since the predicate cannot then be evaluated. Escape hatch:
#      FACTLOG_PYTHON (point it at a usable interpreter).
#   2. Target-path extraction DENYs only in the narrow case where the payload
#      carries a `tool_input` JSON OBJECT, `tool_name` is one of the write-class
#      tools this hook is registered for, and NO usable path can be read from
#      either `tool_input` or the top level. That combination means the payload
#      schema drifted out from under the extractor while a write was in flight,
#      so the predicate cannot be evaluated for a write that may well target an
#      engine input. Escape hatch: FACTLOG_GATE_ALLOW_UNREADABLE_PAYLOAD=1,
#      which only a human can set (see the deny message below). The name states
#      the one branch it releases: it must NOT grow into a switch that also
#      releases branch 1 or 3, or the freshness predicate itself.
#   3. _mtime() DENYs when `stat` cannot report a file's mtime. That branch has
#      NO escape hatch; it is only reachable for a file this script has already
#      seen pass `-f`, i.e. a race or an unreadable filesystem.
# Only branches 1 and 2 carry an escape hatch, and only branches 1 and 2 are
# about the payload/interpreter layer.
#
# Everything else fails OPEN (exit 0). Four of those branches are a check the
# gate SKIPPED because it could not read the call, and each emits a one-line
# note — #323 was filed partly because such skips were silent and left the
# operator believing the gate was running (same rule as the resolver degrade,
# #244). Notes ride the exit-0 channel the hook contract actually surfaces, a
# JSON `systemMessage` on stdout; see the _note/_allow helpers below for why a
# bare stderr line would have reached nobody:
#   - an unparseable payload;
#   - an INCOMPLETE record from the extractor — the interpreter died, or wrote
#     something that is not three NUL-terminated fields. With no record at all
#     we cannot tell the call is even a write, so denying would block every tool
#     call in the session on evidence we do not have.
#   - a write-class `tool_name` whose `tool_input` is absent or is not a JSON
#     object. The JSON parses and the record is complete, so the two notes above
#     do not cover it, yet the gate is just as blind. Under the current schema
#     Write/Edit always send a `tool_input` object, so this fires zero times in
#     normal operation;
#   - a record whose kind field is none of the four the extractor writes, which
#     means a NUL inside a JSON string shifted the fields along. The OS rejects
#     such a path, so the impact is zero — but a silent exit here would make the
#     list below false.
# Two branches stay silent, and neither is a skipped check:
#   - a `tool_name` outside the write-class list, when no path could be read
#     from it either;
#   - a payload with no `tool_name` at all.
#
# Read that first bullet narrowly: it is about not emitting a NOTE, not about
# scope. The write-class list gates the fail-closed branch and the notes; it does
# NOT gate the freshness predicate. Once a target path IS readable, the predicate
# runs on the TARGET regardless of tool name, so a `Read` of a stale
# facts/accepted.dl exits 2 — as do Grep, MultiEdit and NotebookEdit. That is
# deliberate and predates #323. Keying the predicate on the write-class list
# instead would mean a user who widens their matcher to MultiEdit/NotebookEdit —
# the very case the list's comment says it defends — gets no guarding at all,
# because those names are not in it. Denying a read is a false positive that
# only reaches someone who widened the matcher themselves, and it errs toward
# guarding; silently dropping the guard for a renamed write tool does not.
# (The deny text is phrased for a write. If the matcher is ever widened for
# real, that wording needs revisiting.)
#
# WHY THE SPLIT IS A BET, NOT A PRINCIPLE. Branch 2 denies and the third
# fail-open branch above allows, yet the two know exactly the same thing: both
# read `tool_name`, both find it write-class, and neither can read a path. The
# earlier claim that one of them "cannot tell the call is a write" is only true
# of the incomplete-record branch, not of this pair. What actually separates
# them is the SHAPE of the drift and what it costs to be wrong about it:
#   - `tool_input` is still an object but its path key was renamed. The envelope
#     is intact, so the drift is local and probably affects one key. Denying
#     costs writes to two files in one KB, and the escape hatch releases it.
#   - the `tool_input` key ITSELF was renamed or restructured. Every Write/Edit
#     in every session hits this at once, so denying is a global write outage,
#     and the escape hatch has to be set by a human in a NEW session — which is
#     unreachable if every write is already blocked.
# So the split is a deliberate wager that the first drift is likelier and the
# second is more expensive to guess wrong on, not a rule about what the gate
# knows. A future maintainer should not generalise it into "we fail closed
# whenever we know it is a write".
# One more permissive degrade exists further down: the engine-input matcher
# falls back to a raw string comparison if the canonicaliser returns no verdict,
# which can only make a match LESS likely (i.e. more permissive). It now emits a
# note like the branches above rather than degrading silently.
#
# A basename PREFILTER short-circuits to exit 0 before any canonicalisation
# when the target's last path component proves it cannot be an engine input.
# That keeps the gate off the critical path of writes it does not care about;
# see the prefilter itself for its seven guards — the five ways a target's name
# can lie about the file it denotes, plus two that are not name questions at all
# (an unexpanded tilde, and a symlinked engine input) — and how each is closed.
#
# The "resolves to" in the predicate above means the FILESYSTEM's answer, not
# string equality of two canonical paths. A hard link and — on a case-folding
# filesystem such as APFS or NTFS — a differently-cased spelling both name the
# same file under a different string, and the gate treats them as the engine
# input. Its one remaining blind spot is a case-only difference that stat cannot
# settle AND whose directory probe cannot run; see the matcher for the detail.
#
# A gate protecting two files in one KB must not become a global Write/Edit
# outage, so anything short of positive knowledge falls open.

set -euo pipefail

payload="$(</dev/stdin)"

# Determine the KB root: FACTLOG_ROOT > active-KB config > cwd.
# Fail-safe fallback used until the config-aware resolver (below) succeeds.
KB_ROOT="${FACTLOG_ROOT:-.}"

HOOK_DIR="$(cd "${BASH_SOURCE[0]%/*}" && pwd)"
PYTHON_RUNNER_SCRIPT="${FACTLOG_PYTHON_RUNNER:-"$HOOK_DIR/../tools/factlog_python.sh"}"
PYTHON_RUNNER=( "${BASH:-bash}" "$PYTHON_RUNNER_SCRIPT" )

# Operator notes on the fail-OPEN paths (see "Everything else fails OPEN" above).
#
# CHANNEL. The PreToolUse contract routes each exit code differently:
#   exit 2      — stderr is handed to Claude as the block reason; stdout ignored.
#   exit other  — non-blocking error; stderr is shown in the transcript.
#   exit 0      — non-JSON stdout is "logged but not shown in transcript", and
#                 the contract makes no promise at all about stderr.
# Every note below sits on an exit-0 path, so a bare `echo ... >&2` reaches
# nobody but `claude --debug` and an external capture of the hook process. That
# is the wrong end of #323: a note that documents a skipped check has to be
# visible to the person who would otherwise believe the gate ran.
#
# The exit-0 channel that IS surfaced is a JSON object on stdout carrying the
# universal `systemMessage` field ("user-facing warning"). We emit exactly that,
# and deliberately emit NO `hookSpecificOutput.permissionDecision`:
# `permissionDecision: "allow"` would skip the normal permission prompt, i.e.
# this gate would start auto-approving the very writes it just admitted it could
# not check. With no decision field the call continues through the normal
# permission flow, unchanged.
#
# Notes are BUFFERED rather than printed as they occur, because two notes can
# fire in one run (resolver degrade + a fail-open branch) and two JSON objects on
# stdout is not a JSON document. They are also mirrored to stderr, which costs
# nothing and keeps `claude --debug` and external capture working.
#
# FREQUENCY. Every note but one reports a condition of the CALL, so it appears
# on the call that provoked it and nowhere else. The exception is the
# resolver-degrade note: its condition is a broken install, which persists for
# the whole session, and it is evaluated before the target path is read — so on
# such an install it fires on every Write/Edit, KB-related or not. Its emission
# site below carries the detail.
GATE_NOTES=""

_note() {
  echo "[factlog GATE] $1" >&2
  if [ -n "$GATE_NOTES" ]; then
    GATE_NOTES="$GATE_NOTES $1"
  else
    GATE_NOTES="$1"
  fi
}

# Allow the tool call, handing any buffered notes to Claude Code as a
# systemMessage. Escaping goes through json.dumps rather than shell quoting
# because notes interpolate a tool name and a KB path.
#
# `2>/dev/null || true` means that if the interpreter cannot run here, the
# systemMessage is dropped and only the stderr mirror survives. That swallow
# applies to EVERY note branch, not just the one it was reasoned about: the
# "incomplete extractor record" case, where the interpreter is already known to
# be broken and there is nothing better to do. For the other branches it is a
# silent downgrade to a channel the exit-0 contract does not surface. It is
# accepted rather than handled because the alternative — letting a failed note
# take down the allow — would turn a warning into a write outage, which is the
# one thing this gate must not do.
_allow() {
  if [ -n "$GATE_NOTES" ]; then
    FACTLOG_GATE_NOTE_TEXT="$GATE_NOTES" "${PYTHON_RUNNER[@]}" -c \
      'import json, os; print(json.dumps({"systemMessage": "[factlog GATE] " + os.environ["FACTLOG_GATE_NOTE_TEXT"]}))' \
      2>/dev/null || true
  fi
  exit 0
}

# Python 3.11+ is required for JSON parsing and portable path/mtime handling.
# Fail closed: without it we cannot evaluate the predicate safely.
if ! "${PYTHON_RUNNER[@]}" -c 'import sys' >/dev/null 2>&1; then
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
  # opens no new hole) — but make it OBSERVABLE with a one-line note so an
  # operator can see the resolver was bypassed. This does NOT change the
  # exit-code contract or path matching.
  #
  # BEHAVIOUR CHANGE, and the one worth calling out to users: this note used to
  # be a bare `echo ... >&2`, which on an exit-0 path reached nobody but
  # `claude --debug`. It is now a user-visible systemMessage. The condition it
  # reports is install-level and persistent, not per-call — a factlog package
  # that will not import stays broken for the whole session — and this hook runs
  # BEFORE the target path is even looked at. So on such an install the warning
  # fires on EVERY Write/Edit, including files in projects that have no KB
  # anywhere near them (measured: 3/3 writes to an unrelated source file). That
  # is the right direction for #244 — a gate that silently is not running should
  # say so — but it is a repeating warning, and the fix is to repair the install
  # rather than to quiet the note.
  _note "note: factlog config resolver unavailable; freshness gate falling back to \${FACTLOG_ROOT:-cwd} (KB_ROOT=$KB_ROOT)"
fi

# Extract the tool target from the hook payload (issue #323).
#
# Claude Code sends an ENVELOPE on stdin, not the bare tool input:
#   {"session_id":..,"cwd":..,"hook_event_name":"PreToolUse","tool_name":"Write",
#    "tool_input":{"file_path":..,"content":..},"tool_use_id":..}
# so the target path lives under `tool_input`, which the previous extractor
# never looked at — every real payload fell through to the fail-open branch.
#
# Key precedence: `tool_input` first, then the TOP LEVEL as a fallback. No real
# Claude Code payload puts `file_path` at the top level; that fallback exists to
# keep the flat fixture shape used by tests/test_gate_check.sh working.
# `notebook_path` is defensive only: hooks.json registers the matcher "Write|Edit",
# which Claude Code compares by exact tool name, so NotebookEdit (and MultiEdit)
# never reach this hook. It costs nothing and covers a user who widens the
# matcher in their own settings.json.
#
# The extractor pulls each field under its OWN try/except and always writes
# exactly three NUL-terminated fields, so a failure in one field cannot truncate
# the others. NUL is the separator because a path may legally contain a newline,
# and a newline separator misreads such a path (or, with a leading newline,
# denies a perfectly legal write). A JSON string CAN itself contain a NUL, which
# shifts the remaining fields; that is a wash here — a truncated engine-input
# path still matches the engine input (deny), and a NUL-prefixed one is a path
# the OS cannot write to anyway.
#
# Three fields, not two: `tool_name` is carried raw so the write-class decision
# stays a readable `case` in shell right next to hooks.json's matcher, and so
# the deny message can name the offending tool. Collapsing the tool name and the
# tool_input shape into one field is possible, but it moves that decision into
# the embedded Python where it is harder to audit.
#
# Fields are read straight off a pipe: bash command substitution silently drops
# NUL bytes, so `$(...)` cannot capture this. A shell that cannot parse process
# substitution would raise a SYNTAX error, and bash exits 2 on one — which
# PreToolUse reads as DENY, not as a fail-open. That is unreachable in practice
# (bash falls back to FIFOs where /dev/fd is missing, and macOS, Linux and Git
# Bash all support it), so the code is left as is; it is noted only so the
# failure direction is not misdescribed.
GATE_EXTRACT_PY="
import json, sys
PATH_KEYS = (\"file_path\", \"path\", \"notebook_path\")
UNPARSED = object()
try:
    payload = json.load(sys.stdin)
except Exception:
    payload = UNPARSED
try:
    name = payload.get(\"tool_name\")
    tool_name = name if isinstance(name, str) else \"\"
except Exception:
    tool_name = \"\"
try:
    if payload is UNPARSED:
        input_kind = \"unparsed\"
    elif not isinstance(payload, dict) or \"tool_input\" not in payload:
        input_kind = \"absent\"
    elif isinstance(payload[\"tool_input\"], dict):
        input_kind = \"object\"
    else:
        input_kind = \"other\"
except Exception:
    input_kind = \"absent\"
try:
    target = \"\"
    nested = payload.get(\"tool_input\") if isinstance(payload, dict) else None
    for source in (nested, payload):
        if not isinstance(source, dict):
            continue
        for key in PATH_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value:
                target = value
                break
        if target:
            break
except Exception:
    target = \"\"
sys.stdout.write(tool_name + \"\\0\" + target + \"\\0\" + input_kind + \"\\0\")
"

tool_name=""
target_path=""
# Seeded with the value that describes "no record read yet" rather than a real
# payload shape: every path below overwrites it, and a wrong seed would be a
# lie waiting for a future edit to expose it.
tool_input_kind="incomplete"
if ! { IFS= read -r -d '' tool_name \
    && IFS= read -r -d '' target_path \
    && IFS= read -r -d '' tool_input_kind; } \
    < <(printf '%s' "$payload" | "${PYTHON_RUNNER[@]}" -c "$GATE_EXTRACT_PY" 2>/dev/null); then
  # The extractor produced no complete record (the interpreter died, or wrote
  # something that is not three NUL-terminated fields). Treat it as an
  # unparseable payload: fail OPEN, the pre-#323 behaviour. See the header for
  # why this lands opposite to the narrow fail-closed branch below.
  tool_name=""
  target_path=""
  tool_input_kind="incomplete"
fi

# Write-class tool names, matched EXACTLY. A user may register this hook with a
# broader matcher in their own settings.json, so the deny branch below must key
# off the tool name rather than assume only Write/Edit arrive. Anything outside
# this list — including an absent tool_name, which is what the flat test
# fixtures send — is not a write we can reason about, and falls open.
_is_write_tool() {
  case "$1" in
    Write|Edit) return 0 ;;
    *) return 1 ;;
  esac
}

if [ -z "$target_path" ]; then
  if [ "$tool_input_kind" = "object" ] && _is_write_tool "$tool_name"; then
    # Narrow fail-closed: a write-class call carrying a structured tool_input
    # from which no path key could be read. Usually that means the payload
    # schema drifted; a present-but-empty file_path lands here too, and is
    # denied regardless of report freshness because this branch runs before the
    # engine-input match. Either way we cannot tell whether it targets an
    # engine input.
    #
    # The escape hatch is read from THIS process's environment, which a hook
    # inherits from the Claude Code process. A model CAN write settings.json
    # with Bash; what it cannot do is make that take effect for the call it is
    # currently making, because the environment was fixed when Claude Code
    # started. The barrier is the new session, not the edit — so the deny
    # message is addressed to a human operator and says so.
    if [ "${FACTLOG_GATE_ALLOW_UNREADABLE_PAYLOAD:-}" = "1" ]; then
      _note "note: could not read a target path from the $tool_name payload; FACTLOG_GATE_ALLOW_UNREADABLE_PAYLOAD=1 is set, so the write is allowed unchecked."
      _allow
    fi
    echo "[factlog GATE] DENIED: could not read a target path from the $tool_name tool payload." >&2
    echo "  Either the tool call carried an empty path — in which case the write itself is" >&2
    echo "  malformed and nothing needs configuring — or the hook payload schema changed, so" >&2
    echo "  the freshness predicate cannot be evaluated and this write cannot be shown to" >&2
    echo "  miss facts/accepted.dl or facts/query.dl." >&2
    echo "  If it is the schema: this cannot be worked around from inside the session. Ask" >&2
    echo "  the operator to set FACTLOG_GATE_ALLOW_UNREADABLE_PAYLOAD=1 in the Claude Code" >&2
    echo "  environment (the \"env\" block of settings.json, or export it before launching" >&2
    echo "  Claude Code) and start a new session. That bypasses only this check — the" >&2
    echo "  freshness deny and the Python-availability deny still apply." >&2
    echo "  Please also report the payload shape upstream." >&2
    exit 2
  fi
  # Fail OPEN for everything else. Three of those branches mean the gate SKIPPED
  # a check because it could not read the call, which is exactly the "operator
  # believes the gate is running" half of #323, so each emits one stderr line
  # the way the resolver degrade does (#244):
  #   - the payload was not parseable JSON;
  #   - the extractor returned no complete record;
  #   - the call IS a write-class tool, but its `tool_input` is missing or is not
  #     a JSON object (a renamed key, a changed nesting). The JSON parses and the
  #     record is complete here, so neither note above covers it — and under the
  #     current schema Write/Edit always send a `tool_input` object, so this note
  #     fires zero times in normal operation.
  # Only two branches stay silent, and neither is a skipped check: a tool outside
  # the write-class list (a Read is not this gate's business) and a payload with
  # no `tool_name` at all. Those are ordinary traffic; a note on each would be
  # noise on every tool call.
  case "$tool_input_kind" in
    unparsed)
      _note "note: hook payload was not parseable JSON; the freshness gate was skipped for this call (fail-open)."
      ;;
    incomplete)
      _note "note: the payload extractor returned no complete record; the freshness gate was skipped for this call (fail-open)."
      ;;
    absent|other)
      if _is_write_tool "$tool_name"; then
        _note "note: the $tool_name payload carried no tool_input object, so no target path could be read; the freshness gate was skipped for this call (fail-open)."
      fi
      ;;
    *)
      # The extractor only ever writes one of the four kinds above, so an
      # unrecognised one means the record itself shifted — a NUL inside a JSON
      # string pushes the fields along by one. The OS rejects a path containing
      # a NUL, so the practical impact is zero, but exiting 0 with no note at
      # all would contradict the header's enumeration of what is silent.
      _note "note: the payload extractor returned an unrecognised record shape; the freshness gate was skipped for this call (fail-open)."
      ;;
  esac
  _allow
fi

# Normalise: check whether the target is facts/accepted.dl or facts/query.dl
# under the KB root. Match both absolute and relative paths.
#
# Use Python for portable path canonicalisation — realpath -m is GNU-only and
# is not available on macOS/BSD. os.path.realpath resolves symlinks and
# normalises . / .. segments on all platforms without requiring the path to
# exist (matching realpath -m semantics).
#
# The target and BOTH engine inputs are canonicalised and compared in ONE
# interpreter run. Three separate runs read the same, and cost three process
# spawns on a path that now executes for every Write/Edit in the session (before
# #323 the extractor always returned an empty target, so this block was never
# reached and its cost was never charged). The gate protects two files; it must
# not tax every write in every session to do it.
#
# String equality of the canonical paths is NOT sufficient. Two different path
# strings name the same file when:
#   - they are hard links or two spellings reaching one inode; or
#   - the filesystem folds case (APFS is case-insensitive by default on macOS,
#     as is NTFS), so <KB>/facts/Accepted.dl IS <KB>/facts/accepted.dl.
# The header's "TARGET is an engine input iff it resolves to ..." predicate is
# false under either unless the comparison asks the filesystem rather than the
# strings. st_dev/st_ino settles it whenever both paths exist; when the target
# does not exist yet (a first write) stat cannot answer, so a case-only
# difference is resolved by probing whether the engine-input DIRECTORY answers
# to a case-swapped spelling of its own name. The probe is read-only — it
# creates nothing inside the user's KB.
#
# On a case-SENSITIVE filesystem facts/Accepted.dl is a genuinely different
# file, and the probe correctly reports that, so this does not start denying
# legitimate writes on Linux.
GATE_MATCH_PY="
import os, sys

def canon(p):
    return os.path.realpath(os.path.abspath(os.path.expanduser(p)))

def folds_case(directory):
    # Read-only probe: does <directory> answer to a case-swapped spelling of its
    # own last component? Only meaningful for a name with cased characters, and
    # only asked when two canonical paths differ by case alone.
    parent, name = os.path.split(directory)
    swapped = name.swapcase()
    if not parent or not swapped or swapped == name:
        return False
    try:
        return os.path.samestat(os.stat(directory), os.stat(os.path.join(parent, swapped)))
    except OSError:
        return False

target = canon(sys.argv[1])
verdict = \"0\"
for raw in sys.argv[2:]:
    engine = canon(raw)
    if target == engine:
        verdict = \"1\"
        break
    try:
        if os.path.samestat(os.stat(target), os.stat(engine)):
            verdict = \"1\"
            break
    except OSError:
        pass
    if target.lower() == engine.lower() and folds_case(os.path.dirname(engine)):
        verdict = \"1\"
        break
# The canonical target is returned alongside the verdict because the bootstrap
# branch tests it with -e. NUL-separated for the same reason the extractor is: a
# path may legally contain a newline.
sys.stdout.write(verdict + \"\\0\" + target + \"\\0\")
"

# PREFILTER. The matcher above is one interpreter spawn, and it is charged to
# every Write/Edit in every session the plugin is installed in — while the thing
# it guards is two files in one KB. Almost every one of those writes can be
# ruled out in the shell, without spawning anything, by looking at the last path
# component: a path whose basename is neither accepted.dl nor query.dl cannot
# name an engine input.
#
# That rule holds only with every one of the seven guards below. Each was reached
# by construction — guards 3-7 against the naive
# `case "$target_path" in */accepted.dl)` form, guards 1-2 against an earlier
# version of this prefilter that had the other five. Every guard that IS a line
# of code is pinned by a case in tests/test_gate_check.sh: delete the line and at
# least one case goes red — with the exceptions below, which are stated rather
# than implied.
#   - NEITHER line of guard 6's basename computation is pinned, and the two are
#     invisible to this suite for opposite reasons.
#     The SPLIT is asserted by construction: deleting it changes nothing this
#     suite can observe (measured: it stays fully green), because the direction
#     it serves needs a host where os.path is ntpath. CASES 51/51b do not pin
#     the split; what they pin is the POSIX direction it must not break — a
#     literal backslash is an ordinary filename character and must not cause a
#     false deny — which is what rules out the obvious wrong implementation of
#     rewriting `\` to `/` before the match.
#     The ASSIGNMENT above it is invisible for the opposite reason. Delete it
#     and `base` is unset — which does NOT trip the `set -u` at the top of this
#     file, because substring-removal expansions are exempt: `${base##pattern}`
#     on an unset name yields the empty string. That this is a property of the
#     expansion FORM, and not of one interpreter, is shown by the contrast
#     WITHIN a single shell: under the same `set -u`, both `${base##*/}` and
#     `${base##*\\}` yield empty at exit 0 while a plain `$base` errors.
#     The result is that every name hits guard 5's `''` arm, the short-circuit
#     never fires at all, and every call goes to the matcher. So the failure
#     mode of losing this line is "the prefilter stops OPTIMISING", never "the
#     prefilter stops GUARDING" — strictly more conservative, only slower, and
#     therefore invisible to a suite that reads exit codes.
#   - Guard 7's BSD `stat -f %l` fallback is unpinned for the same "safe
#     direction" reason: remove it and, on a host whose stat rejects the GNU
#     `-c %h` form, the query simply fails, the stat-failure arm below returns 1,
#     and the call goes to the matcher. Slower, never leakier — so an exit-code
#     suite cannot see it (measured: fully green without it). Note this is the
#     FALLBACK only. The stat-failure ARM it falls into is a different matter
#     and IS pinned, by CASE 39b, because neutering that one runs off the end of
#     the function into `return 0` and short-circuits a write to a hard link.
#   - Guard 4 is not a line of code at all; it is the decision NOT to strip
#     trailing separators. It is pinned in the direction that can do damage —
#     CASE 46b goes red if a strip is reintroduced anywhere AFTER the -L test —
#     but not in the other, because a strip placed before -L changes no verdict
#     and only buys the short-circuit back for directory-shaped names.
#
# TWO of the three are here because removing them is less OPTIMISING, never less
# GUARDING. That is the test to apply to any future exception, and it is the one
# that would have caught guard 7's stat-failure arm: compare the `stat -f`
# fallback, which merely sends more calls to the matcher, against the ARM it
# falls into, which lets a call run off the end of this function into
# `return 0`. The first is a bullet above; the second is CASE 39b.
#
# The backslash split does NOT fit that test, and its bullet deliberately does
# not claim it. It is here for the other reason — the direction it serves cannot
# be reached from this host at all, so it is asserted by construction. On a Git
# Bash host, removing it WOULD be less guarding: with os.path as ntpath,
# `C:\kb\facts\accepted.dl` canonicalises to the engine input, but with no split
# the whole string survives as the basename, matches neither engine name, and
# short-circuits.
#
# Either justification has to be stated explicitly, and there are only those
# two. "Hard to reach" on its own is NOT one of them — that is what the
# stat-failure arm looked like, and it failed open.
#
# The rule the guards exist to protect is: canon(target) can only equal
# canon(engine) if their BASENAMES agree, because realpath preserves the final
# component. Guards 3-7 are the five ways the TARGET's name can lie about the
# file it denotes. The other two are not name questions: guard 1 is about the
# path string not yet having been through expanduser, and guard 2 is about the
# ENGINE side rather than the target.
#
#   1. TILDE. canon() runs expanduser; nothing in this function does. A leading
#      "~" therefore makes -L, -e and stat interrogate a literal "~/…" that does
#      not exist, and all three answer "no symlink, no hard link" — silently
#      disabling guards 3 and 7. Rather than reimplement expanduser's "~" and
#      "~user" semantics in shell, decline to short-circuit at all. Applies to
#      KB_ROOT too, which reaches the engine paths below.
#   2. A SYMLINKED ENGINE INPUT. The guards below ask whether the TARGET is a
#      symlink; they say nothing about whether accepted.dl or query.dl is one.
#      With `facts/query.dl -> ../shared/my-queries.dl`, canon(engine) ends in
#      my-queries.dl, so a write to that file is an engine-input write whose
#      basename this function has never heard of. Only the final component can
#      do this — a symlinked parent redirects the directory but preserves the
#      basename — so testing exactly those two paths is sufficient. Note
#      compile_facts.py temp+os.replace()s accepted.dl, which breaks a symlink
#      there on the next compile; query.dl is user-authored and never replaced,
#      so a symlink on it persists indefinitely.
#      COST. This guard is about the KB, not about the target, so it answers the
#      same way for every call in that KB — and it sits at the top of this
#      function. Symlinking either engine input therefore switches the
#      short-circuit OFF wholesale: every Write/Edit in that KB pays the matcher
#      spawn, not just writes that could plausibly be the engine input. That is
#      the correct trade (the alternative is not seeing the write at all), but it
#      is the one configuration in which this prefilter stops buying anything.
#   3. SYMLINK. A symlink's own name says nothing about what it resolves to.
#      `facts/notes.dl -> accepted.dl` has basename notes.dl and canonicalises
#      to the engine input; the prefilter runs BEFORE canonicalisation, so it
#      cannot "see the resolved basename". `-L` is a shell builtin test, so
#      falling through on one costs nothing. Only the final component matters:
#      a symlinked PARENT directory does not change the basename.
#   4. TRAILING SEPARATORS. "…/accepted.dl/" canonicalises to the engine input
#      but does not match a `*/accepted.dl` glob. Nothing is stripped: a
#      trailing separator makes `${path##*/}` empty, and an empty basename
#      already falls through on guard 5's arm. An earlier version DID strip,
#      which bought the short-circuit back for these names — but a name ending
#      in a separator denotes a DIRECTORY, which is not a shape Write/Edit can
#      act on, so that saving is on traffic which does not arrive. What it cost
#      was an ordering constraint: the strip had to run BEFORE guards 3 and 7,
#      because `-L` on a name ending in a separator resolves the link and
#      answers "no". Getting that order wrong opened a real hole (a symlink
#      with a trailing slash short-circuited). Not stripping is the more
#      conservative reading of the name and leaves no order to get wrong.
#   5. DOT COMPONENTS. "…/accepted.dl/." canonicalises to the engine input while
#      its basename is ".". An empty, "." or ".." basename tells us nothing, so
#      fall through.
#   6. CASE. On a case-folding filesystem Accepted.dl IS the engine input, so a
#      case-sensitive glob here would silently undo the matcher's case handling.
#      The alternatives are spelled out per character rather than via `tr` (a
#      process spawn on the hot path) or `shopt -s nocasematch` (global state in
#      a safety gate). ASCII is sufficient: the two names are pure ASCII, and no
#      non-ASCII codepoint case-folds into one.
#   7. HARD LINKS. A second name for one inode is invisible to every test above,
#      so the link count has to be asked for. `stat` costs ~3ms against the
#      ~70ms interpreter spawn this prefilter exists to avoid — 4% of the saving
#      to keep the guarantee. Only an existing file can carry one; a path we
#      cannot stat for any other reason falls through.
#
# Every uncertain shape falls THROUGH to the matcher. Over-matching costs one
# spawn and can only make the gate stricter, so it is always the safe direction.
# The short-circuit fires only once all seven guards have been cleared — the
# five ways a name can diverge from its canonical basename, plus the two that
# are not name questions at all. Every guard reads the path string
# exactly as it arrived — none rewrites it for the next one — so there is no
# ordering constraint left to get wrong. This is still not the same as proof: a
# parent directory this process cannot stat would make -L and stat answer "no"
# for a reason other than the truth. That case cannot be reached by a write the
# same process could perform, so it is left uncovered rather than guarded.
_cannot_be_engine_input() {
  local path="$1"
  case "$path" in ~*) return 1 ;; esac
  case "$KB_ROOT" in ~*) return 1 ;; esac
  [ -L "${KB_ROOT}/facts/accepted.dl" ] && return 1
  [ -L "${KB_ROOT}/facts/query.dl" ] && return 1
  # `$path` is never rewritten before the filesystem guards — in particular no
  # trailing separator is stripped (guard 4). A trailing separator makes the
  # shell's -L resolve the link, so `notes.dl/` answers "not a symlink" while
  # realpath() still lands on accepted.dl; a strip would have to run before this
  # test to fix that, and a strip placed after it silently reopens the hole.
  # Instead such a name yields an empty basename below and falls through.
  [ -L "$path" ] && return 1
  # Split on BOTH separators. Python's os.path treats a backslash as a separator
  # on Windows, so under Git Bash "C:\kb\facts\accepted.dl" canonicalises to the
  # engine input while `${path##*/}` hands back the whole string.
  local base="${path##*/}"
  base="${base##*\\}"
  case "$base" in
    ''|.|..) return 1 ;;
    [Aa][Cc][Cc][Ee][Pp][Tt][Ee][Dd].[Dd][Ll]) return 1 ;;
    [Qq][Uu][Ee][Rr][Yy].[Dd][Ll]) return 1 ;;
  esac
  if [ -e "$path" ]; then
    local links
    if links="$(stat -c %h "$path" 2>/dev/null)" || links="$(stat -f %l "$path" 2>/dev/null)"; then
      [ "$links" = "1" ] || return 1
    else
      return 1
    fi
  fi
  return 0
}

if _cannot_be_engine_input "$target_path"; then
  _allow
fi

# Permissive degrade (enumerated in the header): if the matcher cannot run, fall
# back to a raw string comparison and the raw target, which can only make a
# match LESS likely.
is_engine_input=false
match_verdict=""
abs_target=""
if ! { IFS= read -r -d '' match_verdict && IFS= read -r -d '' abs_target; } \
    < <("${PYTHON_RUNNER[@]}" -c "$GATE_MATCH_PY" \
        "$target_path" "${KB_ROOT}/facts/accepted.dl" "${KB_ROOT}/facts/query.dl" 2>/dev/null); then
  match_verdict=""
  abs_target="$target_path"
fi

case "$match_verdict" in
  1) is_engine_input=true ;;
  0) ;;
  *)
    _note "note: the path canonicaliser returned no verdict; the engine-input match fell back to a raw string comparison (more permissive) for this call."
    abs_target="$target_path"
    for engine_file in "${KB_ROOT}/facts/accepted.dl" "${KB_ROOT}/facts/query.dl"; do
      if [ "$target_path" = "$engine_file" ]; then
        is_engine_input=true
        break
      fi
    done
    ;;
esac

# If the target is not an engine input file, allow the tool to proceed.
if [ "$is_engine_input" = false ]; then
  _allow
fi

report="${KB_ROOT}/facts/logic_report.txt"
accepted="${KB_ROOT}/facts/accepted.dl"
query="${KB_ROOT}/facts/query.dl"

# A report of a run in which THE ENGINE NEVER RAN (#338). run_logic_check.py
# writes one when it cannot reach the engine, so that a previous run's report is
# not left on disk to be read as this run's result. Such a report carries no
# finding about the KB, so this gate treats it as no report: it neither
# satisfies freshness nor cancels bootstrap.
#
# The match is on the WHOLE line, fixed-string (`-qxF`), against the constant
# run_logic_check.ENGINE_FAILED_STATUS_LINE. `grep -q` returns 1 for no match
# and 2 for an unreadable file; both mean "do not treat this as failed", which
# is the permissive answer — but a file that cannot be read has already failed
# `-f` at the call sites below, or is about to fail the mtime branch, so this
# opens nothing the freshness predicate does not already close.
#
# WHOLE line, not substring: the report interpolates KB text, and a value that
# merely CONTAINS the marker must not deny — `grep -qF` here would let any KB
# whose data mentions the marker lock its own engine inputs.
#
# Trailing CRs are stripped first, because this predicate fails OPEN on a
# mismatch. A report written with CRLF endings — which is what text-mode writing
# produces on Windows — did not match, so the gate returned 0 and handed out
# edit rights on engine inputs at the moment the engine was broken.
# run_logic_check now pins LF on the writing side; this is the other half, so a
# report from either side reads the same. Measured: with CRLF, exit 0 before
# this line and 2 after.
#
# TRAILING only, and identical to cli.py's rule — see _records_engine_failure.
# The two readers must agree on every report, so "close enough" is the one thing
# this cannot be.
# THREE-VALUED, and the third value is the point:
#   0 — the report records a run in which the engine never ran
#   1 — it does not
#   2 — CANNOT JUDGE; every caller must treat this as deny.
#
# It used to be two-valued, `sed | grep`, and "cannot judge" collapsed into "no
# marker" — which is the ALLOW side. One NUL byte anywhere in the report was
# enough: BSD sed aborts on it, and under `set -euo pipefail` the pipeline is
# non-zero EVEN WHEN GREP MATCHED, so the gate concluded there was no marker,
# fell through to the mtime branch, found the report fresh and permitted a write
# that origin/main refused. An unreadable report (chmod 000) did the same, and
# the old comment's defence — that such a file was already caught by `-f` or
# would be caught by the mtime branch — is false when measured: `-f` does not
# test readability and `stat` still answers. Blocker 2 forged the marker; this
# erased it. Both end with the gate wrong about the same file.
#
# Judged in PYTHON, not sed/grep, for three reasons:
#   - it is the only way to tell "no marker" from "could not look", which is the
#     whole fix; grep's exit 2 does not survive a pipeline and `set -o pipefail`
#     cannot distinguish which stage failed;
#   - the comparison is then byte-for-byte the same operation factlog/cli.py
#     performs — same split, same rstrip, same equality — so the two readers
#     agree by construction rather than by two texts being kept in sync;
#   - this hook already REQUIRES Python 3.11+ and denies without it (fail-closed
#     branch 1 above), so it adds no dependency. Nothing here imports factlog:
#     it is stdlib only, so a broken package cannot turn the judgement into a
#     crash. (Sharing one predicate with cli.py is #364, deliberately not here.)
#
# The verdict is read from STDOUT, not from the exit status, because Python
# exits 1 on an uncaught traceback and 1 is a verdict — "no marker", the allow
# side. A token that must be printed cannot be produced by a crash.
_records_engine_failure() {
  local out status=0
  out=$(FACTLOG_GATE_REPORT="$1" "${PYTHON_RUNNER[@]}" -c '
import os, sys
try:
    raw = open(os.environ["FACTLOG_GATE_REPORT"], "rb").read()
except Exception:
    sys.exit(3)
marker = b"status: engine-did-not-run"
hit = any(line.rstrip(b"\r") == marker for line in raw.split(b"\n"))
print("factlog-report-failed" if hit else "factlog-report-ok")
' 2>/dev/null) || status=$?
  if [ "$status" -ne 0 ]; then
    return 2
  fi
  case "$out" in
    factlog-report-failed) return 0 ;;
    factlog-report-ok)     return 1 ;;
    *)                     return 2 ;;
  esac
}

# Bytes, and TRAILING CRs only — identical to factlog/cli.py's rule.
# `tr -d '\r'` deleted every CR anywhere, and that is not a weaker version of the
# same rule but a different one: it MANUFACTURES the marker out of a line that is
# not the marker. A report line reading "sta<CR>tus: engine-did-not-run" matched
# here and did not in cli.py — the gate denying a completed run while status
# called the same file normal.
#
# Reading `rb` and comparing bytes also means no decoding step can change the
# verdict: undecodable bytes are simply unequal to the marker, where a decoder
# told to ignore errors would delete them and could make a non-marker line into
# one.
report_verdict=0
if [ -f "$report" ]; then
  _records_engine_failure "$report" || report_verdict=$?
fi

# BOOTSTRAP (predicate branch B): a fresh KB has no report of a completed engine
# run, and does not yet have the engine input being created. `factlog init`
# seeds neither file, so the FIRST creation of facts/query.dl (or
# facts/accepted.dl) cannot possibly be preceded by such a report. Allow it; the
# stale-guard takes over once a real report exists. We test the on-disk
# existence of the *target* (not the path string) so this only relaxes the
# genuine first-write case.
#
# A FAILED report does not count as a report here. Without that, running
# /factlog check in a fresh KB — which fails, because facts/accepted.dl does not
# exist yet — would drop a report into facts/ and thereby DENY the very first
# creation of facts/query.dl, a write this gate has always allowed.
# Verdict 2 (cannot judge) deliberately does NOT satisfy this branch: a report we
# could not read is not evidence that this is a fresh KB, so the call falls
# through to the deny below rather than being waved past as a first write.
if { [ ! -f "$report" ] || [ "$report_verdict" -eq 0 ]; } && [ ! -e "$abs_target" ]; then
  _allow
fi

# Predicate: report must exist, record a completed engine run, and be newer than
# the most recently modified engine input file (accepted.dl or query.dl).
if [ ! -f "$report" ]; then
  echo "[factlog GATE] DENIED: facts/logic_report.txt does not exist." >&2
  echo "  An engine input already exists but no report supersedes it." >&2
  echo "  Run /factlog check (\"\${CLAUDE_PLUGIN_ROOT}\"/tools/factlog_python.sh \"\${CLAUDE_PLUGIN_ROOT}\"/tools/run_logic_check.py)" >&2
  echo "  to produce a fresh report before editing engine inputs." >&2
  exit 2
fi

# FAILED report (#338) — checked BEFORE the mtime comparison, because this
# report IS fresh: /factlog check has just written it. Re-running the check
# cannot produce a fresh one until the cause is fixed, so "stale" would be both
# untrue and useless advice, and the deny message must carry the cause instead.
if [ "$report_verdict" -eq 0 ]; then
  echo "[factlog GATE] DENIED: the last logic check could not run the engine." >&2
  echo "  facts/logic_report.txt records the failure, not a result — so it does" >&2
  echo "  not supersede facts/accepted.dl or facts/query.dl." >&2
  # 2>/dev/null and `|| true`: this is a courtesy line, and the deny above does
  # not depend on it. sed aborts on the same NUL-bearing input that used to break
  # the predicate, and under `set -e` that would take the whole script down
  # BEFORE the exit 2 below — turning a deny into a crash on exactly the input
  # this branch was hardened against.
  # Redirect order matters: `>&2` first duplicates the REAL stderr onto stdout,
  # then `2>/dev/null` silences sed's own errors. Written the other way round,
  # `>&2` would inherit the already-silenced fd 2 and the reason would vanish
  # into /dev/null.
  sed -n 's/^reason: /  reason: /p' "$report" >&2 2>/dev/null || true
  echo "  Fix that cause and re-run /factlog check; re-running it unchanged will" >&2
  echo "  fail the same way. Recovery for a KB that cannot produce a report at" >&2
  echo "  all is in docs/guide/determinism.md (Bash is not gated)." >&2
  exit 2
fi

# CANNOT JUDGE — deny, and say so honestly rather than guessing. Reaching here
# means the report exists but could not be read or produced no verdict
# (unreadable, or an interpreter that died). The old code called that "no
# marker", which is the ALLOW side of a guard whose entire job is to withhold
# write access when the KB's state is unknown.
if [ "$report_verdict" -ne 1 ]; then
  echo "[factlog GATE] DENIED: facts/logic_report.txt could not be judged." >&2
  echo "  The file exists but the gate could not read a verdict from it, so it" >&2
  echo "  cannot be treated as a report of a completed run." >&2
  echo "  Check that it is readable and is text; re-run /factlog check to rewrite it." >&2
  exit 2
fi

# Fail-closed branch 3 (see header): if stat cannot report an mtime we cannot
# compare freshness, so we deny. The `exit 2` inside this command substitution
# ends the whole script under `set -e`. There is no escape hatch here — the file
# has already passed `-f` a line earlier, so reaching this needs a race or an
# unreadable filesystem, not a schema change someone has to work around.
_mtime() {
  local value
  if value="$(stat -c %Y "$1" 2>/dev/null)" || value="$(stat -f %m "$1" 2>/dev/null)"; then
    printf '%s\n' "$value"
    return 0
  fi
  echo "[factlog GATE] DENIED: could not read mtime for $1" >&2
  exit 2
}

# Find the most recently modified engine input file that exists.
newest_input_mtime=0
for f in "$accepted" "$query"; do
  if [ -f "$f" ]; then
    mtime="$(_mtime "$f")"
    if [ "$mtime" -gt "$newest_input_mtime" ]; then
      newest_input_mtime="$mtime"
    fi
  fi
done

report_mtime="$(_mtime "$report")"

if [ "$report_mtime" -lt "$newest_input_mtime" ]; then
  echo "[factlog GATE] DENIED: facts/logic_report.txt is stale." >&2
  echo "  The report predates the last modification to facts/accepted.dl or facts/query.dl." >&2
  echo "  Run /factlog check (\"\${CLAUDE_PLUGIN_ROOT}\"/tools/factlog_python.sh \"\${CLAUDE_PLUGIN_ROOT}\"/tools/run_logic_check.py)" >&2
  echo "  to refresh the report before editing engine inputs." >&2
  exit 2
fi

# Report is fresh — allow the write/edit to proceed.
_allow
