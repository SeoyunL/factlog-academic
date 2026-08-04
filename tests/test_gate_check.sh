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
# Helper: run gate for a given KB root, target file_path, and expected exit.
# ---------------------------------------------------------------------------
run_case() {
  local desc="$1"
  local kb_root="$2"
  local target_path="$3"
  local expected_exit="$4"

  local payload
  payload="$(printf '{"file_path":"%s"}' "$target_path")"

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

# ---------------------------------------------------------------------------
# Helper: run gate for a given KB root with a VERBATIM payload and expected exit.
# Used by the envelope cases (#323), where the payload shape is the thing under
# test and cannot be derived from a path alone.
# ---------------------------------------------------------------------------
run_payload_case() {
  local desc="$1"
  local kb_root="$2"
  local payload="$3"
  local expected_exit="$4"

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
  "$BASH_BIN" "$GATE" <<< "$(printf '{"file_path":"%s"}' "$KB_NOPY/facts/accepted.dl")" \
  >/dev/null 2>&1 || nopy_exit=$?
if [ "$nopy_exit" -eq 2 ]; then
  echo "PASS: python3 unavailable on engine-input write — fail-closed deny (exit $nopy_exit)"
  pass=$((pass + 1))
else
  echo "FAIL: python3 unavailable — expected fail-closed exit 2, got $nopy_exit"
  fail=$((fail + 1))
fi

# The escape hatch is named for the ONE deny it releases. CASE 38b already pins
# that it does not release the freshness deny; this pins the other half, that it
# does not release the Python-availability deny either. Both halves matter: the
# hatch's whole justification is that a narrow name cannot grow into a global
# gate switch, and a name is not a guarantee until something tests it.
nopy_hatch_exit=0
PATH="$SHIM_PATH" FACTLOG_ROOT="$KB_NOPY" FACTLOG_GATE_ALLOW_UNREADABLE_PAYLOAD=1 \
  "$BASH_BIN" "$GATE" <<< "$(printf '{"file_path":"%s"}' "$KB_NOPY/facts/accepted.dl")" \
  >/dev/null 2>&1 || nopy_hatch_exit=$?
if [ "$nopy_hatch_exit" -eq 2 ]; then
  echo "PASS: FACTLOG_GATE_ALLOW_UNREADABLE_PAYLOAD=1 does NOT release the Python-availability deny (exit $nopy_hatch_exit)"
  pass=$((pass + 1))
else
  echo "FAIL: FACTLOG_GATE_ALLOW_UNREADABLE_PAYLOAD=1 — Python deny must still fire (exit 2), got $nopy_hatch_exit"
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
  bash "$GATE" <<< "$(printf '{"file_path":"%s"}' "$KB_STUB/facts/accepted.dl")" \
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
env -u FACTLOG_ROOT bash "$GATE" <<< "$(printf '{"file_path":"%s"}' "$KB_CFG/facts/accepted.dl")" \
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
FACTLOG_ROOT="$KB_ENV_FRESH" bash "$GATE" <<< "$(printf '{"file_path":"%s"}' "$KB_CFG_STALE/facts/accepted.dl")" \
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
( cd "$KB_CWD" && env -u FACTLOG_ROOT bash "$GATE" <<< '{"file_path":"facts/accepted.dl"}' ) \
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
  <<< "$(printf '{"file_path":"%s"}' "$KB_OBS/facts/accepted.dl")" \
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
  <<< "$(printf '{"file_path":"%s"}' "$KB_OK/facts/accepted.dl")" \
  >/dev/null 2>"$ok_err" || ok_exit=$?

if [ "$ok_exit" -eq 0 ] && ! grep -qF "factlog config resolver unavailable" "$ok_err"; then
  echo "PASS: healthy resolver — allow preserved and NO observability note emitted (exit $ok_exit)"
  pass=$((pass + 1))
else
  echo "FAIL: healthy resolver — expected allow (exit 0) with no note; exit=$ok_exit stderr=$(cat "$ok_err")"
  fail=$((fail + 1))
fi
rm -rf "$KB_OK" "$ok_err"

# ===========================================================================
# CASES 18-38: REAL HOOK ENVELOPE (#323)
#
# Claude Code does not send the bare tool input; it sends an envelope with the
# tool input nested under `tool_input`. CASES 1-17 above all use the FLAT
# fixture shape, which no production payload has, so before the #323 fix the
# gate returned exit 0 for every envelope payload — the freshness guard never
# fired in production and the harness never noticed.
#
# Each case below is labelled with its PRE-FIX result. "vacuous pre-fix" means
# the old gate passed the case only because it allowed everything; those cases
# pin behaviour but are NOT evidence of the defect. The cases that genuinely
# failed before the fix are marked "PRE-FIX FAIL".
# ===========================================================================

kb_no_report() {
  # Existing engine input, no report → the stale-guard must DENY.
  local root="$1"
  make_kb "$root"
  touch_file "$root/facts/accepted.dl"
}

kb_stale() {
  # Report older than accepted.dl → DENY.
  local root="$1"
  make_kb "$root"
  touch_file "$root/facts/logic_report.txt"
  set_mtime_past "$root/facts/logic_report.txt"
  touch_file "$root/facts/accepted.dl"
}

kb_fresh() {
  # Report newer than accepted.dl → ALLOW.
  local root="$1"
  make_kb "$root"
  touch_file "$root/facts/accepted.dl"
  set_mtime_past "$root/facts/accepted.dl"
  touch_file "$root/facts/logic_report.txt"
}

envelope() {
  # The payload Claude Code actually sends for a Write/Edit PreToolUse hook.
  local tool_name="$1"
  local target="$2"
  printf '{"session_id":"s","cwd":"/tmp","hook_event_name":"PreToolUse","tool_name":"%s","tool_input":{"file_path":"%s","content":"x"},"tool_use_id":"u"}' \
    "$tool_name" "$target"
}

# ---------------------------------------------------------------------------
# CASE 18: envelope, existing engine input, report absent — DENY.
# PRE-FIX FAIL (old gate returned 0). This is the issue's core reproduction.
# ---------------------------------------------------------------------------
KB_ENV1="$(mktemp -d)"
kb_no_report "$KB_ENV1"
run_payload_case "envelope: engine input, report absent — deny" \
  "$KB_ENV1" "$(envelope Write "$KB_ENV1/facts/accepted.dl")" 2
rm -rf "$KB_ENV1"

# ---------------------------------------------------------------------------
# CASE 19: envelope, report stale — DENY.
# PRE-FIX FAIL (old gate returned 0).
# ---------------------------------------------------------------------------
KB_ENV2="$(mktemp -d)"
kb_stale "$KB_ENV2"
run_payload_case "envelope: engine input, report stale — deny" \
  "$KB_ENV2" "$(envelope Edit "$KB_ENV2/facts/accepted.dl")" 2
rm -rf "$KB_ENV2"

# ---------------------------------------------------------------------------
# CASE 20: envelope, report fresh — ALLOW (acceptance criterion; vacuous pre-fix).
# ---------------------------------------------------------------------------
KB_ENV3="$(mktemp -d)"
kb_fresh "$KB_ENV3"
run_payload_case "envelope: engine input, report fresh — allow" \
  "$KB_ENV3" "$(envelope Write "$KB_ENV3/facts/accepted.dl")" 0
rm -rf "$KB_ENV3"

# ---------------------------------------------------------------------------
# CASE 21: envelope bootstrap — fresh KB creating query.dl — ALLOW.
# Acceptance criterion; vacuous pre-fix. Guards against a fix that denies the
# first write in a fresh KB and deadlocks the question→query-draft flow.
# ---------------------------------------------------------------------------
KB_ENV4="$(mktemp -d)"
make_kb "$KB_ENV4"
run_payload_case "envelope: bootstrap fresh KB creating query.dl — allow" \
  "$KB_ENV4" "$(envelope Write "$KB_ENV4/facts/query.dl")" 0
rm -rf "$KB_ENV4"

# ---------------------------------------------------------------------------
# CASE 22: envelope, target is not an engine input — ALLOW (vacuous pre-fix).
# ---------------------------------------------------------------------------
KB_ENV5="$(mktemp -d)"
kb_stale "$KB_ENV5"
run_payload_case "envelope: non-engine-input target in a stale KB — allow" \
  "$KB_ENV5" "$(envelope Write "$KB_ENV5/notes.md")" 0
rm -rf "$KB_ENV5"

# ---------------------------------------------------------------------------
# CASE 23: NARROW FAIL-CLOSED — write-class tool, `tool_input` IS an object, but
# it carries no known path key — DENY.
# PRE-FIX FAIL (old gate returned 0).
#
# This is the schema-drift branch: the payload is a write we cannot evaluate, so
# we cannot show it misses the engine inputs.
# ---------------------------------------------------------------------------
KB_ENV6="$(mktemp -d)"
kb_stale "$KB_ENV6"
run_payload_case "envelope: Write with no path key in tool_input — fail-closed deny" \
  "$KB_ENV6" '{"tool_name":"Write","tool_input":{"content":"x"}}' 2

# ---------------------------------------------------------------------------
# CASE 24: the fail-closed branch requires a write-class `tool_name`. With NO
# tool_name (the shape CASES 1-17 send) the gate must ALLOW — otherwise the
# entire flat harness flips to DENY. Vacuous pre-fix; kills a mutant that denies
# on every empty path.
# ---------------------------------------------------------------------------
run_payload_case "envelope: no tool_name, no path key — allow (not a known write)" \
  "$KB_ENV6" '{"tool_input":{"content":"x"}}' 0

# ---------------------------------------------------------------------------
# CASE 25: tool_name outside the write-class list — ALLOW (vacuous pre-fix).
# Pins the exact-match small list: a user who widens the matcher in their own
# settings.json must not have unrelated tools denied.
# ---------------------------------------------------------------------------
run_payload_case "envelope: non-write tool_name, no path key — allow" \
  "$KB_ENV6" '{"tool_name":"Read","tool_input":{"content":"x"}}' 0
rm -rf "$KB_ENV6"

# ---------------------------------------------------------------------------
# CASE 26: KEY PRECEDENCE — `tool_input.file_path` wins over a top-level
# `file_path`. Here the nested path IS the engine input and the top-level one is
# not, so the gate must DENY. PRE-FIX FAIL (old gate read the top level → 0).
# ---------------------------------------------------------------------------
KB_ENV7="$(mktemp -d)"
kb_stale "$KB_ENV7"
run_payload_case "envelope: tool_input.file_path wins over top-level — deny" \
  "$KB_ENV7" \
  "$(printf '{"tool_name":"Write","file_path":"%s","tool_input":{"file_path":"%s"}}' \
      "$KB_ENV7/notes.md" "$KB_ENV7/facts/accepted.dl")" 2

# ---------------------------------------------------------------------------
# CASE 27: the same precedence in the opposite direction — the nested path is
# NOT an engine input while the top-level one is, so the gate must ALLOW.
# PRE-FIX FAIL (old gate read the top level → 2). Together with CASE 26 this
# pins the precedence in both directions; either case alone is passed by an
# implementation that simply merges the two dicts.
#
# MUTATION ATTRIBUTION (measured, not assumed): CASES 26+27 are what kill the
# priority-inversion mutant `for source in (payload, nested)` — 45 passed /
# 2 failed, and the two failures are exactly these. They do NOT kill the
# `d.get("tool_input") or d` mutant, which reads the nested dict first and so
# gets both directions right; CASE 28 kills that one. See CASE 28.
# ---------------------------------------------------------------------------
run_payload_case "envelope: top-level file_path ignored when tool_input has one — allow" \
  "$KB_ENV7" \
  "$(printf '{"tool_name":"Write","file_path":"%s","tool_input":{"file_path":"%s"}}' \
      "$KB_ENV7/facts/accepted.dl" "$KB_ENV7/notes.md")" 0

# ---------------------------------------------------------------------------
# CASE 28: top-level FALLBACK is still consulted when `tool_input` carries no
# path key. No production payload has a top-level file_path; this keeps the flat
# fixture shape of CASES 1-17 working. Vacuous pre-fix.
#
# MUTATION ATTRIBUTION (measured, not assumed): this is the ONLY case that kills
# the `d.get("tool_input") or d` mutant the issue proposed — 46 passed /
# 1 failed. That mutant stops at the truthy `tool_input` and never consults the
# top level, so it returns 0 here instead of 2. CASES 26/27 pass under it.
# ---------------------------------------------------------------------------
run_payload_case "envelope: falls back to top-level file_path — deny" \
  "$KB_ENV7" \
  "$(printf '{"file_path":"%s","tool_input":{"content":"x"}}' "$KB_ENV7/facts/accepted.dl")" 2

# ---------------------------------------------------------------------------
# CASES 29-31: `tool_input` present but NOT an object (null / string / array).
# That is not the narrow fail-closed condition, so the gate must ALLOW rather
# than deny — and, more importantly, the extractor must not die partway through
# and leave the shell reading a truncated record. Vacuous pre-fix; these are the
# cases that kill an extractor which calls .get() on a non-dict.
#
# CASE 31 deliberately hides an engine-input path inside an ARRAY: a path is
# only honoured from an object, so this must still ALLOW.
# ---------------------------------------------------------------------------
run_payload_case "envelope: tool_input is null — allow (not the fail-closed shape)" \
  "$KB_ENV7" '{"tool_name":"Write","tool_input":null}' 0
run_payload_case "envelope: tool_input is a string — allow (not the fail-closed shape)" \
  "$KB_ENV7" '{"tool_name":"Write","tool_input":"oops"}' 0
run_payload_case "envelope: tool_input is an array — allow (not the fail-closed shape)" \
  "$KB_ENV7" \
  "$(printf '{"tool_name":"Write","tool_input":[{"file_path":"%s"}]}' "$KB_ENV7/facts/accepted.dl")" 0

# ---------------------------------------------------------------------------
# CASE 32: MUTATION PIN (vacuous pre-fix) — the engine-input path appears only
# inside `content`, while the actual target is an unrelated file → ALLOW.
#
# A grep-the-whole-payload "fix" (the shape hooks/gate_reminder.sh:17 uses for
# its non-blocking nudge) would DENY here and make every write that merely
# mentions facts/accepted.dl unblockable without a logic check.
# ---------------------------------------------------------------------------
run_payload_case "envelope: engine-input path only inside content — allow (no payload grep)" \
  "$KB_ENV7" \
  "$(printf '{"tool_name":"Write","tool_input":{"file_path":"%s","content":"see %s"}}' \
      "$KB_ENV7/notes.md" "$KB_ENV7/facts/accepted.dl")" 0

# ---------------------------------------------------------------------------
# CASE 33: NUL SEPARATOR — MUTATION PIN.
#
# The extractor separates its three fields with NUL because a path may legally
# contain a newline. Switching both the write and the read back to "\n" passes
# every other case in this file, so without this one that decision is unpinned.
#
# The target is the engine input path with a newline and one more segment glued
# on. That is NOT the engine input, and the KB is stale, so the gate must ALLOW.
# Under a newline separator the record splits at the newline, field 2 becomes
# exactly "<KB>/facts/accepted.dl", the stale-report guard fires, and the case
# gets exit 2 instead.
# ---------------------------------------------------------------------------
run_payload_case "envelope: newline inside file_path — allow (NUL separator pin)" \
  "$KB_ENV7" \
  "$(printf '{"tool_name":"Write","tool_input":{"file_path":"%s\\nsuffix"}}' \
      "$KB_ENV7/facts/accepted.dl")" 0

# ---------------------------------------------------------------------------
# CASE 34: EMPTY file_path — DENY, and pin it.
#
# `tool_input` is an object and `tool_name` is write-class, but the path is the
# empty string, so no target can be read and the narrow fail-closed branch
# fires. Note this deny happens BEFORE the engine-input match, so it holds even
# in a KB whose report is fresh — hence the second call below against a fresh
# KB. The deny text must therefore not diagnose "schema changed" as the only
# possible cause.
# ---------------------------------------------------------------------------
run_payload_case "envelope: empty file_path in a stale KB — deny" \
  "$KB_ENV7" '{"tool_name":"Write","tool_input":{"file_path":"","content":"x"}}' 2

KB_EMPTY_FRESH="$(mktemp -d)"
kb_fresh "$KB_EMPTY_FRESH"
run_payload_case "envelope: empty file_path in a FRESH KB — deny (runs before the match)" \
  "$KB_EMPTY_FRESH" '{"tool_name":"Write","tool_input":{"file_path":""}}' 2
rm -rf "$KB_EMPTY_FRESH"

# ---------------------------------------------------------------------------
# CASE 35: WRITE-CLASS CALL WITH NO tool_input OBJECT — ALLOW, but say so.
#
# The JSON parses and the extractor record is complete, so neither the
# unparseable nor the incomplete note covers these; the gate is nevertheless
# blind to the target. That is a skipped check, not ordinary traffic, so it must
# be observable. Under the current schema Write/Edit always send a `tool_input`
# object, so in normal operation this note never fires.
#
# Three shapes: tool_input missing entirely, a renamed key (the most plausible
# schema drift), and a tool_input that is not an object.
#
# CHANNEL. These assert STDOUT, not stderr. On exit 0 the PreToolUse contract
# discards plain stderr and does not show plain stdout either; the one exit-0
# channel a person actually sees is a JSON object carrying `systemMessage`.
# Asserting the stderr mirror would pin a contract with nothing on the other end
# of it — the same failure #323 itself was. The helper therefore parses stdout as
# JSON and reads the field Claude Code reads, and separately checks that no
# permission decision is smuggled along with it: a gate that just admitted it
# could not check a write must not also auto-approve it.
# ---------------------------------------------------------------------------
blind_note_case() {
  local desc="$1"
  local payload="$2"

  local out err
  out="$(mktemp)"
  err="$(mktemp)"
  local actual_exit=0
  FACTLOG_ROOT="$KB_ENV7" bash "$GATE" <<< "$payload" >"$out" 2>"$err" || actual_exit=$?

  local surfaced
  surfaced="$(bash "$PYTHON_RUNNER" -c '
import json, sys
try:
    payload = json.load(open(sys.argv[1]))
except Exception:
    print("NO-JSON-ON-STDOUT")
    raise SystemExit(0)
message = payload.get("systemMessage")
if not isinstance(message, str) or "carried no tool_input object" not in message:
    print("NO-SYSTEM-MESSAGE")
elif "hookSpecificOutput" in payload or "permissionDecision" in payload:
    print("SMUGGLED-DECISION")
else:
    print("OK")
' "$out")"

  if [ "$actual_exit" -eq 0 ] && [ "$surfaced" = "OK" ]; then
    echo "PASS: $desc — allow with a surfaced fail-open note (exit $actual_exit)"
    pass=$((pass + 1))
  else
    echo "FAIL: $desc — expected allow (exit 0) with a systemMessage note; exit=$actual_exit verdict=$surfaced stdout=$(cat "$out")"
    fail=$((fail + 1))
  fi
  # The stderr mirror is kept for `claude --debug` and external capture, but it
  # is not the channel the contract surfaces, so it is checked second.
  if ! grep -qF "carried no tool_input object" "$err"; then
    echo "FAIL: $desc — the stderr mirror of the note is missing"
    fail=$((fail + 1))
  fi
  rm -f "$out" "$err"
}

blind_note_case "write-class call with no tool_input at all" \
  '{"tool_name":"Write"}'
blind_note_case "write-class call with a renamed tool_input key" \
  "$(printf '{"tool_name":"Write","toolInput":{"file_path":"%s"}}' "$KB_ENV7/facts/accepted.dl")"
blind_note_case "write-class call with a non-object tool_input" \
  '{"tool_name":"Edit","tool_input":"oops"}'

# A non-write tool in the same shape must stay SILENT — the note must not become
# noise on every Read/Grep/Bash call in a session. Silent means BOTH channels:
# an empty stderr and, now that exit 0 can carry a systemMessage, an empty
# stdout. A systemMessage on ordinary traffic is a user-visible warning on every
# tool call, which is worse than the silence it replaced.
quiet_out="$(mktemp)"
quiet_err="$(mktemp)"
quiet_exit=0
FACTLOG_ROOT="$KB_ENV7" bash "$GATE" <<< '{"tool_name":"Read","tool_input":"oops"}' \
  >"$quiet_out" 2>"$quiet_err" || quiet_exit=$?
if [ "$quiet_exit" -eq 0 ] && [ ! -s "$quiet_err" ] && [ ! -s "$quiet_out" ]; then
  echo "PASS: non-write tool with no tool_input object — allow, and silent on both channels (exit $quiet_exit)"
  pass=$((pass + 1))
else
  echo "FAIL: non-write tool — expected a silent allow; exit=$quiet_exit stdout=$(cat "$quiet_out") stderr=$(cat "$quiet_err")"
  fail=$((fail + 1))
fi
rm -f "$quiet_out" "$quiet_err"

# An ordinary, fully checked allow must be silent too — the hot path is every
# Write/Edit in the session.
plain_out="$(mktemp)"
plain_exit=0
FACTLOG_ROOT="$KB_ENV7" bash "$GATE" \
  <<< "$(envelope Write "$KB_ENV7/notes.md")" >"$plain_out" 2>/dev/null || plain_exit=$?
if [ "$plain_exit" -eq 0 ] && [ ! -s "$plain_out" ]; then
  echo "PASS: ordinary non-engine-input write — allow with no systemMessage (exit $plain_exit)"
  pass=$((pass + 1))
else
  echo "FAIL: ordinary non-engine-input write — expected a silent allow; exit=$plain_exit stdout=$(cat "$plain_out")"
  fail=$((fail + 1))
fi
rm -f "$plain_out"

# ---------------------------------------------------------------------------
# CASE 36: UNPARSEABLE PAYLOAD — ALLOW, but say so.
#
# The header enumerates five fail-open branches; before this case none of the
# harness exercised the two that mean "the gate skipped a check it could not
# read". #323 survived precisely because a documented contract had no case
# riding it, so both are pinned here: exit 0 AND the one-line stderr note that
# keeps the skip visible to an operator (#244's rule).
#
# Garbage payload → json.load raises → fail open with a note.
# Empty payload → same branch (json.load("") raises too).
# ---------------------------------------------------------------------------
run_payload_case "unparseable payload — allow (fail-open)" \
  "$KB_ENV7" 'not json at all' 0
run_payload_case "empty payload — allow (fail-open)" \
  "$KB_ENV7" '' 0

unparsed_out="$(mktemp)"
FACTLOG_ROOT="$KB_ENV7" bash "$GATE" <<< 'not json at all' >"$unparsed_out" 2>/dev/null || true
unparsed_msg="$(bash "$PYTHON_RUNNER" -c '
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("systemMessage", ""))
except Exception:
    print("")
' "$unparsed_out")"
case "$unparsed_msg" in
  *"hook payload was not parseable JSON"*)
    echo "PASS: unparseable payload surfaces a fail-open note as a systemMessage"
    pass=$((pass + 1))
    ;;
  *)
    echo "FAIL: unparseable payload — expected a systemMessage note, got stdout: $(cat "$unparsed_out")"
    fail=$((fail + 1))
    ;;
