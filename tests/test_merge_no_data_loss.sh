#!/usr/bin/env bash
# tests/test_merge_no_data_loss.sh — merge must not erase a populated KB (#218)
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
#   (d) --allow-empty is the explicit opt-out (someone really emptying a KB)
#   (e) a genuinely empty KB (no facts yet) still merges fine — no false alarm
#   (f) a normal KB with runs/ still merges
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
if "$PYTHON" "$MERGE" --wiki "$KB" --allow-empty >/dev/null 2>&1; then
  ok "(d) --allow-empty is the explicit opt-out"
else
  bad "(d) --allow-empty did not permit the rebuild"
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

echo "---"
echo "passed: $pass, failed: $fail"
[ "$fail" -eq 0 ]
