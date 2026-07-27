#!/usr/bin/env bash
# Behavioral matrix for hooks/gate_check.sh
#
# Each case exercises a distinct branch of the deny predicate.
# Exit code 2 = DENY (expected for stale/absent cases).
# Exit code 0 = ALLOW (expected when report is fresh or target is not an engine input).
#
# Usage: bash tests/test_gate_check.sh
#   Returns 0 if all cases pass, 1 if any fail.

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

GATE="$(cd "$(dirname "$0")/.." && pwd)/hooks/gate_check.sh"
PYTHON_RUNNER="$(cd "$(dirname "$0")/.." && pwd)/tools/factlog_python.sh"

pass=0
fail=0

# ---------------------------------------------------------------------------
# Payload builders.
#
# envelope() is the shape Claude Code ACTUALLY sends a PreToolUse hook: the tool
# input wrapped in an envelope, target at tool_input.file_path. Until #591 every
# fixture in this file used legacy_payload() below instead, so all 29 cases
# passed against a gate that could not read a single real invocation and fell
# through to fail-open. Default new cases to envelope(); reach for
# legacy_payload() only to pin the compatibility fallback deliberately.
# ---------------------------------------------------------------------------
envelope() {
  # envelope <file_path> [tool_name]
  printf '{"session_id":"s0","cwd":"/","hook_event_name":"PreToolUse","tool_name":"%s","tool_input":{"file_path":"%s","content":"x"},"tool_use_id":"u0"}' \
    "${2:-Write}" "$1"
}

legacy_payload() {
  # LEGACY/COMPAT ONLY — a bare tool input with the path at the TOP level. Not
  # what Claude Code sends; kept working for callers that pipe a bare tool input
  # (older harnesses, hand-run `echo '{"file_path":...}' | gate_check.sh`).
  printf '{"file_path":"%s"}' "$1"
}

# ---------------------------------------------------------------------------
# Helper: run gate for a given KB root, target file_path, and expected exit.
# Uses the REAL envelope shape.
# ---------------------------------------------------------------------------
run_payload_case() {
  local desc="$1" kb_root="$2" payload="$3" expected_exit="$4"

  local actual_exit=0
  FACTLOG_ROOT="$kb_root" bash "$GATE" <<< "$payload" >/dev/null 2>&1 || actual_exit=$?

  if [ "$actual_exit" -eq "$expected_exit" ]; then
    echo "PASS: $desc (exit $actual_exit)"
    pass=$((pass + 1))
  else
    echo "FAIL: $desc — expected exit $expected_exit, got $actual_exit"
    fail=$((fail + 1))
  fi
}

run_case() {
  local desc="$1" kb_root="$2" target_path="$3" expected_exit="$4"
  run_payload_case "$desc" "$kb_root" "$(envelope "$target_path")" "$expected_exit"
}

# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------
make_kb() {
  # Create a minimal KB skeleton at the given path.
  local root="$1"
  mkdir -p "$root/facts"
}

touch_file() {
  local path="$1"
  touch "$path"
}

set_mtime_past() {
  # Set file mtime to 1 second in the past relative to now.
  # Uses touch -t on macOS/BSD (YYYYMMDDHHMMSS).
  local path="$1"
  local past
  past="$(bash "$PYTHON_RUNNER" -c 'import time,datetime; t=time.time()-2; print(datetime.datetime.fromtimestamp(t).strftime("%Y%m%d%H%M.%S"))')"
  touch -t "$past" "$path"
}

set_mtime_future() {
  # Set file mtime to 2 seconds in the future.
  local path="$1"
  local future
  future="$(bash "$PYTHON_RUNNER" -c 'import time,datetime; t=time.time()+2; print(datetime.datetime.fromtimestamp(t).strftime("%Y%m%d%H%M.%S"))')"
  touch -t "$future" "$path"
}

# ---------------------------------------------------------------------------
# CASE 1: target is not an engine input — always ALLOW
# ---------------------------------------------------------------------------
KB1="$(mktemp -d)"
make_kb "$KB1"
run_case "non-engine-input target — allow" \
  "$KB1" "$KB1/facts/candidates.csv" 0
rm -rf "$KB1"

# ---------------------------------------------------------------------------
# CASE 2: engine input, report absent — DENY
# ---------------------------------------------------------------------------
KB2="$(mktemp -d)"
make_kb "$KB2"
touch_file "$KB2/facts/accepted.dl"
# No logic_report.txt
run_case "engine input, report absent — deny" \
  "$KB2" "$KB2/facts/accepted.dl" 2
rm -rf "$KB2"

# ---------------------------------------------------------------------------
# CASE 3: engine input, report is fresh (newer than accepted.dl) — ALLOW
# ---------------------------------------------------------------------------
KB3="$(mktemp -d)"
make_kb "$KB3"
touch_file "$KB3/facts/accepted.dl"
set_mtime_past "$KB3/facts/accepted.dl"
touch_file "$KB3/facts/logic_report.txt"
# logic_report.txt gets current mtime → newer than accepted.dl
run_case "engine input, report fresh — allow" \
  "$KB3" "$KB3/facts/accepted.dl" 0
rm -rf "$KB3"

# ---------------------------------------------------------------------------
# CASE 4: engine input, report is stale (older than accepted.dl) — DENY
# ---------------------------------------------------------------------------
KB4="$(mktemp -d)"
make_kb "$KB4"
touch_file "$KB4/facts/logic_report.txt"
set_mtime_past "$KB4/facts/logic_report.txt"
touch_file "$KB4/facts/accepted.dl"
# accepted.dl gets current mtime → newer than report
run_case "engine input, report stale — deny" \
  "$KB4" "$KB4/facts/accepted.dl" 2
rm -rf "$KB4"

# ---------------------------------------------------------------------------
# CASE 5: query.dl target, report fresh — ALLOW
# ---------------------------------------------------------------------------
KB5="$(mktemp -d)"
make_kb "$KB5"
touch_file "$KB5/facts/query.dl"
set_mtime_past "$KB5/facts/query.dl"
touch_file "$KB5/facts/logic_report.txt"
run_case "query.dl target, report fresh — allow" \
  "$KB5" "$KB5/facts/query.dl" 0
