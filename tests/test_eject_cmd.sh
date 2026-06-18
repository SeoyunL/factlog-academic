#!/usr/bin/env bash
# tests/test_eject_cmd.sh — `factlog eject`, the inverse of ingest
#
# Pins (XDG-isolated; synthetic data; no pyrewire needed — eject recompiles
# accepted.dl deterministically via compile_facts):
#   - naming a binary original (deck.pptx) also matches its runs/sources/<stem>
#     conversion; eject deletes the conversion, strips the runs/*.json rows
#     (removing the now-empty file), and supersedes the citing candidate row
#   - the original under sources/ is KEPT by default (with a note); accepted.dl
#     drops the retired fact but keeps the others
#   - --dry-run changes nothing
#   - --purge deletes the candidate row instead of superseding it
#   - --delete-original also removes the user's original under sources/
#   - a bare stem matches; an unknown name errors (rc != 0); non-KB path errors
#
# Usage: bash tests/test_eject_cmd.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62)

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

H="subject,relation,object,source,status,confidence,note"

# Scaffold a fresh KB with a binary original + its conversion + a text source,
# candidate rows citing each, and a runs/*.json extraction file for the deck.
seed() {  # $1 = KB path
  local kb="$1"
  "$PYTHON" -m factlog init --target "$kb" >/dev/null
  printf 'PK\003\004 fake pptx\000' > "$kb/sources/deck.pptx"
  printf '<!-- ingested-by-factlog | source: deck.pptx | converter: factlog-pptx | date: 2026-01-01T00:00:00Z -->\nslide text\n' \
    > "$kb/runs/sources/deck.md"
  printf 'plain text\n' > "$kb/sources/notes.md"
  printf '%s\n%s\n%s\n%s\n' "$H" \
    'A,rel,B,runs/sources/deck.md,confirmed,0.9,' \
    'C,rel,D,sources/notes.md,confirmed,0.9,' \
    'E,rel,F,sources/notes.md,confirmed,0.9,' > "$kb/facts/candidates.csv"
  printf '[{"subject":"A","relation":"rel","object":"B","source":"runs/sources/deck.md","status":"candidate","confidence":0.9,"note":""}]\n' \
    > "$kb/runs/2026-01-01-deck.json"
}

# --- default: supersede; conversion + run file removed; original kept ----------
KB="$(mktemp -d)/wiki"; seed "$KB"
out="$("$PYTHON" -m factlog eject deck.pptx --target "$KB" 2>&1)"
printf '%s\n' "$out"; echo "---"
[ ! -f "$KB/runs/sources/deck.md" ] && ok "conversion deleted" || bad "conversion still present"
[ ! -f "$KB/runs/2026-01-01-deck.json" ] && ok "emptied runs/*.json removed" || bad "runs json still present"
[ -f "$KB/sources/deck.pptx" ] && ok "original kept by default" || bad "original was deleted without --delete-original"
grep -q "A,rel,B,runs/sources/deck.md,superseded," "$KB/facts/candidates.csv" && ok "citing row marked superseded" || bad "row not superseded"
grep -q '"A", "rel", "B"' "$KB/facts/accepted.dl" && bad "retired fact still in accepted.dl" || ok "retired fact dropped from accepted.dl"
grep -q '"C", "rel", "D"' "$KB/facts/accepted.dl" && ok "unrelated fact preserved in accepted.dl" || bad "unrelated fact lost"
printf '%s' "$out" | grep -qF "matched source ref" && printf '%s' "$out" | grep -qF "runs/sources/deck.md" && printf '%s' "$out" | grep -qF "sources/deck.pptx" \
  && ok "binary name matches both original and its conversion" || bad "stem-conversion match missing"

# --- --dry-run changes nothing -----------------------------------------------
KB="$(mktemp -d)/wiki"; seed "$KB"
before="$(cat "$KB/facts/candidates.csv")"
"$PYTHON" -m factlog eject deck.pptx --target "$KB" --dry-run >/dev/null 2>&1
[ -f "$KB/runs/sources/deck.md" ] && [ -f "$KB/runs/2026-01-01-deck.json" ] && [ "$(cat "$KB/facts/candidates.csv")" = "$before" ] \
  && ok "--dry-run leaves files and candidates.csv untouched" || bad "--dry-run mutated state"