esac
rm -f "$unparsed_out"

# ---------------------------------------------------------------------------
# CASE 37: INCOMPLETE EXTRACTOR RECORD — ALLOW, but say so.
#
# The extractor writes exactly three NUL-terminated fields; if it dies or writes
# anything else, the shell has no record to reason about. That must fail OPEN
# (we cannot even tell the call is a write) and must NOT be silent.
#
# Simulated hermetically with a FACTLOG_PYTHON_RUNNER shim that breaks ONLY the
# extractor call and delegates every other invocation to the real runner. That
# is the realistic shape of this failure — the interpreter is alive, the
# extraction step is what came back wrong — and it keeps the systemMessage
# channel available so this case can assert the note where a person would see
# it. The KB is stale and the payload targets an engine input, so a working
# extractor would DENY — reaching exit 0 proves the incomplete-record branch was
# taken.
# ---------------------------------------------------------------------------
TRUNC_SHIM="$(mktemp -d)"
cat > "$TRUNC_SHIM/runner.sh" <<SH
#!/usr/bin/env bash
# Hand back a record that is not three NUL-terminated fields for the extractor
# call, and pass everything else through to the genuine runner.
for arg in "\$@"; do
  case "\$arg" in
    *PATH_KEYS*) printf 'Write no-nul-here'; exit 0 ;;
  esac