rm -rf "$KB5"

# ---------------------------------------------------------------------------
# CASE 6: query.dl target, report stale — DENY
# ---------------------------------------------------------------------------
KB6="$(mktemp -d)"
make_kb "$KB6"
touch_file "$KB6/facts/logic_report.txt"
set_mtime_past "$KB6/facts/logic_report.txt"
touch_file "$KB6/facts/query.dl"
run_case "query.dl target, report stale — deny" \
  "$KB6" "$KB6/facts/query.dl" 2
rm -rf "$KB6"

# ---------------------------------------------------------------------------
# CASE 7: BOOTSTRAP — fresh KB (no logic_report.txt, query.dl does not yet
# exist) creating facts/query.dl — ALLOW.
#
# A `factlog init` KB seeds neither facts/logic_report.txt nor facts/query.dl,
# so the FIRST creation of query.dl cannot be preceded by a report. Denying it
# would deadlock the question->query-draft flow. The gate must allow it.
# ---------------------------------------------------------------------------
KB_BOOT="$(mktemp -d)"
make_kb "$KB_BOOT"
# No logic_report.txt, no query.dl on disk.
run_case "bootstrap: fresh KB creating query.dl — allow" \
  "$KB_BOOT" "$KB_BOOT/facts/query.dl" 0
rm -rf "$KB_BOOT"

# ---------------------------------------------------------------------------
# CASE 8: BOOTSTRAP companion — fresh KB creating facts/accepted.dl — ALLOW.
# ---------------------------------------------------------------------------
KB_BOOT2="$(mktemp -d)"
make_kb "$KB_BOOT2"
run_case "bootstrap: fresh KB creating accepted.dl — allow" \
  "$KB_BOOT2" "$KB_BOOT2/facts/accepted.dl" 0
rm -rf "$KB_BOOT2"

# ---------------------------------------------------------------------------
# CASE 9: STALE-GUARD vs bootstrap — query.dl already exists but no report
# (e.g. report was deleted) — DENY.
#
# This proves the bootstrap relaxation does NOT swallow the stale-guard: once
# an engine input exists on disk without a superseding report, the edit is
# denied. Only the genuine first-write (target absent) is allowed.
# ---------------------------------------------------------------------------
KB_STALE="$(mktemp -d)"
make_kb "$KB_STALE"
touch_file "$KB_STALE/facts/query.dl"
# query.dl now exists; no logic_report.txt → must DENY (not bootstrap).
run_case "existing query.dl, report absent — deny (stale-guard, not bootstrap)" \
  "$KB_STALE" "$KB_STALE/facts/query.dl" 2
# Branch B tests the *target* file's existence, not the KB's overall state: in
# this very same KB (query.dl present, report absent) a write creating the
# not-yet-existing accepted.dl is still bootstrap and is ALLOWED. This pins the
# per-target reading that docs/guide/determinism.{md,en.md} documents, and
# together with the DENY above proves the two verdicts differ by target alone.
run_case "same KB, absent accepted.dl — allow (bootstrap is per target file)" \
  "$KB_STALE" "$KB_STALE/facts/accepted.dl" 0
rm -rf "$KB_STALE"

# ---------------------------------------------------------------------------
# CASE 10: REGRESSION — single-quote in KB root path, report stale — DENY
#
# This is the apostrophe-path regression added after the critic identified
# that the original mtime computation used Python source string interpolation
# (`'$f'` and `'$report'`), which broke with paths containing a single quote,
# causing the gate to fail open (allow) instead of denying.  The fix uses
# `sys.argv[1]` to pass the path as a shell argument, which is quote-safe.
# ---------------------------------------------------------------------------
TMPBASE="$(mktemp -d)"
KB7="${TMPBASE}/kb-test's-apostrophe"
mkdir -p "$KB7/facts"
touch_file "$KB7/facts/logic_report.txt"
set_mtime_past "$KB7/facts/logic_report.txt"
touch_file "$KB7/facts/accepted.dl"
# accepted.dl gets current mtime → report is stale → must DENY
run_case "single-quote in KB root, report stale — deny (apostrophe regression)" \
  "$KB7" "$KB7/facts/accepted.dl" 2
rm -rf "$TMPBASE"

# ---------------------------------------------------------------------------
# CASE 11: FAIL-CLOSED INVARIANT — Python unavailable on a Write to an engine
# input — DENY (exit 2), not allow.
#
# u16 removed the dead `command -v python3` guards that sat *below* the mtime
# probes, justified by the assertion that a fail-closed `exit 2` near the TOP of
# gate_check.sh guarantees a usable Python is present before any probe runs. This
# case pins that invariant behaviorally: if Python cannot be found, the gate must
# DENY before it ever reaches a probe — so a future edit that reorders the
# top-of-file fail-closed check below the probes is caught here.
#
# Hermetic simulation: run with an empty throwaway PATH, deliberately omitting
# python3/python/py. The test does not depend on the host Python location.
# ---------------------------------------------------------------------------
KB_NOPY="$(mktemp -d)"
make_kb "$KB_NOPY"
touch_file "$KB_NOPY/facts/accepted.dl"  # existing engine input (not bootstrap)

SHIM_PATH="$(mktemp -d)"
BASH_BIN="${BASH:-$(command -v bash)}"

nopy_exit=0
PATH="$SHIM_PATH" FACTLOG_ROOT="$KB_NOPY" \
  "$BASH_BIN" "$GATE" <<< "$(envelope "$KB_NOPY/facts/accepted.dl")" \
  >/dev/null 2>&1 || nopy_exit=$?
if [ "$nopy_exit" -eq 2 ]; then
  echo "PASS: python3 unavailable on engine-input write — fail-closed deny (exit $nopy_exit)"
  pass=$((pass + 1))
else
  echo "FAIL: python3 unavailable — expected fail-closed exit 2, got $nopy_exit"
  fail=$((fail + 1))
fi
rm -rf "$KB_NOPY" "$SHIM_PATH"

