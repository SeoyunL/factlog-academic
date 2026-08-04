# SPDX-License-Identifier: Apache-2.0
"""A freshly ``factlog init``ed KB must pass ``tools/validate.py`` (#327).

The policy half of that promise lives in ``test_validate_empty_policy.py``. This
module covers the scaffold half: ``init`` never wrote ``facts/candidates.csv`` or
``decisions/open-questions.md``, and ``validate`` requires both — including four
review-section headings that only ever appeared once a ``needs_review`` fact of
that exact class happened to show up. So a new user's first ``validate`` was
rc=1 with nothing they had done wrong.

The fix is in ``init``, not in ``validate``: the fact-ledger header is the schema
contract, and the four review sections are a standing contract a reviewer reads
("here is what was looked at"), not a by-product of the facts. Narrowing
``validate`` to "only require a section that already has bullets" would instead
let a KB silently lose the 충돌 section and only notice on the day a conflict
appears — the exact class of rot validate exists to catch. So the section check
stays total, and the drift pins below hold it there.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import validate
from common import FACT_HEADER

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from factlog.cli import _TEMPLATES, _init_kb  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

# (heading the scaffold writes, substring tools/validate.py looks for). Kept in
# this order so a parametrised failure names the section a reader can find.
SCAFFOLDED_SECTIONS = [
    ("## 중복 개념 후보", "중복"),
    ("## 모호한 관계명", "모호"),
    ("## 출처 부족", "출처"),
    ("## 기존 내용과 충돌할 수 있는 항목", "충돌"),
]


@pytest.fixture()
def fresh_kb(tmp_path, capsys):
    """A KB in exactly the state ``factlog init`` leaves it.

    ``_init_kb`` is ``cmd_init``'s scaffold body without the
    ``factlog_config.write_root`` call, so this never touches the developer's
    active-KB config.
    """
    kb = tmp_path / "kb"
    _init_kb(kb)
    capsys.readouterr()
    return kb


class TestFreshInitPassesValidate:
    def test_validate_reports_no_errors(self, fresh_kb):
        assert validate.validate(fresh_kb) == []

    def test_validate_script_exits_zero(self, fresh_kb):
        # The user-visible contract from the issue: rc=0 straight after init.
        env = dict(os.environ, FACTLOG_ROOT=str(fresh_kb))
        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "validate.py"), str(fresh_kb)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr

    def test_init_scaffolds_the_review_sections(self, fresh_kb):
        text = (fresh_kb / "decisions" / "open-questions.md").read_text(encoding="utf-8")
        for section in ["중복", "모호", "출처", "충돌"]:
            assert section in text

    def test_scaffolded_headings_match_the_sync_headings(self, fresh_kb):
        # merge_candidates.decision_section() emits these exact headings; if the
        # scaffold drifts from them, `sync` appends duplicate sections instead of
        # filling the ones init created.
        import merge_candidates

        lines = (fresh_kb / "decisions" / "open-questions.md").read_text(
            encoding="utf-8"
        ).splitlines()
        for note in ["중복", "모호한 관계", "출처", "충돌"]:
            heading = merge_candidates.decision_section({"note": note})
            assert heading in lines, f"{heading!r} not scaffolded"

    def test_init_scaffolds_the_candidates_header(self, fresh_kb):
        csv_path = fresh_kb / "facts" / "candidates.csv"
        assert csv_path.read_text(encoding="utf-8") == ",".join(FACT_HEADER) + "\n"

    def test_candidates_template_tracks_fact_header(self):
        # Single source of truth: validate compares the header against
        # FACT_HEADER, so the template must be derived from it, not retyped.
        assert _TEMPLATES["facts/candidates.csv"] == ",".join(FACT_HEADER) + "\n"


class TestScaffoldIsNotADoormat:
    """The other direction: a fresh KB passing must not mean validate went blind."""

    @pytest.mark.parametrize("heading,keyword", SCAFFOLDED_SECTIONS)
    def test_removed_review_section_is_still_an_error(self, fresh_kb, heading, keyword):
        # Only the heading line goes; the rest of the scaffold stays. validate's
        # check is a plain substring search over the whole file, so any prose the
        # scaffold writes that happens to repeat a section's keyword would answer
        # the check on the deleted heading's behalf and the section could be lost
        # unnoticed. Running this over all four sections is what keeps the check
        # total — one section at a time hid exactly that leak in 출처 부족.
        decisions = fresh_kb / "decisions" / "open-questions.md"
        lines = decisions.read_text(encoding="utf-8").splitlines()
        assert heading in lines, f"{heading!r} not scaffolded"
        decisions.write_text(
            "\n".join(line for line in lines if line != heading) + "\n", encoding="utf-8"
        )
        errors = validate.validate(fresh_kb)
        assert any(repr(keyword) in e for e in errors), errors

    def test_deleted_open_questions_is_still_an_error(self, fresh_kb):
        (fresh_kb / "decisions" / "open-questions.md").unlink()
        assert "missing decisions/open-questions.md" in validate.validate(fresh_kb)

    def test_deleted_candidates_csv_is_still_an_error(self, fresh_kb):
        (fresh_kb / "facts" / "candidates.csv").unlink()
        assert "missing facts/candidates.csv" in validate.validate(fresh_kb)

    def test_corrupted_candidates_header_is_still_an_error(self, fresh_kb):
        (fresh_kb / "facts" / "candidates.csv").write_text("a,b,c\n", encoding="utf-8")
        assert any(
            "candidates.csv header must be" in e for e in validate.validate(fresh_kb)
        ), validate.validate(fresh_kb)

    def test_needs_review_without_bullets_is_still_an_error(self, fresh_kb):
        (fresh_kb / "sources" / "x.md").write_text("# x\n\n## s\n\nbody\n", encoding="utf-8")
        (fresh_kb / "pages" / "a.md").write_text("# A\n\nsources/x.md\n", encoding="utf-8")
        (fresh_kb / "facts" / "candidates.csv").write_text(
            ",".join(FACT_HEADER) + "\n" "A,uses,B,sources/x.md,needs_review,0.9,ambiguous\n",
            encoding="utf-8",
        )
        errors = validate.validate(fresh_kb)
        assert any("no review bullets" in e for e in errors), errors


class TestSyncFillsTheScaffoldedSections:
    def test_sync_adds_no_heading_of_its_own(self, fresh_kb):
        # The scaffold/sync seam: with the headings already present,
        # merge_candidates must insert into them rather than append a second copy.
        #
        # The load-bearing assertion is that the set of `## ` headings is
        # BYTE-IDENTICAL before and after sync. Asserting only "the bullet landed
        # under a 중복 heading" does not pin this: insert_bullet falls back to
        # appending the section when it cannot find one, so a sync-created
        # heading looks exactly like an init-created one from the bullet's point
        # of view — that weaker form passed both with the scaffold deleted and
        # with a scaffold heading drifted.
        import merge_candidates

        decisions = fresh_kb / "decisions" / "open-questions.md"
        before = [
            line
            for line in decisions.read_text(encoding="utf-8").splitlines()
            if line.startswith("## ")
        ]
        assert before, "init scaffolded no review headings at all"

        merge_candidates.write_decisions(
            fresh_kb,
            [
                {
                    "subject": "A",
                    "relation": "same_as",
                    "object": "B",
                    "source": "sources/x.md",
                    "status": "needs_review",
                    "confidence": "0.5",
                    "note": "duplicate?",
                }
            ],
        )
        after = [
            line
            for line in decisions.read_text(encoding="utf-8").splitlines()
            if line.startswith("## ")
        ]
        assert after == before, (
            "sync changed the heading list — it appended its own section instead "
            f"of filling a scaffolded one: {before!r} -> {after!r}"
        )

    def test_bullet_lands_inside_the_duplicate_section(self, fresh_kb):
        # Placement, given the headings match: the bullet goes under 중복, not at
        # the end of the file. This one does NOT pin the seam — see the test
        # above for that.
        import merge_candidates

        merge_candidates.write_decisions(
            fresh_kb,
            [
                {
                    "subject": "A",
                    "relation": "same_as",
                    "object": "B",
                    "source": "sources/x.md",
                    "status": "needs_review",
                    "confidence": "0.5",
                    "note": "duplicate?",
                }
            ],
        )
        lines = (fresh_kb / "decisions" / "open-questions.md").read_text(
            encoding="utf-8"
        ).splitlines()
        assert lines.count("## 중복 개념 후보") == 1
        heading = lines.index("## 중복 개념 후보")
        following = lines[heading:]
        bullet = next(i for i, line in enumerate(following) if line.startswith("- needs_review:"))
        next_heading = next(
            (i for i, line in enumerate(following[1:], start=1) if line.startswith("## ")),
            len(following),
        )
        assert bullet < next_heading, "bullet did not land inside the 중복 section"
