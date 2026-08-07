#!/usr/bin/env bash
# tests/golden.sh — deterministic golden regression over two KBs (T12 / u13)
#
# Re-runs the deterministic engine steps and diffs each output byte-for-byte
# against the committed golden files in tests/golden/. Protects AC4 determinism:
# any engine change that alters accepted.dl or logic_report.txt is caught
# immediately.
#
# Also exercises generate_logic_policy.py --check (deterministic re-derivation)
# to confirm the committed logic-policy.dl matches what the fixture compiler
# would produce from logic-policy.md.
#
# WHAT THE GOLDEN KBs COVER — read this before citing a green run as evidence.
# Byte-identity is evidence only for the code paths the KB actually walks.
#
#   KB 1, the caller's FACTLOG_ROOT (examples/sample-kb), goldens in
#   tests/golden/: plain relation/3 facts, a requires_review policy rule
#   compiled from logic-policy.md, relation and review_required queries. Its
#   policy/ holds NO single-valued.md, typed-relations.md,
#   attribute-relations.md or relation-aliases.md, so every policy gate below is
#   inert on it — check_conflicts returns early, typed projection never runs,
#   no canonical/3 atom is emitted, and its query.dl has no count or path line.
#
#   KB 2, tests/golden-kb, goldens in tests/golden/policy-kb/: exists precisely
#   to walk those gates. It declares all four policy files and pins
#   single-valued contradiction detection (Step 4), typed projection of all four
#   literal types with thresholds tight against the one fact that satisfies
#   each, alias canonicalisation (a rule written over the canonical name firing
#   on a fact stated with the surface form), count/path query rendering, and two
#   separate things about attribute relations:
#     - the Python-side accepted-entity precheck in run_logic_check.py, which
#       refuses `path("Orbit", "2031-02-01")?` before the engine is asked; and
#     - the engine's own entity_node/1 extent, named by a rule in
#       logic-policy.extra.dl so the golden holds its exact membership. Without
#       that rule the !attr_rel exclusion — the #329 invariant — is unpinned:
#       dropping it from WIRELOG_PROGRAM still left the run green, because the
#       precheck above had already refused the only query that would have
#       noticed.
#
# A change to a path NEITHER KB walks is not covered by a green run here (#354).
#
# Usage:
#   FACTLOG_ROOT=examples/sample-kb bash tests/golden.sh
#   PYTHON=<interpreter> FACTLOG_ROOT=examples/sample-kb bash tests/golden.sh
#
# Returns 0 if all golden diffs pass and --check passes, 1 on first failure.
#
# Acceptance checks (from unit u13):
#   bash -n tests/golden.sh
#   cd /Users/joykim/git/semantic-reasoning/factlog && \
#     FACTLOG_ROOT=examples/sample-kb bash tests/golden.sh && echo GOLDEN-STABLE

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

# pwd -P throughout: every path here is either copied from or written next to,
# and a logical path keeps a symlink in it, which is how a "copy" ended up
# aliasing its own source.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
GOLDEN_DIR="$SCRIPT_DIR/golden"

# The policy-gate KB is harness-owned, not caller-supplied: it is the half of
# the coverage that FACTLOG_ROOT cannot provide, so it runs on every invocation
# regardless of where the caller points KB 1.
POLICY_KB="$SCRIPT_DIR/golden-kb"
POLICY_GOLDEN_DIR="$GOLDEN_DIR/policy-kb"

# FACTLOG_ROOT must be set by the caller; resolve to absolute path.
if [ -z "${FACTLOG_ROOT:-}" ]; then
  echo "FATAL: FACTLOG_ROOT is not set. Run as: FACTLOG_ROOT=examples/sample-kb bash tests/golden.sh" >&2
  exit 1
fi
# Resolve to a PHYSICAL absolute path. String-concatenating cwd left any symlink
# in the caller's value intact, and a symlinked root is not a cosmetic
# difference here: it decides whether the working copy below is a copy or an
# alias of the caller's own KB.
if ! KB_ROOT="$(cd "$FACTLOG_ROOT" 2>/dev/null && pwd -P)"; then
  echo "FATAL: FACTLOG_ROOT is not a readable directory: $FACTLOG_ROOT" >&2
  exit 1
fi
export FACTLOG_ROOT="$KB_ROOT"

# Python interpreter: the caller's PYTHON wins, then factlog-venv, then python3.
# The caller's value used to be overwritten unconditionally, so the
# `PYTHON=<interpreter> bash tests/x.sh` convention every other harness in this
# directory follows was silently discarded here — a run asked to use an
# interpreter with pyrewire fell through to a bare python3 without it, and Step 2
# died for a reason that had nothing to do with the branch under test (#354).
if [ -z "${PYTHON:-}" ]; then
  if [ -x "/tmp/factlog-venv/bin/python" ]; then
    PYTHON="/tmp/factlog-venv/bin/python"
  else
    PYTHON="python3"
  fi