# ---------------------------------------------------------------------------
# CASE 12: WINDOWS STORE STUB REGRESSION — python3 exists but cannot execute.
#
# Windows can put a Microsoft Store python3.exe stub on PATH. The command exists,
# but `python3 -c ...` fails. The gate must skip that stub and use python/py or
# FACTLOG_PYTHON instead of failing open during JSON/path parsing.
# ---------------------------------------------------------------------------
KB_STUB="$(mktemp -d)"
make_kb "$KB_STUB"
touch_file "$KB_STUB/facts/accepted.dl"
set_mtime_past "$KB_STUB/facts/accepted.dl"
touch_file "$KB_STUB/facts/logic_report.txt"

STUB_PATH="$(mktemp -d)"
cat > "$STUB_PATH/python3" <<'SH'
#!/usr/bin/env sh
echo Python
exit 1
SH
chmod +x "$STUB_PATH/python3"

stub_exit=0
PATH="$STUB_PATH:$PATH" FACTLOG_ROOT="$KB_STUB" \
  bash "$GATE" <<< "$(envelope "$KB_STUB/facts/accepted.dl")" \
  >/dev/null 2>&1 || stub_exit=$?
if [ "$stub_exit" -eq 0 ]; then
  echo "PASS: broken python3 stub skipped when another Python is usable (exit $stub_exit)"
  pass=$((pass + 1))
else
  echo "FAIL: broken python3 stub — expected exit 0 via fallback Python, got $stub_exit"
  fail=$((fail + 1))
fi
rm -rf "$KB_STUB" "$STUB_PATH"

# ---------------------------------------------------------------------------
# Config helpers for the active-KB resolution cases (#239).
# XDG_CONFIG_HOME is already isolated to a throwaway dir at the top of this file,
# so writing/clearing config.json here never touches the dev machine's config.
# ---------------------------------------------------------------------------
set_config_root() {
  # Record an active-KB root in the isolated config, mirroring `factlog use`.
  local root="$1"
  local cfg_dir="$XDG_CONFIG_HOME/factlog"
  mkdir -p "$cfg_dir"
  printf '{"root": "%s"}\n' "$root" > "$cfg_dir/config.json"
}

clear_config() {
  rm -f "$XDG_CONFIG_HOME/factlog/config.json"
}

# ---------------------------------------------------------------------------
# CASE 13: FACTLOG_ROOT UNSET + active-KB config set — gate resolves KB_ROOT
# from config (not cwd). Engine input exists, no report → DENY.
#
# Reverting the config-aware resolver makes KB_ROOT fall back to cwd ("."), whose
# facts/accepted.dl does not match the config KB's absolute target, so the gate
# would treat the target as a non-engine-input and ALLOW (exit 0). This case pins
# that the gate guards the SAME KB the tools write to (issue #239).
# ---------------------------------------------------------------------------
KB_CFG="$(mktemp -d)"
make_kb "$KB_CFG"
touch_file "$KB_CFG/facts/accepted.dl"   # existing engine input, no report → must DENY
set_config_root "$KB_CFG"

cfg_exit=0
env -u FACTLOG_ROOT bash "$GATE" <<< "$(envelope "$KB_CFG/facts/accepted.dl")" \
  >/dev/null 2>&1 || cfg_exit=$?
if [ "$cfg_exit" -eq 2 ]; then
  echo "PASS: FACTLOG_ROOT unset, config KB used as KB_ROOT — deny (exit $cfg_exit)"
  pass=$((pass + 1))
else
  echo "FAIL: FACTLOG_ROOT unset, config KB — expected config-resolved deny (exit 2), got $cfg_exit"
  fail=$((fail + 1))
fi
clear_config
rm -rf "$KB_CFG"

# ---------------------------------------------------------------------------
# CASE 14: FACTLOG_ROOT set + a DIFFERENT active-KB config — env wins.
# DISCRIMINATING form (mutation-killing): env → a FRESH KB, config → a STALE KB,
# and the TARGET is the STALE config KB's engine input.
#
# resolve_root precedence is FACTLOG_ROOT > config > cwd. With env winning,
# KB_ROOT is the FRESH env KB, so the stale config KB's file is NOT an engine
# input under KB_ROOT → ALLOW (exit 0). This is exactly the scope in the hook
# comment: the gate guards the ACTIVE KB (env here), and a write to a NON-active
# KB is not the gate's target.
#
# Why this pins env > config: if config wrongly won, KB_ROOT would be the STALE
# config KB, the target WOULD match, the stale-report guard would fire, and the
# gate would DENY (exit 2) → this case FAILS. A resolver that ignored env (or
# ranked config above env) is therefore killed, whereas the previous CASE 14
# (target under the env KB, expect DENY) passed even if config had been ignored.
# ---------------------------------------------------------------------------
KB_ENV_FRESH="$(mktemp -d)"
KB_CFG_STALE="$(mktemp -d)"
make_kb "$KB_ENV_FRESH"
make_kb "$KB_CFG_STALE"
# ENV KB (active): fresh report → would ALLOW if this KB were guarded.
touch_file "$KB_ENV_FRESH/facts/accepted.dl"
set_mtime_past "$KB_ENV_FRESH/facts/accepted.dl"
touch_file "$KB_ENV_FRESH/facts/logic_report.txt"
# CONFIG KB (non-active): STALE (accepted.dl newer than report) → would DENY if
# this KB were (wrongly) guarded.
touch_file "$KB_CFG_STALE/facts/logic_report.txt"
set_mtime_past "$KB_CFG_STALE/facts/logic_report.txt"
touch_file "$KB_CFG_STALE/facts/accepted.dl"
set_config_root "$KB_CFG_STALE"

env_exit=0
FACTLOG_ROOT="$KB_ENV_FRESH" bash "$GATE" <<< "$(envelope "$KB_CFG_STALE/facts/accepted.dl")" \
  >/dev/null 2>&1 || env_exit=$?
if [ "$env_exit" -eq 0 ]; then
  echo "PASS: FACTLOG_ROOT (active) overrides config — write to non-active stale config KB allowed (exit $env_exit)"
  pass=$((pass + 1))
else
  echo "FAIL: env>config not honoured — expected allow (exit 0) for non-active config KB, got $env_exit"
  fail=$((fail + 1))
