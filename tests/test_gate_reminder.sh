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

# Point the active-KB config at a throwaway dir and remove it on exit.
# gate_reminder.sh reads no config at all — it only ever looks at the payload
# — so this is defence against a future edit that gives it one, not something
# the current hook needs. Without the trap every run leaked a temp dir.
XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"
export XDG_CONFIG_HOME
trap 'rm -rf "${XDG_CONFIG_HOME%/*}"' EXIT

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
  local err out
  out="$(mktemp)"
  err="$(printf '%s' "$payload" | bash "$HOOK" 2>&1 >"$out")" || actual_exit=$?
  local stdout_bytes; stdout_bytes="$(wc -c < "$out" | tr -d ' ')"
  rm -f "$out"

  local actual="silent"
  case "$err" in
    *"An engine input was edited"*) actual="fire" ;;
  esac

  # stdout must stay EMPTY. The nudge writes to stderr, and a PostToolUse hook
  # exiting 0 with JSON on stdout would be parsed by Claude Code as hook output
  # — a stray echo here could start steering the session instead of nudging it.
  if [ "$actual" = "$expected" ] && [ "$actual_exit" -eq 0 ] && [ "$stdout_bytes" -eq 0 ]; then
    echo "PASS: $desc ($actual, exit $actual_exit)"
    pass=$((pass + 1))
  else
    echo "FAIL: $desc — expected $expected/exit 0/empty stdout, got $actual/exit $actual_exit/${stdout_bytes}B stdout"
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
# These pin the backslash handling, and they run on any host: the matcher never
# touches the filesystem, so a backslash payload behaves identically on POSIX.
# An earlier comment in the hook claimed the opposite — that these "cannot be
# pinned from a POSIX host" — and justified shipping untested lines with it.
# The suite being green with the backslash handling deleted meant the suite had
# no case, not that no case was possible.
#
# HOW TO REPRODUCE THE NUMBERS BELOW. The matcher does not have a separate
# "strip a backslash" line to delete — it names both separators in four bracket
# sets. The equivalent mutation is to revert one bracket set at a time to the
# forward-slash-only form it had before Git Bash was supported, and run the
# whole suite. Measured on bash 3.2.57, one set at a time:
#
#   hooks/gate_reminder.sh line          reverted to        red  cases
#   -----------------------------------  -----------------  ---  --------------------
#   base="${path##*[/\\]}"               ${path##*/}          8  17 18 19 25 26 27 29 32
#   tail="${parent##*[!/\\.]}"           ${parent##*[!/.]}   10  17 18 19 25 26 27 29 30 31 32
#   [/\\]*) case arm on $tail            [/]*)                8  17 18 19 25 26 27 30 31
#   pdir="${parent##*[/\\]}"             ${parent##*/}        6  17 25 26 27 30 31
#
# The basename set takes every case that reaches its BASENAME through a
# backslash; the pdir set takes every case that reaches its PARENT through one,
# which is why case 18 (parent reached through a forward slash) and case 19 (no
# parent separator at all) survive that one. The tail set takes all ten
# backslash-bearing cases, because a backslash it does not recognise as part of
# the tail is left in `parent` and defeats the pdir comparison downstream.
#
# The first three numbers are the 8 / 10 / 6 the matcher's own comment in
# hooks/gate_reminder.sh reports for its three expansions; the case arm is a
# fourth site and is listed here because it is a separate edit someone could
# make on its own.
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
# CASES 20-24: NORMALISATION, forward-slash side. "//" and "/./" name exactly
# the file the collapsed path names, so an engine input written either way is a
# genuine engine input and must nudge. All five were SILENT before redundant
# separators were handled — this is under-firing, the direction that costs a
# missed signal, and it matters most for candidates.csv and logic-policy.dl,
# which gate_check.sh does not guard at all.
#
# Cases 21 and 23 pin the REPEATED forms of the same two shapes. Under the
# current matcher they are not distinct pins: it reads the tail in one greedy
# expansion and never counts separators, so "///" and "//" take an identical
# path and cases 21/23 can only fail when 20/22 already have. They are kept as
# a floor for a matcher that handles the single form by rewriting it — such a
# matcher passes 20 and 22 and fails these — NOT because anything here needs to
# iterate. Nothing in the matcher does; see CASE 35 for why it must not.
# ---------------------------------------------------------------------------
run_case "double slash before the basename — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"/facts//accepted.dl"}}' fire
run_case "triple slash (repeated separator form) — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"/facts///accepted.dl"}}' fire
run_case "dot component before the basename — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"/facts/./accepted.dl"}}' fire
run_case "repeated dot components — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"/facts/././accepted.dl"}}' fire
run_case "double slash in a policy path — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"/kb/policy//logic-policy.dl"}}' fire

