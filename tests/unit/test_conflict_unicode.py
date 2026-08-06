# SPDX-License-Identifier: Apache-2.0
"""Unit tests for Unicode-normalization folding in conflict detection (#325).

``check_conflicts`` grouped only the *relation* axis under a Unicode fold. The
subject axis and the object axis used the raw string, so a KB that mixes NFC and
NFD spellings of the same text (routine on macOS, whose filesystem and IMEs emit
Hangul in NFD) failed in both directions at once:

* **subject axis, false negative (unsound)** — a real contradiction split into
  two singleton groups and the gate passed a KB that does contain one;
* **object axis, false positive** — two objects that render identically on
  screen were reported as a contradiction the reader cannot act on.

The object fold has to happen *before* ``literal_types.normalize``, not only on
the untyped fallback: an NFD-authored typed literal does not parse, degrades to
the ``"raw"`` tag, and never meets its NFC twin under ``"scalar"``.

Folding is NFC only — compatibility variants (fullwidth) and case stay distinct.
"""
from __future__ import annotations

import itertools
import unicodedata

import check_conflicts
import common


def _fact(subject: str, relation: str, obj: str, status: str = "confirmed") -> dict[str, str]:
    return {
        "subject": subject,
        "relation": relation,
        "object": obj,
        "source": "sources/x.md",
        "status": status,
        "confidence": "0.9",
        "note": "",
    }


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _nfd(value: str) -> str:
    return unicodedata.normalize("NFD", value)


_AMOUNT_SPEC = common.TypedRelSpec("amount", "revenue")
_TYPED_AMOUNT = {"매출": _AMOUNT_SPEC}
_TYPED_ORDINAL = {"순위": common.TypedRelSpec("ordinal", "rank")}


class TestTypedObjectFoldedBeforeParse:
    """An NFD-authored typed literal must reach its scalar, not degrade to raw."""

    def test_amount_nfc_vs_nfd_same_value_no_conflict(self):
        # Same amount, one row authored NFD: the 억 unit decomposes, parse_amount
        # fails, and the row would key as ("raw", …) against its twin's scalar.
        obj = 'amount(5400,"억")'
        facts = [
            _fact("갑사", "매출", _nfc(obj)),
            _fact("갑사", "매출", _nfd(obj)),
        ]
        assert check_conflicts.detect_conflicts(facts, {"매출"}, _TYPED_AMOUNT) == {}

    def test_ordinal_nfc_vs_nfd_same_rank_no_conflict(self):
        facts = [
            _fact("갑", "순위", _nfc("제3호")),
            _fact("갑", "순위", _nfd("제3호")),
        ]
        assert check_conflicts.detect_conflicts(facts, {"순위"}, _TYPED_ORDINAL) == {}

    def test_all_nfd_kb_cross_notation_amounts_collapse(self):
        # 5400억 == 0.54조 == 5.4e11. In an all-NFD KB neither side parsed before,
        # so #116's cross-notation equivalence never fired there; it now does.
        #
        # And it fires on BOTH sides. `literal_types.parse_amount` composes its
        # lookup key, so the raw NFD literal parses without any help from this
        # module's fold: the engine, which hands `normalize` the raw object
        # (common._project_typed_relations), reaches the same 5.4e11 and inserts
        # both rows into `revenue`. Nothing here is a merge only this module can
        # see, so `parse_merges` is empty and the exit-0 disclosure stays silent —
        # asserted directly, since an empty `parse_merges` is the difference
        # between "the engine agrees" and "the reader was not told".
        facts = [
            _fact("갑사", "매출", _nfd('amount(5400,"억")')),
            _fact("갑사", "매출", _nfd('amount(0.54,"조")')),
        ]
        scan = check_conflicts.collect_conflicts(facts, {"매출"}, _TYPED_AMOUNT)
        assert scan.conflicts == {}
        assert scan.parse_merges == {}
        # The engine's own path agrees, row for row — that is why no disclosure
        # is owed here (contrast the ordinal case, where it still is).
        assert {
            common.literal_types.normalize("amount", f["object"], None) for f in facts
        } == {540_000_000_000}

    def test_nfd_typed_literals_with_different_values_still_conflict(self):
        # Folding must not swallow a genuine typed contradiction.
        facts = [
            _fact("갑사", "매출", _nfd('amount(5400,"억")')),
            _fact("갑사", "매출", _nfd('amount(1,"조")')),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"매출"}, _TYPED_AMOUNT)
        assert list(conflicts) == [("갑사", "매출")]
        assert len(conflicts[("갑사", "매출")]) == 2


class TestSubjectAxisFolded:
    """Issue case (a): the unsound false negative."""

    def test_mixed_subject_different_objects_detected(self):
        facts = [
            _fact(_nfc("김철수"), "소속", "A사"),
            _fact(_nfd("김철수"), "소속", "B사"),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"소속"}, {})
        assert len(conflicts) == 1
        ((subject, relation), objects), = conflicts.items()
        assert _nfc(subject) == _nfc("김철수")
        assert relation == "소속"
        assert objects == ["A사", "B사"]

    def test_mixed_subject_same_object_no_conflict(self):
        facts = [
            _fact(_nfc("김철수"), "소속", "A사"),
            _fact(_nfd("김철수"), "소속", "A사"),
        ]
        assert check_conflicts.detect_conflicts(facts, {"소속"}, {}) == {}

    def test_reported_subject_is_a_raw_spelling_actually_present(self):
        # Provenance: the reported subject is one of the strings as written, not a
        # synthesized NFC form.
        raws = [_nfc("김철수"), _nfd("김철수")]
        facts = [_fact(raws[0], "소속", "A사"), _fact(raws[1], "소속", "B사")]
        conflicts = check_conflicts.detect_conflicts(facts, {"소속"}, {})
        (subject, _), = conflicts
        assert subject in raws

    def test_reported_subject_prefers_the_composed_spelling(self):
        # Plain min() would always return the NFD form: conjoining jamo (U+1100…)
        # sort below precomposed syllables (U+AC00…). That is the spelling that
        # will NOT match what a reader types from an NFC editor.
        raws = [_nfc("김철수"), _nfd("김철수")]
        assert min(raws) == _nfd("김철수")  # the trap this avoids
        for ordering in ([raws[0], raws[1]], [raws[1], raws[0]]):
            facts = [_fact(ordering[0], "소속", "A사"), _fact(ordering[1], "소속", "B사")]
            conflicts = check_conflicts.detect_conflicts(facts, {"소속"}, {})
            (subject, _), = conflicts
            assert subject == _nfc("김철수")

    def test_distinct_subjects_are_not_merged(self):
        facts = [
            _fact("김철수", "소속", "A사"),
            _fact("이영희", "소속", "B사"),
        ]
        assert check_conflicts.detect_conflicts(facts, {"소속"}, {}) == {}


class TestUntypedObjectAxisFolded:
    """Issue case (b): the unactionable false positive."""

    def test_mixed_untyped_object_same_value_no_conflict(self):
        facts = [
            _fact("연구소", "소속", _nfc("한국대학교")),
            _fact("연구소", "소속", _nfd("한국대학교")),
        ]
        assert check_conflicts.detect_conflicts(facts, {"소속"}, {}) == {}

    def test_reported_object_is_a_raw_string_actually_present(self):
        raws = [_nfc("한국대학교"), _nfd("한국대학교")]
        facts = [
            _fact("연구소", "소속", raws[0]),
            _fact("연구소", "소속", raws[1]),
            _fact("연구소", "소속", "서울대학교"),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {"소속"}, {})
        values = conflicts[("연구소", "소속")]
        # Two equivalence classes, and the merged one reports its composed form.
        assert values == ["서울대학교", _nfc("한국대학교")]


