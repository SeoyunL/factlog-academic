# SPDX-License-Identifier: Apache-2.0
"""The logic report must answer a query written in either spelling, and echo back
the spelling the author wrote.

``relation_results``, the count branch and ``policy_row_matches`` all compare
RAW, so on a KB whose atoms were folded to one spelling per value the report
answered `0 rows` / `0 (distinct objects)` / `(not evaluated — not an accepted
entity: …)` to queries the KB does support. The report is the artifact SKILL.md
tells the reader to show verbatim before stating a conclusion, and its aggregate
is the line a reader is least able to check by eye.

The echo is the other half. The report is read beside ``facts/query.dl``; if it
printed back the spelling accepted.dl stores rather than the one the author
typed, the difference would be invisible on screen and unsearchable in the file.
"""
from __future__ import annotations

import unicodedata

import pytest

import run_logic_check as rlc
from factlog.common import kb_query_spellings, resolve_query_spellings


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def nfd(value: str) -> str:
    return unicodedata.normalize("NFD", value)


def rows(*triples: tuple[str, str, str]) -> list[dict[str, str]]:
    return [{"subject": s, "relation": r, "object": o} for s, r, o in triples]


MIXED = rows(
    (nfc("삼성"), "대표", nfc("이재용")),
    (nfc("이재용"), "거주", nfd("서울")),
)
VALUES = {row[key] for row in MIXED for key in ("subject", "object")}
NODES = set(VALUES)
SPELLING = kb_query_spellings(MIXED)
# The engine's path/2 extent over MIXED, in the spellings accepted.dl holds —
# what run_wirelog returns for this KB, supplied directly so the pins do not
# need pyrewire.
REACHABLE = {
    "path": {
        (nfc("삼성"), nfc("이재용")),
        (nfc("삼성"), nfd("서울")),
        (nfc("이재용"), nfd("서울")),
    }
}


@pytest.fixture
def evaluate(monkeypatch):
    """Run one query line through ``evaluate_queries`` over MIXED.

    ``query_lines`` reads ``facts/query.dl``; patching it keeps these pins off
    the filesystem and lets each one name its own query."""

    def run(query: str, inferred=None, path_nodes=None) -> list[str]:
        monkeypatch.setattr(rlc, "query_lines", lambda: [query])
        return rlc.evaluate_queries(
            MIXED,
            inferred or REACHABLE,
            set(),
            NODES if path_nodes is None else path_nodes,
        )

    return run


class TestReportAnswersEitherSpelling:
    @pytest.mark.parametrize("form", [nfd, nfc])
    def test_relation_reaches_the_fact(self, form, evaluate) -> None:
        """``relation_results`` compares raw, unlike ``ask``'s
        ``evaluate_relation`` which folds — so the report and the router gave
        different answers to the same query.

        Only the ``nfd`` parametrization is evidence (RED before:
        ``relation results: 0 rows``). 삼성 is stored composed, so the ``nfc``
        one passed already and is a GUARD."""
        [line] = evaluate(f'relation("{form("삼성")}", "대표", O)?')
        assert line == f"relation results: 1 rows; O={nfc('이재용')}"

    @pytest.mark.parametrize("form", [nfd, nfc])
    def test_count_reaches_the_fact(self, form, evaluate) -> None:
        """Only the ``nfd`` parametrization is evidence (RED before:
        ``0 (distinct objects)``, the report's least checkable output offered as
        a verified aggregate). The ``nfc`` one matches the stored spelling by
        luck and is a GUARD."""
        [line] = evaluate(f'count("{form("삼성")}", "대표")?')
        assert line.endswith(": 1 (distinct objects)")

    @pytest.mark.parametrize("form", [nfd, nfc])
    def test_path_is_evaluated_and_traced(self, form, evaluate) -> None:
        """BOTH parametrizations are evidence — measured RED at
        ``(not evaluated — not an accepted entity: 삼성)`` for the decomposed
        form and ``…: 서울`` for the composed one. The endpoints are stored in
        different forms, so no single form the author could type reached this
        KB, and the all-NFC case has no mixed-spelling excuse."""
        [line] = evaluate(f'path("{form("삼성")}", "{form("서울")}")?')
        assert line.endswith(
            f": {nfc('삼성')} -> {nfc('이재용')} -> {nfd('서울')}"
        )


