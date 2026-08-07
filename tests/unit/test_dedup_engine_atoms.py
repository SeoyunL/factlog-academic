# SPDX-License-Identifier: Apache-2.0
"""Regression tests for dedup_engine_atoms triple collapse (#191).

The same (subject, relation, object) accepted from several sources must become
a single engine atom so accepted.dl / ask / run_logic_check use set semantics
(one row, true count) instead of an inflated, duplicated count. The collapse is
first-occurrence stable (not sort-min) so accepted.dl stays byte-identical when
the KB has no duplicate triple. Source aggregation lives on the separate
candidates path and is untouched.

Sameness is `common.engine_atom_key` — subject and object folded to NFC, the
relation verbatim (#342). Two canonically equivalent spellings of one fact are
one atom, not two byte-different visually identical `relation(...)` lines. What
gets WRITTEN is still a row as authored: the group's composed-preferred member,
never a normalized synthesis, so a uniformly decomposed KB keeps its spelling.
"""
from __future__ import annotations

import unicodedata

import common


def _nfc(value):
    return unicodedata.normalize("NFC", value)


def _nfd(value):
    return unicodedata.normalize("NFD", value)


def _row(subject, relation, object_, **extra):
    row = {"subject": subject, "relation": relation, "object": object_}
    row.update(extra)
    return row


class TestDedupEngineAtoms:
    def test_multi_source_same_triple_collapses_to_one(self):
        rows = [
            _row("PMID:16354850", "게재저널", "Chest", source="sources/a.md"),
            _row("PMID:16354850", "게재저널", "Chest", source="sources/b.md"),
        ]
        out = common.dedup_engine_atoms(rows)
        assert len(out) == 1
        assert (out[0]["subject"], out[0]["relation"], out[0]["object"]) == (
            "PMID:16354850",
            "게재저널",
            "Chest",
        )

    def test_first_occurrence_is_kept(self):
        rows = [
            _row("A", "r", "B", source="first"),
            _row("A", "r", "B", source="second"),
        ]
        out = common.dedup_engine_atoms(rows)
        assert len(out) == 1
        # stable, not sort-min: the first-seen row survives verbatim
        assert out[0]["source"] == "first"

    def test_three_or_more_sources_collapse_to_one(self):
        rows = [
            _row("A", "r", "B", source="s1"),
            _row("A", "r", "B", source="s2"),
            _row("A", "r", "B", source="s3"),
            _row("A", "r", "B", source="s4"),
        ]
        out = common.dedup_engine_atoms(rows)
        assert len(out) == 1
        assert out[0]["source"] == "s1"  # first-occurrence survives

    def test_scattered_duplicates_keep_first_and_preserve_order(self):
        # a=same triple appearing 3x, interleaved with distinct b and c:
        # [a, b, a, c, a] -> [a, b, c] with a's FIRST occurrence retained.
        rows = [
            _row("A", "r", "B", source="a1"),
            _row("X", "r", "Y", source="b1"),
            _row("A", "r", "B", source="a2"),
            _row("P", "r", "Q", source="c1"),
            _row("A", "r", "B", source="a3"),
        ]
        out = common.dedup_engine_atoms(rows)
        keys = [(r["subject"], r["relation"], r["object"]) for r in out]
        assert keys == [("A", "r", "B"), ("X", "r", "Y"), ("P", "r", "Q")]
        # the first-seen row for the scattered triple is the one kept
        assert out[0]["source"] == "a1"

    def test_distinct_triples_preserve_order(self):
        rows = [
            _row("A", "r", "B"),
            _row("A", "r", "C"),
            _row("A", "s", "B"),
        ]
        out = common.dedup_engine_atoms(rows)
        keys = [(r["subject"], r["relation"], r["object"]) for r in out]
        assert keys == [("A", "r", "B"), ("A", "r", "C"), ("A", "s", "B")]

    def test_no_duplicates_is_a_noop(self):
        rows = [_row("A", "r", "B"), _row("C", "s", "D")]
        out = common.dedup_engine_atoms(rows)
        assert out == rows

    def test_object_differs_by_case_or_value_not_collapsed(self):
        rows = [_row("A", "r", "B"), _row("A", "r", "b")]
        out = common.dedup_engine_atoms(rows)
        assert len(out) == 2

    def test_empty_input(self):
        assert common.dedup_engine_atoms([]) == []