class TestRepresentativeChoice:
    """The reported string is one that was written, and the one likeliest to grep."""

    def test_representative_prefers_the_composed_spelling(self):
        # Plain min() would always return the NFD form: conjoining jamo (U+1100…)
        # sort below precomposed syllables (U+AC00…). That is the spelling that
        # will NOT match what a reader types from an NFC editor.
        raws = {_nfc("한국대학교"), _nfd("한국대학교")}
        assert min(raws) == _nfd("한국대학교")  # the trap this avoids
        assert check_conflicts._representative(raws) == _nfc("한국대학교")

    def test_representative_is_deterministic_when_no_form_is_composed(self):
        # Two distinct NFD spellings that fold together cannot both be NFC; the
        # choice must still be stable, so it falls back to lexicographic order.
        raws = {_nfd("김철수"), _nfd("김철수") + "x"}
        assert check_conflicts._representative(raws) == min(raws)


class TestNonEquivalentNotationsStayDistinct:
    """NFC only: no NFKC, no casefold."""

    def test_fullwidth_stays_a_separate_value(self):
        facts = [_fact("갑", "속성", "ABC"), _fact("갑", "속성", "ＡＢＣ")]
        conflicts = check_conflicts.detect_conflicts(facts, {"속성"}, {})
        assert conflicts[("갑", "속성")] == sorted(["ABC", "ＡＢＣ"])

    def test_fullwidth_subject_stays_a_separate_entity(self):
        facts = [_fact("ABC", "속성", "x"), _fact("ＡＢＣ", "속성", "y")]
        assert check_conflicts.detect_conflicts(facts, {"속성"}, {}) == {}

    def test_case_stays_a_separate_value(self):
        facts = [_fact("갑", "속성", "abc"), _fact("갑", "속성", "ABC")]
        conflicts = check_conflicts.detect_conflicts(facts, {"속성"}, {})
        assert conflicts[("갑", "속성")] == ["ABC", "abc"]

    def test_case_subject_stays_a_separate_entity(self):
        facts = [_fact("abc", "속성", "x"), _fact("ABC", "속성", "y")]
        assert check_conflicts.detect_conflicts(facts, {"속성"}, {}) == {}


class TestRelationAxisMembershipVsGrouping:
    """The relation axis is two mechanisms, and only one of them is deferred.

    *Membership* — is this relation declared single-valued at all — is folded, so
    a KB written uniformly in NFD reaches the check instead of being skipped.
    ``common._relation_names_from`` does not normalize the names it parses out of
    ``policy/single-valued.md``, so before the fold the test was a raw byte
    comparison between the policy text and the candidates text: an all-NFD KB
    (routine on macOS, the very scenario ``_fold`` cites) never entered the
    grouping loop at all and exited 0 with a contradiction in it.

    *Grouping* — which rows share a conflict key — stays verbatim and is the part
    genuinely deferred to a follow-up, because that is what #210's "no silent NFC
    coercion for non-participating relations" speaks to. Each fixture below uses
    the realistic policy set (one NFC name), which is what the policy file
    actually yields.
    """

    def test_uniform_nfd_kb_reaches_the_membership_gate(self):
        # Both rows spell subject and relation NFD; the policy file spells the
        # relation NFC. Nothing here is a mixed-spelling case — one consistently
        # decomposed KB is enough — so this is a wider false negative than the
        # mixed-subject one #325 set out to fix.
        facts = [
            _fact(_nfd("김철수"), _nfd("소속"), "A사"),
            _fact(_nfd("김철수"), _nfd("소속"), "B사"),
        ]
        conflicts = check_conflicts.detect_conflicts(facts, {_nfc("소속")}, {}, {})
        assert len(conflicts) == 1
        ((_, relation), objects), = conflicts.items()
        assert objects == ["A사", "B사"]
        # Membership folded, grouping did not: the relation is reported as written.
        assert relation == _nfd("소속")
        assert relation != _nfc("소속")

    def test_mixed_relation_forms_still_split(self):
        # Grouping is untouched, so two spellings of one relation remain two
        # groups and no contradiction is reported. This is the deferred axis.
        facts = [
            _fact("김철수", _nfc("소속"), "A사"),
            _fact("김철수", _nfd("소속"), "B사"),
        ]
        assert check_conflicts.detect_conflicts(facts, {_nfc("소속")}, {}, {}) == {}

    def test_nfd_relation_name_reported_verbatim(self):
        # #210's contract, now exercised *through* a folded membership test: the
        # relation gets in on its fold but is still reported byte-for-byte.
        nfd_rel = _nfd("소속")
        facts = [_fact("김철수", nfd_rel, "A사"), _fact("김철수", nfd_rel, "B사")]
        conflicts = check_conflicts.detect_conflicts(facts, {_nfc("소속")}, {}, {})
        (_, relation), = conflicts
        assert relation == nfd_rel


class TestVariantChannels:
    """``collect_conflicts`` exposes the raw spellings behind each reported string.

    ``detect_conflicts`` keeps its established return shape (36 pinned tests
    assert it); the spelling maps ride extra channels so ``main`` can report a
    merge without duplicating the grouping logic. Both folded axes get a channel —
    the information loss is the same on each.
    """

    def test_detect_conflicts_return_shape_unchanged(self):
        facts = [_fact("갑", "속성", "x"), _fact("갑", "속성", "y")]
        assert check_conflicts.detect_conflicts(facts, {"속성"}, {}) == {("갑", "속성"): ["x", "y"]}

    def test_collect_returns_conflicts_and_both_spelling_maps(self):
        raws = [_nfc("김철수"), _nfd("김철수")]
        facts = [_fact(raws[0], "소속", "A사"), _fact(raws[1], "소속", "B사")]
        scan = check_conflicts.collect_conflicts(facts, {"소속"}, {})
        key = (_nfc("김철수"), "소속")
        assert scan.conflicts == {key: ["A사", "B사"]}
        assert scan.subject_variants[key] == sorted(raws)
        assert scan.object_variants[key] == {"A사": ["A사"], "B사": ["B사"]}

    def test_object_channel_lists_the_merged_spellings(self):
        raws = [_nfc("한국대학교"), _nfd("한국대학교")]
        facts = [
            _fact("연구소", "소속", raws[0]),
            _fact("연구소", "소속", raws[1]),
            _fact("연구소", "소속", "서울대학교"),
        ]
        objects = check_conflicts.collect_conflicts(facts, {"소속"}, {}).object_variants
        merged = objects[("연구소", "소속")]
        assert merged[_nfc("한국대학교")] == sorted(raws)
        assert merged["서울대학교"] == ["서울대학교"]

    def test_single_spelling_reports_one_variant_on_both_axes(self):
        facts = [_fact("갑", "속성", "x"), _fact("갑", "속성", "y")]
        scan = check_conflicts.collect_conflicts(facts, {"속성"}, {})
        assert scan.subject_variants[("갑", "속성")] == ["갑"]
        assert scan.object_variants[("갑", "속성")] == {"x": ["x"], "y": ["y"]}

    def test_object_variant_key_order_is_independent_of_row_order(self):
        # The groups are built in a dict keyed off set iteration, so insertion
        # order tracks row order. main() reads this through the sorted conflicts
        # list and the sorted objects list, so the report never showed it — but
        # this is a public return value of a module whose whole contract is
        # determinism, and leaving row order in it is a trap for the next caller.
        base = [_fact("갑", "속성", v) for v in ("a", "b", "c", "d")]
        orders = set()
        for perm in itertools.permutations(base):
            objects = check_conflicts.collect_conflicts(list(perm), {"속성"}, {}).object_variants
            orders.add(tuple(objects[("갑", "속성")]))
        assert orders == {("a", "b", "c", "d")}

    def test_variant_map_is_per_subject_relation_pair(self):
        # A per-folded-subject (global) map would let the mixed spelling of one
        # relation rewrite the reported subject of an unrelated relation.
        raws = [_nfc("김철수"), _nfd("김철수")]
        facts = [
            _fact(raws[0], "소속", "A사"),
            _fact(raws[1], "소속", "B사"),
            _fact(_nfc("김철수"), "직급", "부장"),
            _fact(_nfc("김철수"), "직급", "과장"),
        ]
        scan = check_conflicts.collect_conflicts(facts, {"소속", "직급"}, {})
        assert scan.subject_variants[(_nfc("김철수"), "소속")] == sorted(raws)
        assert scan.subject_variants[(_nfc("김철수"), "직급")] == [_nfc("김철수")]
        assert scan.conflicts[(_nfc("김철수"), "직급")] == ["과장", "부장"]


