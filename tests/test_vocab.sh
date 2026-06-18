#!/usr/bin/env bash
# tests/test_vocab.sh — `factlog vocab` vocabulary discovery (#85)
#
# Pins (XDG-isolated; synthetic data):
#   - default lists BOTH entities and relations with usage counts
#   - entity = subject + object whose relation is NOT an attribute relation
#     (an attribute-relation object is a literal -> excluded from entities)
#   - relations tagged [attribute] / [single-valued] from policy files
#   - --entities / --relations show only one section
#   - default scope is engine facts; --all adds candidate-only names
#   - empty KB graceful; non-KB path errors
#
# Usage: bash tests/test_vocab.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62)

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
H="subject,relation,object,source,status,confidence,note"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'x\n' > "$KB/sources/a.md"
printf -- '- 운영\n' > "$KB/policy/attribute-relations.md"     # 운영 object is a literal
printf '# single-valued\n- 주속성\n' > "$KB/policy/single-valued.md"
printf '%s\n%s\n%s\n%s\n%s\n' "$H" \
  '갑봇,통합,을서비스,sources/a.md,confirmed,0.9,' \
  '갑봇,운영,2030,sources/a.md,confirmed,0.9,' \
  '주체,주속성,값,sources/a.md,confirmed,0.9,' \
  '후보주체,후보관계,후보객체,sources/a.md,candidate,0.5,' > "$KB/facts/candidates.csv"

# --- default: both sections, engine scope ------------------------------------
out="$("$PYTHON" -m factlog vocab --target "$KB" 2>&1)"
printf '%s\n' "$out"; echo "---"
printf '%s' "$out" | grep -qF "— engine facts" && ok "default scope is engine facts" || bad "scope label wrong"
printf '%s' "$out" | grep -qE "entities \(4\):" && ok "entity count excludes literals + candidate-only (4)" || bad "entity count wrong: $(printf '%s' "$out"|grep 'entities (')"
printf '%s' "$out" | grep -qE "relations \(3\):" && ok "relation count is engine-only (3)" || bad "relation count wrong: $(printf '%s' "$out"|grep 'relations (')"
printf '%s' "$out" | grep -qE "\] 을서비스" && ok "non-attribute object listed as an entity" || bad "entity object missing"
printf '%s' "$out" | grep -qF "2030" && bad "attribute-relation object (literal) leaked into vocab" || ok "literal object excluded from entities"
printf '%s' "$out" | grep -qF "후보관계" && bad "candidate-only name shown without --all" || ok "candidate-only names excluded by default"
printf '%s' "$out" | grep -qE "\] 운영  \[attribute\]" && ok "attribute relation tagged" || bad "attribute tag missing"
printf '%s' "$out" | grep -qE "\] 주속성  \[single-valued\]" && ok "single-valued relation tagged" || bad "single-valued tag missing"

# --- --entities / --relations show one section -------------------------------
out="$("$PYTHON" -m factlog vocab --entities --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "entities (" && ! printf '%s' "$out" | grep -qF "relations (" && ok "--entities shows only entities" || bad "--entities leaked relations"
out="$("$PYTHON" -m factlog vocab --relations --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "relations (" && ! printf '%s' "$out" | grep -qF "entities (" && ok "--relations shows only relations" || bad "--relations leaked entities"

# --- --all includes candidate-only names -------------------------------------
out="$("$PYTHON" -m factlog vocab --all --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "— all candidate facts" && ok "--all scope label" || bad "--all scope label wrong"
printf '%s' "$out" | grep -qF "후보관계" && ok "--all includes candidate-only relation" || bad "--all missing candidate relation"
printf '%s' "$out" | grep -qE "\] 후보주체" && ok "--all includes candidate-only entity" || bad "--all missing candidate entity"

# --- empty KB graceful -------------------------------------------------------
EKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$EKB" >/dev/null
out="$("$PYTHON" -m factlog vocab --target "$EKB" 2>&1)"
printf '%s' "$out" | grep -qE "entities \(0\):" && printf '%s' "$out" | grep -qF "(none)" && ok "empty KB shows 0 entities/(none)" || bad "empty KB not graceful: $out"

# --- non-KB path errors ------------------------------------------------------
set +e; "$PYTHON" -m factlog vocab --target "$(mktemp -d)" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -ne 0 ] && ok "vocab on a non-KB path errors" || bad "non-KB path should error"

echo ""
echo "========================================"
echo "test_vocab: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