done
exec bash "$PYTHON_RUNNER" "\$@"
SH
chmod +x "$TRUNC_SHIM/runner.sh"

trunc_out="$(mktemp)"
trunc_err="$(mktemp)"
trunc_exit=0
FACTLOG_PYTHON_RUNNER="$TRUNC_SHIM/runner.sh" FACTLOG_ROOT="$KB_ENV7" \
  bash "$GATE" <<< "$(envelope Write "$KB_ENV7/facts/accepted.dl")" \
  >"$trunc_out" 2>"$trunc_err" || trunc_exit=$?
if [ "$trunc_exit" -eq 0 ]; then
  echo "PASS: incomplete extractor record — allow (fail-open) (exit $trunc_exit)"
  pass=$((pass + 1))
else
  echo "FAIL: incomplete extractor record — expected fail-open (exit 0), got $trunc_exit"
  fail=$((fail + 1))
fi
trunc_msg="$(bash "$PYTHON_RUNNER" -c '
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("systemMessage", ""))
except Exception:
    print("")
' "$trunc_out")"
case "$trunc_msg" in
  *"returned no complete record"*)
    echo "PASS: incomplete extractor record surfaces a fail-open note as a systemMessage"
    pass=$((pass + 1))
    ;;
  *)
    echo "FAIL: incomplete extractor record — expected a systemMessage note, got stdout: $(cat "$trunc_out")"
    fail=$((fail + 1))
    ;;
