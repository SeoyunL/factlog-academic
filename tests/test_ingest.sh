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
  # #213: the conversion keeps the original's full name (extension included) so
  # same-stem/different-extension originals never collide on one output.
  OUT="$KB/runs/sources/report.docx.md"; [ -f "$OUT" ] || OUT="$KB/runs/sources/report.docx.txt"
  if [ -f "$OUT" ]; then
    ok "converted file written under runs/sources/ ($(basename "$OUT"))"
    grep -qF "ingested-by-factlog" "$OUT" && ok "provenance header present" || bad "missing provenance header"
    grep -qF "source: report.docx" "$OUT" && ok "provenance records original filename" || bad "provenance missing original filename"
    grep -qF "Acme uses Python" "$OUT" && ok "document text extracted" || bad "document text not extracted"
  else
    bad "no converted file found under runs/sources/ (expected report.docx.md/.txt)"
  fi
  # nothing was written into sources/
  if [ -e "$KB/sources/report.docx.md" ] || [ -e "$KB/sources/report.docx.txt" ]; then
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
  [ -f "$KB/runs/sources/scanme.docx.md" ] || [ -f "$KB/runs/sources/scanme.docx.txt" ] \
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
[ -f "$SUBKB/runs/sources/a/report.pptx.md" ] && [ -f "$SUBKB/runs/sources/b/report.pptx.md" ] \
  && ok "ingest mirrors the source subdirectory under runs/sources/" \
  || bad "subdirectory not mirrored in conversion path"
[ -f "$SUBKB/runs/sources/report.pptx.md" ] \
  && bad "a flat conversion leaked (subdir not preserved)" \
  || ok "no flat collision: same-stem files in different subdirs stay separate"
grep -qx "alpha slide" "$SUBKB/runs/sources/a/report.pptx.md" && grep -qx "beta slide" "$SUBKB/runs/sources/b/report.pptx.md" \
  && ok "each nested conversion holds its own source's text" || bad "nested conversion content wrong"

# an explicitly-named path OUTSIDE sources/ has no subtree to mirror → flat name
make_pptx "$SRC_DIR/external.pptx" "external slide"
"${FACTLOG[@]}" ingest "$SRC_DIR/external.pptx" --target "$SUBKB" >/dev/null 2>&1
[ -f "$SUBKB/runs/sources/external.pptx.md" ] \
  && ok "explicit path outside sources/ converts to a flat runs/sources/ name" \
  || bad "external explicit path not converted flat"

# ---------------------------------------------------------------------------
# 6. #213: same stem, different extension in ONE folder → distinct conversions
#    (report.hwpx + report.pptx must NOT collide on a single runs/sources/report.md
#    and silently drop the loser). Both built-in converters, no external deps.
# ---------------------------------------------------------------------------
make_hwpx() {  # make_hwpx <path> <text>
  "$PYTHON" - "$1" "$2" <<'PY'
import sys, zipfile
path, text = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(path, "w") as z:
    z.writestr("Contents/section0.xml",
               f'<?xml version="1.0"?><hp:sec><hp:p><hp:t>{text}</hp:t></hp:p></hp:sec>')
PY
}

STEMKB="$(mktemp -d)/wiki"
"${FACTLOG[@]}" init --target "$STEMKB" >/dev/null
make_hwpx "$STEMKB/sources/report.hwpx" "라팀은 1위를 기록했다"
make_pptx "$STEMKB/sources/report.pptx" "라팀은 2위를 기록했다"
"${FACTLOG[@]}" ingest --scan --target "$STEMKB" >/dev/null 2>&1
HWPX_OUT="$STEMKB/runs/sources/report.hwpx.md"
PPTX_OUT="$STEMKB/runs/sources/report.pptx.md"
if [ -f "$HWPX_OUT" ] && [ -f "$PPTX_OUT" ]; then
  ok "#213 same-stem originals produce two distinct conversions (no collision)"
else
  bad "#213 stem collision: missing $(basename "$HWPX_OUT") or $(basename "$PPTX_OUT")"
fi
[ ! -f "$STEMKB/runs/sources/report.md" ] \
  && ok "#213 no legacy flat report.md that would swallow one of the two" \
  || bad "#213 flat report.md still produced (collision not resolved)"
