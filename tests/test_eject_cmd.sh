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

# =============================================================================
# Fact mode: eject a single fact, leaving the source in place (#74)
# =============================================================================

# Seed a text source with two facts + a runs/*.json asserting both.
seed_facts() {  # $1 = KB path
  local kb="$1"
  "$PYTHON" -m factlog init --target "$kb" >/dev/null
  printf 'plain\n' > "$kb/sources/a.md"
  printf '%s\n%s\n%s\n' "$H" \
    'X,wrongrel,Y,sources/a.md,confirmed,0.9,' \
    'X,goodrel,Z,sources/a.md,confirmed,0.9,' > "$kb/facts/candidates.csv"
  printf '[{"subject":"X","relation":"wrongrel","object":"Y","source":"sources/a.md","status":"confirmed","confidence":0.9,"note":""},{"subject":"X","relation":"goodrel","object":"Z","source":"sources/a.md","status":"confirmed","confidence":0.9,"note":""}]\n' \
    > "$kb/runs/r.json"
}

# --- default (supersede): retire one fact, keep source + runs + other fact -----
KB="$(mktemp -d)/wiki"; seed_facts "$KB"
"$PYTHON" -m factlog eject --fact X wrongrel Y --target "$KB" >/dev/null 2>&1
grep -q "X,wrongrel,Y,sources/a.md,superseded," "$KB/facts/candidates.csv" && ok "fact mode: matched triple superseded" || bad "fact not superseded"
grep -q "X,goodrel,Z,sources/a.md,confirmed," "$KB/facts/candidates.csv" && ok "fact mode: other fact untouched" || bad "other fact altered"
[ -f "$KB/sources/a.md" ] && ok "fact mode: source kept" || bad "source deleted in fact mode"
grep -q "wrongrel" "$KB/runs/r.json" && ok "fact mode default: runs/*.json kept (durable supersede)" || bad "runs stripped on default supersede"
grep -q '"X", "goodrel", "Z"' "$KB/facts/accepted.dl" && ! grep -q '"X", "wrongrel", "Y"' "$KB/facts/accepted.dl" \
  && ok "fact mode: accepted.dl drops only the retired fact" || bad "accepted.dl wrong after fact eject"

# --- supersede is durable across a re-merge -----------------------------------
"$PYTHON" tools/merge_candidates.py --wiki "$KB" >/dev/null 2>&1
grep -q "X,wrongrel,Y,sources/a.md,superseded," "$KB/facts/candidates.csv" && ok "fact mode: supersede survives re-merge" || bad "supersede lost after re-merge"

# --- supersede survives re-merge even when the section anchor drifts -----------
# candidate cites sources/a.md#sec3; a later run re-asserts the bare path. The
# supersede preservation key is anchor-insensitive, so the fact stays retired.
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'plain\n' > "$KB/sources/a.md"
printf '%s\n%s\n' "$H" 'X,wrongrel,Y,sources/a.md#sec3,confirmed,0.9,' > "$KB/facts/candidates.csv"
"$PYTHON" -m factlog eject --fact X wrongrel Y --target "$KB" >/dev/null 2>&1
# next sync re-asserts the same triple from the same file, anchor dropped
printf '[{"subject":"X","relation":"wrongrel","object":"Y","source":"sources/a.md","status":"confirmed","confidence":0.9,"note":""}]\n' > "$KB/runs/drift.json"
"$PYTHON" tools/merge_candidates.py --wiki "$KB" >/dev/null 2>&1
grep -q ",superseded," "$KB/facts/candidates.csv" && ! grep -q "wrongrel,Y,sources/a.md,confirmed" "$KB/facts/candidates.csv" \
  && ok "fact mode: supersede survives anchor drift across re-merge" || bad "supersede lost on anchor drift: $(grep wrongrel "$KB/facts/candidates.csv")"