# ---------------------------------------------------------------------------
# CASES 25-27: NORMALISATION, backslash side. The same redundant forms a Git
# Bash payload can carry. These were still SILENT after the forward-slash side
# was handled — genuine engine inputs missed on the one platform the backslash
# handling exists to serve, which is the same "list reads closed but has a
# member missing" shape as the earlier round's finding.
#
# Case 27 is the repeated form on this side, and stands to 25 exactly as case
# 21 stands to 20: a floor against a rewriting matcher, not a distinct pin.
# ---------------------------------------------------------------------------
run_case "Git Bash: doubled backslash before the basename — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"C:\\kb\\facts\\\\accepted.dl"}}' fire
run_case "Git Bash: backslash dot component — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"C:\\kb\\facts\\.\\accepted.dl"}}' fire
run_case "Git Bash: tripled backslash (repeated separator form) — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"C:\\kb\\facts\\\\\\accepted.dl"}}' fire

# ---------------------------------------------------------------------------
# CASE 28 (CONTROL): `..` is NOT resolved, deliberately rather than by omission.
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

# ---------------------------------------------------------------------------
# CASES 29-32: MIXED SEPARATORS in the redundant forms. Git Bash payloads mix
# `/` and `\` freely (CASE 18 already relies on that), so the redundant forms
# come mixed too. All four were SILENT until the matcher stopped enumerating
# forms and started reading the tail.
#
# They need no special code — that is the point. Nothing in the matcher knows
# which kind of separator it crossed, so these pass for the same reason the
# single-separator forms do.
# ---------------------------------------------------------------------------
run_case "mixed: forward-slash dot component, backslash before basename — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"C:/kb/facts/.\\accepted.dl"}}' fire
run_case "mixed: backslash dot component, forward slash before basename — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"C:\\kb\\facts\\./accepted.dl"}}' fire
run_case "mixed: backslash then forward slash — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"C:\\kb\\facts\\/accepted.dl"}}' fire
run_case "mixed: forward slash then backslash — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"C:/kb/facts/\\accepted.dl"}}' fire

# ---------------------------------------------------------------------------
# CASE 33 (CONTROL): a directory whose NAME ends in a dot is not the engine
# input. It passes pre-fix and on the previous commit too — both were silent
# here for their own reasons — so it is not a pin for this change. It guards
# the NEW tail logic: neutering the tail check makes it red (measured below).
#
# `/kb/facts./accepted.dl` sits in a directory called `facts.`, so it must stay
# silent. This pins the tail check that distinguishes "dots that are their own
# components" from "dots belonging to the component" — without it the trailing
# dot is skipped, `facts.` reads as `facts`, and this becomes a false positive.
# ---------------------------------------------------------------------------
run_case "CONTROL: directory named 'facts.' is not the engine input — silent" \
  '{"tool_name":"Write","tool_input":{"file_path":"/kb/facts./accepted.dl"}}' silent