fi
clear_config
rm -rf "$KB_ENV_FRESH" "$KB_CFG_STALE"

# ---------------------------------------------------------------------------
# CASE 15: neither FACTLOG_ROOT nor config set — cwd fallback preserved.
#
# The first-user path: with no active KB, KB_ROOT must fall back to cwd. Run the
# gate from within a KB dir so cwd IS the KB; an existing engine input with no
# report → DENY, proving cwd resolution still matches engine inputs.
# ---------------------------------------------------------------------------
KB_CWD="$(mktemp -d)"
make_kb "$KB_CWD"
touch_file "$KB_CWD/facts/accepted.dl"   # existing engine input, no report → DENY
clear_config

cwd_exit=0
( cd "$KB_CWD" && env -u FACTLOG_ROOT bash "$GATE" <<< "$(envelope "facts/accepted.dl")" ) \
  >/dev/null 2>&1 || cwd_exit=$?
if [ "$cwd_exit" -eq 2 ]; then
  echo "PASS: no FACTLOG_ROOT, no config — cwd fallback used as KB_ROOT — deny (exit $cwd_exit)"
  pass=$((pass + 1))
else
  echo "FAIL: cwd fallback — expected deny (exit 2), got $cwd_exit"
  fail=$((fail + 1))
fi
rm -rf "$KB_CWD"

# ---------------------------------------------------------------------------
# CASE 16: OBSERVABILITY (#244) — config resolver returns empty because the
# factlog package fails to import, while Python IS available. The gate must emit
# a one-line stderr note AND still degrade to the prior ${FACTLOG_ROOT:-cwd}
# behaviour (no exit-code / path-matching regression).
#
# resolve_root(None) always yields a non-empty path (cwd fallback), so the only
# way resolved_root goes empty is the factlog import failing in the child. We
# reproduce that deterministically WITHOUT breaking the real install: a fake
# plugin root whose factlog/__init__.py raises ImportError. The gate inserts that
# fake root at sys.path[0] (FACTLOG_HOOK_PLUGIN_ROOT="$HOOK_DIR/.."), so the
# broken package shadows the installed one for the resolver invocation only. The
# python-availability check (import sys) is unaffected, so we are NOT in the
# fail-closed window. FACTLOG_PYTHON_RUNNER points at the real runner so a usable
# Python is still found.
#
# Reverting the warning (removing the else branch) makes this case FAIL: the
# stderr note disappears while the degrade stays silent.
# ---------------------------------------------------------------------------
FAKE_PLUGIN="$(mktemp -d)"
mkdir -p "$FAKE_PLUGIN/hooks" "$FAKE_PLUGIN/factlog"
printf 'raise ImportError("factlog package intentionally broken for gate observability test (#244)")\n' \
  > "$FAKE_PLUGIN/factlog/__init__.py"
cp "$GATE" "$FAKE_PLUGIN/hooks/gate_check.sh"

# KB with an existing engine input and NO report → the prior behaviour, once
# KB_ROOT degrades to $FACTLOG_ROOT, is DENY (exit 2). This proves the fallback
# KB_ROOT still resolves the engine input exactly as before the resolver ran.
KB_OBS="$(mktemp -d)"
make_kb "$KB_OBS"
touch_file "$KB_OBS/facts/accepted.dl"   # existing engine input, no report → deny
clear_config

obs_err="$(mktemp)"
obs_exit=0
FACTLOG_PYTHON_RUNNER="$PYTHON_RUNNER" FACTLOG_ROOT="$KB_OBS" \
  bash "$FAKE_PLUGIN/hooks/gate_check.sh" \
  <<< "$(envelope "$KB_OBS/facts/accepted.dl")" \
  >/dev/null 2>"$obs_err" || obs_exit=$?

if [ "$obs_exit" -eq 2 ]; then
  echo "PASS: resolver import-failure — KB_ROOT degrades to \$FACTLOG_ROOT, deny preserved (exit $obs_exit)"
  pass=$((pass + 1))
else
  echo "FAIL: resolver import-failure — expected prior-behaviour deny (exit 2), got $obs_exit"
  fail=$((fail + 1))
fi

if grep -qF "factlog config resolver unavailable" "$obs_err"; then
  echo "PASS: resolver import-failure emits one-line stderr observability note"
  pass=$((pass + 1))
else
  echo "FAIL: resolver import-failure — expected stderr note 'factlog config resolver unavailable', got: $(cat "$obs_err")"
  fail=$((fail + 1))
fi
rm -rf "$FAKE_PLUGIN" "$KB_OBS" "$obs_err"

# ---------------------------------------------------------------------------
# CASE 17: OBSERVABILITY companion — when the resolver IS healthy, NO warning is
# emitted (the note must not become noise on the happy path). Uses the REAL gate
# so factlog imports fine; a fresh-report allow case keeps exit 0.
# ---------------------------------------------------------------------------
KB_OK="$(mktemp -d)"
make_kb "$KB_OK"
touch_file "$KB_OK/facts/accepted.dl"
set_mtime_past "$KB_OK/facts/accepted.dl"
touch_file "$KB_OK/facts/logic_report.txt"   # fresh report → allow
clear_config

ok_err="$(mktemp)"
ok_exit=0
FACTLOG_ROOT="$KB_OK" bash "$GATE" \
  <<< "$(envelope "$KB_OK/facts/accepted.dl")" \
  >/dev/null 2>"$ok_err" || ok_exit=$?

if [ "$ok_exit" -eq 0 ] && ! grep -qF "factlog config resolver unavailable" "$ok_err"; then
  echo "PASS: healthy resolver — allow preserved and NO observability note emitted (exit $ok_exit)"
  pass=$((pass + 1))
else
  echo "FAIL: healthy resolver — expected allow (exit 0) with no note; exit=$ok_exit stderr=$(cat "$ok_err")"
  fail=$((fail + 1))
fi
rm -rf "$KB_OK" "$ok_err"

