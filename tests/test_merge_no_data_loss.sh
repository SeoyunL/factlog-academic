#!/usr/bin/env bash
# tests/test_merge_no_data_loss.sh — merge must not erase a populated KB
#
# facts/candidates.csv is REBUILT from runs/*.json on every merge. If runs/ is
# gone, the rebuild wrote an (almost) empty table: every accepted fact a human
# reviewed was destroyed and only `superseded` tombstones survived — and finalize
# then reported "done — no contradictions" over an empty engine input, exit 0.
#
# runs/ is easy to lose: the shipped .gitignore excluded it and the docs called it
# an engine artifact, so a version-controlled KB lost every fact on a fresh clone.
#
# Pins:
#   (a) an empty runs/ + a populated candidates.csv -> merge REFUSES, exit 1
#   (b) candidates.csv is left byte-identical (nothing destroyed)
#   (c) finalize propagates the failure instead of reporting success
#   (d) --allow-delete is the explicit opt-out (someone really discarding facts)
#   (e) a genuinely empty KB (no facts yet) still merges fine — no false alarm
#   (f) a normal KB with runs/ still merges
#   (g) THE RATCHET: a partial runs/ loss (or a clone re-synced from scratch) must
#       not delete an accepted fact either. Guarding only "runs/ is empty" was a
#       cliff: /factlog sync re-extracts, so raw_rows > 0, the empty-check stayed
#       silent, and every fact not re-extracted vanished with exit 0.
#   (h) examples/sample-kb — the KB this repo ships — must survive its own rule
#
# Usage: bash tests/test_merge_no_data_loss.sh

set -euo pipefail

TMP_ROOT="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$TMP_ROOT"' EXIT

export XDG_CONFIG_HOME="$TMP_ROOT/cfg"  # isolate the active-KB config (#62)

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
MERGE="$PLUGIN_ROOT/tools/merge_candidates.py"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

new_kb() {  # echoes a KB root with one accepted fact backed by a run file
  local kb
  kb="$(mktemp -d "$TMP_ROOT/kb.XXXXXX")/wiki"
  "$PYTHON" -m factlog init --target "$kb" >/dev/null
  printf 'a\n' > "$kb/sources/a.md"
  cat > "$kb/runs/2026-01-01T00-00-00-a.json" <<'JSON'
[{"subject":"A","relation":"knows","object":"B","source":"sources/a.md","status":"confirmed","confidence":0.9,"note":""}]
JSON
  "$PYTHON" "$MERGE" --wiki "$kb" >/dev/null 2>&1
  echo "$kb"
}

facts_in() { echo $(($(wc -l < "$1/facts/candidates.csv") - 1)); }

# ------------------------------------------------------------------ (f) first
KB="$(new_kb)"
if [ "$(facts_in "$KB")" = "1" ]; then
  ok "(f) a normal KB with runs/ merges"
else
  bad "(f) baseline merge produced $(facts_in "$KB") rows, expected 1"
fi

