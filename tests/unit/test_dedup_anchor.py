# SPDX-License-Identifier: Apache-2.0
"""Unit tests for normalize_rows anchor-insensitive dedup (#135).

The raw candidate dedup key must strip any '#anchor', consistent with the
status-preservation keys (existing_superseded_keys / existing_engine_keys /
existing_review_keys).  Two rows sharing (subject, relation, object, source-file)
that differ only by anchor collapse to one row, and the surviving 'source' is
chosen deterministically: the full source that sorts lexicographically first
(bare path < any anchored variant), independent of input order.
"""
from __future__ import annotations

import merge_candidates as mc


def _root_with_source(tmp_path, name="a.md"):
    """A KB root whose sources/ holds one real file, so rows referencing it
    pass the source-existence check inside normalize_rows."""
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / name).write_text("# heading\n", encoding="utf-8")
    return tmp_path


def _row(subject, relation, obj, source, status="candidate", confidence="0.50", note=""):
    return {
        "subject": subject,
        "relation": relation,
        "object": obj,
        "source": source,
        "status": status,
        "confidence": confidence,
        "note": note,
    }


class TestAnchorInsensitiveDedup:
    def test_bare_and_anchored_collapse_to_one_row(self, tmp_path):
        root = _root_with_source(tmp_path)
        rows = [
            _row("A", "rel", "B", "sources/a.md"),
            _row("A", "rel", "B", "sources/a.md#sec1"),
        ]
        out = mc.normalize_rows(root, rows)
        assert len(out) == 1
        # Bare path sorts before the anchored variant, so it survives.
        assert out[0]["source"] == "sources/a.md"

    def test_two_anchors_collapse_keeping_lexicographically_first(self, tmp_path):
        root = _root_with_source(tmp_path)
        rows = [
            _row("A", "rel", "B", "sources/a.md#sec2"),
            _row("A", "rel", "B", "sources/a.md#sec1"),
        ]
        out = mc.normalize_rows(root, rows)
        assert len(out) == 1
        # Lexicographically-first anchor wins; no bare variant present.
        assert out[0]["source"] == "sources/a.md#sec1"

    def test_surviving_source_is_order_independent(self, tmp_path):
        root = _root_with_source(tmp_path)
        forward = [
            _row("A", "rel", "B", "sources/a.md#sec1"),
            _row("A", "rel", "B", "sources/a.md"),
        ]
        reverse = list(reversed(forward))
        out_forward = mc.normalize_rows(root, forward)
        out_reverse = mc.normalize_rows(root, reverse)
        assert len(out_forward) == len(out_reverse) == 1
        # Same surviving source regardless of which order the rows arrived in.
        assert out_forward[0]["source"] == out_reverse[0]["source"] == "sources/a.md"

    def test_distinct_triples_on_same_file_are_kept(self, tmp_path):
        root = _root_with_source(tmp_path)
        rows = [
            _row("A", "rel", "B", "sources/a.md#sec1"),
            _row("C", "rel", "D", "sources/a.md#sec2"),
        ]
        out = mc.normalize_rows(root, rows)
        assert len(out) == 2
