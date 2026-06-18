#!/usr/bin/env bash
# tests/test_ingest.sh — `factlog ingest` binary→text conversion (#4)
#
# Covers:
#   - a real .docx (minimal OOXML fixture) → runs/sources/<name>.md with a
#     provenance header and the document text (when a converter is available),
#   - idempotency: a second run is a no-op ("up to date"), --force re-converts,
#   - --scan: auto-discovers binaries under sources/ and converts them,
#   - an unsupported format (.hwp) → non-zero exit + actionable hint.
#
# Runs factlog from the working tree via PYTHONPATH (no install / no pyrewire).
#
# Usage: bash tests/test_ingest.sh
#   Returns 0 if all checks pass, 1 if any fail.

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
FACTLOG=("$PYTHON" -m factlog)

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"${FACTLOG[@]}" init --target "$KB" >/dev/null
SRC_DIR="$(mktemp -d)"

# Minimal, valid .docx (OOXML) fixture writer.
make_docx() {  # make_docx <path> <text>
  "$PYTHON" - "$1" "$2" <<'PY'
import sys, zipfile
path, text = sys.argv[1], sys.argv[2]
ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
      '<Default Extension="xml" ContentType="application/xml"/>'
      '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
      '</Types>')
rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>')
doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
       '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
       f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("[Content_Types].xml", ct)
    z.writestr("_rels/.rels", rels)
    z.writestr("word/document.xml", doc)
PY
}

DOCX="$SRC_DIR/report.docx"
make_docx "$DOCX" "Acme uses Python for the ingest test."

have_conv=no
command -v pandoc >/dev/null 2>&1 && have_conv=yes
command -v textutil >/dev/null 2>&1 && have_conv=yes

# ---------------------------------------------------------------------------
# 1. ingest an explicit .docx -> runs/sources/
# ---------------------------------------------------------------------------
if [ "$have_conv" = yes ]; then
  if "${FACTLOG[@]}" ingest "$DOCX" --target "$KB" >/dev/null 2>&1; then
    ok "ingest .docx exits 0"
  else
    bad "ingest .docx should exit 0 (a converter is present)"
  fi
  OUT="$KB/runs/sources/report.md"; [ -f "$OUT" ] || OUT="$KB/runs/sources/report.txt"
  if [ -f "$OUT" ]; then
    ok "converted file written under runs/sources/ ($(basename "$OUT"))"
    grep -qF "ingested-by-factlog" "$OUT" && ok "provenance header present" || bad "missing provenance header"
    grep -qF "source: report.docx" "$OUT" && ok "provenance records original filename" || bad "provenance missing original filename"
    grep -qF "Acme uses Python" "$OUT" && ok "document text extracted" || bad "document text not extracted"
  else
    bad "no converted file found under runs/sources/"
  fi
  # nothing was written into sources/
  if [ -e "$KB/sources/report.md" ] || [ -e "$KB/sources/report.txt" ]; then
    bad "converted file leaked into sources/ (must stay in runs/sources/)"
  else
    ok "sources/ kept free of generated files"
  fi

  # 2. idempotency: a second run is a no-op; --force re-converts
  out2="$("${FACTLOG[@]}" ingest "$DOCX" --target "$KB" 2>&1)"; rc2=$?
  if [ "$rc2" -eq 0 ] && printf '%s' "$out2" | grep -qF "up to date"; then
    ok "second ingest is an idempotent no-op (up to date)"
  else
    bad "second ingest should be a no-op 'up to date' (rc=$rc2)"
  fi
  if "${FACTLOG[@]}" ingest "$DOCX" --target "$KB" --force >/dev/null 2>&1; then
    ok "ingest --force re-converts (exit 0)"
  else
    bad "ingest --force should succeed"
  fi

  # 3. --scan: a binary copied into sources/ is auto-converted
  cp "$DOCX" "$KB/sources/scanme.docx"
  if "${FACTLOG[@]}" ingest --scan --target "$KB" >/dev/null 2>&1; then
    ok "ingest --scan exits 0"
  else
    bad "ingest --scan should exit 0"
  fi
  [ -f "$KB/runs/sources/scanme.md" ] || [ -f "$KB/runs/sources/scanme.txt" ] \
    && ok "--scan converted sources/scanme.docx into runs/sources/" \
    || bad "--scan did not convert the binary under sources/"
else
  echo "SKIP: no docx converter (pandoc/textutil) — skipping conversion assertions"
fi

# ---------------------------------------------------------------------------
# 4. unsupported format (.hwp) named explicitly → non-zero + hint
# ---------------------------------------------------------------------------
touch "$SRC_DIR/notes.hwp"
err="$(mktemp)"
if "${FACTLOG[@]}" ingest "$SRC_DIR/notes.hwp" --target "$KB" >/dev/null 2>"$err"; then
  bad "ingest explicit .hwp should exit non-zero"
else
  ok "ingest explicit unsupported .hwp exits non-zero"
fi
grep -qiE "hwp|no common converter|no converter" "$err" && ok ".hwp prints an actionable hint" || bad ".hwp hint missing"

# ---------------------------------------------------------------------------
# 5. subdirectory structure is preserved (stdlib .pptx converter; no external dep)
# ---------------------------------------------------------------------------
make_pptx() {  # make_pptx <path> <text>
  "$PYTHON" - "$1" "$2" <<'PY'
import sys, zipfile
path, text = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(path, "w") as z:
    z.writestr("[Content_Types].xml", "<Types/>")
    z.writestr("ppt/slides/slide1.xml",
               f'<?xml version="1.0"?><p:sld><a:p><a:t>{text}</a:t></a:p></p:sld>')
PY
}

SUBKB="$(mktemp -d)/wiki"
"${FACTLOG[@]}" init --target "$SUBKB" >/dev/null
mkdir -p "$SUBKB/sources/a" "$SUBKB/sources/b"
make_pptx "$SUBKB/sources/a/report.pptx" "alpha slide"
make_pptx "$SUBKB/sources/b/report.pptx" "beta slide"   # same stem, different subdir
"${FACTLOG[@]}" ingest --scan --target "$SUBKB" >/dev/null 2>&1
[ -f "$SUBKB/runs/sources/a/report.md" ] && [ -f "$SUBKB/runs/sources/b/report.md" ] \
  && ok "ingest mirrors the source subdirectory under runs/sources/" \
  || bad "subdirectory not mirrored in conversion path"
[ -f "$SUBKB/runs/sources/report.md" ] \
  && bad "a flat conversion leaked (subdir not preserved)" \
  || ok "no flat collision: same-stem files in different subdirs stay separate"
grep -qx "alpha slide" "$SUBKB/runs/sources/a/report.md" && grep -qx "beta slide" "$SUBKB/runs/sources/b/report.md" \
  && ok "each nested conversion holds its own source's text" || bad "nested conversion content wrong"

# an explicitly-named path OUTSIDE sources/ has no subtree to mirror → flat name
make_pptx "$SRC_DIR/external.pptx" "external slide"
"${FACTLOG[@]}" ingest "$SRC_DIR/external.pptx" --target "$SUBKB" >/dev/null 2>&1
[ -f "$SUBKB/runs/sources/external.md" ] \
  && ok "explicit path outside sources/ converts to a flat runs/sources/ name" \
  || bad "external explicit path not converted flat"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo "test_ingest: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
