#!/usr/bin/env bash
# tests/test_coverage.sh — source coverage critic (#36)
#
# Pins:
#   - a source cited by >=1 ENGINE fact reports its count; 0-fact sources are gaps
#   - only engine-input rows count: a source backed solely by superseded /
#     needs_review rows is a gap, not "covered"
#   - TEXT gap vs BINARY gap distinguished; a binary under runs/sources/ gets a
#     distinct "ingest output should be text" message (not "run ingest")
#   - a fact citing a non-existent source file is reported as an ORPHAN
#   - default run is informational (exit 0) — even with no candidates.csv and on
#     an empty KB; --strict exits non-zero ONLY when a TEXT source is uncovered
#   - counting spans sources/ and runs/sources/; the '#anchor' is ignored
#
# Deterministic; no pyrewire.  Usage: bash tests/test_coverage.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
COV="$PLUGIN_ROOT/tools/coverage.py"
HEADER="subject,relation,object,source,status,confidence,note"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
csv() { printf '%s\n' "$HEADER" "$@" > "$KB/facts/candidates.csv"; }
run() { set +e; out="$("$PYTHON" "$COV" --wiki "$KB" "$@" 2>&1)"; rc=$?; set -e; }

# --- empty KB (init scaffolds an empty sources/, no candidates.csv) -----------
rm -f "$KB/facts/candidates.csv"
run
[ "$rc" -eq 0 ] && ok "missing candidates.csv exits 0 (not a hard fail)" || bad "missing CSV exit $rc"
printf '%s' "$out" | grep -qF "coverage: no source files" && ok "empty KB reports no source files" || bad "empty KB message missing"

# --- main matrix --------------------------------------------------------------
# a.md cited (via #anchor), b.md text-but-uncited, c.docx binary-uncited,
# d.md under runs/sources cited.
printf 'a content\n' > "$KB/sources/a.md"
printf 'b content\n' > "$KB/sources/b.md"
printf '\x00\x01bin\x00'  > "$KB/sources/c.docx"
mkdir -p "$KB/runs/sources"
printf 'd content\n' > "$KB/runs/sources/d.md"
csv \
  '갑봇,통합,을서비스,sources/a.md#sec,accepted,0.9,' \
  '구성_요소,포함,주_속성,runs/sources/d.md,accepted,0.9,'
run
[ "$rc" -eq 0 ] && ok "default run exits 0 (informational)" || bad "default exit $rc"
printf '%s' "$out" | grep -qE "1 fact\(s\): sources/a.md" && ok "cited source reports count (anchor stripped)" || bad "a.md count missing"
printf '%s' "$out" | grep -qE "1 fact\(s\): runs/sources/d.md" && ok "runs/sources cited source counted" || bad "d.md count missing"
printf '%s' "$out" | grep -qF "GAP (text, run /factlog sync): sources/b.md" && ok "uncited text source flagged" || bad "b.md text gap missing"
printf '%s' "$out" | grep -qF "GAP (binary, run factlog ingest): sources/c.docx" && ok "uncited binary under sources flagged" || bad "c.docx binary gap missing"
printf '%s' "$out" | grep -qF "4 source(s); 2 covered, 1 text gap(s), 1 binary needing conversion, 0 orphan citation(s)" && ok "summary tallies" || bad "summary tally wrong"

# --strict fails on the text gap.
run --strict
[ "$rc" -ne 0 ] && ok "--strict exits non-zero on text gap" || bad "--strict did not fail on text gap"

# --- engine-only counting: superseded / needs_review do NOT cover -------------
# b.md cited only by a superseded row, c.md only by needs_review -> both gaps.
rm -f "$KB/sources/c.docx"
printf 'c content\n' > "$KB/sources/c.md"
csv \
  '갑봇,통합,을서비스,sources/a.md,accepted,0.9,' \
  '구성_요소,포함,주_속성,runs/sources/d.md,accepted,0.9,' \
  '값가,대체,값나,sources/b.md,superseded,0.9,' \
  '항목,후보,자료,sources/c.md,needs_review,0.5,'
run
printf '%s' "$out" | grep -qF "GAP (text, run /factlog sync): sources/b.md" && ok "superseded-only source is a gap" || bad "superseded source falsely covered"
printf '%s' "$out" | grep -qF "GAP (text, run /factlog sync): sources/c.md" && ok "needs_review-only source is a gap" || bad "needs_review source falsely covered"
printf '%s' "$out" | grep -qE "0 fact\(s\): sources/b.md" && ok "superseded source counts 0 engine facts" || bad "superseded counted as fact"

# --- binary under runs/sources/ gets the distinct anomaly message -------------
printf '\x00\x01bin\x00' > "$KB/runs/sources/conv.pdf"
csv '갑봇,통합,을서비스,sources/a.md,accepted,0.9,' '구성_요소,포함,주_속성,runs/sources/d.md,accepted,0.9,'
rm -f "$KB/sources/b.md" "$KB/sources/c.md"
run
printf '%s' "$out" | grep -qF "GAP (binary under runs/sources — ingest output should be text): runs/sources/conv.pdf" && ok "binary under runs/sources gets anomaly message" || bad "runs/sources binary message wrong"
rm -f "$KB/runs/sources/conv.pdf"

# --- orphan citation: fact cites a file that does not exist -------------------
csv '갑봇,통합,을서비스,sources/a.md,accepted,0.9,' '유령,참조,대상,sources/ghost.md,accepted,0.9,'
run
printf '%s' "$out" | grep -qF "ORPHAN citation (source file missing): sources/ghost.md" && ok "orphan citation reported" || bad "orphan citation missing"
printf '%s' "$out" | grep -qF "1 orphan citation(s)" && ok "orphan count in summary" || bad "orphan count missing"
[ "$rc" -eq 0 ] && ok "orphan alone does not fail default run" || bad "orphan caused non-zero exit"

# --- all text sources covered -> --strict clean -------------------------------
csv '갑봇,통합,을서비스,sources/a.md,accepted,0.9,' '구성_요소,포함,주_속성,runs/sources/d.md,accepted,0.9,'
run --strict
[ "$rc" -eq 0 ] && ok "--strict clean when all text sources covered" || bad "--strict false-positive"

# --- NFC/NFD: an NFD-named source cited NFC is covered, not orphan (#64) ------
NFCKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$NFCKB" >/dev/null
FACTLOG_NFCKB="$NFCKB" "$PYTHON" - <<'PY'
import os, unicodedata
kb = os.environ["FACTLOG_NFCKB"]
nfd = unicodedata.normalize("NFD", "각노트.md")
open(os.path.join(kb, "sources", nfd), "w", encoding="utf-8").write("내용\n")   # NFD on disk
H = "subject,relation,object,source,status,confidence,note\n"
open(os.path.join(kb, "facts", "candidates.csv"), "w", encoding="utf-8").write(
    H + "갑봇,포함,값가,sources/각노트.md,accepted,0.9,\n")               # NFC citation
PY
nout="$("$PYTHON" "$COV" --wiki "$NFCKB" 2>&1)"
printf '%s' "$nout" | grep -qE "1 fact\(s\): sources/각노트.md" && ok "NFD-named source cited NFC is covered (not 0)" || bad "NFD source shows 0 facts"
printf '%s' "$nout" | grep -qF "ORPHAN" && bad "NFD source falsely reported as orphan" || ok "no orphan false-positive for NFD-named source"

echo ""
echo "========================================"
echo "test_coverage: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