class TestEchoIsWhatTheAuthorWrote:
    def test_count_echoes_the_written_line(self, evaluate) -> None:
        """The echo must NOT be resolved. This is what stops a later refactor
        from folding the echo along with the evaluation — at which point the
        reader could no longer find the line in facts/query.dl."""
        query = f'count("{nfd("삼성")}", "대표")?'
        [line] = evaluate(query)
        assert line == f"count results (query: {query}): 1 (distinct objects)"
        assert nfd("삼성") in line
        assert f'"{nfc("삼성")}"' not in line

    def test_path_head_echoes_the_written_endpoints(self, evaluate) -> None:
        query = f'path("{nfd("삼성")}", "{nfd("서울")}")?'
        [line] = evaluate(query)
        assert line.startswith(f"path {nfd('삼성')} -> {nfd('서울')}: ")

    def test_path_refusal_names_a_constant_that_DID_move(self, evaluate) -> None:
        """The refusal message must name the WRITTEN endpoint even when that
        endpoint was resolved on the way to the verdict.

        The earlier version of this pin asked about 현대 (absent from the map)
        and 서울 (asked in its stored form), so resolution was the identity on
        every constant in the line and a message built from the *tested* constant
        read the same — it could not fail. This one asks about 서울 in the form
        the KB does NOT store, so the constant moves, and the endpoint is a
        literal (object of an attribute relation) so the refusal still fires.
        Mutating the message to ``{display_value(tested)}`` dies here."""
        [line] = evaluate(
            f'path("{nfc("삼성")}", "{nfc("서울")}")?',
            inferred=REACHABLE,
            path_nodes={nfc("삼성")},
        )
        assert line == (
            f"path {nfc('삼성')} -> {nfc('서울')}: "
            f"(not evaluated — not an accepted entity: {nfc('서울')})"
        )
        assert nfd("서울") not in line

    def test_path_refusal_still_names_an_absent_endpoint(self, evaluate) -> None:
        """GUARD, not evidence — 현대 is absent from the map, so resolution is
        the identity here and this passes either way. Kept so the message cannot
        regress for the ordinary unknown-constant case."""
        [line] = evaluate(f'path("{nfd("현대")}", "{nfd("서울")}")?')
        assert line == (
            f"path {nfd('현대')} -> {nfd('서울')}: "
            f"(not evaluated — not an accepted entity: {nfd('현대')})"
        )


class TestValidateQueryVocabulary:
    def test_a_resolvable_constant_is_not_warned_as_absent(self) -> None:
        """RED before: ``query references non-engine entity or relation: 삼성``.
        The warning is the report telling the reader the KB never heard of a
        value it in fact holds."""
        for form in (nfd, nfc):
            errors, warnings = rlc.validate_query(
                f'path("{form("삼성")}", "{form("서울")}")?',
                VALUES,
                set(),
                NODES,
                SPELLING,
            )
            assert (errors, warnings) == ([], []), form

    def test_a_warning_about_a_MOVED_constant_names_the_written_form(self) -> None:
        """The path-endpoint warning must quote what the author typed even when
        the constant was resolved to reach the verdict.

        Same gap the router pin uses: 서울 is a literal here (path_nodes excludes
        it), so it resolves — NFC to the stored NFD — and is still warned about.
        A warning built from the tested constant would print the NFD form the
        author never wrote. The earlier pin asked only about constants on which
        resolution was the identity, so it could not fail."""
        _errors, warnings = rlc.validate_query(
            f'path("{nfc("삼성")}", "{nfc("서울")}")?',
            VALUES,
            set(),
            {nfc("삼성")},
            SPELLING,
        )
        assert warnings == [
            f"query path argument is not an accepted entity: {nfc('서울')}"
        ]
        assert nfd("서울") not in warnings[0]

    def test_an_absent_constant_is_still_warned_and_named_as_typed(self) -> None:
        """GUARD, not evidence — 현대 is absent from the map, so resolution is
        the identity and this reads the same either way."""
        _errors, warnings = rlc.validate_query(
            f'path("{nfd("현대")}", "{nfd("서울")}")?', VALUES, set(), NODES, SPELLING
        )
        assert warnings == [
            f"query references non-engine entity or relation: {nfd('현대')}"
        ]


