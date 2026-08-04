# SPDX-License-Identifier: Apache-2.0
"""validate must treat a ruleless policy as *no policy*, not as a defect (#327).

#190 taught ``check``/``ask`` that a ``policy/logic-policy.md`` with no compilable
bullets is a legitimate empty policy. ``tools/validate.py`` was never updated, so
it kept demanding a ``policy/logic-policy.dl`` that ``generate_logic_policy.py``
deliberately refuses to produce for such an ``.md`` — an unreachable requirement.

Both directions are pinned here: the empty-policy amnesty, and every drift the
byte-comparison used to catch.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import validate

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from factlog.cli import _init_kb  # noqa: E402

POLICY_STUB = "// no policy rules\n"  # finalize.POLICY_STUB
RULE_MD = "# Logic policy\n\n## Rules\n\n- [c1] flag when `requires_review`\n"
REAL_DL = 'requires_review(X, "c1") :- relation(X, "uses", _).\n'


@pytest.fixture()
def policy_kb(tmp_path, capsys):
    """A real KB root, so ``generate_logic_policy.py --check`` actually compares
    the two policy files instead of bailing out on a missing layout.

    ``_init_kb`` is ``cmd_init``'s scaffold body without the
    ``factlog_config.write_root`` call, so this never touches the developer's
    active-KB config. It leaves ``policy/logic-policy.md`` as prose (no rules).
    """
    kb = tmp_path / "kb"
    _init_kb(kb)
    capsys.readouterr()
    return kb


def policy_errors(root: Path) -> list[str]:
    """Only the logic-policy findings — this module owns that axis alone."""
    return [e for e in validate.validate(root) if "logic-policy" in e]


class TestNoPolicyIsAccepted:
    def test_prose_md_and_absent_dl(self, policy_kb):
        assert policy_errors(policy_kb) == []

    def test_prose_md_and_finalize_stub_dl(self, policy_kb):
        # finalize writes exactly this stub for a ruleless policy; validate used
        # to reject its own pipeline's output as drift.
        (policy_kb / "policy" / "logic-policy.dl").write_text(POLICY_STUB, encoding="utf-8")
        assert policy_errors(policy_kb) == []

    def test_prose_md_and_empty_dl(self, policy_kb):
        (policy_kb / "policy" / "logic-policy.dl").write_text("\n  \n", encoding="utf-8")
        assert policy_errors(policy_kb) == []

    def test_prose_md_and_comment_only_dl(self, policy_kb):
        # `//` only. `#` is NOT a Datalog comment and must not appear here — see
        # TestHashIsNotAComment below for why.
        (policy_kb / "policy" / "logic-policy.dl").write_text(
            "// hand note\n// another note\n", encoding="utf-8"
        )
        assert policy_errors(policy_kb) == []


class TestRealDriftStillCaught:
    """Characterization pins: these already failed before the fix and must keep
    failing. Without them the amnesty above could quietly widen into "validate
    stopped checking the policy at all"."""

    def test_rules_in_md_but_no_dl(self, policy_kb):
        (policy_kb / "policy" / "logic-policy.md").write_text(RULE_MD, encoding="utf-8")
        assert policy_errors(policy_kb) == ["missing or empty policy/logic-policy.dl"]

    def test_rules_in_md_but_stub_dl(self, policy_kb):
        # A stub left over from a pre-#194 finalize must not launder real rules.
        (policy_kb / "policy" / "logic-policy.md").write_text(RULE_MD, encoding="utf-8")
        (policy_kb / "policy" / "logic-policy.dl").write_text(POLICY_STUB, encoding="utf-8")
        assert any("does not match" in e for e in policy_errors(policy_kb))

    def test_rules_in_md_with_mismatched_dl(self, policy_kb):
        (policy_kb / "policy" / "logic-policy.md").write_text(RULE_MD, encoding="utf-8")
        (policy_kb / "policy" / "logic-policy.dl").write_text(
            'requires_review(X, "stale") :- relation(X, "was", _).\n', encoding="utf-8"
        )
        assert any("does not match" in e for e in policy_errors(policy_kb))

    def test_ruleless_md_with_a_real_dl(self, policy_kb):
        # The rules→empty transition: the .md lost its rules but a compiled .dl
        # is still on disk, so the engine would keep applying the old policy.
        (policy_kb / "policy" / "logic-policy.dl").write_text(REAL_DL, encoding="utf-8")
        assert any("does not match" in e for e in policy_errors(policy_kb))


class TestHashIsNotAComment:
    """``#`` is a comment in ``logic-policy.extra.dl`` only — never in the main file.

    ``common._load_logic_policy_from`` filters comment-only text out of the
    *sibling* ``logic-policy.extra.dl``, but reads the main ``logic-policy.dl``
    verbatim (``read_text().strip()``), so its bytes become the engine program
    as-is. Measured on the two files, same content::

        logic-policy.dl        = "# hand note\\n"  ->  '# hand note'
        logic-policy.extra.dl  = "# hand note\\n"  ->  ''

    So validate must not extend extra.dl's amnesty to the main file: a ``#``-only
    ``.dl`` is not empty to the engine, and ``common`` treats ``#`` reaching the
    engine as a bug it goes out of its way to prevent on the extra.dl side.

    Scope of the claim: this pins validate's *judgement*, not an engine crash.
    pyrewire 1.0.4 was measured to accept a stray ``#`` line in the program
    rather than ParseError on it, so the mismatch is latent today. Nothing
    legitimate is lost by the stricter reading — ``finalize.POLICY_STUB`` is
    ``// no policy rules`` and ``generate_logic_policy`` never emits ``#`` into
    a ``.dl``.
    """

    def test_hash_only_dl_is_an_error(self, policy_kb):
        (policy_kb / "policy" / "logic-policy.dl").write_text(
            "# hand note\n", encoding="utf-8"
        )
        assert policy_errors(policy_kb) != [], "a #-only .dl reached the engine unflagged"

    def test_hash_after_slash_comments_is_an_error(self, policy_kb):
        # The old fixture's exact bytes: the `//` line is a real comment, but the
        # `#` line still ends up in the engine program.
        (policy_kb / "policy" / "logic-policy.dl").write_text(
            "// hand note\n# another note\n", encoding="utf-8"
        )
        assert policy_errors(policy_kb) != [], "a #-only .dl reached the engine unflagged"


class TestLogicPolicyDlHasRules:
    @pytest.mark.parametrize("text", ["", "\n", "   \n\t\n", POLICY_STUB, "//no space\n"])
    def test_empty_bodies(self, text):
        assert validate.logic_policy_dl_has_rules(text) is False

    @pytest.mark.parametrize(
        "text",
        [
            REAL_DL,
            "// note\n" + REAL_DL,
            ".decl foo(x:symbol)\n",
            # Not comments here, whatever they look like — see TestHashIsNotAComment.
            "# b\n",
            "// a\n# b\n",
        ],
    )
    def test_bodies_with_rules(self, text):
        assert validate.logic_policy_dl_has_rules(text) is True