esac
rm -rf "$TRUNC_SHIM" "$trunc_out" "$trunc_err"

# ---------------------------------------------------------------------------
# CASE 38: FACTLOG_GATE_ALLOW_UNREADABLE_PAYLOAD=1 escape hatch.
#
# (a) It releases the narrow schema-drift deny of CASE 23 — without an escape
#     hatch, a payload-schema change turns this gate into a global Write/Edit
#     outage, and the deny stderr is fed back to the model as a retry loop.
# (b) It must NOT release the freshness deny, which is the gate's whole purpose.
# Vacuous pre-fix for (a); PRE-FIX FAIL for (b) (old gate returned 0).
# ---------------------------------------------------------------------------
hatch_exit=0
FACTLOG_GATE_ALLOW_UNREADABLE_PAYLOAD=1 FACTLOG_ROOT="$KB_ENV7" bash "$GATE" \
  <<< '{"tool_name":"Write","tool_input":{"content":"x"}}' >/dev/null 2>&1 || hatch_exit=$?
if [ "$hatch_exit" -eq 0 ]; then
  echo "PASS: FACTLOG_GATE_ALLOW_UNREADABLE_PAYLOAD=1 releases the schema-drift deny (exit $hatch_exit)"
  pass=$((pass + 1))
else
  echo "FAIL: FACTLOG_GATE_ALLOW_UNREADABLE_PAYLOAD=1 — expected allow (exit 0), got $hatch_exit"
  fail=$((fail + 1))
