# SPDX-License-Identifier: Apache-2.0
"""Regression tests for stale-source ref detection (#101, #102)."""
from __future__ import annotations

import merge_candidates
import resolve_stale_refs


class TestStaleRe:
    """resolve_stale_refs must match refs with the optional runs/ prefix (#101)."""

    def test_matches_plain_sources(self):
        line = "- stale_source: pages/p.md references removed source sources/a.md"
        m = resolve_stale_refs.STALE_RE.match(line)
        assert m and m.group("source") == "sources/a.md"

    def test_matches_runs_sources(self):
        line = "- stale_source: pages/p.md references removed source runs/sources/a.md"
        m = resolve_stale_refs.STALE_RE.match(line)
        assert m and m.group("source") == "runs/sources/a.md"


class TestExistingSourceRefs:
    """merge_candidates must find txt/csv/runs refs, not just .md (#102)."""

    def _refs(self, tmp_path, body):
        p = tmp_path / "page.md"
        p.write_text(body, encoding="utf-8")
        return merge_candidates.existing_source_refs(p)

    def test_finds_md(self, tmp_path):
        assert "sources/a.md" in self._refs(tmp_path, "see (sources/a.md)")

    def test_finds_txt(self, tmp_path):
        assert "sources/notes.txt" in self._refs(tmp_path, "see (sources/notes.txt)")

    def test_finds_csv(self, tmp_path):
        assert "sources/data.csv" in self._refs(tmp_path, "see (sources/data.csv)")

    def test_finds_runs_sources(self, tmp_path):
        assert "runs/sources/x.txt" in self._refs(tmp_path, "see (runs/sources/x.txt)")