fi

pass=0
fail=0

# Which KB a verdict belongs to. The `### KB n/2` headers go to stdout while
# FAIL: goes to stderr, so on any consumer that keeps the streams apart the two
# KBs' failures were character-identical — the same "no reader or grep may take
# one for the other" problem the Step 0 labels below exist to avoid, unapplied to
# the KB axis. Every verdict carries its KB.
CURRENT_KB=""

ok() {
  echo "PASS: ${CURRENT_KB:+[$CURRENT_KB] }$*"
  pass=$((pass + 1))
}

fail_msg() {
  echo "FAIL: ${CURRENT_KB:+[$CURRENT_KB] }$*" >&2
  fail=$((fail + 1))
}

# --- scratch space ----------------------------------------------------------
# Every KB is copied here and run from the copy. Nothing under the checkout is
# written, so a run cannot rewrite a tracked fixture, cannot leave one deleted if
# it is killed, and two runs cannot collide on the fixed in-repo path of KB 2.
# Cleanup is best-effort: a SIGKILL leaks a temp directory, which is the failure
# mode we want, because SIGKILL cannot be trapped and anything that relies on a
# trap to put a repo file back is one kill away from deleting tracked data.
WORK_ROOT="$(mktemp -d)"
trap 'rm -rf "$WORK_ROOT"' EXIT

assert_golden() {
  local label="$1"
  local actual="$2"
  local expected="$3"
  # Missing is a failure, never a skip. For a Step 1/2 comparison this is the
  # whole point: the artifact is deleted from the working copy before its step
  # runs, so absence means the step did not write it and there is nothing from
  # this branch to compare. Passing here would be the vacuous PASS (#354).
  if [ ! -f "$actual" ]; then
    fail_msg "$label is missing, so there is nothing to compare against the golden"
    return 0
  fi
  if diff -u "$expected" "$actual" >/dev/null 2>&1; then
    ok "$label matches golden"
  else
    fail_msg "$label differs from golden"
    diff -u "$expected" "$actual" >&2 || true
  fi
}

# ---------------------------------------------------------------------------
# One pass = the three deterministic steps over one KB, each diffed against that
# KB's golden directory.
#   Step 1: compile_facts.py          → facts/accepted.dl
#   Step 2: run_logic_check.py        → facts/logic_report.txt
#   Step 3: generate_logic_policy.py --check (deterministic re-derivation)
# ---------------------------------------------------------------------------
#
# The KB is copied into $WORK_ROOT and every step runs against the copy.
#
# What that guarantees, precisely: the copy contains no symlink that leaves it,
# so no write inside $WORK_ROOT can land outside it, and the source KB is
# therefore read-only for the whole run. That is a property of `cp -RL`, not of
# copying as such — `cp -R` dereferences neither a symlinked source operand
# (POSIX: -R implies -P) nor an inner symlink, and both cases produced a "copy"
# that wrote straight back into the caller's KB while the run went green. The
# symlinked-root case was worse than the in-place behaviour it replaced: the
# `rm -f` below reached the real file, and on a dead step nothing put it back.
#
# Two consequences worth stating:
#
#   - The step that regenerates an artifact is the only thing that can create it
#     in the copy, so each artifact is deleted from the copy first. A step that
#     dies leaves nothing to diff and the comparison fails loudly, instead of
#     silently comparing the committed file against the golden copy and passing
#     no matter what the branch changed (#354). Deleting inside a throwaway copy
#     needs no trap to undo it.
#   - Nothing verifies the committed artifacts any more, since they are no longer
#     overwritten — so each pass checks them against the golden separately. That
#     is a different claim from the regeneration diff and is labelled as one.
#
run_pass() {
  local kb="$1"
  local golden="$2"
  local work="$WORK_ROOT/$3"
  CURRENT_KB="$3"

  # -L: dereference on copy, so the result holds real files and directories and
  # no link back to the source. It cannot resolve a cyclic symlink or a broken
  # one, and in both cases it skips and carries on, leaving a silently incomplete
  # copy — so the failure is caught here rather than measured later as a
  # difference from the golden. cp's own message on the line above names which
  # path it was; this one must not claim to know.
  if ! cp -RL "$kb" "$work"; then
    echo "FATAL: could not copy $kb into the work area (a symlink cycle or a broken link inside the KB will do this)" >&2
    exit 1
  fi
  # cp copies modes, so a read-only source KB produced a read-only copy: the
  # `rm -f` below failed, `set -e` killed the run mid-step with no summary line
  # at all, and the EXIT trap could not remove $WORK_ROOT either. Running on a
  # copy is supposed to mean the source's properties do not reach the run; its
  # permissions still did.
  chmod -R u+w "$work"

  # Labels deliberately unlike the Step 1/2 ones: "committed <file>" is a claim
  # about what is checked in, "<file>" is a claim about what this run produced.
  # A reader — or a test grepping this output — must not be able to take one for
  # the other, which is how a vacuous comparison got cited in the first place.
  echo "=== Step 0: committed artifacts in sync with golden ==="
  assert_golden "committed facts/accepted.dl" \
    "$kb/facts/accepted.dl" \
    "$golden/accepted.dl"
  assert_golden "committed facts/logic_report.txt" \
    "$kb/facts/logic_report.txt" \
    "$golden/logic_report.txt"

  echo ""
  echo "=== Step 1: compile_facts.py ==="
  rm -f "$work/facts/accepted.dl"
  if FACTLOG_ROOT="$work" "$PYTHON" "$PLUGIN_ROOT/tools/compile_facts.py" 2>&1; then
    ok "compile_facts.py exit 0"
  else
    fail_msg "compile_facts.py exited non-zero"
  fi
  assert_golden "facts/accepted.dl" \
    "$work/facts/accepted.dl" \
    "$golden/accepted.dl"

  echo ""
  echo "=== Step 2: run_logic_check.py ==="
  rm -f "$work/facts/logic_report.txt"
  if FACTLOG_ROOT="$work" "$PYTHON" "$PLUGIN_ROOT/tools/run_logic_check.py" 2>&1; then
    ok "run_logic_check.py exit 0"
  else
    fail_msg "run_logic_check.py exited non-zero"
  fi
  assert_golden "facts/logic_report.txt" \
    "$work/facts/logic_report.txt" \
    "$golden/logic_report.txt"

  echo ""
  echo "=== Step 3: generate_logic_policy.py --check ==="
  if FACTLOG_ROOT="$work" "$PYTHON" "$PLUGIN_ROOT/tools/generate_logic_policy.py" --check 2>&1; then
    ok "generate_logic_policy.py --check exit 0"
  else
    fail_msg "generate_logic_policy.py --check exited non-zero (policy/logic-policy.dl is stale)"
  fi
}

