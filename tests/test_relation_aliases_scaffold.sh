#!/usr/bin/env bash
# init must scaffold policy/relation-aliases.md, its example must work, and a mapping
# without backticks must warn rather than be silently ignored (#230). This file was the
# only policy file init did not create, the only one absent from both READMEs, and the
# only one whose parser dropped a malformed line without a word.
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

[ -f "$KB/policy/relation-aliases.md" ] && ok "(a) init scaffolds policy/relation-aliases.md" \
  || { bad "(a) not scaffolded"; exit 1; }

# every `factlog <sub>` the scaffold names must exist (no dead-end advice, per #224)
for SUB in $(grep -oE '(^|[^/])factlog [a-z-]+' "$KB/policy/relation-aliases.md" 2>/dev/null \
             | sed -E 's/.*factlog //' | sort -u); do
  "$PY" -m factlog "$SUB" --help >/dev/null 2>&1 && ok "(a) 'factlog $SUB' exists" \
    || bad "(a) scaffold names 'factlog $SUB', which does not exist"
done

# the scaffolded example must actually parse and fold both aliases to the canonical
sed -e 's/^# - `\(.*\)` -> `\(.*\)`$/- `\1` -> `\2`/' "$KB/policy/relation-aliases.md" > "$KB/policy/ra.tmp"
mv "$KB/policy/ra.tmp" "$KB/policy/relation-aliases.md"
PARSED="$(FACTLOG_ROOT="$KB" "$PY" -c "
import os, sys; sys.path.insert(0, os.getcwd())
from pathlib import Path
import factlog.common as c
print(c.relation_aliases(Path(os.environ['FACTLOG_ROOT'])))" 2>/dev/null)"
printf '%s' "$PARSED" | grep -q "'게재연도': 'published_year'" \
  && printf '%s' "$PARSED" | grep -q "'publication_year': 'published_year'" \
  && ok "(b) the scaffolded example parses and folds both aliases to the canonical" \
  || bad "(b) the scaffolded example does not work: $PARSED"

# a mapping without backticks must warn, not silently no-op
KB2="$(mktemp -d)/kb"
"$PY" -m factlog init --target "$KB2" >/dev/null
printf -- '- 게재연도 -> published_year\n' > "$KB2/policy/relation-aliases.md"
ERR="$(FACTLOG_ROOT="$KB2" "$PY" -c "
import os, sys; sys.path.insert(0, os.getcwd())
from pathlib import Path
import factlog.common as c
c.relation_aliases(Path(os.environ['FACTLOG_ROOT']))" 2>&1 >/dev/null)"
printf '%s' "$ERR" | grep -q "skipping malformed line" \
  && ok "(c) a mapping without backticks is reported malformed, not silently dropped" \
  || bad "(c) the malformed mapping was silent"

# a properly-quoted mapping does NOT warn (no crying wolf)
printf -- '- `게재연도` -> `published_year`\n' > "$KB2/policy/relation-aliases.md"
ERR2="$(FACTLOG_ROOT="$KB2" "$PY" -c "
import os, sys; sys.path.insert(0, os.getcwd())
from pathlib import Path
import factlog.common as c
c.relation_aliases(Path(os.environ['FACTLOG_ROOT']))" 2>&1 >/dev/null)"
printf '%s' "$ERR2" | grep -q "malformed" && bad "(c) a valid mapping was warned about" \
  || ok "(c) a valid mapping is silent"

# a plain comment/blank line is NOT warned about
printf -- '# just a comment\n\n' > "$KB2/policy/relation-aliases.md"
ERR3="$(FACTLOG_ROOT="$KB2" "$PY" -c "
import os, sys; sys.path.insert(0, os.getcwd())
from pathlib import Path
import factlog.common as c
c.relation_aliases(Path(os.environ['FACTLOG_ROOT']))" 2>&1 >/dev/null)"
printf '%s' "$ERR3" | grep -q "malformed" && bad "(c) a comment line was warned about" \
  || ok "(c) comments and blanks are silent"

# refinements: inline comments, unicode arrows, backtick-internal '#'
parse_out() {  # $1=kb -> the parsed dict, stderr merged, malformed marker preserved
  FACTLOG_ROOT="$1" "$PY" -c "
import os, sys; sys.path.insert(0, os.getcwd())
from pathlib import Path
import factlog.common as c
print(c.relation_aliases(Path(os.environ['FACTLOG_ROOT'])))" 2>&1
}

printf -- '- `게재연도` -> `published_year`  # 저널 게재연도\n' > "$KB2/policy/relation-aliases.md"
O="$(parse_out "$KB2")"
printf '%s' "$O" | grep -q "'게재연도': 'published_year'" && ! printf '%s' "$O" | grep -q malformed \
  && ok "(e) a valid mapping with an inline comment parses, no warning" \
  || bad "(e) an inline comment broke a valid mapping: $O"

printf -- '- `게재연도` \xe2\x86\x92 `published_year`\n' > "$KB2/policy/relation-aliases.md"  # unicode arrow
O="$(parse_out "$KB2")"
printf '%s' "$O" | grep -q malformed \
  && ok "(e) a unicode-arrow mapping is warned, not silently dropped" \
  || bad "(e) a unicode arrow was dropped silently: $O"

printf -- '- `a#b` -> `c`\n' > "$KB2/policy/relation-aliases.md"  # '#' inside backticks
O="$(parse_out "$KB2")"
printf '%s' "$O" | grep -q "'a#b': 'c'" && ! printf '%s' "$O" | grep -q malformed \
  && ok "(e) a '#' inside backticks is part of the name, not a comment" \
  || bad "(e) a backtick-internal '#' was mishandled: $O"

grep -q "policy/relation-aliases.md" README.md && ok "(d) README documents it" || bad "(d) README does not"
grep -q "policy/relation-aliases.md" README.ko.md && ok "(d) README.ko documents it" || bad "(d) README.ko does not"

echo
if [ "$fails" -eq 0 ]; then echo "relation-aliases scaffold: all passed"; else echo "relation-aliases scaffold: $fails failed"; exit 1; fi