grep -qF "1위" "$HWPX_OUT" 2>/dev/null && ok "#213 hwpx conversion holds its own text (1위)" || bad "#213 hwpx content missing/wrong"
grep -qF "2위" "$PPTX_OUT" 2>/dev/null && ok "#213 pptx conversion holds its own text (2위)" || bad "#213 pptx content missing/wrong (would be the lost source)"
grep -qF "source: report.hwpx" "$HWPX_OUT" 2>/dev/null && ok "#213 hwpx provenance points at report.hwpx" || bad "#213 hwpx provenance wrong"
grep -qF "source: report.pptx" "$PPTX_OUT" 2>/dev/null && ok "#213 pptx provenance points at report.pptx" || bad "#213 pptx provenance wrong"

# re-run is idempotent per-original: each compares only against its own
# conversion, no mutual skip, both stay up to date
rerun="$("${FACTLOG[@]}" ingest --scan --target "$STEMKB" 2>&1)"
n_skipped="$(printf '%s' "$rerun" | grep -c "up to date" || true)"
[ "$n_skipped" -ge 2 ] \
  && ok "#213 re-run skips each conversion on its own freshness (no mutual skip)" \
  || bad "#213 re-run did not idempotently skip both conversions (got: $rerun)"

# `factlog sources` pairs each original with ITS OWN conversion (no mispair)
srcout="$("${FACTLOG[@]}" sources --target "$STEMKB" 2>&1)"
printf '%s' "$srcout" | grep -qF "sources/report.hwpx" && printf '%s' "$srcout" | grep -qF "runs/sources/report.hwpx.md" \
  && ok "#213 sources lists report.hwpx → its own conversion" || bad "#213 sources mispaired report.hwpx"
printf '%s' "$srcout" | grep -qF "sources/report.pptx" && printf '%s' "$srcout" | grep -qF "runs/sources/report.pptx.md" \
  && ok "#213 sources lists report.pptx → its own conversion" || bad "#213 sources mispaired report.pptx"
# a bare "runs/sources/report.md" is not a substring of report.hwpx.md/report.pptx.md,
# so it appears only if the old collision produced one.
printf '%s' "$srcout" | grep -qF "runs/sources/report.md" \
  && bad "#213 sources references a bare report.md (collision not resolved)" \
  || ok "#213 sources never references a bare report.md"

# ---------------------------------------------------------------------------
# 7. #214: the provenance `source:` records the path *relative to sources/* so
#    same-name originals in different subdirs get distinct provenance, and
#    eject --orphans still pairs each conversion with its own original (no false
#    delete, no mispair) — for both the new header and a legacy basename header.
# ---------------------------------------------------------------------------
PROVKB="$(mktemp -d)/wiki"
"${FACTLOG[@]}" init --target "$PROVKB" >/dev/null
mkdir -p "$PROVKB/sources/sub_a" "$PROVKB/sources/sub_b"
make_hwpx "$PROVKB/sources/sub_a/data.hwpx" "alpha body"
make_hwpx "$PROVKB/sources/sub_b/data.hwpx" "beta body"
make_hwpx "$PROVKB/sources/top.hwpx" "root body"
"${FACTLOG[@]}" ingest --scan --target "$PROVKB" >/dev/null 2>&1
A_HDR="$(head -1 "$PROVKB/runs/sources/sub_a/data.hwpx.md" 2>/dev/null || true)"
B_HDR="$(head -1 "$PROVKB/runs/sources/sub_b/data.hwpx.md" 2>/dev/null || true)"
printf '%s' "$A_HDR" | grep -qF "source: sub_a/data.hwpx" \
  && ok "#214 sub_a conversion records sources-relative provenance (sub_a/data.hwpx)" \
  || bad "#214 sub_a provenance not sources-relative: $A_HDR"
printf '%s' "$B_HDR" | grep -qF "source: sub_b/data.hwpx" \
  && ok "#214 sub_b conversion records sources-relative provenance (sub_b/data.hwpx)" \
  || bad "#214 sub_b provenance not sources-relative: $B_HDR"
[ "$A_HDR" != "$B_HDR" ] \
  && ok "#214 same-name originals in different subdirs get distinct provenance" \
  || bad "#214 same-name subdir originals still collide on identical provenance"
# root-direct original is unchanged (bare basename, no regression)
grep -qF "source: top.hwpx" "$PROVKB/runs/sources/top.hwpx.md" \
  && ok "#214 root-direct original keeps a bare-basename provenance (no regression)" \
  || bad "#214 root-direct provenance changed (regression)"
