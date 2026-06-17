#!/usr/bin/env bash
# tests/test_nfc_sources.sh — Unicode NFC/NFD source matching (#57)
#
# Pins (deterministic on any platform — both encodings are written explicitly):
#   - a source file whose on-disk name is NFD (decomposed Korean) is matched by
#     a candidate row whose source is NFC (composed) — the fact is NOT dropped
#   - the source written to candidates.csv is NFC-normalised
#   - an ASCII-named source is unaffected (no regression)
#
# Usage: bash tests/test_nfc_sources.sh

set -euo pipefail

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

# Create an NFD-named source file + an NFC-sourced candidate row, plus an ASCII
# control. The candidate JSON is the merge input (runs/*.json).
FACTLOG_NFC_KB="$KB" "$PYTHON" - <<'PY'
import json, os, unicodedata
kb = os.environ["FACTLOG_NFC_KB"]
name_nfc = "각문서.md"                       # composed
name_nfd = unicodedata.normalize("NFD", name_nfc)  # decomposed (≠ as bytes)
assert name_nfc != name_nfd, "expected NFC/NFD to differ"
# source file on disk uses the NFD name
open(os.path.join(kb, "sources", name_nfd), "w", encoding="utf-8").write("내용\n")
open(os.path.join(kb, "sources", "ascii.md"), "w", encoding="utf-8").write("x\n")
# candidate rows cite the NFC source path + an ASCII control
rows = [
    {"subject": "갑봇", "relation": "포함", "object": "값가",
     "source": "sources/" + name_nfc, "status": "accepted", "confidence": "0.9", "note": ""},
    {"subject": "을서비스", "relation": "포함", "object": "값나",
     "source": "sources/ascii.md", "status": "accepted", "confidence": "0.9", "note": ""},
]
json.dump(rows, open(os.path.join(kb, "runs", "extract.json"), "w", encoding="utf-8"), ensure_ascii=False)
print("NFC repr:", repr("sources/" + name_nfc))
PY

"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1

# The NFD-on-disk / NFC-cited fact must survive (not dropped).
grep -qF "갑봇,포함,값가" "$KB/facts/candidates.csv" && ok "NFD-named source matched by NFC candidate (fact kept)" || bad "NFC/NFD mismatch dropped the fact"
# ASCII control survives too.
grep -qF "을서비스,포함,값나" "$KB/facts/candidates.csv" && ok "ASCII-named source unaffected" || bad "ASCII source regressed"

# Source stored in candidates.csv is NFC.
verdict="$(FACTLOG_NFC_KB="$KB" "$PYTHON" - <<'PY'
import csv, os, unicodedata
kb = os.environ["FACTLOG_NFC_KB"]
srcs = [r["source"] for r in csv.DictReader(open(os.path.join(kb, "facts", "candidates.csv"), encoding="utf-8"))]
kor = [s for s in srcs if "각" in unicodedata.normalize("NFC", s)]
ok = kor and all(s == unicodedata.normalize("NFC", s) for s in kor)
print("OK" if ok else f"FAIL: stored sources not NFC: {kor!r}")
PY
)"
[ "$verdict" = "OK" ] && ok "candidates.csv stores the source as NFC" || bad "$verdict"

echo ""
echo "========================================"
echo "test_nfc_sources: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
