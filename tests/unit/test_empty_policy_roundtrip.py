# SPDX-License-Identifier: Apache-2.0
"""A KB with zero policy rules validates, compiles, and re-checks (#491).

`factlog init` scaffolds a prose-only policy/logic-policy.md and no
policy/logic-policy.dl, and until #491 the compiler had no way to say "no rules": it
exited on an empty rule list, so the fresh KB failed tools/validate.py, and writing the
missing .dl by hand failed too — the compiler could not agree that any byte string
represented an empty policy. Reproduced from the issue body in
test_the_issue_reproduction_now_passes below.

The fix makes zero rules an ordinary outcome whose .dl is common.EMPTY_POLICY_DL. The
cases here pin both halves of that: the empty policy round-trips (write -> check -> check
with the file removed), and the loud path #190 installed is untouched — a .md that DOES
define rules with no compiled .dl still fails, and stale compiled rules over an emptied
.md are still caught rather than silently kept in force.

The boundary between the two halves is a bullet that ATTEMPTS a rule and names no
backtick relation. That is an authoring error, not an empty policy, and it stays fatal;
an early cut of this fix keyed only on the rule count and made such a bullet disappear
without a word, since a successful run's only louder channel is stderr and both
tools/validate.py and tools/finalize.py read the child's stderr solely when it exits
non-zero. test_a_bullet_that_names_no_relation_is_still_loud pins it.

The engine-side companion is tests/test_check_empty_policy.sh, which pins the same
absent-.dl equivalence for `/factlog check`. It SKIPs without pyrewire, so the
validate/compile side lives here instead of there: these cases need no engine and so run
everywhere the unit suite does.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import factlog.common as fcommon

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import finalize as fin  # noqa: E402
import generate_logic_policy as glp  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]

# One bullet in the grammar markdown_policy_items parses, so logic_policy_md_has_rules is
# True and the compiler emits a real rule. The loud cases need a policy that a user could
# plausibly have written and lost.
RULES_MD = (
    "# Logic policy\n\n## Rules\n\n"
    "- [bidirectional_check] Facts with the `develops` relation require review when a "
    "matching `developed_by` relation also exists.\n"
)

# What `factlog init` scaffolds: headings and prose, no list item that attempts a rule.
PROSE_ONLY_MD = (
    "# Logic policy\n\n## Rules\n\nAdd your policy rules here.\n"
)

# The authoring error the empty-policy path must not absorb: a bullet with an [id] tag
# (so the author meant it as a rule) whose relation name is missing its backticks.
NO_RELATION_MD = (
    "# Logic policy\n\n## Rules\n\n"
    "- [capability_check] Facts with the develops relation require review.\n"
)


def _init_kb(root: Path) -> Path:
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(root)],
        capture_output=True,
        check=True,
        cwd=_REPO,
    )
    return root


def _script(name: str, *args: str, kb: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_REPO / "tools" / name), *args],
        capture_output=True,
        text=True,
        cwd=_REPO,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": str(_REPO)},
    )


def _generate(kb: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _script("generate_logic_policy.py", *args, kb=kb)


def _validate(kb: Path) -> subprocess.CompletedProcess[str]:
    return _script("validate.py", str(kb), kb=kb)


def _policy_lines(proc: subprocess.CompletedProcess[str]) -> list[str]:
    """The validate.py complaints that mention the policy .dl, in either stream."""
    return [
        line
        for line in (proc.stdout + proc.stderr).splitlines()
        if "logic-policy.dl" in line
    ]


@pytest.fixture
def kb(tmp_path):
    return _init_kb(tmp_path / "kb")


def test_a_freshly_initialized_kb_validates_without_a_policy_dl(kb):
    """(1) The issue's first reproduction: `init` then validate, no .dl complaint.

    Asserts on the policy lines only, not on rc. The other scaffold gaps `init` used to
    leave (facts/candidates.csv, decisions/open-questions.md) were a separate matter,
    closed since by #495; pinning rc=0 here would quietly make this test the owner of
    that fix, which tests/unit/test_open_questions_contract.py holds.
    """
    assert not (kb / "policy" / "logic-policy.dl").exists()
    assert _policy_lines(_validate(kb)) == []


def test_zero_rules_compiles_to_the_shared_empty_policy_bytes(kb):
    """(2) Generation succeeds on a prose-only .md and writes EMPTY_POLICY_DL."""
    proc = _generate(kb)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "policy rules: 0" in proc.stdout, proc.stdout
    dl = kb / "policy" / "logic-policy.dl"
    assert dl.read_bytes() == fcommon.EMPTY_POLICY_DL.encode("utf-8")


def test_the_empty_policy_round_trips_and_absence_equals_empty(kb):
    """(3) --check passes on the generated empty .dl, and again once it is deleted.

    The second half is the equivalence common._load_logic_policy_from already applies:
    with no rules in the .md, an absent .dl and an empty one describe the same policy.
    """
    assert _generate(kb).returncode == 0
    checked = _generate(kb, "--check")
    assert checked.returncode == 0, checked.stdout + checked.stderr

    (kb / "policy" / "logic-policy.dl").unlink()
    absent = _generate(kb, "--check")
    assert absent.returncode == 0, absent.stdout + absent.stderr


def test_rules_without_a_compiled_dl_still_fail_loud(kb):
    """(4) #190's invariant: a policy the user wrote is never silently dropped.

    This is the case the #491 relaxation must not swallow — the .md defines a rule and no
    .dl exists, so the KB is running with a policy nobody compiled. Checked through
    validate.py as well as --check, because validate delegating the absent-.dl verdict is
    exactly what changed.
    """
    (kb / "policy" / "logic-policy.md").write_text(RULES_MD, encoding="utf-8")
    assert not (kb / "policy" / "logic-policy.dl").exists()

    checked = _generate(kb, "--check")
    assert checked.returncode != 0
    combined = checked.stdout + checked.stderr
    assert "missing policy/logic-policy.dl" in combined, combined
    assert "generate_logic_policy" in combined, combined

    lines = _policy_lines(_validate(kb))
    assert lines, _validate(kb).stdout
    assert any("generate_logic_policy" in line for line in lines), lines


def test_a_bullet_that_names_no_relation_is_still_loud(kb):
    """(4b) An attempted rule that names no relation is an authoring error, not an
    empty policy.

    This is the line #491 must not cross. Keying the success path on the rule count alone
    made this .md compile to "// no policy rules" with rc=0 and no complaint anywhere in
    merge -> validate -> finalize: the rejects went to stderr, which validate reads only
    when the child exits non-zero and finalize forwards only on its failure branch. The
    author's rule is gone and nothing says so — the silent drop #190 exists to prevent.
    The line number is asserted because it is what makes the message actionable.
    """
    (kb / "policy" / "logic-policy.md").write_text(NO_RELATION_MD, encoding="utf-8")

    gen = _generate(kb)
    assert gen.returncode != 0, gen.stdout + gen.stderr
    assert not (kb / "policy" / "logic-policy.dl").exists()
    assert "line 5" in gen.stdout + gen.stderr, gen.stdout + gen.stderr

    checked = _generate(kb, "--check")
    assert checked.returncode != 0, checked.stdout + checked.stderr

    lines = _policy_lines(_validate(kb))
    assert lines, _validate(kb).stdout
    assert any("line 5" in line for line in lines), lines


def test_old_rules_left_over_an_emptied_policy_are_stale_not_silent(kb):
    """(5) Deleting every bullet does not leave the compiled rules quietly in force."""
    (kb / "policy" / "logic-policy.md").write_text(RULES_MD, encoding="utf-8")
    assert _generate(kb).returncode == 0
    compiled = (kb / "policy" / "logic-policy.dl").read_text(encoding="utf-8")
    assert "requires_review" in compiled, compiled

    (kb / "policy" / "logic-policy.md").write_text(
        "# Logic policy\n\n## Rules\n\nNo rules yet.\n", encoding="utf-8"
    )
    checked = _generate(kb, "--check")
    assert checked.returncode != 0
    assert "stale" in checked.stdout + checked.stderr


def test_the_empty_policy_bytes_have_one_definition():
    """(6) Compiler, finalize's stub and the shared constant cannot drift apart.

    Same shape as test_reserved_predicate_parity: the three agreed by copied literal
    before #491, and the moment they disagree a ruleless KB reports its own output as
    stale on every run.
    """
    assert glp.compile_policy([]) == fcommon.EMPTY_POLICY_DL
    assert fin.POLICY_STUB == fcommon.EMPTY_POLICY_DL
    # The exact bytes are pinned too, not only the parity: they are what every existing
    # empty KB already holds, so changing them is a migration and should fail here first.
    assert fcommon.EMPTY_POLICY_DL == "// no policy rules\n"


class TestTheDraftGuard:
    """_reject_dropped_policy, called directly — the deterministic path cannot reach it.

    fixture_policy_json returns zero rules only for a .md that defines none, so on that
    path the guard is a no-op by construction; it exists for the LLM draft path, where a
    model can answer {"rules": []} to a rule-bearing .md and where normalized_rules' old
    "policy JSON has no rules" ValueError used to be the backstop. Nothing in the
    subprocess-driven suites can produce that input, so without these calls the whole
    guard is a mutant nobody catches — replacing its body with `return` left every suite
    green.
    """

    def test_zero_rules_against_a_rule_bearing_md_is_refused(self):
        with pytest.raises(fcommon.FactlogError) as excinfo:
            glp._reject_dropped_policy([], RULES_MD)
        assert "#190" in str(excinfo.value), str(excinfo.value)

    def test_zero_rules_against_a_ruleless_md_is_allowed(self):
        glp._reject_dropped_policy([], PROSE_ONLY_MD)

    def test_rules_present_is_allowed_whatever_the_md_says(self):
        glp._reject_dropped_policy([{"predicate": "requires_review"}], PROSE_ONLY_MD)
        glp._reject_dropped_policy([{"predicate": "requires_review"}], RULES_MD)

    def test_both_paths_to_compile_policy_are_guarded(self):
        """A text pin on the call sites, in the spirit of test_policy_control_char_gate.

        The guard has to sit on BOTH routes into compile_policy — the --check branch and
        the generating branch — because a draft reaching either one with an empty rule
        list is the same silent drop. A rename or reflow can break this pin harmlessly;
        before adjusting it, confirm every compile_policy call is still preceded by the
        guard rather than deleting the assertion.
        """
        source = Path(glp.__file__).read_text(encoding="utf-8")
        calls = source.count("_reject_dropped_policy(rules, policy_text)")
        assert calls == 2, f"expected 2 guarded call sites, found {calls}"
        assert source.count("def _reject_dropped_policy") == 1


def test_the_issue_reproduction_now_passes(kb):
    """(7) #491's manual repro verbatim: hand-write the stub, then validate.

    Those bytes were chosen as the canonical empty policy BECAUSE a user (and finalize
    since #194) already writes them, so the reproduction passing is the point rather than
    a side effect.
    """
    (kb / "policy" / "logic-policy.dl").write_text("// no policy rules\n", encoding="utf-8")
    assert _policy_lines(_validate(kb)) == []
    checked = _generate(kb, "--check")
    assert checked.returncode == 0, checked.stdout + checked.stderr
