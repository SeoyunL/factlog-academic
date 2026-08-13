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
# SHARED WITH gate_check.sh since #359. Both hooks read the same envelope with
# the same key precedence, and they now do it with the same code:
# hooks/gate_payload.sh, sourced below. Between #337 and #359 this hook carried
# a second COPY of that extractor — #338 was rewriting gate_check.sh's freshness
# predicate at the time and two simultaneous edits to a security gate was the
# worse risk — which is the arrangement #337 itself was filed against. What is
# left here is only what this hook does with the target once it has it, which is
# genuinely different from what gate_check.sh does with it; see the next
# paragraph.
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
#     the engine input and gets no nudge. Redundant separators and `.`
#     components ARE handled, in every mix of the two separators — `//`, `/./`,
#     `\\`, `\.\`, `\/`, `/\`, `/.\`, `\./` — because the matcher reads the tail
#     rather than enumerating forms. `..` is not, because resolving it by string
#     surgery is WRONG when a component is a symlink: a/b/../c is not a/c if b
#     is a link. Doing it correctly needs the filesystem, which is the
#     interpreter spawn this hook exists without.
# All three were already true of the payload grep this replaces, so none is a
# regression; they are listed because the list used to read as though it were
# closed, and a reader was entitled to assume anything absent was handled.
# The backslash forms are in that list for the same reason: they were silent
# until this round, on the one platform the backslash splits exist to serve.
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
# whole. Roughly 11 ms/invocation before #337, roughly 90 ms after (20
# iterations of an unrelated Write payload). Only the ~8x ratio is stable: the
# same code measured 10.9 -> 97.4 in one run and 14.8 -> 110.5 in another, so a
# third significant figure here would be reporting host load, not cost.
#
# That is the price of not nudging on every document that mentions a KB path,
# and it is charged on Write/Edit events that already spawn Python several
# times. Counted by wrapping FACTLOG_PYTHON_RUNNER and tallying: gate_check.sh
# spawns three on an unrelated Write and four on an engine-input one; this hook
# spawns one. So it is the FOURTH spawn on an unrelated write and the fifth on
# an engine-input write — not a spawn added to an event that had none.
# Declining the KB-root resolution is what keeps this hook's own count at one.
#
# The spawn is the whole of the cost for any path a filesystem can hand over.
# It is not an unconditional bound: cost still depends on the path's SHAPE, and
# a separator run sitting immediately before the basename is quadratic in the
# length of that run — 0.44s at 6000 separators, 12s at 32000. What makes the
# spawn dominant is PATH_MAX, which puts a real payload near 1000, where the
# matching costs 0.013s against the spawn's ~90ms.
#
# A rewriting collapse used to make this far worse — superlinear at every
# shape, and on thousands of repeated separators it ran past hooks.json's
# `timeout: 10` and killed the nudge outright. That is gone. The matcher's own
# comment has both sets of measurements. Any future edit that reintroduces
# per-character work on the whole path reopens it, and CASES 35 and 35b of
# tests/test_gate_reminder.sh are what notice.
#
# WHEN THE TARGET CANNOT BE READ (no usable Python, an unparseable payload, an
# envelope with no path key at all, or — since #359 — a shared extractor that
# will not load) it falls back to the payload-wide grep —
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

# Read the tool target out of the hook payload (issues #337, #359).
#
# The extractor is hooks/gate_payload.sh, SHARED with hooks/gate_check.sh. Its
# header carries the reasoning — the envelope shape, the `tool_input`-before-
# top-level key precedence, the NUL framing. Until #359 this hook carried a
# COPY of it, because #338 was rewriting gate_check.sh at the same time and two
# simultaneous edits to a security gate was the worse risk. The copy is gone:
# #337 exists precisely because a second reader of this envelope drifted from
# the real one, and a second copy of the fixed reader schedules the same bug
# again.
#
# Loaded by ABSOLUTE path off $HOOK_DIR, not relatively. hooks.json invokes this
# hook as `"${CLAUDE_PLUGIN_ROOT}"/hooks/gate_reminder.sh` and Claude Code runs
# it with the SESSION's cwd — the user's project, not the plugin — so a relative
# source would read a file out of whatever project the model is editing.
#
# The shared record has three fields; this hook uses only `target`. The other
# two exist for gate_check.sh, which has to tell a payload with no `tool_input`
# object apart from one that has an object with no path key in it. Reading a
# record it partly ignores is the price of there being ONE record shape.
#
# IF THE LIBRARY CANNOT BE LOADED the target stays empty, which routes to the
# payload-wide grep below — the pre-#337 behaviour, exactly where a broken
# interpreter and an unparseable payload already land. That is the OPPOSITE of
# what gate_check.sh does with the same condition, and deliberately so: there a
# missing extractor means a write cannot be shown to be safe, so it denies;
# here it means one line of output might be wrong, so it errs toward still
# saying something. This hook must not turn an install problem into silence.
GATE_PAYLOAD_LIB="$HOOK_DIR/gate_payload.sh"

target_path=""
if [ -r "$GATE_PAYLOAD_LIB" ] \
  && . "$GATE_PAYLOAD_LIB" \
  && declare -f factlog_hook_read_payload >/dev/null 2>&1; then
  factlog_hook_read_payload "$payload" "${PYTHON_RUNNER[@]}"
  target_path="$FACTLOG_HOOK_TARGET_PATH"
fi