# eject --orphans after removing ONLY sub_a's original: sub_a's conversion is an
# orphan, sub_b's (its original still present) is kept — no false delete/mispair.
rm "$PROVKB/sources/sub_a/data.hwpx"
ejout="$("${FACTLOG[@]}" eject --orphans --target "$PROVKB" 2>&1)"; ejrc=$?
printf '%s' "$ejout" | grep -qF "runs/sources/sub_a/data.hwpx.md" \
  && ok "#214 eject --orphans flags sub_a's conversion (its original is gone)" \
  || bad "#214 eject --orphans missed the real orphan (sub_a)"
printf '%s' "$ejout" | grep -qF "runs/sources/sub_b/data.hwpx.md" \
  && bad "#214 eject --orphans FALSE-deleted sub_b (original still present)" \
  || ok "#214 eject --orphans keeps sub_b's conversion (no false delete/mispair)"
[ -f "$PROVKB/runs/sources/sub_b/data.hwpx.md" ] \
  && ok "#214 sub_b conversion survives on disk" \
  || bad "#214 sub_b conversion wrongly deleted from disk"

# legacy basename header (pre-#214): same-name originals whose headers BOTH say
# `source: data.hwpx` must still pair by mirrored subdir — no false orphan delete.
LEGKB="$(mktemp -d)/wiki"
"${FACTLOG[@]}" init --target "$LEGKB" >/dev/null
mkdir -p "$LEGKB/sources/sub_a" "$LEGKB/sources/sub_b" \
         "$LEGKB/runs/sources/sub_a" "$LEGKB/runs/sources/sub_b"
touch "$LEGKB/sources/sub_a/data.hwpx" "$LEGKB/sources/sub_b/data.hwpx"
printf '<!-- ingested-by-factlog | source: data.hwpx | converter: x | date: y -->\n\nalpha\n' \
  > "$LEGKB/runs/sources/sub_a/data.hwpx.md"
printf '<!-- ingested-by-factlog | source: data.hwpx | converter: x | date: y -->\n\nbeta\n' \
  > "$LEGKB/runs/sources/sub_b/data.hwpx.md"
rm "$LEGKB/sources/sub_a/data.hwpx"
legout="$("${FACTLOG[@]}" eject --orphans --target "$LEGKB" 2>&1)"
if printf '%s' "$legout" | grep -qF "runs/sources/sub_a/data.hwpx.md" \
   && ! printf '%s' "$legout" | grep -qF "runs/sources/sub_b/data.hwpx.md"; then
  ok "#214 legacy basename headers still pair by subdir (no false orphan on sub_b)"
else
  bad "#214 legacy basename header pairing regressed: $legout"
fi

# ---------------------------------------------------------------------------
# 8. #215: --scan surfaces a recognized binary-extension file whose CONTENT is
#    plain text (or 0 bytes) instead of silently dropping it. A genuine plain
#    text source (no converter extension) stays unflagged.
# ---------------------------------------------------------------------------
SCANKB="$(mktemp -d)/wiki"
"${FACTLOG[@]}" init --target "$SCANKB" >/dev/null
printf 'plain text pretending to be hwpx\n' > "$SCANKB/sources/05_fake_ext.hwpx"
: > "$SCANKB/sources/06_zero_bytes.hwpx"
printf 'legit plain notes\n' > "$SCANKB/sources/notes.txt"
scanout="$("${FACTLOG[@]}" ingest --scan --target "$SCANKB" 2>&1)"
printf '%s' "$scanout" | grep -qF "05_fake_ext.hwpx" \
  && ok "#215 --scan surfaces the plaintext-content binary-ext file (05)" \
  || bad "#215 --scan silently dropped 05_fake_ext.hwpx"
printf '%s' "$scanout" | grep -qiE "non-binary content" \
  && ok "#215 --scan labels it 'binary extension, non-binary content'" \
  || bad "#215 --scan missing non-binary-content warning"
printf '%s' "$scanout" | grep -qF "06_zero_bytes.hwpx" \
  && ok "#215 --scan surfaces the 0-byte binary-ext file (06)" \
  || bad "#215 --scan silently dropped 06_zero_bytes.hwpx"
printf '%s' "$scanout" | grep -qiE "empty file|0 bytes" \
  && ok "#215 --scan labels the 0-byte file distinctly" \
  || bad "#215 --scan missing 0-byte label"
