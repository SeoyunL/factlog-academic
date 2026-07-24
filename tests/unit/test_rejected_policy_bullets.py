# SPDX-License-Identifier: Apache-2.0
"""A policy whose every bullet was REJECTED is an authoring defect, not an empty one (#496).

#491 made "this KB has zero policy rules" an ordinary outcome, and the boundary it drew
is a bullet that ATTEMPTS a rule and names no backtick relation: that stays fatal in the
compiler. The rest of the codebase, however, asked only ``logic_policy_md_has_rules``,
which is False for both shapes — so the fatal one was read as the benign one. Downstream
that meant an empty-policy stub written over a policy the compiler had refused, and a
verification gate reporting 0 findings for a KB whose author had written rules.

These cases pin the shared verdict the fix keys on:

  * ``logic_policy_text_has_rejected_items`` is exactly the compiler's ``rejected`` list
    being non-empty — same two parsers (``markdown_policy_items`` +
    ``logic_policy_md_relations``), so it cannot drift from what generation does; and
  * ``not has_rules and has_rejected_items`` is exactly the compiler's fatal
    ``not rules and rejected``, cross-checked here against a real
    ``generate_logic_policy`` run rather than restated by hand.

``_load_logic_policy_from`` is covered directly because it is the engine-present half of
the fix: with the .dl absent, it is what turns this state into a loud failure for
``/factlog check`` and, through ``run_logic_check``, for ``tools/finalize.py``. The
finalize chain itself is pinned in tests/test_finalize.sh (#496 block).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import factlog.common as fcommon
from factlog.common import FactlogError

_REPO = Path(__file__).resolve().parents[2]

# Every tagged bullet names a relation -> rules, nothing rejected.
RULES_MD = (
    "# Logic policy\n\n## Rules\n\n"
    "- [c1] Facts with the `uses` relation require review.\n"
)

# `factlog init`'s scaffold: prose, no bullet that attempts a rule. #491's benign shape.
PROSE_ONLY_MD = "# Logic policy\n\n## Rules\n\nAdd your policy rules here.\n"

# The defect: an [id] tag (the author meant a rule) with the backticks missing.
REJECTED_ONLY_MD = (
    "# Logic policy\n\n## Rules\n\n"
    "- [c1] Facts with the uses relation require review.\n"
)

# A partial policy: one bullet compiles, one is rejected. Still a policy (#491), so the
# fatal verdict must NOT fire — the rejected bullet is reported on stderr instead.
MIXED_MD = (
    "# Logic policy\n\n## Rules\n\n"
    "- [c1] Facts with the `uses` relation require review.\n"
    "- [c2] Facts with the deployed_on relation require review.\n"
)


def _write_policy(tmp_path: Path, md_text: str) -> Path:
    """Return the (absent) logic-policy.dl path next to a written logic-policy.md."""
    policy = tmp_path / "policy"
    policy.mkdir(parents=True, exist_ok=True)
    (policy / "logic-policy.md").write_text(md_text, encoding="utf-8")
    return policy / "logic-policy.dl"


@pytest.mark.parametrize(
    "md_text, has_rules, has_rejected",
    [
        (RULES_MD, True, False),
        (PROSE_ONLY_MD, False, False),
        (REJECTED_ONLY_MD, False, True),
        (MIXED_MD, True, True),
        ("", False, False),
        # Numbered list markers and wrapped continuation lines are part of the bullet
        # grammar, so a rejected bullet written either way is still seen. A regex
        # look-alike that only knew "- [id]" would miss both.
        ("1. [c1] Facts with the uses relation require review.\n", False, True),
        ("- [c1] Facts with the uses relation\n  require review.\n", False, True),
        # Fenced code is documentation, never a live rule: a rejected-looking bullet
        # inside it must not make a prose-only .md look defective.
        ("```\n- [c1] Facts with the uses relation require review.\n```\n", False, False),
        # An untagged bullet is not an attempted rule at all — markdown_policy_items
        # admits only [id]-tagged items, so plain prose bullets stay invisible.
        ("- Facts with the uses relation require review.\n", False, False),
    ],
)
def test_has_rules_and_has_rejected_items_partition_the_tagged_bullets(
    md_text, has_rules, has_rejected
):
    assert fcommon.logic_policy_text_has_rules(md_text) is has_rules
    assert fcommon.logic_policy_text_has_rejected_items(md_text) is has_rejected


@pytest.mark.parametrize(
    "md_text", [RULES_MD, PROSE_ONLY_MD, REJECTED_ONLY_MD, MIXED_MD]
)
def test_the_fatal_verdict_matches_what_generation_actually_does(tmp_path, md_text):
    """``not has_rules and has_rejected`` == generate_logic_policy exiting non-zero.

    The whole fix rests on this equality, so it is measured against a real generation run
    instead of asserted from the same reasoning that wrote the helper. If the compiler's
    accept/reject rule ever moves, this fails here rather than silently letting finalize
    stub over a policy the compiler refused.
    """
    kb = tmp_path / "kb"
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        capture_output=True,
        check=True,
        cwd=_REPO,
    )
    (kb / "policy" / "logic-policy.md").write_text(md_text, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_REPO / "tools" / "generate_logic_policy.py")],
        capture_output=True,
        text=True,
        cwd=_REPO,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": str(_REPO)},
    )
    fatal = not fcommon.logic_policy_text_has_rules(
        md_text
    ) and fcommon.logic_policy_text_has_rejected_items(md_text)
    assert (proc.returncode != 0) is fatal, proc.stdout + proc.stderr
    assert (kb / "policy" / "logic-policy.dl").is_file() is not fatal


def test_absent_dl_over_a_rejected_only_policy_fails_loud(tmp_path):
    """The engine-present half: `check` must not run an EMPTY policy on this KB.

    Without this the loader treated the state as "no policy", so run_logic_check passed,
    the report said 0 findings, and the author's mistyped rule was gone with nothing said.
    """
    dl = _write_policy(tmp_path, REJECTED_ONLY_MD)
    with pytest.raises(FactlogError) as excinfo:
        fcommon._load_logic_policy_from(dl)
    message = str(excinfo.value)
    assert "backtick" in message
    # The remediation has to name the fix for THIS shape (quote the relation), not just
    # "compile it" — the bullets do not compile, so re-running the generator alone loops.
    assert "generate_logic_policy.py" in message


def test_absent_dl_over_a_prose_only_policy_stays_graceful(tmp_path):
    """#491's invariant, unmoved: zero rules is a normal KB, not a defect."""
    dl = _write_policy(tmp_path, PROSE_ONLY_MD)
    assert fcommon._load_logic_policy_from(dl) == ""


def test_absent_dl_over_a_mixed_policy_still_fails_loud_as_uncompiled_rules(tmp_path):
    """A partial policy has rules, so the pre-existing #190 error owns it — the new
    rejected-items error must not shadow it with the wrong remediation."""
    dl = _write_policy(tmp_path, MIXED_MD)
    with pytest.raises(FactlogError) as excinfo:
        fcommon._load_logic_policy_from(dl)
    assert "defines rules" in str(excinfo.value)


def test_a_present_dl_is_read_normally_whatever_the_md_says(tmp_path):
    """The guard is about an ABSENT .dl. A compiled .dl on disk is loaded as-is, so a
    KB that was fixed and recompiled is not held hostage by a stale .md read."""
    dl = _write_policy(tmp_path, REJECTED_ONLY_MD)
    dl.write_text('requires_review(X, "c1") :- relation(X, "uses", _).\n', encoding="utf-8")
    assert "requires_review" in fcommon._load_logic_policy_from(dl)
