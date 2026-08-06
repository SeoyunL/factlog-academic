#!/usr/bin/env bash
# Behavioral matrix for hooks/gate_reminder.sh
#
# The hook is a PostToolUse NUDGE: it never blocks and its exit code is ignored,
# so the observable is not an exit code but whether the reminder text reaches
# stderr. Every case therefore asserts two things:
#   - fire / silent, read off the reminder's first line;
#   - exit 0, unconditionally. A nudge that exits non-zero would turn a reminder
#     into a reported hook error, which is the one thing it must not do.
#
# Usage: bash tests/test_gate_reminder.sh
#   Returns 0 if all cases pass, 1 if any fail.

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

HOOK="$(cd "$(dirname "$0")/.." && pwd)/hooks/gate_reminder.sh"

pass=0
fail=0

# ---------------------------------------------------------------------------
# Helper: feed a VERBATIM payload to the hook and assert fire/silent + exit 0.
#
# The payload is the thing under test in every case here (#337 is precisely
# about reading the target out of the payload rather than grepping all of it),
# so there is no path-only convenience runner.
# ---------------------------------------------------------------------------
run_case() {
  local desc="$1"
  local payload="$2"
  local expected="$3"   # fire | silent

  local actual_exit=0
  local err
  err="$(printf '%s' "$payload" | bash "$HOOK" 2>&1 >/dev/null)" || actual_exit=$?

  local actual="silent"
  case "$err" in
    *"An engine input was edited"*) actual="fire" ;;
  esac

  if [ "$actual" = "$expected" ] && [ "$actual_exit" -eq 0 ]; then
    echo "PASS: $desc ($actual, exit $actual_exit)"
    pass=$((pass + 1))
  else
    echo "FAIL: $desc — expected $expected/exit 0, got $actual/exit $actual_exit"
    fail=$((fail + 1))
  fi
}

# ---------------------------------------------------------------------------
# CASES 1-4 (CONTROL): a write whose TARGET is an engine input must nudge.
#
# These pass both before and after #337 — the payload-wide grep also finds a
# path that really is the target. They are here to pin the direction the fix
# must not break: making the match structural must not make the nudge stop
# firing on the writes it exists for. Marked CONTROL because passing them is
# not evidence that anything was fixed.
# ---------------------------------------------------------------------------
run_case "CONTROL: Write targets facts/query.dl — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"/kb/facts/query.dl","content":"q"}}' fire
run_case "CONTROL: Write targets facts/accepted.dl — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"/kb/facts/accepted.dl","content":"a"}}' fire
run_case "CONTROL: Write targets facts/candidates.csv — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"/kb/facts/candidates.csv","content":"c"}}' fire
run_case "CONTROL: Edit targets policy/logic-policy.dl — fire" \
  '{"tool_name":"Edit","tool_input":{"file_path":"/kb/policy/logic-policy.dl","old_string":"x","new_string":"y"}}' fire

# ---------------------------------------------------------------------------
# CASE 5 (CONTROL): a relative target is still the engine input.
#
# hooks.json gives the hook no KB root, and the nudge deliberately does not
# resolve one (see the hook header), so a relative path has to match on its own
# last two components.
# ---------------------------------------------------------------------------
run_case "CONTROL: relative target facts/accepted.dl — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"facts/accepted.dl","content":"a"}}' fire

# ---------------------------------------------------------------------------
# CASE 6 (CONTROL): a write with no engine input anywhere in it stays silent.
#
# The floor case. If this ever fires, the nudge fires on everything and carries
# no information at all.
# ---------------------------------------------------------------------------
run_case "CONTROL: unrelated write, no engine input mentioned — silent" \
  '{"tool_name":"Write","tool_input":{"file_path":"/tmp/notes.md","content":"hello"}}' silent

echo "---"
echo "gate_reminder: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