# ---------------------------------------------------------------------------
# CASE 18: the runner's off-PATH selection notice reaches the hook's stderr.
#
# The runner discloses on stderr when it execs an interpreter from outside PATH
# (#578). Every call site here used to discard stderr, making that disclosure zero
# per gate evaluation — a silent selection by another route. A stub runner stands
# in for the real one so the case cannot depend on the developer's own PATH.
# ---------------------------------------------------------------------------
KB_NOTE="$(mktemp -d)"
make_kb "$KB_NOTE"
clear_config

NOTE_RUNNER="$(mktemp)"
cat > "$NOTE_RUNNER" <<'RUNNER'
#!/usr/bin/env bash
echo "[factlog] using /somewhere/off-path/python (test stub)" >&2
exec python3 "$@"
RUNNER

note_err="$(mktemp)"
note_exit=0
FACTLOG_ROOT="$KB_NOTE" FACTLOG_PYTHON_RUNNER="$NOTE_RUNNER" bash "$GATE" \
  <<< "$(envelope "$KB_NOTE/facts/accepted.dl")" \
  >/dev/null 2>"$note_err" || note_exit=$?

if grep -qF "[factlog] using /somewhere/off-path/python" "$note_err"; then
  echo "PASS: runner's off-PATH selection notice survives to the hook's stderr"
  pass=$((pass + 1))
else
  echo "FAIL: runner's off-PATH notice was swallowed; exit=$note_exit stderr=$(cat "$note_err")"
  fail=$((fail + 1))
fi

# Once per evaluation, not once per runner exec, or it drowns the deny reason.
note_count="$(grep -cF "[factlog] using /somewhere/off-path/python" "$note_err" || true)"
if [ "$note_count" -eq 1 ]; then
  echo "PASS: the notice appears exactly once per gate evaluation"
  pass=$((pass + 1))
else
  echo "FAIL: expected the notice once per evaluation, got $note_count"
  fail=$((fail + 1))
fi
rm -rf "$KB_NOTE" "$NOTE_RUNNER" "$note_err"

# ---------------------------------------------------------------------------
# CASE 19: how many times ONE gate evaluation execs the runner.
#
# The number carries two arguments — "one probe answers both questions, so the
# dependency check adds no spawn" in tools/factlog_python.sh, and the timing that
# justified removing the resolution cache. It was written as a constant and
# drifted into three mutually inconsistent values, because it is not a constant:
# given a usable Python 3.11+ it is 3, 5, or 6, decided by the target alone —
# and WITHOUT one it is 1 for every target, because the fail-closed probe below
# is that single exec. This case fixes the whole table so the next reader
# measures nothing.
#
# A counting wrapper stands in for the runner via FACTLOG_PYTHON_RUNNER — the
# same seam CASE 16/18 use — so the count cannot depend on the developer's PATH.
# ---------------------------------------------------------------------------
COUNT_DIR="$(mktemp -d)"
COUNT_RUNNER="$COUNT_DIR/runner.sh"
cat > "$COUNT_RUNNER" <<'RUNNER'
#!/usr/bin/env bash
printf 'x\n' >> "$GATE_RUNNER_CALLS"
exec "${BASH:-bash}" "$GATE_REAL_RUNNER" "$@"
RUNNER

# A runner that finds no usable Python 3.11+ — exit 127 is the launcher's own
# contract for that state, so the gate sees exactly what a Store-stub-only
# Windows box gives it.
DEAD_RUNNER="$COUNT_DIR/dead-runner.sh"
cat > "$DEAD_RUNNER" <<'RUNNER'
#!/usr/bin/env bash
echo "[factlog] no usable Python 3.11+ found. Set FACTLOG_PYTHON to a venv/system python." >&2
exit 127
RUNNER

# count_execs <desc> <expected-execs> <kb-root> <payload> [expected-exit] [inner-runner]
count_execs() {
  local desc="$1" expected="$2" kb_root="$3" payload="$4"
  local expected_exit="${5:-}" inner="${6:-$PYTHON_RUNNER}" actual rc=0
  : > "$COUNT_DIR/calls"
  GATE_RUNNER_CALLS="$COUNT_DIR/calls" GATE_REAL_RUNNER="$inner" \
    FACTLOG_PYTHON_RUNNER="$COUNT_RUNNER" FACTLOG_ROOT="$kb_root" \
    bash "$GATE" <<< "$payload" >/dev/null 2>&1 || rc=$?
  actual="$(wc -l < "$COUNT_DIR/calls" | tr -d ' ')"
  if [ "$actual" != "$expected" ]; then
    echo "FAIL: $desc — expected $expected runner execs, got $actual"
    fail=$((fail + 1))
    return
  fi
  if [ -n "$expected_exit" ] && [ "$rc" != "$expected_exit" ]; then
    echo "FAIL: $desc — $actual runner execs but exit $rc, expected $expected_exit"
    fail=$((fail + 1))
    return
  fi
  echo "PASS: $desc ($actual runner execs)"
  pass=$((pass + 1))
}

# Populated KB: both engine inputs on disk, report fresh, so every verdict below
# is allow.
KB_EXEC="$(mktemp -d)"
make_kb "$KB_EXEC"
touch_file "$KB_EXEC/facts/accepted.dl"
touch_file "$KB_EXEC/facts/query.dl"
set_mtime_past "$KB_EXEC/facts/accepted.dl"
set_mtime_past "$KB_EXEC/facts/query.dl"
touch_file "$KB_EXEC/facts/logic_report.txt"
clear_config

count_execs "no file_path in payload — gate exits before canonicalising" \
  3 "$KB_EXEC" '{"tool":"Write"}'
count_execs "unparseable payload — same early exit" \
  3 "$KB_EXEC" 'not json at all'
count_execs "accepted.dl — engine-input loop breaks on its first entry" \
  5 "$KB_EXEC" "$(envelope "$KB_EXEC/facts/accepted.dl")"
count_execs "query.dl — loop reaches its second entry" \
  6 "$KB_EXEC" "$(envelope "$KB_EXEC/facts/query.dl")"
count_execs "non-engine target — loop runs both entries, matches neither" \
  6 "$KB_EXEC" "$(envelope "$KB_EXEC/facts/candidates.csv")"

