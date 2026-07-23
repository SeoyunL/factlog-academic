# SPDX-License-Identifier: Apache-2.0
"""validate.py warns when a source's front matter never closes (#422).

The reader is fail-closed: a block whose closing fence it cannot find yields
nothing at all, because trusting such a block let a user's own note register its
body lines as a paper's identity, and that fails silently (#409). The trade is
right, but its cost runs the other way and had no signal — a tool-written source
whose fence a human deleted stops looking imported, drops out of de-duplication,
and announces itself only when a second ``.md`` lands beside the first.

These pin the signal: which files are reported, which are not, that the reason
distinguishes a missing fence from a search that stopped, and that saying so never
turns a valid KB into a failing one.
"""
from __future__ import annotations

import validate
from common import source_files

from factlog.front_matter_scan import (
    FRONT_MATTER_MAX_CHARS,
    FRONT_MATTER_UNCLOSED,
    FRONT_MATTER_UNSCANNED,
)

# A source as one of the writers renders it, trimmed to the keys that matter here.
INTACT = '---\nopenalex_id: "W2741809807"\ntitle: "A paper"\n---\n\nAbstract.\n'
# The same file after a human deleted the closing fence — the case under test.
DAMAGED = '---\nopenalex_id: "W2741809807"\ntitle: "A paper"\n\nAbstract.\n'


def _sources(root, **files) -> None:
    """Write ``sources/<name>`` for each keyword, creating the directory."""
    (root / "sources").mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (root / "sources" / name.replace("__", ".")).write_text(text, encoding="utf-8")


class TestWhichFilesAreReported:
    def test_a_damaged_source_is_reported(self, tmp_path):
        _sources(tmp_path, damaged__md=DAMAGED)
        warnings = validate.front_matter_warnings(tmp_path)
        assert len(warnings) == 1
        assert warnings[0].startswith("sources/damaged.md: ")
        assert FRONT_MATTER_UNCLOSED in warnings[0]

    def test_an_intact_source_is_not_reported(self, tmp_path):
        """The control that keeps the test above from passing on any source at all.

        Both files carry the same keys and differ only by the closing fence, so a
        check that reported on the wrong property would fail here.
        """
        _sources(tmp_path, intact__md=INTACT, damaged__md=DAMAGED)
        warnings = validate.front_matter_warnings(tmp_path)
        assert [w.split(":")[0] for w in warnings] == ["sources/damaged.md"]

    def test_a_conversion_without_front_matter_is_not_reported(self, tmp_path):
        """An ingest conversion carries an HTML comment, not YAML.

        It has no opening fence, so the reader returns nothing for it too. These
        are the ordinary majority of a source tree, and warning on them would bury
        the one file that is actually damaged.
        """
        _sources(tmp_path, converted__md="<!-- provenance: report.pdf -->\n\nText.\n")
        assert validate.front_matter_warnings(tmp_path) == []

    def test_a_non_markdown_conversion_is_not_scanned(self, tmp_path):
        """``.txt``/``.csv`` conversions are not asked to carry a block.

        A pdftotext dump whose first line happens to be ``---`` is not a damaged
        source, and no writer puts front matter in one.
        """
        _sources(tmp_path, dump__txt=DAMAGED, table__csv=DAMAGED)
        assert validate.front_matter_warnings(tmp_path) == []

    def test_an_uncited_source_is_still_reported(self, tmp_path):
        """No ``facts/candidates.csv`` at all, and the file is still found.

        The rest of validate.py reaches sources through the facts and pages that
        cite them. De-duplication does not — it walks the tree — so a source no
        fact cites is exactly as able to be re-imported into a duplicate.
        """
        _sources(tmp_path, damaged__md=DAMAGED)
        assert not (tmp_path / "facts").exists()
        assert len(validate.front_matter_warnings(tmp_path)) == 1

    def test_a_nested_source_is_reported(self, tmp_path):
        (tmp_path / "sources" / "2020").mkdir(parents=True)
        (tmp_path / "sources" / "2020" / "damaged.md").write_text(DAMAGED, encoding="utf-8")
        warnings = validate.front_matter_warnings(tmp_path)
        assert [w.split(":")[0] for w in warnings] == ["sources/2020/damaged.md"]

    def test_run_sources_are_reported_too(self, tmp_path):
        """``runs/sources/`` is a source root here as it is everywhere else.

        validate.py already accepts both prefixes for a fact's source, so scanning
        only ``sources/`` would leave half the tree unwatched.
        """
        (tmp_path / "runs" / "sources").mkdir(parents=True)
        (tmp_path / "runs" / "sources" / "damaged.md").write_text(DAMAGED, encoding="utf-8")
        warnings = validate.front_matter_warnings(tmp_path)
        assert [w.split(":")[0] for w in warnings] == ["runs/sources/damaged.md"]

    def test_the_order_is_the_shared_enumerator_s(self, tmp_path):
        """Deterministic, and in the order every other sources/ walker uses.

        Expected from ``source_files`` rather than restated, so the two cannot
        drift: a copy of today's ordering here would go quietly wrong the moment
        the enumerator's did.
        """
        _sources(tmp_path, b__md=DAMAGED, a__md=DAMAGED)
        (tmp_path / "runs" / "sources").mkdir(parents=True)
        (tmp_path / "runs" / "sources" / "c.md").write_text(DAMAGED, encoding="utf-8")
        expected = [p.relative_to(tmp_path).as_posix() for p in source_files(tmp_path)]
        assert len(expected) == 3, "fixture is not what the enumerator sees"
        assert [w.split(":")[0] for w in validate.front_matter_warnings(tmp_path)] == expected

    def test_a_hidden_path_is_not_a_source(self, tmp_path):
        """``sources/.obsidian/…`` and ``sources/.hidden.md`` are not sources (#67).

        ``factlog sources``, ``sync`` and ``export`` all skip them through one
        enumerator. A private glob here would warn about editor state and a
        ``.git`` checkout under sources/, which no re-import can ever duplicate.
        """
        (tmp_path / "sources" / ".obsidian").mkdir(parents=True)
        (tmp_path / "sources" / ".obsidian" / "workspace.md").write_text(DAMAGED, encoding="utf-8")
        (tmp_path / "sources" / ".hidden.md").write_text(DAMAGED, encoding="utf-8")
        assert source_files(tmp_path) == [], "fixture is visible to the enumerator"
        assert validate.front_matter_warnings(tmp_path) == []

    def test_a_kb_with_no_source_directories_reports_nothing(self, tmp_path):
        """A missing ``sources/`` is already an error elsewhere, not a crash here."""
        assert validate.front_matter_warnings(tmp_path) == []