# ---------------------------------------------------------------------------
# CASE 34 (CONTROL): `..` must be REJECTED, not skipped like `.`. Also passes
# on the previous commit, for the same reason as CASE 33 — it exists to guard
# the new tail logic, where skipping `..` like `.` would be a false positive.
#
# facts/../accepted.dl does not denote an engine input at all — it resolves to
# /accepted.dl — so skipping `..` the way `.` is skipped would read the parent
# as `facts` and fire. Distinct from CASE 28, which is an engine input that goes
# unnudged; this one is a non-engine-input that must not nudge.
# ---------------------------------------------------------------------------
run_case "CONTROL: parent-dir component must not be skipped like a dot — silent" \
  '{"tool_name":"Write","tool_input":{"file_path":"/facts/../accepted.dl"}}' silent

# ---------------------------------------------------------------------------
# CASES 35 and 35b: the cost of a long separator run, pinned against
# hooks.json's OWN timeout.
#
# A rewriting collapse was superlinear in the number of repeated separators:
# measured 3.5s at N=4000 and 12.0s at N=6000, past hooks.json's `timeout: 10`,
# so Claude Code kills the hook and NO nudge appears — a silent non-firing, the
# one direction this hook must not fail in, and the fallback never runs either.
#
# WHERE THE RUN SITS DECIDES THE COST, so both cases put it in the expensive
# place. The matcher no longer rewrites, but `${parent##*[!/\\.]}` is still a
# greedy `##` and bash walks prefixes longest-first. If another component
# follows the run, the match is found near the full length and the scan is
# effectively linear; if the run sits immediately BEFORE the basename, every
# prefix fails and the scan is quadratic. Measured on bash 3.2.57, matcher
# only, seconds per call:
#
#   N        "/kb" + run + "facts/accepted.dl"   "/kb/facts" + run + "accepted.dl"
#   1000     0.0007                              0.013
#   6000     0.0035                              0.436
#   20000    0.011                               4.771
#   32000    0.018                              12.239
#
# The left column is flat at any N, so a case built on it cannot go red however
# bad the per-character cost gets. An earlier version of CASE 35 used exactly
# that shape, which is why both cases now use the right-hand one.
#
# CASE 35b is the same shape with a "/." run: two characters per repetition and
# so about four times the work at equal N — 1.011s against 0.204s at N=4000.
#
# The bound is not a tuned wall-clock number, it is the timeout the plugin
# actually ships in hooks.json. End to end through the hook, including its one
# interpreter spawn: 0.5-0.7s for CASE 35 and 1.1-1.3s for CASE 35b against the
# 10s limit, while the rewriting collapse takes SIGALRM on both shapes. The
# range is across two interpreters (3.11 and 3.14) — the spawn is the part that
# moves, and quoting one figure would be reporting host load. Better than 7x
# margin on the passing side even at the slow end, and an overshoot on the
# failing side, so a loaded host cannot flip either. The harness imposes no
# timeout of its own, so
# without the alarm the old collapse would simply take 12s and PASS — which is
# why these are written this way and not as plain run_cases.
#
# N is chosen to catch the collapse, not for realism: inside PATH_MAX the cost
# of either shape is negligible (0.013s at N=1000). What these defend is the
# bound the matcher's comment in hooks/gate_reminder.sh asserts, not a timeout
# reachable in production.
#
# `perl -e 'alarm N; exec @ARGV'` is the timeout: /usr/bin/perl is present on
# macOS and Linux, and `timeout(1)` is not on macOS (measured — it is coreutils,
# not BSD).
# ---------------------------------------------------------------------------
run_timeout_case() {
  local desc="$1" payload="$2"
  local slow_exit=0 slow_err slow_verdict
  slow_err="$(printf '%s' "$payload" \
    | perl -e 'alarm 10; exec @ARGV' bash "$HOOK" 2>&1 >/dev/null)" || slow_exit=$?
  case "$slow_err" in
    *"An engine input was edited"*) slow_verdict=fire ;;
    *) slow_verdict=silent ;;
  esac
  if [ "$slow_verdict" = "fire" ] && [ "$slow_exit" -eq 0 ]; then
    echo "PASS: $desc (fire, exit 0)"
    pass=$((pass + 1))
  else
    echo "FAIL: $desc — expected fire/exit 0 within 10s, got $slow_verdict/exit $slow_exit"
    echo "      (exit 142 = SIGALRM, i.e. the hook blew its timeout and produced no nudge)"
    fail=$((fail + 1))
  fi
}

