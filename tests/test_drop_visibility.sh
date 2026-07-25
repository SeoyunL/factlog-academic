#!/usr/bin/env bash
# tests/test_drop_visibility.sh — deleting a source must not make its rows
# unobservable (#558)
#
# The round trip this pins is the one a KB actually takes:
#
#   sync/merge -> a source file is deleted -> merge again -> status query
#
# The second merge rebuilds facts/candidates.csv WITHOUT the rows citing the
# deleted source (they fail the sources/-on-disk check before the CSV is
# written), and it says so on stderr. But stderr is gone the moment the terminal
# scrolls, and every status query afterwards reads candidates.csv — where those
# rows no longer exist. So the KB kept 145 rows citing a deleted source while
# `source_coverage.py` reported `0 orphan citation(s)`, run after run, and
# `factlog eject --orphans` (which builds its cited set from the same CSV)
# reported nothing to clean. Not a loop that closes: a loop that cannot close.
#
# The rows are only `candidate` here on purpose. A confirmed/accepted row is
# caught by the #218 ratchet, which REFUSES the rebuild and leaves the ghost row
# in the CSV where the orphan axis sees it. `candidate` is the status that
# rebuilds silently, and it is what all 145 observed rows were.
#
# Pins:
#   (a) after delete + re-merge, coverage names the deleted source
#   (b) it reports the ROW COUNT, and that count equals merge's own per-source
#       `skip row ... (N rows)` figure — NOT the `N row(s) dropped during
#       normalise/dedup` total, which also counts dedup losers. The fixture makes
#       the two differ (1 vs 2) so a comparison against the wrong counter fails.
#   (c) the surviving source is unaffected: still enumerated, never reported as
#       missing (its own fact count is 0 — coverage counts engine facts and these
#       rows are `candidate`, which is the pre-existing rule, untouched here)
#   (d) the report stays informational — exit 0, no --strict gate
#
# Deterministic; no pyrewire.  Usage: bash tests/test_drop_visibility.sh

set -euo pipefail

TMP_ROOT="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$TMP_ROOT"' EXIT

export XDG_CONFIG_HOME="$TMP_ROOT/cfg"  # isolate the active-KB config (#62)

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
MERGE="$PLUGIN_ROOT/tools/merge_candidates.py"
COV="$PLUGIN_ROOT/tools/source_coverage.py"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$TMP_ROOT/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null

# --- two sources, both cited by run rows --------------------------------------
printf 'keeper\n' > "$KB/sources/keeper.md"
printf 'doomed\n' > "$KB/sources/doomed.md"
cat > "$KB/runs/2026-01-01T00-00-00-a.json" <<'JSON'
[{"subject":"갑봇","relation":"통합","object":"을서비스","source":"sources/keeper.md","status":"candidate","confidence":0.9,"note":""},
 {"subject":"구성_요소","relation":"포함","object":"주_속성","source":"sources/doomed.md","status":"candidate","confidence":0.9,"note":""}]
JSON
# A duplicate of the keeper row, in a second run file: it is dropped by DEDUP,
# never by the source check. Its only job is to make the two merge counters
# differ, so (b) cannot pass by reading the wrong one.
cat > "$KB/runs/2026-01-01T00-00-01-b.json" <<'JSON'
[{"subject":"갑봇","relation":"통합","object":"을서비스","source":"sources/keeper.md","status":"candidate","confidence":0.9,"note":""}]
JSON

"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
grep -qF "sources/doomed.md" "$KB/facts/candidates.csv" \
  && ok "before the delete: the doomed source's row is in candidates.csv" \
  || bad "first merge did not record the doomed source's row"

# --- delete one source, merge again -------------------------------------------
rm -f "$KB/sources/doomed.md"
set +e
merge_err="$("$PYTHON" "$MERGE" --wiki "$KB" 2>&1 >/dev/null)"
merge_rc=$?
set -e
[ "$merge_rc" -eq 0 ] && ok "re-merge after the delete succeeds (candidate rows rebuild silently)" \
  || bad "re-merge exited $merge_rc: $merge_err"

# What merge itself says: one source, one row rejected for it...
printf '%s' "$merge_err" | grep -qF "skip row: source 'sources/doomed.md' not found in sources/" \
  && ok "merge warns about the deleted source" || bad "merge warning missing: $merge_err"
skip_n="$(printf '%s' "$merge_err" | sed -n "s/.*skip row: source 'sources\/doomed.md'.*(\([0-9]*\) rows*).*/\1/p")"
[ "$skip_n" = "1" ] && ok "merge's per-source skip count is 1" || bad "unexpected skip count '$skip_n': $merge_err"
# ...while its normalise/dedup total is 2, because the duplicate keeper row is in
# it. These are different questions; the report must answer the first one.
dropped_n="$(printf '%s' "$merge_err" | sed -n 's/.*warning: \([0-9]*\) row(s) dropped during normalise\/dedup.*/\1/p')"
[ "$dropped_n" = "2" ] && ok "the normalise/dedup total (2) differs from the skip count (1)" \
  || bad "fixture broken: dropped total '$dropped_n' should be 2"

# ...and the row is gone from candidates.csv, which is why every status query
# reading that file goes blind here.
grep -qF "sources/doomed.md" "$KB/facts/candidates.csv" \
  && bad "the rebuild kept the ghost row (fixture no longer reproduces the blind spot)" \
  || ok "after the delete: candidates.csv no longer carries the row"

# --- the status query must still see it ---------------------------------------
set +e
cov_out="$("$PYTHON" "$COV" --wiki "$KB" 2>&1)"
cov_rc=$?
set -e
printf '%s' "$cov_out" | grep -qF "sources/doomed.md" \
  && ok "coverage names the deleted source after the re-merge" \
  || bad "coverage is blind to the deleted source: $cov_out"
printf '%s' "$cov_out" | grep -qF "RUN ROWS cite a missing source (dropped at merge, ${skip_n} row(s)): sources/doomed.md" \
  && ok "coverage's row count equals merge's own skip count ($skip_n)" \
  || bad "coverage row count disagrees with merge's skip count ($skip_n): $cov_out"
printf '%s' "$cov_out" | grep -qF "1 run-cited source(s) missing" \
  && ok "the summary line carries the count" || bad "summary field missing: $cov_out"
[ "$cov_rc" -eq 0 ] && ok "coverage stays informational (exit 0)" || bad "coverage exited $cov_rc"

# The surviving source is untouched by any of this: it is on disk, so it is
# listed as a source and never as a run-cited missing one. (Its own count is 0
# because coverage counts ENGINE facts and these rows are `candidate` — the
# pre-existing rule this change does not touch, and the same reason the row-level
# loss above was invisible to the orphan axis in the first place.)
printf '%s' "$cov_out" | grep -qF "coverage: 1 source(s)" \
  && ok "the surviving source is still enumerated" || bad "source tally wrong: $cov_out"
printf '%s' "$cov_out" | grep -qE "0 fact\(s\): sources/keeper.md" \
  && ok "the surviving source is listed (0 engine facts: its rows are candidates)" \
  || bad "keeper.md line wrong: $cov_out"
printf '%s' "$cov_out" | grep -qE "RUN ROWS cite a missing source.*sources/keeper\.md" \
  && bad "a source that IS on disk was reported as missing" \
  || ok "a source on disk is never reported as run-cited-missing"

echo ""
echo "========================================"
echo "test_drop_visibility: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
