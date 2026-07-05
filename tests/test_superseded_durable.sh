#!/usr/bin/env bash
# tests/test_superseded_durable.sh — superseded tombstone durability (#260)
#
# check_conflicts.py documents that a row marked 'superseded' "stays for audit".
# But candidates.csv was a pure projection of runs/*.json, so the preservation
# only re-marked rows a run RE-ASSERTS. When the backing run was replaced/removed
# (e.g. a source re-extracted without the fact), the superseded row vanished and a
# later re-assertion resurrected it as 'candidate' — silently undoing a human's
# conflict resolution and letting a retired fact re-enter the candidate pool.
#
# Pins:
#   - a superseded row survives a merge whose runs OMIT the fact (tombstone durable)
#   - after that, a run that RE-ASSERTS the fact does not resurrect it as candidate
#   - the realistic path (runs accumulate; original run stays) is unaffected
#
# Deterministic; no pyrewire required.  Usage: bash tests/test_superseded_durable.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
MERGE="$PLUGIN_ROOT/tools/merge_candidates.py"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
mkdir -p "$KB/sources"; printf 'doc' > "$KB/sources/doc.md"
CSV="$KB/facts/candidates.csv"

merge() { "$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1 || true; }
run_json() { printf '%s' "$2" > "$KB/runs/$1"; }
has_status() { grep -E "^A,knows,B," "$CSV" 2>/dev/null | head -1 | cut -d, -f5; }

FACT='[{"subject":"A","relation":"knows","object":"B","source":"sources/doc.md","status":"candidate","confidence":0.9,"note":""}]'
OTHER='[{"subject":"C","relation":"knows","object":"D","source":"sources/doc.md","status":"candidate","confidence":0.9,"note":""}]'

# 1. assert the fact, merge, then a human marks it superseded.
run_json 20260101-r1.json "$FACT"
merge
"$PYTHON" - "$CSV" <<'PY'
import sys, csv, pathlib
p = pathlib.Path(sys.argv[1]); rows = list(csv.DictReader(p.open()))
for r in rows:
    if (r["subject"], r["relation"], r["object"]) == ("A", "knows", "B"):
        r["status"] = "superseded"
with p.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
PY
[ "$(has_status)" = "superseded" ] && ok "setup: fact marked superseded" || bad "setup: expected superseded, got '$(has_status)'"

# 2. the backing run is REPLACED by an extraction that omits the fact.
run_json 20260101-r1.json '[]'
merge
[ "$(has_status)" = "superseded" ] && ok "#260: superseded tombstone survives a run that omits the fact" \
  || bad "#260: tombstone lost when run omits the fact (got '$(has_status)')"

# 3. a still-later run re-asserts the fact: must NOT resurrect as candidate.
run_json 20260303-r3.json "$FACT"
merge
[ "$(has_status)" = "superseded" ] && ok "#260: re-asserted retired fact stays superseded (no resurrection)" \
  || bad "#260: retired fact resurrected as '$(has_status)'"

# 4. realistic path regression: original run stays + a new run omitting the fact.
KB2="$(mktemp -d)/wiki"; "$PYTHON" -m factlog init --target "$KB2" >/dev/null
mkdir -p "$KB2/sources"; printf 'doc' > "$KB2/sources/doc.md"
CSV2="$KB2/facts/candidates.csv"
printf '%s' "$FACT" > "$KB2/runs/20260101-r1.json"
"$PYTHON" "$MERGE" --wiki "$KB2" >/dev/null 2>&1 || true
"$PYTHON" - "$CSV2" <<'PY'
import sys, csv, pathlib
p = pathlib.Path(sys.argv[1]); rows = list(csv.DictReader(p.open()))
for r in rows:
    if (r["subject"], r["relation"], r["object"]) == ("A", "knows", "B"):
        r["status"] = "superseded"
with p.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
PY
printf '%s' "$OTHER" > "$KB2/runs/20260202-r2.json"
"$PYTHON" "$MERGE" --wiki "$KB2" >/dev/null 2>&1 || true
s2="$(grep -E '^A,knows,B,' "$CSV2" | head -1 | cut -d, -f5)"
[ "$s2" = "superseded" ] && ok "#260: realistic accumulate-runs path still preserves superseded" \
  || bad "#260: realistic path regressed (got '$s2')"

echo ""
echo "========================================"
echo "test_superseded_durable: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