def _run_main(monkeypatch, facts, single_valued, typed=None, aliases=None):
    monkeypatch.setattr(check_conflicts, "ensure_dirs", lambda: None)
    monkeypatch.setattr(check_conflicts, "load_facts", lambda: facts)
    monkeypatch.setattr(check_conflicts, "single_valued_relations", lambda: single_valued)
    monkeypatch.setattr(check_conflicts, "typed_relations", lambda: typed or {})
    monkeypatch.setattr(check_conflicts, "relation_aliases", lambda: aliases or {})
    return check_conflicts.main([])


class TestReportExposesTheMerge:
    """A merged group must say so, and must show the spellings.

    Representative restoration keeps provenance, but when folding actually merged
    two spellings the reader who greps the reported string finds only some of the
    rows — and the strings render identically, so a count alone is not actionable.
    The disclosure is *additive*: a contradiction that a mixed spelling merely
    joined is still a contradiction, so the supersede guidance must not disappear.
    """

    def test_mixed_forms_are_named_in_the_conflict_line(self, monkeypatch, capsys):
        raws = [_nfc("김철수"), _nfd("김철수")]
        facts = [_fact(raws[0], "소속", "A사"), _fact(raws[1], "소속", "B사")]
        assert _run_main(monkeypatch, facts, {"소속"}) == 1
        err = capsys.readouterr().err
        assert "(subject written in 2 mixed Unicode normalization forms)" in err

    def test_mixed_subject_spellings_are_printed_escaped(self, monkeypatch, capsys):
        raws = [_nfc("김철수"), _nfd("김철수")]
        facts = [_fact(raws[0], "소속", "A사"), _fact(raws[1], "소속", "B사")]
        _run_main(monkeypatch, facts, {"소속"})
        err = capsys.readouterr().err
        # Labelled: an escaped code-point run tells the reader the spellings
        # differ but not which row to keep. The tool already knows — the
        # representative on the CONFLICT line above is the NFC one.
        assert (
            f"    subject spellings: {ascii(raws[1])} (NFD), {ascii(raws[0])} (NFC)\n"
        ) in err

    def test_mixed_object_spellings_are_printed_escaped(self, monkeypatch, capsys):
        raws = [_nfc("한국대학교"), _nfd("한국대학교")]
        facts = [
            _fact("연구소", "소속", raws[0]),
            _fact("연구소", "소속", raws[1]),
            _fact("연구소", "소속", "서울대학교"),
        ]
        _run_main(monkeypatch, facts, {"소속"})
        err = capsys.readouterr().err
        assert (
            f"    value {raws[0]!r} spellings: "
            f"{ascii(raws[1])} (NFD), {ascii(raws[0])} (NFC)\n"
        ) in err

    def test_form_label_names_only_the_two_folded_forms(self):
        assert check_conflicts._form_label(_nfc("김철수")) == "NFC"
        assert check_conflicts._form_label(_nfd("김철수")) == "NFD"
        # Neither form: composed and decomposed syllables in one string. Reported
        # as such rather than guessed at.
        assert check_conflicts._form_label(_nfc("김") + _nfd("철수")) == "mixed"

    def test_mixed_forms_get_the_unify_guidance_on_top(self, monkeypatch, capsys):
        raws = [_nfc("김철수"), _nfd("김철수")]
        facts = [_fact(raws[0], "소속", "A사"), _fact(raws[1], "소속", "B사")]
        _run_main(monkeypatch, facts, {"소속"})
        err = capsys.readouterr().err
        assert "Unify the spelling in sources/" in err
        assert "status='superseded'" in err

    def test_unify_guidance_points_at_the_source_not_candidates_csv(self, monkeypatch, capsys):
        # merge_candidates rebuilds rows from runs/*.json and carries back only
        # status, keyed on the raw triple — so a hand-edited spelling in
        # candidates.csv is discarded on the next merge AND stops matching the
        # key that preserves its 'superseded' mark. Recommending that edit sends
        # the reader round a loop that also undoes the repair that does work.
        raws = [_nfc("김철수"), _nfd("김철수")]
        facts = [_fact(raws[0], "소속", "A사"), _fact(raws[1], "소속", "B사")]
        _run_main(monkeypatch, facts, {"소속"})
        err = capsys.readouterr().err
        assert "Unify the spelling in sources/ and re-collect" in err
        assert "runs/*.json" in err
        assert "Unify them to one form in facts/candidates.csv" not in err
        # Superseding IS durable and still belongs on candidates.csv.
        assert "status='superseded'" in err

    def test_ordinary_conflict_keeps_the_supersede_guidance(self, monkeypatch, capsys):
        facts = [_fact("갑", "속성", "x"), _fact("갑", "속성", "y")]
        assert _run_main(monkeypatch, facts, {"속성"}) == 1
        err = capsys.readouterr().err
        assert "status='superseded'" in err
        assert "mixed Unicode normalization forms" not in err
        assert "Unify the spelling in sources/" not in err

    def test_supersede_guidance_survives_a_mixed_spelling_joining_a_real_conflict(
        self, monkeypatch, capsys
    ):
        # The contradiction (A사 vs B사) is detected with or without folding: both
        # differing rows carry the NFC subject. A third row spells the subject NFD,
        # which makes the pair "mixed" without folding having caused the conflict.
        # Superseding is still the correct repair and must still be stated.
        facts = [
            _fact(_nfc("김철수"), "소속", "A사"),
            _fact(_nfc("김철수"), "소속", "B사"),
            _fact(_nfd("김철수"), "소속", "A사"),
        ]
        assert _run_main(monkeypatch, facts, {"소속"}) == 1
        err = capsys.readouterr().err
        assert "mixed Unicode normalization forms" in err
        assert "status='superseded'" in err

    def test_mixed_and_ordinary_conflicts_get_both_guidances(self, monkeypatch, capsys):
        raws = [_nfc("김철수"), _nfd("김철수")]
        facts = [
            _fact(raws[0], "소속", "A사"),
            _fact(raws[1], "소속", "B사"),
            _fact("갑", "속성", "x"),
            _fact("갑", "속성", "y"),
        ]
        _run_main(monkeypatch, facts, {"소속", "속성"})
        err = capsys.readouterr().err
        assert "status='superseded'" in err
        assert "Unify the spelling in sources/" in err


