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
from factlog.common import canonical_value, kb_query_spellings, resolve_query_spellings


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


class TestResolutionOntoACanonicalAmount:
    """A query that resolves onto a canonical ``amount`` value must not be warned
    about as absent, and the report must not contradict itself about the line.

    Every canonical ``amount`` value carries a ``"`` (``amount(1000,"億")`` — it
    is the form merge stores), so resolution puts a ``\\"`` into the query STRING:
    ``resolve_query_spellings`` re-quotes through ``json.dumps``. Scanning that
    string with ``quoted_constants``' raw ``"([^"]+)"`` splits on the escape and
    returns a DIFFERENT NUMBER of constants than the written line has —
    ``['amount(1000,\\\\', ')', '규모']`` against ``['amount(1000,億)', '규모']`` —
    so ``_paired_constants`` took its desync fallback and reverted the vocabulary
    test to the unresolved reading.

    The result was one report disagreeing with itself about one line: the answer
    branches index ``query_args(resolved)`` and answered correctly, while the
    warning said the KB had never heard of the value it had just counted. That is
    the #328/#329 shape. Nothing to do with forbidden characters — it fires for
    any query naming an amount in the unquoted-unit form a human types.
    """

    ROWS = rows(("예산안", "규모", 'amount(1000,"億")'))
    VALUES = {row[key] for row in ROWS for key in ("subject", "object")}
    SPELLING = kb_query_spellings(ROWS)
    # The unquoted-unit spelling, which is what an author writes and what
    # `_canonical_value` folds onto the stored form.
    # The relation argument is a VARIABLE so every quoted constant on the line
    # is a value the KB holds: the relation NAME is legitimate vocabulary that
    # `build_report_text` drops through `names_a_relation`, and leaving it in
    # would put a warning in these lists that says nothing about resolution.
    WRITTEN = 'relation("예산안", R, "amount(1000,億)")?'

    def test_a_resolvable_amount_is_not_warned_as_absent(self) -> None:
        """RED before:
        ``['query references non-engine entity or relation: amount(1000,億)']``."""
        errors, warnings = rlc.validate_query(
            self.WRITTEN, self.VALUES, set(), None, self.SPELLING
        )
        assert (errors, warnings) == ([], [])

    def test_the_report_does_not_contradict_itself_about_the_line(
        self, monkeypatch
    ) -> None:
        """The answer branch reads ``query_args(resolved)`` directly and was
        always right; the warning came from the pairing and was wrong. Asserted
        together because a report carrying both lines is the defect — either line
        alone reads as correct."""
        monkeypatch.setattr(rlc, "query_lines", lambda: [self.WRITTEN])
        assert rlc.evaluate_queries(
            self.ROWS, {}, set(), None, self.SPELLING
        ) == ["relation results: 1 rows; R=규모"]
        _errors, warnings = rlc.validate_query(
            self.WRITTEN, self.VALUES, set(), None, self.SPELLING
        )
        assert warnings == []

    def test_an_absent_amount_is_still_warned_and_named_as_typed(self) -> None:
        """GUARD for the display side of the pairing: the constant reaching the
        message is now the parser's decoded argument rather than the regex's raw
        capture, so pin that an amount the KB does NOT hold still draws the
        warning and still reads back in the spelling the author typed."""
        written = 'relation("예산안", R, "amount(2000,億)")?'
        _errors, warnings = rlc.validate_query(
            written, self.VALUES, set(), None, self.SPELLING
        )
        assert warnings == [
            "query references non-engine entity or relation: amount(2000,億)"
        ]


class TestPolicyBranchEchoNamesTheWrittenConstant:
    """``validate_query``'s policy branch tests the RESOLVED constant and names
    the WRITTEN one. Both halves need a pin; only the test half had one.

    The echo half is unobservable through ``build_report_text``, which derives
    *entities* and *spelling* from the same rows — a constant that resolved is
    then necessarily in *entities*, so the warning cannot fire for it and the two
    constants can never differ where a message is produced. That is a CALLER
    invariant, not something ``validate_query`` enforces: it takes *entities* and
    *spelling* as independent parameters and never checks one against the other.

    So this calls it directly with a map whose value is deliberately absent from
    *entities*. That is not a KB the pipeline can produce today, and it is the
    point — it is the configuration a future second caller would introduce, and
    the reason the message operand is written the way it is. Mutating
    ``arg_value(args[0])`` to ``arg_value(query_args(resolved)[0])`` dies here and
    nowhere else in the suite.
    """

    WRITTEN = nfd("한라산기지")
    STORED = nfc("한라산기지")
    # Values NOT a superset of the map's — the invariant build_report_text keeps
    # and this caller breaks on purpose.
    ENTITIES = {"무관한값"}
    SPELLING = {canonical_value(WRITTEN): STORED}

    def test_the_warning_names_the_spelling_the_author_typed(self) -> None:
        """RED after mutating the message operand to the resolved constant:
        the warning names 한라산기지 in the KB's NFC form, which the author never
        wrote and cannot find in their query file."""
        line = f'needs_review("{self.WRITTEN}", R)?'
        errors, warnings = rlc.validate_query(
            line, self.ENTITIES, {"needs_review"}, None, self.SPELLING
        )
        assert errors == []
        assert warnings == [
            f"query references non-engine entity: {self.WRITTEN}"
        ]
        assert self.STORED not in warnings[0]

    def test_the_membership_test_reads_the_resolved_constant(self) -> None:
        """The other half, kept beside it: with the stored spelling present in
        *entities* the warning must NOT fire, even though the constant the author
        wrote is absent from *entities* in that spelling. Mutating
        ``query_args(resolved)[0]`` back to ``args[0]`` dies here."""
        line = f'needs_review("{self.WRITTEN}", R)?'
        _errors, warnings = rlc.validate_query(
            line, {self.STORED}, {"needs_review"}, None, self.SPELLING
        )
        assert warnings == []


class TestPairedConstantsFallback:
    """``_paired_constants`` degrades rather than raising, and what it degrades TO
    is the whole point: the unresolved reading, so the fallback can only warn
    where the pre-resolution code warned.

    Nothing pinned it. Deleting the guard (``zip(..., strict=False)``) and
    inverting it (``[(c, c) for c in resolved]``) both left the suite green, so
    the next refactor could take either. It is a guard for a caller that does not
    exist yet, which is exactly the kind of branch that gets removed as dead — and
    the branch whose previous annotation caused the desync bug.
    """

    def test_equal_lengths_zip_pairwise(self) -> None:
        assert rlc._paired_constants(["a", "b"], ["A", "B"]) == [("a", "A"), ("b", "B")]

    def test_a_desync_pairs_every_written_constant_with_ITSELF(self) -> None:
        """Not truncated against the resolved list, and not the resolved list's
        own constants. Both wrong answers keep the same length as one correct
        one, so length alone does not distinguish them — assert the values."""
        assert rlc._paired_constants(["a", "b"], ["A"]) == [("a", "a"), ("b", "b")]
        assert rlc._paired_constants(["a"], ["A", "B"]) == [("a", "a")]
