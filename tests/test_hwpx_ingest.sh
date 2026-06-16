#!/usr/bin/env bash
# tests/test_hwpx_ingest.sh — native .hwpx (Hancom OWPML) ingest (#53)
#
# Pins (with a SYNTHETIC hwpx fixture — a zip of OWPML XML, no real content):
#   - a .hwpx ingests to runs/sources/<stem>.md via the built-in converter,
#     with the standard provenance header
#   - paragraph text is extracted; inline tags are stripped; entities are
#     unescaped; empty paragraphs are dropped; multiple <hp:t> runs join
#   - text from MULTIPLE Contents/section*.xml is included
#   - --scan auto-discovers and converts a hwpx in sources/
#   - a corrupt / section-less hwpx fails gracefully (explicit: non-zero;
#     under --scan it is reported but does not fail the run)
#
# No external converter needed (stdlib zip/XML). Usage: bash tests/test_hwpx_ingest.sh

set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null

# Build a synthetic hwpx: a zip with Contents/section0.xml + section1.xml.
# section0 exercises: multi-run join, inline-tag strip, entity unescape, empty para.
make_hwpx() {  # $1 = dest path
  FACTLOG_HWPX_DST="$1" "$PYTHON" - <<'PY'
import os, zipfile
dst = os.environ["FACTLOG_HWPX_DST"]
sec0 = (
    '<?xml version="1.0"?><hml>'
    '<hp:p><hp:t>첫째 </hp:t><hp:t>문단</hp:t></hp:p>'          # two runs join -> "첫째 문단"
    '<hp:p><hp:t>태그<hp:lineBreak/>제거</hp:t></hp:p>'         # inline tag stripped -> "태그제거"
    '<hp:p><hp:t>A &amp; B</hp:t></hp:p>'                       # entity unescape -> "A & B"
    '<hp:p><hp:t charPrIDRef="3">스타일런</hp:t></hp:p>'        # attributed run -> "스타일런"
    '<hp:p></hp:p>'                                             # empty paragraph -> dropped
    '<hp:p><hp:t>   </hp:t></hp:p>'                             # whitespace-only -> dropped
    '</hml>'
)
sec1 = '<?xml version="1.0"?><hml><hp:p><hp:t>둘째 섹션</hp:t></hp:p></hml>'
with zipfile.ZipFile(dst, "w") as z:
    z.writestr("version.xml", "<v/>")
    z.writestr("Contents/section0.xml", sec0)
    z.writestr("Contents/section1.xml", sec1)
PY
}

# --- explicit ingest ----------------------------------------------------------
make_hwpx "$KB/sources/sample.hwpx"
set +e; out="$("$PYTHON" -m factlog ingest --target "$KB" "$KB/sources/sample.hwpx" 2>&1)"; rc=$?; set -e
md="$KB/runs/sources/sample.md"
[ "$rc" -eq 0 ] && [ -f "$md" ] && ok "hwpx ingests to runs/sources/<stem>.md (rc 0)" || bad "hwpx ingest failed (rc=$rc)"
head -1 "$md" | grep -qF "ingested-by-factlog" && head -1 "$md" | grep -qF "factlog-hwpx" && ok "provenance header written" || bad "provenance header missing"
grep -qx "첫째 문단" "$md" && ok "multiple <hp:t> runs join into one line" || bad "runs not joined"
grep -qx "태그제거" "$md" && ok "inline tags stripped" || bad "inline tag not stripped"
grep -qx "A & B" "$md" && ok "entities unescaped" || bad "entity not unescaped"
grep -qx "스타일런" "$md" && ok "attributed <hp:t charPrIDRef=..> run extracted" || bad "attributed run silently dropped"
grep -qx "둘째 섹션" "$md" && ok "text from a second section included" || bad "section1 text missing"
[ "$(grep -c '^$' "$md")" -ge 0 ] && ! grep -qx "   " "$md" && ok "empty/whitespace paragraphs dropped" || bad "empty paragraph leaked"

# --- --scan auto-discovery ----------------------------------------------------
rm -f "$md"
make_hwpx "$KB/sources/scanned.hwpx"
set +e; "$PYTHON" -m factlog ingest --target "$KB" --scan >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 0 ] && [ -f "$KB/runs/sources/scanned.md" ] && ok "--scan auto-discovers and converts hwpx" || bad "--scan did not convert hwpx (rc=$rc)"

# --- corrupt hwpx: explicit fails, --scan reports but does not fail ------------
printf 'not a zip file' > "$KB/sources/broken.hwpx"
set +e; out="$("$PYTHON" -m factlog ingest --target "$KB" "$KB/sources/broken.hwpx" 2>&1)"; rc=$?; set -e
[ "$rc" -ne 0 ] && printf '%s' "$out" | grep -qF "failed on broken.hwpx" && ok "corrupt hwpx (explicit) fails with a clear message" || bad "corrupt hwpx not handled"
set +e; "$PYTHON" -m factlog ingest --target "$KB" --scan >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 0 ] && ok "corrupt hwpx under --scan does not fail the run" || bad "--scan failed on a stray corrupt file (rc=$rc)"

# --- hwpx with no section XML fails gracefully --------------------------------
FACTLOG_HWPX_DST="$KB/sources/nosec.hwpx" "$PYTHON" - <<'PY'
import os, zipfile
with zipfile.ZipFile(os.environ["FACTLOG_HWPX_DST"], "w") as z:
    z.writestr("version.xml", "<v/>")
PY
set +e; out="$("$PYTHON" -m factlog ingest --target "$KB" "$KB/sources/nosec.hwpx" 2>&1)"; rc=$?; set -e
[ "$rc" -ne 0 ] && printf '%s' "$out" | grep -qF "failed on nosec.hwpx" && ok "section-less hwpx fails gracefully" || bad "section-less hwpx not handled"

echo ""
echo "========================================"
echo "test_hwpx_ingest: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
