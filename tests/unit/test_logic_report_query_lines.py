# SPDX-License-Identifier: Apache-2.0
"""What the "Query evaluation" section of logic_report.txt is allowed to claim.

Four measured gaps from the #329 round-3 review, all in run_logic_check:

* ``path("갑봇", "")?`` vanished from the report — no result line and no warning
  — while ``ask``'s router answered it with a reason. #329 is what made ``""`` a
  graph node that is not an accepted entity, so the report owes an answer.
* a query.dl holding only variable-form path queries reported
  ``- no facts/query.dl found``, with the file sitting right there.
* the warning filter tested the tail of EVERY warning against the relation names,
  so a warning about a value that happens to equal a relation name disappeared.
* ``evaluate_queries`` re-read policy/attribute-relations.md once per path query.
"""
from __future__ import annotations

import pytest

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
    R("병사", "메모", ""),
]
VALUES = {"갑봇", "을서비스", "2030.1", "병사"}   # value_set: "" is dropped
NODES = {"갑봇", "을서비스", "병사"}              # entity_set


@pytest.fixture
def attrs(monkeypatch):
    monkeypatch.setattr(fl_common, "attribute_relations", lambda: {"정식_운영"})
    # raising=False: the name did not exist before the hoist, and these pins must
    # fail on their own assertion rather than on the fixture.
    monkeypatch.setattr(rlc, "attribute_relations", lambda: {"정식_운영"}, raising=False)


@pytest.fixture
def queries(monkeypatch):
    """Bind facts/query.dl's contents without touching the filesystem."""

    def _bind(*lines: str) -> None:
        monkeypatch.setattr(rlc, "query_lines", lambda: list(lines))

    return _bind


class TestEmptyStringEndpoint:
    """An empty-string path endpoint must be answered, not skipped."""

    QUERY = 'path("갑봇", "")?'

    def test_result_line_names_the_reason(self, attrs, queries):
        # quoted_constants' `"([^"]+)"` dropped `""`, the `len(constants) >= 2`
        # gate then skipped the query, and the report said nothing at all.
        queries(self.QUERY)
        assert rlc.evaluate_queries(FACTS, {"path": set()}, set(), NODES) == [
            'path 갑봇 -> "": (not evaluated — not an accepted entity: "")'
        ]

    def test_the_router_gives_the_same_reason(self, attrs):
        ok, kind, _ = fl_common.classify_query(self.QUERY, FACTS)
        assert (ok, kind) == (False, "entity_not_accepted")

    def test_warning_is_emitted(self, attrs):
        errors, warnings = rlc.validate_query(self.QUERY, VALUES, set(), NODES)
        assert errors == []
        assert warnings == ['query path argument is not an accepted entity: ""']

    def test_an_ordinary_endpoint_is_not_quoted(self, attrs, queries):
        # CONTROL — only the empty value gains the quotes.
        queries('path("갑봇", "을서비스")?')
        assert rlc.evaluate_queries(
            FACTS, {"path": {("갑봇", "을서비스")}}, set(), NODES
        ) == ["path 갑봇 -> 을서비스: 갑봇 -> 을서비스"]

    def test_a_variable_endpoint_still_produces_no_line(self, attrs, queries):
        # CONTROL — path_endpoints counts only quoted args, so a half-bound query
        # produces no result line rather than a malformed one.
        queries('path("갑봇", Y)?')
        assert rlc.evaluate_queries(FACTS, {"path": set()}, set(), NODES) == []