class TestFoldingThatResolvesAConflict:
    """When folding dissolves the contradiction, disclosure is the ONLY signal.

    Object-axis folding can drop a pair before any variant map is built: the raw
    spellings would report a contradiction, the folded ones agree, and the pair
    never reaches the reporting loop. The checker then exits 0 and ``finalize``
    proceeds to compile. Nothing else in the run mentions it.

    Until #342 the compile made it concrete: ``dedup_engine_atoms`` keyed on the
    raw triple, so both spellings landed in ``accepted.dl`` as two distinct atoms
    of the same visible fact. They are one atom now, which changes what the
    advisory should SAY but not whether it is owed — the author still wrote two
    strings and the gate still treated them as one value on their behalf.

    The gate direction differs per axis: subject folding merges rows and can only
    *create* conflicts (checker stricter), object folding can only *resolve* them
    (checker more permissive). Only the second needs an advisory.
    """

    def test_object_fold_that_resolves_a_conflict_is_disclosed(self, monkeypatch, capsys):
        raws = [_nfc("한국대학교"), _nfd("한국대학교")]
        facts = [_fact("연구소", "소속", raws[0]), _fact("연구소", "소속", raws[1])]
        assert _run_main(monkeypatch, facts, {"소속"}) == 0
        out = capsys.readouterr().out
        assert "merged into one value" in out
        # The code points, not just a count: the strings render alike.
        assert ascii(raws[0]) in out and ascii(raws[1]) in out
        assert "accepted.dl" in out
        # …and what it says about accepted.dl has to match what compile does.
        # This message was written when the two spellings each became their own
        # atom; #342 collapsed them, so the old sentence would now send the
        # reader looking for a duplicate that is not there.
        assert "single facts/accepted.dl atom" in out
        assert "separate atom" not in out

    def test_collect_records_the_resolved_group_outside_conflicts(self):
        raws = [_nfc("한국대학교"), _nfd("한국대학교")]
        facts = [_fact("연구소", "소속", raws[0]), _fact("연구소", "소속", raws[1])]
        scan = check_conflicts.collect_conflicts(facts, {"소속"}, {})
        assert scan.conflicts == {}
        assert scan.object_variants[("연구소", "소속")][_nfc("한국대학교")] == sorted(raws)

    def test_scalar_only_merge_is_not_disclosed(self, monkeypatch, capsys):
        # #116 cross-notation equivalence, zero decomposed code points. Keying the
        # advisory on "the group holds several strings" would fire here — that is
        # the false diagnostic this whole disclosure must not reproduce.
        facts = [
            _fact("갑사", "매출", 'amount(5400,"억")'),
            _fact("갑사", "매출", 'amount(0.54,"조")'),
        ]
        assert _run_main(monkeypatch, facts, {"매출"}, _TYPED_AMOUNT) == 0
        captured = capsys.readouterr()
        assert captured.out == (
            "check_conflicts: 0 conflicts across 1 single-valued relation(s)\n"
        )
        assert captured.err == ""

    def test_subject_fold_alone_is_not_disclosed(self, monkeypatch, capsys):
        # Subject folding cannot resolve a contradiction, so there is nothing to
        # disclose even though the KB does mix spellings.
        facts = [
            _fact(_nfc("김철수"), "소속", "A사"),
            _fact(_nfd("김철수"), "소속", "A사"),
        ]
        assert _run_main(monkeypatch, facts, {"소속"}) == 0
        assert capsys.readouterr().out == (
            "check_conflicts: 0 conflicts across 1 single-valued relation(s)\n"
        )

    def test_disclosure_rides_alongside_a_surviving_conflict(self, monkeypatch, capsys):
        # A resolved merge on one pair and a real contradiction on another: the
        # advisory must not displace the supersede guidance, or vice versa.
        raws = [_nfc("한국대학교"), _nfd("한국대학교")]
        facts = [
            _fact("연구소", "소속", raws[0]),
            _fact("연구소", "소속", raws[1]),
            _fact("갑", "속성", "x"),
            _fact("갑", "속성", "y"),
        ]
        assert _run_main(monkeypatch, facts, {"소속", "속성"}) == 1
        captured = capsys.readouterr()
        assert "merged into one value" in captured.out
        assert "status='superseded'" in captured.err