class TestPolicyResultLineFiltersOnResolvedArgs:
    """``policy_row_matches`` compares RAW at every position, so the constants it
    filters with must already carry the KB's spelling — otherwise the report
    answers 0 rows for a policy row the engine really inferred, and prints it
    beside a "Policy evaluation: N rows" extent line that disagrees.

    This is the pin that makes ``filter_args = query_args(resolved)`` load-bearing;
    with ``filter_args = args`` the whole suite still passed.
    """

    # A reason code that is also a KB value, stored decomposed — the only shape
    # where a position past the first moves. See
    # test_a_reason_code_that_is_also_a_kb_value_is_rewritten for why this is a
    # documented cost rather than a bug.
    FACTS = rows((nfc("삼성"), "상태", nfd("보류")))
    INFERRED = {"needs_review": {(nfc("삼성"), nfd("보류"))}}

    def test_position_0_filters_on_the_resolved_constant(self) -> None:
        spelling = kb_query_spellings(rows((nfd("서울"), "상태", "x")))
        line = rlc.policy_result_line(
            "needs_review",
            f'needs_review("{nfc("서울")}", R)?',
            {"needs_review": {(nfd("서울"), "stale")}},
            resolve_query_spellings(f'needs_review("{nfc("서울")}", R)?', spelling),
        )
        assert line.startswith("needs_review results (query: ") and "1 rows" in line

    def test_positions_past_the_first_filter_on_the_resolved_constant(self) -> None:
        spelling = kb_query_spellings(self.FACTS)
        written = f'needs_review("{nfc("삼성")}", "{nfc("보류")}")?'
        line = rlc.policy_result_line(
            "needs_review",
            written,
            self.INFERRED,
            resolve_query_spellings(written, spelling),
        )
        assert "1 rows" in line, line
        # ...and the echo is still the line the author wrote.
        assert f"(query: {written})" in line

    def test_omitting_resolved_keeps_the_unresolved_reading(self) -> None:
        """GUARD, not evidence. The parameter is trailing and optional; the
        three-argument callers that existed before must behave as they did."""
        written = f'needs_review("{nfc("삼성")}", "{nfc("보류")}")?'
        line = rlc.policy_result_line("needs_review", written, self.INFERRED)
        assert "0 rows" in line, line

    def test_omitting_the_map_keeps_the_unresolved_reading(self) -> None:
        """GUARD, not evidence. The parameter is trailing and optional; the
        four-argument callers that existed before must behave exactly as they
        did."""
        _errors, warnings = rlc.validate_query(
            f'path("{nfd("삼성")}", "{nfd("서울")}")?', VALUES, set(), NODES
        )
        assert warnings == [
            f"query references non-engine entity or relation: {nfd('삼성')}"
        ]


class TestUniformKbIsUntouched:
    """GUARD, not evidence — a KB written one way resolves every constant to
    itself, so no report line may move. This is the property the reviewer praised
    on the write side, kept on the read side."""

    @pytest.mark.parametrize("form", [nfc, nfd])
    def test_report_lines_are_unchanged(self, form, monkeypatch) -> None:
        uniform = rows(
            (form("삼성"), "대표", form("이재용")),
            (form("이재용"), "거주", form("서울")),
        )
        nodes = {row[key] for row in uniform for key in ("subject", "object")}
        reachable = {
            "path": {
                (form("삼성"), form("이재용")),
                (form("삼성"), form("서울")),
                (form("이재용"), form("서울")),
            }
        }
        queries = [
            f'count("{form("삼성")}", "대표")?',
            f'path("{form("삼성")}", "{form("서울")}")?',
            f'relation("{form("삼성")}", "대표", O)?',
        ]
        monkeypatch.setattr(rlc, "query_lines", lambda: queries)
        assert rlc.evaluate_queries(uniform, reachable, set(), nodes) == [
            f'count results (query: {queries[0]}): 1 (distinct objects)',
            f"path {form('삼성')} -> {form('서울')}: "
            f"{form('삼성')} -> {form('이재용')} -> {form('서울')}",
            f"relation results: 1 rows; O={form('이재용')}",
        ]
