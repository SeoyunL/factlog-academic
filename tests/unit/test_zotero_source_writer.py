# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Zotero SourceWriter (phase 1, #7).

Covers slug generation (normal / anonymous / no-year / no-title / non-ASCII),
global-unique collision suffixes, idempotent re-import by zotero_key, P4
non-overwrite of existing originals, front-matter round-trip, and the
include_abstract switch.
"""
from __future__ import annotations

from factlog.integrations.zotero.source_writer import (
    SourceWriter,
    read_zotero_key,
)


def _parsed(**over):
    base = {
        "zotero_key": "ABCD1234",
        "item_type": "journalArticle",
        "title": "Omega-3 fatty acids and COPD",
        "authors": [{"last": "Matsuyama", "first": "W", "name": "Matsuyama W"}],
        "year": "2005",
        "date": "2005-06",
        "journal": "Chest",
        "doi": "10.1378/chest.128.6.3817",
        "pmid": "16354850",
        "abstract": "Background: ...",
        "tags": ["retracted", "omega-3"],
        "date_modified": "2020-01-02",
        "retracted": True,
    }
    base.update(over)
    return base


class TestGenerateSlug:
    def test_normal(self):
        slug = SourceWriter().generate_slug(_parsed())
        assert slug.startswith("matsuyama-2005-")
        assert slug.endswith(".md")

    def test_anonymous_when_no_authors(self):
        assert SourceWriter().generate_slug(_parsed(authors=[])).startswith("anonymous-2005-")

    def test_no_year_uses_nd_slot(self):
        slug = SourceWriter().generate_slug(_parsed(year=""))
        assert "-n-d-" in slug

    def test_no_title_uses_untitled(self):
        slug = SourceWriter().generate_slug(_parsed(title=""))
        assert slug.endswith("-untitled.md")

    def test_non_ascii_author_preserved(self):
        parsed = _parsed(authors=[{"last": "김무성", "first": "", "name": "김무성"}])
        assert SourceWriter().generate_slug(parsed).startswith("김무성-2005-")

    def test_long_title_truncated(self):
        long_title = "word " * 40  # slugifies to ~200 chars if untruncated
        slug = SourceWriter().generate_slug(_parsed(title=long_title))
        # Title portion is byte-capped, so the whole name stays well under the
        # filesystem limit and shorter than the untruncated slug would be.
        assert len(slug.encode("utf-8")) <= 120
        assert slug.count("word") < 40


class TestWrite:
    def test_writes_file_with_front_matter_and_body(self, tmp_path):
        res = SourceWriter().write(_parsed(), tmp_path, imported_at="2026-07-08T00:00:00Z")
        assert res.status == "imported"
        assert res.path.parent == tmp_path / "sources"
        text = res.path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert 'zotero_key: "ABCD1234"' in text
        assert 'title: "Omega-3 fatty acids and COPD"' in text
        assert 'authors: ["Matsuyama W"]' in text
        assert "imported_from: zotero" in text
        assert 'imported_at: "2026-07-08T00:00:00Z"' in text
        assert "retracted: true" in text
        assert "# Omega-3 fatty acids and COPD" in text
        assert "## Abstract" in text
        assert "zotero://select/library/items/ABCD1234" in text
        assert "DOI: 10.1378/chest.128.6.3817" in text
        assert "PMID: 16354850" in text

    def test_front_matter_key_round_trips(self, tmp_path):
        res = SourceWriter().write(_parsed(), tmp_path)
        assert read_zotero_key(res.path) == "ABCD1234"

    def test_include_abstract_false_omits_section(self, tmp_path):
        res = SourceWriter(include_abstract=False).write(_parsed(), tmp_path)
        text = res.path.read_text(encoding="utf-8")
        assert "## Abstract" not in text
        assert "## Original source" in text

    def test_optional_empty_fields_omitted(self, tmp_path):
        res = SourceWriter().write(
            _parsed(doi="", pmid="", journal="", tags=[], retracted=False), tmp_path
        )
        text = res.path.read_text(encoding="utf-8")
        assert "doi:" not in text
        assert "pmid:" not in text
        assert "journal:" not in text
        assert "tags:" not in text
        assert "retracted:" not in text

    def test_no_imported_at_omits_field(self, tmp_path):
        text = SourceWriter().write(_parsed(), tmp_path).path.read_text(encoding="utf-8")
        assert "imported_at:" not in text


class TestIdempotency:
    def test_reimport_same_key_is_skipped(self, tmp_path):
        w = SourceWriter()
        first = w.write(_parsed(), tmp_path, imported_at="2026-01-01T00:00:00Z")
        assert first.status == "imported"
        second = w.write(_parsed(), tmp_path, imported_at="2026-02-02T00:00:00Z")
        assert second.status == "skipped"
        assert second.path == first.path
        # Only one file exists; the original was not rewritten.
        assert len(list((tmp_path / "sources").glob("*.md"))) == 1

    def test_reimport_leaves_file_bytes_unchanged(self, tmp_path):
        w = SourceWriter()
        first = w.write(_parsed(), tmp_path, imported_at="2026-01-01T00:00:00Z")
        before = first.path.read_bytes()
        w.write(_parsed(), tmp_path, imported_at="2026-09-09T00:00:00Z")
        assert first.path.read_bytes() == before

    def test_skip_disabled_still_no_overwrite_new_file(self, tmp_path):
        # With skip_duplicates=False the same item is written again, but to a
        # NEW globally-unique path — the original is still never overwritten.
        w = SourceWriter(skip_duplicates=False)
        first = w.write(_parsed(), tmp_path)
        second = w.write(_parsed(), tmp_path)
        assert second.status == "imported"
        assert second.path != first.path
        assert first.path.exists() and second.path.exists()


class TestGlobalUnique:
    def test_different_items_same_slug_get_suffix(self, tmp_path):
        w = SourceWriter()
        a = w.write(_parsed(zotero_key="KEYA"), tmp_path)
        b = w.write(_parsed(zotero_key="KEYB"), tmp_path)  # same author/year/title
        assert a.path != b.path
        assert b.path.name.endswith("-2.md")
        assert read_zotero_key(a.path) == "KEYA"
        assert read_zotero_key(b.path) == "KEYB"

    def test_third_collision_gets_dash_three(self, tmp_path):
        w = SourceWriter()
        w.write(_parsed(zotero_key="K1"), tmp_path)
        w.write(_parsed(zotero_key="K2"), tmp_path)
        c = w.write(_parsed(zotero_key="K3"), tmp_path)
        assert c.path.name.endswith("-3.md")

    def test_does_not_overwrite_unrelated_existing_file(self, tmp_path):
        sources = tmp_path / "sources"
        sources.mkdir()
        slug = SourceWriter().generate_slug(_parsed())
        squatter = sources / slug
        squatter.write_text("# a user's own source, no front matter\n", encoding="utf-8")
        res = SourceWriter().write(_parsed(), tmp_path)
        assert res.path != squatter
        assert squatter.read_text(encoding="utf-8").startswith("# a user's own source")


class TestPlan:
    def test_plan_creates_no_file_but_predicts_path(self, tmp_path):
        res = SourceWriter().plan(_parsed(), tmp_path)
        assert res.status == "imported"
        assert res.path.name.endswith(".md")
        assert not (tmp_path / "sources").exists() or not list((tmp_path / "sources").glob("*.md"))

    def test_plan_matches_write_decision(self, tmp_path):
        planned = SourceWriter().plan(_parsed(), tmp_path)
        written = SourceWriter().write(_parsed(), tmp_path)
        assert planned.status == written.status
        assert planned.path.name == written.path.name

    def test_plan_predicts_collision_suffix_within_batch(self, tmp_path):
        w = SourceWriter()
        a = w.plan(_parsed(zotero_key="A"), tmp_path)
        b = w.plan(_parsed(zotero_key="B"), tmp_path)  # same slug base
        assert a.path.name != b.path.name
        assert b.path.name.endswith("-2.md")
        assert not (tmp_path / "sources").exists() or not list((tmp_path / "sources").glob("*.md"))

    def test_plan_predicts_skip_for_existing_key(self, tmp_path):
        SourceWriter().write(_parsed(), tmp_path)
        assert SourceWriter().plan(_parsed(), tmp_path).status == "skipped"

    def test_plan_missing_key_is_error(self, tmp_path):
        assert SourceWriter().plan(_parsed(zotero_key=""), tmp_path).status == "error"

    def test_plan_then_write_same_instance_still_writes(self, tmp_path):
        # Regression: plan() must not pollute write()'s index and make it skip.
        w = SourceWriter()
        planned = w.plan(_parsed(), tmp_path)
        written = w.write(_parsed(), tmp_path)
        assert written.status == "imported"
        assert written.path.name == planned.path.name
        assert written.path.exists()

    def test_repeated_plan_predicts_batch_collisions(self, tmp_path):
        # Consecutive plans of distinct items sharing a slug base still predict -2.
        w = SourceWriter()
        a = w.plan(_parsed(zotero_key="A"), tmp_path)
        b = w.plan(_parsed(zotero_key="B"), tmp_path)
        assert b.path.name.endswith("-2.md") and a.path.name != b.path.name


class TestReadZoteroKey:
    def test_missing_front_matter_returns_empty(self, tmp_path):
        f = tmp_path / "plain.md"
        f.write_text("# just a source\n", encoding="utf-8")
        assert read_zotero_key(f) == ""

    def test_nonexistent_file_returns_empty(self, tmp_path):
        assert read_zotero_key(tmp_path / "nope.md") == ""

    def test_annotation_source_not_treated_as_bib_key(self, tmp_path):
        # A companion notes file carries a zotero_key but must not be picked as the
        # bibliographic source for that key (would mis-pair on re-import).
        f = tmp_path / "x-notes.md"
        f.write_text('---\nsource_kind: annotations\nzotero_key: "K1"\n---\n', encoding="utf-8")
        assert read_zotero_key(f) == ""

    def test_marker_substring_in_title_is_not_annotation(self, tmp_path):
        # A real bib file whose TITLE merely contains the marker text must still
        # yield its key (line-anchored marker match, no false positive).
        f = tmp_path / "bib.md"
        f.write_text('---\nzotero_key: "K1"\ntitle: "On source_kind: annotations"\n---\n',
                     encoding="utf-8")
        assert read_zotero_key(f) == "K1"

    def test_body_zotero_key_text_not_matched(self, tmp_path):
        # A `zotero_key:` line in the BODY (after the closing fence) must not be
        # mistaken for the front-matter key.
        f = tmp_path / "s.md"
        f.write_text(
            '---\nzotero_key: "REAL"\n---\n\n# t\n\nSee zotero_key: "FAKE" in a quote.\n',
            encoding="utf-8",
        )
        assert read_zotero_key(f) == "REAL"


class TestYamlEscaping:
    def test_newline_tab_quote_backslash_escaped(self, tmp_path):
        parsed = _parsed(title='a\nb\tc "q" \\z', journal="Jour: nal")
        text = SourceWriter().write(parsed, tmp_path).path.read_text(encoding="utf-8")
        # The front matter title stays on ONE physical line.
        fm = text.split("---")[1]
        title_lines = [ln for ln in fm.splitlines() if ln.startswith("title:")]
        assert len(title_lines) == 1
        assert r"\n" in title_lines[0] and r"\t" in title_lines[0]
        assert r"\"q\"" in title_lines[0] and r"\\z" in title_lines[0]

    def test_control_char_becomes_hex(self, tmp_path):
        text = SourceWriter().write(_parsed(title="a\x01b"), tmp_path).path.read_text("utf-8")
        assert r"\x01" in text

    def test_key_round_trips_despite_messy_title(self, tmp_path):
        res = SourceWriter().write(_parsed(title="line1\nline2"), tmp_path)
        assert read_zotero_key(res.path) == "ABCD1234"


class TestMissingKey:
    def test_missing_key_is_error_no_file(self, tmp_path):
        res = SourceWriter().write(_parsed(zotero_key=""), tmp_path)
        assert res.status == "error"
        assert res.path is None
        assert not list((tmp_path / "sources").glob("*.md")) if (tmp_path / "sources").is_dir() else True

    def test_missing_key_does_not_proliferate(self, tmp_path):
        w = SourceWriter()
        w.write(_parsed(zotero_key=""), tmp_path)
        w.write(_parsed(zotero_key=""), tmp_path)
        srcdir = tmp_path / "sources"
        assert not srcdir.is_dir() or not list(srcdir.glob("*.md"))


class TestFilenameByteCap:
    def test_long_non_ascii_author_stays_within_fs_limit(self, tmp_path):
        parsed = _parsed(authors=[{"last": "가" * 300, "first": "", "name": "가" * 300}])
        res = SourceWriter().write(parsed, tmp_path)
        assert res.status == "imported"
        assert len(res.path.name.encode("utf-8")) <= 255

    def test_item_title_not_forced_to_untitled(self, tmp_path):
        slug = SourceWriter().generate_slug(_parsed(title="Item Response Theory"))
        assert "item-response-theory" in slug
        assert "untitled" not in slug
