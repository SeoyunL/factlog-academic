#!/usr/bin/env bash
# tests/test_hwp_ingest.sh — legacy .hwp ingest via hwp5html -> pandoc (#59)
#
# A real HWP 5.x file is an OLE binary that cannot be synthesised portably, and
# hwp5html (pyhwp) is not installed in CI — so this pins the REGISTRATION and the
# MISSING-TOOL behaviour deterministically (by running with a PATH that excludes
# hwp5html). The happy path (hwp5html + pandoc present) is verified manually.
#
# Pins:
#   - .hwp is registered as a built-in converter (not a "no converter" hint)
#   - with hwp5html absent: explicit ingest fails with an install-pyhwp hint;
#     under --scan it is a soft skip (run does not fail)
#
# Usage: bash tests/test_hwp_ingest.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
# Resolve to an absolute path before the restricted-PATH cases below (#459): a bare
# command name would fall outside PATH="/usr/bin:/bin" and the interpreter itself
# would be unfound (rc 127), masking the hwp5html-absent behaviour under test.
PYTHON_ABS="$(command -v "$PYTHON")" || { echo "PYTHON not found: $PYTHON" >&2; exit 1; }

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

# --- registration -------------------------------------------------------------
verdict="$(FACTLOG_ROOT="/tmp" "$PYTHON" - <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.environ.get("PYTHONPATH", "").split(":")[0], "factlog"))
from factlog import ingest
problems = []
chain = ingest.INGEST_CONVERTERS.get(".hwp")
if not chain or chain[0][0] != "factlog-hwp":
    problems.append(".hwp not registered to factlog-hwp")
if "factlog-hwp" not in ingest.BUILTIN_CONVERTERS:
    problems.append("factlog-hwp not a built-in converter")
if ".hwp" in ingest.INGEST_HINTS:
    problems.append(".hwp still in unsupported hints")
if chain and chain[0][1] != ".md":
    problems.append(".hwp output suffix is not .md")
print("OK" if not problems else "FAIL: " + "; ".join(problems))
PY
)"
[ "$verdict" = "OK" ] && ok "hwp registered as built-in .md converter (not unsupported)" || bad "$verdict"

# --- missing-tool behaviour (force hwp5html absent via restricted PATH) -------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf '\xd0\xcf\x11\xe0\x00binary' > "$KB/sources/doc.hwp"   # OLE-ish; never parsed (tool 'absent')

# explicit: fails, with the install hint
set +e
out="$(PATH="/usr/bin:/bin" "$PYTHON_ABS" -m factlog ingest --target "$KB" "$KB/sources/doc.hwp" 2>&1)"
rc=$?
set -e
[ "$rc" -ne 0 ] && ok "explicit .hwp with hwp5html absent fails (rc!=0)" || bad "expected failure when hwp5html absent (rc=$rc)"
printf '%s' "$out" | grep -qF "install pyhwp" && ok "missing-tool message hints pip install pyhwp" || bad "no pyhwp install hint: $out"

# --scan: soft skip, run does not fail
set +e
PATH="/usr/bin:/bin" "$PYTHON_ABS" -m factlog ingest --target "$KB" --scan >/dev/null 2>&1
rc=$?
set -e
[ "$rc" -eq 0 ] && ok "--scan with hwp5html absent soft-skips (rc 0)" || bad "--scan failed on absent tool (rc=$rc)"

echo ""
echo "========================================"
echo "test_hwp_ingest: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