class TestTheReasonIsAccurate:
    def test_a_block_past_the_cap_is_not_called_unclosed(self, tmp_path):
        """The fence is in the file; the search stopped before reaching it.

        Unreachable in practice — it takes a megabyte of front matter — but the two
        cases are indistinguishable to the reader, so the message is the only place
        the difference survives. Telling this operator to restore a ``---`` would
        send them looking for something already there.
        """
        pad = "x" * FRONT_MATTER_MAX_CHARS
        _sources(tmp_path, huge__md=f'---\ntitle: "T"\nauthors: {pad}\n---\n\nBody.\n')
        warnings = validate.front_matter_warnings(tmp_path)
        assert len(warnings) == 1
        assert FRONT_MATTER_UNSCANNED in warnings[0]
        assert FRONT_MATTER_UNCLOSED not in warnings[0]

    def test_the_warning_says_what_it_costs(self, tmp_path):
        """The remedy is not obvious from "no front matter", which is why #422 exists.

        A user reading only the reason would learn the file is malformed, not that
        it has silently left de-duplication — which is the part that produces the
        duplicate they will otherwise find later.
        """
        _sources(tmp_path, damaged__md=DAMAGED)
        assert validate.FRONT_MATTER_CONSEQUENCE in validate.front_matter_warnings(tmp_path)[0]
        assert "duplicate" in validate.FRONT_MATTER_CONSEQUENCE


class TestItStaysAWarning:
    """Reported, never fatal.

    An unclosed block leaves the KB entirely valid — the facts, the refs and the
    schema all still hold — and every KB that has one such file would start failing
    if this were an error. The whole point is to make a cost visible early, not to
    add a new way to stop.
    """

    @staticmethod
    def _run(tmp_path, monkeypatch, capsys, errors):
        monkeypatch.setattr(validate, "validate", lambda root: errors)
        monkeypatch.setattr("sys.argv", ["validate.py", str(tmp_path)])
        code = validate.main()
        return code, capsys.readouterr().out

    def test_a_damaged_source_alone_still_passes(self, tmp_path, monkeypatch, capsys):
        _sources(tmp_path, damaged__md=DAMAGED)
        code, out = self._run(tmp_path, monkeypatch, capsys, [])
        assert code == 0
        assert "validation passed" in out
        assert "warning: no_closing_fence: sources/damaged.md" in out

    def test_the_warning_survives_a_failing_run(self, tmp_path, monkeypatch, capsys):
        """Printed before the verdict, so a failure does not swallow it."""
        _sources(tmp_path, damaged__md=DAMAGED)
        code, out = self._run(tmp_path, monkeypatch, capsys, ["missing directory: pages/"])
        assert code == 1
        assert "warning: no_closing_fence: sources/damaged.md" in out
        assert out.index("no_closing_fence") < out.index("validation failed")

    def test_a_clean_kb_prints_no_warning_line(self, tmp_path, monkeypatch, capsys):
        _sources(tmp_path, intact__md=INTACT)
        code, out = self._run(tmp_path, monkeypatch, capsys, [])
        assert code == 0
        assert "warning" not in out

    def test_the_tag_does_not_assert_the_stronger_reason(self):
        """``no_closing_fence`` holds for both reported reasons; ``unclosed`` does not.

        The tag is what a script greps for, so it has to be true of the cap case as
        well — and both reason strings say those exact words.
        """
        for reason in validate.WARNED_FRONT_MATTER_ABSENCES:
            assert "no closing fence" in reason
