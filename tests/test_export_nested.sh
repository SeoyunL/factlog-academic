#!/usr/bin/env bash
# `export` must see every source `factlog sources` sees (#223). It globbed only the top
# level, so a source in a subdirectory vanished from the citation list with exit 0 and
# no warning -- a bibliography quietly losing a work.
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${FACTLOG_PY:-${PYTHON:-python3}}"
export PYTHONPATH="$PWD"
fails=0
check() { if printf '%s' "$2" | grep -qF "$3"; then echo "  ok: $1"; else echo "FAIL: $1 (missing: $3)"; fails=$((fails+1)); fi; }
ok_j() { echo "  ok: $1"; }
bad_j() { echo "FAIL: $1"; fails=$((fails+1)); }
absent() { if printf '%s' "$2" | grep -qF "$3"; then echo "FAIL: $1 (unexpected: $3)"; fails=$((fails+1)); else echo "  ok: $1"; fi; }

KB="$(mktemp -d)/kb"
export XDG_CONFIG_HOME="$(mktemp -d)"
"$PY" -m factlog init --target "$KB" >/dev/null || { echo "FAIL: init"; exit 1; }
mkdir -p "$KB/sources/lit" "$KB/sources/other"
printf -- '---\ntitle: Top\nauthor: A\nyear: 2020\n---\ntop\n' > "$KB/sources/top.md"
printf -- '---\ntitle: Nested\nauthor: B\nyear: 2021\n---\nnested\n' > "$KB/sources/lit/nested.md"
printf -- '---\ntitle: Same stem\nauthor: C\n---\nx\n' > "$KB/sources/other/nested.md"
printf 'no front matter\n' > "$KB/sources/plain.md"

OUT="$(FACTLOG_ROOT="$KB" "$PY" -m factlog export --bibtex 2>&1)"
check "(a) a nested source is exported" "$OUT" "@misc{nested,"
check "(b) the top-level source is still exported" "$OUT" "@misc{top,"
check "(c) a colliding stem gets its own key, not a silent overwrite" "$OUT" "@misc{nested-2,"
check "(c) the collision is reported" "$OUT" "citation key 'nested' is used by"
check "(d) an uncitable source is named, not dropped silently" "$OUT" "skipped sources/plain.md"

# every citable source `factlog sources` lists must appear
N_SRC="$(FACTLOG_ROOT="$KB" "$PY" -m factlog sources 2>&1 | grep -cE '^  \[')"
N_BIB="$(printf '%s' "$OUT" | grep -c '^@misc{')"
if [ "$N_BIB" -eq 3 ] && [ "$N_SRC" -eq 4 ]; then
  echo "  ok: (e) 3 of the 4 sources are citable and all 3 are exported"
else
  echo "FAIL: (e) sources=$N_SRC exported=$N_BIB (want 4 / 3)"; fails=$((fails+1))
fi

# stdout ONLY: the skip notice goes to stderr, and folding them together would make
# this assertion read the warning as bibliography content.
OUT_CSL="$(FACTLOG_ROOT="$KB" "$PY" -m factlog export --csl 2>/dev/null)"
check "(f) --csl sees the nested source too" "$OUT_CSL" '"Nested"'
absent "(g) the uncitable source is not in the CSL output" "$OUT_CSL" "plain"

# --- collision on the EMITTED key, not the stem -------------------------------------
# BibTeX sanitizes the key: safe_cite_key collapses non-ASCII to "ref", so two sources
# with different stems (한글.md, 다른이름.md) both emit `@misc{ref,` and one silently
# wins in every processor -- the exact silent loss this fix claims to prevent. `a.b`
# and `a-b` collide the same way. Dedup must run on the emitted key.
KB3="$(mktemp -d)/kb"
"$PY" -m factlog init --target "$KB3" >/dev/null
printf -- '---\ntitle: 첫번째\n---\nx\n' > "$KB3/sources/한글.md"
printf -- '---\ntitle: 두번째\n---\ny\n' > "$KB3/sources/다른이름.md"
printf -- '---\ntitle: dot\n---\nz\n' > "$KB3/sources/a.b.md"
printf -- '---\ntitle: dash\n---\nw\n' > "$KB3/sources/a-b.md"

OUT3="$(FACTLOG_ROOT="$KB3" "$PY" -m factlog export --bibtex 2>/dev/null)"
DUP="$(printf '%s' "$OUT3" | grep -oE '@misc\{[^,]+' | sort | uniq -d)"
if [ -z "$DUP" ]; then echo "  ok: (h) BibTeX keys are unique across non-ASCII and punctuation-only stems"; else echo "FAIL: (h) duplicate BibTeX key: $DUP"; fails=$((fails+1)); fi
N_ENTRIES="$(printf '%s' "$OUT3" | grep -c '^@misc{')"
[ "$N_ENTRIES" -eq 4 ] && echo "  ok: (h) all four sources are exported" || { echo "FAIL: (h) exported $N_ENTRIES of 4"; fails=$((fails+1)); }

ERR3="$(FACTLOG_ROOT="$KB3" "$PY" -m factlog export --bibtex 2>&1 >/dev/null)"
printf '%s' "$ERR3" | grep -q "citation key 'ref' is used by" && echo "  ok: (h) the non-ASCII collision is reported, not silent" || { echo "FAIL: (h) the non-ASCII collision was silent"; fails=$((fails+1)); }

# CSL keeps the stem as id, so non-ASCII ids stay distinct with no collision at all.
CSL3="$(FACTLOG_ROOT="$KB3" "$PY" -m factlog export --csl 2>/dev/null)"
{ printf '%s' "$CSL3" | grep -q '한글' && printf '%s' "$CSL3" | grep -q '다른이름'; } && echo "  ok: (i) CSL keeps distinct non-ASCII ids" || { echo "FAIL: (i) CSL lost a non-ASCII id"; fails=$((fails+1)); }

# --- runs/sources/ is a source root too --------------------------------------------
# `factlog sources` lists both sources/ and runs/sources/. export walked only sources/,
# so a citable .md placed under runs/sources/ was dropped from the bibliography with
# exit 0 and no warning -- the same silent loss #223 is about, in the second root.
KB4="$(mktemp -d)/kb"
"$PY" -m factlog init --target "$KB4" >/dev/null
printf -- '---\ntitle: Top\n---\nx\n' > "$KB4/sources/top.md"
mkdir -p "$KB4/runs/sources"
printf -- '---\ntitle: Hand placed in runs\nauthor: R\n---\ny\n' > "$KB4/runs/sources/hand.md"
# a real ingest conversion carries an HTML comment header, not YAML front matter
printf -- '<!-- ingested-by-factlog | source: doc.html | converter: pandoc -->\nbody\n' \
  > "$KB4/runs/sources/doc.html.md"

OUT4="$(FACTLOG_ROOT="$KB4" "$PY" -m factlog export --bibtex 2>/dev/null)"
printf '%s' "$OUT4" | grep -q "Hand placed in runs" \
  && ok_j "(j) a citable .md under runs/sources/ is exported, not dropped" \
  || bad_j "(j) a citable runs/sources/ source was dropped silently"

ERR4="$(FACTLOG_ROOT="$KB4" "$PY" -m factlog export --bibtex 2>&1 >/dev/null)"
printf '%s' "$ERR4" | grep -q "skipped runs/sources/doc.html.md" \
  && ok_j "(j) an ingest conversion (no front matter) is reported skipped, not dropped" \
  || bad_j "(j) the ingest conversion was dropped without a word"

echo
if [ "$fails" -eq 0 ]; then echo "export nested: all passed"; else echo "export nested: $fails failed"; exit 1; fi
