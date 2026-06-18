#!/usr/bin/env bash
# tests/test_pptx_ingest.sh — native .pptx (PowerPoint OOXML) ingest
#
# Pins (with a SYNTHETIC pptx fixture — a zip of OOXML XML, no real content):
#   - a .pptx ingests to runs/sources/<stem>.md via the built-in converter,
#     with the standard provenance header
#   - paragraph text is extracted; inline tags are stripped; entities are
#     unescaped; empty paragraphs are dropped; multiple <a:t> runs join
#   - text from MULTIPLE ppt/slides/slideN.xml is included, in NUMERIC order
#     (slide10 after slide2, not lexicographic)
#   - --scan auto-discovers and converts a pptx in sources/
#   - a corrupt / slide-less pptx fails gracefully (explicit: non-zero;
#     under --scan it is reported but does not fail the run)
#
# No external converter needed (stdlib zip/XML). Usage: bash tests/test_pptx_ingest.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null

# Build a synthetic pptx: a zip with ppt/slides/slide1.xml, slide2.xml, slide10.xml.
# slide1 exercises: multi-run join, inline-tag strip, entity unescape, empty para.
# slide10 vs slide2 exercises numeric (not lexicographic) slide ordering.
make_pptx() {  # $1 = dest path
  FACTLOG_PPTX_DST="$1" "$PYTHON" - <<'PY'
import os, zipfile
dst = os.environ["FACTLOG_PPTX_DST"]
slide1 = (
    '<?xml version="1.0"?><p:sld xmlns:a="x" xmlns:p="y">'
    '<a:p><a:r><a:t>첫째 </a:t></a:r><a:r><a:t>줄</a:t></a:r></a:p>'   # two runs join -> "첫째 줄"
    '<a:p><a:t>태그<a:br/>제거</a:t></a:p>'                            # inline tag stripped -> "태그제거"
    '<a:p><a:t>A &amp; B</a:t></a:p>'                                  # entity unescape -> "A & B"
    '<a:p><a:t lang="ko-KR">스타일런</a:t></a:p>'                      # attributed run -> "스타일런"
    '<a:p></a:p>'                                                      # empty paragraph -> dropped
    '<a:p><a:t>   </a:t></a:p>'                                        # whitespace-only -> dropped
    '</p:sld>'
)
slide2 = '<?xml version="1.0"?><p:sld><a:p><a:t>둘째 슬라이드</a:t></a:p></p:sld>'
slide10 = '<?xml version="1.0"?><p:sld><a:p><a:t>열번째 슬라이드</a:t></a:p></p:sld>'
with zipfile.ZipFile(dst, "w") as z:
    z.writestr("[Content_Types].xml", "<Types/>")
    z.writestr("ppt/slides/slide1.xml", slide1)
    z.writestr("ppt/slides/slide2.xml", slide2)
    z.writestr("ppt/slides/slide10.xml", slide10)
PY
}

# --- explicit ingest ----------------------------------------------------------
make_pptx "$KB/sources/deck.pptx"
set +e; out="$("$PYTHON" -m factlog ingest --target "$KB" "$KB/sources/deck.pptx" 2>&1)"; rc=$?; set -e
md="$KB/runs/sources/deck.md"
[ "$rc" -eq 0 ] && [ -f "$md" ] && ok "pptx ingests to runs/sources/<stem>.md (rc 0)" || bad "pptx ingest failed (rc=$rc): $out"
head -1 "$md" | grep -qF "ingested-by-factlog" && head -1 "$md" | grep -qF "factlog-pptx" && ok "provenance header written" || bad "provenance header missing"
grep -qx "첫째 줄" "$md" && ok "multiple <a:t> runs join into one line" || bad "runs not joined"
grep -qx "태그제거" "$md" && ok "inline tags stripped" || bad "inline tag not stripped"
grep -qx "A & B" "$md" && ok "entities unescaped" || bad "entity not unescaped"
grep -qx "스타일런" "$md" && ok "attributed <a:t lang=..> run extracted" || bad "attributed run silently dropped"
grep -qx "둘째 슬라이드" "$md" && ok "text from a second slide included" || bad "slide2 text missing"
grep -qx "열번째 슬라이드" "$md" && ok "text from slide10 included" || bad "slide10 text missing"
! grep -qx "   " "$md" && ok "empty/whitespace paragraphs dropped" || bad "empty paragraph leaked"
# numeric slide ordering: slide2 line must precede slide10 line in the output.
awk '/^둘째 슬라이드$/{s2=NR} /^열번째 슬라이드$/{s10=NR} END{exit !(s2 && s10 && s2 < s10)}' "$md" \
  && ok "slides ordered numerically (slide2 before slide10)" || bad "slide ordering is lexicographic (slide10 before slide2)"

# --- --scan auto-discovery ----------------------------------------------------
rm -f "$md"
make_pptx "$KB/sources/scanned.pptx"
set +e; "$PYTHON" -m factlog ingest --target "$KB" --scan >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 0 ] && [ -f "$KB/runs/sources/scanned.md" ] && ok "--scan auto-discovers and converts pptx" || bad "--scan did not convert pptx (rc=$rc)"

# --- corrupt pptx: explicit fails, --scan reports but does not fail ------------
printf 'not a zip file' > "$KB/sources/broken.pptx"
set +e; out="$("$PYTHON" -m factlog ingest --target "$KB" "$KB/sources/broken.pptx" 2>&1)"; rc=$?; set -e
[ "$rc" -ne 0 ] && printf '%s' "$out" | grep -qF "failed on broken.pptx" && ok "corrupt pptx (explicit) fails with a clear message" || bad "corrupt pptx not handled"
set +e; "$PYTHON" -m factlog ingest --target "$KB" --scan >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 0 ] && ok "corrupt pptx under --scan does not fail the run" || bad "--scan failed on a stray corrupt file (rc=$rc)"

# --- pptx with no slide XML fails gracefully ----------------------------------
FACTLOG_PPTX_DST="$KB/sources/noslide.pptx" "$PYTHON" - <<'PY'
import os, zipfile
with zipfile.ZipFile(os.environ["FACTLOG_PPTX_DST"], "w") as z:
    z.writestr("[Content_Types].xml", "<Types/>")
PY
set +e; out="$("$PYTHON" -m factlog ingest --target "$KB" "$KB/sources/noslide.pptx" 2>&1)"; rc=$?; set -e
[ "$rc" -ne 0 ] && printf '%s' "$out" | grep -qF "failed on noslide.pptx" && ok "slide-less pptx fails gracefully" || bad "slide-less pptx not handled"

echo ""
echo "========================================"
echo "test_pptx_ingest: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
