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

from factlog.md_lines import fence_flags, headings
from factlog.review_sections import REVIEW_CATEGORIES, REVIEW_KEYWORDS

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


# The bullet a merge writes for the row _seed_needs_review plants. Spelled out so a
# test can put the identical line somewhere else in the file and mean it.
SEEDED_BULLET = (
    "- needs_review: Widget / related_to / Gadget "
    "(sources/note.md, confidence=0.40) - relation name is imprecise"
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
    """The headings of *text*, as the code reads them.

    md_lines' answer and not a `#`-prefix scan of its own: a helper with a blinder
    the code no longer has proves nothing about the code. Fence-aware, and Setext
    headings count.
    """
    return [found.text for found in headings(text)]


def _review_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.lstrip().startswith("- needs_review")]


def _heading_above(text: str, prefix: str) -> str | None:
    """The **real** level-2 heading the first *prefix* line sits under.

    Fence-aware on purpose. An earlier version of this helper walked back to any
    `## ` line, which let a bullet orphaned just past a closing fence look correctly
    filed: the heading it "sat under" was the code sample inside the fence. A test
    that measures placement with a blinder the code no longer has proves nothing —
    so it asks md_lines, which is also why a Setext heading counts here.
    """
    at = next(
        i for i, line in enumerate(text.splitlines()) if line.startswith(prefix)
    )
    above = [h for h in headings(text) if h.level == 2 and h.end <= at]
    return above[-1].text if above else None


def _is_fenced(text: str, prefix: str) -> bool:
    """Did the first *prefix* line end up inside a code fence?"""
    lines = text.splitlines()
    flags, _ = fence_flags(text)
    return flags[next(i for i, line in enumerate(lines) if line.startswith(prefix))]


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


