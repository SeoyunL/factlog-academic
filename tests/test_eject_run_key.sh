#!/usr/bin/env bash
# tests/test_eject_run_key.sh — eject's runs/*.json matcher uses merge's key (#562)
#
# merge STRIPS a run row's `source` on the way in (its loader and clean_row both
# do), so a run row written as "sources/live.md  " is a LIVE row that keeps
# re-asserting the fact. eject's runs matcher did not strip, so it silently
# matched nothing: the command reported success — deleting the conversion, the
# candidate row, and with --delete-original the user's own original — while the
# run row survived. The cleanup loop never closed (#480 shape).
#
# Pins:
#   - a padded run source IS stripped by eject (the run row actually disappears)
#   - after --purge the fact does NOT come back on the next merge (no #480 revival)
#   - the OPPOSITE direction is unchanged: a candidates.csv row whose source
#     carries whitespace is still NOT matched. That asymmetry is deliberate and
#     documented — source_coverage.eject_visible_refs derives what this command
#     can act on from the unstripped CSV rule, so widening it there would break a
#     promise that report makes. Only the runs side moved.
#   - an NFD run source (run / csv / disk in different Unicode forms) is matched
#
# Deterministic; no pyrewire required. Usage: bash tests/test_eject_run_key.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62)

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
MERGE="$PLUGIN_ROOT/tools/merge_candidates.py"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

H="subject,relation,object,source,status,confidence,note"

# --- 1. padded run source: the run row is stripped ----------------------------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'live text\n' > "$KB/sources/live.md"
printf '%s\n%s\n' "$H" 'L,rel,M,sources/live.md,confirmed,0.90,' > "$KB/facts/candidates.csv"
# NOTE the trailing spaces: merge strips them, so this row is alive to merge.
printf '[{"subject":"L","relation":"rel","object":"M","source":"sources/live.md  ","status":"candidate","confidence":0.9,"note":""}]\n' \
  > "$KB/runs/r.json"

out="$("$PYTHON" -m factlog eject sources/live.md --target "$KB" 2>&1)"
printf '%s\n' "$out"; echo "---"
[ ! -f "$KB/runs/r.json" ] && ok "padded run row stripped (emptied run file removed)" \
  || bad "padded run source not stripped: $(cat "$KB/runs/r.json")"
printf '%s' "$out" | grep -qF "1 run row(s) stripped" && ok "eject reports the stripped run row" \
  || bad "eject reported no stripped run row"
grep -q "L,rel,M,sources/live.md,superseded," "$KB/facts/candidates.csv" \
  && ok "citing candidate row superseded" || bad "candidate row not superseded"

# --- 2. --purge + a padded run row: the fact does not come back (#480) --------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'live text\n' > "$KB/sources/live.md"
printf '%s\n%s\n' "$H" 'L,rel,M,sources/live.md,confirmed,0.90,' > "$KB/facts/candidates.csv"
printf '[{"subject":"L","relation":"rel","object":"M","source":"sources/live.md  ","status":"candidate","confidence":0.9,"note":""}]\n' \
  > "$KB/runs/r.json"

"$PYTHON" -m factlog eject sources/live.md --purge --target "$KB" >/dev/null 2>&1
grep -q "^L,rel,M," "$KB/facts/candidates.csv" && bad "purged row still in candidates.csv" \
  || ok "purged row removed from candidates.csv"
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1 || true
grep -q "^L,rel,M," "$KB/facts/candidates.csv" \
  && bad "purged fact was resurrected by the next merge (#480)" \
  || ok "purged fact stays gone across a re-merge"

# --- 3. opposite direction: a padded candidates.csv source is still NOT matched -
# The CSV matcher must stay unstripped (source_coverage documents the asymmetry).
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'live text\n' > "$KB/sources/live.md"
{ printf '%s\n' "$H"
  printf 'P,rel,Q, sources/live.md,confirmed,0.90,\n'      # leading space
  printf 'R,rel,S,sources/live.md  ,confirmed,0.90,\n'     # trailing spaces
  printf 'T,rel,U,sources/live.md,confirmed,0.90,\n'; } > "$KB/facts/candidates.csv"

"$PYTHON" -m factlog eject sources/live.md --target "$KB" >/dev/null 2>&1
grep -q "^P,rel,Q, sources/live.md,confirmed," "$KB/facts/candidates.csv" \
  && ok "leading-space candidates row still not matched (asymmetry kept)" \
  || bad "csv matcher started stripping — source_coverage's documented rule broke"
grep -q "^R,rel,S,sources/live.md  ,confirmed," "$KB/facts/candidates.csv" \
  && ok "trailing-space candidates row still not matched (asymmetry kept)" \
  || bad "csv matcher started stripping — source_coverage's documented rule broke"
grep -q "^T,rel,U,sources/live.md,superseded," "$KB/facts/candidates.csv" \
  && ok "the exactly-spelled candidates row is still retired" \
  || bad "exact candidates row not retired"

# --- 4. NFD run source: run / csv / disk in different Unicode forms ------------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
# The original is created NFC; the candidates row cites it NFC; the run row cites
# the NFD spelling WITH padding — the combination merge folds+strips into one ref.
"$PYTHON" - "$KB" "$H" <<'PY'
import json, pathlib, sys, unicodedata
kb = pathlib.Path(sys.argv[1]); header = sys.argv[2]
nfc = unicodedata.normalize("NFC", "한글문서.md")
nfd = unicodedata.normalize("NFD", "한글문서.md")
(kb / "sources" / nfc).write_text("한글 본문\n", encoding="utf-8")
(kb / "facts" / "candidates.csv").write_text(
    f"{header}\n가,rel,나,sources/{nfc},confirmed,0.90,\n", encoding="utf-8"
)
(kb / "runs" / "r.json").write_text(
    json.dumps(
        [{"subject": "가", "relation": "rel", "object": "나",
          "source": f" sources/{nfd} ", "status": "candidate",
          "confidence": 0.9, "note": ""}],
        ensure_ascii=False,
    )
    + "\n",
    encoding="utf-8",
)
PY
NFC_NAME="$("$PYTHON" -c 'import unicodedata;print(unicodedata.normalize("NFC","한글문서.md"))')"
out="$("$PYTHON" -m factlog eject "sources/$NFC_NAME" --target "$KB" 2>&1)"
printf '%s\n' "$out"; echo "---"
[ ! -f "$KB/runs/r.json" ] && ok "NFD+padded run row stripped" \
  || bad "NFD run source not stripped: $(cat "$KB/runs/r.json")"
grep -q ",superseded," "$KB/facts/candidates.csv" && ok "NFC candidates row superseded" \
  || bad "NFC candidates row not superseded"

echo ""
echo "========================================"
echo "test_eject_run_key: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