class TestSpellingPayloadMatchesTheGate:
    """The strings listed must be exactly the ones a Unicode fold merged.

    The gate decides *whether* a merge happened; the payload says *which strings*.
    Both have to come from one computation, because a value group keys on the
    typed scalar (#116) and can therefore hold strings that are not canonically
    equivalent at all: ``amount(5400,"억")`` and ``amount(0.54,"조")`` share a
    group by parsing to 5.4e11, ``제3호`` and ``3위`` by ordinal rank. Dumping the
    whole group tells the reader those are "canonically equivalent" and asks
    them to unify two notations #116 exists to keep apart.
    """

    def test_scalar_equivalents_are_not_listed_as_spellings(self, monkeypatch, capsys):
        nfc, nfd = _nfc('amount(5400,"억")'), _nfd('amount(5400,"억")')
        facts = [
            _fact("갑사", "매출", nfc),
            _fact("갑사", "매출", nfd),
            _fact("갑사", "매출", 'amount(0.54,"조")'),
            _fact("갑사", "매출", 'amount(1,"조")'),
        ]
        assert _run_main(monkeypatch, facts, {"매출"}, _TYPED_AMOUNT) == 1
        lines = [ln for ln in capsys.readouterr().err.splitlines() if "spellings:" in ln]
        assert len(lines) == 1
        assert ascii(nfc) in lines[0] and ascii(nfd) in lines[0]
        assert ascii('amount(0.54,"조")') not in lines[0]

    def test_ordinal_rank_equivalents_are_not_listed_as_spellings(self, monkeypatch, capsys):
        nfc, nfd = _nfc("제3호"), _nfd("제3호")
        facts = [
            _fact("갑", "순위", nfc),
            _fact("갑", "순위", nfd),
            _fact("갑", "순위", "3위"),
            _fact("갑", "순위", "5위"),
        ]
        assert _run_main(monkeypatch, facts, {"순위"}, _TYPED_ORDINAL) == 1
        lines = [ln for ln in capsys.readouterr().err.splitlines() if "spellings:" in ln]
        assert len(lines) == 1
        assert ascii("3위") not in lines[0]

    def test_each_fold_class_gets_its_own_line(self, monkeypatch, capsys):
        # One value group, two independent equivalence classes — all four parse to
        # 5.4e11. "canonically equivalent" is true within each class and false
        # across them, so one line cannot carry both.
        facts = [
            _fact("갑사", "매출", _nfc('amount(5400,"억")')),
            _fact("갑사", "매출", _nfd('amount(5400,"억")')),
            _fact("갑사", "매출", _nfc('amount(0.54,"조")')),
            _fact("갑사", "매출", _nfd('amount(0.54,"조")')),
            _fact("갑사", "매출", 'amount(1,"조")'),
        ]
        assert _run_main(monkeypatch, facts, {"매출"}, _TYPED_AMOUNT) == 1
        lines = [ln for ln in capsys.readouterr().err.splitlines() if "spellings:" in ln]
        assert len(lines) == 2
        for line in lines:
            assert line.count("(NFC)") == 1 and line.count("(NFD)") == 1

    def test_the_composed_twin_is_kept(self, monkeypatch, capsys):
        # Listing only the raws where _fold(r) != r would drop the NFC spelling —
        # the one the reader must unify TO, and the one already on the CONFLICT
        # line via _representative.
        raws = [_nfc("한국대학교"), _nfd("한국대학교")]
        facts = [
            _fact("연구소", "소속", raws[0]),
            _fact("연구소", "소속", raws[1]),
            _fact("연구소", "소속", "서울대학교"),
        ]
        _run_main(monkeypatch, facts, {"소속"})
        lines = [ln for ln in capsys.readouterr().err.splitlines() if "spellings:" in ln]
        assert len(lines) == 1
        assert ascii(raws[0]) in lines[0] and ascii(raws[1]) in lines[0]

    def test_non_equivalent_raws_stay_in_the_group(self, monkeypatch, capsys):
        # Narrowing the payload must not narrow the GROUPING: 5400억 and 0.54조
        # still collapse to one value, which is what #116 is for.
        facts = [
            _fact("갑사", "매출", _nfc('amount(5400,"억")')),
            _fact("갑사", "매출", _nfd('amount(5400,"억")')),
            _fact("갑사", "매출", 'amount(0.54,"조")'),
        ]
        assert _run_main(monkeypatch, facts, {"매출"}, _TYPED_AMOUNT) == 0

    def test_exit_zero_advisory_also_excludes_scalar_equivalents(self, monkeypatch, capsys):
        # On this path the line is the ONLY signal the run emits.
        nfc, nfd = _nfc('amount(5400,"억")'), _nfd('amount(5400,"억")')
        facts = [
            _fact("갑사", "매출", nfc),
            _fact("갑사", "매출", nfd),
            _fact("갑사", "매출", 'amount(0.54,"조")'),
        ]
        assert _run_main(monkeypatch, facts, {"매출"}, _TYPED_AMOUNT) == 0
        lines = [ln for ln in capsys.readouterr().out.splitlines() if "spellings:" in ln]
        assert len(lines) == 1
        assert ascii(nfc) in lines[0] and ascii(nfd) in lines[0]
        assert ascii('amount(0.54,"조")') not in lines[0]

    def test_trailer_describes_the_carry_back_accurately(self, monkeypatch, capsys):
        # merge_candidates keys on a 4-tuple including the anchor-stripped source
        # (existing_superseded_keys), and #260 carries back whole superseded rows,
        # not a status field. The operative warning is unaffected; the
        # parenthetical was simply false.
        raws = [_nfc("김철수"), _nfd("김철수")]
        facts = [_fact(raws[0], "소속", "A사"), _fact(raws[1], "소속", "B사")]
        _run_main(monkeypatch, facts, {"소속"})
        err = capsys.readouterr().err
        # The anchor is stripped before matching (row["source"].partition("#")[0]
        # in merge_candidates), so naming a bare `source` overstates how precise
        # the key is — and SKILL.md already said it correctly.
        assert "(subject, relation, object, source-without-anchor)" in err
        assert "carries back only status" not in err