def test_validate_reports_every_missing_section(tmp_path):
    """The validator names each absent category, not just the first.

    A KB whose open-questions.md is the legacy bare title has none of the four, and
    all four have to appear in the output — otherwise a file could lose three
    sections and the operator would be told about one. (Which path *supplies* the
    sections is not this test's business: TestInitAlone and TestMergeAlone each
    drive one writer with the other denied, so a mutant that breaks either is caught
    there rather than here.)
    """
    kb = _init(tmp_path / "kb", tmp_path)
    (kb / "decisions" / "open-questions.md").write_text(LEGACY_TITLE_ONLY, encoding="utf-8")
    proc = _validate(kb, tmp_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    out = proc.stdout + proc.stderr
    for keyword in KEYWORDS:
        assert f"should keep a {keyword!r} review section" in out, out


class TestCodeFencesEndToEnd:
    """Fences, through the actual `merge_candidates` run (#504).

    These exist because the pure unit tests did not catch the worst bug in this
    area. `ensure_review_sections` correctly declined to write into a file ending in
    an unclosed fence and every unit test agreed — and then `insert_bullet`, called
    right after it in the same function, appended its own heading and the bullet at
    the end of the file, which is inside that fence. A review item was written into
    a code block where no rendered view shows it, and the run exited 0.

    A guard that the next writer in the same pipeline walks straight past is not a
    guard. Only running the pipeline says so.
    """

    def _fenced_kb(self, tmp_path, text: str) -> Path:
        kb = _init(tmp_path / "kb", tmp_path)
        (kb / "decisions" / "open-questions.md").write_text(text, encoding="utf-8")
        _seed_needs_review(kb)
        return kb

    UNCLOSED = (
        "# Open Questions\n\n## 중복 개념 후보\n\n```\n## 모호 (Ambiguous)\n- 기존 큐\n"
    )

    def test_a_merge_writes_nothing_into_an_unclosed_fence(self, tmp_path):
        kb = self._fenced_kb(tmp_path, self.UNCLOSED)
        proc = _merge(kb, tmp_path)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        after = _open_questions(kb)
        assert after == self.UNCLOSED, "the file was written to"
        assert "needs_review" not in after, "a review item was buried in the fence"

    def test_the_merge_says_which_line_to_fix(self, tmp_path):
        kb = self._fenced_kb(tmp_path, self.UNCLOSED)
        err = _merge(kb, tmp_path).stderr
        assert "code fence on line 5" in err, err
        assert err.count("opens a code fence") == 1, "both writers complained"

    def test_the_validator_explains_the_baffling_error(self, tmp_path):
        """"'모호' section missing" is unusable when the heading is right there."""
        kb = self._fenced_kb(tmp_path, self.UNCLOSED)
        proc = _validate(kb, tmp_path)
        out = proc.stdout + proc.stderr
        assert proc.returncode == 1, out
        assert "warning: unclosed_fence:" in out, out
        assert "line 5" in out, out

    def test_a_fence_opened_after_the_headings_warns_without_failing(self, tmp_path):
        """Where the fence opens decides the exit code; the warning does not.

        A fragment pasted onto the end of a complete file hides nothing any check
        requires, so the run passes with only the warning to show for it. The docs
        and the issue comment both said "rc=1" flatly, which is false in exactly this
        shape — the common one. Pinned here so the claim cannot drift back.
        """
        kb = _init(tmp_path / "kb", tmp_path)
        _seed_needs_review(kb)
        assert _merge(kb, tmp_path).returncode == 0
        with (kb / "decisions" / "open-questions.md").open("a", encoding="utf-8") as f:
            f.write("\n```\n붙여넣다 만 조각\n")
        proc = _validate(kb, tmp_path)
        out = proc.stdout + proc.stderr
        assert "warning: unclosed_fence:" in out, out
        assert proc.returncode == 0, out
        # and the message does not blame errors that were never reported
        assert "reported missing" not in out, out

    def test_a_fence_opened_before_the_headings_does_fail(self, tmp_path):
        """The other half of the same rule, so the pair states the whole contract."""
        kb = self._fenced_kb(tmp_path, self.UNCLOSED)
        proc = _validate(kb, tmp_path)
        assert proc.returncode == 1, proc.stdout + proc.stderr

    def test_a_tilde_block_quoting_backticks_is_written_to_normally(self, tmp_path):
        """The shape the reference recommends must not hit the hard stop.

        Wrapping a bullet-format example in `~~~` is how anyone avoids nesting
        backticks — and this PR added both the recommendation and the hard stop, so a
        file written the recommended way was refused forever, pointed at the line
        that closes it and told to close it.
        """
        doc = (
            "# Open Questions\n\n## 중복 개념 후보\n\n## 모호한 관계명\n\n형식 예시:\n\n"
            "~~~\n- needs_review: X / r / Y\n```\n~~~\n\n"
            "## 출처 부족\n\n## 기존 내용과 충돌할 수 있는 항목\n"
        )
        kb = self._fenced_kb(tmp_path, doc)
        proc = _merge(kb, tmp_path)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "opens a code fence" not in proc.stderr, proc.stderr
        text = _open_questions(kb)
        assert not _is_fenced(text, "- needs_review: Widget"), text
        assert _heading_above(text, "- needs_review: Widget") == "## 모호한 관계명", text
        assert _validate(kb, tmp_path).returncode == 0

    def test_merging_twice_leaves_the_file_byte_identical(self, tmp_path):
        # The #504 regression grew the file by three headings on every pass.
        kb = self._fenced_kb(tmp_path, self.UNCLOSED)
        _merge(kb, tmp_path)
        once = _open_questions(kb)
        _merge(kb, tmp_path)
        assert _open_questions(kb) == once == self.UNCLOSED

    def test_a_closed_fence_example_does_not_capture_the_bullet(self, tmp_path):
        """The other half: `lines.index` used to match the heading *in* the fence.

        It is a code sample, not a section. The bullet went in just past the closing
        fence — under no heading at all — and the run still exited 0.
        """
        doc = (
            "# Open Questions\n\n## 중복 개념 후보\n\n예시:\n\n```\n"
            "## 모호한 관계명\n```\n\n## 출처 부족\n\n## 기존 내용과 충돌할 수 있는 항목\n"
        )
        kb = self._fenced_kb(tmp_path, doc)
        assert _merge(kb, tmp_path).returncode == 0
        text = _open_questions(kb)
        assert not _is_fenced(text, "- needs_review"), text
        # under a *real* section — the fenced sample is not one
        assert _heading_above(text, "- needs_review") == "## 모호한 관계명", text
        assert _validate(kb, tmp_path).returncode == 0

    def test_a_stale_source_note_is_not_buried_in_a_fence(self, tmp_path):
        """The second writer, which has its own append and its own way in.

        record_stale_page_refs runs before write_decisions and files its own bullet.
        With only write_decisions guarded, a KB with a removed source still had a
        stale_source note written into the code fence.
        """
        kb = _init(tmp_path / "kb", tmp_path)
        (kb / "decisions" / "open-questions.md").write_text(self.UNCLOSED, encoding="utf-8")
        (kb / "pages" / "p.md").write_text("# p\n\n- see sources/gone.md\n", encoding="utf-8")
        mc = _merge_module()
        assert mc.record_stale_page_refs(kb) == []
        assert _open_questions(kb) == self.UNCLOSED
        assert "stale_source" not in _open_questions(kb)

    def test_a_fenced_format_example_does_not_swallow_the_real_bullet(self, tmp_path):
        """The worst shape found here: the item vanishes and the KB reports valid.

        A KB that documents its own bullet format inside a code fence lost the first
        real bullet identical to that example — the producer deduplicated against the
        fenced line and wrote nothing, and the validator counted that same fenced line
        as proof review bullets existed. Measured: zero real bullets in the file a
        human reads, one needs_review row in candidates.csv, rc=0, no warning.

        Both ends read md_lines.bullets now, so neither can count it.
        """
        # Byte-identical to what _seed_needs_review makes the merge write — the
        # collision is the whole point, so this must not drift from it.
        bullet = SEEDED_BULLET
        doc = (
            "# Open Questions\n\n## 중복 개념 후보\n\n## 모호한 관계명\n\n형식 예시:\n\n"
            f"```\n{bullet}\n```\n\n## 출처 부족\n\n## 기존 내용과 충돌할 수 있는 항목\n"
        )
        kb = self._fenced_kb(tmp_path, doc)
        assert _merge(kb, tmp_path).returncode == 0
        text = _open_questions(kb)
        flags, _ = fence_flags(text)
        real = [
            line
            for line, fenced in zip(text.splitlines(), flags)
            if not fenced and line.startswith("- needs_review")
        ]
        assert real == [bullet], text  # written, once, outside the fence
        assert _validate(kb, tmp_path).returncode == 0

    def test_a_fenced_example_alone_does_not_satisfy_the_validator(self, tmp_path):
        """The validator's half, with the producer taken out of the picture.

        The file is left as a human wrote it — one needs_review row in candidates.csv
        and nothing filed but a fenced example — and that has to be an error.
        """
        kb = _init(tmp_path / "kb", tmp_path)
        _seed_needs_review(kb)
        assert _merge(kb, tmp_path).returncode == 0
        (kb / "decisions" / "open-questions.md").write_text(
            "# Open Questions\n\n## 중복 개념 후보\n\n## 모호한 관계명\n\n"
            "```\n- needs_review: W / related_to / G\n```\n\n"
            "## 출처 부족\n\n## 기존 내용과 충돌할 수 있는 항목\n",
            encoding="utf-8",
        )
        proc = _validate(kb, tmp_path)
        out = proc.stdout + proc.stderr
        assert proc.returncode == 1, out
        assert "no review bullets" in out, out

    @pytest.mark.parametrize(
        "filed,expected_errors",
        [
            (None, 1),      # nothing recorded — the error stands
            ("real", 0),    # recorded as a bullet — the error is answered
            ("fenced", 1),  # recorded only as an example — answers nothing
        ],
        ids=["not-recorded", "recorded", "fenced-example-only"],
    )
    def test_only_a_real_bullet_answers_the_stale_source_error(
        self, tmp_path, filed, expected_errors
    ):
        """A stale-source record inside a fence is an example, not a record.

        The check was a substring test over the whole document — neither line-based
        nor fence-aware — so the fenced case was indistinguishable from the recorded
        one and silenced the error for a KB that had recorded nothing. The writer
        files these through a fence-aware insert_bullet; whatever reads back what it
        filed has to agree with it.
        """
        record = "- stale_source: pages/p.md references removed source sources/gone.md"
        body = {
            None: "",
            "real": f"{record}\n",
            "fenced": f"예시:\n\n```\n{record}\n```\n",
        }[filed]
        kb = _init(tmp_path / "kb", tmp_path)
        (kb / "pages" / "p.md").write_text("# p\n\n- see sources/gone.md\n", encoding="utf-8")
        (kb / "decisions" / "open-questions.md").write_text(
            "# Open Questions\n\n## 중복 개념 후보\n\n## 모호한 관계명\n\n## 출처 부족\n"
            f"{body}\n## 기존 내용과 충돌할 수 있는 항목\n",
            encoding="utf-8",
        )
        out = _validate(kb, tmp_path).stdout
        assert out.count("source file does not exist") == expected_errors, out

    def test_both_writers_refuse_even_when_only_one_speaks(self, tmp_path):
        """Two calls, two refusals, one complaint — the observable behaviour.

        This does not pin the *structure* that produces it: deciding once and
        returning the remembered answer gives the same results here, since anything
        reaching the cached path has already read True out of the file. What the
        separation buys is mutation sensitivity — one break now propagates to both
        writers instead of one — and that shows up in kill counts, not here.
        """
        mc = _merge_module()
        kb = _init(tmp_path / "kb", tmp_path)
        (kb / "decisions" / "open-questions.md").write_text(self.UNCLOSED, encoding="utf-8")
        text = _open_questions(kb)
        assert mc.refuse_unclosed_fence(kb, text) is True
        assert mc.refuse_unclosed_fence(kb, text) is True  # still refused, silently
        # and a closed file is never refused, warned-about or not
        assert mc.refuse_unclosed_fence(kb, HAND_SPELLED) is False

    def test_a_bullet_clears_a_fenced_example_inside_its_own_section(self, tmp_path):
        """A section may quote a `## ` example; the bullet still goes after it.

        The end-of-section scan has to skip fenced lines the same way the lookup
        does. Stopping at the fenced heading put the bullet *between the fence
        opener and the example*, inside the code block.
        """
        mc = _merge_module()
        text = (
            "# Open Questions\n\n## 출처 부족\n- a\n\n"
            "```\n## 모호한 관계명\n```\n\n"
            "## 기존 내용과 충돌할 수 있는 항목\n"
        )
        out = mc.insert_bullet(text, "출처", "- b")
        assert not _is_fenced(out, "- b"), out
        assert _heading_above(out, "- b") == "## 출처 부족", out


class TestKeywordDrift:
    """merge_candidates' routing keywords are checked against the contract at import.

    Without it the drift is invisible exactly where it matters. A keyword
    REVIEW_CATEGORIES no longer defines raises KeyError out of `section_for` only
    when the file has no heading carrying it — a fresh KB. On a populated one the
    old heading is found and returned, so bullets keep being filed under a category
    the contract has stopped naming, and nothing anywhere says so.
    """

    def test_the_routed_keywords_are_all_defined(self):
        mc = _merge_module()
        assert mc.ROUTED_KEYWORDS <= REVIEW_KEYWORDS
        # and they really are the ones the classifier hands out
        rows = [
            {"subject": "a", "relation": "same_as", "object": "b", "note": ""},
            {"subject": "a", "relation": "r", "object": "b", "note": "evidence"},
            {"subject": "a", "relation": "r", "object": "b", "note": "conflict"},
            {"subject": "a", "relation": "r", "object": "b", "note": ""},
        ]
        assert {mc.decision_section(row) for row in rows} == mc.ROUTED_KEYWORDS

    def test_importing_with_a_dropped_category_fails_loudly(self):
        """Drop a category from the contract and merge_candidates refuses to load."""
        import importlib  # noqa: PLC0415

        from factlog import review_sections as rs  # noqa: PLC0415

        mc = _merge_module()
        original = rs.REVIEW_KEYWORDS
        try:
            rs.REVIEW_KEYWORDS = frozenset(original - {"출처"})
            with pytest.raises(RuntimeError, match="출처"):
                importlib.reload(mc)
        finally:
            rs.REVIEW_KEYWORDS = original
            importlib.reload(mc)
        assert _merge_module().ROUTED_KEYWORDS <= REVIEW_KEYWORDS


class TestSplitSectionWarning:
    """An already-split file is not repaired, so it has to be reported.

    Both halves matter. Repairing would move bullets a human wrote and filed, which
    is their call; staying silent leaves the state that caused this issue invisible,
    because a split file passes every structural check there is.
    """

    def _split_kb(self, tmp_path) -> Path:
        kb = _init(tmp_path / "kb", tmp_path)
        (kb / "decisions" / "open-questions.md").write_text(
            HAND_SPELLED + "\n## 모호한 관계명\n- needs_review: filed here earlier\n",
            encoding="utf-8",
        )
        return kb

    def test_the_split_is_named_and_does_not_fail_the_run(self, tmp_path):
        """The two sections are named by their titles, without the `## `.

        A deliberate change to operator-visible output, and the only one in this
        branch. The message used to print each heading's raw line, which is fine for
        ATX and wrong for an underlined heading: the raw span is two lines, so a
        one-line warning carried a literal newline through the middle of itself
        (`has 2 '출처' sections ('출처\n----', …)`). Naming both spellings the same
        way is what a reader sees anyway — the `## ` was never information, it was
        syntax that one of the two spellings happens to use.
        """
        proc = _validate(self._split_kb(tmp_path), tmp_path)
        out = proc.stdout + proc.stderr
        assert proc.returncode == 0, out
        assert "warning: split_review_section:" in out, out
        assert "'모호 (관계명·개념 판단 필요)'" in out, out
        assert "'모호한 관계명'" in out, out
        assert "\n" not in out.split("split_review_section:")[1].split("\n")[0]

    def test_a_kb_with_one_heading_per_category_is_not_warned_about(self, tmp_path):
        kb = _init(tmp_path / "kb", tmp_path)
        proc = _validate(kb, tmp_path)
        assert proc.returncode == 0
        assert "split_review_section" not in proc.stdout + proc.stderr

    def test_merging_a_split_kb_leaves_the_split_alone(self, tmp_path):
        """No churn and no repair: the headings a human wrote are still all there."""
        kb = self._split_kb(tmp_path)
        before = _open_questions(kb)
        assert _merge(kb, tmp_path).returncode == 0
        assert _headings(_open_questions(kb)) == _headings(before)


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
        assert _heading_above(text, "- needs_review") == "## 모호 (관계명·개념 판단 필요)", text

    def test_a_stale_source_bullet_lands_in_the_source_section(self, tmp_path):
        """The other writer's destination, pinned the same way.

        record_stale_page_refs asks for the 출처 section by keyword, and nothing was
        checking that it asked for the right one: swapping its keyword to 모호 filed
        every stale-source note under ambiguity and the whole suite stayed green.
        """
        mc = _merge_module()
        kb = _init(tmp_path / "kb", tmp_path)
        (kb / "decisions" / "open-questions.md").write_text(HAND_SPELLED, encoding="utf-8")
        (kb / "pages" / "p.md").write_text("# p\n\n- see sources/gone.md\n", encoding="utf-8")
        added = mc.record_stale_page_refs(kb)
        assert added, "the stale ref was not recorded"
        text = _open_questions(kb)
        assert _headings(text) == _headings(HAND_SPELLED), text
        assert _heading_above(text, "- stale_source") == "## 출처 (근거 강도 부족)", text

    def test_a_new_bullet_keeps_a_blank_line_before_the_next_heading(self, tmp_path):
        """Placement must not leave the bullet flush against the following heading.

        The bullet goes at the end of its section, which — when that section's content
        ends in a blank line — is the next heading's own index. Routing bullets to a
        file's existing sections made that the ordinary case, and the result read as
        the next section's lead-in rather than as this section's last item.
        """
        kb = _init(tmp_path / "kb", tmp_path)
        (kb / "decisions" / "open-questions.md").write_text(HAND_SPELLED, encoding="utf-8")
        _seed_needs_review(kb)
        assert _merge(kb, tmp_path).returncode == 0
        lines = _open_questions(kb).splitlines()
        bullet = next(i for i, line in enumerate(lines) if line.startswith("- needs_review"))
        after = lines[bullet + 1]
        assert not any(h.start == bullet + 1 for h in headings("\n".join(lines)))
        assert after.strip() == ""


# ---------------------------------------------------------------------------
# A KB whose sections are written the other way round (#500).
# ---------------------------------------------------------------------------

# The four categories underlined instead of prefixed. A human writing markdown by
# hand does this; nothing in the KB's own documentation told them not to.
SETEXT_SPELLED = (
    "# Open Questions\n\n"
    "중복\n----\n- a\n\n"
    "모호\n----\n- b\n\n"
    "출처\n----\n\n"
    "충돌\n----\n"
)


class TestSetextSpelledSections:
    """The #500 reproduction, end to end — four measured symptoms, one cause.

    Before this, the scan read `## ` and nothing else, so every one of these four
    headings was invisible: the file reported all four categories missing, the
    validator failed it, a merge appended four ATX sections below the ones already
    there, and the new bullets went to the new sections while `- a` and `- b` stayed
    orphaned in the old.

    The fourth symptom is the one the issue did not name and the worst of them. Once
    the merge had run, each category really did have two headings — and
    `split_review_sections` returned nothing, because it could only see one of each.
    The warning #495 added to make a split visible was passed by a split #495's own
    narrowness had just manufactured, and the run exited 0.
    """

    def test_the_four_sections_are_recognised(self, tmp_path):
        kb = _init(tmp_path / "kb", tmp_path)
        (kb / "decisions" / "open-questions.md").write_text(SETEXT_SPELLED, encoding="utf-8")
        assert _validate(kb, tmp_path).returncode == 0

    def test_a_merge_leaves_the_file_alone_but_for_the_bullet(self, tmp_path):
        kb = _init(tmp_path / "kb", tmp_path)
        (kb / "decisions" / "open-questions.md").write_text(SETEXT_SPELLED, encoding="utf-8")
        _seed_needs_review(kb)
        assert _merge(kb, tmp_path).returncode == 0
        text = _open_questions(kb)
        # no section was opened beside the ones the file already had
        assert _headings(text) == _headings(SETEXT_SPELLED), text
        # and the only change is the one bullet
        added = set(text.splitlines()) - set(SETEXT_SPELLED.splitlines())
        assert added == {SEEDED_BULLET}, text

    def test_the_bullet_joins_the_section_the_file_already_had(self, tmp_path):
        kb = _init(tmp_path / "kb", tmp_path)
        (kb / "decisions" / "open-questions.md").write_text(SETEXT_SPELLED, encoding="utf-8")
        _seed_needs_review(kb)
        assert _merge(kb, tmp_path).returncode == 0
        text = _open_questions(kb)
        assert _heading_above(text, "- needs_review") == "모호\n----", text
        assert "- b" in text.splitlines(), "the existing bullet was orphaned"
        assert _validate(kb, tmp_path).returncode == 0

    def test_a_bullet_at_the_end_of_a_section_keeps_the_next_heading_a_heading(
        self, tmp_path
    ):
        """The 출처 section is empty, so its bullet lands right above 충돌.

        Without a blank line after it, `충돌` is swallowed as a lazy continuation of
        the bullet's list and the `----` under it renders as a horizontal rule —
        measured against a CommonMark renderer, the file loses a section a human can
        see while every scan still reports it. The bullet here is the stale-source
        one, which is the writer that files under 출처.
        """
        mc = _merge_module()
        kb = _init(tmp_path / "kb", tmp_path)
        (kb / "decisions" / "open-questions.md").write_text(SETEXT_SPELLED, encoding="utf-8")
        (kb / "pages" / "p.md").write_text("# p\n\n- see sources/gone.md\n", encoding="utf-8")
        assert mc.record_stale_page_refs(kb), "the stale ref was not recorded"
        text = _open_questions(kb)
        lines = text.splitlines()
        at = next(i for i, line in enumerate(lines) if line.startswith("- stale_source"))
        assert lines[at + 1].strip() == "", text
        # the next heading is still a heading, and it is 충돌's title line
        following = next(h for h in headings(text) if h.start > at)
        assert (following.start, following.text) == (at + 2, "충돌\n----"), text
        assert _headings(text) == _headings(SETEXT_SPELLED), text

    def test_a_file_damaged_by_the_old_behaviour_is_now_reported_as_split(
        self, tmp_path
    ):
        """What a KB merged under the old code looks like, and what it now hears.

        Two headings per category — the human's Setext ones on top with the queue in
        them, the producer's ATX ones below. That is a split, and it went unreported
        for as long as only one of the two spellings was visible. It is a warning and
        not an error, so rc does not move; what changes is that it is said out loud.
        Nothing repairs it: joining the two moves bullets a human filed.
        """
        kb = _init(tmp_path / "kb", tmp_path)
        damaged = SETEXT_SPELLED + "\n" + "\n\n".join(
            heading for _, heading in REVIEW_CATEGORIES
        ) + "\n"
        (kb / "decisions" / "open-questions.md").write_text(damaged, encoding="utf-8")
        proc = _validate(kb, tmp_path)
        assert proc.returncode == 0
        report = proc.stdout + proc.stderr
        assert report.count("split_review_section") == len(KEYWORDS), report
        for keyword in KEYWORDS:
            assert f"2 {keyword!r} sections" in report, report
        assert _open_questions(kb) == damaged, "a warning must not rewrite the file"
