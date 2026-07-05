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