printf '%s' "$scanout" | grep -qF "notes.txt" \
  && bad "#215 --scan wrongly flagged a legit plain .txt source" \
  || ok "#215 --scan leaves a legit plain-text source unflagged (no false positive)"

# 8b. #215 (first acceptance criterion): the --scan summary arithmetic must
#     balance — converted + skipped + failed + ignored == discovered conversion
#     targets. Sum the counts from the summary line(s) and compare to the number
#     of recognized-extension originals. Covers BOTH a mixed KB (a real summary
#     line) and an all-ignored KB (the early-return "no ... to convert" note),
#     so neither path can silently lose a file's contribution to the total.
#
# extract_ignored: total files reported "ignored" across both ignore classes.
extract_ignored() {  # extract_ignored <output>  -> echoes the summed ignored count
  local total=0 n
  while read -r n; do
    [ -n "$n" ] && total=$((total + n))
  done < <(printf '%s' "$1" \
    | grep -oE "[0-9]+ ignored \((binary extension, non-binary content|empty file)\)" \
    | grep -oE "^[0-9]+")
  echo "$total"
}

# Mixed KB: 1 real binary + 1 non-binary (.hwpx-ext plaintext) + 1 zero-byte.
MIXKB="$(mktemp -d)/wiki"
"${FACTLOG[@]}" init --target "$MIXKB" >/dev/null
make_hwpx "$MIXKB/sources/real.hwpx" "real body"
printf 'plain text pretending to be hwpx\n' > "$MIXKB/sources/fake.hwpx"
: > "$MIXKB/sources/zero.hwpx"
mixout="$("${FACTLOG[@]}" ingest --scan --target "$MIXKB" 2>&1)"
mixline="$(printf '%s' "$mixout" | grep -E "^factlog ingest: [0-9]+ converted")"
m_conv="$(printf '%s' "$mixline"  | grep -oE "[0-9]+ converted"  | grep -oE "^[0-9]+")"
m_skip="$(printf '%s' "$mixline"  | grep -oE "[0-9]+ skipped"    | grep -oE "^[0-9]+")"
m_fail="$(printf '%s' "$mixline"  | grep -oE "[0-9]+ failed"     | grep -oE "^[0-9]+")"
m_ign="$(extract_ignored "$mixline")"
m_sum=$(( m_conv + m_skip + m_fail + m_ign ))
[ "$m_conv" = "1" ] && ok "#215 mixed KB: the real binary is counted as converted" \
  || bad "#215 mixed KB: real binary not converted (got '$mixline')"
[ "$m_ign" = "2" ] && ok "#215 mixed KB: fake + zero counted as ignored (2)" \
  || bad "#215 mixed KB: ignored count wrong (got $m_ign; line: '$mixline')"
[ "$m_sum" = "3" ] \
  && ok "#215 mixed KB: summary arithmetic balances (converted+skipped+failed+ignored == 3 discovered)" \
  || bad "#215 mixed KB: summary arithmetic off (sum=$m_sum, expected 3; line: '$mixline')"

# All-ignored KB: no convertible binary → the early-return note must still carry
# the ignored counts (INFO 1) so the arithmetic (0 converted + 2 ignored == 2)
# is visible, not just per-file warnings.
ALLKB="$(mktemp -d)/wiki"
"${FACTLOG[@]}" init --target "$ALLKB" >/dev/null
printf 'plain\n' > "$ALLKB/sources/fake.hwpx"
: > "$ALLKB/sources/zero.hwpx"
allout="$("${FACTLOG[@]}" ingest --scan --target "$ALLKB" 2>&1)"
allnote="$(printf '%s' "$allout" | grep -E "no binary source files to convert")"
printf '%s' "$allnote" | grep -qE "ignored" \
  && ok "#215 all-ignored KB: the no-convert summary line carries the ignored counts (INFO 1)" \
  || bad "#215 all-ignored KB: no-convert line lost the ignored counts (got '$allnote')"
a_ign="$(extract_ignored "$allnote")"
[ "$a_ign" = "2" ] \
  && ok "#215 all-ignored KB: summary arithmetic balances (0 converted + 2 ignored == 2 discovered)" \
  || bad "#215 all-ignored KB: ignored count wrong (got $a_ign; line: '$allnote')"

