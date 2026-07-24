# SPDX-License-Identifier: Apache-2.0
"""`init` and `merge_candidates` both hold the open-questions contract (#495).

The issue's reproduction is the first case here: `factlog init` produced a KB that
tools/validate.py rejected and no amount of running the normal pipeline could fix,
because the only writer of decisions/open-questions.md created it as the 17 bytes
`# Open Questions\\n` while the validator required four review sections in it.

Two paths scaffold those sections now — `init` up front, and
merge_candidates.write_decisions for a KB that predates it — which is exactly the
shape that makes a mutation invisible: break one and the other still gets the file
to rc=0. So each case below drives **one** path and denies itself the other. The
init cases never run merge_candidates; the merge cases start from a KB whose
open-questions.md is the legacy bare title, which init would not have written and
will not repair. test_neither_path_scaffolds_when_both_are_broken pins that the
pair is not covering for each other.

The placement case is the other half of the fix, and is why scaffolding alone was
not enough: given a KB whose four headings are spelled differently, a producer with
a hardcoded heading opened a *second* section for the category and put the review
queue there, leaving the section a human reads empty. That is the state
~/factlog-kb was found in.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from factlog.review_sections import REVIEW_CATEGORIES

_REPO = Path(__file__).resolve().parents[2]
_MERGE = _REPO / "tools" / "merge_candidates.py"
_VALIDATE = _REPO / "tools" / "validate.py"

KEYWORDS = [keyword for keyword, _ in REVIEW_CATEGORIES]

# The bare title decisions/open-questions.md used to be created as — a legacy KB, and
# the input that tells the two scaffold paths apart.
LEGACY_TITLE_ONLY = "# Open Questions\n"

# A KB whose sections are spelled by hand, none of them the canonical heading.
HAND_SPELLED = (
    "# Open Questions\n\n"
    "## 중복 (같은 개념의 다른 이름)\n\n"
    "## 모호 (관계명·개념 판단 필요)\n\n"
    "## 출처 (근거 강도 부족)\n\n"
    "## 충돌 (상충하는 후보)\n"
)


def _env(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["XDG_CONFIG_HOME"] = str(tmp_path / "cfg")
    return env


def _init(kb: Path, tmp_path: Path) -> Path:
    proc = subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        cwd=_REPO, env=_env(tmp_path), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return kb


def _merge(kb: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_MERGE), "--wiki", str(kb)],
        cwd=_REPO, env=_env(tmp_path), capture_output=True, text=True,
    )


def _validate(kb: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_VALIDATE), str(kb)],
        cwd=_REPO, env={**_env(tmp_path), "FACTLOG_ROOT": str(kb)},
        capture_output=True, text=True,
    )


def _seed_needs_review(kb: Path) -> None:
    """One ambiguous-relation row, via runs/ — the source of truth for a merge."""
    (kb / "sources" / "note.md").write_text("# note\n", encoding="utf-8")
    (kb / "runs").mkdir(exist_ok=True)
    (kb / "runs" / "r1.json").write_text(
        json.dumps([{
            "subject": "Widget", "relation": "related_to", "object": "Gadget",
            "source": "sources/note.md", "status": "needs_review",
            "confidence": 0.4, "note": "relation name is imprecise",
        }]),
        encoding="utf-8",
    )


def _merge_module():
    """tools/ is not a package; the conftest puts it on sys.path for direct import."""
    import merge_candidates  # noqa: PLC0415

    return merge_candidates


def _open_questions(kb: Path) -> str:
    return (kb / "decisions" / "open-questions.md").read_text(encoding="utf-8")


def _headings(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("#")]


def _review_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.lstrip().startswith("- needs_review")]


# ---------------------------------------------------------------------------
# The `init` path, alone — merge_candidates is never run in this class.
# ---------------------------------------------------------------------------

class TestInitAlone:
    def test_a_freshly_initialised_kb_validates(self, tmp_path):
        """The issue's reproduction: init, then validate, nothing in between."""
        kb = _init(tmp_path / "kb", tmp_path)
        proc = _validate(kb, tmp_path)
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_init_writes_open_questions_with_all_four_sections(self, tmp_path):
        kb = _init(tmp_path / "kb", tmp_path)
        text = _open_questions(kb)
        for keyword in KEYWORDS:
            assert any(keyword in line for line in _headings(text)), (keyword, text)

    def test_init_writes_the_candidates_header(self, tmp_path):
        # The other artefact validate.py required and init did not write.
        kb = _init(tmp_path / "kb", tmp_path)
        csv_path = kb / "facts" / "candidates.csv"
        assert csv_path.is_file()
        assert csv_path.read_text(encoding="utf-8").splitlines() == [
            "subject,relation,object,source,status,confidence,note"
        ]

    @pytest.mark.parametrize("rel", ["decisions/open-questions.md", "facts/candidates.csv"])
    def test_re_init_does_not_overwrite_either_file(self, tmp_path, rel):
        kb = _init(tmp_path / "kb", tmp_path)
        edited = "# Open Questions\n\n## 중복 x\n## 모호 x\n## 출처 x\n## 충돌 x\n- mine\n"
        (kb / rel).write_text(edited, encoding="utf-8")
        _init(kb, tmp_path)
        assert (kb / rel).read_text(encoding="utf-8") == edited