many_slashes="$(printf '%*s' 6000 '' | tr ' ' '/')"
run_timeout_case "6000 separators immediately before the basename, within hooks.json's timeout" \
  "$(printf '{"tool_name":"Write","tool_input":{"file_path":"/kb/facts%saccepted.dl"}}' "$many_slashes")"

many_dots=""
dot_i=0
while [ "$dot_i" -lt 4000 ]; do
  many_dots="$many_dots/."
  dot_i=$((dot_i + 1))
done
run_timeout_case "4000 '/.' components immediately before the basename, within hooks.json's timeout" \
  "$(printf '{"tool_name":"Write","tool_input":{"file_path":"/kb/facts%s/accepted.dl"}}' "$many_dots")"

# ---------------------------------------------------------------------------
# CASE 36: a JSON-escaped forward slash. `\/` is a legal JSON escape for `/`,
# so the decoded target is an ordinary engine-input path — the extractor gets
# the decoded string and never sees the escape.
#
# The pre-#337 grep searched the RAW payload text, where the escape is still
# there, so `facts\/accepted.dl` did not match `facts/accepted.dl` and it stayed
# silent. Reading the decoded target is what fixes it.
# ---------------------------------------------------------------------------
run_case "JSON-escaped forward slash in the target — fire" \
  '{"tool_name":"Write","tool_input":{"file_path":"\/kb\/facts\/accepted.dl"}}' fire

# ---------------------------------------------------------------------------
# CASES 37-38: the other two PATH_KEYS, pinned the way CASE 12b pins the
# top-level fallback — by wanting SILENT.
#
# The obvious shape (key names an engine input, expect fire) does NOT pin them:
# delete `path` from PATH_KEYS and no target is read, so the payload-wide grep
# fires on the same payload for a different reason and the case still passes.
# Measured — that version left the suite fully green under both deletions,
# which is the same masking CASE 12 documents.
#
# So each case names an UNRELATED target through the key and mentions an engine
# input only in content. Read the key and the verdict is silent; lose the key
# and the fallback fires. `file_path` is the only key a real Write/Edit sends,
# so these two are defensive — but they were unpinned here AND in
# tests/test_gate_check.sh, and an unpinned key is one nobody notices losing at
# #359, where the two extractors become one.
# ---------------------------------------------------------------------------
run_case "target read from the 'path' key — silent" \
  '{"tool_name":"Write","tool_input":{"path":"/tmp/notes.md","content":"see facts/accepted.dl"}}' silent
run_case "target read from the 'notebook_path' key — silent" \
  '{"tool_name":"NotebookEdit","tool_input":{"notebook_path":"/tmp/nb.ipynb","content":"see facts/query.dl"}}' silent