# ---------------------------------------------------------------------------
# 9. #229: a conversion whose body is only the provenance header (a scanned/
#    image PDF -> pdftotext exits 0 with empty text) is counted as
#    converted-but-empty and distinguished by `sources`/`status`, while a normal
#    text PDF is unaffected. Guarded on pdftotext because that is the converter
#    THIS case needs — not because it is the only one that can reach the state.
#    It is not, and the claim that it was is what #617 removed. Measured at
#    bb3909c on a fresh KB: an empty `sources/x.html` and a minimal empty RTF
#    (`{\rtf1\ansi}`) each go through pandoc, which exits 0 and writes a
#    non-empty file, and `factlog ingest --scan` reports both as
#    `converted-but-empty (likely scanned/needs OCR)`. What survives of the old
#    parenthetical is narrower and still true: the BUILT-IN converters treat an
#    empty extraction as failure (_conv_hwpx/_conv_hwp/_conv_pptx return False),
#    and factlog/ingest.py:167 counts a zero-byte output as failure for every
#    converter.
# ---------------------------------------------------------------------------
if command -v pdftotext >/dev/null 2>&1; then
  EMPTYKB="$(mktemp -d)/wiki"
  "${FACTLOG[@]}" init --target "$EMPTYKB" >/dev/null
  # scanned/image PDF: valid PDF, one page, NO text operators -> empty text out.
  # A binary marker comment keeps _looks_binary True so --scan converts it.
  "$PYTHON" - "$EMPTYKB/sources/03_scanned.pdf" <<'PY'
import sys
pdf = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + (
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Size 4/Root 1 0 R>>\n%%EOF"
)
open(sys.argv[1], "wb").write(pdf)
PY
  # a normal text PDF (has a text-showing content stream) -> non-empty output.
  "$PYTHON" - "$EMPTYKB/sources/02_text.pdf" <<'PY'
import sys
content = b"BT /F1 24 Tf 72 700 Td (Hello factlog world) Tj ET"
objs = [
    b"<</Type/Catalog/Pages 2 0 R>>",
    b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
    b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources<</Font<</F1 5 0 R>>>>/Contents 4 0 R>>",
    b"<</Length %d>>\nstream\n%s\nendstream" % (len(content), content),
    b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
]
out = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"; offs = []
for i, o in enumerate(objs, 1):
    offs.append(len(out)); out += b"%d 0 obj" % i + o + b"endobj\n"
xref = len(out); out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
for off in offs: out += b"%010d 00000 n \n" % off
out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)
open(sys.argv[1], "wb").write(out)
PY
  emptyout="$("${FACTLOG[@]}" ingest --scan --target "$EMPTYKB" 2>&1)"
  printf '%s' "$emptyout" | grep -qiE "converted-but-empty" \
    && ok "#229 empty conversion (scanned PDF) counted as converted-but-empty" \
    || bad "#229 empty conversion not flagged: $emptyout"
  printf '%s' "$emptyout" | grep -qF "03_scanned.pdf" \
    && ok "#229 the empty conversion names the scanned source" \
    || bad "#229 empty-conversion warning missing the source name"
  # normal text PDF must NOT be mis-flagged as empty (no regression)
  if printf '%s' "$emptyout" | grep "02_text.pdf" | grep -qiE "converted-but-empty"; then
    bad "#229 a normal text PDF was wrongly flagged converted-but-empty (regression)"
  else
    ok "#229 a normal text PDF is a plain success (no regression)"
  fi
  # `sources` distinguishes the empty conversion from a not-yet-synced source
  esrc="$("${FACTLOG[@]}" sources --target "$EMPTYKB" 2>&1)"
  printf '%s' "$esrc" | grep "03_scanned.pdf" | grep -qiE "converted-but-empty" \
    && ok "#229 sources marks the scanned PDF converted-but-empty" \
    || bad "#229 sources did not distinguish the empty conversion"
  printf '%s' "$esrc" | grep "02_text.pdf" | grep -qiE "converted-but-empty" \
    && bad "#229 sources wrongly marked the text PDF converted-but-empty" \
    || ok "#229 sources leaves the text PDF unmarked (no regression)"
  # `status` counts converted-but-empty separately
  estat="$("${FACTLOG[@]}" status --target "$EMPTYKB" 2>&1)"
  printf '%s' "$estat" | grep -i "sources:" | grep -qiE "converted-but-empty" \
    && ok "#229 status counts converted-but-empty on the sources line" \
    || bad "#229 status did not surface converted-but-empty"
else
  echo "SKIP: no pdftotext — skipping #229 empty-conversion assertions"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo "test_ingest: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
