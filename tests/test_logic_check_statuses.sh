#!/usr/bin/env bash
# tests/test_logic_check_statuses.sh — run_logic_check status vocabulary (#208)
#
# `factlog reject` / `factlog amend` retire a row by setting status=superseded.
# common.py defines that status (SUPERSEDED_STATUSES) and documents it as a
# legitimate non-engine row kept for audit. run_logic_check must therefore treat
# it as KNOWN and stay silent: warning once per retired row made the report
# unreadable in proportion to how much review work had been done.
#
# Pins:
#   (a) a superseded row produces NO "unknown status" warning
#   (b) a genuinely unrecognised status (a typo) still warns — the guard is not
#       simply removed
#   (c) the superseded row stays out of engine input either way
#
# Synthetic data only (no pyrewire needed: the warning path runs before the
# engine and the report is written regardless).
# Usage: bash tests/test_logic_check_statuses.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
COMPILE="$PLUGIN_ROOT/tools/compile_facts.py"
CHECK="$PLUGIN_ROOT/tools/run_logic_check.py"
HEADER="subject,relation,object,source,status,confidence,note"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

run_check() {  # $1 = KB root; compiles engine input, then prints the report
  (
    cd "$1"
    export FACTLOG_ROOT="$1"
    "$PYTHON" "$COMPILE" >/dev/null 2>&1
    "$PYTHON" "$CHECK" >/dev/null 2>&1 || true  # non-zero without pyrewire; the report is still written
  )
  cat "$1/facts/logic_report.txt"
}

# ---------------------------------------------------------------- (a) + (c)
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'a\n' > "$KB/sources/a.md"
printf '%s\n%s\n%s\n' "$HEADER" \
  'PMID_1,개입_영양소,오메가-3,sources/a.md,accepted,0.90,' \
  'PMID_1,개입_영양소,EPA,sources/a.md,superseded,0.90,retired by amend' \
  > "$KB/facts/candidates.csv"

report="$(run_check "$KB")"

if grep -q "unknown status treated as non-engine input: superseded" <<<"$report"; then
  bad "(a) superseded warned as unknown status"
else
  ok "(a) superseded produces no unknown-status warning"
fi

if grep -q "EPA" "$KB/facts/accepted.dl" 2>/dev/null; then
  bad "(c) superseded row reached engine input"
else
  ok "(c) superseded row stays out of engine input"
fi

# -------------------------------------------------------------------- (b)
KB2="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB2" >/dev/null
printf 'a\n' > "$KB2/sources/a.md"
printf '%s\n%s\n' "$HEADER" \
  'PMID_1,개입_영양소,오메가-3,sources/a.md,bogus,0.90,typo status' \
  > "$KB2/facts/candidates.csv"

report2="$(run_check "$KB2")"

if grep -q "unknown status treated as non-engine input: bogus" <<<"$report2"; then
  ok "(b) an unrecognised status still warns"
else
  bad "(b) unrecognised status 'bogus' was not warned — guard lost"
fi

echo "---"
echo "passed: $pass, failed: $fail"
[ "$fail" -eq 0 ]
