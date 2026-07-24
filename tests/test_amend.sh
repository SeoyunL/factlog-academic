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

# --- #106: a human re-review (accepted -> needs_review in candidates.csv) is ---
# --- NOT silently re-promoted by a stale 'accepted' left in runs/*.json --------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'a\n' > "$KB/sources/a.md"
printf '[{"subject":"Gizmo","relation":"codename","object":"Nova","source":"sources/a.md","status":"accepted","confidence":0.9,"note":""}]\n' \
  > "$KB/runs/r.json"  # runs/*.json carries the fact as accepted (e.g. an earlier accept)
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
grep -q "Gizmo,codename,Nova,sources/a.md,accepted," "$KB/facts/candidates.csv" && ok "seed: fact merges as accepted" || bad "seed merge failed"
# human pulls it back for re-review in candidates.csv ONLY (runs/*.json untouched)
"$PYTHON" - "$KB/facts/candidates.csv" <<'PY'
import sys
p = sys.argv[1]
t = open(p, encoding="utf-8").read().replace(
    "Gizmo,codename,Nova,sources/a.md,accepted,", "Gizmo,codename,Nova,sources/a.md,needs_review,")
open(p, "w", encoding="utf-8").write(t)
PY
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
grep -q "Gizmo,codename,Nova,sources/a.md,needs_review," "$KB/facts/candidates.csv" \
  && ok "human re-review held at needs_review across re-merge (#106)" \
  || bad "re-review silently re-promoted: $(grep codename "$KB/facts/candidates.csv")"

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

# --- recompile failure: edit saved, rc 1, clear message ----------------------
KB="$(mktemp -d)/wiki"; seed "$KB"
rm -f "$KB/facts/accepted.dl"; mkdir "$KB/facts/accepted.dl"   # make compile_facts fail to write
set +e; out="$("$PYTHON" -m factlog amend Widget codename Draft --set-object Falcon --target "$KB" 2>&1)"; rc=$?; set -e
[ "$rc" -eq 1 ] && printf '%s' "$out" | grep -qF "NOT recompiled" && ok "recompile failure exits rc 1 with 'NOT recompiled'" || bad "compile-failure path wrong (rc=$rc)"
grep -q "Widget,codename,Falcon," "$KB/facts/candidates.csv" && ok "edit saved even when recompile fails" || bad "edit not saved on recompile failure"
rmdir "$KB/facts/accepted.dl"

