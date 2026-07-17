# SPDX-License-Identifier: Apache-2.0
"""Every static query predicate must have a matching evaluate_queries branch (#312).

#306 made the report and ask share one static query vocabulary (``QUERY_PREDICATES``),
so the two can no longer disagree on which predicates are *known*. But the other
half of the contract — that a known predicate actually gets *evaluated* — is still
hand-maintained: adding a predicate to ``QUERY_PREDICATES`` registers it in both
vocabularies automatically, yet if no evaluation branch is added to
``evaluate_queries`` the predicate is "known but silent" — exactly the ``conflict``
symptom #306 removed, and the silent-omission class #220 exists to prevent.

This guard walks ``QUERY_PREDICATES``, feeds each predicate a minimal well-formed
query, and asserts evaluate_queries answers with a real evaluation line (not an
unknown/malformed marker, not nothing). Omitting the branch for a newly added
predicate fails this test immediately.

The two ``red_sim`` tests prove the guard actually bites: a phantom predicate added
to the vocabulary (the binding ``evaluate_queries`` reads) with no branch trips the
"silent" assertion, and one added with no fixture trips the pairing assertion.
"""
from __future__ import annotations

import pytest

import common
import run_logic_check as rlc

# One minimal well-formed query per static predicate. Empty facts and an empty
# ``path`` inference are enough: a well-formed-but-unsatisfied query is still a
# real evaluation ("0 rows" / "(not found)"), which is all the pairing needs.
# A new entry here is required for every predicate added to QUERY_PREDICATES.
FIXTURES = {
    "relation": 'relation("A", "uses", "B")?',
    "path": 'path("A", "B")?',
    "count": 'count("A", "uses")?',
    "review_required": 'review_required("원 질문")?',
}

_MARKER = "see Errors above"  # the shared suffix of every unknown/malformed pointer


def _run_pairing_guard(pred, monkeypatch, fixtures=FIXTURES):
    """Assert `pred` is paired: a fixture exists AND evaluate_queries really evaluates it."""
    assert pred in fixtures, (
        f"query predicate {pred!r} is in QUERY_PREDICATES but has no fixture here — "
        f"a predicate was added to the vocabulary without a paired evaluator. Add both "
        f"a fixture and an evaluate_queries branch for it (#306/#312)."
    )
    monkeypatch.setattr(rlc, "query_lines", lambda: [fixtures[pred]])
    results = rlc.evaluate_queries([], {"path": set()}, set(), hierarchy={})
    assert results, (
        f"evaluate_queries produced nothing for known predicate {pred!r} — it is "
        f"known but silent (#306 regression). Add its evaluation branch."
    )
    assert not any(_MARKER in line for line in results), (
        f"predicate {pred!r} produced only an unknown/malformed marker, not a real "
        f"evaluation line: {results!r}"
    )
    return results


class TestEveryStaticPredicateHasEvaluator:
    def test_all_current_predicates_are_paired(self, monkeypatch):
        # Walks the LIVE vocabulary, so a predicate added to QUERY_PREDICATES without
        # a fixture-and-branch pair fails here without touching this test.
        for pred in sorted(rlc.QUERY_PREDICATES):
            _run_pairing_guard(pred, monkeypatch)

    def test_fixtures_cover_exactly_the_vocabulary(self):
        # No stale fixture for a predicate that has since left the vocabulary, and no
        # predicate left without a fixture. Keeps FIXTURES honest against drift.
        assert set(FIXTURES) == set(rlc.QUERY_PREDICATES)

    def test_evaluator_reads_the_shared_vocabulary_object(self):
        # evaluate_queries reads rlc's QUERY_PREDICATES; #306 tied it to common's.
        # If these ever became distinct objects the red-sim monkeypatch (and the
        # #306 single-source guarantee) would be testing the wrong binding.
        assert rlc.QUERY_PREDICATES is common.QUERY_PREDICATES


class TestGuardBites:
    """The guard must fail for the two ways the pairing can break."""

    def test_red_sim_known_predicate_without_branch(self, monkeypatch):
        # Phantom added to the vocabulary evaluate_queries reads AND given a fixture.
        # Because it is now "known", the unknown-predicate branch skips it, and with
        # no evaluation branch it yields nothing — the real #306 symptom. The
        # "silent" assertion must fire (not the fixture one).
        monkeypatch.setattr(rlc, "QUERY_PREDICATES", rlc.QUERY_PREDICATES | {"phantom"})
        fixtures = {**FIXTURES, "phantom": 'phantom("A", "B")?'}
        with pytest.raises(AssertionError, match="known but silent"):
            _run_pairing_guard("phantom", monkeypatch, fixtures=fixtures)

    def test_red_sim_predicate_without_fixture(self, monkeypatch):
        # Phantom added to the vocabulary only. The pairing assertion fires before
        # evaluate_queries is even called.
        monkeypatch.setattr(rlc, "QUERY_PREDICATES", rlc.QUERY_PREDICATES | {"phantom"})
        with pytest.raises(AssertionError, match="no fixture here"):
            _run_pairing_guard("phantom", monkeypatch)
