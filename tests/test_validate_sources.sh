#!/usr/bin/env bash
# tests/test_validate_sources.sh — validate.py accepts runs/sources/ origins (#24)
#
# runs/sources/ is a second valid source root (factlog ingest writes converted
# text there). This pins that validate.py accepts a runs/sources/-prefixed
# `source` exactly like a sources/-prefixed one, still rejects a bare filename,
# and that validate_source_ref resolves a runs/sources/ file.
#
# Also pins the `superseded` tombstone waiver (#562): a superseded row points, by
# definition, at a source that is gone — `eject` leaves it behind on purpose — so
# the file-existence check must not fire for it, or every cleanup would leave the
# KB permanently "validation failed". The waiver is file-existence ONLY: a
# `confirmed` row with a missing source still fails, and the #anchor check still
# fires for a superseded row whose file is present.
#
# Asserts on the specific "source must start with" message so unrelated
# structural validations in the KB do not affect the result.
#
# Usage: bash tests/test_validate_sources.sh  -> 0 if all pass, 1 otherwise.

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
VALIDATE="$PLUGIN_ROOT/tools/validate.py"
HEADER="subject,relation,object,source,status,confidence,note"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
mkdir -p "$KB/runs/sources"
printf '# converted\n\nWidgetX integrates ToolA.\n' > "$KB/runs/sources/conv.md"
printf '# original\n\nAcme uses FastAPI.\n' > "$KB/sources/orig.md"

# write a candidates.csv with the given source value (one row)
write_csv() { printf '%s\nWidgetX,integrates,ToolA,%s,confirmed,0.90,\n' "$HEADER" "$1" > "$KB/facts/candidates.csv"; }
prefix_err() { "$PYTHON" "$VALIDATE" "$KB" 2>&1 | grep -c "source must start with" || true; }

# runs/sources/ source -> must NOT raise the prefix error
write_csv "runs/sources/conv.md"
if [ "$(prefix_err)" = "0" ]; then ok "runs/sources/ source accepted (no prefix error)"; else bad "runs/sources/ source wrongly rejected"; fi

# sources/ source -> must NOT raise the prefix error (unchanged behavior)
write_csv "sources/orig.md"
if [ "$(prefix_err)" = "0" ]; then ok "sources/ source still accepted"; else bad "sources/ source wrongly rejected"; fi

# bare filename -> MUST raise the prefix error
write_csv "conv.md"
if [ "$(prefix_err)" -ge 1 ]; then ok "bare filename still rejected"; else bad "bare filename was not rejected"; fi

# runs/sources/ ref to a MISSING file -> source-existence error (not prefix)
write_csv "runs/sources/missing.md"
out="$("$PYTHON" "$VALIDATE" "$KB" 2>&1 || true)"
if printf '%s' "$out" | grep -q "source must start with"; then bad "missing runs/sources file raised a prefix error (should be existence error)"; else ok "missing runs/sources file passes prefix check (handed to existence check)"; fi
if printf '%s' "$out" | grep -q "source file does not exist"; then ok "validate_source_ref resolves runs/sources/ (flags the missing file)"; else bad "validate_source_ref did not flag the missing file"; fi

# --- superseded tombstones (#562) ---------------------------------------------
# A separate KB so the whole EXIT CODE is meaningful: a tombstone must leave the
# KB validating clean, not merely drop one message. Needs a page, otherwise the
# unrelated "facts exist but pages/ has no concept pages" error masks the result.
TKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$TKB" >/dev/null
printf '# live\n\nWidgetX integrates ToolA.\n' > "$TKB/sources/live.md"
printf '# WidgetX\n\n- WidgetX integrates ToolA\n' > "$TKB/pages/WidgetX.md"
twrite() { printf '%s\nWidgetX,integrates,ToolA,%s,%s,0.90,\n' "$HEADER" "$1" "$2" > "$TKB/facts/candidates.csv"; }
trun() { "$PYTHON" "$VALIDATE" "$TKB" 2>&1; }

# superseded + source file gone -> clean (rc 0)
twrite "sources/ghosty.md" "superseded"
set +e; out="$(trun)"; rc=$?; set -e
printf '%s\n' "$out"
if [ "$rc" -eq 0 ]; then ok "superseded tombstone with a removed source validates clean (rc 0)"; else bad "superseded tombstone still fails validation (rc $rc)"; fi

# confirmed + the SAME missing source -> still an error (the waiver is status-keyed)
twrite "sources/ghosty.md" "confirmed"
set +e; out="$(trun)"; rc=$?; set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "source file does not exist: sources/ghosty.md"; then
  ok "confirmed row with a removed source still fails"
else
  bad "the waiver leaked to confirmed rows"
fi

# superseded + file PRESENT + bad #anchor -> the section check is still alive
twrite "sources/live.md#no-such-section" "superseded"
set +e; out="$(trun)"; rc=$?; set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "source section does not exist: sources/live.md#no-such-section"; then
  ok "superseded row still gets its #anchor checked when the file exists"
else
  bad "the waiver killed the anchor check for superseded rows"
fi

# superseded + file present + a REAL anchor -> clean, so the check above is not
# just "superseded always fails on an anchor".
twrite "sources/live.md#live" "superseded"
set +e; out="$(trun)"; rc=$?; set -e
if [ "$rc" -eq 0 ]; then ok "superseded row with a resolvable #anchor validates clean"; else bad "valid superseded anchor rejected (rc $rc): $out"; fi

echo ""
echo "========================================"
echo "test_validate_sources: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
