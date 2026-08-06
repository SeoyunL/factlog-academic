#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# factlog deterministic gate — SCAFFOLD (non-blocking).
#
# Fires after Write/Edit. If an engine input was just edited by the model, it
# reminds the session to run the deterministic logic check and show the report
# verbatim before concluding — the core factlog rule ("the agent does not draw
# conclusions; the CLI returns the verifiable report").
#
# This is a nudge only. The enforcing version (block conclusions until
# run_logic_check.py has run and logic_report.txt is shown) is authored in the
# delivery plan (T3) using a PreToolUse deny on the relevant action. A Stop hook
# cannot block, so enforcement must sit on a tool action, not on completion.
#
# WHAT IT LOOKS AT (issue #337). It reads the TARGET path out of the payload and
# matches on that. It used to grep the whole payload string, so a write whose
# CONTENT merely mentioned facts/accepted.dl nudged even though the target was
# an unrelated file. The blast radius of that was zero — nothing is blocked and
# the exit code is ignored — but a nudge that fires on writes it has no business
# with trains the reader to skip the line, and then it is not there when it
# matters. This is the same defect #323 fixed in hooks/gate_check.sh.
#
# NOT SHARED WITH gate_check.sh, yet. Both hooks now read the same envelope with
# the same key precedence, and #337 asks for one extractor sourced by both. That
# refactor touches gate_check.sh, which is under change elsewhere, so the
# extraction below is deliberately a duplicate for now and is the thing to
# delete when the shared helper lands.
#
# HOW MUCH IT CHECKS. Far less than gate_check.sh, on purpose. gate_check.sh
# resolves the active KB root, canonicalises the path, and asks the filesystem
# about hard links and case folding, because it DENIES and a wrong answer costs
# the user a blocked write. This hook only prints a line, so it matches on the
# last two components of the target path and stops there. Known consequences,
# all of them "no nudge" rather than "wrong nudge":
#   - an engine input reached through a symlink or a second hard link is not
#     recognised (gate_check.sh still guards that write);
#   - on a case-folding filesystem facts/Accepted.dl IS the engine input, and
#     the match here is case-sensitive, so it is not recognised;
#   - the match is not KB-root aware: ANY path ending in facts/accepted.dl and
#     friends nudges, including one in a directory that is not a KB at all.
# The first two were already true of the payload grep this replaces; the third
# is the grep's behaviour kept deliberately, since the nudge costs a line and
# resolving a root costs a second interpreter spawn on every write.
#
# WHEN THE TARGET CANNOT BE READ (no usable Python, an unparseable payload, or
# an envelope with no path key at all) it falls back to the payload-wide grep —
# i.e. to the pre-#337 behaviour. That direction is the opposite of the one
# gate_check.sh takes for a comparable blind spot, and deliberately so: there,
# guessing costs a blocked write, so it falls open; here, guessing costs one
# extra line of output, so it errs toward still saying something. The fallback
# can only over-fire, never under-fire, relative to what shipped before.
#
# No `set -e`/`set -u` here, unlike gate_check.sh. Every unset or failed step
# below leaves the target empty, which routes to that same fallback, so the
# permissive-in-the-nudge-direction behaviour holds without the risk that a
# stray non-zero status turns this reminder into a reported hook failure.

payload="$(</dev/stdin)"

