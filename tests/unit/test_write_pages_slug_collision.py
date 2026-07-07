# SPDX-License-Identifier: Apache-2.0
"""Regression tests: distinct entities whose titles slugify to the same name must
each keep a concept page (#258).

`slugify` collapses every run of non-alphanumerics to a single '-', so "A B",
"A-B", and "A/B" all map to `a-b.md`. write_pages iterated distinct entities and
wrote each to `pages/<slug>.md`, so the second colliding entity silently
OVERWROTE the first's page — one concept page lost (the source rows survive in
candidates.csv, but the page view does not). The long-title path already
disambiguates with a hash suffix (tests/unit/test_page_filename.py); this pins
the same no-collision invariant for short slugs, at the write layer where the
collision actually manifests.
"""
from __future__ import annotations

import merge_candidates


def _row(subject, object_, source):
    return {
        "subject": subject,
        "relation": "관계",
        "object": object_,
        "source": source,
        "status": "confirmed",
        "confidence": "0.90",
        "note": "",
    }


class TestWritePagesSlugCollision:
    def test_slug_colliding_entities_keep_distinct_pages(self, tmp_path):
        rows = [
            _row("A B", "obj-one", "sources/a.md"),
            _row("A-B", "obj-two", "sources/b.md"),
        ]
        merge_candidates.write_pages(tmp_path, rows)
        blob = "\n".join(
            p.read_text(encoding="utf-8") for p in (tmp_path / "pages").glob("*.md")
        )
        # The template renders each concept as an H1 '# {{ENTITY}}'. Both distinct
        # subjects must keep their own page — neither overwritten by the slug twin.
        assert "# A B\n" in blob, "concept page for 'A B' was lost to a slug collision"
        assert "# A-B\n" in blob, "concept page for 'A-B' was lost to a slug collision"

    def test_non_colliding_titles_keep_clean_slug(self, tmp_path):
        # Regression anchor: a normal, non-colliding title keeps its readable slug
        # (no gratuitous hash suffix churn on the common case).
        rows = [_row("New York", "obj", "sources/a.md")]
        merge_candidates.write_pages(tmp_path, rows)
        pages = {p.name for p in (tmp_path / "pages").glob("*.md")}
        assert "new-york.md" in pages  # clean slug preserved (object also gets its own page)

    def test_reverse_collision_suffix_matches_another_base(self, tmp_path):
        # Adversarial #258 reverse-collision. The hash used to disambiguate "A B"/
        # "A-B" is a-b-<sha1("A B")[:10]>.md, which is ALSO the clean slug of a third
        # distinct title `f"A B {that_hash}"`. A base-only uniqueness check let that
        # third entity keep the name and be silently overwritten by "A B"'s suffix —
        # 3 distinct subjects, 2 pages, one H1 lost. The global-unique pre-pass must
        # give all three distinct pages.
        import hashlib

        h = hashlib.sha1("A B".encode("utf-8")).hexdigest()[:10]
        third = f"A B {h}"  # slugifies to a-b-<h>, colliding with "A B"'s suffix
        rows = [
            _row("A B", "obj-one", "sources/a.md"),
            _row("A-B", "obj-two", "sources/b.md"),
            _row(third, "obj-three", "sources/c.md"),
        ]
        merge_candidates.write_pages(tmp_path, rows)
        pages = list((tmp_path / "pages").glob("*.md"))
        blob = "\n".join(p.read_text(encoding="utf-8") for p in pages)
        assert "# A B\n" in blob, "'A B' page lost to reverse collision"
        assert "# A-B\n" in blob, "'A-B' page lost to slug collision"
        assert f"# {third}\n" in blob, "third entity's page lost to reverse collision"
        # 3 distinct subjects + 3 distinct objects = 6 distinct pages (5 under the bug).
        assert len(pages) == 6

    def test_all_nonalnum_titles_keep_distinct_pages(self, tmp_path):
        # slugify falls back to "item" for all-non-alphanumeric titles, so "!!!" and
        # "@@@" share base item.md. The global-unique pre-pass must still give each a
        # distinct page (this class was previously uncovered).
        rows = [
            _row("!!!", "obj-a", "sources/a.md"),
            _row("@@@", "obj-b", "sources/b.md"),
        ]
        merge_candidates.write_pages(tmp_path, rows)
        blob = "\n".join(
            p.read_text(encoding="utf-8") for p in (tmp_path / "pages").glob("*.md")
        )
        assert "# !!!\n" in blob, "'!!!' page lost to the 'item' fallback collision"
        assert "# @@@\n" in blob, "'@@@' page lost to the 'item' fallback collision"
