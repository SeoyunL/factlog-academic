#!/usr/bin/env bash
# tests/test_corroboration.sh — multi-source corroboration (#33)
#
# Pins:
#   - a fact backed by 2 distinct sources reports "2 source(s)"; a single-source
#     fact reports "1 source(s)"
#   - same (s,r,o) from the same source twice counts as 1 (distinct sources)
#   - single-valued relations with competing values show per-source support
#   - corroboration.py is informational (always exit 0)
#
# Deterministic; no pyrewire.  Usage: bash tests/test_corroboration.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
CORR="$PLUGIN_ROOT/tools/corroboration.py"
HEADER="subject,relation,object,source,status,confidence,note"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
csv() { printf '%s\n' "$HEADER" "$@" > "$KB/facts/candidates.csv"; }

# Acme API/uses/FastAPI backed by 2 sources; Acme API/depends_on/Postgres by 1.
csv \
  'Acme API,uses,FastAPI,sources/a.md,confirmed,0.9,' \
  'Acme API,uses,FastAPI,sources/b.md,confirmed,0.9,' \
  'Acme API,depends_on,Postgres,sources/a.md,confirmed,0.9,'
set +e; out="$("$PYTHON" "$CORR" --wiki "$KB" 2>&1)"; rc=$?; set -e  # capture before errexit
[ "$rc" -eq 0 ] && ok "corroboration.py exits 0 (informational)" || bad "corroboration.py exit $rc"
printf '%s' "$out" | grep -qF "2 source(s): Acme API, uses, FastAPI" && ok "fact backed by 2 sources reports 2" || bad "2-source fact not reported"
printf '%s' "$out" | grep -qF "1 source(s): Acme API, depends_on, Postgres" && ok "single-source fact reports 1" || bad "1-source fact not reported"

# same source twice = 1 distinct source
csv 'X,r,Y,sources/a.md,confirmed,0.9,' 'X,r,Y,sources/a.md,confirmed,0.9,dup'
printf '%s' "$("$PYTHON" "$CORR" --wiki "$KB" 2>&1)" | grep -qF "1 source(s): X, r, Y" && ok "same source twice counts as 1 distinct" || bad "duplicate source miscounted"

# single-valued competing values show per-source support
printf '# single-valued\n- 주_속성\n' > "$KB/policy/single-valued.md"
csv \
  '을서비스,주_속성,값가,sources/a.md,confirmed,0.9,' \
  '을서비스,주_속성,값나,sources/b.md,confirmed,0.9,'
co="$("$PYTHON" "$CORR" --wiki "$KB" 2>&1)"
printf '%s' "$co" | grep -qF "competing values" && ok "single-valued competing values reported" || bad "competing values not reported"
printf '%s' "$co" | grep -qF "값가 (1 src)" && ok "competing value shows per-source support" || bad "per-source support missing"

# uniformly-NFD KB still reaches the competing-values bucket (#325): the policy
# file holds the composed relation name, the rows the decomposed one. Nothing is
# mixed — one consistently decomposed KB is enough, and a raw membership test
# matches none of it. Exercises the consumer, not just the shared helper.
"$PYTHON" - "$KB" <<'PY'
import sys, unicodedata
from pathlib import Path
kb = Path(sys.argv[1])
nfc = lambda s: unicodedata.normalize("NFC", s)
nfd = lambda s: unicodedata.normalize("NFD", s)
(kb / "policy" / "single-valued.md").write_text(
    f"# single-valued\n- {nfc('소속')}\n", encoding="utf-8"
)
(kb / "facts" / "candidates.csv").write_text(
    "subject,relation,object,source,status,confidence,note\n"
    f"{nfd('김철수')},{nfd('소속')},AAA,sources/a.md,confirmed,0.9,\n"
    f"{nfd('김철수')},{nfd('소속')},BBB,sources/b.md,confirmed,0.9,\n",
    encoding="utf-8",
)
PY
co="$("$PYTHON" "$CORR" --wiki "$KB" 2>&1)"
printf '%s' "$co" | grep -qF "competing values" \
  && ok "uniformly-NFD KB reaches competing values (membership folded)" \
  || bad "NFD KB competing values missed: $(printf '%s' "$co" | tail -3)"

echo ""
echo "========================================"
echo "test_corroboration: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
