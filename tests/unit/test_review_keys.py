# SPDX-License-Identifier: Apache-2.0
"""Unit tests for existing_review_keys (#106 re-review preservation)."""
from __future__ import annotations

import merge_candidates as mc

HEADER = "subject,relation,object,source,status,confidence,note\n"


def _write_csv(tmp_path, *rows):
    facts = tmp_path / "facts"
    facts.mkdir()
    (facts / "candidates.csv").write_text(HEADER + "".join(r + "\n" for r in rows), encoding="utf-8")
    return tmp_path


class TestExistingReviewKeys:
    def test_collects_needs_review_rows(self, tmp_path):
        root = _write_csv(tmp_path, "A,rel,B,sources/a.md,needs_review,0.5,")
        assert ("A", "rel", "B", "sources/a.md") in mc.existing_review_keys(root)

    def test_ignores_accepted_and_candidate(self, tmp_path):
        root = _write_csv(
            tmp_path,
            "A,rel,B,sources/a.md,accepted,0.9,",
            "C,rel,D,sources/a.md,candidate,0.3,",
        )
        assert mc.existing_review_keys(root) == set()

    def test_anchor_insensitive_key(self, tmp_path):
        root = _write_csv(tmp_path, "A,rel,B,sources/a.md#sec2,needs_review,0.5,")
        # The '#sec2' anchor is stripped from the key.
        assert ("A", "rel", "B", "sources/a.md") in mc.existing_review_keys(root)

    def test_missing_csv_returns_empty(self, tmp_path):
        assert mc.existing_review_keys(tmp_path) == set()
