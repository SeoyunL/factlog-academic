#!/usr/bin/env bash
# tests/test_sources_cmd.sh — `factlog sources` listing (#65) + NFC matching (#64)
#
# Pins (XDG-isolated; NFD/NFC written explicitly so it is deterministic):
#   - lists each original source, its conversion, and fact count
#   - an NFD-named original maps to its NFC-cited conversion (not split / 0)
#   - total fact count is reported; uses the active KB when no --target
#   - empty KB (no sources) is handled gracefully
#
# Usage: bash tests/test_sources_cmd.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62)

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null   # also records active KB

# Build a binary original with an NFD name + its NFC-named conversion + a candidate
# row that cites the conversion in NFC. Mirrors the macOS ingest situation.
FACTLOG_KB="$KB" "$PYTHON" - <<'PY'
import os, unicodedata
kb = os.environ["FACTLOG_KB"]
nfc = "각문서.docx"
nfd = unicodedata.normalize("NFD", nfc)
assert nfc != nfd
open(os.path.join(kb, "sources", nfd), "wb").write(b"\x00\x01bin")      # binary original, NFD name
open(os.path.join(kb, "runs", "sources", "각문서.md"), "w", encoding="utf-8").write("내용\n")  # conversion, NFC
# an ASCII text source with a direct citation, too
open(os.path.join(kb, "sources", "note.md"), "w", encoding="utf-8").write("x\n")
H = "subject,relation,object,source,status,confidence,note\n"
rows = [
    "갑봇,포함,값가,runs/sources/각문서.md,accepted,0.9,",       # NFC citation -> NFD-named original
    "갑봇,포함,값나,runs/sources/각문서.md,accepted,0.9,",
    "을서비스,포함,값다,sources/note.md,accepted,0.9,",
]
open(os.path.join(kb, "facts", "candidates.csv"), "w", encoding="utf-8").write(H + "\n".join(rows) + "\n")
PY

# Run from a different cwd with no --target (uses active KB).
out="$(cd /tmp && "$PYTHON" -m factlog sources 2>&1)"
printf '%s\n' "$out"
echo "---"
printf '%s' "$out" | grep -qF "sources/각문서.docx" && printf '%s' "$out" | grep -qF "→  runs/sources/각문서.md" \
  && ok "NFD-named original maps to its NFC-cited conversion" || bad "NFD original/conversion not mapped"
printf '%s' "$out" | grep -qE "\[ *2\] sources/각문서.docx" && ok "fact count attributed to the conversion (2)" || bad "fact count wrong for NFD source"
printf '%s' "$out" | grep -qE "\[ *1\] sources/note.md" && ok "ASCII text source counted directly (1)" || bad "ASCII source miscounted"
printf '%s' "$out" | grep -qF "2 source(s), 3 fact(s)" && ok "summary totals correct" || bad "summary total wrong"
# the conversion is mapped onto its original's line, not listed as a separate entry
[ "$(printf '%s' "$out" | grep -c "각문서")" -eq 1 ] && ok "conversion mapped under original (single line, not double-listed)" || bad "conversion double-listed"

# empty KB graceful
KB2="$(mktemp -d)/wiki2"
"$PYTHON" -m factlog init --target "$KB2" >/dev/null
out="$("$PYTHON" -m factlog sources --target "$KB2" 2>&1)"
printf '%s' "$out" | grep -qF "0 source(s), 0 fact(s)" && ok "empty KB -> 0 sources, 0 facts" || bad "empty KB not graceful: $out"

echo ""
echo "========================================"
echo "test_sources_cmd: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
