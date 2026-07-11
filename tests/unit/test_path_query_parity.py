"""A path query is answered from the ENGINE's path/2, by both the report and ask (#220).

The report used to handle only two quoted constants, so `path("A", X)?` produced no
result line at all -- the result list came back empty and the fallback claimed
`no facts/query.dl found` about a file that was right there, while ask answered the same
question with two rows. path_query_rows is the one matcher both callers use, and it
takes the truth set rather than recomputing one: a second closure in the tree is a
second thing to drift.

Report/ask parity is pinned end to end in tests/test_path_report.sh, where a real KB and
a real engine exist. Here we pin the matching itself.
"""

import pytest

from factlog.common import path_query_rows, query_args

FACTS = [
    {"subject": "A", "relation": "uses", "object": "B", "status": "accepted"},
    {"subject": "B", "relation": "uses", "object": "C", "status": "accepted"},
]
# What the engine derives for these facts.
PAIRS = {("A", "B"), ("B", "C"), ("A", "C")}

EXPECTED = {
    'path("A", X)?': [["A", "B"], ["A", "C"]],
    'path(X, "C")?': [["A", "C"], ["B", "C"]],
    'path("A", "C")?': [["A", "B", "C"]],  # two constants answer WHICH WAY
    'path(X, Y)?': [["A", "B"], ["A", "C"], ["B", "C"]],
    'path("C", X)?': [],  # nothing leaves C -- an honest empty
}


@pytest.mark.parametrize("query", sorted(EXPECTED))
def test_the_matcher_answers_the_query(query):
    assert path_query_rows(query_args(query), FACTS, PAIRS) == EXPECTED[query]


def test_two_constants_return_the_route_not_just_the_pair():
    assert path_query_rows(query_args('path("A", "C")?'), FACTS, PAIRS) == [["A", "B", "C"]]


def test_an_unreachable_pair_is_an_honest_empty():
    assert path_query_rows(query_args('path("C", "A")?'), FACTS, PAIRS) == []


def test_the_same_variable_twice_is_a_join_not_two_wildcards():
    """`path(X, X)?` asks which nodes lie on a CYCLE."""
    assert path_query_rows(query_args("path(X, X)?"), FACTS, PAIRS) == []
    cyclic = [*FACTS, {"subject": "C", "relation": "uses", "object": "A", "status": "accepted"}]
    pairs = PAIRS | {("C", "A"), ("A", "A"), ("B", "B"), ("C", "C"), ("B", "A"), ("C", "B")}
    assert path_query_rows(query_args("path(X, X)?"), cyclic, pairs) == [
        ["A", "A"],
        ["B", "B"],
        ["C", "C"],
    ]


def test_a_pair_the_engine_proved_is_never_denied():
    """A rule in logic-policy.extra.dl can put an edge no closure over relation/3 sees.

    The matcher reports the pair rather than inventing a hop or denying the engine; the
    report labels it, and that label is pinned in tests/test_path_report.sh.
    """
    engine_only = {("C", "A")}
    assert path_query_rows(query_args('path("C", "A")?'), FACTS, engine_only) == [["C", "A"]]
    assert path_query_rows(query_args('path("A", "C")?'), FACTS, engine_only) == []