# ---------------------------------------------------------------------------
# CASES 42-43: A SHARED EXTRACTOR THAT WILL NOT LOAD FALLS BACK, NOT SILENT (#359).
#
# Since #359 the extractor lives in hooks/gate_payload.sh, shared with
# hooks/gate_check.sh and sourced by absolute path off the hook's own directory.
# If it cannot be loaded, this hook must land where a broken interpreter already
# lands (CASE 16): target empty, payload-wide grep decides, exit 0. That is the
# OPPOSITE of what gate_check.sh does with the same condition — there a missing
# extractor DENIES, because a write it cannot read cannot be shown to be safe;
# here it costs one line of output, so the hook keeps talking.
#
# The payload is #337's false positive: an unrelated target whose CONTENT names
# an engine input. With the library present that is SILENT — the whole point of
# #337. With it missing the grep fires. One payload, two verdicts, so the pair
# proves the fallback ran rather than that the hook fires on everything.
#
# A PREMISE WORTH WRITING DOWN: in the shipped configuration the fire arm is not
# observable. hooks.json registers gate_check.sh on PreToolUse for the same
# Write|Edit matcher, and a missing gate_payload.sh makes THAT hook deny every
# such call (branch 1b), so the tool never runs and this PostToolUse hook never
# fires at all. The #337 false positive really does come back when the library
# goes missing, but only for someone running this hook on its own — which is what
# this case does. It pins the hook's own contract, not a user-visible regression.
#
# A fake hooks/ directory holding gate_reminder.sh alone, rather than deleting
# the real library, so the rest of the suite and any concurrent run are
# untouched.
#
# FACTLOG_PYTHON_RUNNER is pinned at the real runner for BOTH halves. The hook
# locates tools/factlog_python.sh relatively to itself too, so a bare temp
# directory takes the interpreter away along with the library — and then the
# first half fires because there is no Python, which is CASE 16's condition, not
# this one. Measured: without this, the second half fires as well and the pair
# stops distinguishing anything.
# ---------------------------------------------------------------------------
NOLIB_HOOKS="$(mktemp -d)"
cp "$HOOK" "$NOLIB_HOOKS/gate_reminder.sh"      # deliberately NOT gate_payload.sh
REAL_RUNNER="$(cd "$(dirname "$0")/.." && pwd)/tools/factlog_python.sh"
FP_PAYLOAD='{"tool_name":"Write","tool_input":{"file_path":"/tmp/notes.md","content":"see facts/accepted.dl"}}'

nolib_exit=0
nolib_err="$(printf '%s' "$FP_PAYLOAD" \
  | FACTLOG_PYTHON_RUNNER="$REAL_RUNNER" bash "$NOLIB_HOOKS/gate_reminder.sh" 2>&1 >/dev/null)" || nolib_exit=$?
case "$nolib_err" in
  *"An engine input was edited"*) nolib_fired=fire ;;
  *) nolib_fired=silent ;;
esac
if [ "$nolib_fired" = "fire" ] && [ "$nolib_exit" -eq 0 ]; then
  echo "PASS: missing hooks/gate_payload.sh — falls back to the payload grep, still exit 0 (fire, exit 0)"
  pass=$((pass + 1))
else
  echo "FAIL: missing hooks/gate_payload.sh — expected fire/exit 0, got $nolib_fired/exit $nolib_exit"
  fail=$((fail + 1))
fi

cp "$(cd "$(dirname "$0")/.." && pwd)/hooks/gate_payload.sh" "$NOLIB_HOOKS/gate_payload.sh"
lib_exit=0
lib_err="$(printf '%s' "$FP_PAYLOAD" \
  | FACTLOG_PYTHON_RUNNER="$REAL_RUNNER" bash "$NOLIB_HOOKS/gate_reminder.sh" 2>&1 >/dev/null)" || lib_exit=$?
case "$lib_err" in
  *"An engine input was edited"*) lib_fired=fire ;;
  *) lib_fired=silent ;;
esac
if [ "$lib_fired" = "silent" ] && [ "$lib_exit" -eq 0 ]; then
  echo "PASS: the same payload WITH hooks/gate_payload.sh — silent (silent, exit 0)"
  pass=$((pass + 1))
else
  echo "FAIL: the same payload WITH hooks/gate_payload.sh — expected silent/exit 0, got $lib_fired/exit $lib_exit"
  fail=$((fail + 1))
fi
rm -rf "$NOLIB_HOOKS"

# ---------------------------------------------------------------------------
# CASE 44: A NUL IN tool_name DOES NOT COST THE NUDGE (#359).
#
# This hook used to run its own extractor, which emitted the target as the ONLY
# field, so nothing could shift it. Sharing gate_check.sh's three-field record
# would have put the nudge behind two other fields — and a NUL inside a JSON
# string pushes every later field along by one, so a NUL in `tool_name` would
# have moved the real path out of `target` and the nudge would have gone silent.
# The shared record writes `target` FIRST for exactly this reason.
#
# Under-firing is the direction this hook's header calls the one that costs
# something, and it is what a naive de-duplication would have bought. Reordering
# the fields in hooks/gate_payload.sh turns this case red.
# ---------------------------------------------------------------------------
run_case "NUL inside tool_name — target still read, nudge survives" \
  '{"tool_name":"Wr\u0000ite","tool_input":{"file_path":"/kb/facts/accepted.dl"}}' fire

