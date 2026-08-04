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
        # so #116's cross-notation equivalence never fired there; now it does.
        facts = [
            _fact("갑사", "매출", _nfd('amount(5400,"억")')),
            _fact("갑사", "매출", _nfd('amount(0.54,"조")')),
        ]
        assert check_conflicts.detect_conflicts(facts, {"매출"}, _TYPED_AMOUNT) == {}

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
        conflicts, subjects, objects = check_conflicts.collect_conflicts(facts, {"소속"}, {})
        key = (_nfc("김철수"), "소속")
        assert conflicts == {key: ["A사", "B사"]}
        assert subjects[key] == sorted(raws)
        assert objects[key] == {"A사": ["A사"], "B사": ["B사"]}

    def test_object_channel_lists_the_merged_spellings(self):
        raws = [_nfc("한국대학교"), _nfd("한국대학교")]
        facts = [
            _fact("연구소", "소속", raws[0]),
            _fact("연구소", "소속", raws[1]),
            _fact("연구소", "소속", "서울대학교"),
        ]
        _, _, objects = check_conflicts.collect_conflicts(facts, {"소속"}, {})
        merged = objects[("연구소", "소속")]
        assert merged[_nfc("한국대학교")] == sorted(raws)
        assert merged["서울대학교"] == ["서울대학교"]

    def test_single_spelling_reports_one_variant_on_both_axes(self):
        facts = [_fact("갑", "속성", "x"), _fact("갑", "속성", "y")]
        _, subjects, objects = check_conflicts.collect_conflicts(facts, {"속성"}, {})
        assert subjects[("갑", "속성")] == ["갑"]
        assert objects[("갑", "속성")] == {"x": ["x"], "y": ["y"]}

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
        conflicts, subjects, _ = check_conflicts.collect_conflicts(facts, {"소속", "직급"}, {})
        assert subjects[(_nfc("김철수"), "소속")] == sorted(raws)
        assert subjects[(_nfc("김철수"), "직급")] == [_nfc("김철수")]
        assert conflicts[(_nfc("김철수"), "직급")] == ["과장", "부장"]


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
        assert f"    subject spellings: {ascii(raws[1])}, {ascii(raws[0])}\n" in err

    def test_mixed_object_spellings_are_printed_escaped(self, monkeypatch, capsys):
        raws = [_nfc("한국대학교"), _nfd("한국대학교")]
        facts = [
            _fact("연구소", "소속", raws[0]),
            _fact("연구소", "소속", raws[1]),
            _fact("연구소", "소속", "서울대학교"),
        ]
        _run_main(monkeypatch, facts, {"소속"})
        err = capsys.readouterr().err
        assert f"    value {raws[0]!r} spellings: {ascii(raws[1])}, {ascii(raws[0])}\n" in err

    def test_mixed_forms_get_the_unify_guidance_on_top(self, monkeypatch, capsys):
        raws = [_nfc("김철수"), _nfd("김철수")]
        facts = [_fact(raws[0], "소속", "A사"), _fact(raws[1], "소속", "B사")]
        _run_main(monkeypatch, facts, {"소속"})
        err = capsys.readouterr().err
        assert "Unify them to one form" in err
        assert "status='superseded'" in err

    def test_ordinary_conflict_keeps_the_supersede_guidance(self, monkeypatch, capsys):
        facts = [_fact("갑", "속성", "x"), _fact("갑", "속성", "y")]
        assert _run_main(monkeypatch, facts, {"속성"}) == 1
        err = capsys.readouterr().err
        assert "status='superseded'" in err
        assert "mixed Unicode normalization forms" not in err
        assert "Unify them to one form" not in err

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
        assert "Unify them to one form" in err


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
