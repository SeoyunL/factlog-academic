#!/usr/bin/env bash
# tests/test_nontext_sources.sh — merge_candidates.py binary-source warning (#1)
#
# The in-session fact extraction reads sources/ files as text, so binary
# formats (.docx, .pdf, images, ...) are silently non-ingested. This test pins
# the warning behaviour added for issue #1:
#   - non-text source files are reported (by KB-relative path) on stderr,
#   - genuine text sources are NOT reported,
#   - --strict turns their presence into a non-zero exit.
#
# Uses python3 directly; merge_candidates.py does not require pyrewire.
#
# Usage: bash tests/test_nontext_sources.sh
#   Returns 0 if all checks pass, 1 if any fail.

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MERGE="$PLUGIN_ROOT/tools/merge_candidates.py"
PYTHON="${PYTHON:-python3}"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

# ---------------------------------------------------------------------------
# Build a minimal KB with one text source and two binary sources.
# ---------------------------------------------------------------------------
KB="$(mktemp -d)/kb"
mkdir -p "$KB"/{sources,pages,facts,decisions,policy,policy/prompts,runs}

printf '# Note\n\nAcme uses Python.\n' > "$KB/sources/note.md"
# PNG magic: high byte + embedded NUL  -> non-text
printf '\x89PNG\r\n\x1a\n\x00\x00IHDRbinary' > "$KB/sources/diagram.png"
# DOCX is a zip: PK magic + NUL bytes  -> non-text
printf 'PK\x03\x04\x00\x00\x00\x00binarypayload' > "$KB/sources/report.docx"
# A hidden system file must be ignored (no warning noise)
printf '\x00\x00\x00' > "$KB/sources/.DS_Store"

export FACTLOG_ROOT="$KB"

# ---------------------------------------------------------------------------
# Run 1 (default): merge succeeds (exit 0) and warns about the binary files.
# ---------------------------------------------------------------------------
ERR="$(mktemp)"
run_exit=0
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>"$ERR" || run_exit=$?

if [ "$run_exit" -eq 0 ]; then ok "default run exits 0 despite binary sources"; else bad "default run should exit 0, got $run_exit"; fi
if grep -qE "binary source file\(s\)" "$ERR"; then ok "warning header emitted"; else bad "missing non-text warning header"; fi
if grep -qF "sources/diagram.png" "$ERR"; then ok "diagram.png flagged"; else bad "diagram.png not flagged"; fi
if grep -qF "sources/report.docx" "$ERR"; then ok "report.docx flagged"; else bad "report.docx not flagged"; fi
if grep -qF "sources/note.md" "$ERR"; then bad "text note.md wrongly flagged as non-text"; else ok "text source note.md not flagged"; fi
if grep -qF ".DS_Store" "$ERR"; then bad "hidden .DS_Store wrongly flagged"; else ok "hidden .DS_Store ignored"; fi

# ---------------------------------------------------------------------------
# Run 2 (--strict): presence of binary sources is a hard failure (non-zero).
# ---------------------------------------------------------------------------
strict_exit=0
"$PYTHON" "$MERGE" --wiki "$KB" --strict >/dev/null 2>&1 || strict_exit=$?
if [ "$strict_exit" -ne 0 ]; then ok "--strict exits non-zero with binary sources (exit $strict_exit)"; else bad "--strict should exit non-zero with binary sources"; fi

# ---------------------------------------------------------------------------
# Run 2.5: a binary with its NEW-naming conversion (runs/sources/<name>.<ext>.md,
# #213) is no longer flagged; an unconverted binary still is. This pins that the
# no-conversion detection follows the new naming (a false-pass would hide the
# #213 blocker where detection still looked for the old <stem>.md name).
# ---------------------------------------------------------------------------
mkdir -p "$KB/runs/sources"
printf '<!-- ingested-by-factlog | source: report.docx | converter: pandoc | date: 2026-01-01T00:00:00Z -->\n\nAcme uses Python.\n' \
  > "$KB/runs/sources/report.docx.md"   # new naming keeps the original's full name