# ------------------------------------------------------------ (a) (b) (c)
before="$(md5 -q "$KB/facts/candidates.csv" 2>/dev/null || md5sum "$KB/facts/candidates.csv" | cut -d' ' -f1)"
rm -f "$KB"/runs/*.json          # the clone-without-runs disaster

if "$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1; then
  bad "(a) merge rebuilt candidates.csv from an empty runs/ instead of refusing"
else
  ok "(a) merge refuses to rebuild from an empty runs/"
fi

after="$(md5 -q "$KB/facts/candidates.csv" 2>/dev/null || md5sum "$KB/facts/candidates.csv" | cut -d' ' -f1)"
if [ "$before" = "$after" ]; then
  ok "(b) candidates.csv is untouched — nothing destroyed"
else
  bad "(b) candidates.csv changed ($(facts_in "$KB") rows left)"
fi

if ( cd "$KB" && FACTLOG_ROOT="$KB" "$PYTHON" "$PLUGIN_ROOT/tools/finalize.py" --target "$KB" >/dev/null 2>&1 ); then
  bad "(c) finalize reported success over a refused merge"
else
  ok "(c) finalize propagates the refusal instead of reporting success"
fi

# ------------------------------------------------------------------ (d)
if "$PYTHON" "$MERGE" --wiki "$KB" --allow-delete >/dev/null 2>&1; then
  ok "(d) --allow-delete is the explicit opt-out"
else
  bad "(d) --allow-delete did not permit the rebuild"
fi

# ------------------------------------------------------------------ (e)
FRESH="$(mktemp -d "$TMP_ROOT/fresh.XXXXXX")/wiki"
"$PYTHON" -m factlog init --target "$FRESH" >/dev/null
printf 'a\n' > "$FRESH/sources/a.md"
if "$PYTHON" "$MERGE" --wiki "$FRESH" >/dev/null 2>&1; then
  ok "(e) a KB with no facts yet still merges — the guard does not false-alarm"
else
  bad "(e) the guard blocked a legitimately empty KB"
fi

# ------------------------------------------------------------------ (g)
RATCHET="$(new_kb)"
cat > "$RATCHET/runs/2026-01-02T00-00-00-b.json" <<'JSON'
[{"subject":"C","relation":"knows","object":"D","source":"sources/a.md","status":"confirmed","confidence":0.9,"note":""}]
JSON
"$PYTHON" "$MERGE" --wiki "$RATCHET" >/dev/null 2>&1
rm -f "$RATCHET/runs/2026-01-02T00-00-00-b.json"   # one run file lost, the other intact

if "$PYTHON" "$MERGE" --wiki "$RATCHET" >/dev/null 2>&1; then
  bad "(g) a partial runs/ loss silently deleted an accepted fact"
else
  ok "(g) a partial runs/ loss is refused too — the guard is a ratchet, not a cliff"
fi
if grep -q "^C,knows,D," "$RATCHET/facts/candidates.csv"; then
  ok "(g) the accepted fact survived the refused merge"
else
  bad "(g) the accepted fact was deleted anyway"
fi

# ------------------------------------------------------------------ (h)
SAMPLE="$TMP_ROOT/sample-kb"
cp -r "$PLUGIN_ROOT/examples/sample-kb" "$SAMPLE"
if "$PYTHON" "$MERGE" --wiki "$SAMPLE" >/dev/null 2>&1; then
  ok "(h) examples/sample-kb obeys its own rule (runs/*.json is committed)"
else
  bad "(h) the shipped sample KB fails the guard — it has no runs/*.json"
fi

# ------------------------------------------------------------------ (i)
# needs_review is a human ruling too. The empty-runs guard already counted it as
# keepable, so guarding it in one place and not the other gave the absurd split of
# refusing a TOTAL loss while silently accepting a PARTIAL one.
REVIEW="$(mktemp -d "$TMP_ROOT/rev.XXXXXX")/wiki"
"$PYTHON" -m factlog init --target "$REVIEW" >/dev/null
printf 'a\n' > "$REVIEW/sources/a.md"
cat > "$REVIEW/runs/r1.json" <<'JSON'
[{"subject":"A","relation":"knows","object":"B","source":"sources/a.md","status":"confirmed","confidence":0.9,"note":""},
 {"subject":"C","relation":"knows","object":"D","source":"sources/a.md","status":"needs_review","confidence":0.9,"note":""}]
JSON
"$PYTHON" "$MERGE" --wiki "$REVIEW" >/dev/null 2>&1
cat > "$REVIEW/runs/r1.json" <<'JSON'
[{"subject":"A","relation":"knows","object":"B","source":"sources/a.md","status":"confirmed","confidence":0.9,"note":""}]
JSON
if "$PYTHON" "$MERGE" --wiki "$REVIEW" >/dev/null 2>&1; then
  bad "(i) a needs_review row was silently deleted by a partial runs/ loss"
else
  ok "(i) needs_review is protected too — both guards use one definition"
fi

# ------------------------------------------------------------------ (j)
# The refusal must name a gate that actually works. `factlog reject` is a no-op on
# an accepted fact (it only retires PENDING rows), so telling the user to run it
# would leave them stuck, following advice that does nothing.
out="$("$PYTHON" "$MERGE" --wiki "$REVIEW" 2>&1 || true)"
if grep -q "eject --fact" <<<"$out" && grep -q "reject.* will NOT" <<<"$out"; then
  ok "(j) the refusal names the gate that works, and says reject does not"
else
  bad "(j) the refusal advises a gate that is a no-op on a ruled-on fact"
fi

echo "---"
echo "passed: $pass, failed: $fail"
[ "$fail" -eq 0 ]