# The same targets in a bare KB: no report and no engine inputs, so the verdicts
# flip to bootstrap-allow and deny. The counts must NOT move. That is the half of
# the claim which says KB state is not a determinant — and it is exactly the
# assumption the drifted comments got wrong.
KB_BARE="$(mktemp -d)"
make_kb "$KB_BARE"
clear_config

count_execs "bare KB, accepted.dl — count unchanged by KB state" \
  5 "$KB_BARE" "$(envelope "$KB_BARE/facts/accepted.dl")"
count_execs "bare KB, query.dl — count unchanged by KB state" \
  6 "$KB_BARE" "$(envelope "$KB_BARE/facts/query.dl")"
count_execs "bare KB, non-engine target — count unchanged by KB state" \
  6 "$KB_BARE" "$(envelope "$KB_BARE/notes.md")"

# The one row the ENVIRONMENT decides, not the target. With no usable Python
# 3.11+ the fail-closed probe is the first exec and DENIES on the spot, so every
# target above collapses to 1 — including the two that read 3, which no reading
# of "the target decides" would predict. The exit code is asserted alongside the
# count: 1 exec on its own could equally mean the gate gave up and allowed, which
# is the opposite of what this branch must do.
count_execs "no usable Python — accepted.dl collapses to the fail-closed probe" \
  1 "$KB_EXEC" "$(envelope "$KB_EXEC/facts/accepted.dl")" 2 "$DEAD_RUNNER"
count_execs "no usable Python — query.dl collapses too" \
  1 "$KB_EXEC" "$(envelope "$KB_EXEC/facts/query.dl")" 2 "$DEAD_RUNNER"
count_execs "no usable Python — a non-engine target denies as well" \
  1 "$KB_EXEC" "$(envelope "$KB_EXEC/facts/candidates.csv")" 2 "$DEAD_RUNNER"
count_execs "no usable Python — even a pathless payload never reaches 3" \
  1 "$KB_EXEC" '{"tool":"Write"}' 2 "$DEAD_RUNNER"

# An engine-input-shaped payload whose path cannot be extracted skips
# canonicalisation and the engine-input loop entirely, so it sits at 3 like the
# pathless rows above — yet it is NOT the same early exit: the pathless rows had
# nothing to judge, this one goes on to the predicate. Both KBs are shown so the
# count is seen not to move while the verdict does (bare KB → deny, fresh-report
# KB → allow), which is the pair the pathless rows cannot produce at all.
count_execs "unreadable shape naming an engine input, bare KB — 3 execs, denies" \
  3 "$KB_BARE" "$(printf '{"tool_name":"Write","tool_input":{"target":"%s"}}' "$KB_BARE/facts/accepted.dl")" 2
count_execs "same shape, fresh report — 3 execs, allows" \
  3 "$KB_EXEC" "$(printf '{"tool_name":"Write","tool_input":{"target":"%s"}}' "$KB_EXEC/facts/accepted.dl")" 0

rm -rf "$COUNT_DIR" "$KB_EXEC" "$KB_BARE"

# ---------------------------------------------------------------------------
# CASE 20: PAYLOAD SHAPE (#591) — the gate must read the envelope Claude Code
# actually sends.
#
# Every case above this one used to feed a bare top-level {"file_path":...}. The
# real PreToolUse payload nests the target under tool_input, so the gate found no
# path, took the fail-open branch, and allowed EVERY engine-input write for its
# entire existence while 29 green cases said otherwise. This block pins the shape
# contract itself rather than trusting the shared helper to stay correct.
#
# The KB: an engine input on disk with no report → the correct verdict for
# anything that resolves to it is DENY (2). So exit 0 here means "the gate did
# not see the target", which is precisely the defect.
# ---------------------------------------------------------------------------
KB_SHAPE="$(mktemp -d)"
make_kb "$KB_SHAPE"
touch_file "$KB_SHAPE/facts/accepted.dl"
touch_file "$KB_SHAPE/facts/query.dl"
clear_config

# The real thing, stated as the headline of the contract.
#
# These two rows are DOUBLE-DEFENCE rows, not discriminating ones: revert the
# extraction to top-level-only and they still PASS, because the raw-text probe
# below catches the same payload and denies it for the other reason. Only
# breaking BOTH the nested read and the probe turns them red. That is a real
# property worth pinning — the two layers back each other — but it is not what a
# reader assumes from "REAL envelope … deny", so it is written down here. The
# discriminating rows for the nested read are the bootstrap pair right below,
# where the probe cannot stand in.
run_payload_case "REAL envelope, Write — deny" \
  "$KB_SHAPE" "$(envelope "$KB_SHAPE/facts/accepted.dl" Write)" 2

# Edit carries the target under the SAME key as Write — tool_input.file_path —
# with old_string/new_string in place of content. Written out literally rather
# than via envelope() so the claim is pinned by a fixture, not by a helper that
# happens to be shared.
run_payload_case "REAL envelope, Edit — same tool_input.file_path key — deny" \
  "$KB_SHAPE" \
  "$(printf '{"session_id":"s0","cwd":"/","hook_event_name":"PreToolUse","tool_name":"Edit","tool_input":{"file_path":"%s","old_string":"a","new_string":"b"},"tool_use_id":"u0"}' \
     "$KB_SHAPE/facts/query.dl")" \
  2

# The discriminating pair: the same real envelope against a BOOTSTRAP KB.
#
# Bootstrap needs a RESOLVED target — it asks whether that one named file exists
# on disk — so the raw-text probe cannot produce this verdict. Read the nested
# key and the answer is ALLOW; miss it and the payload falls to the probe, which
# skips bootstrap and DENIES. So an implementation that stops reading
# tool_input.file_path flips these two rows on its own, with no help from any
# other case in this file. Write and Edit are covered separately: nothing
# guarantees a future extractor treats the two tools' payloads alike.
KB_SHAPE_BOOT2="$(mktemp -d)"
make_kb "$KB_SHAPE_BOOT2"
run_payload_case "REAL envelope, Write, bootstrap KB — allow (nested read, probe cannot fake it)" \
  "$KB_SHAPE_BOOT2" "$(envelope "$KB_SHAPE_BOOT2/facts/query.dl" Write)" 0
