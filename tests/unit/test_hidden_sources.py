# SPDX-License-Identifier: Apache-2.0
"""One definition of "hidden" under sources/ across every enumerator (#67).

Before #67, `factlog sources`, `ingest --scan` and eject checked only a path's
*name* (`p.name.startswith(".")`), while sync/coverage checked *every* component
(`any(part.startswith("."))`). So a file inside a hidden directory —
`sources/.provenance/x.json`, a nested `.git/`, `.obsidian/` editor state — was
listed as a real source by `factlog sources` but invisible to coverage; the two
counts disagreed and the `[no facts]` hint pointed at a file sync would never
touch. The rule now lives in one place, `common.source_files()`, via
`common.is_hidden_source()`.

Tests drive the real CLI (`cli.main`) and the real enumerator helpers, never a
reimplementation of the hidden rule.
"""
from __future__ import annotations

import common
import pytest
from source_coverage import coverage_rows

from factlog import cli

_MD = "# paper\n\ncontent\n"


def _kb_with_hidden(root):
    """A KB with one real source plus assorted hidden paths under sources/."""
    src = root / "sources"
    src.mkdir()
    (src / "paper.md").write_text(_MD, encoding="utf-8")
    # a file inside a hidden directory (the issue's exact reproduction)
    (src / ".provenance").mkdir()
    (src / ".provenance" / "x.json").write_text("{}\n", encoding="utf-8")
    # a nested git checkout and editor state under sources/
    (src / ".git").mkdir()
    (src / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (src / ".obsidian").mkdir()
    (src / ".obsidian" / "workspace.json").write_text("{}\n", encoding="utf-8")
    # a hidden file nested two levels deep, and a top-level dotfile / .DS_Store
    (src / "notes").mkdir()
    (src / "notes" / ".Trash").mkdir()
    (src / "notes" / ".Trash" / "old.md").write_text(_MD, encoding="utf-8")
    (src / ".DS_Store").write_text("", encoding="utf-8")
    (src / ".hidden.md").write_text(_MD, encoding="utf-8")
    return root


# --- the shared helper -------------------------------------------------------

class TestIsHiddenSource:
    def test_top_level_dotfile_is_hidden(self, tmp_path):
        base = tmp_path / "sources"
        assert common.is_hidden_source(base / ".DS_Store", base)
        assert common.is_hidden_source(base / ".hidden.md", base)

    def test_file_inside_hidden_dir_is_hidden(self, tmp_path):
        base = tmp_path / "sources"
        assert common.is_hidden_source(base / ".provenance" / "x.json", base)
        assert common.is_hidden_source(base / ".git" / "config", base)

    def test_deeply_nested_hidden_component_is_hidden(self, tmp_path):
        base = tmp_path / "sources"
        assert common.is_hidden_source(base / "notes" / ".Trash" / "old.md", base)

    def test_a_plain_nested_source_is_not_hidden(self, tmp_path):
        base = tmp_path / "sources"
        assert not common.is_hidden_source(base / "sub" / "paper.md", base)
        assert not common.is_hidden_source(base / "paper.md", base)


# --- the single enumeration point --------------------------------------------

class TestSourceFilesFilters:
    def test_source_files_returns_only_the_visible_source(self, tmp_path):
        kb = _kb_with_hidden(tmp_path)
        names = [p.relative_to(kb).as_posix() for p in common.source_files(kb)]
        assert names == ["sources/paper.md"]

    def test_source_file_refs_exclude_every_hidden_path(self, tmp_path):
        kb = _kb_with_hidden(tmp_path)
        assert common.source_file_refs(kb) == {"sources/paper.md"}


# --- every enumerator agrees, through the real surfaces ----------------------

def _sources_count(out: str) -> int:
    # "factlog sources (active KB: ...): N source(s), ..."
    marker = "): "
    return int(out.split(marker, 1)[1].split(" source(s)", 1)[0])


def _status_source_count(out: str) -> int:
    # "  sources:    N file(s), ..."
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("sources:"))
    return int(line.split("sources:", 1)[1].split("file(s)", 1)[0].strip())


class TestSourcesAndCoverageAgree:
    def test_sources_lists_one_and_hides_the_hidden(self, tmp_path, capsys):
        kb = _kb_with_hidden(tmp_path)
        rc = cli.main(["sources", "--target", str(kb)])
        out = capsys.readouterr().out
        assert rc == 0
        assert _sources_count(out) == 1
        assert "sources/paper.md" in out
        # not one hidden path leaks into the listing
        for hidden in (".provenance", ".git", ".obsidian", ".Trash",
                       ".DS_Store", ".hidden.md"):
            assert hidden not in out

    def test_sources_and_status_report_the_same_count(self, tmp_path, capsys):
        kb = _kb_with_hidden(tmp_path)

        cli.main(["sources", "--target", str(kb)])
        sources_out = capsys.readouterr().out

        cli.main(["status", "--target", str(kb)])
        status_out = capsys.readouterr().out

        # the acceptance criterion: the two surfaces agree for a KB with a
        # hidden directory (both count only sources/paper.md).
        assert _sources_count(sources_out) == 1
        assert _status_source_count(status_out) == 1

    def test_coverage_rows_sees_only_the_visible_source(self, tmp_path):
        kb = _kb_with_hidden(tmp_path)
        rows, _orphans = coverage_rows(kb, [])
        assert [r["file"] for r in rows] == ["sources/paper.md"]


