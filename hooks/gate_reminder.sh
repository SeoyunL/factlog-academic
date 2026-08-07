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
# NOT SHARED WITH gate_check.sh, and that is a KNOWN COST, not an oversight.
# Both hooks now read the same envelope with the same key precedence, so the
# extractor below is a second copy of gate_check.sh's — and #337 exists because
# a second, sloppier reader of this envelope drifted from the real one. A third
# copy schedules the next divergence; saying "we will de-duplicate later" is the
# promise that produced this bug.
#
# It is duplicated anyway because #338 is changing gate_check.sh's freshness
# predicate right now (a report that records the engine did not run must stop
# counting as a valid result), and two simultaneous refactors of a security gate
# is the worse risk. The de-duplication is #359, which names #338 as its
# predecessor — a tracked item with an unblocking condition, not a comment.
# Whoever takes #359 deletes the extractor below and sources the shared one.
#
# HOW MUCH IT CHECKS. Far less than gate_check.sh, on purpose. gate_check.sh
# resolves the active KB root, canonicalises the path, and asks the filesystem
# about hard links and case folding, because it DENIES and a wrong answer costs
# the user a blocked write. This hook only prints a line, so it matches on the
# last two components of the target path and stops there.
#
# UNDER-FIRES (a genuine engine input that gets no nudge). This is the direction
# that costs something, because the nudge is the only signal for
# facts/candidates.csv and policy/logic-policy.dl — gate_check.sh does not guard
# those two at all, so there is no backstop behind them:
#   - an engine input reached through a symlink or a second hard link is not
#     recognised (for accepted.dl/query.dl gate_check.sh still guards the write;
#     for the other two, nothing does);
#   - on a case-folding filesystem facts/Accepted.dl IS the engine input, and
#     the match here is case-sensitive, so it is not recognised;
#   - a path that needs `..` resolution is missed: facts/x/../accepted.dl names
#     the engine input and gets no nudge. `//` and `/./` ARE handled (see the
#     matcher), but `..` is not, because collapsing it by string surgery is
#     WRONG when a component is a symlink — a/b/../c is not a/c if b is a link.
#     Doing it correctly needs the filesystem, which is the interpreter spawn
#     this hook exists without.
# All four were already true of the payload grep this replaces, so none is a
# regression; they are listed because the list used to read as though it were
# closed, and a reader was entitled to assume anything absent was handled.
#
# OVER-FIRES (a nudge on something that is not an engine input). Deliberate,
# and cheap — a wrong nudge costs one line of output:
#   - the match is not KB-root aware: ANY path ending in facts/accepted.dl and
#     friends nudges, including one in a directory that is not a KB at all.
#     That is the grep's behaviour kept on purpose, since resolving a root costs
#     an interpreter spawn on every write;
#   - on POSIX, a single file whose NAME contains a backslash — literally
#     `facts\accepted.dl`, one directory entry — nudges, because the matcher
#     splits on backslash for Git Bash's sake. The pre-#337 grep did not fire on
#     it. Such a filename is vanishingly rare and the cost is one line, which is
#     why the Windows support is worth it; see the matcher for the trade.
#
# COST, both sides of it. The paragraph above declines to resolve a KB root
# because that costs an interpreter spawn — but reading the target structurally
# introduces the FIRST spawn this hook ever paid, so the ledger has to be stated
# whole. Measured on this host, 20 iterations of an unrelated Write payload:
# 10.9 ms/invocation before #337, 97.4 ms after. That is the price of not
# nudging on every document that mentions a KB path. It is charged on the same
# Write/Edit events where gate_check.sh already spawns Python twice, so it adds
# a third spawn to an event that had two, rather than a spawn to an event that
# had none. Declining the KB-root resolution keeps it at one rather than two.
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
# BOTH backslash splits are pinned by tests/test_gate_reminder.sh. Measured:
# deleting `base="${base##*\\}"` turns three cases red, deleting
# `pdir="${pdir##*\\}"` turns one red.
#
# An earlier version of this comment claimed they "cannot be pinned from a POSIX
# host". That was false, and the way it was false is worth keeping: the suite
# WAS green with either line deleted, and I read that as evidence about the host
# instead of as a gap in the suite. Nothing here touches the filesystem —
# `_is_engine_input_path` is pure string manipulation — so a payload carrying
# backslashes behaves identically on any host, and the cases below run anywhere.
# "Deleting the line leaves the suite green" only ever meant the suite had no
# case for it.
#
# What the split costs on POSIX is one new false positive, not zero: a single
# file literally named `facts\accepted.dl` now nudges where the pre-#337 grep did
# not. That is the trade — Windows coverage for one spurious line on a filename
# almost nobody writes — and it is a wrong-nudge, the cheap direction.
#
# The trailing-separator strip below is pinned hardest of all: delete it and 14
# cases go red, 6 of them CONTROL, because without it every parent directory
# reads as empty and nothing ever matches.
#
# NORMALISATION is deliberately partial. `//` and `/./` are collapsed, because
# both are pure string rewrites that cannot change which file a path denotes.
# `..` is NOT, and must not be: a/b/../c is not a/c when b is a symlink, so
# collapsing it by string surgery would be wrong in exactly the case that
# matters. Resolving `..` needs the filesystem, and this hook does not touch it.
# So facts/x/../accepted.dl is a genuine engine input that gets no nudge; that
# is listed with the other under-fires in the header.
#
# Beyond that nothing is stripped. A trailing separator yields an empty basename
# and matches nothing, which is the right answer twice over — such a name
# denotes a directory, which is not a shape Write/Edit can act on. Comparing
# COMPONENTS rather than searching for a substring is what rules out
# "…/facts/query.dl.bak" and "…/myfacts/query.dl", both of which the old payload
# grep nudged on.
_is_engine_input_path() {
  local path="$1"
  local base parent pdir
  # Collapse "//" and "/./" (see NORMALISATION above). Loop because one pass
  # leaves "///" and "/././" half-done: each rewrite can create a fresh match.
  #
  # The separators go through variables rather than being written inline. In
  # `${var//pattern/replacement}` the replacement must be UNQUOTED — bash 3.2,
  # the floor this repo supports, inserts the quote characters literally if you
  # quote it (`${p//"$a"/"$b"}` yields `"/"`, measured on 3.2.57) — while the
  # pattern must be QUOTED to stay literal. Writing the slashes inline instead
  # needs backslash escapes that 3.2 turns into a literal backslash. Both wrong
  # forms produce a path that still matches nothing, i.e. they fail silently in
  # the under-fire direction, which is why this is spelled out.
  local sl="/" dbl="//" dotd="/./"
  while :; do
    case "$path" in
      *"$dbl"*) path="${path//"$dbl"/$sl}" ;;
      *"$dotd"*) path="${path//"$dotd"/$sl}" ;;
      *) break ;;
    esac
  done
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
