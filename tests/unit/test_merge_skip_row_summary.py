# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the folded 'skip row' diagnostic in normalize_rows (#492).

A missing source used to warn once per row, so a few stale paths pushed the
merge summary -- and the validate failures after it -- off the screen.  The
warning is now one line per anchor-stripped source path with a row count, in
path order, and it must still appear on the --strict early exit.
"""
from __future__ import annotations

import pytest

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


def _missing_rows():
    """3 rows on one missing source (two of them differing only by anchor, so
    the fold must key on the anchor-stripped path) + 1 row on another."""
    return [
        _row("A", "rel", "B", "sources/gone.md"),
        _row("C", "rel", "D", "sources/gone.md#sec1"),
        _row("E", "rel", "F", "sources/gone.md#sec2"),
        _row("G", "rel", "H", "sources/other.md"),
    ]


def _skip_lines(capsys):
    err = capsys.readouterr().err
    return [line for line in err.splitlines() if line.strip().startswith("skip row:")]


class TestSkipRowSummary:
    def test_one_line_per_source_with_row_counts(self, tmp_path, capsys):
        root = _root_with_source(tmp_path)
        mc.normalize_rows(root, _missing_rows())
        lines = _skip_lines(capsys)
        assert len(lines) == 2
        assert "sources/gone.md" in lines[0] and "(3 rows)" in lines[0]
        assert "sources/other.md" in lines[1] and "(1 row)" in lines[1]

    def test_singular_and_plural_are_not_conflated(self, tmp_path, capsys):
        root = _root_with_source(tmp_path)
        mc.normalize_rows(root, _missing_rows())
        lines = _skip_lines(capsys)
        assert not any("row(s)" in line for line in lines)

    def test_line_order_is_independent_of_input_order(self, tmp_path, capsys):
        root = _root_with_source(tmp_path)
        forward = _missing_rows()
        mc.normalize_rows(root, forward)
        lines_forward = _skip_lines(capsys)
        mc.normalize_rows(root, list(reversed(forward)))
        lines_reverse = _skip_lines(capsys)
        assert lines_forward == lines_reverse

    def test_dropped_summary_and_returned_rows_unchanged(self, tmp_path, capsys):
        root = _root_with_source(tmp_path)
        rows = _missing_rows() + [_row("K", "rel", "L", "sources/a.md")]
        out = mc.normalize_rows(root, rows)
        err = capsys.readouterr().err
        assert "  warning: 4 row(s) dropped during normalise/dedup" in err
        # Only the row whose source exists survives.
        assert len(out) == 1
        assert out[0]["source"] == "sources/a.md"

    def test_strict_prints_the_summary_before_exiting(self, tmp_path, capsys):
        root = _root_with_source(tmp_path)
        with pytest.raises(SystemExit) as excinfo:
            mc.normalize_rows(root, _missing_rows(), strict=True)
        # strict still dies on the FIRST offending row, message unchanged.
        assert "--strict: input row rejected (source not found): sources/gone.md" in str(
            excinfo.value
        )
        lines = _skip_lines(capsys)
        assert len(lines) == 1
        assert "sources/gone.md" in lines[0] and "(1 row)" in lines[0]