# --- --purge: delete the row and strip runs -----------------------------------
KB="$(mktemp -d)/wiki"; seed_facts "$KB"
"$PYTHON" -m factlog eject --fact X wrongrel Y --target "$KB" --purge >/dev/null 2>&1
grep -q "wrongrel" "$KB/facts/candidates.csv" && bad "--purge left the fact row" || ok "fact mode --purge: row deleted"
grep -q "wrongrel" "$KB/runs/r.json" && bad "--purge left runs row" || ok "fact mode --purge: runs row stripped"
grep -q "goodrel" "$KB/runs/r.json" && ok "fact mode --purge: unrelated runs row kept" || bad "unrelated runs row lost"

# --- #480: --purge strips the run row even after merge normalised the value ----
# eject used to key on a bare NFC 3-tuple, so a run row merge had stripped
# (" A " -> A) or canonicalised (amount(7,억) -> amount(7,"억")) into
# candidates.csv never matched its own candidate row. --purge reported success,
# left the run row, and the next merge rebuilt candidates.csv from it — the
# purged fact came straight back. common.fact_key aligns both keys.
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'n\n' > "$KB/sources/note.md"
printf '[{"subject":" A ","relation":"R","object":"X","source":"sources/note.md","status":"candidate","confidence":"0.9","note":""},{"subject":"Cost","relation":"is","object":"amount(7,억)","source":"sources/note.md","status":"candidate","confidence":"0.9","note":""}]\n' \
  > "$KB/runs/r1.json"
"$PYTHON" tools/merge_candidates.py --wiki "$KB" >/dev/null 2>&1
grep -q "^A,R,X,sources/note.md,candidate," "$KB/facts/candidates.csv" && ok "#480 seed: merge stripped ' A ' -> A" || bad "#480 seed subject not stripped: $(cat "$KB/facts/candidates.csv")"
out="$("$PYTHON" -m factlog eject --fact A R X --purge --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "1 run row(s) stripped" && ok "#480 eject --purge strips the run row (stripped subject)" || bad "#480 stripped-subject run row NOT stripped: $out"
out2="$("$PYTHON" -m factlog eject --fact Cost is 'amount(7,"억")' --purge --target "$KB" 2>&1)"
printf '%s' "$out2" | grep -qF "1 run row(s) stripped" && ok "#480 eject --purge strips the run row (canonical amount)" || bad "#480 amount run row NOT stripped: $out2"
# durability: re-merge must NOT resurrect either purged fact from runs/*.json
"$PYTHON" tools/merge_candidates.py --wiki "$KB" >/dev/null 2>&1
if grep -qE "^A,R,X,|amount" "$KB/facts/candidates.csv"; then
  bad "#480 purged fact revived by re-merge: $(cat "$KB/facts/candidates.csv")"
else
  ok "#480 purged facts stay gone across a re-merge (runs were stripped)"
fi
grep -q '"A", "R", "X"' "$KB/facts/accepted.dl" && bad "#480 purged fact still in accepted.dl" || ok "#480 purged fact dropped from accepted.dl"

