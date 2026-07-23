# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the lookup key of ``excluded_sources_by_id`` (#428).

The consumer this file guards is a *lookup*, not an export: every ``--id`` caller
(``pubmed-acknowledge-retraction``, ``arxiv-acknowledge-withdrawal``,
``openalex-acknowledge-retraction``) validates and canonicalises the operator's
id before asking, while the dict was keyed on the front-matter value exactly as
stored. A source whose id was spelled differently could therefore never be found,
and the caller answered "not in this KB" about a paper that is in it — the one
sentence #112 built this function to stop saying.

Kept apart from ``test_nested_and_runs_sources.py``, which pins *which files* are
excluded. This pins *what the answer is keyed by*; a mutant in one should not be
able to hide behind the other's coverage.
"""
from __future__ import annotations

from factlog.integrations.common.provenance import excluded_sources_by_id


def _kb(tmp_path):
    """A KB whose ``sources/`` exists and whose excluded sources live elsewhere."""
    kb = tmp_path / "kb"
    (kb / "sources").mkdir(parents=True)
    return kb


def _excluded(kb, name, body):
    """Write ``runs/sources/<name>`` — inside the KB, outside the provenance root."""
    outside = kb / "runs" / "sources"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / name).write_text(f"---\n{body}\n---\n\nbody\n", encoding="utf-8")


class TestTheKeyIsNormalized:
    def test_a_full_width_pmid_is_found_by_its_ascii_id(self, tmp_path):
        # The measured failure: `pubmed-acknowledge-retraction --id 12345678`
        # normalizes to ASCII digits, the stored value is a pre-#398 full-width
        # spelling of the same PMID, and the lookup missed forever.
        kb = _kb(tmp_path)
        _excluded(kb, "a.md", 'pmid: "１２３４５６７８"')
        assert excluded_sources_by_id(kb, "pmid").get("12345678") == ("runs/sources/a.md",)

    def test_a_version_pinned_arxiv_id_is_found_by_its_base_id(self, tmp_path):
        # `arxiv-acknowledge-withdrawal` keys on `normalize_arxiv_id(...).base`
        # and refuses a version pin outright, so a stored `v2` could not match.
        kb = _kb(tmp_path)
        _excluded(kb, "b.md", 'arxiv_id: "2311.09277v2"')
        assert excluded_sources_by_id(kb, "arxiv_id").get("2311.09277") == \
            ("runs/sources/b.md",)

    def test_an_old_style_arxiv_subject_class_is_dropped(self, tmp_path):
        kb = _kb(tmp_path)
        _excluded(kb, "c.md", 'arxiv_id: "math.GT/0309136"')
        assert excluded_sources_by_id(kb, "arxiv_id").get("math/0309136") == \
            ("runs/sources/c.md",)

    def test_an_already_canonical_id_is_unchanged(self, tmp_path):
        # The fix must not move a key that was already right; every caller that
        # works today keeps working.
        kb = _kb(tmp_path)
        _excluded(kb, "d.md", 'pmid: "16354850"')
        assert excluded_sources_by_id(kb, "pmid") == {"16354850": ("runs/sources/d.md",)}

    def test_the_paths_are_untouched_by_the_normalization(self, tmp_path):
        # Only the key is derived. The value is the KB-relative path, and folding
        # a key must not disturb it.
        kb = _kb(tmp_path)
        nested = kb / "runs" / "sources" / "deep"
        nested.mkdir(parents=True)
        (nested / "e.md").write_text('---\npmid: "１２３"\n---\n\nbody\n', encoding="utf-8")
        assert excluded_sources_by_id(kb, "pmid") == {"123": ("runs/sources/deep/e.md",)}


class TestTwoSpellingsAreOnePaper:
    def test_they_collapse_into_one_entry_listing_both_paths(self, tmp_path):
        # Before the fix these were two entries, so an enumerating caller
        # (`backfill`, the refresh checks) reported one paper as two rows.
        kb = _kb(tmp_path)
        _excluded(kb, "a.md", 'pmid: "１２３"')
        _excluded(kb, "b.md", 'pmid: "123"')
        assert excluded_sources_by_id(kb, "pmid") == {
            "123": ("runs/sources/a.md", "runs/sources/b.md")
        }

    def test_different_papers_still_get_separate_entries(self, tmp_path):
        # The negative twin: collapsing is by identity, not by proximity. A
        # normalizer that folded too hard would merge these.
        kb = _kb(tmp_path)
        _excluded(kb, "a.md", 'pmid: "１２３"')
        _excluded(kb, "b.md", 'pmid: "124"')
        assert sorted(excluded_sources_by_id(kb, "pmid")) == ["123", "124"]


class TestTheToleranceAndItsLimit:
    """What the fix repairs and what it deliberately does NOT, measured not assumed.

    ``normalize_cross_id`` is used precisely because it runs over hand-editable
    files without raising. It repairs a *spelling* of a well-formed id always, and
    a *wrapping* of one only for the identifiers whose normalizer unwraps a URL:
    ``openalex_id`` gains that here (#444), while ``pmid`` still does not. These
    pin the boundary so a future reader neither over- nor under-reads the fix.
    """

    def test_a_malformed_id_does_not_abort_the_report(self, tmp_path):
        # The reason a CLI validator could not be used here: `normalize_arxiv_id`
        # raises on this, and one bad file would take out a whole KB's report.
        kb = _kb(tmp_path)
        _excluded(kb, "bad.md", 'arxiv_id: "not-an-id"')
        _excluded(kb, "good.md", 'arxiv_id: "2311.09277v2"')
        result = excluded_sources_by_id(kb, "arxiv_id")
        assert result["not-an-id"] == ("runs/sources/bad.md",)
        assert result["2311.09277"] == ("runs/sources/good.md",)

    def test_a_labelled_pmid_still_misses(self, tmp_path):
        kb = _kb(tmp_path)
        _excluded(kb, "a.md", 'pmid: "pmid:123"')
        result = excluded_sources_by_id(kb, "pmid")
        assert result.get("123") is None
        assert result["pmid:123"] == ("runs/sources/a.md",)

    def test_an_openalex_url_is_found_by_its_bare_id(self, tmp_path):
        # Was `test_an_openalex_url_still_misses`, the characterization test that
        # pinned the pre-#444 miss. #444 added an `openalex_id` branch to
        # `normalize_cross_id` (reusing `normalize_work_id`), so a hand-edited
        # source storing the URL form is now found by the bare id that
        # `openalex-acknowledge-retraction --id W1` canonicalises to. This was safe
        # to widen here because `openalex_id` is not a dedup join key (it is absent
        # from `CROSS_SOURCE_IDS`); this lookup is the branch's only caller.
        kb = _kb(tmp_path)
        _excluded(kb, "a.md", 'openalex_id: "https://openalex.org/W1"')
        result = excluded_sources_by_id(kb, "openalex_id")
        assert result.get("W1") == ("runs/sources/a.md",)
        assert result.get("https://openalex.org/W1") is None

    def test_a_stored_openalex_id_that_needs_no_repair_is_unmoved(self, tmp_path):
        # The negative twin of the widening: a bare, already-canonical `W1`
        # normalizes to itself, so the common case is byte-identical to the old
        # `.strip()` and the key does not move.
        kb = _kb(tmp_path)
        _excluded(kb, "a.md", 'openalex_id: "W1"')
        assert excluded_sources_by_id(kb, "openalex_id").get("W1") == ("runs/sources/a.md",)

    def test_a_malformed_openalex_id_does_not_abort_the_report(self, tmp_path):
        # `normalize_work_id` raises on a zero-padded/`W0` id; the branch catches it
        # and falls back to `.strip()`, so one bad hand-edited file cannot take out
        # a whole KB's report — the same tolerance the arxiv branch relies on.
        kb = _kb(tmp_path)
        _excluded(kb, "bad.md", 'openalex_id: "W0"')
        _excluded(kb, "good.md", 'openalex_id: "https://openalex.org/W1"')
        result = excluded_sources_by_id(kb, "openalex_id")
        assert result["W0"] == ("runs/sources/bad.md",)
        assert result["W1"] == ("runs/sources/good.md",)