# --- --purge deletes the candidate row ---------------------------------------
KB="$(mktemp -d)/wiki"; seed "$KB"
"$PYTHON" -m factlog eject deck.pptx --target "$KB" --purge >/dev/null 2>&1
grep -q "runs/sources/deck.md" "$KB/facts/candidates.csv" && bad "--purge left the row" || ok "--purge deletes the candidate row"

# --- --delete-original removes the user's original ----------------------------
KB="$(mktemp -d)/wiki"; seed "$KB"
"$PYTHON" -m factlog eject notes.md --target "$KB" --purge --delete-original >/dev/null 2>&1
[ ! -f "$KB/sources/notes.md" ] && ok "--delete-original removes the original" || bad "original not deleted"
grep -q "sources/notes.md" "$KB/facts/candidates.csv" && bad "notes rows not purged" || ok "text-source rows purged"

# --- bare stem matches; unknown name errors ----------------------------------
KB="$(mktemp -d)/wiki"; seed "$KB"
"$PYTHON" -m factlog eject deck --target "$KB" >/dev/null 2>&1 \
  && [ ! -f "$KB/runs/sources/deck.md" ] && ok "bare stem 'deck' matches the source" || bad "bare stem did not match"
set +e; "$PYTHON" -m factlog eject nope --target "$KB" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -ne 0 ] && ok "unknown source name errors (rc != 0)" || bad "unknown name should error"

# --- a sibling sharing the stem is NOT pulled in by the conversion's provenance -
# report.pptx was ingested (provenance source: report.pptx); report.docx was not.
# Ejecting report.docx must not delete report.pptx's conversion or retire its fact.
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'PK\003\004\000' > "$KB/sources/report.pptx"
printf 'PK\003\004\000' > "$KB/sources/report.docx"
printf '<!-- ingested-by-factlog | source: report.pptx | converter: factlog-pptx | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/report.md"
printf '%s\n%s\n' "$H" 'A,rel,B,runs/sources/report.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
"$PYTHON" -m factlog eject report.docx --target "$KB" >/dev/null 2>&1 || true
[ -f "$KB/runs/sources/report.md" ] && ok "ejecting report.docx keeps report.pptx's conversion (provenance-tied)" || bad "wrong conversion deleted"
grep -q "runs/sources/report.md,confirmed," "$KB/facts/candidates.csv" && ok "report.pptx's fact not retired by ejecting report.docx" || bad "wrong fact retired"

# --- a full KB-relative path does NOT match a same-name file in another dir ----
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
mkdir -p "$KB/sources/a" "$KB/sources/b"
printf 'a\n' > "$KB/sources/a/dup.md"; printf 'b\n' > "$KB/sources/b/dup.md"
printf '%s\n%s\n%s\n' "$H" \
  'A,rel,B,sources/a/dup.md,confirmed,0.9,' \
  'C,rel,D,sources/b/dup.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
"$PYTHON" -m factlog eject sources/a/dup.md --target "$KB" --delete-original >/dev/null 2>&1 || true
[ ! -f "$KB/sources/a/dup.md" ] && [ -f "$KB/sources/b/dup.md" ] && ok "full path ejects only that file, not the same-name sibling" || bad "full path matched across directories"
grep -q "sources/b/dup.md,confirmed," "$KB/facts/candidates.csv" && ok "sibling's fact preserved" || bad "sibling fact wrongly retired"

# --- a candidates.csv whose header lacks 'status' is not truncated -------------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'plain\n' > "$KB/sources/x.md"; printf 'plain\n' > "$KB/sources/y.md"
printf 'subject,relation,object,source\n%s\n%s\n' \
  'A,rel,B,sources/x.md' 'C,rel,D,sources/y.md' > "$KB/facts/candidates.csv"
"$PYTHON" -m factlog eject x.md --target "$KB" >/dev/null 2>&1 || true
grep -q "sources/y.md" "$KB/facts/candidates.csv" && ok "rows preserved when header lacks a status column (no truncation)" || bad "candidates.csv truncated on missing status column"

# --- non-KB path errors -------------------------------------------------------
set +e; "$PYTHON" -m factlog eject anything --target "$(mktemp -d)" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -ne 0 ] && ok "eject on a non-KB path errors" || bad "non-KB path should error"

echo ""
echo "========================================"
echo "test_eject_cmd: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