_hook_dir="${BASH_SOURCE[0]}"
case "$_hook_dir" in
  */*) _hook_dir="${_hook_dir%/*}" ;;
  *) _hook_dir="." ;;
esac
HOOK_DIR="$(cd "$_hook_dir" && pwd)"
PYTHON_RUNNER_SCRIPT="${FACTLOG_PYTHON_RUNNER:-"$HOOK_DIR/../tools/factlog_python.sh"}"
PYTHON_RUNNER=( "${BASH:-bash}" "$PYTHON_RUNNER_SCRIPT" )

# Extract the tool target from the hook payload.
#
# Claude Code sends an ENVELOPE on stdin, not the bare tool input:
#   {"session_id":..,"cwd":..,"hook_event_name":"PostToolUse","tool_name":"Write",
#    "tool_input":{"file_path":..,"content":..},"tool_response":{..}}
# so the target path lives under `tool_input`. Key precedence matches
# gate_check.sh: `tool_input` first, then the TOP LEVEL as a fallback for the
# flat fixture shape the tests use. `notebook_path` is defensive only —
# hooks.json registers the matcher "Write|Edit", compared by exact tool name, so
# NotebookEdit never reaches this hook.
#
# `tool_input` FIRST is load-bearing, not cosmetic: the whole point of #337 is
# that the path which decides the nudge must be the one the tool actually wrote
# to, and that is the nested one.
#
# The field is NUL-terminated and read straight off a pipe rather than through
# `$(...)`, because command substitution drops NUL bytes and strips trailing
# newlines — and a path may legally contain a newline.
GATE_EXTRACT_PY="
import json, sys
PATH_KEYS = (\"file_path\", \"path\", \"notebook_path\")
try:
    payload = json.load(sys.stdin)
except Exception:
    payload = None
target = \"\"
try:
    nested = payload.get(\"tool_input\") if isinstance(payload, dict) else None
    for source in (nested, payload):
        if not isinstance(source, dict):
            continue
        for key in PATH_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value:
                target = value
                break
        if target:
            break
except Exception:
    target = \"\"
sys.stdout.write(target + \"\\0\")
"

target_path=""
if ! IFS= read -r -d '' target_path \
    < <(printf '%s' "$payload" | "${PYTHON_RUNNER[@]}" -c "$GATE_EXTRACT_PY" 2>/dev/null); then
  target_path=""
fi

# Is this path one of the four engine inputs, judged by its last two components?
#
# Both separators are honoured: under Git Bash a payload carries
# "C:\kb\facts\query.dl", where `${path##*/}` hands back the whole string.
# Neither backslash split is pinned by tests/test_gate_reminder.sh, and this is
# measured, not assumed: deleting either line leaves that file fully green. They
# cannot be pinned from a POSIX host, where a backslash is an ordinary filename
# character, so they are asserted by construction — the same standing as the
# equivalent split in gate_check.sh's prefilter. Losing them costs a nudge on
# Windows; it cannot cause a false one. The trailing-separator strip below is a
# different matter and IS pinned (delete it and six cases go red), because
# without it every parent directory reads as empty and nothing ever matches.
#
# Nothing is stripped from the path before splitting. A trailing separator
# therefore yields an empty basename and matches nothing, which is the right
# answer twice over — such a name denotes a directory, which is not a shape
# Write/Edit can act on. Comparing COMPONENTS rather than searching for a
# substring is what rules out "…/facts/query.dl.bak" and "…/myfacts/query.dl",
# both of which the old payload grep nudged on.
_is_engine_input_path() {
  local path="$1"
  local base parent pdir
  base="${path##*/}"
  base="${base##*\\}"
  parent="${path%"$base"}"
  parent="${parent%[/\\]}"
  pdir="${parent##*/}"
  pdir="${pdir##*\\}"
  case "$pdir/$base" in
    facts/query.dl|facts/candidates.csv|facts/accepted.dl|policy/logic-policy.dl)
      return 0 ;;
  esac
  return 1
}

should_remind=false
if [ -n "$target_path" ]; then
  if _is_engine_input_path "$target_path"; then
    should_remind=true
  fi
elif printf '%s' "$payload" | grep -Eq 'facts/(query\.dl|candidates\.csv|accepted\.dl)|policy/logic-policy\.dl'; then
  # No target could be read: fall back to the pre-#337 payload-wide grep. See
  # the header for why this branch errs toward firing.
  should_remind=true
fi

if [ "$should_remind" = true ]; then
  echo "[factlog] An engine input was edited. Run the logic check before concluding:" >&2
  echo "          \"\${CLAUDE_PLUGIN_ROOT}\"/tools/factlog_python.sh \"\${CLAUDE_PLUGIN_ROOT}\"/tools/run_logic_check.py" >&2
  echo "          then show facts/logic_report.txt verbatim. Candidates are not engine input until confirmed." >&2
fi

exit 0
