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

echo
if [ "$fails" -eq 0 ]; then echo "export nested: all passed"; else echo "export nested: $fails failed"; exit 1; fi