class TestQueryFileFallbackText:
    """`- no facts/query.dl found` may only be printed when it is not there."""

    def _report(self, tmp_path, monkeypatch, query_text):
        monkeypatch.setattr(rlc, "FACTS_DIR", tmp_path)
        monkeypatch.setattr(rlc, "ensure_dirs", lambda: None)
        monkeypatch.setattr(rlc, "load_accepted_facts", lambda: list(FACTS))
        monkeypatch.setattr(rlc, "load_facts", lambda: list(FACTS))
        monkeypatch.setattr(rlc, "load_logic_policy", lambda: "")
        monkeypatch.setattr(rlc, "run_wirelog", lambda: {"path": set()})
        if query_text is not None:
            (tmp_path / "query.dl").write_text(query_text, encoding="utf-8")
        rlc.main()
        return (tmp_path / "logic_report.txt").read_text(encoding="utf-8")

    def test_variable_form_path_query_does_not_claim_the_file_is_missing(
        self, tmp_path, monkeypatch, attrs
    ):
        # A path result line names its two endpoints, so `path(X, Y)?` renders
        # nothing — which is not the same as the file being absent.
        report = self._report(tmp_path, monkeypatch, "path(X, Y)?\n")
        assert "- no facts/query.dl found" not in report
        assert "- no answerable queries in facts/query.dl (see Errors)" in report

    def test_missing_file_still_says_so(self, tmp_path, monkeypatch, attrs):
        # CONTROL — the original message survives for the case it describes.
        report = self._report(tmp_path, monkeypatch, None)
        assert "- no facts/query.dl found" in report

    def test_comment_only_file_is_not_reported_as_missing(
        self, tmp_path, monkeypatch, attrs
    ):
        # #348 keys the fallback on `query_lines()`, so a comment-only file — which
        # has no query lines either — was still called missing. The condition is
        # file existence; the wording is #348's.
        report = self._report(tmp_path, monkeypatch, "// only a comment\n")
        assert "- no facts/query.dl found" not in report
        assert "- no answerable queries in facts/query.dl (see Errors)" in report


class TestWarningFilterTargetsTheRightWarnings:
    """The relation-name filter belongs to the unknown-constant warnings only."""

    def test_a_path_warning_whose_value_is_a_relation_name_survives(self):
        # `rsplit(": ", 1)[-1] not in relations` dropped ANY warning ending in a
        # relation name. A KB where a literal value equals a relation name — a
        # duplicate string, nothing exotic — silently lost the path warning.
        warning = "query path argument is not an accepted entity: 통합"
        assert rlc.names_a_relation(warning, {"통합"}) is False

    def test_an_unknown_constant_warning_about_a_relation_is_still_dropped(self):
        # CONTROL — a relation name IS legitimate vocabulary; this is the case the
        # filter was written for and it must keep working.
        warning = "query references non-engine entity or relation: 통합"
        assert rlc.names_a_relation(warning, {"통합"}) is True
        assert rlc.names_a_relation(warning, {"메모"}) is False

    def test_a_value_containing_the_separator_is_matched_whole(self):
        # `rsplit(": ", 1)` cut the value itself; stripping the known prefix does not.
        warning = "query references non-engine entity: a: b"
        assert rlc.names_a_relation(warning, {"a: b"}) is True
        assert rlc.names_a_relation(warning, {"b"}) is False

    def test_the_filter_runs_end_to_end(self, tmp_path, monkeypatch, attrs):
        # The path warning reaches the report even though its value is also a
        # relation name in this KB.
        facts = [R("갑봇", "통합", "을서비스"), R("을서비스", "정식_운영", "통합")]
        monkeypatch.setattr(rlc, "FACTS_DIR", tmp_path)
        monkeypatch.setattr(rlc, "ensure_dirs", lambda: None)
        monkeypatch.setattr(rlc, "load_accepted_facts", lambda: list(facts))
        monkeypatch.setattr(rlc, "load_facts", lambda: list(facts))
        monkeypatch.setattr(rlc, "load_logic_policy", lambda: "")
        monkeypatch.setattr(rlc, "run_wirelog", lambda: {"path": set()})
        (tmp_path / "query.dl").write_text('path("갑봇", "통합")?\n', encoding="utf-8")
        rlc.main()
        report = (tmp_path / "logic_report.txt").read_text(encoding="utf-8")
        assert "- query path argument is not an accepted entity: 통합" in report
        assert "warnings: 1" in report


