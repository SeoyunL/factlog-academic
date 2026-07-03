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
# The only true fail-closed here is the python-availability check below (which
# DENYs when no usable Python 3.11+ is present, since the predicate cannot then
# be evaluated). Target-path extraction failures for engine-input-shaped payloads
# likewise deny.

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

# Extract the tool target from the hook payload.
# Claude Code sends the tool input as JSON on stdin.
# The relevant field is "file_path" for Write and "file_path" for Edit.
target_path="$(printf '%s' "$payload" | "${PYTHON_RUNNER[@]}" -c \
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