class TestFoldEnabledTypedParseIsDisclosed:
    """The other way folding resolves a contradiction, and it is not equivalence.

    ``_group_key`` folds *before* ``literal_types.normalize``, so the fold decides
    whether a typed literal parses at all. ``NFD('제3호')`` does not parse and keys
    ``("raw", …)``; folded it is ordinal rank 3 and meets ``'3위'``. The previous
    release reported that pair as a CONFLICT and this one does not — yet
    ``_fold_classes`` is silent about it, correctly: the two strings are not
    canonically equivalent, so the equivalence axis has nothing to say. Keying the
    whole exit-0 disclosure on that axis therefore left the merge with **no**
    signal at all: exit 0, empty stderr, and ``finalize`` reporting "done — no
    contradictions" where it used to delete ``facts/accepted.dl``.

    The engine makes it worse, not better: ``common._project_typed_relations``
    hands ``normalize`` the object as written, so it cannot reproduce the merge
    and loads both literals untyped. The checker is entitled to be more willing to
    merge than the engine only while it says so.

    #342 does not touch this. It folds engine-atom identity on canonical
    equivalence, and ``NFD('제3호')`` and ``'3위'`` are not canonically equivalent —
    two atoms before, two atoms after. Only the wording moved: the message names
    the *typed projection* as the half that does not fold, because engine atoms
    now do.
    """

    def test_nfd_typed_literal_merged_with_its_notation_twin_is_disclosed(
        self, monkeypatch, capsys
    ):
        nfd_ordinal = _nfd("제3호")
        facts = [_fact("갑", "순위", nfd_ordinal), _fact("갑", "순위", "3위")]
        assert _run_main(monkeypatch, facts, {"순위"}, _TYPED_ORDINAL) == 0
        out = capsys.readouterr().out
        assert "merged only because a Unicode fold made a typed literal parse" in out
        assert ascii(nfd_ordinal) in out and ascii("3위") in out

    def test_disclosure_names_the_engine_divergence(self, monkeypatch, capsys):
        # The actionable half: the reader must not conclude the engine agrees.
        # Named as the TYPED PROJECTION since #342: engine atoms do fold now, so
        # a flat "the engine does not fold" would be false in general — and the
        # divergence this message is about survives it, because a parse merge is
        # not canonical equivalence and the atom fold never reaches it.
        facts = [_fact("갑", "순위", _nfd("제3호")), _fact("갑", "순위", "3위")]
        _run_main(monkeypatch, facts, {"순위"}, _TYPED_ORDINAL)
        out = capsys.readouterr().out
        assert "typed projection does not fold" in out
        assert "they stay two separate atoms" in out
        assert "Unify the spelling in sources/" in out

    def test_engine_claim_is_narrowed_to_the_decomposed_literal(self, monkeypatch, capsys):
        # `_project_typed_relations` runs per row: with an NFC relation name the
        # spec lookup hits and the COMPOSED literal is inserted typed (pinned in
        # tests/unit/test_typed_projection_fake.py). An unconditional "loads every
        # one of them untyped" is therefore false on this KB — exactly the shape
        # the message is printed for. The conclusion is unchanged; the mechanism
        # sentence has to be true as well.
        facts = [_fact("갑", "순위", _nfd("제3호")), _fact("갑", "순위", "3위")]
        _run_main(monkeypatch, facts, {"순위"}, _TYPED_ORDINAL)
        out = capsys.readouterr().out
        assert "loads the decomposed literal untyped" in out
        assert "when the relation name is decomposed too, every one of them" in out
        assert "loads every one of them untyped" not in out

    def test_merged_notations_are_not_called_canonically_equivalent(self, monkeypatch, capsys):
        # They are not, and the equivalence message asks for the wrong repair.
        facts = [_fact("갑", "순위", _nfd("제3호")), _fact("갑", "순위", "3위")]
        _run_main(monkeypatch, facts, {"순위"}, _TYPED_ORDINAL)
        out = capsys.readouterr().out
        assert "merged into one value" not in out
        assert "NOT canonically equivalent" in out

    def test_all_nfd_ordinal_kb_is_disclosed(self, monkeypatch, capsys):
        # The most likely shape of all: normalization is a property of the source
        # document, so a whole KB arrives decomposed. `_ORDINAL_KO_RE` spells its
        # markers composed (제/호/위), so neither NFD literal parses as written,
        # both parse folded, and they collapse onto rank 3 — a merge only this
        # module makes, hence a disclosure.
        raws = [_nfd("제3호"), _nfd("3위")]
        facts = [_fact("갑", "순위", raws[0]), _fact("갑", "순위", raws[1])]
        assert _run_main(monkeypatch, facts, {"순위"}, _TYPED_ORDINAL) == 0
        out = capsys.readouterr().out
        assert "made a typed literal parse" in out
        assert ascii(raws[0]) in out and ascii(raws[1]) in out

    def test_one_representative_per_component_not_every_member(self):
        # MUTATION PIN (passes on this branch before and after; it exists because
        # replacing `_representative(members)` with every member left the whole
        # suite green). `_parse_merge` reports ONE representative per component:
        # the members
        # inside a component are canonically equivalent, so `_fold_classes`
        # already lists them and repeating them here says "the fold enabled a
        # parse" about a plain Unicode merge. NFD('제3호') is its own component;
        # NFD('3위') and NFC('3위') are one, and only its composed spelling is
        # named. Listing all members gives three notations instead of two, which
        # the whole test suite otherwise accepts.
        raws = [_nfd("제3호"), _nfd("3위"), _nfc("3위")]
        facts = [_fact("갑", "순위", raw) for raw in raws]
        scan = check_conflicts.collect_conflicts(facts, {"순위"}, _TYPED_ORDINAL)
        assert scan.conflicts == {}
        notations = scan.parse_merges[("갑", "순위")]["3위"]
        assert notations == sorted([_nfd("제3호"), _nfc("3위")])
        assert _nfd("3위") not in notations

    def test_parse_merge_is_disclosed_when_a_contradiction_also_remains(
        self, monkeypatch, capsys
    ):
        # A pair can merge two notations under the fold AND still contradict on a
        # third value. Keying the disclosure on the conflict-free pairs skipped
        # exactly this shape: the CONFLICT line names the survivors, the
        # per-object spelling lines under it come from `_fold_classes` (silent
        # about a merge that is not canonical equivalence), and NFD('제3호')
        # therefore appeared nowhere in the output in any form — a row the
        # previous release listed by name. The reader supersedes what is on
        # screen and never learns a third row is behind it.
        nfd_ordinal = _nfd("제3호")
        facts = [
            _fact("Acme", "순위", nfd_ordinal),
            _fact("Acme", "순위", "3위"),
            _fact("Acme", "순위", "5위"),
        ]
        assert _run_main(monkeypatch, facts, {"순위"}, _TYPED_ORDINAL) == 1
        captured = capsys.readouterr()
        assert "has 2 values: 3위, 5위" in captured.err
        assert "made a typed literal parse" in captured.out
        assert ascii(nfd_ordinal) in captured.out

    def test_parse_merge_is_recorded_on_a_still_conflicting_pair(self):
        # The scan half of the above: `collect_conflicts` only filled
        # `parse_merges` in its single-group branch, so the report had nothing to
        # print even after it started looking.
        facts = [
            _fact("Acme", "순위", _nfd("제3호")),
            _fact("Acme", "순위", "3위"),
            _fact("Acme", "순위", "5위"),
        ]
        scan = check_conflicts.collect_conflicts(facts, {"순위"}, _TYPED_ORDINAL)
        assert list(scan.conflicts) == [("Acme", "순위")]
        assert scan.parse_merges[("Acme", "순위")] == {
            "3위": sorted([_nfd("제3호"), "3위"])
        }

    def test_the_headline_does_not_claim_the_pair_is_contradiction_free(
        self, monkeypatch, capsys
    ):
        # The exit-0 wording ("so no contradiction is reported for them") is false
        # on this path — one IS reported, just not between these two notations.
        #
        # The headline must also make no CLAIM ABOUT POSITION. This advisory goes
        # to stdout and the CONFLICT lines to stderr, and this function is called
        # BEFORE the loop that prints them, so "any CONFLICT above" was false in
        # program order and unverifiable in any order once the streams are
        # separated or redirected apart. Asserting on stderr as well, because a
        # stdout-only assertion cannot see a claim made about a stderr line —
        # which is exactly how the directional wording survived.
        facts = [
            _fact("Acme", "순위", _nfd("제3호")),
            _fact("Acme", "순위", "3위"),
            _fact("Acme", "순위", "5위"),
        ]
        _run_main(monkeypatch, facts, {"순위"}, _TYPED_ORDINAL)
        captured = capsys.readouterr()
        out, err = captured.out, captured.err
        assert "counted as one value" in out
        assert "no contradiction is reported for them" not in out
        # The CONFLICT line this advisory would have been pointing at is on the
        # other stream, and is emitted after this text.
        assert "CONFLICT: single-valued" in err
        assert "CONFLICT" not in out
        headline = next(li for li in out.splitlines() if "merged only because" in li)
        assert "above" not in headline and "below:" not in headline

    def test_all_nfd_amount_kb_needs_no_disclosure(self, monkeypatch, capsys):
        # The same shape on the `amount` axis is NOT disclosed, and must not be:
        # `parse_amount` composes its unit lookup key, so the engine parses these
        # rows exactly as this module does (pinned above and in
        # tests/unit/test_amount_units_unicode.py). Disclosing here would tell the
        # reader to go unify a spelling that changes nothing.
        raws = [_nfd('amount(5400,"억")'), _nfd('amount(0.54,"조")')]
        facts = [_fact("갑사", "매출", raws[0]), _fact("갑사", "매출", raws[1])]
        assert _run_main(monkeypatch, facts, {"매출"}, _TYPED_AMOUNT) == 0
        assert capsys.readouterr().out == (
            "check_conflicts: 0 conflicts across 1 single-valued relation(s)\n"
        )

    def test_scalar_merge_the_fold_did_not_cause_stays_silent(self, monkeypatch, capsys):
        # CONTROL for the gate's lower edge, and the pin the previous round's
        # `len(raws) > 1` mutant survived. #116 cross-notation equivalence, zero
        # decomposed code points: both literals key to 5.4e11 with or without the
        # fold, so folding merged nothing and there is nothing to disclose.
        facts = [
            _fact("갑사", "매출", 'amount(5400,"억")'),
            _fact("갑사", "매출", 'amount(0.54,"조")'),
        ]
        scan = check_conflicts.collect_conflicts(facts, {"매출"}, _TYPED_AMOUNT)
        assert scan.object_variants == {} and scan.parse_merges == {}
        assert _run_main(monkeypatch, facts, {"매출"}, _TYPED_AMOUNT) == 0
        assert capsys.readouterr().out == (
            "check_conflicts: 0 conflicts across 1 single-valued relation(s)\n"
        )

    def test_merge_canonical_equivalence_already_explains_is_not_repeated(
        self, monkeypatch, capsys
    ):
        # 제3호 in both forms plus 3위. Unfolded, NFC('제3호') and '3위' already
        # share rank 3 (#218), and NFD('제3호') joins by canonical equivalence —
        # which `_fold_classes` reports. Nothing is left for the parse message, so
        # announcing one here would tell the reader that #218's rank equivalence
        # is a Unicode artifact.
        facts = [
            _fact("갑", "순위", _nfc("제3호")),
            _fact("갑", "순위", _nfd("제3호")),
            _fact("갑", "순위", "3위"),
        ]
        scan = check_conflicts.collect_conflicts(facts, {"순위"}, _TYPED_ORDINAL)
        assert scan.parse_merges == {}
        assert _run_main(monkeypatch, facts, {"순위"}, _TYPED_ORDINAL) == 0
        out = capsys.readouterr().out
        assert "merged into one value" in out
        assert "made a typed literal parse" not in out

    def test_untyped_fold_alone_raises_no_parse_message(self, monkeypatch, capsys):
        # CONTROL: the equivalence path must keep its own message and only that.
        raws = [_nfc("한국대학교"), _nfd("한국대학교")]
        facts = [_fact("연구소", "소속", raws[0]), _fact("연구소", "소속", raws[1])]
        assert _run_main(monkeypatch, facts, {"소속"}) == 0
        out = capsys.readouterr().out
        assert "merged into one value" in out
        assert "made a typed literal parse" not in out

    def test_reported_notations_are_row_order_independent(self):
        base = [
            _fact("갑", "순위", _nfd("제3호")),
            _fact("갑", "순위", "3위"),
            _fact("갑", "순위", _nfc("제3호")),
        ]
        seen = {
            tuple(sorted(check_conflicts.collect_conflicts(list(perm), {"순위"}, _TYPED_ORDINAL).parse_merges.items()))
            for perm in itertools.permutations(base)
        }
        assert len(seen) == 1


