# SPDX-License-Identifier: Apache-2.0
# factlog hook payload extractor — SHARED by hooks/gate_check.sh and
# hooks/gate_reminder.sh (issue #359).
#
# This file is SOURCED, never executed, so it carries no shebang and no
# `set` line: it must behave identically inside gate_check.sh, which runs under
# `set -euo pipefail`, and inside gate_reminder.sh, which deliberately runs
# under none of them. Everything below is a definition; nothing here exits,
# prints, or reads stdin on its own. Each caller decides what a failure means,
# and the two callers decide it OPPOSITELY — see "FAILURE IS THE CALLER'S" at
# the bottom.
#
# WHY IT IS SHARED. #337 was a defect of exactly this shape: hooks/gate_reminder.sh
# read the same envelope with a second, sloppier reader (a grep over the whole
# payload) and it disagreed with the real one, so the nudge fired on writes whose
# CONTENT merely mentioned an engine input. #337 fixed the reader but COPIED
# gate_check.sh's extractor to do it, because #338 was rewriting gate_check.sh's
# freshness predicate at the same time and two simultaneous edits to a security
# gate is the worse risk. This file is the promised de-duplication: the key
# precedence and the NUL framing now exist once, so the next edit to either
# cannot desync them.
#
# HOW CALLERS FIND IT. By ABSOLUTE path, off the hook's own $HOOK_DIR. Both
# hooks are registered in hooks/hooks.json as
# `"${CLAUDE_PLUGIN_ROOT}"/hooks/<name>.sh`, and Claude Code runs them with the
# SESSION's cwd — the user's project directory, which is not the plugin and in
# general is nowhere near it. A relative `source hooks/gate_payload.sh` would
# therefore read whatever file happens to sit at that path under the project the
# model is editing, or nothing at all; the first of those is worse than the
# second. `$HOOK_DIR` is derived from `${BASH_SOURCE[0]}`, which Claude Code
# hands over absolute, and both hooks already depend on it to locate
# tools/factlog_python.sh.
#
# WHAT IT READS. Claude Code sends an ENVELOPE on stdin, not the bare tool input:
#   {"session_id":..,"cwd":..,"hook_event_name":"PreToolUse","tool_name":"Write",
#    "tool_input":{"file_path":..,"content":..},"tool_use_id":..}
# so the target path lives under `tool_input`. The pre-#323 extractor looked only
# at the top level, which is why every real payload fell through to its
# fail-open branch.
#
# KEY PRECEDENCE: `tool_input` first, then the TOP LEVEL as a fallback. Nested
# FIRST is load-bearing rather than cosmetic — the path that decides both the
# deny and the nudge has to be the one the tool will actually write to, and that
# is the nested one. No real Claude Code payload puts `file_path` at the top
# level; that fallback exists to keep the flat fixture shape used by
# tests/test_gate_check.sh and tests/test_gate_reminder.sh working.
# Within one source the order is `file_path`, `path`, `notebook_path`.
# `notebook_path` is defensive only: hooks.json registers the matcher
# "Write|Edit", which Claude Code compares by exact tool name, so NotebookEdit
# (and MultiEdit) never reach either hook. It costs nothing and covers a user who
# widens the matcher in their own settings.json.
#
# THREE FIELDS, NOT ONE. gate_reminder.sh only needs `target`, and its own copy
# emitted only that. Sharing means it now reads a record it partly ignores. That
# is the cheaper side of the trade: one record shape can only be desynced by
# editing this file, whereas two shapes can be desynced by editing either.
#   target          — the path the tool call will write to, "" if none was read.
#   tool_name       — carried RAW so the write-class decision stays a readable
#                     `case` in shell right next to hooks.json's matcher, and so
#                     a deny message can name the offending tool. Collapsing it
#                     into a derived flag is possible but moves that decision
#                     into embedded Python where it is harder to audit.
#   input_kind      — one of "unparsed", "absent", "object", "other"; it is how
#                     gate_check.sh tells "the payload had no tool_input object"
#                     apart from "it had one and no path key was in it", which is
#                     the difference between falling open and denying.
#
# EACH FIELD IS PULLED UNDER ITS OWN try/except and the record is always written
# whole, so a failure while reading one field cannot truncate the others.
#
# NUL FRAMING, and why `target` is FIRST. The separator is NUL because a path may
# legally contain a newline, and a newline separator would misread such a path —
# or, with a LEADING newline, deny a perfectly legal write. A JSON string can
# itself contain a NUL (`"\u0000"` parses), which pushes every LATER field along
# by one; the first field is the only one that cannot be shifted by another
# field's contents. `target` is the field both hooks base a decision on, so it
# takes that slot.
#
# That ordering is a change from the pre-#359 record, which led with `tool_name`,
# and it changes one verdict: a payload whose TOOL_NAME contains a NUL used to
# push the real path out of `target` and into `input_kind`, so gate_check.sh read
# a fragment of the tool name as the target and ALLOWED a write to an engine
# input. It now reads the path and denies. Nothing reachable is affected —
# `tool_name` is written by Claude Code, not by the model — but the direction is
# the safe one.
#
# What the ordering buys is NARROW, and stating it broadly would be a lie the
# next reader inherits. It buys exactly this: no OTHER field's contents can
# displace `target`. It does NOT make `target` equal to the payload's path key in
# every case, because there is a third state. A NUL inside the PATH truncates
# `target` at that NUL, so the hooks see a PREFIX of the real path — and which
# way that falls depends on where the NUL sits. Measured against a stale KB whose
# intact engine-input write denies with rc 2:
#   <KB>/facts/accepted.dl\0tail  ->  target = the whole engine input  ->  deny
#   <KB>/facts/acc\0epted.dl      ->  target = "<KB>/facts/acc"        ->  ALLOW
#   <KB>/fac\0ts/accepted.dl      ->  target = "<KB>/fac"              ->  ALLOW
# So a NUL landing before the end of the basename walks the gate past the
# engine-input match, silently. That is unreachable rather than harmless: the OS
# rejects any path containing a NUL, so the write this would allow cannot be
# performed. An earlier version of this comment called truncation "a wash"
# because "a truncated engine-input path still matches the engine input" — that
# is false in two of the three shapes above, and it was equally false on main.
#
# Fields are read straight off a pipe because bash command substitution silently
# drops NUL bytes and strips trailing newlines, so `$(...)` cannot capture this.
# A shell that could not parse process substitution would raise a SYNTAX error,
# and bash exits 2 on one — which PreToolUse reads as DENY, not as a fail-open.
# That is unreachable in practice (bash falls back to FIFOs where /dev/fd is
# missing, and macOS, Linux and Git Bash all support it); it is noted only so the
# failure direction is not misdescribed.
#
# The literal `PATH_KEYS` below is load-bearing beyond Python: CASE 37 of
# tests/test_gate_check.sh shims the interpreter and recognises the extractor
# call by matching `*PATH_KEYS*` in its arguments.
FACTLOG_HOOK_PAYLOAD_PY="
import json, sys
PATH_KEYS = (\"file_path\", \"path\", \"notebook_path\")
UNPARSED = object()
try:
    payload = json.load(sys.stdin)
except Exception:
    payload = UNPARSED
try:
    name = payload.get(\"tool_name\")
    tool_name = name if isinstance(name, str) else \"\"
except Exception:
    tool_name = \"\"
try:
    if payload is UNPARSED:
        input_kind = \"unparsed\"
    elif not isinstance(payload, dict) or \"tool_input\" not in payload:
        input_kind = \"absent\"
    elif isinstance(payload[\"tool_input\"], dict):
        input_kind = \"object\"
    else:
        input_kind = \"other\"
except Exception:
    input_kind = \"absent\"
try:
    target = \"\"
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
sys.stdout.write(target + \"\\0\" + tool_name + \"\\0\" + input_kind + \"\\0\")
"

# factlog_hook_read_payload <payload> <runner> [runner-arg...]
#
# Runs the extractor over <payload> and sets, in the caller's shell:
#   FACTLOG_HOOK_TARGET_PATH      the target path, or "" when none was read
#   FACTLOG_HOOK_TOOL_NAME        the raw tool_name, or ""
#   FACTLOG_HOOK_TOOL_INPUT_KIND  "unparsed"|"absent"|"object"|"other", or
#                                 "incomplete" when NO record came back at all
#
# FAILURE IS THE CALLER'S. This function ALWAYS returns 0 and always leaves the
# three variables set. A dead interpreter, a truncated record, anything that is
# not three NUL-terminated fields — all of it lands as
# ("", "", "incomplete"), which is a state the caller can read and act on rather
# than an exit status it might forget to check. That matters because the callers
# want opposite things from it: gate_check.sh falls OPEN, since with no record it
# cannot even tell the call is a write and denying would block every tool call in
# the session on evidence it does not have; gate_reminder.sh falls back to its
# pre-#337 payload-wide grep, since guessing there costs one extra line of output
# instead of a blocked write. Returning non-zero would also have been a hazard in
# the caller that runs under `set -e`.
#
# "incomplete" is chosen as the seed precisely because the Python above can never
# write it — it only ever writes one of the four kinds — so it is unambiguous.
# A record whose kind field is none of the five means a NUL inside a JSON string
# shifted the fields; gate_check.sh has a branch for that.
factlog_hook_read_payload() {
  local _payload="$1"
  shift

  FACTLOG_HOOK_TARGET_PATH=""
  FACTLOG_HOOK_TOOL_NAME=""
  FACTLOG_HOOK_TOOL_INPUT_KIND="incomplete"

  if ! { IFS= read -r -d '' FACTLOG_HOOK_TARGET_PATH \
      && IFS= read -r -d '' FACTLOG_HOOK_TOOL_NAME \
      && IFS= read -r -d '' FACTLOG_HOOK_TOOL_INPUT_KIND; } \
      < <(printf '%s' "$_payload" | "$@" -c "$FACTLOG_HOOK_PAYLOAD_PY" 2>/dev/null); then
    # A partial read leaves the earlier variables holding real values, so reset
    # all three: "incomplete" has to mean the whole record is absent, not that
    # the tail of it is.
    FACTLOG_HOOK_TARGET_PATH=""
    FACTLOG_HOOK_TOOL_NAME=""
    FACTLOG_HOOK_TOOL_INPUT_KIND="incomplete"
  fi

  return 0
}
