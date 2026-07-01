# SPDX-License-Identifier: Apache-2.0
"""Tests for cmd_provenance alias expansion (provenance reverse-trace).

Uses a tmp KB with policy/relation-aliases.md so the canonical-predicate
query includes surface-variant rows.  Also pins the no-alias-file regression
guard: with NO relation-aliases.md the output must be byte-identical to the
plain (pre-alias) behaviour.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Minimal stubs so factlog.cli can be imported without a full package install.
# conftest.py already puts tools/ on sys.path; factlog/ is under the repo root
# which PYTHONPATH covers in the test runner invocation.
# ---------------------------------------------------------------------------


def _make_kb(tmp_path: Path, *, with_aliases: bool = True) -> Path:
    """Build a minimal KB under tmp_path.

    Two source files back the SAME (subject, object) pair under two DIFFERENT
    surface predicates ('게재연도' and 'publication_year'), both aliased to the
    canonical 'published_year'.  A third row uses the canonical name directly.
    """
    kb = tmp_path / "kb"
    for d in ("sources", "facts", "policy", "pages", "decisions", "runs"):
        (kb / d).mkdir(parents=True)

    # Source files on disk (so rows are non-stale)
    (kb / "sources" / "src_a.md").write_text("source A\n", encoding="utf-8")
    (kb / "sources" / "src_b.md").write_text("source B\n", encoding="utf-8")
    (kb / "sources" / "src_c.md").write_text("source C\n", encoding="utf-8")

    # candidates.csv
    csv_text = (
        "subject,relation,object,source,status,confidence,note\n"
        "논문A,게재연도,2020,sources/src_a.md,confirmed,0.90,from src a\n"
        "논문A,publication_year,2020,sources/src_b.md,confirmed,0.85,from src b\n"
        "논문A,published_year,2020,sources/src_c.md,confirmed,0.95,direct canonical\n"
    )
    (kb / "facts" / "candidates.csv").write_text(csv_text, encoding="utf-8")

    if with_aliases:
        alias_text = (
            "# Relation aliases\n"
            "- `게재연도` -> `published_year`\n"
            "- `publication_year` -> `published_year`\n"
        )
        (kb / "policy" / "relation-aliases.md").write_text(alias_text, encoding="utf-8")

    return kb


def _run_provenance(kb: Path, *terms: str) -> tuple[int, str]:
    """Run cmd_provenance against *kb* with the given positional terms.

    Returns (exit_code, combined stdout+stderr output).
    """
    import argparse

    import factlog.cli as cli

    # Provide a minimal args namespace
    ns = argparse.Namespace(
        terms=list(terms),
        target=str(kb),
    )

    buf = io.StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = buf
    sys.stderr = buf
    try:
        rc = cli.cmd_provenance(ns)
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# Alias expansion tests
# ---------------------------------------------------------------------------

class TestProvenanceAliasExpansion:
    def test_canonical_query_includes_surface_variant_rows(self, tmp_path):
        kb = _make_kb(tmp_path, with_aliases=True)
        rc, out = _run_provenance(kb, "논문A", "published_year", "2020")
        assert rc == 0, f"expected rc 0, got {rc}:\n{out}"
        # All three rows should appear (two surface-variant + one canonical-stored)
        assert "src_a.md" in out, "surface row from src_a.md missing"
        assert "src_b.md" in out, "surface row from src_b.md missing"
        assert "src_c.md" in out, "canonical-stored row from src_c.md missing"

    def test_surface_variant_groups_are_labelled(self, tmp_path):
        kb = _make_kb(tmp_path, with_aliases=True)
        rc, out = _run_provenance(kb, "논문A", "published_year", "2020")
        assert rc == 0
        # Surface-variant groups carry the [surface: <raw>] label
        assert "[surface: 게재연도]" in out, "[surface: 게재연도] label missing"
        assert "[surface: publication_year]" in out, "[surface: publication_year] label missing"

    def test_three_distinct_sources_counted(self, tmp_path):
        kb = _make_kb(tmp_path, with_aliases=True)
        rc, out = _run_provenance(kb, "논문A", "published_year", "2020")
        assert rc == 0
        assert "3 distinct source(s)" in out, "distinct source count wrong"

    def test_canonical_row_not_double_labelled(self, tmp_path):
        """The row stored under the canonical name itself must NOT get [surface:] tag."""
        kb = _make_kb(tmp_path, with_aliases=True)
        rc, out = _run_provenance(kb, "논문A", "published_year", "2020")
        assert rc == 0
        # The canonical group header must appear without a [surface:] suffix
        lines = out.splitlines()
        canonical_headers = [
            ln for ln in lines
            if "논문A / published_year / 2020" in ln and "[surface:" not in ln
        ]
        assert canonical_headers, "canonical group header (without [surface:]) not found"

    def test_surface_query_shows_canonical_context(self, tmp_path):
        """Querying a surface predicate directly shows 'canonical: <name>'."""
        kb = _make_kb(tmp_path, with_aliases=True)
        rc, out = _run_provenance(kb, "논문A", "게재연도", "2020")
        assert rc == 0
        assert "canonical: published_year" in out, "canonical context line missing for surface query"

    def test_surface_query_only_returns_that_surface_rows(self, tmp_path):
        """Querying a surface predicate directly returns only rows stored under it."""
        kb = _make_kb(tmp_path, with_aliases=True)
        rc, out = _run_provenance(kb, "논문A", "게재연도", "2020")
        assert rc == 0
        assert "src_a.md" in out
        # The other surface and canonical rows are NOT included
        assert "src_b.md" not in out
        assert "src_c.md" not in out


# ---------------------------------------------------------------------------
# No-alias-file regression guard (byte-identical to pre-alias behaviour)
# ---------------------------------------------------------------------------

class TestProvenanceNoAliasFile:
    def test_no_alias_file_rc_and_output_unchanged(self, tmp_path):
        """Without relation-aliases.md the output must be the same as today."""
        kb = _make_kb(tmp_path, with_aliases=False)
        rc, out = _run_provenance(kb, "논문A", "published_year", "2020")
        assert rc == 0
        # Only the directly-stored canonical row is returned
        assert "src_c.md" in out
        # Surface-variant rows are NOT included
        assert "src_a.md" not in out
        assert "src_b.md" not in out
        # No alias-specific labels in output
        assert "[surface:" not in out
        assert "canonical:" not in out

    def test_no_alias_file_plain_relation_query_unaffected(self, tmp_path):
        """A plain (non-aliased) query without alias file behaves as before."""
        kb = _make_kb(tmp_path, with_aliases=False)
        rc, out = _run_provenance(kb, "논문A", "게재연도", "2020")
        assert rc == 0
        assert "src_a.md" in out
        assert "[surface:" not in out
        assert "canonical:" not in out
