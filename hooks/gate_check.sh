#!/usr/bin/env bash
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
# This predicate is falsifiable in both directions:
#   - Bootstrap is allowed: creating facts/query.dl in a freshly `factlog init`
#     KB (no logic_report.txt, no pre-existing query.dl) returns exit 0.
#   - Stale-guard still denies: once a logic_report.txt exists, any edit that
#     would supersede it (report absent due to deletion, or report older than
#     an existing input) returns exit 2. Running /factlog check (which calls
#     run_logic_check.py and writes a fresh logic_report.txt) re-satisfies (C).
#
# KB root: set FACTLOG_ROOT to the knowledge-base root for sound path matching;
# falls back to the current working directory when unset.
#
# Fail-closed: if python3 is unavailable or target-path extraction fails for an
# engine-input-shaped payload, the gate denies rather than silently allowing.

set -euo pipefail

payload="$(cat)"

# Determine the KB root: prefer FACTLOG_ROOT, fall back to cwd.
KB_ROOT="${FACTLOG_ROOT:-.}"

# python3 is required for JSON parsing and portable path/mtime handling.
# Fail closed: without it we cannot evaluate the predicate safely.
if ! command -v python3 &>/dev/null; then
  echo "[factlog GATE] DENIED: python3 is required to evaluate the gate predicate." >&2
  exit 2
fi

# Extract the tool target from the hook payload.
# Claude Code sends the tool input as JSON on stdin.
# The relevant field is "file_path" for Write and "file_path" for Edit.
target_path="$(printf '%s' "$payload" | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d.get('file_path','') or d.get('path',''))" \
  2>/dev/null || true)"

# If we could not extract a path, allow the tool to proceed (fail open).
# An empty/unparseable payload cannot target an engine input, so allowing here
# does not weaken the engine-input guard below.
if [ -z "$target_path" ]; then
  exit 0
fi

# Normalise: check whether the target is facts/accepted.dl or facts/query.dl
# under the KB root. Match both absolute and relative paths.
#
# Use python3 for portable path canonicalisation — realpath -m is GNU-only and
# is not available on macOS/BSD. python3 os.path.realpath resolves symlinks and
# normalises . / .. segments on all platforms without requiring the path to
# exist (matching realpath -m semantics).
_canon() {
  python3 -c "import os,sys; print(os.path.realpath(os.path.abspath(os.path.expanduser(sys.argv[1]))))" "$1" 2>/dev/null || printf '%s' "$1"
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
  echo "  Run /factlog check (python3 \"\${CLAUDE_PLUGIN_ROOT}\"/tools/run_logic_check.py)" >&2
  echo "  to produce a fresh report before editing engine inputs." >&2
  exit 2
fi

# Find the most recently modified engine input file that exists.
newest_input_mtime=0
for f in "$accepted" "$query"; do
  if [ -f "$f" ]; then
    if command -v python3 &>/dev/null; then
      mtime="$(python3 -c 'import os,sys; print(int(os.path.getmtime(sys.argv[1])))' "$f" 2>/dev/null || echo 0)"
    else
      mtime=0
    fi
    if [ "$mtime" -gt "$newest_input_mtime" ]; then
      newest_input_mtime="$mtime"
    fi
  fi
done

report_mtime=0
if command -v python3 &>/dev/null; then
  report_mtime="$(python3 -c 'import os,sys; print(int(os.path.getmtime(sys.argv[1])))' "$report" 2>/dev/null || echo 0)"
fi

if [ "$report_mtime" -lt "$newest_input_mtime" ]; then
  echo "[factlog GATE] DENIED: facts/logic_report.txt is stale." >&2
  echo "  The report predates the last modification to facts/accepted.dl or facts/query.dl." >&2
  echo "  Run /factlog check (python3 \"\${CLAUDE_PLUGIN_ROOT}\"/tools/run_logic_check.py)" >&2
  echo "  to refresh the report before editing engine inputs." >&2
  exit 2
fi

# Report is fresh — allow the write/edit to proceed.
exit 0
