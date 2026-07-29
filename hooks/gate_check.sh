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
#   Let TARGET be the tool target path. TARGET is an "engine input" iff it
#   resolves to <KB_ROOT>/facts/accepted.dl OR <KB_ROOT>/facts/query.dl.
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
#   TARGET itself is read from the hook payload; when it cannot be read at all
#   the predicate above is undefined, and the narrow fail-closed rule described
#   under "fail-closed branches" below decides instead.
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
# If the resolver cannot run (e.g. the factlog package is unavailable), KB_ROOT
# safely degrades to the prior ${FACTLOG_ROOT:-.} behaviour (usually cwd). This
# is a fail-to-previous-behaviour, NOT a fail-closed: it opens no new hole beyond
# what existed before this resolver, but it is permissive for cross-KB writes.
# That degrade is made OBSERVABLE: when Python is available but the resolver
# returns empty (package import failure), a one-line stderr note is emitted so
# the silent permissive fallback is visible to an operator (see below).
# There are exactly TWO fail-closed branches, both with an env escape hatch:
#   1. The python-availability check below DENYs when no usable Python 3.11+ is
#      present, since the predicate cannot then be evaluated. Escape hatch:
#      FACTLOG_PYTHON (point it at a usable interpreter).
#   2. Target-path extraction DENYs only in the narrow case where the payload
#      carries a `tool_input` JSON OBJECT, `tool_name` is one of the write-class
#      tools this hook is registered for, and NO usable path can be read from
#      either `tool_input` or the top level. That combination means the payload
#      schema drifted out from under the extractor while a write was in flight,
#      so the predicate cannot be evaluated for a write that may well target an
#      engine input. Escape hatch: FACTLOG_GATE_FAIL_OPEN=1 (named in the deny
#      message, since a PreToolUse exit 2 stderr is fed back to the model and
#      without a stated recovery it becomes a retry loop).
# Everything else fails OPEN (exit 0): unparseable payloads, a missing/absent
# `tool_name`, a `tool_name` outside the write-class list, and a `tool_input`
# that is not a JSON object. Those cannot be distinguished from non-write
# traffic, and a gate protecting two files in one KB must not become a global
# Write/Edit outage.

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
  # opens no new hole) — but make it OBSERVABLE with a one-line stderr note so an
  # operator can see the resolver was bypassed. This does NOT change the
  # exit-code contract or path matching.
  echo "[factlog GATE] note: factlog config resolver unavailable; freshness gate falling back to \${FACTLOG_ROOT:-cwd} (KB_ROOT=$KB_ROOT)" >&2
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
# the others. NUL is the separator because a path may legally contain a newline.
# (Fields are read straight off a pipe: bash command substitution silently drops
# NUL bytes, so `$(...)` cannot be used to capture this.)
GATE_EXTRACT_PY="
import json, sys
PATH_KEYS = (\"file_path\", \"path\", \"notebook_path\")
try:
    payload = json.load(sys.stdin)
except Exception:
    payload = None
try:
    name = payload.get(\"tool_name\")
    tool_name = name if isinstance(name, str) else \"\"
except Exception:
    tool_name = \"\"
try:
    if not isinstance(payload, dict) or \"tool_input\" not in payload:
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
tool_input_kind="absent"
if ! { IFS= read -r -d '' tool_name \
    && IFS= read -r -d '' target_path \
    && IFS= read -r -d '' tool_input_kind; } \
    < <(printf '%s' "$payload" | "${PYTHON_RUNNER[@]}" -c "$GATE_EXTRACT_PY" 2>/dev/null); then
  # The extractor produced no complete record (e.g. the interpreter died). Treat
  # it as an unparseable payload: fail OPEN, same as before this change.
  tool_name=""
  target_path=""
  tool_input_kind="absent"
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
    # from which no path key could be read. The payload schema drifted; we
    # cannot tell whether it targets an engine input.
    if [ "${FACTLOG_GATE_FAIL_OPEN:-}" = "1" ]; then
      echo "[factlog GATE] note: could not read a target path from the $tool_name payload; FACTLOG_GATE_FAIL_OPEN=1 is set, so the write is allowed unchecked." >&2
      exit 0
    fi
    echo "[factlog GATE] DENIED: could not read a target path from the $tool_name tool payload." >&2
    echo "  The hook payload schema changed, so the freshness predicate cannot be evaluated" >&2
    echo "  and this write cannot be shown to miss facts/accepted.dl or facts/query.dl." >&2
    echo "  Re-run with FACTLOG_GATE_FAIL_OPEN=1 to bypass this specific check (the" >&2
    echo "  freshness deny still applies), and please report the payload shape upstream." >&2
    exit 2
  fi
  # Fail OPEN for everything else: unparseable payload, non-write tool, absent
  # tool_name, or a tool_input that is not a JSON object.
  exit 0
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

abs_target="$(_canon "$target_path")"

is_engine_input=false
for engine_file in "${KB_ROOT}/facts/accepted.dl" "${KB_ROOT}/facts/query.dl"; do
  abs_engine="$(_canon "$engine_file")"
  if [ "$abs_target" = "$abs_engine" ]; then
    is_engine_input=true
    break
  fi
done

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
if [ ! -f "$report" ] && [ ! -e "$abs_target" ]; then
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
exit 0