# --- #480: a purge matching NO run row warns (silent-loss signal) --------------
# A candidates-only fact purged with no backing run must warn, so a real key
# mismatch (csv_changed > 0, runs_changed == 0) can never pass quietly again.
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'n\n' > "$KB/sources/note.md"
printf '%s\n%s\n' "$H" 'Solo,rel,V,sources/note.md,candidate,0.9,' > "$KB/facts/candidates.csv"
out="$("$PYTHON" -m factlog eject --fact Solo rel V --purge --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "matched NO runs/*.json row" && ok "#480 purge with no run match warns" || bad "#480 no-run-match warning missing: $out"

# --- --dry-run changes nothing ------------------------------------------------
KB="$(mktemp -d)/wiki"; seed_facts "$KB"
before="$(cat "$KB/facts/candidates.csv")"
"$PYTHON" -m factlog eject --fact X wrongrel Y --target "$KB" --dry-run >/dev/null 2>&1
[ "$(cat "$KB/facts/candidates.csv")" = "$before" ] && ok "fact mode --dry-run: no change" || bad "--dry-run mutated state"

# --- validation: mode mixing, --delete-original, neither, no-match -------------
KB="$(mktemp -d)/wiki"; seed_facts "$KB"
set +e
"$PYTHON" -m factlog eject a.md --fact X wrongrel Y --target "$KB" >/dev/null 2>&1; [ $? -eq 2 ] && ok "rejects source + --fact together" || bad "mode mixing not rejected"
"$PYTHON" -m factlog eject --fact X wrongrel Y --delete-original --target "$KB" >/dev/null 2>&1; [ $? -eq 2 ] && ok "rejects --delete-original in fact mode" || bad "--delete-original not rejected in fact mode"
"$PYTHON" -m factlog eject --target "$KB" >/dev/null 2>&1; [ $? -eq 2 ] && ok "rejects neither source nor --fact" || bad "empty invocation not rejected"
"$PYTHON" -m factlog eject --fact No Such Triple --target "$KB" >/dev/null 2>&1; [ $? -eq 1 ] && ok "fact mode: unknown triple errors (rc 1)" || bad "no-match triple did not error"
set -e

# =============================================================================
# Orphan mode: auto-detect and eject sources whose original is gone (#orphans)
# =============================================================================

# Seed a KB mixing: a live conversion (original present), an orphaned conversion
# (original deleted), a hand-placed conversion (no provenance), a live text
# source, and a cited-but-missing text source.
seed_orphans() {  # $1 = KB path
  local kb="$1"
  "$PYTHON" -m factlog init --target "$kb" >/dev/null
  printf 'PK\003\004\000' > "$kb/sources/live.pdf"                       # original present
  printf '<!-- ingested-by-factlog | source: live.pdf | converter: pdftotext | date: 2026-01-01T00:00:00Z -->\nlive\n' \
    > "$kb/runs/sources/live.md"
  printf '<!-- ingested-by-factlog | source: gone.docx | converter: pandoc | date: 2026-01-01T00:00:00Z -->\ngone\n' \
    > "$kb/runs/sources/gone.md"                                          # original gone.docx absent
  printf 'hand authored, no provenance header\n' > "$kb/runs/sources/hand.md"  # hand-placed
  printf 'plain\n' > "$kb/sources/keep.md"                               # live text source
  printf '%s\n%s\n%s\n%s\n%s\n%s\n' "$H" \
    'L,rel,M,runs/sources/live.md,confirmed,0.9,' \
    'G,rel,H,runs/sources/gone.md,confirmed,0.9,' \
    'N,rel,O,runs/sources/hand.md,confirmed,0.9,' \
    'K,rel,P,sources/keep.md,confirmed,0.9,' \
    'D,rel,E,sources/deleted.md,confirmed,0.9,' > "$kb/facts/candidates.csv"  # deleted.md has no file
  printf '[{"subject":"G","relation":"rel","object":"H","source":"runs/sources/gone.md","status":"confirmed","confidence":0.9,"note":""}]\n' \
    > "$kb/runs/2026-01-01-gone.json"
}

# --- default (supersede): only the orphans are retired ------------------------
KB="$(mktemp -d)/wiki"; seed_orphans "$KB"
out="$("$PYTHON" -m factlog eject --orphans --target "$KB" 2>&1)"
printf '%s\n' "$out"; echo "---"
printf '%s' "$out" | grep -qF "orphan scan" && ok "orphan mode announces the scan" || bad "no orphan scan header"
[ ! -f "$KB/runs/sources/gone.md" ] && ok "orphan conversion (original gone) deleted" || bad "orphan conversion kept"
[ -f "$KB/runs/sources/live.md" ] && ok "live conversion (original present) kept" || bad "live conversion wrongly deleted"
[ -f "$KB/runs/sources/hand.md" ] && ok "hand-placed conversion (no provenance) kept" || bad "hand-placed conversion wrongly ejected"
[ -f "$KB/sources/keep.md" ] && ok "live text source kept" || bad "live text source deleted"
grep -q "G,rel,H,runs/sources/gone.md,superseded," "$KB/facts/candidates.csv" && ok "orphan conversion fact superseded" || bad "orphan conversion fact not retired"
grep -q "D,rel,E,sources/deleted.md,superseded," "$KB/facts/candidates.csv" && ok "cited-but-missing source fact superseded" || bad "missing-file fact not retired"
grep -q "L,rel,M,runs/sources/live.md,confirmed," "$KB/facts/candidates.csv" && ok "live conversion fact preserved" || bad "live fact wrongly retired"
grep -q "N,rel,O,runs/sources/hand.md,confirmed," "$KB/facts/candidates.csv" && ok "hand-placed conversion fact preserved" || bad "hand-placed fact wrongly retired"
grep -q "K,rel,P,sources/keep.md,confirmed," "$KB/facts/candidates.csv" && ok "live text fact preserved" || bad "live text fact wrongly retired"
grep -q '"G", "rel", "H"' "$KB/facts/accepted.dl" && bad "orphan fact still in accepted.dl" || ok "orphan facts dropped from accepted.dl"
grep -q '"L", "rel", "M"' "$KB/facts/accepted.dl" && ok "live fact kept in accepted.dl" || bad "live fact lost from accepted.dl"

# --- --orphans --purge removes rows and strips runs/*.json --------------------
KB="$(mktemp -d)/wiki"; seed_orphans "$KB"
"$PYTHON" -m factlog eject --orphans --purge --target "$KB" >/dev/null 2>&1
grep -q "runs/sources/gone.md" "$KB/facts/candidates.csv" && bad "--orphans --purge left the orphan row" || ok "--orphans --purge deletes the orphan row"
[ ! -f "$KB/runs/2026-01-01-gone.json" ] && ok "--orphans --purge strips the emptied runs/*.json" || bad "runs json not stripped"
grep -q "sources/keep.md" "$KB/facts/candidates.csv" && ok "--orphans --purge keeps live rows" || bad "live rows wrongly purged"

# --- --dry-run changes nothing ------------------------------------------------
KB="$(mktemp -d)/wiki"; seed_orphans "$KB"
before="$(cat "$KB/facts/candidates.csv")"
"$PYTHON" -m factlog eject --orphans --dry-run --target "$KB" >/dev/null 2>&1
[ -f "$KB/runs/sources/gone.md" ] && [ "$(cat "$KB/facts/candidates.csv")" = "$before" ] \
  && ok "--orphans --dry-run leaves files and candidates.csv untouched" || bad "--orphans --dry-run mutated state"

# --- a clean KB (no orphans) reports none and exits 0, touching nothing -------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'plain\n' > "$KB/sources/ok.md"
printf '%s\n%s\n' "$H" 'A,rel,B,sources/ok.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
set +e; out="$("$PYTHON" -m factlog eject --orphans --target "$KB" 2>&1)"; rc=$?; set -e
[ "$rc" -eq 0 ] && printf '%s' "$out" | grep -qF "no orphaned sources found" && ok "clean KB: orphan scan finds none (rc 0)" || bad "clean KB orphan scan misbehaved (rc=$rc)"
grep -q "sources/ok.md,confirmed," "$KB/facts/candidates.csv" && ok "clean KB: rows untouched" || bad "clean KB rows changed"

# --- validation: --orphans cannot mix with source(s) or --fact ----------------
KB="$(mktemp -d)/wiki"; seed_orphans "$KB"
set +e
"$PYTHON" -m factlog eject --orphans live.pdf --target "$KB" >/dev/null 2>&1; [ $? -eq 2 ] && ok "rejects --orphans + source" || bad "--orphans + source not rejected"
"$PYTHON" -m factlog eject --orphans --fact G rel H --target "$KB" >/dev/null 2>&1; [ $? -eq 2 ] && ok "rejects --orphans + --fact" || bad "--orphans + --fact not rejected"
set -e

# --- edge cases: subdir original, NFC Korean name, empty/malformed header, ----
# --- fragment citation, malformed-not-under-root, uncited orphan conversion ---
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
mkdir -p "$KB/sources/sub"
# (a) original in a subdirectory, header records basename only -> kept
printf 'PK\003\004\000' > "$KB/sources/sub/deep.pdf"
printf '<!-- ingested-by-factlog | source: deep.pdf | converter: pdftotext | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/deep.md"
# (b) Korean-named original present -> its conversion kept (NFC compare)
printf 'PK\003\004\000' > "$KB/sources/한글문서.pdf"
printf '<!-- ingested-by-factlog | source: 한글문서.pdf | converter: pdftotext | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/한글문서.md"
# (c) empty source value in header -> no reliable origin -> kept (not an orphan)
printf '<!-- ingested-by-factlog | source:  | converter: pandoc | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/empty.md"
# (d) uncited orphan conversion (header original absent, no candidates row) -> deleted
printf '<!-- ingested-by-factlog | source: lonely.docx | converter: pandoc | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/lonely.md"
printf '%s\n%s\n%s\n%s\n%s\n' "$H" \
  'P,rel,Q,runs/sources/deep.md,confirmed,0.9,' \
  'R,rel,S,runs/sources/한글문서.md,confirmed,0.9,' \
  'T,rel,U,runs/sources/empty.md,confirmed,0.9,' \
  'V,rel,W,/etc/passwd,confirmed,0.9,' > "$KB/facts/candidates.csv"  # (e) malformed, not under a source root
out="$("$PYTHON" -m factlog eject --orphans --target "$KB" 2>&1)"
[ -f "$KB/runs/sources/deep.md" ] && ok "subdir original (basename header) keeps its conversion" || bad "subdir original wrongly orphaned"
[ -f "$KB/runs/sources/한글문서.md" ] && ok "NFC Korean original keeps its conversion" || bad "Korean original wrongly orphaned"
[ -f "$KB/runs/sources/empty.md" ] && ok "empty source: header is not parsed as an orphan" || bad "empty-header conversion wrongly ejected"
[ ! -f "$KB/runs/sources/lonely.md" ] && ok "uncited orphan conversion is cleaned up" || bad "uncited orphan conversion kept"
printf '%s' "$out" | grep -qF "/etc/passwd" && bad "malformed citation not under a source root was auto-ejected" || ok "malformed citation (not under sources/) never auto-ejected"

# --- fragment-cited orphan is still retired (anchor-insensitive match) ---------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf '<!-- ingested-by-factlog | source: frag.docx | converter: pandoc | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/frag.md"  # original frag.docx absent -> orphan
printf '%s\n%s\n' "$H" 'A,rel,B,runs/sources/frag.md#sec2,confirmed,0.9,' > "$KB/facts/candidates.csv"
"$PYTHON" -m factlog eject --orphans --target "$KB" >/dev/null 2>&1
grep -q ",superseded," "$KB/facts/candidates.csv" && ok "orphan fact cited with a #fragment is still retired" || bad "fragment-cited orphan not retired"

# --- same-basename originals in different subtrees: only the deleted one's -----
# --- mirrored conversion is orphaned, not the surviving sibling (#103) ---------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
mkdir -p "$KB/sources/a" "$KB/sources/b" "$KB/runs/sources/a" "$KB/runs/sources/b"
printf 'PK\003\004\000' > "$KB/sources/b/report.pdf"   # b survives; a/report.pdf is "deleted" (never created)
printf '<!-- ingested-by-factlog | source: report.pdf | converter: pdftotext | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/a/report.md"
printf '<!-- ingested-by-factlog | source: report.pdf | converter: pdftotext | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/b/report.md"
printf '%s\n%s\n%s\n' "$H" \
  'P,rel,Q,runs/sources/a/report.md,confirmed,0.9,' \
  'R,rel,S,runs/sources/b/report.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
"$PYTHON" -m factlog eject --orphans --target "$KB" >/dev/null 2>&1
[ ! -f "$KB/runs/sources/a/report.md" ] && ok "orphaned subdir conversion (deleted original) is ejected" || bad "same-basename collision masked the orphan"
[ -f "$KB/runs/sources/b/report.md" ] && ok "surviving subdir sibling's conversion is kept" || bad "surviving sibling wrongly orphaned"

# --- non-KB path errors -------------------------------------------------------
set +e; "$PYTHON" -m factlog eject anything --target "$(mktemp -d)" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -ne 0 ] && ok "eject on a non-KB path errors" || bad "non-KB path should error"

echo ""
echo "========================================"
echo "test_eject_cmd: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