run_payload_case "REAL envelope, Edit, bootstrap KB — allow (Edit's key genuinely read)" \
  "$KB_SHAPE_BOOT2" \
  "$(printf '{"session_id":"s0","cwd":"/","hook_event_name":"PreToolUse","tool_name":"Edit","tool_input":{"file_path":"%s","old_string":"a","new_string":"b"},"tool_use_id":"u0"}' \
     "$KB_SHAPE_BOOT2/facts/accepted.dl")" \
  0
rm -rf "$KB_SHAPE_BOOT2"

# tool_name is NOT part of the predicate. hooks.json's matcher decides what is
# routed here; the gate judges whatever arrives by its target alone. Two rows: a
# tool name outside the matcher, and no tool_name field at all. Adding a
# `tool_name == Write|Edit` filter would fail-open on both — the same
# unrecognised-field fail-open that #591 was.
run_payload_case "envelope with a non-matcher tool_name — judged by target, deny" \
  "$KB_SHAPE" "$(envelope "$KB_SHAPE/facts/accepted.dl" MultiEdit)" 2
run_payload_case "envelope with NO tool_name — judged by target, deny" \
  "$KB_SHAPE" \
  "$(printf '{"hook_event_name":"PreToolUse","tool_input":{"file_path":"%s"}}' "$KB_SHAPE/facts/accepted.dl")" \
  2

# LEGACY/COMPAT: a bare tool input still works. Kept as its OWN case, explicitly
# labelled, so it can never again be mistaken for coverage of the real shape.
run_payload_case "LEGACY bare tool input (top-level file_path) — still deny" \
  "$KB_SHAPE" "$(legacy_payload "$KB_SHAPE/facts/accepted.dl")" 2

# The row above cannot, on its own, show the top-level fallback still READS:
# drop the fallback and the raw-text probe below denies the same payload for a
# different reason, so it stays green. Mutation found that; this row is the fix.
# Bootstrap is where the two disagree — it needs a RESOLVED target, so a fresh KB
# creating query.dl is ALLOW when the top level is read and DENY when it is not.
KB_LEGACY_BOOT="$(mktemp -d)"
make_kb "$KB_LEGACY_BOOT"
run_payload_case "LEGACY bare tool input, bootstrap KB — allow (proves top level is still read)" \
  "$KB_LEGACY_BOOT" "$(legacy_payload "$KB_LEGACY_BOOT/facts/query.dl")" 0
rm -rf "$KB_LEGACY_BOOT"

# Nested wins over a stale top-level duplicate: if both keys are present the
# envelope's tool_input is the authoritative one. Here the top level points at a
# harmless file and tool_input at the engine input — an implementation that
# preferred the top level would ALLOW.
run_payload_case "both keys present — tool_input takes precedence — deny" \
  "$KB_SHAPE" \
  "$(printf '{"file_path":"%s","tool_input":{"file_path":"%s"}}' \
     "$KB_SHAPE/notes.md" "$KB_SHAPE/facts/accepted.dl")" \
  2

# ---------------------------------------------------------------------------
# CASE 21 (#591): the fail-open branch, split by evidence instead of assumption.
#
# The old comment claimed "an empty/unparseable payload cannot target an engine
# input". For a payload we simply misread, that was false. So the branch now asks
# rather than assumes: no path extracted AND no engine-input filename anywhere in
# the raw text → allow; no path extracted BUT the text names one → treat it as an
# engine input of unknown identity and let the freshness predicate decide.
# ---------------------------------------------------------------------------
# (a) Nothing to judge — allow. This half must stay, or the gate would deny every
# Write and Edit in a session over a contract covering two files.
run_payload_case "pathless payload, no engine input named — allow (fail-open kept)" \
  "$KB_SHAPE" '{"tool_name":"Write","tool_input":{"content":"hello"}}' 0
run_payload_case "unparseable payload, no engine input named — allow" \
  "$KB_SHAPE" 'not json at all' 0
run_payload_case "JSON that is not an object — allow" \
  "$KB_SHAPE" '"just a string"' 0

# (b) Unreadable shape that DOES name an engine input — deny. A future schema
# change lands here instead of silently reopening the hole.
run_payload_case "unreadable shape naming accepted.dl — deny" \
  "$KB_SHAPE" \
  "$(printf '{"tool_name":"Write","tool_input":{"target":"%s"}}' "$KB_SHAPE/facts/accepted.dl")" 2
run_payload_case "unreadable shape naming query.dl — deny" \
  "$KB_SHAPE" \
  "$(printf '{"tool_name":"Edit","edits":[{"dest":"%s"}]}' "$KB_SHAPE/facts/query.dl")" 2
# Not even JSON, but it names an engine input: the raw-text probe is deliberately
# shape-agnostic, so a payload the parser rejects outright still cannot slip past.
run_payload_case "unparseable payload naming an engine input — deny" \
  "$KB_SHAPE" "garbage $KB_SHAPE/facts/accepted.dl garbage" 2

# The deny above is a predicate, not a wall. With a fresh report the same
# unreadable payload is ALLOWED — so an operator whose schema drifted is not
# locked out: /factlog check clears it. A hard deny-on-unresolved would fail this
# row, which is why the branch routes into the predicate instead of exiting 2.
KB_SHAPE_FRESH="$(mktemp -d)"
make_kb "$KB_SHAPE_FRESH"
touch_file "$KB_SHAPE_FRESH/facts/accepted.dl"
set_mtime_past "$KB_SHAPE_FRESH/facts/accepted.dl"
touch_file "$KB_SHAPE_FRESH/facts/logic_report.txt"
run_payload_case "unresolved target but report fresh — allow (escapable, not a wall)" \
  "$KB_SHAPE_FRESH" \
  "$(printf '{"tool_name":"Write","tool_input":{"target":"%s"}}' "$KB_SHAPE_FRESH/facts/accepted.dl")" 0
rm -rf "$KB_SHAPE_FRESH"

