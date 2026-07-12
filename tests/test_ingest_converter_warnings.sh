#!/usr/bin/env bash
# tests/test_ingest_converter_warnings.sh — #239: a converter that exits 0 but
# writes a quality warning to stderr must SURFACE that warning, not swallow it.
#
# pandoc converts a cp949 RTF with exit 0 while warning "Unsupported code page
# 949. Text will likely be garbled." on stderr. cmd_ingest judged success by
# returncode alone and printed stderr only on failure, so the mojibake entered
# extraction as prose silently — the same harm #222 killed, in a new mask.
#
# This harness stubs pandoc on PATH (exit 0, garbled body, stderr warning) so it
# runs anywhere, with no dependency on a real cp949 file or a real pandoc. It
# uses .epub, whose converter chain is pandoc-only (no textutil fallback), so the
# stub is always the chosen tool.
#
# Usage: bash tests/test_ingest_converter_warnings.sh
#   Returns 0 if all checks pass, 1 if any fail.

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62)

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

# --- fake pandoc: always exit 0 and write a body; warn on stderr only for a
#     source whose name contains "garble" (so one stub covers both the warning
#     case and the clean control) -----------------------------------------------
STUB_DIR="$(mktemp -d)"
cat > "$STUB_DIR/pandoc" <<'STUB'
#!/usr/bin/env bash
src=""; dst=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) dst="$2"; shift 2;;
    -*) shift;;
    *) [ -z "$src" ] && src="$1"; shift;;
  esac
done
printf 'converted body text that is not empty\n' > "$dst"
case "$src" in
  *garble*) printf '[WARNING] Unsupported code page 949. Text will likely be garbled.\n' >&2;;
esac
exit 0
STUB
chmod +x "$STUB_DIR/pandoc"
export PATH="$STUB_DIR:$PATH"

FACTLOG=("$PYTHON" -m factlog)

KB="$(mktemp -d)/wiki"
"${FACTLOG[@]}" init --target "$KB" >/dev/null

# ---------------------------------------------------------------------------
# (a) a converter warning on the success path is surfaced, not swallowed
# ---------------------------------------------------------------------------
printf 'binary-ish epub bytes\n' > "$KB/sources/garble.epub"
warnout="$("${FACTLOG[@]}" ingest "$KB/sources/garble.epub" --target "$KB" 2>&1)"; wrc=$?

[ "$wrc" -eq 0 ] \
  && ok "(a) a converter warning does not fail the run (exit 0 — warning, not error)" \
  || bad "(a) ingest exited non-zero on a warning-only conversion (rc=$wrc)"
printf '%s' "$warnout" | grep -qF "Unsupported code page 949" \
  && ok "(a) the converter's stderr warning is surfaced to the operator" \
  || bad "(a) the converter warning was SWALLOWED (the #239 bug): $warnout"

# ---------------------------------------------------------------------------
# (b) the warning conversion is counted separately (converted-with-warnings),
#     NOT rolled into a clean `converted` — the operator sees a distinct signal
# ---------------------------------------------------------------------------
printf '%s' "$warnout" | grep -qiE "converted-with-warnings" \
  && ok "(b) the conversion is counted as converted-with-warnings" \
  || bad "(b) no converted-with-warnings signal (rolled into a clean count?): $warnout"
sumline="$(printf '%s' "$warnout" | grep -E "^factlog ingest: [0-9]+ converted")"
printf '%s' "$sumline" | grep -qE "0 converted," \
  && ok "(b) a warned conversion is split OUT of the clean converted count (0 converted)" \
  || bad "(b) the warned conversion still counted as clean-converted: $sumline"

# ---------------------------------------------------------------------------
# (c) the body is still written — a warning is visibility, not a block
# ---------------------------------------------------------------------------
OUT="$KB/runs/sources/garble.epub.md"
if [ -f "$OUT" ] && grep -qF "converted body text" "$OUT"; then
  ok "(c) the conversion is still written to disk (warning does not discard it)"
else
  bad "(c) the warned conversion was not written / lost its body"
fi

# ---------------------------------------------------------------------------
# (d) control: a clean conversion (no stderr) is a plain success — no false
#     warning, counted as converted, no converted-with-warnings noise
# ---------------------------------------------------------------------------
printf 'binary-ish epub bytes\n' > "$KB/sources/clean.epub"
cleanout="$("${FACTLOG[@]}" ingest "$KB/sources/clean.epub" --target "$KB" 2>&1)"
printf '%s' "$cleanout" | grep -qiE "converted-with-warnings" \
  && bad "(d) a clean conversion was wrongly flagged converted-with-warnings: $cleanout" \
  || ok "(d) a clean conversion is a plain success (no false warning)"
printf '%s' "$cleanout" | grep -E "^factlog ingest: [0-9]+ converted" | grep -qE "^factlog ingest: 1 converted" \
  && ok "(d) a clean conversion is counted as a normal converted" \
  || bad "(d) a clean conversion was not counted as converted: $cleanout"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo "test_ingest_converter_warnings: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
