#!/usr/bin/env bash
# tests/test_finalize_conflict_atomicity.sh — finalize is atomic w.r.t. conflicts (#212)
#
# Background: /factlog add's finalize chain used to compile facts/accepted.dl FIRST
# and only THEN run the single-valued contradiction check. When a contradiction was
# found it returned 1 (correctly failing) but left the two contradictory facts in
# accepted.dl — the engine's trusted input that ask_router and /factlog check read
# directly from disk WITHOUT recompiling. A failed finalize therefore silently
# poisoned the KB, defeating factlog's deterministic contradiction gate on the next
# `factlog ask`.
#
# This pins the fix (gate before compile + heal a pre-poisoned file):
#   - a single-valued relation with 2 distinct confirmed objects for one subject
#     makes finalize exit non-zero (conflict gate still fires), AND
#   - accepted.dl does NOT end up holding both contradictory facts, AND
#   - a KB already poisoned by a pre-fix finalize is healed (contradictory
#     accepted.dl removed) rather than left for ask/check to read.
# Reverting the fix (compile-then-check) must fail the "no contradictory pair in
# accepted.dl" assertions — it's a genuine regression guard, not a tautology.
#
# Deterministic; no pyrewire required. Usage: bash tests/test_finalize_conflict_atomicity.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
FINALIZE="$PLUGIN_ROOT/tools/finalize.py"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

# --- fixture: single-valued `owner` with a contradictory pair (Alice vs Bob) ------
new_conflict_kb() {
  local kb="$1"
  "$PYTHON" -m factlog init --target "$kb" >/dev/null
  printf '# src\n\nProjectX is owned by Alice. ProjectX is owned by Bob.\n' > "$kb/sources/p.md"
  printf '# single-valued relations\n\n- owner\n' > "$kb/policy/single-valued.md"
  printf '[{"subject":"ProjectX","relation":"owner","object":"Alice","source":"sources/p.md","status":"confirmed","confidence":0.95,"note":""},{"subject":"ProjectX","relation":"owner","object":"Bob","source":"sources/p.md","status":"confirmed","confidence":0.95,"note":""}]' > "$kb/runs/r1.json"
}

# accepted.dl must not hold BOTH contradictory facts at once (absent counts as clean).
accepted_has_both() {
  local kb="$1"
  [ -f "$kb/facts/accepted.dl" ] || return 1
  grep -q 'relation("ProjectX", "owner", "Alice")' "$kb/facts/accepted.dl" \
    && grep -q 'relation("ProjectX", "owner", "Bob")' "$kb/facts/accepted.dl"
}

# --- case 1: fresh conflicting KB ------------------------------------------------
KB1="$(mktemp -d)/wiki"
new_conflict_kb "$KB1"
rc1=0; out1="$("$PYTHON" "$FINALIZE" --target "$KB1" 2>&1)" || rc1=$?
[ "$rc1" -ne 0 ] && ok "conflicting finalize exits non-zero (rc=$rc1)" || bad "conflicting finalize exited 0 (gate broken)"
if accepted_has_both "$KB1"; then
  bad "#212: accepted.dl holds BOTH contradictory facts after a failed finalize (poisoned)"
else
  ok "#212: accepted.dl does NOT hold the contradictory pair (no poisoned engine input)"
fi
printf '%s' "$out1" | grep -qF "CONFLICT" && ok "conflict reported with a CONFLICT line" || bad "no CONFLICT line in output"
printf '%s' "$out1" | grep -qiF "NOT compiled" && ok "message states facts were NOT compiled" || bad "message does not state facts were uncompiled"

# --- case 2: idempotent — re-running on the still-conflicting KB stays clean ------
rc1b=0; "$PYTHON" "$FINALIZE" --target "$KB1" >/dev/null 2>&1 || rc1b=$?
[ "$rc1b" -ne 0 ] && ok "re-run on unresolved conflict still exits non-zero" || bad "re-run on unresolved conflict exited 0"
if accepted_has_both "$KB1"; then bad "#212: re-run re-poisoned accepted.dl"; else ok "#212: re-run leaves no contradictory pair in accepted.dl"; fi