fi

hatch_fresh_exit=0
FACTLOG_GATE_ALLOW_UNREADABLE_PAYLOAD=1 FACTLOG_ROOT="$KB_ENV7" bash "$GATE" \
  <<< "$(envelope Write "$KB_ENV7/facts/accepted.dl")" >/dev/null 2>&1 || hatch_fresh_exit=$?
if [ "$hatch_fresh_exit" -eq 2 ]; then
  echo "PASS: FACTLOG_GATE_ALLOW_UNREADABLE_PAYLOAD=1 does NOT release the freshness deny (exit $hatch_fresh_exit)"
  pass=$((pass + 1))
else
  echo "FAIL: FACTLOG_GATE_ALLOW_UNREADABLE_PAYLOAD=1 — freshness deny must still fire (exit 2), got $hatch_fresh_exit"
  fail=$((fail + 1))
fi
rm -rf "$KB_ENV7"

# ---------------------------------------------------------------------------
# CASE 39: SAME FILE UNDER ANOTHER NAME — DENY.
#
# The header states "TARGET is an engine input iff it resolves to
# <KB_ROOT>/facts/accepted.dl OR query.dl". A hard link reaches the same inode
# under a different name, and Claude Code's Write truncates in place rather than
# replacing the file, so writing through the link writes accepted.dl. Comparing
# canonical path STRINGS calls that a different file and lets it past.
#
# Platform-independent: hard links behave the same everywhere.
# ---------------------------------------------------------------------------
KB_LINK="$(mktemp -d)"
kb_stale "$KB_LINK"
ln "$KB_LINK/facts/accepted.dl" "$KB_LINK/facts/hardlink.dl"
run_payload_case "hard link to accepted.dl — deny (same inode)" \
  "$KB_LINK" "$(envelope Write "$KB_LINK/facts/hardlink.dl")" 2