class TestCanonicallyEquivalentSpellingsCollapse:
    """#342: the raw triple was the dedup key, so one fact written two ways
    reached the engine as two entities.

    Measured before the fix, with `tools/compile_facts.py` on a KB holding the
    same fact in NFC and in NFD: `facts/accepted.dl` carried two
    `relation("삼성", "대표", "이재용").` lines — distinct as written: 2,
    distinct under NFC: 1. The checker had already folded both axes (#334), so
    `finalize` compiled and shipped the duplicate.
    """

    def test_object_axis_nfc_and_nfd_are_one_atom(self):
        # The issue's reproduction, verbatim.
        rows = [
            _row("연구소", "소속", _nfc("한국대학교"), source="sources/a.md"),
            _row("연구소", "소속", _nfd("한국대학교"), source="sources/b.md"),
        ]
        out = common.dedup_engine_atoms(rows)
        assert len(out) == 1

    def test_subject_axis_nfc_and_nfd_are_one_atom(self):
        rows = [
            _row(_nfc("한국대학교"), "소속", "연구소", source="sources/a.md"),
            _row(_nfd("한국대학교"), "소속", "연구소", source="sources/b.md"),
        ]
        out = common.dedup_engine_atoms(rows)
        assert len(out) == 1

    def test_whole_row_nfc_and_nfd_are_one_atom(self):
        # The engine-compile reproduction on the issue: every axis spelled twice.
        rows = [
            _row(_nfc("삼성"), "대표", _nfc("이재용"), source="sources/a.md"),
            _row(_nfd("삼성"), "대표", _nfd("이재용"), source="sources/a.md"),
        ]
        out = common.dedup_engine_atoms(rows)
        assert len(out) == 1

    def test_composed_spelling_wins_even_when_decomposed_comes_first(self):
        # The composed spelling is the one a reader greps for from an NFC editor
        # and the only one the typed projection can parse, so first-occurrence
        # does not decide the SPELLING. It still decides everything else: the
        # non-triple fields come from the group's first row.
        rows = [
            _row("연구소", "소속", _nfd("한국대학교"), source="sources/decomposed.md"),
            _row("연구소", "소속", _nfc("한국대학교"), source="sources/composed.md"),
        ]
        out = common.dedup_engine_atoms(rows)
        assert len(out) == 1
        assert out[0]["object"] == _nfc("한국대학교")
        assert out[0]["source"] == "sources/decomposed.md"

    # GUARD, not a pin: the triples here are byte-identical, so origin/main's
    # raw-triple dedup collapsed them too and this passes there. It exists to
    # catch a fix that normalizes on the way OUT, which the mutation check in
    # the report confirms it does catch.
    def test_uniformly_decomposed_group_keeps_its_decomposed_spelling_GUARD(self):
        # Fold to decide identity, never to rewrite the output: with no composed
        # member the group has no composed spelling to prefer, and normalizing
        # here would invent a string the KB never wrote.
        rows = [
            _row(_nfd("연구소"), "소속", _nfd("한국대학교"), source="sources/a.md"),
            _row(_nfd("연구소"), "소속", _nfd("한국대학교"), source="sources/b.md"),
        ]
        out = common.dedup_engine_atoms(rows)
        assert len(out) == 1
        assert out[0]["subject"] == _nfd("연구소")
        assert out[0]["object"] == _nfd("한국대학교")
        assert out[0]["source"] == "sources/a.md"  # first-occurrence still breaks the tie

    def test_cross_group_gets_the_composed_spelling_on_BOTH_axes(self):
        # The cross case: no member is composed on both axes. Ranking whole rows
        # has to pick one of them and therefore writes a decomposed axis while
        # the group demonstrably holds a composed spelling for it — which makes
        # check_conflicts' "written in the composed spelling" a false statement
        # and, on a typed relation, silently drops the fact from the typed table.
        # The axes are independent and are chosen independently.
        rows = [
            _row(_nfc("삼성"), "대표", _nfd("이재용"), source="sources/a.md"),
            _row(_nfd("삼성"), "대표", _nfc("이재용"), source="sources/b.md"),
        ]
        out = common.dedup_engine_atoms(rows)
        assert len(out) == 1
        assert out[0]["subject"] == _nfc("삼성")
        assert out[0]["object"] == _nfc("이재용")

    def test_each_axis_agrees_with_composed_spelling(self):
        # The invariant check_conflicts._representative's docstring states: both
        # stand a representative in front of a folded group and must pick the
        # same one, or the report and the compiled atom name a value differently.
        rows = [
            _row(_nfc("삼성"), "대표", _nfd("이재용"), source="sources/a.md"),
            _row(_nfd("삼성"), "대표", _nfc("이재용"), source="sources/b.md"),
            _row(_nfd("삼성"), "대표", _nfd("이재용"), source="sources/c.md"),
        ]
        out = common.dedup_engine_atoms(rows)
        assert len(out) == 1
        assert out[0]["subject"] == common.composed_spelling({r["subject"] for r in rows})
        assert out[0]["object"] == common.composed_spelling({r["object"] for r in rows})

    def test_synthesized_atom_still_resolves_its_provenance(self):
        # Synthesis is only safe because every atom-keyed map is keyed on
        # engine_atom_key. If one were left raw it would miss this atom outright
        # and drop the sources it is supposed to report.
        #
        # NOT a pin, and not a guard either. On origin/main it ERRORS rather
        # than fails — engine_atom_key does not exist there — so "it fails on
        # main" is not evidence for anything. Its meaningful baseline is the
        # round-1 branch, where it passes: the representative was a real row, so
        # a raw map resolved it. It is a consistency check that synthesis did
        # not break what round 1 already had.
        facts = [
            _row(_nfc("삼성"), "대표", _nfd("이재용"), source="sources/a.md", status="confirmed"),
            _row(_nfd("삼성"), "대표", _nfc("이재용"), source="sources/b.md", status="confirmed"),
        ]
        atom = common.dedup_engine_atoms(facts)[0]
        assert common.corroboration_counts(facts)[common.engine_atom_key(atom)] == 2

    # GUARD, not a pin: passes on origin/main too, by construction. It cannot
    # fail before the fix because before the fix NOTHING folded, so it can only
    # ever catch a LATER change that over-folds. Kept for that, not offered as
    # evidence that #342 was fixed.
    def test_relation_axis_is_deliberately_not_folded_GUARD(self):
        # #210's deferred call: the checker's grouping keeps the relation
        # verbatim and so does this. Both sides raw is agreement, not a gap that
        # this fix opened — but it IS the axis that still costs two atoms.
        rows = [
            _row("연구소", _nfc("소속"), "한국대학교", source="sources/a.md"),
            _row("연구소", _nfd("소속"), "한국대학교", source="sources/b.md"),
        ]
        assert len(common.dedup_engine_atoms(rows)) == 2

    # GUARD, not a pin: passes on origin/main too (see above).
    def test_compatibility_and_case_variants_stay_distinct_GUARD(self):
        # NFC, never NFKC and never casefold: these are different values.
        rows = [
            _row("A", "r", "ABC"),
            _row("A", "r", "ＡＢＣ"),
            _row("A", "r", "abc"),
        ]
        assert len(common.dedup_engine_atoms(rows)) == 3

    def test_group_order_is_first_occurrence(self):
        rows = [
            _row("X", "r", "Y", source="x"),
            _row("연구소", "소속", _nfd("한국대학교"), source="a"),
            _row("P", "r", "Q", source="p"),
            _row("연구소", "소속", _nfc("한국대학교"), source="b"),
        ]
        out = common.dedup_engine_atoms(rows)
        assert [r["subject"] for r in out] == ["X", "연구소", "P"]


