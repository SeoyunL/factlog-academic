#!/usr/bin/env bash
# tests/test_amend.sh — `factlog amend` durable fact correction (#90)
#
# Pins (XDG-isolated; SYNTHETIC data only):
#   - --set-object/-subject/-relation/-note rewrite the matching fact
#   - the edit updates BOTH candidates.csv AND its backing runs/*.json, so it
#     survives a re-merge (value's source of truth is runs/*.json)
#   - --accept promotes to accepted (durable via merge engine-preservation)
#   - --dry-run no-op; no match -> rc 1; empty --set-X -> rc 2; nothing-to-do rc 2
#   - a candidates-only fact (no runs backing) warns it won't survive re-merge
#
# Usage: bash tests/test_amend.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62)

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
H="subject,relation,object,source,status,confidence,note"
MERGE="$PLUGIN_ROOT/tools/merge_candidates.py"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

# Seed a KB whose facts come from a runs/*.json (the real pipeline shape), merged.
seed() {  # $1 = KB
  local kb="$1"
  "$PYTHON" -m factlog init --target "$kb" >/dev/null
  printf 'a\n' > "$kb/sources/a.md"
  printf '[{"subject":"Widget","relation":"codename","object":"Draft","source":"sources/a.md","status":"needs_review","confidence":0.5,"note":"tentative"}]\n' \
    > "$kb/runs/r.json"
  "$PYTHON" "$MERGE" --wiki "$kb" >/dev/null 2>&1
}

# --- value + note edit, both stores updated, --accept ------------------------
KB="$(mktemp -d)/wiki"; seed "$KB"
out="$("$PYTHON" -m factlog amend Widget codename Draft --set-object Falcon --set-note "name finalized" --accept --target "$KB" 2>&1)"
printf '%s\n' "$out"; echo "---"
grep -q "Widget,codename,Falcon,sources/a.md,accepted,0.50,name finalized" "$KB/facts/candidates.csv" && ok "candidates.csv: object+note+status updated" || bad "candidates not updated: $(grep codename "$KB/facts/candidates.csv")"
grep -qF '"object": "Falcon"' "$KB/runs/r.json" && ok "runs/*.json: object updated (durable source)" || bad "runs not updated"
grep -qF '"note": "name finalized"' "$KB/runs/r.json" && ok "runs/*.json: note updated" || bad "runs note not updated"
printf '%s' "$out" | grep -qF "1 runs/*.json row(s) updated" && ok "reports runs rows updated" || bad "runs-updated count missing"

# --- durable across a re-merge (value AND accepted status) --------------------
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
grep -q "Widget,codename,Falcon,sources/a.md,accepted,0.50,name finalized" "$KB/facts/candidates.csv" \
  && ok "amended value + accepted status survive a re-merge" || bad "amend reverted after re-merge: $(grep codename "$KB/facts/candidates.csv")"

# --- --set-subject / --set-relation ------------------------------------------
KB="$(mktemp -d)/wiki"; seed "$KB"
"$PYTHON" -m factlog amend Widget codename Draft --set-subject "Widget v2" --set-relation alias --target "$KB" >/dev/null 2>&1
grep -q "Widget v2,alias,Draft," "$KB/facts/candidates.csv" && ok "subject+relation rewritten" || bad "subject/relation not rewritten"

# --- --dry-run no-op ---------------------------------------------------------
KB="$(mktemp -d)/wiki"; seed "$KB"
before="$(cat "$KB/facts/candidates.csv")"; rbefore="$(cat "$KB/runs/r.json")"
"$PYTHON" -m factlog amend Widget codename Draft --set-object X --dry-run --target "$KB" >/dev/null 2>&1
[ "$(cat "$KB/facts/candidates.csv")" = "$before" ] && [ "$(cat "$KB/runs/r.json")" = "$rbefore" ] && ok "--dry-run leaves candidates.csv and runs/*.json untouched" || bad "--dry-run mutated state"

# --- error paths -------------------------------------------------------------
KB="$(mktemp -d)/wiki"; seed "$KB"
set +e
"$PYTHON" -m factlog amend nope nope nope --set-object X --target "$KB" >/dev/null 2>&1; [ $? -eq 1 ] && ok "no match exits rc 1" || bad "no-match rc wrong"
"$PYTHON" -m factlog amend Widget codename Draft --set-subject "" --target "$KB" >/dev/null 2>&1; [ $? -eq 2 ] && ok "empty --set-subject rc 2" || bad "empty set rc wrong"
"$PYTHON" -m factlog amend Widget codename Draft --target "$KB" >/dev/null 2>&1; [ $? -eq 2 ] && ok "no --set-* and no --accept rc 2" || bad "nothing-to-do rc wrong"
"$PYTHON" -m factlog amend Widget codename Draft --set-object X --target "$(mktemp -d)" >/dev/null 2>&1; [ $? -ne 0 ] && ok "amend on a non-KB path errors" || bad "non-KB path should error"
set -e

# --- candidates-only fact (no runs backing) warns about durability -----------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'a\n' > "$KB/sources/a.md"
printf '%s\n%s\n' "$H" 'Solo,rel,Old,sources/a.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
out="$("$PYTHON" -m factlog amend Solo rel Old --set-object New --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "0 runs/*.json row(s) updated" && printf '%s' "$out" | grep -qF "will NOT survive a re-merge" \
  && ok "candidates-only edit warns it won't survive re-merge" || bad "no-runs durability warning missing"

echo ""
echo "========================================"
echo "test_amend: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