class TestSplitRelationIsDisclosedAtExitZero:
    """Rows that pass membership and then vanish on the relation axis.

    Membership folds, grouping does not, so a KB whose rows flipped NFC↔NFD as a
    whole — subject and relation together, the realistic shape — enters the
    grouping loop and then splits into two singleton pairs. The checker looked at
    two rows that contradict each other and printed "0 conflicts". Deferring the
    grouping decision is the #210 maintainer call; deferring the *disclosure* has
    no such justification, because at exit 0 there is no CONFLICT line to hang
    the existing "(relation written in N mixed …)" suffix on.
    """

    def test_whole_row_flipped_is_disclosed(self, monkeypatch, capsys):
        rel = [_nfc("소속"), _nfd("소속")]
        facts = [
            _fact(_nfc("김철수"), rel[0], "A사"),
            _fact(_nfd("김철수"), rel[1], "B사"),
        ]
        assert _run_main(monkeypatch, facts, {_nfc("소속")}) == 0
        out = capsys.readouterr().out
        assert "(subject, relation) pair(s)" in out
        assert ascii(rel[0]) in out and ascii(rel[1]) in out
        assert "never compared against" in out

    def test_split_relation_names_the_subject_as_written(self, monkeypatch, capsys):
        facts = [
            _fact(_nfc("김철수"), _nfc("소속"), "A사"),
            _fact(_nfd("김철수"), _nfd("소속"), "B사"),
        ]
        _run_main(monkeypatch, facts, {_nfc("소속")})
        assert f"on '{_nfc('김철수')}'" in capsys.readouterr().out

    def test_conflicting_pair_is_not_disclosed_twice(self, monkeypatch, capsys):
        # CONTROL (passes before this change too): the CONFLICT line already
        # carries the suffix and the spelling list on stderr, and repeating it on
        # stdout would report one fact in two streams.
        facts = [
            _fact("김철수", _nfc("소속"), "A사"),
            _fact("김철수", _nfc("소속"), "B사"),
            _fact("김철수", _nfd("소속"), "A사"),
            _fact("김철수", _nfd("소속"), "B사"),
        ]
        assert _run_main(monkeypatch, facts, {_nfc("소속")}) == 1
        captured = capsys.readouterr()
        assert "(subject, relation) pair(s)" not in captured.out
        assert "(relation written in 2 mixed Unicode normalization forms)" in captured.err

    def test_non_conflicting_spelling_still_reaches_the_conflict_line(self, monkeypatch, capsys):
        # One spelling conflicts on its own, the other holds a single value. The
        # hidden row is invisible to that conflict either way, so the suffix must
        # count spellings over every pair examined, not only conflicting ones.
        facts = [
            _fact("김철수", _nfc("소속"), "A사"),
            _fact("김철수", _nfc("소속"), "B사"),
            _fact("김철수", _nfd("소속"), "C사"),
        ]
        assert _run_main(monkeypatch, facts, {_nfc("소속")}) == 1
        captured = capsys.readouterr()
        assert "(relation written in 2 mixed Unicode normalization forms)" in captured.err
        assert "(subject, relation) pair(s)" not in captured.out

    def test_single_relation_spelling_says_nothing(self, monkeypatch, capsys):
        # CONTROL: this is what keeps an NFC-only KB byte-identical.
        facts = [_fact("김철수", "소속", "A사"), _fact("김철수", "직급", "부장")]
        assert _run_main(monkeypatch, facts, {"소속", "직급"}) == 0
        assert capsys.readouterr().out == (
            "check_conflicts: 0 conflicts across 2 single-valued relation(s)\n"
        )

    def test_two_subjects_are_not_conflated(self, monkeypatch, capsys):
        # CONTROL: the channel is keyed per subject, so one subject's mixed
        # spelling must not implicate another's.
        facts = [
            _fact("김철수", _nfc("소속"), "A사"),
            _fact("김철수", _nfd("소속"), "B사"),
            _fact("박영희", _nfc("소속"), "C사"),
        ]
        _run_main(monkeypatch, facts, {_nfc("소속")})
        out = capsys.readouterr().out
        assert "1 (subject, relation) pair(s)" in out
        assert "박영희" not in out

    def test_header_counts_pairs_not_relations(self, monkeypatch, capsys):
        # One subject, two split relations. Naming the SUBJECT axis miscounts here
        # — "2 subject(s)" reads as two people when there is one.
        facts = [
            _fact("김철수", _nfc("소속"), "A사"),
            _fact("김철수", _nfd("소속"), "B사"),
            _fact("김철수", _nfc("직급"), "부장"),
            _fact("김철수", _nfd("직급"), "과장"),
        ]
        assert _run_main(monkeypatch, facts, {_nfc("소속"), _nfc("직급")}) == 0
        out = capsys.readouterr().out
        assert "2 (subject, relation) pair(s)" in out
        assert "subject(s)" not in out
        assert len([ln for ln in out.splitlines() if ln.startswith("    on ")]) == 2

    def test_header_counts_pairs_not_subjects(self, monkeypatch, capsys):
        # The mirror image: two subjects, one split relation each. Naming the
        # RELATION axis miscounts here — "2 relation(s) … for one subject" reads
        # as one person with two relations when there are two people with one.
        # Both pins are needed because the header has been wrong in each
        # direction in turn; the count is over pairs and names neither axis.
        facts = [
            _fact("김철수", _nfc("소속"), "A사"),
            _fact("김철수", _nfd("소속"), "B사"),
            _fact("박영희", _nfc("소속"), "C사"),
            _fact("박영희", _nfd("소속"), "D사"),
        ]
        assert _run_main(monkeypatch, facts, {_nfc("소속")}) == 0
        out = capsys.readouterr().out
        assert "2 (subject, relation) pair(s)" in out
        assert "for one subject" not in out
        assert f"on '{_nfc('김철수')}'" in out and f"on '{_nfc('박영희')}'" in out
        assert len([ln for ln in out.splitlines() if ln.startswith("    on ")]) == 2


