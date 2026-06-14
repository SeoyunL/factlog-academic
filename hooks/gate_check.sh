#!/usr/bin/env bash
# factlog PreToolUse gate — deny writes to engine inputs when logic_report.txt
# is absent or stale.
#
# Fires BEFORE Write|Edit. If the tool is about to touch facts/accepted.dl or
# facts/query.dl, this script checks that facts/logic_report.txt exists and is
# newer than both files. If the predicate fails it exits 2, which Claude Code
# interprets as a permissionDecision=deny and blocks the tool call.
#
# FALSIFIABLE predicate (per CRITIC M4):
#   DENY iff:
#     1. the tool target path ends with facts/accepted.dl OR facts/query.dl, AND
#     2. facts/logic_report.txt does not exist OR is older than facts/accepted.dl
#        or facts/query.dl (whichever was most recently modified).
#
# This predicate is falsifiable: running /factlog check (which calls
# run_logic_check.py and writes a fresh logic_report.txt) will satisfy the
# condition and allow the next Write/Edit to proceed.

set -euo pipefail

payload="$(cat)"

# Determine the KB root: prefer FACTLOG_ROOT, fall back to cwd.
KB_ROOT="${FACTLOG_ROOT:-.}"

# Extract the tool target from the hook payload.
# Claude Code sends the tool input as JSON on stdin.
# The relevant field is "file_path" for Write and "file_path" for Edit.
target_path=""
if command -v python3 &>/dev/null; then
  target_path="$(printf '%s' "$payload" | python3 -c \
    "import json,sys; d=json.load(sys.stdin); print(d.get('file_path','') or d.get('path',''))" \
    2>/dev/null || true)"
fi

# If we could not extract a path, allow the tool to proceed (fail open).
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

# Predicate: report must exist and be newer than the most recently modified
# engine input file (accepted.dl or query.dl).
if [ ! -f "$report" ]; then
  echo "[factlog GATE] DENIED: facts/logic_report.txt does not exist." >&2
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
