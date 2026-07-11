#!/usr/bin/env bash
# `init` must scaffold policy/single-valued.md (#224). Without it, contradiction
# detection -- the feature that distinguishes this from a notes wiki -- was
# undiscoverable: `status` pointed users at a file that did not exist and no README
# said what goes in it.
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${FACTLOG_PY:-${PYTHON:-python3}}"
export PYTHONPATH="$PWD"
fails=0
ok() { echo "  ok: $1"; }
bad() { echo "FAIL: $1"; fails=$((fails+1)); }

KB="$(mktemp -d)/kb"
export XDG_CONFIG_HOME="$(mktemp -d)"
"$PY" -m factlog init --target "$KB" >/dev/null || { echo "FAIL: init"; exit 1; }

[ -f "$KB/policy/single-valued.md" ] && ok "(a) init scaffolds policy/single-valued.md" \
  || { bad "(a) single-valued.md is not scaffolded"; exit 1; }

grep -q "eject --fact" "$KB/policy/single-valued.md" \
  && ok "(b) it names the human-gate command, not a candidates.csv hand-edit" \
  || bad "(b) no gate command in the scaffold"
grep -q "value-hierarchy.md" "$KB/policy/single-valued.md" \
  && ok "(b) it points at the hierarchy when the values are a supertype/subtype" \
  || bad "(b) no hierarchy pointer"

# The scaffolded EXAMPLE must actually work. Shipping a non-parsing example is how the
# ordinal `rank 3` bug (#227) reached users.
printf 'a\n' > "$KB/sources/a.md"
{ printf 'subject,relation,object,source,status,confidence,note\n'
  printf 'P,published_year,2020,sources/a.md,accepted,0.9,\n'
  printf 'P,published_year,2021,sources/a.md,accepted,0.9,\n'; } > "$KB/facts/candidates.csv"
# uncomment the example exactly as the file tells the user to
sed -e 's/^# published_year$/published_year/' "$KB/policy/single-valued.md" > "$KB/policy/sv.tmp"
mv "$KB/policy/sv.tmp" "$KB/policy/single-valued.md"
FACTLOG_ROOT="$KB" "$PY" tools/compile_facts.py >/dev/null 2>&1

OUT="$(FACTLOG_ROOT="$KB" "$PY" tools/check_conflicts.py 2>&1)"
printf '%s' "$OUT" | grep -q "CONFLICT: single-valued 'published_year'" \
  && ok "(c) uncommenting the scaffolded example actually detects the conflict" \
  || bad "(c) the scaffolded example does not work"

FACTLOG_ROOT="$KB" "$PY" -m factlog status 2>&1 | grep -q "conflicts:  1" \
  && ok "(d) status agrees" || bad "(d) status disagrees with the gate"

# README must say what goes in the file -- status points users at it.
grep -q "policy/single-valued.md" README.md && ok "(e) README documents it" || bad "(e) README does not"
grep -q "policy/single-valued.md" README.ko.md && ok "(e) README.ko documents it" || bad "(e) README.ko does not"

echo
if [ "$fails" -eq 0 ]; then echo "single-valued scaffold: all passed"; else echo "single-valued scaffold: $fails failed"; exit 1; fi
