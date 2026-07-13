#!/usr/bin/env bash
# tests/test_sync_ignore.sh — per-KB sync-ignore list (#76)
#
# Pins (XDG-isolated; synthetic data):
#   - `factlog init` scaffolds policy/sync-ignore.md
#   - `factlog ignore` add / list / --remove round-trip (file-backed)
#   - pattern matching: glob (drafts/*.md), full ref (sources/x.md), in-source
#     path; a bare `*.md` glob is NOT eaten as a bullet; comments/backticks
#   - `factlog ingest --scan` skips an ignored binary (explicit path still works)
#   - `factlog sources` shows [ignored]; coverage reports `excluded`, not a gap,
#     and --strict does not fail on an ignored text source
#   - an ignored source's already-merged facts are RETAINED across a re-merge
#
# Usage: bash tests/test_sync_ignore.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62)

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

# --- unit: common.sync_ignore_patterns / is_sync_ignored ----------------------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
[ -f "$KB/policy/sync-ignore.md" ] && ok "init scaffolds policy/sync-ignore.md" || bad "sync-ignore.md not scaffolded"

# format: comments, '-' bullets, backtick-quoted, and a bare *.md glob survive
cat > "$KB/policy/sync-ignore.md" <<'EOF'
# a comment, ignored
- drafts/*.md
- sources/wip-notes.md
- `with space.md`
*.tmp
- archive/**
- build/
EOF
out="$(FACTLOG_ROOT="$KB" PYTHONPATH="$PLUGIN_ROOT/tools:$PYTHONPATH" "$PYTHON" - <<'PY'
import common
pats = common.sync_ignore_patterns()
print("PATTERNS", pats)
def chk(ref): print(ref, common.is_sync_ignored(ref, pats))
chk("sources/drafts/x.md")        # glob via in-source path (single segment)
chk("sources/drafts/sub/deep.md") # * must NOT cross '/' -> not matched
chk("sources/keep.md")            # not matched
chk("sources/wip-notes.md")       # full ref
chk("sources/with space.md")      # backtick-quoted with space
chk("runs/sources/scratch.tmp")   # bare *.tmp glob (not eaten as bullet)
chk("sources/archive/2020/old.md")# '**' crosses segments
chk("sources/build/out/x.md")     # trailing '/' = whole subtree
PY
)"
printf '%s\n' "$out" | grep -qF "'drafts/*.md'" && printf '%s\n' "$out" | grep -qF "'*.tmp'" && ok "parser: glob '*.tmp' survives (not a bullet); comments dropped" || bad "parser wrong: $out"
printf '%s\n' "$out" | grep -qx "sources/drafts/x.md True" && ok "match: glob via in-source path (single segment)" || bad "glob in-source match failed"
printf '%s\n' "$out" | grep -qx "sources/drafts/sub/deep.md False" && ok "match: '*' does NOT cross '/' (depth boundary)" || bad "'*' wrongly crossed '/': $out"
printf '%s\n' "$out" | grep -qx "sources/keep.md False" && ok "non-matching source not ignored" || bad "false positive match"
printf '%s\n' "$out" | grep -qx "sources/wip-notes.md True" && ok "match: full ref" || bad "full-ref match failed"
printf '%s\n' "$out" | grep -qx "sources/with space.md True" && ok "match: backtick-quoted pattern with space" || bad "quoted-pattern match failed"
printf '%s\n' "$out" | grep -qx "runs/sources/scratch.tmp True" && ok "match: bare glob against runs/sources ref" || bad "runs/sources glob match failed"
printf '%s\n' "$out" | grep -qx "sources/archive/2020/old.md True" && ok "match: '**' crosses segments" || bad "'**' did not cross segments"
printf '%s\n' "$out" | grep -qx "sources/build/out/x.md True" && ok "match: trailing '/' = whole subtree" || bad "trailing-slash subtree match failed"

# --- factlog ignore add / list / remove --------------------------------------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
mkdir -p "$KB/sources/drafts"
printf 'd\n' > "$KB/sources/drafts/wip.md"; printf 'k\n' > "$KB/sources/keep.md"
# active (non-comment) lines only — the scaffold ships commented examples
active() { grep -v '^[[:space:]]*#' "$KB/policy/sync-ignore.md"; }
"$PYTHON" -m factlog ignore "drafts/*.md" --target "$KB" >/dev/null
active | grep -qF 'drafts/*.md' && ok "ignore: pattern added to policy file" || bad "pattern not written"
"$PYTHON" -m factlog ignore "drafts/*.md" --target "$KB" 2>&1 | grep -qF "already present" && ok "ignore: duplicate add is a no-op" || bad "duplicate not detected"
"$PYTHON" -m factlog ignore --target "$KB" 2>&1 | grep -qF "sources/drafts/wip.md" && ok "ignore (list): shows matching on-disk source" || bad "list did not show match"
"$PYTHON" -m factlog ignore --remove "drafts/*.md" --target "$KB" >/dev/null
active | grep -qF 'drafts/*.md' && bad "pattern not removed" || ok "ignore --remove deletes the pattern"
set +e; "$PYTHON" -m factlog ignore --remove --target "$KB" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 2 ] && ok "ignore --remove with no pattern errors (rc 2)" || bad "--remove without pattern should error"
# --remove against an absent policy file must not create an empty file
rm -f "$KB/policy/sync-ignore.md"
"$PYTHON" -m factlog ignore --remove "anything" --target "$KB" >/dev/null 2>&1
[ ! -f "$KB/policy/sync-ignore.md" ] && ok "ignore --remove on absent file creates nothing" || bad "empty policy file materialized"

# --- ingest --scan skips an ignored binary -----------------------------------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'PK\003\004\000' > "$KB/sources/skip.bin"; printf 'PK\003\004\000' > "$KB/sources/take.bin"
"$PYTHON" -m factlog ignore "skip.bin" --target "$KB" >/dev/null
out="$("$PYTHON" -m factlog ingest --scan --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "skipped 1 sync-ignored" && ok "ingest --scan skips the ignored binary" || bad "ignored binary not skipped: $out"

# --- sources shows [ignored] --------------------------------------------------
"$PYTHON" -m factlog sources --target "$KB" 2>&1 | grep -qF "sources/skip.bin" \
  && "$PYTHON" -m factlog sources --target "$KB" 2>&1 | grep -F "skip.bin" | grep -qF "ignored" \
  && ok "sources marks the ignored source [ignored]" || bad "sources did not mark ignored"

# --- coverage: excluded, not a gap; --strict tolerant -------------------------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'wip\n' > "$KB/sources/wip.md"
printf -- '- wip.md\n' >> "$KB/policy/sync-ignore.md"
printf '[{"subject":"W","relation":"rel","object":"V","source":"sources/wip.md","status":"confirmed","confidence":0.9,"note":""}]\n' > "$KB/runs/wip.json"
"$PYTHON" tools/merge_candidates.py --wiki "$KB" >/dev/null 2>&1
out="$("$PYTHON" tools/source_coverage.py --wiki "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "1 excluded (sync-ignored)" && ok "coverage: ignored source counted as excluded" || bad "coverage excluded count wrong: $out"
printf '%s' "$out" | grep -F "wip.md" | grep -qF "[excluded]" && ok "coverage: row tagged [excluded]" || bad "coverage row not tagged"
printf '%s' "$out" | grep -qiE "GAP .*wip.md" && bad "ignored source wrongly flagged as a gap" || ok "ignored source not flagged as a gap"
set +e; "$PYTHON" tools/source_coverage.py --wiki "$KB" --strict >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 0 ] && ok "coverage --strict does not fail on an ignored text source" || bad "--strict failed on ignored source"

# --- an ignored source's existing facts are retained across re-merge -----------
grep -q "W,rel,V,sources/wip.md,confirmed" "$KB/facts/candidates.csv" && ok "ignored source's facts retained after merge" || bad "ignored source's facts lost"

echo ""
echo "========================================"
echo "test_sync_ignore: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
