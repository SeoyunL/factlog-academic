#!/usr/bin/env bash
# tests/test_attr_path_exclusion.sh — attribute-relation objects are not path nodes (#329)
#
# policy/attribute-relations.md (scaffolded by `factlog init`) promises the object
# of a declared attribute relation is kept out of the entity set "so they do not
# show up as entities, path nodes, or count subjects". path did not honour it:
# the emitted engine rule was `edge(S, O) :- relation(S, R, O).` with no filter,
# so a date became a node of the entity graph.
#
# Pins, end to end through compile_facts + run_logic_check (the real engine):
#   - NO attribute-relations declared -> path through the literal is reported
#     (backward compatibility; the scaffolded stub declares nothing)
#   - declared attribute relation     -> engine path/2 has no pair ending at the
#     literal, and the report line NAMES the reason rather than answering
#     "(not found)", which would read as "the facts do not connect them" while
#     `ask` rejects the same query as entity_not_accepted (#329 round 2)
#   - a non-attribute edge in the same KB is untouched
#   - the literal remains a verifiable relation-query object
#
# Requires pyrewire. Skipped cleanly if absent.
# Usage: PYTHON=<path> bash tests/test_attr_path_exclusion.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62)

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
CF="$PLUGIN_ROOT/tools/compile_facts.py"
RLC="$PLUGIN_ROOT/tools/run_logic_check.py"
HEADER="subject,relation,object,source,status,confidence,note"

pass=0
fail=0
ok()  { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

if ! "$PYTHON" -c "import pyrewire" >/dev/null 2>&1; then
  echo "SKIP: pyrewire not installed; test_attr_path_exclusion requires the engine"
  exit 0
fi

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null 2>&1

printf '%s\n' "$HEADER" \
  '갑봇,통합,을서비스,sources/a.md,accepted,0.9,' \
  '을서비스,정식_운영,2030.1,sources/a.md,accepted,0.9,' \
  > "$KB/facts/candidates.csv"
printf '# a\n' > "$KB/sources/a.md"

cat > "$KB/facts/query.dl" <<'EOF'
path("갑봇", "2030.1")?
path("갑봇", "을서비스")?
relation("을서비스", "정식_운영", "2030.1")?
EOF

FACTLOG_ROOT="$KB" "$PYTHON" "$CF" >/dev/null 2>&1

# Engine path/2 extent, decoded — read straight out of run_wirelog so the engine
# side is pinned on its own and not only through the report's rendered line.
engine_pairs() {
  FACTLOG_ROOT="$KB" "$PYTHON" - <<'PY'
import sys; sys.path.insert(0, "tools")
import common as c
for s, t in sorted(c.run_wirelog()["path"]):
    print(f"{s} -> {t}")
PY
}

# ---------------------------------------------------------------------------
# Case 1: nothing declared (scaffolded stub) — behaviour unchanged
# ---------------------------------------------------------------------------
before="$(engine_pairs)"
printf '%s' "$before" | grep -qF '을서비스 -> 2030.1' \
  && ok "no declarations -> engine path still reaches the literal (backward compat)" \
  || bad "no declarations -> engine path lost 을서비스 -> 2030.1: $before"

report="$(FACTLOG_ROOT="$KB" "$PYTHON" "$RLC" 2>&1)"
printf '%s' "$report" | grep -qF 'path 갑봇 -> 2030.1: 갑봇 -> 을서비스 -> 2030.1' \
  && ok "no declarations -> report traces the path through the literal" \
  || bad "no declarations -> expected trace missing from report"

# ---------------------------------------------------------------------------
# Case 2: 정식_운영 declared an attribute relation
# ---------------------------------------------------------------------------
printf -- '- `정식_운영`\n' > "$KB/policy/attribute-relations.md"

after="$(engine_pairs)"
printf '%s' "$after" | grep -qF '2030.1' \
  && bad "declared attribute -> engine path still contains the literal: $after" \
  || ok "declared attribute -> engine path/2 has no pair naming the literal"

printf '%s' "$after" | grep -qF '갑봇 -> 을서비스' \
  && ok "declared attribute -> the non-attribute edge is untouched (non-vacuous)" \
  || bad "declared attribute -> lost the unrelated edge 갑봇 -> 을서비스: $after"

report="$(FACTLOG_ROOT="$KB" "$PYTHON" "$RLC" 2>&1)"
printf '%s' "$report" | grep -qF 'path 갑봇 -> 2030.1: (not evaluated — not an accepted entity: 2030.1)' \
  && ok "declared attribute -> report gives the literal path query its reason" \
  || bad "declared attribute -> report still traces a path to the literal, or hides the reason"

printf '%s' "$report" | grep -qF 'query path argument is not an accepted entity: 2030.1' \
  && ok "declared attribute -> report warns with the same wording ask uses" \
  || bad "declared attribute -> report answers the literal path query with no warning"

printf '%s' "$report" | grep -qF 'path 갑봇 -> 을서비스: 갑봇 -> 을서비스' \
  && ok "declared attribute -> the entity path query still answers" \
  || bad "declared attribute -> entity path query regressed"

printf '%s' "$report" | grep -qF 'relation results: 1 rows; 을서비스, 정식_운영, 2030.1' \
  && ok "declared attribute -> the literal is still a verifiable relation-query object" \
  || bad "declared attribute -> relation query on the literal object regressed"

echo ""
echo "========================================"
echo "test_attr_path_exclusion: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
