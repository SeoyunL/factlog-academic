# SPDX-License-Identifier: Apache-2.0
"""The report and `ask` must give the same answer to the same path query (#329).

``run_logic_check`` validated every query constant against ``value_set``, which
includes literal values, so ``path("갑봇", "2030.1")?`` produced
``- path 갑봇 -> 2030.1: (not found)`` with **no warning at all**, while
``common.classify_query`` rejected the identical query as
``entity_not_accepted``. "(not found)" reads as "the facts were searched and do
not connect them"; the real reason is that a literal cannot be a path node.

The direction was already wrong before #329 (the report drew a POSITIVE path
through the literal), so this is not a new divergence — but the repo spent five
commits (287ec84…bf586b3) on exactly "the report and the router answer the same
query the same way", and the path axis was left out.
"""
from __future__ import annotations

import pytest

import common
import run_logic_check as rlc
from factlog import common as fl_common


def R(subject: str, relation: str, object_: str) -> dict[str, str]:
    return {
        "subject": subject,
        "relation": relation,
        "object": object_,
        "status": "accepted",
        "source": "sources/a.md",
    }


FACTS = [
    R("갑봇", "통합", "을서비스"),
    R("을서비스", "정식_운영", "2030.1"),
]
VALUES = {"갑봇", "을서비스", "2030.1"}     # value_set
NODES = {"갑봇", "을서비스"}                 # entity_set: 정식_운영 is an attribute relation
LITERAL_QUERY = 'path("갑봇", "2030.1")?'
ENTITY_QUERY = 'path("갑봇", "을서비스")?'


@pytest.fixture
def queries(monkeypatch):
    """Bind facts/query.dl's contents without touching the filesystem."""

    def _bind(*lines: str) -> None:
        monkeypatch.setattr(rlc, "query_lines", lambda: list(lines))

    return _bind


@pytest.fixture
def attrs(monkeypatch):
    monkeypatch.setattr(fl_common, "attribute_relations", lambda: {"정식_운영"})


class TestReportNamesTheReason:
    def test_result_line_gives_the_reason_instead_of_not_found(self, queries):
        # The engine cannot derive the pair either, so inferred["path"] is empty —
        # the point is that "(not found)" is not the honest rendering of
        # "2030.1 is not a node at all".
        queries(LITERAL_QUERY)
        assert rlc.evaluate_queries(FACTS, {"path": set()}, set(), NODES) == [
            "path 갑봇 -> 2030.1: (not evaluated — not an accepted entity: 2030.1)"
        ]

    def test_validate_query_warns_with_the_same_wording_ask_uses(self, attrs):
        _, warnings = rlc.validate_query(LITERAL_QUERY, VALUES, set(), NODES)
        assert warnings == ["query path argument is not an accepted entity: 2030.1"]
        # ask's wording for the same query, so a reader who sees both recognises
        # them as one finding.
        ok, code, reason = common.classify_query(LITERAL_QUERY, FACTS, policy_program="")
        assert (ok, code) == (False, common.QUERY_ENTITY_NOT_ACCEPTED)
        assert reason.endswith("path argument is not an accepted entity: 2030.1")


class TestEntityPathQueryIsUntouched:
    """CONTROL — passes before and after; keeps the pins above non-vacuous."""

    def test_entity_endpoints_are_still_evaluated(self, queries, attrs):
        queries(ENTITY_QUERY)
        assert rlc.evaluate_queries(FACTS, {"path": {("갑봇", "을서비스")}}, set(), NODES) == [
            "path 갑봇 -> 을서비스: 갑봇 -> 을서비스"
        ]

    def test_entity_endpoints_produce_no_warning(self):
        assert rlc.validate_query(ENTITY_QUERY, VALUES, set(), NODES) == ([], [])


class TestThreeArgumentCallersAreUnchanged:
    """CONTROL — passes before and after. ``path_nodes=None`` means "do not
    distinguish", so the pre-#329 shape of both functions is preserved for the
    callers that do not supply an entity set."""

    def test_evaluate_queries_without_path_nodes(self, queries):
        queries(LITERAL_QUERY)
        assert rlc.evaluate_queries(FACTS, {"path": set()}, set()) == [
            "path 갑봇 -> 2030.1: (not found)"
        ]

    def test_validate_query_without_path_nodes(self):
        assert rlc.validate_query(LITERAL_QUERY, VALUES, set()) == ([], [])