rm -rf "$KB_LINK"

# ---------------------------------------------------------------------------
# CASES 40-41: CASE-FOLDING FILESYSTEM.
#
# On APFS (macOS default) and NTFS, facts/Accepted.dl IS facts/accepted.dl —
# same inode, same file, and an unguarded write to it supersedes the report. On
# ext4/xfs it is a genuinely different file that must NOT be denied, or the gate
# starts blocking legitimate writes on Linux.
#
# The expected exit therefore depends on the filesystem under $TMPDIR, and the
# harness ASKS it rather than assuming: it creates facts/accepted.dl and checks
# whether the same inode answers to facts/ACCEPTED.DL. CI runs ubuntu-latest, so
# CI exercises the case-sensitive half; a macOS developer machine exercises the
# other. Both halves are the contract.
#
# CASE 40 covers the target-exists path (settled by st_dev/st_ino) and CASE 41
# the target-does-not-exist path (settled by the read-only directory probe),
# because those are two different mechanisms in the matcher.
# ---------------------------------------------------------------------------
KB_CASE="$(mktemp -d)"
kb_stale "$KB_CASE"
if [ -e "$KB_CASE/facts/ACCEPTED.DL" ]; then
  case_fold_expect=2
  case_fold_label="case-folding filesystem"
else
  case_fold_expect=0
  case_fold_label="case-sensitive filesystem"
fi
run_payload_case "case-variant accepted.dl, target exists ($case_fold_label) — expect $case_fold_expect" \
  "$KB_CASE" "$(envelope Write "$KB_CASE/facts/Accepted.dl")" "$case_fold_expect"

# Same question with the engine input ABSENT: query.dl becomes the stale input,
# and the target Accepted.dl does not exist on disk, so stat cannot compare
# inodes. Only the directory probe can answer.
touch_file "$KB_CASE/facts/query.dl"
rm -f "$KB_CASE/facts/accepted.dl"
run_payload_case "case-variant accepted.dl, target absent ($case_fold_label) — expect $case_fold_expect" \
  "$KB_CASE" "$(envelope Write "$KB_CASE/facts/Accepted.dl")" "$case_fold_expect"
rm -rf "$KB_CASE"

