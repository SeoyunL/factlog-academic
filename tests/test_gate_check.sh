#!/usr/bin/env bash
# Behavioral matrix for hooks/gate_check.sh
#
# Each case exercises a distinct branch of the deny predicate.
# Exit code 2 = DENY (expected for stale/absent cases).
# Exit code 0 = ALLOW (expected when report is fresh or target is not an engine input).
#
# Usage: bash tests/test_gate_check.sh
#   Returns 0 if all cases pass, 1 if any fail.

set -euo pipefail

GATE="$(cd "$(dirname "$0")/.." && pwd)/hooks/gate_check.sh"

pass=0
fail=0

# ---------------------------------------------------------------------------
# Helper: run gate for a given KB root, target file_path, and expected exit.
# ---------------------------------------------------------------------------
run_case() {
  local desc="$1"
  local kb_root="$2"
  local target_path="$3"
  local expected_exit="$4"

  local payload
  payload="$(printf '{"file_path":"%s"}' "$target_path")"

  local actual_exit=0
  FACTLOG_ROOT="$kb_root" bash "$GATE" <<< "$payload" >/dev/null 2>&1 || actual_exit=$?

  if [ "$actual_exit" -eq "$expected_exit" ]; then
    echo "PASS: $desc (exit $actual_exit)"
    pass=$((pass + 1))
  else
    echo "FAIL: $desc — expected exit $expected_exit, got $actual_exit"
    fail=$((fail + 1))
  fi
}

# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------
make_kb() {
  # Create a minimal KB skeleton at the given path.
  local root="$1"
  mkdir -p "$root/facts"
}

touch_file() {
  local path="$1"
  touch "$path"
}

set_mtime_past() {
  # Set file mtime to 1 second in the past relative to now.
  # Uses touch -t on macOS/BSD (YYYYMMDDHHMMSS).
  local path="$1"
  local past
  past="$(python3 -c 'import time,datetime; t=time.time()-2; print(datetime.datetime.fromtimestamp(t).strftime("%Y%m%d%H%M.%S"))')"
  touch -t "$past" "$path"
}

set_mtime_future() {
  # Set file mtime to 2 seconds in the future.
  local path="$1"
  local future
  future="$(python3 -c 'import time,datetime; t=time.time()+2; print(datetime.datetime.fromtimestamp(t).strftime("%Y%m%d%H%M.%S"))')"
  touch -t "$future" "$path"
}

# ---------------------------------------------------------------------------
# CASE 1: target is not an engine input — always ALLOW
# ---------------------------------------------------------------------------
KB1="$(mktemp -d)"
make_kb "$KB1"
run_case "non-engine-input target — allow" \
  "$KB1" "$KB1/facts/candidates.csv" 0
rm -rf "$KB1"

# ---------------------------------------------------------------------------
# CASE 2: engine input, report absent — DENY
# ---------------------------------------------------------------------------
KB2="$(mktemp -d)"
make_kb "$KB2"
touch_file "$KB2/facts/accepted.dl"
# No logic_report.txt
run_case "engine input, report absent — deny" \
  "$KB2" "$KB2/facts/accepted.dl" 2
rm -rf "$KB2"

# ---------------------------------------------------------------------------
# CASE 3: engine input, report is fresh (newer than accepted.dl) — ALLOW
# ---------------------------------------------------------------------------
KB3="$(mktemp -d)"
make_kb "$KB3"
touch_file "$KB3/facts/accepted.dl"
set_mtime_past "$KB3/facts/accepted.dl"
touch_file "$KB3/facts/logic_report.txt"
# logic_report.txt gets current mtime → newer than accepted.dl
run_case "engine input, report fresh — allow" \
  "$KB3" "$KB3/facts/accepted.dl" 0
rm -rf "$KB3"

# ---------------------------------------------------------------------------
# CASE 4: engine input, report is stale (older than accepted.dl) — DENY
# ---------------------------------------------------------------------------
KB4="$(mktemp -d)"
make_kb "$KB4"
touch_file "$KB4/facts/logic_report.txt"
set_mtime_past "$KB4/facts/logic_report.txt"
touch_file "$KB4/facts/accepted.dl"
# accepted.dl gets current mtime → newer than report
run_case "engine input, report stale — deny" \
  "$KB4" "$KB4/facts/accepted.dl" 2
