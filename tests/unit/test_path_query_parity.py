"""The report and the ask router must answer a path query the same way (#220).

The report handled only two quoted constants, so `path("A", X)?` appended no result
line at all -- the result list came back empty and main's fallback printed
`no facts/query.dl found` about a file that was right there, while `ask` answered the
same question with two rows. #213 unified relation and count this way; path was left
behind, and one shared predicate is what keeps them from drifting again.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))

from factlog.common import path_query_rows, query_args, reachable_pairs  # noqa: E402

FACTS = [
    {"subject": "A", "relation": "uses", "object": "B", "status": "accepted"},
    {"subject": "B", "relation": "uses", "object": "C", "status": "accepted"},
]

# The EXPECTED rows, not just "the two agree". Sharing one predicate makes a pure
# parity assertion vacuous -- both sides would agree on a wrong answer, and on the
# empty answer that was the bug. Pin the answer itself.
EXPECTED = {
    'path("A", X)?': [["A", "B"], ["A", "C"]],
    'path(X, "C")?': [["A", "C"], ["B", "C"]],
    'path("A", "C")?': [["A", "B", "C"]],  # two constants answer WHICH WAY
    'path(X, Y)?': [["A", "B"], ["A", "C"], ["B", "C"]],
    'path("C", X)?': [],  # nothing leaves C -- an honest empty
}


@pytest.mark.parametrize("query", sorted(EXPECTED))
def test_the_report_answers_the_query(query):
    assert path_query_rows(query_args(query), FACTS) == EXPECTED[query]


# Report/ask parity now needs a real KB: both sides ask the ENGINE for the truth set,
# so a fixture without an accepted.dl would only prove they degrade alike. It is pinned
# end to end in tests/test_path_report.sh instead.


def test_a_variable_query_returns_rows_not_silence():
    """The bug: no rows meant the report claimed the query file did not exist."""
    rows = path_query_rows(query_args('path("A", X)?'), FACTS)
    assert rows == [["A", "B"], ["A", "C"]]


def test_two_constants_still_return_the_route_not_just_the_pair():
    """A constant query answers WHICH WAY, and that must not regress to a bare pair."""
    assert path_query_rows(query_args('path("A", "C")?'), FACTS) == [["A", "B", "C"]]


def test_an_unreachable_pair_is_an_honest_empty():
    assert path_query_rows(query_args('path("C", "A")?'), FACTS) == []
    assert path_query_rows(query_args('path("C", X)?'), FACTS) == []


def test_reachable_pairs_is_the_transitive_closure():
    assert reachable_pairs(FACTS) == {("A", "B"), ("B", "C"), ("A", "C")}


def test_the_same_variable_twice_is_a_join_not_two_wildcards():
    """`path(X, X)?` asks which nodes lie on a CYCLE.

    Treating the two arguments independently answered it with every reachable pair --
    a wrong answer, and the report now signs it with the engine's name.
    """
    assert path_query_rows(query_args("path(X, X)?"), FACTS) == []
    cyclic = [*FACTS, {"subject": "C", "relation": "uses", "object": "A", "status": "accepted"}]
    rows = path_query_rows(query_args("path(X, X)?"), cyclic)
    assert rows == [["A", "A"], ["B", "B"], ["C", "C"]]


def test_the_engine_is_the_authority_when_a_truth_set_is_given():
    """The report passes the engine's path/2; a policy rule can add pairs no python
    closure over accepted facts would find, and the report must not deny them."""
    engine_pairs = {("C", "A")}  # e.g. from an edge rule in logic-policy.extra.dl
    rows = path_query_rows(query_args('path("C", "A")?'), FACTS, engine_pairs)
    assert rows == [["C", "A"]]  # reported as a pair: python knows no route for it
    assert path_query_rows(query_args('path("A", "C")?'), FACTS, engine_pairs) == []