# --- candidates-only fact (no runs backing) warns about durability -----------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'a\n' > "$KB/sources/a.md"
printf '%s\n%s\n' "$H" 'Solo,rel,Old,sources/a.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
out="$("$PYTHON" -m factlog amend Solo rel Old --set-object New --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "0 runs/*.json row(s) updated" && printf '%s' "$out" | grep -qF "will NOT survive a re-merge" \
  && ok "candidates-only edit warns it won't survive re-merge" || bad "no-runs durability warning missing"

# --- #220: amend leaves a superseded tombstone so a re-extraction of the ------
# --- ORIGINAL (uncorrected) source does NOT revive the old value -------------
# The durability case above only re-merges the SAME runs/*.json the amend
# rewrote, so it can't catch a fresh re-extraction. Here a new runs/*.json
# re-asserts the original (pre-amend) value — as a real re-sync would, because
# the source text still carries the typo. Without a tombstone the old value
# comes back as a live candidate and lingers in the review queue forever.
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'a\n' > "$KB/sources/a.md"
printf '[{"subject":"Svc","relation":"uses","object":"FastApi","source":"sources/a.md","status":"needs_review","confidence":0.5,"note":""}]\n' \
  > "$KB/runs/T1.json"
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
grep -q "Svc,uses,FastApi,sources/a.md,needs_review," "$KB/facts/candidates.csv" && ok "#220 seed: original typo merges into the review queue" || bad "#220 seed merge failed: $(grep uses "$KB/facts/candidates.csv")"
# correct the typo and accept
"$PYTHON" -m factlog amend Svc uses FastApi --set-object FastAPI --accept --target "$KB" >/dev/null 2>&1
grep -q "Svc,uses,FastAPI,sources/a.md,accepted," "$KB/facts/candidates.csv" && ok "#220 amend: corrected value is accepted" || bad "#220 amend did not accept corrected value"
grep -q "Svc,uses,FastApi,sources/a.md,superseded," "$KB/facts/candidates.csv" && ok "#220 amend: leaves a superseded tombstone for the old value" || bad "#220 no tombstone left: $(grep uses "$KB/facts/candidates.csv")"
# a fresh extraction of the still-uncorrected source re-asserts the old value
printf '[{"subject":"Svc","relation":"uses","object":"FastApi","source":"sources/a.md","status":"needs_review","confidence":0.5,"note":""}]\n' \
  > "$KB/runs/T2.json"
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
# corrected value survives as accepted...
grep -q "Svc,uses,FastAPI,sources/a.md,accepted," "$KB/facts/candidates.csv" && ok "#220 corrected value stays accepted after re-extraction" || bad "#220 corrected value lost after re-extraction: $(grep uses "$KB/facts/candidates.csv")"
# ...and the old typo does NOT come back as a live candidate / review item
if grep -qE "^Svc,uses,FastApi,sources/a\.md,(candidate|needs_review)," "$KB/facts/candidates.csv"; then
  bad "#220 old typo revived by re-extraction: $(grep uses "$KB/facts/candidates.csv")"
else
  ok "#220 old typo NOT revived by re-extraction (tombstone suppresses it)"
fi

# --- #220: subject/relation amend also leaves a tombstone --------------------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'a\n' > "$KB/sources/a.md"
printf '[{"subject":"Widgit","relation":"codename","object":"Nova","source":"sources/a.md","status":"needs_review","confidence":0.5,"note":""}]\n' \
  > "$KB/runs/T1.json"
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
"$PYTHON" -m factlog amend Widgit codename Nova --set-subject Widget --accept --target "$KB" >/dev/null 2>&1
grep -q "Widgit,codename,Nova,sources/a.md,superseded," "$KB/facts/candidates.csv" && ok "#220 subject amend leaves a tombstone for the old subject" || bad "#220 subject amend left no tombstone: $(grep codename "$KB/facts/candidates.csv")"
printf '[{"subject":"Widgit","relation":"codename","object":"Nova","source":"sources/a.md","status":"needs_review","confidence":0.5,"note":""}]\n' \
  > "$KB/runs/T2.json"
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
if grep -qE "^Widgit,codename,Nova,sources/a\.md,(candidate|needs_review)," "$KB/facts/candidates.csv"; then
  bad "#220 old subject revived by re-extraction: $(grep codename "$KB/facts/candidates.csv")"
else
  ok "#220 old subject NOT revived by re-extraction"
fi

# --- #220 defect 1: tombstone must survive a merge with NO re-extraction ------
# The old value is rewritten out of runs, so a candidates-only tombstone is lost
# the first time a merge doesn't re-assert it (candidates.csv is rebuilt from
# runs/*.json). The tombstone must be RUN-BACKED so a "dissolution window" merge
# (e.g. a sync-ignored source, a bare merge_candidates re-run, an extraction that
# produced nothing new) can't drop it and let the next re-extraction revive it.
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'a\n' > "$KB/sources/a.md"
printf '[{"subject":"Api","relation":"impl","object":"FastApi","source":"sources/a.md","status":"candidate","confidence":0.5,"note":""}]\n' \
  > "$KB/runs/T1.json"
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
"$PYTHON" -m factlog amend Api impl FastApi --set-object FastAPI --accept --target "$KB" >/dev/null 2>&1
# (a) a merge with NO re-extraction of the old value (only the current runs)
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
grep -q "Api,impl,FastApi,sources/a.md,superseded," "$KB/facts/candidates.csv" && ok "#220 d1: tombstone survives a merge with no re-extraction (run-backed)" || bad "#220 d1: tombstone dissolved after a plain merge: $(grep impl "$KB/facts/candidates.csv")"
grep -q "Api,impl,FastAPI,sources/a.md,accepted," "$KB/facts/candidates.csv" && ok "#220 d1: corrected value still accepted after plain merge" || bad "#220 d1: corrected value lost: $(grep impl "$KB/facts/candidates.csv")"
# (b) NOW the original source is re-extracted — old value must stay retired
printf '[{"subject":"Api","relation":"impl","object":"FastApi","source":"sources/a.md","status":"candidate","confidence":0.5,"note":""}]\n' \
  > "$KB/runs/T2.json"
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
if grep -qE "^Api,impl,FastApi,sources/a\.md,(candidate|needs_review)," "$KB/facts/candidates.csv"; then
  bad "#220 d1: old value revived after dissolution-window merge: $(grep impl "$KB/facts/candidates.csv")"
else
  ok "#220 d1: old value stays retired across dissolution window + re-extraction"
fi

# --- #220 defect 2: a repeated identical amend is idempotent ------------------
# is_old must NOT re-target its own superseded tombstone, else the 2nd amend
# promotes the tombstone back to accepted (duplicate accepted row) and re-adds a
# tombstone — reviving a human-retired value.
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'a\n' > "$KB/sources/a.md"
printf '[{"subject":"Lib","relation":"name","object":"Numpy","source":"sources/a.md","status":"candidate","confidence":0.5,"note":""}]\n' \
  > "$KB/runs/T1.json"
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
"$PYTHON" -m factlog amend Lib name Numpy --set-object NumPy --accept --target "$KB" >/dev/null 2>&1
"$PYTHON" -m factlog amend Lib name Numpy --set-object NumPy --accept --target "$KB" >/dev/null 2>&1 || true
n_acc=$(grep -c "Lib,name,NumPy,sources/a.md,accepted," "$KB/facts/candidates.csv" || true)
[ "$n_acc" -eq 1 ] && ok "#220 d2: repeated amend keeps exactly one accepted row (no duplicate)" || bad "#220 d2: duplicate accepted rows ($n_acc): $(grep name "$KB/facts/candidates.csv")"
n_tomb=$(grep -c "Lib,name,Numpy,sources/a.md,superseded," "$KB/facts/candidates.csv" || true)
[ "$n_tomb" -eq 1 ] && ok "#220 d2: exactly one tombstone kept (old value stays retired)" || bad "#220 d2: tombstone count wrong ($n_tomb): $(grep name "$KB/facts/candidates.csv")"
if grep -qE "^Lib,name,Numpy,sources/a\.md,(candidate|needs_review|accepted)," "$KB/facts/candidates.csv"; then
  bad "#220 d2: repeated amend revived old value as live: $(grep name "$KB/facts/candidates.csv")"
else
  ok "#220 d2: old value not revived by repeated amend"
fi

# --- #220 counterexample: note-only / --accept-only leave NO tombstone --------
KB="$(mktemp -d)/wiki"; seed "$KB"
"$PYTHON" -m factlog amend Widget codename Draft --set-note "checked" --target "$KB" >/dev/null 2>&1
if grep -qE "Widget,codename,Draft,sources/a\.md,superseded," "$KB/facts/candidates.csv"; then
  bad "#220 note-only edit wrongly created a tombstone: $(grep codename "$KB/facts/candidates.csv")"
else
  ok "#220 note-only edit (no triple change) creates no tombstone"
fi
KB="$(mktemp -d)/wiki"; seed "$KB"
"$PYTHON" -m factlog amend Widget codename Draft --accept --target "$KB" >/dev/null 2>&1
if grep -qE "Widget,codename,Draft,sources/a\.md,superseded," "$KB/facts/candidates.csv"; then
  bad "#220 --accept-only edit wrongly created a tombstone: $(grep codename "$KB/facts/candidates.csv")"
else
  ok "#220 --accept-only edit (no triple change) creates no tombstone"
fi

# --- #220 multi-source: a tombstone per backing source -----------------------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'a\n' > "$KB/sources/a.md"; printf 'b\n' > "$KB/sources/b.md"
printf '[{"subject":"Db","relation":"name","object":"Postgre","source":"sources/a.md","status":"candidate","confidence":0.5,"note":""},{"subject":"Db","relation":"name","object":"Postgre","source":"sources/b.md","status":"candidate","confidence":0.5,"note":""}]\n' \
  > "$KB/runs/T1.json"
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
"$PYTHON" -m factlog amend Db name Postgre --set-object PostgreSQL --accept --target "$KB" >/dev/null 2>&1
grep -q "Db,name,Postgre,sources/a.md,superseded," "$KB/facts/candidates.csv" && ok "#220 multi-source: tombstone for source a" || bad "#220 no tombstone for source a: $(grep name "$KB/facts/candidates.csv")"
grep -q "Db,name,Postgre,sources/b.md,superseded," "$KB/facts/candidates.csv" && ok "#220 multi-source: tombstone for source b" || bad "#220 no tombstone for source b: $(grep name "$KB/facts/candidates.csv")"
# both re-extractions stay retired after re-merge
printf '[{"subject":"Db","relation":"name","object":"Postgre","source":"sources/a.md","status":"candidate","confidence":0.5,"note":""},{"subject":"Db","relation":"name","object":"Postgre","source":"sources/b.md","status":"candidate","confidence":0.5,"note":""}]\n' \
  > "$KB/runs/T2.json"
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
if grep -qE "^Db,name,Postgre,sources/[ab]\.md,(candidate|needs_review)," "$KB/facts/candidates.csv"; then
  bad "#220 multi-source: an old value revived: $(grep name "$KB/facts/candidates.csv")"
else
  ok "#220 multi-source: both old values stay retired across re-extraction"
fi

# --- #480: amend matches the run row after merge canonicalised the object -----
# The run stores amount(7,억); merge rewrites candidates.csv to amount(7,"억").
# The old runs match keyed on a bare NFC 3-tuple with no canonical_amount, so it
# missed the run row: the edit reached candidates.csv only and vanished on the
# next re-merge (candidates.csv is rebuilt from runs/*.json). common.fact_key
# canonicalises the object on BOTH sides, so the CLI and merge agree which run
# row IS this fact.
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'a\n' > "$KB/sources/a.md"
printf '[{"subject":"Cost","relation":"is","object":"amount(7,억)","source":"sources/a.md","status":"needs_review","confidence":0.5,"note":""}]\n' \
  > "$KB/runs/T1.json"
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
grep -q 'Cost,is,"amount(7,""억"")",sources/a.md,needs_review,' "$KB/facts/candidates.csv" \
  && ok "#480 seed: merge canonicalised amount(7,억) -> amount(7,\"억\")" || bad "#480 seed amount not canonicalised: $(cat "$KB/facts/candidates.csv")"
out="$("$PYTHON" -m factlog amend Cost is 'amount(7,"억")' --set-note checked --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "1 runs/*.json row(s) updated" && ok "#480 amend matches the canonicalised-amount run row" || bad "#480 amend missed the run row: $out"
grep -qF '"note": "checked"' "$KB/runs/T1.json" && ok "#480 amend wrote the note to runs/*.json" || bad "#480 note not in runs: $(cat "$KB/runs/T1.json")"
# durability: the note survives a re-merge rebuilding candidates.csv from runs
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
grep -qF ',needs_review,0.50,checked' "$KB/facts/candidates.csv" && ok "#480 amend note survives a re-merge (amount object)" || bad "#480 amend note lost after re-merge: $(cat "$KB/facts/candidates.csv")"

echo ""
echo "========================================"
echo "test_amend: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