rm -rf "$KB4"

# ---------------------------------------------------------------------------
# CASE 5: query.dl target, report fresh — ALLOW
# ---------------------------------------------------------------------------
KB5="$(mktemp -d)"
make_kb "$KB5"
touch_file "$KB5/facts/query.dl"
set_mtime_past "$KB5/facts/query.dl"
touch_file "$KB5/facts/logic_report.txt"
run_case "query.dl target, report fresh — allow" \
  "$KB5" "$KB5/facts/query.dl" 0
rm -rf "$KB5"

# ---------------------------------------------------------------------------
# CASE 6: query.dl target, report stale — DENY
# ---------------------------------------------------------------------------
KB6="$(mktemp -d)"
make_kb "$KB6"
touch_file "$KB6/facts/logic_report.txt"
set_mtime_past "$KB6/facts/logic_report.txt"
touch_file "$KB6/facts/query.dl"
run_case "query.dl target, report stale — deny" \
  "$KB6" "$KB6/facts/query.dl" 2
rm -rf "$KB6"

# ---------------------------------------------------------------------------
# CASE 7: BOOTSTRAP — fresh KB (no logic_report.txt, query.dl does not yet
# exist) creating facts/query.dl — ALLOW.
#
# A `factlog init` KB seeds neither facts/logic_report.txt nor facts/query.dl,
# so the FIRST creation of query.dl cannot be preceded by a report. Denying it
# would deadlock the question->query-draft flow. The gate must allow it.
# ---------------------------------------------------------------------------
KB_BOOT="$(mktemp -d)"
make_kb "$KB_BOOT"
# No logic_report.txt, no query.dl on disk.
run_case "bootstrap: fresh KB creating query.dl — allow" \
  "$KB_BOOT" "$KB_BOOT/facts/query.dl" 0
rm -rf "$KB_BOOT"

# ---------------------------------------------------------------------------
# CASE 8: BOOTSTRAP companion — fresh KB creating facts/accepted.dl — ALLOW.
# ---------------------------------------------------------------------------
KB_BOOT2="$(mktemp -d)"
make_kb "$KB_BOOT2"
run_case "bootstrap: fresh KB creating accepted.dl — allow" \
  "$KB_BOOT2" "$KB_BOOT2/facts/accepted.dl" 0
rm -rf "$KB_BOOT2"

# ---------------------------------------------------------------------------
# CASE 9: STALE-GUARD vs bootstrap — query.dl already exists but no report
# (e.g. report was deleted) — DENY.
#
# This proves the bootstrap relaxation does NOT swallow the stale-guard: once
# an engine input exists on disk without a superseding report, the edit is
# denied. Only the genuine first-write (target absent) is allowed.
# ---------------------------------------------------------------------------
KB_STALE="$(mktemp -d)"
make_kb "$KB_STALE"
touch_file "$KB_STALE/facts/query.dl"
# query.dl now exists; no logic_report.txt → must DENY (not bootstrap).
run_case "existing query.dl, report absent — deny (stale-guard, not bootstrap)" \
  "$KB_STALE" "$KB_STALE/facts/query.dl" 2
rm -rf "$KB_STALE"

# ---------------------------------------------------------------------------
# CASE 10: REGRESSION — single-quote in KB root path, report stale — DENY
#
# This is the apostrophe-path regression added after the critic identified
# that the original mtime computation used Python source string interpolation
# (`'$f'` and `'$report'`), which broke with paths containing a single quote,
# causing the gate to fail open (allow) instead of denying.  The fix uses
# `sys.argv[1]` to pass the path as a shell argument, which is quote-safe.
# ---------------------------------------------------------------------------
TMPBASE="$(mktemp -d)"
KB7="${TMPBASE}/kb-test's-apostrophe"
mkdir -p "$KB7/facts"
touch_file "$KB7/facts/logic_report.txt"
set_mtime_past "$KB7/facts/logic_report.txt"
touch_file "$KB7/facts/accepted.dl"
# accepted.dl gets current mtime → report is stale → must DENY
run_case "single-quote in KB root, report stale — deny (apostrophe regression)" \
  "$KB7" "$KB7/facts/accepted.dl" 2
rm -rf "$TMPBASE"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $pass passed, $fail failed"
if [ "$fail" -gt 0 ]; then
  exit 1
fi
exit 0