class TestRelationSpellingIsDisclosed:
    """One contradiction reported as N lines must say why there are N.

    Membership is folded but grouping is not (the #210 call is deferred), so a
    relation spelled two ways yields two groups and two CONFLICT lines that are
    byte-different and visually identical. Collapsing them would mean folding the
    grouping key — the maintainer decision this PR deliberately leaves open — so
    the fix here is disclosure, matching what the subject axis already does.

    The policy file does not drive the count: ``sv`` is a set of folded names, so
    two policy spellings collapse to one element. The count comes purely from
    distinct raw relation spellings among the rows.
    """

    def test_mixed_relation_spellings_are_named_in_the_conflict_line(self, monkeypatch, capsys):
        facts = [
            _fact("김철수", _nfc("소속"), "A사"),
            _fact("김철수", _nfc("소속"), "B사"),
            _fact("김철수", _nfd("소속"), "A사"),
            _fact("김철수", _nfd("소속"), "B사"),
        ]
        assert _run_main(monkeypatch, facts, {_nfc("소속")}) == 1
        err = capsys.readouterr().err
        assert "(relation written in 2 mixed Unicode normalization forms)" in err

    def test_mixed_relation_spellings_are_printed_escaped(self, monkeypatch, capsys):
        facts = [
            _fact("김철수", _nfc("소속"), "A사"),
            _fact("김철수", _nfc("소속"), "B사"),
            _fact("김철수", _nfd("소속"), "A사"),
            _fact("김철수", _nfd("소속"), "B사"),
        ]
        _run_main(monkeypatch, facts, {_nfc("소속")})
        err = capsys.readouterr().err
        assert (
            f"    relation spellings: {ascii(_nfd('소속'))} (NFD), "
            f"{ascii(_nfc('소속'))} (NFC)\n"
        ) in err

    def test_mixed_relation_gets_the_unify_guidance(self, monkeypatch, capsys):
        facts = [
            _fact("김철수", _nfc("소속"), "A사"),
            _fact("김철수", _nfc("소속"), "B사"),
            _fact("김철수", _nfd("소속"), "A사"),
            _fact("김철수", _nfd("소속"), "B사"),
        ]
        _run_main(monkeypatch, facts, {_nfc("소속")})
        assert "Unify the spelling in sources/" in capsys.readouterr().err

    def test_single_relation_spelling_says_nothing(self, monkeypatch, capsys):
        # No disclosure when there is nothing to disclose — this is what keeps an
        # NFC-only KB byte-identical.
        facts = [_fact("김철수", "소속", "A사"), _fact("김철수", "소속", "B사")]
        _run_main(monkeypatch, facts, {"소속"})
        err = capsys.readouterr().err
        assert "relation written in" not in err
        assert "relation spellings:" not in err

    def test_distinct_relations_are_not_conflated(self, monkeypatch, capsys):
        # Two genuinely different relations on one subject are not "spellings".
        facts = [
            _fact("김철수", "소속", "A사"),
            _fact("김철수", "소속", "B사"),
            _fact("김철수", "직급", "부장"),
            _fact("김철수", "직급", "과장"),
        ]
        _run_main(monkeypatch, facts, {"소속", "직급"})
        assert "relation written in" not in capsys.readouterr().err


class TestNfcOnlyKbByteIdentical:
    """The invariant the issue asks for: an NFC-only KB reports exactly as before."""

    def test_report_lines_unchanged_for_nfc_only_kb(self, monkeypatch, capsys):
        # The 매출 rows carry a #116 cross-notation merge that has nothing to do
        # with Unicode: 5400억 and 0.54조 are both plain NFC and both parse to
        # 5.4e11, so they land in ONE object group holding TWO raw spellings.
        # That is the input the merge disclosure must stay silent on. Without the
        # third row every object group is a singleton, the disclosure branch is
        # unreachable, and this pin cannot fail in either direction.
        facts = [
            _fact("갑사", "매출", 'amount(5400,"억")'),
            _fact("갑사", "매출", 'amount(0.54,"조")'),
            _fact("갑사", "매출", 'amount(1,"조")'),
            _fact("김철수", "소속", "A사"),
            _fact("김철수", "소속", "B사"),
        ]
        assert _run_main(monkeypatch, facts, {"매출", "소속"}, _TYPED_AMOUNT) == 1
        assert capsys.readouterr().err == (
            "check_conflicts: 2 conflict(s) found\n"
            "  CONFLICT: single-valued '매출' on '갑사' has 2 values: "
            'amount(0.54,"조"), amount(1,"조")\n'
            "  CONFLICT: single-valued '소속' on '김철수' has 2 values: A사, B사\n"
            "  Resolve by marking the outdated row(s) status='superseded' in "
            "facts/candidates.csv, then re-run.\n"
        )

    def test_report_lines_unchanged_for_nfc_only_kb_with_aliases(self, monkeypatch, capsys):
        # The alias path prints an extra suffix and re-reads relation_aliases();
        # pin it too, so the folding change cannot perturb the canonical-name
        # report of a KB that has a relation-aliases.md file.
        aliases = {"게재연도": "published_year", "발행년도": "published_year"}
        facts = [
            _fact("논문A", "게재연도", "2005"),
            _fact("논문A", "발행년도", "2007"),
            _fact("논문B", "소속", "A사"),
            _fact("논문B", "소속", "B사"),
        ]
        rc = _run_main(monkeypatch, facts, {"published_year", "소속"}, None, aliases)
        assert rc == 1
        assert capsys.readouterr().err == (
            "check_conflicts: 2 conflict(s) found\n"
            "  CONFLICT: single-valued 'published_year' (canonical; incl. surface variants) "
            "on '논문A' has 2 values: 2005, 2007\n"
            "  CONFLICT: single-valued '소속' on '논문B' has 2 values: A사, B사\n"
            "  Resolve by marking the outdated row(s) status='superseded' in "
            "facts/candidates.csv, then re-run.\n"
        )

    def test_no_conflict_path_unchanged(self, monkeypatch, capsys):
        facts = [_fact("김철수", "소속", "A사")]
        assert _run_main(monkeypatch, facts, {"소속"}) == 0
        assert capsys.readouterr().out == (
            "check_conflicts: 0 conflicts across 1 single-valued relation(s)\n"
        )

    def test_no_single_valued_relations_early_return_unchanged(self, monkeypatch, capsys):
        # Early return before collect_conflicts is ever called; pinned so the new
        # three-value return cannot leak into this path.
        facts = [_fact("김철수", "소속", "A사"), _fact("김철수", "소속", "B사")]
        assert _run_main(monkeypatch, facts, set()) == 0
        captured = capsys.readouterr()
        assert captured.out == (
            "check_conflicts: no single-valued relations declared "
            "(policy/single-valued.md); nothing to check\n"
        )
        assert captured.err == ""