ERR3="$(mktemp)"
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>"$ERR3" || true
if grep -qF "sources/report.docx" "$ERR3"; then bad "report.docx flagged despite its runs/sources/report.docx.md conversion"; else ok "converted binary (new naming) no longer flagged"; fi
if grep -qF "sources/diagram.png" "$ERR3"; then ok "unconverted diagram.png still flagged"; else bad "diagram.png should still be flagged"; fi

# ---------------------------------------------------------------------------
# Run 2.6: backward compat — a LEGACY-named conversion (runs/sources/<stem>.md)
# whose provenance header names this exact original still pairs (provenance-
# verified stem fallback), so an already-ingested pre-#213 KB is not re-flagged.
# ---------------------------------------------------------------------------
rm -f "$KB/runs/sources/report.docx.md"
printf '<!-- ingested-by-factlog | source: report.docx | converter: pandoc | date: 2026-01-01T00:00:00Z -->\n\nAcme uses Python.\n' \
  > "$KB/runs/sources/report.md"        # legacy stem naming
ERR4="$(mktemp)"
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>"$ERR4" || true
if grep -qF "sources/report.docx" "$ERR4"; then bad "legacy-named conversion (provenance report.docx) not recognised"; else ok "legacy-named conversion still pairs (provenance-verified fallback)"; fi
rm -f "$KB/runs/sources/report.md"

# ---------------------------------------------------------------------------
# Run 2.7: BLOCKER regression (#213) — a REAL `factlog ingest` of a .pptx must
# leave no false "no conversion" warning and let --strict exit 0. Uses the
# built-in pptx converter (no pyrewire, no external tools).
# ---------------------------------------------------------------------------
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
RKB="$(mktemp -d)/kb"
"$PYTHON" -m factlog init --target "$RKB" >/dev/null
"$PYTHON" - "$RKB/sources/deck.pptx" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w") as z:
    z.writestr("ppt/slides/slide1.xml", '<?xml version="1.0"?><p:sld><a:p><a:t>Acme uses Python.</a:t></a:p></p:sld>')
PY
"$PYTHON" -m factlog ingest --scan --target "$RKB" >/dev/null 2>&1
[ -f "$RKB/runs/sources/deck.pptx.md" ] && ok "real ingest wrote runs/sources/deck.pptx.md" || bad "real ingest did not produce the new-naming conversion"
ERR5="$(mktemp)"
strict_rc=0
FACTLOG_ROOT="$RKB" "$PYTHON" "$MERGE" --wiki "$RKB" --strict >/dev/null 2>"$ERR5" || strict_rc=$?
if grep -qE "binary source file\(s\)" "$ERR5"; then bad "#213 regression: false 'no conversion' warning after real pptx ingest"; else ok "#213: no false warning after real pptx ingest"; fi
if [ "$strict_rc" -eq 0 ]; then ok "#213: merge_candidates --strict exits 0 on a properly-ingested KB"; else bad "#213 regression: --strict exit $strict_rc on ingested KB ($(cat "$ERR5"))"; fi

# ---------------------------------------------------------------------------
# Run 3 (text-only): no warning, and --strict exits 0.
# ---------------------------------------------------------------------------
rm -rf "$KB/runs/sources"
rm -f "$KB/sources/diagram.png" "$KB/sources/report.docx" "$KB/sources/.DS_Store"
ERR2="$(mktemp)"
clean_exit=0
"$PYTHON" "$MERGE" --wiki "$KB" --strict >/dev/null 2>"$ERR2" || clean_exit=$?
if [ "$clean_exit" -eq 0 ]; then ok "text-only KB passes --strict (exit 0)"; else bad "text-only KB should pass --strict, got $clean_exit"; fi
if grep -qE "binary source file\(s\)" "$ERR2"; then bad "warning emitted for text-only KB"; else ok "no warning for text-only KB"; fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo "test_nontext_sources: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