# ---------------------------------------------------------------------------
# CASES 42-46: THE BASENAME PREFILTER MUST NOT OPEN A HOLE.
#
# The gate short-circuits to ALLOW, without canonicalising, when the target's
# last path component cannot be an engine input. That saves an interpreter spawn
# on every Write/Edit in the session, and it is the whole reason the fix does not
# double write latency — but it is a NAME test standing in for a PATH question,
# and a name lies in five ways. CASE 42 pins the short-circuit itself; 43-46 are
# the paths that must survive it, each of which the naive form
# `case "$target_path" in */accepted.dl) ;; *) exit 0 ;; esac` gets wrong.
#
# CASE 39 (hard link) is the fifth and already sits above: it is what forces the
# prefilter to pay for a `stat`.
#
# All five run against a stale KB, so "reached the matcher" is observable as
# exit 2 and "short-circuited" as exit 0.
# ---------------------------------------------------------------------------
KB_PRE="$(mktemp -d)"
kb_stale "$KB_PRE"

# CASE 42: the short-circuit itself. A neighbouring name in the same directory
# must ALLOW — this is the case that carries the latency saving.
run_payload_case "prefilter: facts/accepted.dl.bak in a stale KB — allow" \
  "$KB_PRE" "$(envelope Write "$KB_PRE/facts/accepted.dl.bak")" 0

# CASE 43: SYMLINK whose own name differs from its target's. The prefilter runs
# BEFORE canonicalisation, so it cannot "resolve it and then see the basename";
# only the -L test saves this.
ln -s accepted.dl "$KB_PRE/facts/notes.dl"
run_payload_case "prefilter: symlink notes.dl -> accepted.dl — deny" \
  "$KB_PRE" "$(envelope Write "$KB_PRE/facts/notes.dl")" 2

# CASE 44: TRAILING SLASH. Canonicalises to the engine input; does not match a
# `*/accepted.dl` glob.
run_payload_case "prefilter: trailing slash on accepted.dl — deny" \
  "$KB_PRE" "$(envelope Write "$KB_PRE/facts/accepted.dl/")" 2

# CASE 45: TRAILING DOT COMPONENT. Canonicalises to the engine input; its
# basename is ".".
run_payload_case "prefilter: accepted.dl/. — deny" \
  "$KB_PRE" "$(envelope Write "$KB_PRE/facts/accepted.dl/.")" 2

# CASE 46: CASE VARIANT. A case-sensitive glob here would undo the matcher's
# case handling before the matcher ever ran. Filesystem-dependent for the same
# reason as CASES 40-41, and asked the same way.
if [ -e "$KB_PRE/facts/ACCEPTED.DL" ]; then
  pre_case_expect=2
else
  pre_case_expect=0
fi
run_payload_case "prefilter: case-variant Accepted.dl reaches the matcher — expect $pre_case_expect" \
  "$KB_PRE" "$(envelope Write "$KB_PRE/facts/Accepted.dl")" "$pre_case_expect"
rm -rf "$KB_PRE"

# ---------------------------------------------------------------------------
# CASE 47: THE ENGINE INPUT IS ITSELF A SYMLINK — DENY.
#
# Every guard in CASES 43-46 asks about the TARGET. None asks whether
# accepted.dl or query.dl is a symlink. With
# `facts/query.dl -> ../shared/my-queries.dl`, the engine input canonicalises to
# a name the prefilter has never heard of, so a write to the real file behind it
# IS an engine-input write that the basename test cannot recognise.
#
# This one does not self-heal. compile_facts.py writes accepted.dl through
# temp + os.replace, which breaks a symlink there on the next compile; query.dl
# is user-authored and never atomically replaced, so the symlink survives.
# ---------------------------------------------------------------------------
KB_ESYM="$(mktemp -d)"
make_kb "$KB_ESYM"
mkdir -p "$KB_ESYM/shared"
touch_file "$KB_ESYM/facts/logic_report.txt"
set_mtime_past "$KB_ESYM/facts/logic_report.txt"
touch_file "$KB_ESYM/shared/my-queries.dl"
ln -s ../shared/my-queries.dl "$KB_ESYM/facts/query.dl"
run_payload_case "engine input query.dl is a symlink — deny writes to its real file" \
  "$KB_ESYM" "$(envelope Write "$KB_ESYM/shared/my-queries.dl")" 2
rm -rf "$KB_ESYM"

# ---------------------------------------------------------------------------
# CASES 48-49: A LEADING TILDE MUST NOT DISABLE THE GUARDS.
#
# canon() runs expanduser; the prefilter does not. A "~/…" target therefore made
# `-L`, `-e` and `stat` interrogate a literal path that does not exist, and all
# three answered "not a symlink, no hard link" — silently switching off the two
# guards that need the filesystem. Both of the shapes those guards exist for are
# pinned here through a tilde.
#
# HOME is repointed at the KB's parent so the tilde resolves into the fixture
# and the case never touches the developer's real home directory.
# ---------------------------------------------------------------------------
TILDE_HOME="$(mktemp -d)"
KB_TILDE="$TILDE_HOME/kb"
make_kb "$KB_TILDE"
touch_file "$KB_TILDE/facts/logic_report.txt"
set_mtime_past "$KB_TILDE/facts/logic_report.txt"
touch_file "$KB_TILDE/facts/accepted.dl"
ln -s accepted.dl "$KB_TILDE/facts/notes.dl"
ln "$KB_TILDE/facts/accepted.dl" "$KB_TILDE/facts/hardlink.dl"