# ---------------------------------------------------------------------------
# CASE 45: A gate_payload.sh IN THE CWD IS NOT SOURCED (#359).
#
# Sourcing runs arbitrary code, so the library is read from the directory this
# SCRIPT lives in and nowhere else. The one invocation form where
# `${BASH_SOURCE[0]}` carries no path — `bash gate_reminder.sh` from inside some
# directory — must therefore skip the source rather than fall back to ".".
#
# #359 is what made this worth closing. Before it the nearest thing this hook
# sourced was `$HOOK_DIR/../tools/factlog_python.sh`, in a PARENT directory; the
# shared library brought a sourced path down into the SAME directory a decoy
# would sit in.
#
# The decoy here is a library that reports a harmless target. If it were sourced,
# the engine-input payload would extract "/decoy/..." and the hook would go
# SILENT. It must fire — via the documented empty-target fallback — instead.
# Removing `_hook_dir_is_script_dir` from the source guard turns this case red.
# ---------------------------------------------------------------------------
CWD_DECOY="$(mktemp -d)"
cp "$HOOK" "$CWD_DECOY/gate_reminder.sh"
cat > "$CWD_DECOY/gate_payload.sh" <<'DECOY'
factlog_hook_read_payload() {
  FACTLOG_HOOK_TARGET_PATH="/decoy/not/an/engine/input"
  FACTLOG_HOOK_TOOL_NAME="Write"
  FACTLOG_HOOK_TOOL_INPUT_KIND="object"
  return 0
}
DECOY
ENGINE_PAYLOAD='{"tool_name":"Write","tool_input":{"file_path":"/kb/facts/accepted.dl","content":"a"}}'

decoy_exit=0
decoy_err="$(cd "$CWD_DECOY" && printf '%s' "$ENGINE_PAYLOAD" \
  | FACTLOG_PYTHON_RUNNER="$REAL_RUNNER" bash gate_reminder.sh 2>&1 >/dev/null)" || decoy_exit=$?
case "$decoy_err" in
  *"An engine input was edited"*) decoy_fired=fire ;;
  *) decoy_fired=silent ;;
esac
if [ "$decoy_fired" = "fire" ] && [ "$decoy_exit" -eq 0 ]; then
  echo "PASS: bare-name invocation ignores a gate_payload.sh in the cwd (fire, exit 0)"
  pass=$((pass + 1))
else
  echo "FAIL: bare-name invocation — expected fire/exit 0, got $decoy_fired/exit $decoy_exit"
  fail=$((fail + 1))
fi

# CONTROL: the identical decoy IS sourced when the hook is reached by a path, so
# the case above is not passing merely because the decoy is inert.
ctl_exit=0
ctl_err="$(printf '%s' "$ENGINE_PAYLOAD" \
  | FACTLOG_PYTHON_RUNNER="$REAL_RUNNER" bash "$CWD_DECOY/gate_reminder.sh" 2>&1 >/dev/null)" || ctl_exit=$?
case "$ctl_err" in
  *"An engine input was edited"*) ctl_fired=fire ;;
  *) ctl_fired=silent ;;
esac
if [ "$ctl_fired" = "silent" ] && [ "$ctl_exit" -eq 0 ]; then
  echo "PASS: control — the same decoy IS sourced via a path, and silences the nudge (silent, exit 0)"
  pass=$((pass + 1))
else
  echo "FAIL: control — expected the decoy to silence the nudge, got $ctl_fired/exit $ctl_exit"
  fail=$((fail + 1))
fi
rm -rf "$CWD_DECOY"

echo "---"
echo "gate_reminder: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