# --- case 3: heal a KB already poisoned by a PRE-FIX finalize ---------------------
# Simulate the old bug's aftermath: accepted.dl on disk already carries both facts.
# The fixed finalize must detect the conflict and remove that poisoned file (option
# (c) defensive heal) rather than leave it for ask/check to read.
KB2="$(mktemp -d)/wiki"
new_conflict_kb "$KB2"
mkdir -p "$KB2/facts"
printf 'relation("ProjectX", "owner", "Alice").\nrelation("ProjectX", "owner", "Bob").\n' > "$KB2/facts/accepted.dl"
rc2=0; "$PYTHON" "$FINALIZE" --target "$KB2" >/dev/null 2>&1 || rc2=$?
[ "$rc2" -ne 0 ] && ok "poisoned KB: finalize still fails on the conflict" || bad "poisoned KB: finalize exited 0"
if accepted_has_both "$KB2"; then
  bad "#212: pre-poisoned accepted.dl was left intact (not healed)"
else
  ok "#212: pre-poisoned accepted.dl was healed (contradictory pair removed)"
fi

# --- case 4: resolving the conflict lets finalize compile normally (recovery) -----
# Supersede the outdated row; finalize must now exit 0 and compile the winning fact.
KB3="$(mktemp -d)/wiki"
new_conflict_kb "$KB3"
"$PYTHON" "$FINALIZE" --target "$KB3" >/dev/null 2>&1 || true
# Mark Alice superseded (keep Bob confirmed) directly in candidates.csv.
"$PYTHON" - "$KB3" <<'PY'
import csv, sys
from pathlib import Path
p = Path(sys.argv[1]) / "facts" / "candidates.csv"
rows = list(csv.DictReader(p.open(encoding="utf-8")))
for r in rows:
    if r["object"] == "Alice":
        r["status"] = "superseded"
with p.open("w", encoding="utf-8", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=rows[0].keys())
    w.writeheader()
    w.writerows(rows)
PY
rc3=0; "$PYTHON" "$FINALIZE" --target "$KB3" >/dev/null 2>&1 || rc3=$?
[ "$rc3" -eq 0 ] && ok "recovery: finalize exits 0 once the conflict is superseded" || bad "recovery: finalize still failed after supersession (rc=$rc3)"
if [ -f "$KB3/facts/accepted.dl" ] \
   && grep -q 'relation("ProjectX", "owner", "Bob")' "$KB3/facts/accepted.dl" \
   && ! grep -q 'relation("ProjectX", "owner", "Alice")' "$KB3/facts/accepted.dl"; then
  ok "recovery: accepted.dl compiled with only the winning fact (Bob)"
else
  bad "recovery: accepted.dl missing the winning fact or still holds the superseded one"
fi

# --- case 5: no-conflict path is untouched (single confirmed fact) ----------------
KB4="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB4" >/dev/null
printf '# src\n\nAcme API uses FastAPI.\n' > "$KB4/sources/a.md"
printf '[{"subject":"Acme API","relation":"uses","object":"FastAPI","source":"sources/a.md","status":"confirmed","confidence":0.95,"note":""}]' > "$KB4/runs/r1.json"
rc4=0; "$PYTHON" "$FINALIZE" --target "$KB4" >/dev/null 2>&1 || rc4=$?
[ "$rc4" -eq 0 ] && ok "no-conflict path: finalize exits 0" || bad "no-conflict path: finalize exited $rc4"
[ -f "$KB4/facts/accepted.dl" ] && grep -q 'relation("Acme API", "uses", "FastAPI")' "$KB4/facts/accepted.dl" \
  && ok "no-conflict path: accepted.dl compiled with the fact" || bad "no-conflict path: fact not compiled"

echo ""
echo "========================================"
echo "test_finalize_conflict_atomicity: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
