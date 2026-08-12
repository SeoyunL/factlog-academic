# SPDX-License-Identifier: Apache-2.0
"""``kb_query_spellings`` / ``resolve_query_spellings`` — the READ side of the
spelling ``dedup_engine_atoms`` writes.

``kb_spellings`` picks one spelling per value for ``accepted.dl``. Its own
docstring says every map keyed that way must be *looked up* through it too, and
until these two helpers existed no lookup existed: a KB whose atoms had been
folded to one spelling could not be addressed by a query written in the other.
These tests pin the map's shape and the rewrite's invariants; the end-to-end
effect on ``ask`` and on the report is pinned where those paths live.
"""
from __future__ import annotations

import unicodedata

import pytest

from factlog.common import kb_query_spellings, resolve_query_spellings


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def nfd(value: str) -> str:
    return unicodedata.normalize("NFD", value)


def rows(*triples: tuple[str, str, str]) -> list[dict[str, str]]:
    """Engine-input rows (no ``status`` column), the shape ``load_accepted_facts``
    returns — which is what the map must describe, because it reports what
    ``accepted.dl`` HOLDS, not what a compile would decide to write."""
    return [
        {"subject": s, "relation": r, "object": o}
        for s, r, o in triples
    ]


# The reviewer's reproduction, after this PR's fold: 삼성/이재용 land composed,
# 서울 stays decomposed, so no single normalization form addresses the file.
MIXED = rows(
    (nfc("삼성"), "대표", nfc("이재용")),
    (nfc("이재용"), "거주", nfd("서울")),
)


class TestKbQuerySpellings:
    def test_maps_a_folded_key_to_the_spelling_the_file_holds(self) -> None:
        spelling = kb_query_spellings(MIXED)
        assert spelling[nfc("삼성")] == nfc("삼성")
        assert spelling[nfc("서울")] == nfd("서울")

    def test_key_is_the_folded_form_so_either_spelling_finds_it(self) -> None:
        """The map is keyed by ``_canonical_value``, so a query constant typed in
        EITHER form hits the same entry. This is the property that makes the
        rewrite possible at all."""
        spelling = kb_query_spellings(MIXED)
        assert spelling[nfc(nfd("서울"))] == nfd("서울")

    def test_relation_names_are_not_in_the_map(self) -> None:
        """The pool is ``value_set`` — subjects and objects only. A relation name
        must never acquire a representative here: this change leaves the relation
        axis unfolded, so a value that is also a relation name would otherwise be
        rewritten toward a spelling chosen for the wrong axis."""
        spelling = kb_query_spellings(MIXED)
        assert "대표" not in spelling
        assert "거주" not in spelling

    def test_ambiguous_fold_is_dropped_not_guessed(self) -> None:
        """``_canonical_value`` folds past NFC (``literal_types.canonical_amount``)
        while ``merge_candidates`` canonicalises only the object, so one
        ``accepted.dl`` really can hold two distinct atoms sharing one key.
        Resolving such a key would answer a question about one atom from the
        other, silently — so the key is dropped and the query is passed through
        to fail the way it does without the map."""
        ambiguous = rows(
            ("A", "금액", 'amount(1,000,"억")'),
            ("B", "금액", 'amount(1000,"억")'),
        )
        spelling = kb_query_spellings(ambiguous)
        assert [key for key in spelling if key.startswith("amount(")] == []
        # The unambiguous neighbours in the same KB are unaffected.
        assert spelling["A"] == "A"

    def test_unambiguous_amount_fold_still_resolves(self) -> None:
        """GUARD, not evidence — it passes before and after the refusal rule.
        It is here so the rule above cannot be "fixed" into dropping every
        amount-shaped key: only a value the KB spells more than one way is
        refused."""
        single = rows(("A", "금액", 'amount(1000,"억")'))
        spelling = kb_query_spellings(single)
        assert spelling['amount(1000,"억")'] == 'amount(1000,"억")'

    def test_two_spellings_of_one_value_are_refused_even_within_NFC(self) -> None:
        """The refusal is on the RAW spellings, not on the NFC forms.

        ``relation/3`` atoms are keyed on BYTES, so two canonically equivalent
        subjects are TWO atoms. A rule that refused only a second NFC form would
        pass this pool and resolve the decomposed query onto the composed atom —
        answering a question about one atom with another atom's object, which is
        worse than the unaddressability the map exists to cure.

        Measured before the raw-spelling refusal:
        ``relation(NFD(삼성), "대표", O)?`` returned ``O=이재용`` where the raw
        report returned ``O=이건희``."""
        stale = rows(
            (nfd("삼성"), "대표", "이건희"),
            (nfc("삼성"), "대표", "이재용"),
        )
        spelling = kb_query_spellings(stale)
        assert nfc("삼성") not in spelling
        line = f'relation("{nfd("삼성")}", "대표", O)?'
        assert resolve_query_spellings(line, spelling) is line
        # The rest of the same KB keeps resolving.
        assert spelling["이건희"] == "이건희"

    def test_refusal_costs_nothing_on_a_compiled_kb(self) -> None:
        """``kb_spellings`` already gives each value ONE spelling in
        accepted.dl, so every pool of a freshly compiled KB is a singleton and
        the stricter refusal drops nothing. Pinned on the mixed KB the reviewer
        reproduced with, which is the hardest case: both forms occur in the file,
        but no single VALUE is spelled two ways."""
        spelling = kb_query_spellings(MIXED)
        assert spelling[nfc("삼성")] == nfc("삼성")
        assert spelling[nfc("서울")] == nfd("서울")
        assert spelling[nfc("이재용")] == nfc("이재용")


