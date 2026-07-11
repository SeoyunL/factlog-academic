#!/usr/bin/env bash
# tests/test_logic_check_statuses.sh — superseded rows in the logic report (#208)
#
# `factlog reject` / `factlog amend` retire a row by setting status=superseded.
# common.py defines that status (SUPERSEDED_STATUSES) and documents it as a
# legitimate non-engine row kept for audit. The report must therefore treat it
# as KNOWN and stay silent: warning once per retired row made the report
# unreadable in proportion to how much review work had been done.
#
# This is the end-to-end pin (report as written to disk). The vocabulary itself
# is pinned engine-free in tests/unit/test_run_logic_check.py::TestStatusWarnings,
# which is what runs in CI's pytest job — main() calls run_wirelog() BEFORE the
# warning loop, so no report is written at all without the engine.
#
# Pins:
#   (a) a superseded row produces NO "unknown status" warning
#   (b) a genuinely unrecognised status (a typo) still warns — the guard is not
#       simply removed
#   (c) engine input is unchanged: the accepted row IS in accepted.dl and the
#       superseded row is NOT (both halves asserted — a missing/empty
#       accepted.dl must fail, not pass silently)
#
# Usage: bash tests/test_logic_check_statuses.sh

set -euo pipefail

# One temp root, created in THIS shell. new_kb() runs inside a command
# substitution (a subshell), so it cannot register anything for cleanup itself —
# an array appended to there is lost, and the stale trap then returned non-zero
# and failed the run even when every assertion passed.
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

export XDG_CONFIG_HOME="$TMP_ROOT/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
COMPILE="$PLUGIN_ROOT/tools/compile_facts.py"
CHECK="$PLUGIN_ROOT/tools/run_logic_check.py"
HEADER="subject,relation,object,source,status,confidence,note"

# Skip cleanly if the engine is absent (offline installs, CI's shell job): main()
# runs wirelog before the warning loop, so without pyrewire no report exists to
# assert on. The unit tests cover the vocabulary in that case.
if ! "$PYTHON" -c "import pyrewire" >/dev/null 2>&1; then
  echo "SKIP: pyrewire not installed; test_logic_check_statuses requires the engine"
  exit 0
fi

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

new_kb() {  # $1 = candidates.csv body (rows after the header); echoes the KB root
  local kb
  kb="$(mktemp -d "$TMP_ROOT/kb.XXXXXX")/wiki"
  "$PYTHON" -m factlog init --target "$kb" >/dev/null
  printf 'a\n' > "$kb/sources/a.md"
  printf '%s\n%s' "$HEADER" "$1" > "$kb/facts/candidates.csv"
  echo "$kb"
}

run_check() {  # $1 = KB root. Compiles engine input, then writes the report.
  (                                    # errors are NOT swallowed: a broken
    cd "$1"                            # compile/check must fail the test loudly
    export FACTLOG_ROOT="$1"           # rather than leave an absent report for
    "$PYTHON" "$COMPILE" >/dev/null    # a later assertion to misread as "clean".
    "$PYTHON" "$CHECK" >/dev/null
  )
}

# ---------------------------------------------------------------- (a) and (c)
KB="$(new_kb 'PMID_1,개입_영양소,오메가-3,sources/a.md,accepted,0.90,
PMID_1,개입_영양소,EPA,sources/a.md,superseded,0.90,retired by amend
')"
run_check "$KB"
report="$(cat "$KB/facts/logic_report.txt")"

if grep -q "unknown status treated as non-engine input: superseded" <<<"$report"; then
  bad "(a) superseded warned as unknown status"
else
  ok "(a) superseded produces no unknown-status warning"
fi

# Both halves: the positive one is what stops (c) passing on an empty accepted.dl.
if ! grep -q '"오메가-3"' "$KB/facts/accepted.dl"; then
  bad "(c) accepted row missing from engine input — accepted.dl empty or broken"
elif grep -q '"EPA"' "$KB/facts/accepted.dl"; then
  bad "(c) superseded row reached engine input"
else
  ok "(c) engine input holds the accepted row and not the superseded one"
fi

# -------------------------------------------------------------------- (b)
KB2="$(new_kb 'PMID_1,개입_영양소,오메가-3,sources/a.md,bogus,0.90,typo status
')"
run_check "$KB2"

if grep -q "unknown status treated as non-engine input: bogus" "$KB2/facts/logic_report.txt"; then
  ok "(b) an unrecognised status still warns"
else
  bad "(b) unrecognised status 'bogus' was not warned — guard lost"
fi

echo "---"
echo "passed: $pass, failed: $fail"
[ "$fail" -eq 0 ]