class TestAttributeRelationsAreReadOnce:
    """`evaluate_queries` must hoist the declarations, as classify_query does."""

    def test_two_path_queries_read_the_file_once(self, monkeypatch):
        calls = []

        def counted():
            calls.append(1)
            return {"정식_운영"}

        monkeypatch.setattr(fl_common, "attribute_relations", counted)
        monkeypatch.setattr(rlc, "attribute_relations", counted, raising=False)
        monkeypatch.setattr(
            rlc,
            "query_lines",
            lambda: ['path("갑봇", "을서비스")?', 'path("을서비스", "갑봇")?'],
        )
        inferred = {"path": {("갑봇", "을서비스"), ("을서비스", "갑봇")}}
        rlc.evaluate_queries(FACTS, inferred, set(), NODES)
        assert len(calls) == 1

    def test_no_path_query_reads_nothing(self, monkeypatch):
        calls = []
        monkeypatch.setattr(fl_common, "attribute_relations", lambda: calls.append(1) or set())
        monkeypatch.setattr(rlc, "attribute_relations", lambda: calls.append(1) or set(), raising=False)
        monkeypatch.setattr(rlc, "query_lines", lambda: ['relation("갑봇", "통합", "을서비스")?'])
        rlc.evaluate_queries(FACTS, {"path": set()}, set(), NODES)
        assert calls == []


class TestCountEchoIsWrapped:
    """The count echo prints the query line verbatim, and that line can carry a
    character which would corrupt the report.

    The echo looked structurally safe: it is only reached after ``query_error``
    accepted the line, so every argument is a strict variable or a string
    ``json.loads`` parsed, and JSON forbids raw control characters below 0x20.
    But ``_FORBIDDEN_IN_LINE`` is wider than C0 — it also covers DEL (0x7f) and
    the C1 block (0x80-0x9f) — and JSON accepts all of those raw inside a string
    literal. So a query.dl carrying the byte itself reaches the echo with it
    intact, on one physical line, with no error raised anywhere on the way.

    Below 0x20 really is unreachable, and this asserts that too: written raw the
    line is refused by ``query_error``, and written as a ``\\uXXXX`` escape the
    physical line holds six ordinary characters that need no wrapping. That is
    the case the report was already safe against.
    """

    @pytest.mark.parametrize("char", ["\x7f", "\x80", "\x9f"])
    def test_a_c1_or_del_byte_in_the_echo_is_escaped(self, char, queries):
        """RED before (with the echo unwrapped): the report line carries the raw
        byte, which is the whole hazard ``one_line`` exists for — an unprintable
        that a terminal may act on rather than show."""
        subject = f"갑{char}봇"
        query = f'count("{subject}", "통합")?'
        assert rlc.query_error("count", query) is None, "the gate must accept it"
        assert len(query.splitlines()) == 1, "it must be ONE physical line"
        queries(query)
        [line] = rlc.evaluate_queries([R(subject, "통합", "을서비스")], {}, set(), NODES)
        assert char not in line
        assert line == f"count results (query: {query!r}): 1 (distinct objects)"

    def test_a_c0_byte_never_reaches_the_echo(self, queries):
        """GUARD for the boundary the note draws: raw, the gate refuses it, so
        the echo is not the thing standing between it and the report."""
        query = 'count("갑\x01봇", "통합")?'
        assert rlc.query_error("count", query) is not None

    def test_a_c0_escape_leaves_the_echo_byte_identical(self, queries):
        """GUARD: written as an escape it is six ordinary characters in the
        physical line, so ``one_line`` is the identity and the report reads back
        exactly what the author typed."""
        query = 'count("갑\\u0001봇", "통합")?'
        queries(query)
        [line] = rlc.evaluate_queries([R("갑\x01봇", "통합", "을서비스")], {}, set(), NODES)
        assert line == f"count results (query: {query}): 1 (distinct objects)"