class TestEngineAtomKey:
    def test_folds_subject_and_object_but_not_relation(self):
        key = common.engine_atom_key(
            _row(_nfd("연구소"), _nfd("소속"), _nfd("한국대학교"))
        )
        assert key == (_nfc("연구소"), _nfd("소속"), _nfc("한국대학교"))

    def test_corroboration_counts_aggregate_under_the_folded_atom(self):
        # The compile log annotates the atom dedup wrote. Keyed raw, a fact
        # backed by two sources under two spellings reported sources=1 for the
        # surviving spelling and dropped the other source from the log entirely.
        facts = [
            _row("연구소", "소속", _nfc("한국대학교"), source="sources/a.md", status="confirmed"),
            _row("연구소", "소속", _nfd("한국대학교"), source="sources/b.md", status="confirmed"),
        ]
        counts = common.corroboration_counts(facts)
        assert counts == {("연구소", "소속", _nfc("한국대학교")): 2}

    def test_one_source_backing_both_spellings_counts_once(self):
        facts = [
            _row("연구소", "소속", _nfc("한국대학교"), source="sources/a.md", status="confirmed"),
            _row("연구소", "소속", _nfd("한국대학교"), source="sources/a.md", status="confirmed"),
        ]
        assert common.corroboration_counts(facts) == {
            ("연구소", "소속", _nfc("한국대학교")): 1
        }