# Bootstrap cannot rescue an unresolved target: branch B is a claim about ONE
# named file being absent on disk, and here we do not know which file was meant.
# A bare KB therefore denies — and stays escapable, per the row above.
KB_SHAPE_BOOT="$(mktemp -d)"
make_kb "$KB_SHAPE_BOOT"
run_payload_case "unresolved target in a bare KB — deny (bootstrap needs a known target)" \
  "$KB_SHAPE_BOOT" \
  "$(printf '{"tool_name":"Write","tool_input":{"target":"%s"}}' "$KB_SHAPE_BOOT/facts/query.dl")" 2
rm -rf "$KB_SHAPE_BOOT"

# The unresolved deny is announced, not silent: an operator has to be able to
# tell this apart from an ordinary stale-report deny.
shape_err="$(mktemp)"
shape_exit=0
FACTLOG_ROOT="$KB_SHAPE" bash "$GATE" \
  <<< "$(printf '{"tool_name":"Write","tool_input":{"target":"%s"}}' "$KB_SHAPE/facts/accepted.dl")" \
  >/dev/null 2>"$shape_err" || shape_exit=$?
if grep -qF "could not extract a target path" "$shape_err"; then
  echo "PASS: unresolved-target deny is announced on stderr"
  pass=$((pass + 1))
else
  echo "FAIL: unresolved-target deny was silent; exit=$shape_exit stderr=$(cat "$shape_err")"
  fail=$((fail + 1))
fi
# And it stays quiet on the resolvable path, or it becomes noise on every write.
quiet_err="$(mktemp)"
quiet_exit=0
FACTLOG_ROOT="$KB_SHAPE" bash "$GATE" <<< "$(envelope "$KB_SHAPE/notes.md")" \
  >/dev/null 2>"$quiet_err" || quiet_exit=$?
quiet_hits="$(grep -cF "could not extract a target path" "$quiet_err" || true)"
if [ "$quiet_exit" -eq 0 ] && [ "${quiet_hits:-0}" -eq 0 ]; then
  echo "PASS: resolvable target emits no unresolved-target note"
  pass=$((pass + 1))
else
  echo "FAIL: unresolved-target note leaked onto a resolvable target; exit=$quiet_exit hits=${quiet_hits:-unset} stderr=$(cat "$quiet_err")"
  fail=$((fail + 1))
fi
rm -rf "$KB_SHAPE" "$shape_err" "$quiet_err"

# ---------------------------------------------------------------------------
# CASE 22 (#591): the engine-input probe must not depend on an external command.
#
# The probe was first written as `printf ... | grep -Eq`. With grep missing from
# PATH the pipeline just fails, and because it sits in an `if` condition,
# `set -e` does not fire — the branch fell through to ALLOW. That is a second
# fail-open with a second trigger, sitting inside the fix for the first one, and
# it decides a security verdict on whether a coreutil happens to be installed.
# The probe is now a shell `case`, so this row pins "no external command".
#
# Hermetic setup: PATH is emptied, but a runner stub execs an interpreter by
# ABSOLUTE path, so Python stays available and we are NOT in the fail-closed
# python-availability window — the deny below can only come from the probe.
# Everything the gate runs after that point (stat, the shell builtins) must also
# survive an empty PATH, which this row incidentally pins too.
KB_NOGREP="$(mktemp -d)"
make_kb "$KB_NOGREP"
touch_file "$KB_NOGREP/facts/accepted.dl"   # existing engine input, no report → deny
clear_config

NOGREP_PY="$(bash "$PYTHON_RUNNER" -c 'import sys; print(sys.executable)' 2>/dev/null)"
NOGREP_RUNNER="$(mktemp)"
cat > "$NOGREP_RUNNER" <<RUNNER
#!/usr/bin/env bash
exec "$NOGREP_PY" "\$@"
RUNNER

if [ -z "$NOGREP_PY" ] || [ ! -x "$NOGREP_PY" ]; then
  echo "FAIL: empty-PATH probe — could not resolve an absolute interpreter for the stub runner (got '$NOGREP_PY')"
  fail=$((fail + 1))
else
  nogrep_exit=0
  PATH="" FACTLOG_PYTHON_RUNNER="$NOGREP_RUNNER" FACTLOG_ROOT="$KB_NOGREP" \
    "$BASH_BIN" "$GATE" \
    <<< "$(printf '{"tool_name":"Write","tool_input":{"target":"%s"}}' "$KB_NOGREP/facts/accepted.dl")" \
    >/dev/null 2>&1 || nogrep_exit=$?
  if [ "$nogrep_exit" -eq 2 ]; then
    echo "PASS: engine-input probe works with an empty PATH — no external command (exit $nogrep_exit)"
    pass=$((pass + 1))
  else
    echo "FAIL: empty-PATH probe — expected deny (exit 2), got $nogrep_exit (probe fell back to fail-open?)"
    fail=$((fail + 1))
  fi

  # Companion: with an empty PATH the ALLOW half must still be reachable, or the
  # row above could be satisfied by a gate that simply denies everything when
  # PATH is empty.
  nogrep_allow_exit=0
  PATH="" FACTLOG_PYTHON_RUNNER="$NOGREP_RUNNER" FACTLOG_ROOT="$KB_NOGREP" \
    "$BASH_BIN" "$GATE" <<< '{"tool_name":"Write","tool_input":{"content":"nothing to see"}}' \
    >/dev/null 2>&1 || nogrep_allow_exit=$?
  if [ "$nogrep_allow_exit" -eq 0 ]; then
    echo "PASS: empty PATH, no engine input named — still allows (probe discriminates, not blanket-denies)"
    pass=$((pass + 1))
  else
    echo "FAIL: empty PATH, no engine input named — expected allow (exit 0), got $nogrep_allow_exit"
    fail=$((fail + 1))
  fi
fi

# Windows spells the separator with a backslash, and JSON escapes it, so the raw
# payload text carries `facts\\accepted.dl`. A probe written only for "/" reads
# that as no engine input at all and allows.
run_payload_case "backslash separator (JSON-escaped Windows path) — deny" \
  "$KB_NOGREP" '{"tool_name":"Write","tool_input":{"target":"C:\\kb\\facts\\accepted.dl"}}' 2

rm -rf "$KB_NOGREP" "$NOGREP_RUNNER"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $pass passed, $fail failed"
if [ "$fail" -gt 0 ]; then
  exit 1
fi
exit 0
