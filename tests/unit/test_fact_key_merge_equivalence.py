# SPDX-License-Identifier: Apache-2.0
"""common.fact_key IS merge's dedup identity — the guard against a third drift (#477).

Twice now a human-confirmed fact vanished from the engine because two places disagreed
about what "the same fact" means: the decision never reaching runs/*.json (#233), and a
decision keyed on the triple alone flipping another source's row (#477). The fix is that
there is ONE definition, common.fact_key, which merge_candidates.normalize_rows keys its
dedup on and the review CLI keys its runs/*.json writes on.

These tests check that equivalence against merge's OBSERVABLE behaviour (which rows
collapse, which survive), not by re-reading the implementation. What they catch is a
DIVERGENCE in normalisation: the moment one side applies a fold, a canonicalisation or a
strip the other does not, the collapse pattern stops matching fact_key's grouping and
these fail. Re-deriving the key inline while keeping every normalisation identical passes
— correctly so: that mutant behaves exactly like merge did before this change, which is
also the evidence that routing merge through fact_key was a no-op for merge itself. The
argument for the single definition is that the next edit to one side cannot silently
diverge; these tests are what makes such a divergence loud.
"""
from __future__ import annotations

import unicodedata

import merge_candidates as mc
import pytest

from factlog.common import fact_key

NFC = unicodedata.normalize("NFC", "가나")
NFD = unicodedata.normalize("NFD", "가나")


def _root(tmp_path, names=("a.md", "b.md")):
    (tmp_path / "sources").mkdir()
    for name in names:
        (tmp_path / "sources" / name).write_text("# heading\n", encoding="utf-8")
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


def _grouped(rows):
    """The fact_key groups the input rows fall into."""
    return {fact_key(r["subject"], r["relation"], r["object"], r["source"]) for r in rows}


def _surviving(root, rows):
    """The fact_key of every row merge kept after normalise/dedup."""
    out = mc.normalize_rows(root, rows)
    return {fact_key(r["subject"], r["relation"], r["object"], r["source"]) for r in out}


CASES = {
    # amount: merge canonicalises the object before keying, so the bare, quoted and
    # comma-grouped forms are ONE fact.
    "amount_bare_vs_quoted": (
        [
            _row("A", "costs", "amount(7,억)", "sources/a.md"),
            _row("A", "costs", 'amount(7,"억")', "sources/a.md"),
        ],
        1,
    ),
    "amount_thousands_separator": (
        [
            _row("A", "costs", "amount(1,000,원)", "sources/a.md"),
            _row("A", "costs", 'amount(1000,"원")', "sources/a.md"),
        ],
        1,
    ),
    "amount_unit_differs": (
        [
            _row("A", "costs", 'amount(7,"억")', "sources/a.md"),
            _row("A", "costs", 'amount(7,"만")', "sources/a.md"),
        ],
        2,
    ),
    # Unicode: content values are stored verbatim, so the two forms are TWO facts.
    "subject_nfc_vs_nfd": (
        [
            _row(NFC, "R", "X", "sources/a.md"),
            _row(NFD, "R", "X", "sources/a.md"),
        ],
        2,
    ),
    "object_nfc_vs_nfd": (
        [
            _row("A", "R", NFC, "sources/a.md"),
            _row("A", "R", NFD, "sources/a.md"),
        ],
        2,
    ),
    # The source, by contrast, IS folded (filesystem artifact) and cut at '#'.
    "anchor_variants": (
        [
            _row("A", "R", "X", "sources/a.md"),
            _row("A", "R", "X", "sources/a.md#sec1"),
            _row("A", "R", "X", "sources/a.md#sec2"),
        ],
        1,
    ),
    "different_source_files": (
        [
            _row("A", "R", "X", "sources/a.md"),
            _row("A", "R", "X", "sources/b.md"),
        ],
        2,
    ),
    # Surrounding whitespace is stripped on both sides.
    "whitespace_padding": (
        [
            _row("A", "R", "X", "sources/a.md"),
            _row(" A ", " R ", " X ", "sources/a.md"),
        ],
        1,
    ),
    # Typed literals are NOT canonicalised by merge (only amount is), so a date/number/
    # ordinal written two ways stays two facts. fact_key must not get clever here.
    "date_forms": (
        [
            _row("A", "on", "2026-07-24", "sources/a.md"),
            _row("A", "on", "2026/07/24", "sources/a.md"),
        ],
        2,
    ),
    "number_forms": (
        [
            _row("A", "n", "1000", "sources/a.md"),
            _row("A", "n", "1,000", "sources/a.md"),
        ],
        2,
    ),
    "ordinal_forms": (
        [
            _row("A", "rank", "1st", "sources/a.md"),
            _row("A", "rank", "1", "sources/a.md"),
        ],
        2,
    ),
    # Fields fact_key deliberately ignores: merge collapses rows differing only there,
    # so a decision on the surviving row must reach every run row behind it.
    "confidence_and_note_ignored": (
        [
            _row("A", "R", "X", "sources/a.md", confidence="0.10", note="first"),
            _row("A", "R", "X", "sources/a.md", confidence="0.90", note="second"),
        ],
        1,
    ),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_fact_key_grouping_matches_merge_dedup(tmp_path, name):
    rows, expected_facts = CASES[name]
    root = _root(tmp_path)

    # fact_key groups the input rows exactly as merge collapses them...
    assert len(_grouped(rows)) == expected_facts, f"{name}: fact_key grouping"
    assert len(mc.normalize_rows(root, rows)) == expected_facts, f"{name}: merge dedup"
    # ...and the rows merge kept carry exactly those keys (no key drifts in the rewrite
    # merge performs on the way out, e.g. its amount canonicalisation).
    assert _surviving(root, rows) == _grouped(rows), f"{name}: surviving keys"


def test_fact_key_is_idempotent_over_its_own_output():
    """Re-keying a row merge already normalised must not move it to another fact."""
    key = fact_key("A", "costs", "amount(7,억)", "sources/a.md#s1")
    assert fact_key(*key) == key


def test_merge_output_rekeys_to_the_same_fact(tmp_path):
    """The row merge writes to candidates.csv must key to the same fact as the run row
    it came from -- this is precisely what accept/reject relies on to find run rows."""
    root = _root(tmp_path)
    run_row = _row("A", "costs", "amount(1,000,원)", "sources/a.md#s1")
    (csv_row,) = mc.normalize_rows(root, [run_row])

    assert fact_key(
        csv_row["subject"], csv_row["relation"], csv_row["object"], csv_row["source"]
    ) == fact_key(run_row["subject"], run_row["relation"], run_row["object"], run_row["source"])