class TestResolveQuerySpellings:
    def test_rewrites_a_decomposed_constant_onto_the_composed_atom(self) -> None:
        spelling = kb_query_spellings(MIXED)
        line = f'path("{nfd("삼성")}", "{nfd("서울")}")?'
        assert resolve_query_spellings(line, spelling) == (
            f'path("{nfc("삼성")}", "{nfd("서울")}")?'
        )

    def test_rewrites_a_composed_constant_onto_the_decomposed_atom(self) -> None:
        """The reverse direction matters as much: the file is mixed, so an
        all-NFC query is just as unaddressable as an all-NFD one."""
        spelling = kb_query_spellings(MIXED)
        line = f'path("{nfc("삼성")}", "{nfc("서울")}")?'
        assert resolve_query_spellings(line, spelling) == (
            f'path("{nfc("삼성")}", "{nfd("서울")}")?'
        )

    def test_relation_argument_is_never_rewritten(self) -> None:
        """``engine_atom_key`` leaves the relation axis unfolded, so one file may
        hold two spellings of one relation and there is no representative to
        resolve onto. A KB where a relation name also appears as a value must not
        leak that value's representative into the relation position."""
        both = rows((nfd("서울"), nfc("서울"), "x"))
        spelling = kb_query_spellings(both)
        line = f'relation(S, "{nfc("서울")}", O)?'
        assert resolve_query_spellings(line, spelling) == line

    def test_review_required_question_is_never_rewritten(self) -> None:
        """Its constant is the user's original question, not a KB value."""
        spelling = kb_query_spellings(MIXED)
        line = f'review_required("{nfd("서울")} 인구는?")?'
        assert resolve_query_spellings(line, spelling) == line

    def test_variables_and_bare_tokens_are_left_alone(self) -> None:
        spelling = kb_query_spellings(MIXED)
        line = f'relation("{nfd("삼성")}", R, O)?'
        assert resolve_query_spellings(line, spelling) == (
            f'relation("{nfc("삼성")}", R, O)?'
        )

    def test_policy_predicate_position_0_resolves(self) -> None:
        """An unknown predicate is a policy predicate. ``policy_row_matches``
        compares RAW at every position, so an unresolved constant simply misses
        the row."""
        spelling = kb_query_spellings(MIXED)
        assert resolve_query_spellings(
            f'needs_review("{nfc("서울")}", "stale")?', spelling
        ) == f'needs_review("{nfd("서울")}", "stale")?'

    def test_policy_predicate_resolves_positions_PAST_the_first(self) -> None:
        """The pin that makes ``tuple(range(len(args)))`` load-bearing.

        An ASCII reason code like ``"stale"`` is absent from the value map, so it
        passes through whether the code resolves one position or all of them — an
        assertion using one cannot tell ``(0,)`` from ``range``. This uses a
        reason code that IS a KB value stored in the other normal form, which is
        the only shape that distinguishes them.

        Reachable only through a hand-written ``logic-policy.extra.dl``:
        ``generate_logic_policy.REASON_RE`` forces ``[a-z0-9_]+`` on generated
        codes, so no generated one can collide with a Korean value. The trade-off
        is recorded in ``resolve_query_spellings``' docstring — see also
        ``test_a_reason_code_that_is_also_a_kb_value_is_rewritten`` below, which
        pins the cost rather than hiding it."""
        facts = rows((nfc("삼성"), "상태", nfd("보류")))
        spelling = kb_query_spellings(facts)
        assert resolve_query_spellings(
            f'needs_review("{nfc("삼성")}", "{nfc("보류")}")?', spelling
        ) == f'needs_review("{nfc("삼성")}", "{nfd("보류")}")?'

    def test_a_reason_code_that_is_also_a_kb_value_is_rewritten(self) -> None:
        """The KNOWN COST of resolving every policy position, pinned so it cannot
        be rediscovered as a surprise.

        When a hand-written policy emits a reason code that is also a KB value,
        and the KB stores that value in the other normal form, the query's reason
        constant moves and ``policy_row_matches`` (raw) stops matching the row.
        The alternative — resolving only position 0 — breaks every hand-written
        policy query on a folded KB instead, which is the wider harm. If this
        test ever needs to change, that trade-off is what is being revisited."""
        facts = rows((nfc("삼성"), "상태", nfd("보류")))
        spelling = kb_query_spellings(facts)
        resolved = resolve_query_spellings(
            f'needs_review("{nfc("삼성")}", "{nfc("보류")}")?', spelling
        )
        # An engine row carrying the NFC reason code no longer matches.
        assert nfd("보류") in resolved and f'"{nfc("보류")}"' not in resolved

    def test_returns_the_input_object_unchanged_when_nothing_moves(self) -> None:
        """IDENTITY, not merely an equal line.

        Two fixtures, because they fail the "rewrite unconditionally" mutant for
        different reasons and only one of them justifies the ``is``:

        * the ALREADY-CANONICAL line is the one that makes ``is`` load-bearing.
          Reassembly reproduces it character for character, so an ``==``
          assertion passes on the mutant and only object identity catches it.
        * the LOOSELY spaced line shows what the invariant is protecting —
          reassembly normalises whitespace, so a function that rewrote
          unconditionally would silently reformat every query line of a
          uniformly spelled KB, where it has nothing to do. ``==`` would catch
          this one too; that is not what it is here for.

        Measured against a guard-less copy of the function: canonical → ``==``
        survives, ``is`` fails; loose → both fail."""
        spelling = kb_query_spellings(MIXED)
        canonical = f'relation("{nfc("삼성")}", "대표", O)?'
        assert resolve_query_spellings(canonical, spelling) is canonical
        loose = f'relation( "{nfc("삼성")}" ,  "대표" , O )?'
        assert resolve_query_spellings(loose, spelling) is loose

    @pytest.mark.parametrize(
        "line",
        [
            "not a query",
            "relation(",
            "",
            "path()?",
        ],
    )
    def test_unparseable_lines_pass_through(self, line: str) -> None:
        """Total: the gate, not this function, is what refuses a malformed line."""
        assert resolve_query_spellings(line, kb_query_spellings(MIXED)) is line

    def test_embedded_quotes_and_commas_round_trip(self) -> None:
        """``_query_args`` is quote-aware and the rewrite re-quotes with
        ``json.dumps``, mirroring ``arg_value``'s ``json.loads``. A value carrying
        the delimiters must survive being taken apart and put back together."""
        tricky = nfd('서울, "특별시" \\ 1')
        both = rows((nfc("삼성"), "대표", tricky))
        spelling = kb_query_spellings(both)
        import json

        line = f'relation("{nfc("삼성")}", "대표", {json.dumps(nfc(tricky), ensure_ascii=False)})?'
        out = resolve_query_spellings(line, spelling)
        from factlog.common import arg_value, query_args

        assert arg_value(query_args(out)[2]) == tricky


class TestUniformKbIsUntouched:
    """GUARD, not evidence — every case here passes before and after the fix.

    They exist because the reviewer's strongest praise for this PR was that a
    uniformly spelled KB compiles byte-identically to main. The query side must
    keep that promise: on a KB written one way, every pool is a singleton, every
    constant already names its own spelling, and no line may move.
    """

    @pytest.mark.parametrize("form", [nfc, nfd])
    def test_no_line_moves_on_a_uniform_kb(self, form) -> None:
        uniform = rows(
            (form("삼성"), "대표", form("이재용")),
            (form("이재용"), "거주", form("서울")),
        )
        spelling = kb_query_spellings(uniform)
        for line in (
            f'path("{form("삼성")}", "{form("서울")}")?',
            f'relation("{form("삼성")}", "대표", O)?',
            f'count("{form("삼성")}", "대표")?',
        ):
            assert resolve_query_spellings(line, spelling) is line

    @pytest.mark.parametrize("form", [nfc, nfd])
    def test_map_is_the_identity_on_a_uniform_kb(self, form) -> None:
        uniform = rows((form("삼성"), "대표", form("이재용")))
        for key, value in kb_query_spellings(uniform).items():
            assert value == key or nfc(value) == key
