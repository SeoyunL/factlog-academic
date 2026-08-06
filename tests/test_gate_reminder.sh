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

# ---------------------------------------------------------------------------
# CASE 7: THE ISSUE (#337). The target is an unrelated file; the engine input
# appears only inside `content`. Must stay SILENT.
#
# Pre-#337 this fired, because the hook grepped the whole payload string.
# ---------------------------------------------------------------------------
run_case "engine input only inside Write content — silent" \
  '{"tool_name":"Write","tool_input":{"file_path":"/tmp/notes.md","content":"see facts/accepted.dl"}}' silent

# ---------------------------------------------------------------------------
# CASE 8: same defect through an Edit's replacement strings rather than
# `content`. Editing prose ABOUT the KB is the common way to hit this — every
# doc change that names facts/query.dl nudged.
# ---------------------------------------------------------------------------
run_case "engine input only inside Edit old_string/new_string — silent" \
  '{"tool_name":"Edit","tool_input":{"file_path":"/tmp/README.md","old_string":"facts/query.dl","new_string":"facts/query.dl (renamed)"}}' silent

# ---------------------------------------------------------------------------
# CASE 9: the policy file has the same problem and its own regex branch, so it
# needs its own case; the content pin above does not reach it.
# ---------------------------------------------------------------------------
run_case "policy path only inside content — silent" \
  '{"tool_name":"Write","tool_input":{"file_path":"/tmp/notes.md","content":"edit policy/logic-policy.dl next"}}' silent

# ---------------------------------------------------------------------------
# CASE 10: KEY PRECEDENCE PIN. `tool_input.file_path` is the unrelated file the
# tool really wrote; a top-level `file_path` names the engine input. The nested
# key wins, so this must stay SILENT.
#
# Reversing the precedence to "top level first, tool_input as fallback" passes
# every other case in this file — including CASE 12, which only proves the
# top-level key is read AT ALL — so without this case the order is unpinned and
# the hook would be reading a key that no real payload uses.
# ---------------------------------------------------------------------------
run_case "top-level file_path must not override tool_input.file_path — silent" \
  '{"tool_name":"Write","file_path":"/kb/facts/accepted.dl","tool_input":{"file_path":"/tmp/notes.md","content":"x"}}' silent

# ---------------------------------------------------------------------------
# CASE 11: SUFFIX vs COMPONENT. Neither of these paths IS an engine input:
# query.dl.bak is a backup beside one, and myfacts/ is not facts/. A substring
# search says otherwise, which is what the old grep did.
# ---------------------------------------------------------------------------
run_case "target facts/query.dl.bak is not the engine input — silent" \
  '{"tool_name":"Write","tool_input":{"file_path":"/kb/facts/query.dl.bak","content":"x"}}' silent
run_case "target myfacts/query.dl is not the engine input — silent" \
  '{"tool_name":"Write","tool_input":{"file_path":"/kb/myfacts/query.dl","content":"x"}}' silent

# ---------------------------------------------------------------------------
# CASE 12 (CONTROL, and genuinely UNPINNED): the flat fixture shape — no
# `tool_input`, `file_path` at the top level — still nudges.
#
# It passes pre-fix, and it does NOT pin the top-level fallback in the
# extractor: deleting `payload` from the extractor's source tuple leaves this
# whole file green (measured). With no target read, the payload-wide grep
# fallback fires on the same payload for a different reason, and this case
# reads exit-equivalent either way. CASE 12b below is the one that pins the
# fallback; this case is kept only as a plain regression floor for the shape.
# ---------------------------------------------------------------------------
run_case "CONTROL: flat fixture, top-level file_path — fire" \
  '{"file_path":"/kb/facts/query.dl"}' fire

# ---------------------------------------------------------------------------
# CASE 12b: the flat shape with an UNRELATED target and the engine input only in
# `content` — silent. This is what actually pins the top-level fallback.
#
# Drop `payload` from the extractor's source tuple and no target is read from a
# flat payload at all, so the payload-wide grep decides and this fires. CASE 12
# cannot see that mutation because the fallback happens to give it the answer it
# wanted; this case wants the opposite answer, so the fallback cannot mask it.
# ---------------------------------------------------------------------------
run_case "flat fixture, unrelated target, engine input in content — silent" \
  '{"file_path":"/tmp/notes.md","content":"see facts/accepted.dl"}' silent

# ---------------------------------------------------------------------------
# CASE 13 (CONTROL, mutation-pinned): an unparseable payload that mentions an
# engine input still nudges — the documented fall back to pre-#337 behaviour
# when no target can be read. Passes pre-fix as well; deleting the fallback
# branch turns it silent.
# ---------------------------------------------------------------------------
run_case "CONTROL: unparseable payload mentioning an engine input — fire (fallback)" \
  'not json at all, but it names facts/accepted.dl' fire

# ---------------------------------------------------------------------------
# CASE 14 (CONTROL, mutation-pinned): a write-class envelope whose tool_input
# carries no path key at all. No target is readable, so the same fallback
# applies and the nudge fires rather than going quiet.
# ---------------------------------------------------------------------------
run_case "CONTROL: envelope with no path key — fire (fallback)" \
  '{"tool_name":"Write","tool_input":{"content":"see facts/query.dl"}}' fire

# ---------------------------------------------------------------------------
# CASE 15: NEWLINE IN THE TARGET. The extractor frames its one field with NUL
# because a path may legally contain a newline, and the matcher compares path
# COMPONENTS rather than grepping lines.
#
# The target here is the engine input path with a newline and one more segment
# glued on. That file is not the engine input, so this must stay SILENT. Switch
# the NUL framing to "\n" and the read stops at the newline, the target becomes
# exactly "/kb/facts/query.dl", and this fires.
# ---------------------------------------------------------------------------
run_case "newline inside file_path — silent (NUL framing pin)" \
  '{"tool_name":"Write","tool_input":{"file_path":"/kb/facts/query.dl\nsuffix"}}' silent

# ---------------------------------------------------------------------------
# CASE 16: the nudge must survive a broken interpreter. With FACTLOG_PYTHON
# pointing at something that is not Python, no target can be extracted, so the
# documented fallback runs: exit 0, and the pre-#337 payload grep decides.
#
# This is the one case where #337's false positive is still reachable — it is
# what "falls back to the previous behaviour" costs. Asserted here so the
# degrade is visible rather than discovered.
# ---------------------------------------------------------------------------
broken_exit=0
broken_err="$(printf '%s' '{"tool_name":"Write","tool_input":{"file_path":"/tmp/notes.md","content":"see facts/accepted.dl"}}' \
  | FACTLOG_PYTHON=/bin/false bash "$HOOK" 2>&1 >/dev/null)" || broken_exit=$?
case "$broken_err" in
  *"An engine input was edited"*) broken_fired=fire ;;
  *) broken_fired=silent ;;
esac
if [ "$broken_fired" = "fire" ] && [ "$broken_exit" -eq 0 ]; then
  echo "PASS: no usable Python — falls back to the payload grep, still exit 0 (fire, exit 0)"
  pass=$((pass + 1))
else
  echo "FAIL: no usable Python — expected fire/exit 0, got $broken_fired/exit $broken_exit"
  fail=$((fail + 1))
fi

echo "---"
echo "gate_reminder: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