echo "### KB 1/2: $KB_ROOT"
run_pass "$KB_ROOT" "$GOLDEN_DIR" kb1

echo ""
echo "### KB 2/2: $POLICY_KB"
run_pass "$POLICY_KB" "$POLICY_GOLDEN_DIR" kb2

# ---------------------------------------------------------------------------
# Step 4 (policy KB only): check_conflicts.py → single-valued contradictions
#
# The other steps never reach this gate: run_logic_check.py does not read
# policy/single-valued.md at all, so conflict detection has no golden coverage
# except here. The fixture KB asserts two maintainers for one subject, so the
# tool is EXPECTED to exit 1 — exit 0 would mean the contradiction went
# undetected, which is the regression this pins.
#
# Exit 1 alone does NOT establish that, though: a tool that cannot start exits
# non-zero too, and reporting "the contradiction is detected" because the
# interpreter did not exist is the same vacuous-PASS defect this whole issue
# exists to remove. So the verdict needs the finding line as well — a run that
# died has no findings to print.
# ---------------------------------------------------------------------------
echo ""
CURRENT_KB="kb2"
echo "=== Step 4: check_conflicts.py (policy KB) ==="
conflicts_out="$WORK_ROOT/conflicts.txt"
conflicts_rc=0
FACTLOG_ROOT="$WORK_ROOT/kb2" "$PYTHON" "$PLUGIN_ROOT/tools/check_conflicts.py" \
  > "$conflicts_out" 2>&1 || conflicts_rc=$?
cat "$conflicts_out"
if [ "$conflicts_rc" -eq 1 ] && grep -qF 'conflict(s) found' "$conflicts_out"; then
  ok "check_conflicts.py exit 1 with a conflict report (the declared contradiction is detected)"
elif [ "$conflicts_rc" -eq 1 ]; then
  fail_msg "check_conflicts.py exited 1 without reporting any conflict — it died rather than detecting the contradiction"
else
  fail_msg "check_conflicts.py exited $conflicts_rc, expected 1 (single-valued contradiction not detected)"
fi
assert_golden "check_conflicts.py output" \
  "$conflicts_out" \
  "$POLICY_GOLDEN_DIR/conflicts.txt"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo "Golden results: $pass passed, $fail failed"
echo "========================================"
if [ "$fail" -gt 0 ]; then
  exit 1
fi
exit 0