# ---------------------------------------------------------------------------
# The merge path, alone — every KB here starts from the legacy bare title, which
# `init` did not write and cannot repair.
# ---------------------------------------------------------------------------

class TestMergeAlone:
    def _legacy_kb(self, tmp_path) -> Path:
        kb = _init(tmp_path / "kb", tmp_path)
        (kb / "decisions" / "open-questions.md").write_text(
            LEGACY_TITLE_ONLY, encoding="utf-8"
        )
        return kb

    def test_merge_repairs_a_legacy_open_questions_file(self, tmp_path):
        kb = self._legacy_kb(tmp_path)
        proc = _merge(kb, tmp_path)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        text = _open_questions(kb)
        for keyword in KEYWORDS:
            assert any(keyword in line for line in _headings(text)), (keyword, text)
        assert _validate(kb, tmp_path).returncode == 0

    def test_a_second_merge_changes_nothing(self, tmp_path):
        kb = self._legacy_kb(tmp_path)
        assert _merge(kb, tmp_path).returncode == 0
        once = _open_questions(kb)
        assert _merge(kb, tmp_path).returncode == 0
        assert _open_questions(kb) == once

    def test_write_decisions_scaffolds_on_its_own(self, tmp_path):
        """The writer, called directly — a full merge run would hide a break here.

        merge_candidates has two writers of the file and calls both, so a pipeline
        test cannot tell which one supplied the sections: dropping the scaffold from
        write_decisions leaves every end-to-end assertion green because
        record_stale_page_refs runs afterwards and puts them back.
        """
        mc = _merge_module()
        kb = self._legacy_kb(tmp_path)
        mc.write_decisions(kb, [])
        text = _open_questions(kb)
        for keyword in KEYWORDS:
            assert any(keyword in line for line in _headings(text)), (keyword, text)

    def test_stale_ref_recording_scaffolds_on_its_own(self, tmp_path):
        """record_stale_page_refs is the second writer and holds the same contract."""
        mc = _merge_module()
        kb = self._legacy_kb(tmp_path)
        (kb / "pages" / "p.md").write_text(
            "# p\n\n- see sources/gone.md\n", encoding="utf-8"
        )
        added = mc.record_stale_page_refs(kb)
        assert added, "the stale ref was not recorded"
        text = _open_questions(kb)
        for keyword in KEYWORDS:
            assert any(keyword in line for line in _headings(text)), (keyword, text)


def test_neither_path_scaffolds_when_both_are_broken(tmp_path):
    """Break both writers and the KB stays unvalidatable — the pair is load-bearing.

    Without this, a mutant that disables one path is equivalent: the other path in
    the same pipeline puts the sections back. Here `init`'s two templates are gone
    and merge starts from the legacy bare title, so nothing can supply them, and the
    validator has to say so for all four categories.
    """
    kb = _init(tmp_path / "kb", tmp_path)
    (kb / "decisions" / "open-questions.md").write_text(LEGACY_TITLE_ONLY, encoding="utf-8")
    proc = _validate(kb, tmp_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    out = proc.stdout + proc.stderr
    for keyword in KEYWORDS:
        assert f"should keep a {keyword!r} review section" in out, out


# ---------------------------------------------------------------------------
# Placement: a bullet joins the section the file already has.
# ---------------------------------------------------------------------------

class TestBulletPlacement:
    def test_a_needs_review_bullet_lands_in_the_scaffolded_section(self, tmp_path):
        kb = _init(tmp_path / "kb", tmp_path)
        _seed_needs_review(kb)
        proc = _merge(kb, tmp_path)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        text = _open_questions(kb)
        assert len(_review_lines(text)) == 1, text
        # exactly one heading per category — no second section opened beside the
        # scaffolded one
        for keyword in KEYWORDS:
            assert sum(keyword in line for line in _headings(text)) == 1, (keyword, text)
        assert _validate(kb, tmp_path).returncode == 0

    def test_a_hand_spelled_section_receives_the_bullet(self, tmp_path):
        """The ~/factlog-kb damage: the bullet must not open '## 모호한 관계명'."""
        kb = _init(tmp_path / "kb", tmp_path)
        (kb / "decisions" / "open-questions.md").write_text(HAND_SPELLED, encoding="utf-8")
        _seed_needs_review(kb)
        proc = _merge(kb, tmp_path)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        text = _open_questions(kb)
        assert _headings(text) == _headings(HAND_SPELLED), text
        lines = text.splitlines()
        bullet = next(i for i, line in enumerate(lines) if line.startswith("- needs_review"))
        heading = next(
            lines[i] for i in range(bullet, -1, -1) if lines[i].startswith("## ")
        )
        assert heading == "## 모호 (관계명·개념 판단 필요)", text
