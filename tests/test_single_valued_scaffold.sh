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

# Every `factlog <sub>` the scaffold names must EXIST. Grepping for a string proves the
# text is there, not that the advice works -- and this file shipped `factlog check`,
# which is a Claude Code slash command, not a CLI subcommand. Pointing users at
# something that does not exist is the very complaint #224 was filed about.
for SUB in $(grep -oE '(^|[^/])factlog [a-z-]+' "$KB/policy/single-valued.md" \
             | sed -E 's/.*factlog //' | sort -u); do
  if "$PY" -m factlog "$SUB" --help >/dev/null 2>&1; then
    ok "(b) 'factlog $SUB' is a real command"
  else
    bad "(b) the scaffold names 'factlog $SUB', which does not exist"
  fi
done
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

ST="$(FACTLOG_ROOT="$KB" "$PY" -m factlog status 2>&1 || true)"
printf '%s' "$ST" | grep -q "conflicts:  1" \
  && ok "(d) status agrees" || bad "(d) status disagrees with the gate"

# The SECOND scaffolded example is backtick-quoted and contains a space. Ship an
# example, prove it parses.
KB2="$(mktemp -d)/kb"
"$PY" -m factlog init --target "$KB2" >/dev/null
printf 'a\n' > "$KB2/sources/a.md"
{ printf 'subject,relation,object,source,status,confidence,note\n'
  printf 'P,연구 유형,관찰연구,sources/a.md,accepted,0.9,\n'
  printf 'P,연구 유형,실험연구,sources/a.md,accepted,0.9,\n'; } > "$KB2/facts/candidates.csv"
sed -e 's/^# `연구 유형`$/`연구 유형`/' "$KB2/policy/single-valued.md" > "$KB2/policy/sv.tmp"
mv "$KB2/policy/sv.tmp" "$KB2/policy/single-valued.md"
FACTLOG_ROOT="$KB2" "$PY" tools/compile_facts.py >/dev/null 2>&1
# Capture, do not pipe: check_conflicts exits 1 when it finds a conflict, and under
# `set -o pipefail` that fails the whole pipeline even when grep matched.
OUT2="$(FACTLOG_ROOT="$KB2" "$PY" tools/check_conflicts.py 2>&1 || true)"
printf '%s' "$OUT2" | grep -q "single-valued '연구 유형'" \
  && ok "(f) the backtick-quoted example with a space parses too" \
  || bad "(f) the backtick-quoted example does not work"

# Re-running init must NOT clobber a user's declarations.
printf 'my_relation\n' > "$KB2/policy/single-valued.md"
"$PY" -m factlog init --target "$KB2" >/dev/null 2>&1
[ "$(cat "$KB2/policy/single-valued.md")" = "my_relation" ] \
  && ok "(g) re-running init does not overwrite an existing single-valued.md" \
  || bad "(g) init CLOBBERED the user's declarations"

# The docs must say what goes in the file -- status points users at it.
# The prose moved out of README.md (now an abridged index) into docs/reference/.
grep -q "policy/single-valued.md" docs/reference/single-valued.md && ok "(e) docs document it" || bad "(e) docs do not"
grep -q "policy/single-valued.md" docs/reference/single-valued.en.md && ok "(e) docs.en document it" || bad "(e) docs.en do not"

echo
if [ "$fails" -eq 0 ]; then echo "single-valued scaffold: all passed"; else echo "single-valued scaffold: $fails failed"; exit 1; fi