class TestNestedRepoStateIsInvisibleEverywhere:
    """A nested `.git/` or `.obsidian/` under sources/ must be invisible to
    every enumerator, not just to some of them."""

    @pytest.mark.parametrize("hidden_dir, hidden_file", [
        (".git", "config"),
        (".obsidian", "workspace.json"),
    ])
    def test_no_enumerator_sees_nested_repo_state(self, tmp_path, capsys,
                                                  hidden_dir, hidden_file):
        src = tmp_path / "sources"
        src.mkdir()
        (src / "paper.md").write_text(_MD, encoding="utf-8")
        d = src / hidden_dir
        d.mkdir()
        (d / hidden_file).write_text("x\n", encoding="utf-8")

        rel = f"sources/{hidden_dir}/{hidden_file}"

        # helper enumerators
        assert rel not in common.source_file_refs(tmp_path)
        assert rel not in {r["file"] for r in coverage_rows(tmp_path, [])[0]}

        # CLI surfaces
        cli.main(["sources", "--target", str(tmp_path)])
        assert hidden_dir not in capsys.readouterr().out
        cli.main(["status", "--target", str(tmp_path)])
        status_out = capsys.readouterr().out
        assert hidden_dir not in status_out
        assert _status_source_count(status_out) == 1


class TestAKbUnderADotDirectoryIsNotEmpty:
    """`is_hidden_source` measures "hidden" relative to the SOURCE ROOT, not the
    filesystem root. Measuring it against an absolute path would make every source
    hidden for a KB at `~/.factlog-kb`, silently emptying a real user's library.
    Nothing pinned that."""

    @pytest.mark.parametrize("kb_rel", [".factlog-kb", ".a/.b/kb", "plain-kb"])
    def test_every_source_is_still_visible(self, tmp_path, kb_rel):
        kb = tmp_path / kb_rel
        (kb / "sources").mkdir(parents=True)
        (kb / "sources" / "paper.md").write_text("---\ntitle: P\n---\n", encoding="utf-8")
        (kb / "sources" / ".hidden").mkdir()
        (kb / "sources" / ".hidden" / "x.md").write_text("x", encoding="utf-8")

        found = [p.name for p in common.source_files(kb)]
        assert found == ["paper.md"], f"KB under {kb_rel!r} lost its sources"

    def test_the_dot_component_of_the_kb_path_is_not_treated_as_hidden(self, tmp_path):
        kb = tmp_path / ".factlog-kb"
        (kb / "sources").mkdir(parents=True)
        paper = kb / "sources" / "paper.md"
        paper.write_text("---\ntitle: P\n---\n", encoding="utf-8")
        assert not common.is_hidden_source(paper, kb / "sources")


class TestACandidateCitingAHiddenSourceIsDropped:
    """A behaviour change this issue did not ask for, pinned so it is not a
    surprise. `common.source_file_refs()` no longer returns dot-named paths, so
    `merge_candidates` drops a candidate citing one and `factlog provenance` calls
    it stale.

    The blast radius is nil in practice: no factlog pipeline ever created such a
    fact, because sync and coverage already skipped hidden paths (they used the
    path-parts check), and the importers only ever write `sources/<slug>.md`. Only
    a hand-placed file and a hand-written candidate can reach this."""

    def test_a_hidden_source_is_absent_from_the_on_disk_ref_set(self, tmp_path):
        (tmp_path / "sources" / ".prov").mkdir(parents=True)
        (tmp_path / "sources" / ".prov" / "x.md").write_text("x", encoding="utf-8")
        (tmp_path / "sources" / "paper.md").write_text("p", encoding="utf-8")

        refs = common.source_file_refs(tmp_path)
        assert "sources/paper.md" in refs
        assert "sources/.prov/x.md" not in refs

    def test_sync_never_created_such_a_fact_in_the_first_place(self, tmp_path):
        # The bound on the blast radius, asserted rather than argued: a file under
        # a hidden directory is not a source, so nothing can cite it legitimately.
        (tmp_path / "sources" / ".prov").mkdir(parents=True)
        hidden = tmp_path / "sources" / ".prov" / "x.md"
        hidden.write_text("---\ntitle: X\n---\n", encoding="utf-8")
        assert common.is_hidden_source(hidden, tmp_path / "sources")
        assert hidden not in common.source_files(tmp_path)
