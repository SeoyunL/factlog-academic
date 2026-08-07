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
# extractor: deleting `payload` from the extractor's source tuple leaves THIS
# CASE green (measured — the suite as a whole goes to exactly one FAIL, and that
# one is CASE 12b). With no target read, the payload-wide grep fallback fires on
# the same payload for a different reason, so this case reads the same either
# way. CASE 12b below is the one that pins the fallback; this case is kept only
# as a plain regression floor for the shape.
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

# ---------------------------------------------------------------------------
# CASES 17-18: GIT BASH SEPARATORS. A payload from Git Bash carries backslashes,
# where `${path##*/}` hands back the whole string, so the matcher splits on both
# separators.
#
# These pin BOTH backslash splits, and they run on any host: the matcher never
# touches the filesystem, so a backslash payload behaves identically on POSIX.
# An earlier comment in the hook claimed the opposite — that these "cannot be
# pinned from a POSIX host" — and justified shipping two untested lines with it.
# The suite being green with either line deleted meant the suite had no case,
# not that no case was possible.
#
# Measured: deleting `base="${base##*\\}"` fails all three backslash cases
# (17, 18 and 19 — every one of them reaches its basename through a backslash).
# Deleting `pdir="${pdir##*\\}"` fails only case 17, whose PARENT is reached
# through a backslash; case 18's parent is already reached through a forward
# slash, and case 19 has no parent separator at all.
# ---------------------------------------------------------------------------
run_case "Git Bash: all-backslash path to the engine input — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"C:\\kb\\facts\\query.dl"}}' fire
run_case "Git Bash: mixed separators, backslash on the last component — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"C:/kb/facts\\query.dl"}}' fire

# ---------------------------------------------------------------------------
# CASE 19: the POSIX cost of that split, asserted rather than left to be
# discovered. A single file whose NAME contains a backslash — one directory
# entry called `facts\accepted.dl` — nudges, though it is not an engine input.
# The pre-#337 grep stayed silent on it (it searches for a forward slash).
#
# This case exists to make the trade explicit and to fail loudly if anyone
# "fixes" it by dropping the split, which would cost Windows coverage. It is a
# wrong-nudge, the direction that costs one line of output.
# ---------------------------------------------------------------------------
run_case "POSIX file literally named 'facts\\accepted.dl' — fire (known over-fire)" \
  '{"tool_name":"Write","tool_input":{"file_path":"facts\\accepted.dl"}}' fire

# ---------------------------------------------------------------------------
# CASES 20-23: NORMALISATION. "//" and "/./" name exactly the file the collapsed
# path names, so an engine input written either way is a genuine engine input
# and must nudge. All four were SILENT before the collapse was added — this is
# under-firing, the direction that costs a missed signal, and it matters most
# for candidates.csv and logic-policy.dl, which gate_check.sh does not guard.
#
# The loop is pinned by the repeated forms: one pass over "///" leaves a "//"
# behind, so a non-looping collapse fails cases 21 and 23.
# ---------------------------------------------------------------------------
run_case "double slash before the basename — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"/facts//accepted.dl"}}' fire
run_case "triple slash (pins the loop, not just one pass) — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"/facts///accepted.dl"}}' fire
run_case "dot component before the basename — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"/facts/./accepted.dl"}}' fire
run_case "repeated dot components (pins the loop) — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"/facts/././accepted.dl"}}' fire
run_case "double slash in a policy path — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"/kb/policy//logic-policy.dl"}}' fire

# ---------------------------------------------------------------------------
# CASE 24 (CONTROL): `..` is NOT resolved, deliberately rather than by omission.
#
# Labelled CONTROL because it passes pre-fix too — the old payload grep also
# stayed silent on this path, for its own reason — so passing is not evidence
# that anything here works. It is a documentation case, not a pin.
#
# a/b/../c is not a/c when b is a symlink, so collapsing `..` by string surgery
# would be wrong in exactly the case that matters; doing it correctly needs the
# filesystem, which this hook does not touch.
#
# So this genuine engine input gets no nudge. Asserted so the limit is visible
# and so anyone who later adds `..` collapsing has to change this case on
# purpose and think about the symlink question first.
# ---------------------------------------------------------------------------
run_case "CONTROL: parent-dir component is not resolved — silent (documented under-fire)" \
  '{"tool_name":"Write","tool_input":{"file_path":"/facts/x/../accepted.dl"}}' silent

echo "---"
echo "gate_reminder: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