tilde_case() {
  local desc="$1"
  local target="$2"
  local actual_exit=0
  HOME="$TILDE_HOME" FACTLOG_ROOT="$KB_TILDE" bash "$GATE" \
    <<< "$(envelope Write "$target")" >/dev/null 2>&1 || actual_exit=$?
  if [ "$actual_exit" -eq 2 ]; then
    echo "PASS: $desc (exit $actual_exit)"
    pass=$((pass + 1))
  else
    echo "FAIL: $desc — expected deny (exit 2), got $actual_exit"
    fail=$((fail + 1))
  fi
}

tilde_case "tilde path through a symlink — deny (guard 3 not disabled)" \
  "~/kb/facts/notes.dl"
tilde_case "tilde path through a hard link — deny (guard 7 not disabled)" \
  "~/kb/facts/hardlink.dl"
rm -rf "$TILDE_HOME"

# ---------------------------------------------------------------------------
# CASE 50: THE PREDICATE IS KEYED ON THE TARGET, NOT THE TOOL NAME.
#
# `_is_write_tool` gates the fail-closed empty-path branch and the fail-open
# notes. It does NOT gate the freshness predicate: once a target path is
# readable, any tool that reaches this hook and names a stale engine input is
# denied. hooks.json registers only Write|Edit, so nothing else arrives in
# practice — this is what a user who widens their own matcher gets.
#
# Pinned because the alternative is worse, not because a denied Read is nice:
# keying the predicate on the write-class list would leave a widened matcher
# (MultiEdit, NotebookEdit) completely unguarded, since those names are not on
# the list. Erring toward guarding is the safe direction; silently dropping the
# guard is not. Behaviour predates #323 and was previously unpinned.
# ---------------------------------------------------------------------------
KB_TOOLNAME="$(mktemp -d)"
kb_stale "$KB_TOOLNAME"
for other_tool in Read Grep MultiEdit NotebookEdit; do
  run_payload_case "$other_tool naming a stale engine input — deny (predicate keys on the target)" \
    "$KB_TOOLNAME" "$(envelope "$other_tool" "$KB_TOOLNAME/facts/accepted.dl")" 2
done
rm -rf "$KB_TOOLNAME"

# ---------------------------------------------------------------------------
# CASE 47: MTIME EQUALITY IS FRESH — ALLOW.
#
# The freshness comparison is `report_mtime -lt newest_input_mtime`, so a report
# written in the SAME second as the engine input counts as fresh. That is not an
# accident to be tightened later: run_logic_check.py writes logic_report.txt
# immediately after compile_facts.py writes accepted.dl, and on a filesystem
# with second-granularity mtimes the two land on the same value routinely.
# Flipping the comparison to `-le` would deny the ordinary path right after a
# successful /factlog check.
#
# Both files are stamped to one fixed second so the case cannot go flaky.
# ---------------------------------------------------------------------------
KB_EQ="$(mktemp -d)"
make_kb "$KB_EQ"
touch_file "$KB_EQ/facts/accepted.dl"
touch_file "$KB_EQ/facts/logic_report.txt"
touch -t 202001011200.00 "$KB_EQ/facts/accepted.dl" "$KB_EQ/facts/logic_report.txt"
run_payload_case "report and engine input share an mtime — allow (boundary is -lt)" \
  "$KB_EQ" "$(envelope Write "$KB_EQ/facts/accepted.dl")" 0
rm -rf "$KB_EQ"

# ---------------------------------------------------------------------------
# CASE 48: SHIFTED EXTRACTOR RECORD IS NOT SILENT.
#
# A NUL inside a JSON string value pushes the extractor's three fields along by
# one, so `tool_input_kind` receives a path instead of one of the four kinds the
# extractor writes. The gate falls open, which is right — but before the `*)`
# arm it did so without a single line on any channel, contradicting the header's
# enumeration of exactly which branches are silent.
#
# The OS rejects a path containing a NUL, so the exit code here is not the
# interesting part; the note is. Asserted on stdout because that is the channel
# exit 0 surfaces.
# ---------------------------------------------------------------------------
KB_NUL="$(mktemp -d)"
kb_stale "$KB_NUL"
nul_out="$(mktemp)"
nul_exit=0
FACTLOG_ROOT="$KB_NUL" bash "$GATE" \
  <<< "$(printf '{"tool_name":"Write","tool_input":{"file_path":"\\u0000%s"}}' "$KB_NUL/facts/accepted.dl")" \
  >"$nul_out" 2>/dev/null || nul_exit=$?
nul_msg="$(bash "$PYTHON_RUNNER" -c '
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("systemMessage", ""))
except Exception:
    print("")
' "$nul_out")"
case "$nul_exit:$nul_msg" in
  0:*"unrecognised record shape"*)
    echo "PASS: NUL inside file_path — allow, and says the gate was skipped (exit $nul_exit)"
    pass=$((pass + 1))
    ;;
  *)
    echo "FAIL: NUL inside file_path — expected exit 0 with a systemMessage note; exit=$nul_exit stdout=$(cat "$nul_out")"
    fail=$((fail + 1))
    ;;
esac
rm -rf "$KB_NUL" "$nul_out"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $pass passed, $fail failed"
if [ "$fail" -gt 0 ]; then
  exit 1
fi
exit 0