# Is this path one of the four engine inputs, judged by its last two components?
#
# Both separators are honoured: under Git Bash a payload carries
# "C:\kb\facts\query.dl", and every expansion below treats `/` and `\` alike via
# the bracket set `[/\\]`. All three are pinned by tests/test_gate_reminder.sh —
# measured, reverting one bracket set at a time to the forward-slash-only form
# turns 8 cases red in the basename set, 10 in the tail set and 6 in the pdir
# set (and 8 in the `[/\\]*` case arm on `tail`, a fourth site someone could
# edit alone). CASES 17-19 there list which cases and why.
#
# An earlier version of this comment claimed backslash handling "cannot be
# pinned from a POSIX host". That was false, and the way it was false is worth
# keeping: the suite WAS green with the old split lines deleted, and I read that
# as evidence about the host instead of as a gap in the suite. Nothing here
# touches the filesystem — `_is_engine_input_path` is pure string manipulation —
# so a payload carrying backslashes behaves identically on any host, and the
# cases run anywhere. "Deleting the line leaves the suite green" only ever meant
# the suite had no case for it.
#
# What backslash handling costs on POSIX is one false positive, not zero: a
# single file literally named `facts\accepted.dl` nudges where the pre-#337 grep
# did not. That is the trade — Windows coverage for one spurious line on a
# filename almost nobody writes — and it is a wrong-nudge, the cheap direction.
#
# HOW THE TWO COMPONENTS ARE READ, and why there is no rewriting loop.
#
# An earlier version collapsed redundant separators by rewriting the WHOLE path
# repeatedly (`${path//\/\//\/}` in a loop). It was superlinear and it did not
# merely get slow, it broke the hook: hooks.json gives this hook `timeout: 10`,
# and on "/kb" + N slashes + "facts/accepted.dl" the collapse took 3.5s at
# N=4000, 12.0s at N=6000 — past the timeout, so the process is killed, NO nudge
# appears, and the payload-grep fallback never runs either. That is a silent
# non-firing, the one direction this hook must not fail in, and it contradicted
# the header's claim that failure leans toward firing. At N=20000 it was 406s.
#
# Nothing is rewritten now. Redundant separators and "." components are skipped
# by READING the tail, in a fixed number of parameter expansions with no loop.
#
# That removes the loop but NOT the dependence on the path's shape, and the
# honest number depends on where the run sits. `##` is greedy and bash walks
# prefixes longest-first, so a run followed by another component matches near
# the full length and scans effectively linearly, while a run sitting
# immediately before the basename fails at every prefix and scans quadratically.
# Measured on bash 3.2.57, matcher only, seconds per call:
#
#   N        "/kb" + run + "facts/accepted.dl"   "/kb/facts" + run + "accepted.dl"
#   1000     0.0007                              0.013
#   6000     0.0035                              0.436
#   20000    0.011                               4.771
#   32000    0.018                              12.239
#
# So the quadratic column is 300x cheaper than the collapse at N=6000 (0.436s
# against 12.0s) but it is not a constant, and at N=32000 it would blow the
# same timeout the collapse blew. That N is unreachable: PATH_MAX bounds a real
# payload near N=1000, where the cost is 0.013s. The bound worth stating is
# therefore conditional, not absolute — see tests/test_gate_reminder.sh CASES 35
# and 35b, which pin the expensive shape rather than the flat one.
#
#   base   = everything after the last separator of EITHER kind. `##*[/\\]` is
#            greedy, so one expansion crosses any number of separators.
#   tail   = the run of separators and dots that trails the parent, found as
#            "everything after the last character that is not one of / \ ." —
#            again one greedy expansion, however long the run.
#   pdir   = the last component of the parent once that tail is removed.
#
# So `//`, `/./`, `\\`, `\.\` AND the mixed forms `\/`, `/\`, `/.\`, `\./` all
# fall out of one rule rather than four rewrites; the mixed forms were silent
# before this and are not enumerated anywhere, because nothing here needs to
# know which kind of separator it crossed.
#
# Skipping them cannot change the verdict, and that is the exact claim: it
# cannot change THE LAST TWO COMPONENTS, which is all this matcher reads. The
# broader claim would be false — a pathname beginning with exactly two slashes
# is implementation-defined in POSIX and MSYS2/Git Bash reads `//server/share`
# as UNC, so collapsing a leading `//` CAN change which file a path denotes.
#
# TWO shapes are rejected rather than skipped, both by inspecting `tail`:
#   - a `..` anywhere in it. a/b/../c is not a/c when b is a symlink, so
#     resolving it needs the filesystem this hook does not touch. Rejecting is
#     the safe direction: facts/x/../accepted.dl gets no nudge, and so does
#     facts/../accepted.dl, which does NOT denote an engine input and would be a
#     false positive if `..` were skipped like `.`;
#   - a tail that does not begin with a separator, which means the trailing dots
#     belong to the component itself: `/kb/facts./accepted.dl` sits in a
#     directory named `facts.`, not `facts`, and must stay silent.
#
# A trailing separator still matches nothing: it makes `base` empty, which fails
# the basename check immediately — right twice over, since such a name denotes a
# directory, not a shape Write/Edit can act on. Comparing COMPONENTS rather than
# searching for a substring is what rules out "…/facts/query.dl.bak" and
# "…/myfacts/query.dl", both of which the old payload grep nudged on.
_is_engine_input_path() {
  local path="$1"
  local base parent tail pdir

  # Basename first: it is one expansion and one `case`, and it rejects almost
  # every write this hook sees without touching the rest of the path.
  base="${path##*[/\\]}"
  case "$base" in
    query.dl|candidates.csv|accepted.dl|logic-policy.dl) ;;
    *) return 1 ;;
  esac

  parent="${path%"$base"}"
  tail="${parent##*[!/\\.]}"
  case "$tail" in
    "") ;;
    *..*) return 1 ;;
    [/\\]*) parent="${parent%"$tail"}" ;;
    *) return 1 ;;
  esac
  pdir="${parent##*[/\\]}"

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
