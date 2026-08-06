# SPDX-License-Identifier: Apache-2.0
"""The conditional guidance a conflict on a non-ASCII-digit value carries (#331).

``check_conflicts`` resolves conflicts by telling the user to mark the outdated
row ``status='superseded'``. Under a relation declared **typed**, that advice
assumes one of the two values is out of date. A value carrying non-ASCII digits
does not parse as the declared type at all, so it degrades to a raw-string key —
and following the generic advice on the *ASCII* row clears the gate while leaving
the KB holding the value the engine cannot read (measured: ``check_conflicts``
then reports ``0 conflicts`` and exits 0).

**The note is gated on the relation actually having a typed spec**, and that gate
is load-bearing rather than defensive. Under an UNTYPED single-valued relation
``_group_key`` returns a raw key because ``spec is None``, not because of digit
width: ``GPT-４`` and its ASCII twin ``GPT-5`` key identically, ``GPT-４`` is a
perfectly usable ``relation/3`` fact, and superseding the outdated row is exactly
the right fix. Every clause of the note would be false there, so it must not fire.

What these tests hold down is the *shape* of the note, not its prose:

* it fires only for a typed relation, and only when a value actually carries
  non-ASCII digits (the negative controls are what make that real — a note
  printed unconditionally would satisfy the positive cases alone);
* it names the offending characters as escapes, because ``repr('１００억')`` is
  ``'１００억'`` — visually identical to ``'100억'`` in most fonts;
* it does NOT claim supersession cannot resolve the conflict. Superseding the
  full-width row *does* resolve it. Nor does it claim re-collection *replaces*
  supersession: for genuinely different values (``100억`` vs ``２００억``)
  correcting the source leaves ``100억`` vs ``200억``, still a conflict that
  supersession must settle.
"""
from __future__ import annotations

import unicodedata

import check_conflicts
import common
import literal_types

# A typed single-valued amount relation — the only shape the note applies to.
_AMOUNT_SPEC = common.TypedRelSpec("amount", "revenue")


def _nfd(value: str) -> str:
    return unicodedata.normalize("NFD", value)


class TestUntypedRelationsNeverGetTheNote:
    """The blocker case. Under an untyped relation every clause of the note is
    false, so the spec gate — not the digit predicate — has to decide here."""

    def test_untyped_relation_with_a_full_width_value_gets_no_note(self):
        # Real shape: single-valued `모델`, no typed declaration, an old `GPT-４`
        # superseded by `GPT-5`. Supersession IS the right fix, and the raw key
        # comes from `spec is None`, not from digit width.
        assert check_conflicts.non_ascii_digit_note(["GPT-5", "GPT-４"], None) is None

    def test_untyped_relation_with_a_bare_full_width_number_gets_no_note(self):
        assert check_conflicts.non_ascii_digit_note(["100억", "１００억"], None) is None

    def test_spec_gate_beats_the_digit_predicate(self):
        # The value really does carry non-ASCII digits; the spec gate still wins.
        assert literal_types.has_non_ascii_digits("１００억") is True
        assert check_conflicts.non_ascii_digit_note(["１００억", "２００억"], None) is None

    def test_untyped_ascii_twin_keys_the_same_way(self):
        # Why the note would be false: digit width changes nothing about the key
        # when there is no spec, so "compared here as a raw string" would not be
        # attributable to the digits at all.
        assert check_conflicts._group_key("GPT-４", None) == ("raw", "GPT-４")
        assert check_conflicts._group_key("GPT-5", None) == ("raw", "GPT-5")


class TestAsciiCleanGroupsGetNoNote:
    """Negative controls. Without these the positive cases below would pass just
    as well against a note that was printed for every conflict."""

    def test_two_ascii_amounts_get_no_note(self):
        assert check_conflicts.non_ascii_digit_note(["100억", "200억"], _AMOUNT_SPEC) is None

    def test_unparseable_ascii_strings_get_no_note(self):
        # Unparseable is not the trigger — non-ASCII digits are. Both of these
        # degrade to raw keys under a typed spec exactly like a full-width value.
        assert check_conflicts.non_ascii_digit_note(["n/a", "unknown"], _AMOUNT_SPEC) is None

    def test_empty_group_gets_no_note(self):
        assert check_conflicts.non_ascii_digit_note([], _AMOUNT_SPEC) is None

    def test_non_ascii_digits_in_the_UNIT_NAME_get_no_note(self):
        # The unit group is deliberately outside the digit policy, and
        # _parse_amount_units does not validate the unit NAME, so a declared unit
        # may carry non-ASCII digits. Such a value parses to a scalar and
        # _group_key keys it ("scalar", …) — every clause of the note ("does not
        # parse", "compared here as a raw string") would be false. Carrying
        # non-ASCII digits is not the same as failing to parse.
        spec = common.TypedRelSpec("amount", "revenue", {"억１": 100000000})
        objects = ['amount(100,"억１")', 'amount(200,"억１")']
        assert all(literal_types.has_non_ascii_digits(o) for o in objects)
        assert literal_types.normalize(spec.type, objects[0], spec.units) == 10000000000
        assert check_conflicts._group_key(objects[0], spec) == ("scalar", 10000000000)
        assert check_conflicts.non_ascii_digit_note(objects, spec) is None

    def test_only_the_values_that_really_fail_to_parse_are_named(self):
        # Mixed group: the unit-name value parses, the numeral one does not. The
        # note must name the second only — naming both would restate the false
        # claim about the first.
        spec = common.TypedRelSpec("amount", "revenue", {"억１": 100000000})
        note = "\n".join(check_conflicts.non_ascii_digit_note(['amount(100,"억１")', "２００억"], spec))
        assert "\\uff12\\uff10\\uff10억" in note
        assert "억\\uff11" not in note


class TestTypedNonAsciiDigitGroupsGetTheNote:
    def test_note_names_the_offender_as_escapes(self):
        note = "\n".join(check_conflicts.non_ascii_digit_note(["100억", "１００억"], _AMOUNT_SPEC))
        # The escaped codepoints, NOT the raw glyph: a reader must be able to see
        # WHICH characters are wrong.
        assert "\\uff11\\uff10\\uff10억" in note
        assert "'１００억'" not in note

    def test_note_states_the_cause_and_the_fix(self):
        note = "\n".join(check_conflicts.non_ascii_digit_note(["100억", "１００억"], _AMOUNT_SPEC))
        assert "does not parse" in note
        assert "re-collect" in note
        assert "docs/reference/typed-relations.md" in note

    def test_note_does_not_claim_supersede_cannot_work(self):
        # Superseding the full-width row DOES resolve the conflict. The note must
        # hedge ("can leave") rather than assert supersession is useless, or the
        # gate would be printing something false at the moment it fails.
        note = "\n".join(check_conflicts.non_ascii_digit_note(["100억", "１００억"], _AMOUNT_SPEC))
        assert "can leave" in note
        assert "cannot" not in note

    def test_note_does_not_claim_re_collection_replaces_supersession(self):
        # For 100억 vs ２００억 the values genuinely differ: correcting the source
        # yields 100억 vs 200억, still a conflict that supersession must settle.
        # So the guidance must not say "re-collect INSTEAD of superseding", and it
        # must still name supersession as the follow-up.
        note = "\n".join(check_conflicts.non_ascii_digit_note(["100억", "２００억"], _AMOUNT_SPEC))
        assert "instead" not in note.lower()
        assert "supersede" in note.lower()

    def test_only_the_offending_value_is_named(self):
        note = "\n".join(check_conflicts.non_ascii_digit_note(["100억", "１００억"], _AMOUNT_SPEC))
        assert "'100억'" not in note

    def test_fires_for_digit_systems_other_than_full_width(self):
        note = check_conflicts.non_ascii_digit_note(["100", "١٠٠"], _AMOUNT_SPEC)
        assert note is not None
        assert "\\u0661\\u0660\\u0660" in "\n".join(note)

    def test_fires_for_devanagari(self):
        note = check_conflicts.non_ascii_digit_note(["123", "१२३"], _AMOUNT_SPEC)
        assert note is not None
        assert "\\u0967\\u0968\\u0969" in "\n".join(note)

    def test_every_offender_is_named(self):
        note = "\n".join(check_conflicts.non_ascii_digit_note(["１００억", "２００억"], _AMOUNT_SPEC))
        assert "\\uff11\\uff10\\uff10억" in note
        assert "\\uff12\\uff10\\uff10억" in note

    def test_returns_lines_without_trailing_newlines(self):
        # main() prints these one per call; embedded newlines would double-space.
        for line in check_conflicts.non_ascii_digit_note(["100억", "１００억"], _AMOUNT_SPEC):
            assert "\n" not in line


class TestUnitNameFoldDoesNotDisturbTheDigitPolicy:
    """#325 folds unit names; #331/#336 rejects non-ASCII digits in values.

    Both touch ``parse_amount``, so the two policies have to be shown not to
    interfere. They cannot, for a reason worth stating once: the fold is **NFC**,
    and NFC leaves every non-ASCII digit alone — only NFKC maps fullwidth ``１``
    U+FF11 onto ``1``. The fold also runs strictly after the numeric group has
    matched ``[0-9]``, so it is downstream of the digit gate in the direction
    that matters.

    **Every test in this class is a CONTROL**: each one passes both with and
    without the #325 fold, verified by reverting both fold sites on the merged
    tree (24/24 still green). That is the point — the claim being held down is
    "#325 changed nothing here", and a test that only passed after the fold would
    be evidence of exactly the interference this class denies. The pins that do
    fail pre-fix live in tests/unit/test_amount_units_unicode.py.
    """

    def test_nfc_never_changes_a_non_ascii_digit(self):
        # The property the safety argument rests on. If NFC ever folded a digit
        # system onto ASCII, the fold would silently reopen the digit policy.
        for digits in ("１２３", "١٢٣", "१२३", "๑๒๓"):
            assert unicodedata.normalize("NFC", digits) == digits

    def test_full_width_value_still_rejected_under_a_folded_table(self):
        # #336's policy, unchanged: the VALUE's digits are still the gate, and a
        # units table that went through the fold does not rescue them.
        units = common._parse_amount_units("억=1e8")
        assert literal_types.parse_amount("１００억", units) is None
        assert literal_types.parse_amount('amount(１００,"억")', units) is None
        assert literal_types.parse_amount("100억", units) == 10000000000  # control

    def test_declared_unit_name_with_a_full_width_digit_survives_the_fold(self):
        # main's existing pin builds this spec from a literal dict; this is the
        # other route in — the declaration parser, which is what #325 changed.
        # Folding must not rename the unit, or the value stops matching it.
        units = common._parse_amount_units("억１=100000000")
        assert set(units) == {"억１"}
        spec = common.TypedRelSpec("amount", "revenue", units)
        objects = ['amount(200,"억１")', 'amount(300,"억１")']
        assert literal_types.normalize(spec.type, objects[0], spec.units) == 20000000000
        assert check_conflicts.non_ascii_digit_note(objects, spec) is None

    def test_unit_value_may_still_be_written_with_full_width_digits(self):
        # The digit rule does not govern the declaration file: the multiplier is
        # read with Decimal, which accepts them. Folding the NAME left that alone.
        assert common._parse_amount_units("억=１００００００００") == {"억": 100000000}

    def test_note_stays_silent_on_an_nfd_table_that_now_parses(self):
        # The #325 fix's own KB shape, and a control by construction: an NFD
        # table met an NFD object as raw bytes even before the fold, so these
        # parsed then too. What the fold changed is the MIXED case, not this one.
        # Held here because the note's silence must survive either way — the
        # values carry only ASCII digits.
        units = common._parse_amount_units(_nfd("억=1e8, 조=1e12"))
        spec = common.TypedRelSpec("amount", "revenue", units)
        objects = [_nfd("5400억"), _nfd("1조")]
        assert all(literal_types.normalize(spec.type, o, spec.units) is not None for o in objects)
        assert check_conflicts.non_ascii_digit_note(objects, spec) is None

    def test_note_still_fires_on_an_nfd_table_carrying_full_width_digits(self):
        # The fold must not swallow the note. Control: fullwidth digits fail the
        # numeric group whatever the table's normalization form, so this held
        # before #325 too — it is here to prove the fold did not quietly rescue
        # a value the digit policy rejects.
        units = common._parse_amount_units(_nfd("억=1e8"))
        spec = common.TypedRelSpec("amount", "revenue", units)
        note = check_conflicts.non_ascii_digit_note([_nfd("１００억"), _nfd("200억")], spec)
        assert note is not None
        assert "\\uff11\\uff10\\uff10" in "\n".join(note)


class TestTheNoteSurvivesTheFoldClassRepresentativeList:
    """``main()`` hands the note **fold-class representatives**, not raw objects.

    On main, ``conflicts[key]`` was the list of distinct raw objects and
    ``non_ascii_digit_note`` was written against that. #325 changed it: a group
    is now keyed on the fold, and the CONFLICT line — hence the note's input —
    carries one *representative* per group. Coverage is unaffected only because
    NFC never folds fullwidth onto halfwidth (that is NFKC), so a value carrying
    non-ASCII digits is always alone in its fold class and is therefore always
    its own representative. That is an argument; these are the measurements.

    These run through ``main()`` rather than calling the note directly, because
    the substitution being checked happens in ``main()`` and every existing
    #331/#336 test calls the note with a hand-built list.
    """

    def _run(self, monkeypatch, capsys, facts, single_valued, typed):
        monkeypatch.setattr(check_conflicts, "ensure_dirs", lambda: None)
        monkeypatch.setattr(check_conflicts, "load_facts", lambda: facts)
        monkeypatch.setattr(check_conflicts, "single_valued_relations", lambda: single_valued)
        monkeypatch.setattr(check_conflicts, "typed_relations", lambda: typed)
        monkeypatch.setattr(check_conflicts, "relation_aliases", lambda: {})
        rc = check_conflicts.main([])
        return rc, capsys.readouterr().err

    def _fact(self, obj, src):
        return {
            "subject": "Acme", "relation": "매출", "object": obj, "source": src,
            "status": "confirmed", "confidence": "0.9", "note": "",
        }

    def test_both_notes_fire_together_in_the_fixed_order(self, monkeypatch, capsys):
        # The case the coverage argument turns on: ONE fold class holding two
        # spellings of 100억, plus a SEPARATE fullwidth-digit value. The spelling
        # disclosure describes the grouping, the digit note qualifies the repair
        # advice, and both must be present.
        facts = [
            self._fact("100억", "sources/a.md"),
            self._fact(_nfd("100억"), "sources/b.md"),
            self._fact("１００억", "sources/c.md"),
        ]
        rc, err = self._run(monkeypatch, capsys, facts, {"매출"},
                            {"매출": common.TypedRelSpec("amount", "revenue")})
        assert rc == 1
        i_spelling = err.find("spellings:")
        i_digits = err.find("does not parse")
        i_advice = err.find("Resolve by marking")
        assert -1 < i_spelling < i_digits < i_advice, err

    def test_the_note_names_the_offender_not_the_representative(self, monkeypatch, capsys):
        # The substitution risk stated concretely: if the representative stood in
        # for the offender the escapes would be 100억's, and the reader would be
        # pointed at a value that is perfectly readable.
        facts = [
            self._fact("100억", "sources/a.md"),
            self._fact(_nfd("100억"), "sources/b.md"),
            self._fact("１００억", "sources/c.md"),
        ]
        _, err = self._run(monkeypatch, capsys, facts, {"매출"},
                           {"매출": common.TypedRelSpec("amount", "revenue")})
        assert "\\uff11\\uff10\\uff10억" in err

    def test_a_full_width_value_is_its_own_fold_class(self, monkeypatch, capsys):
        # The mechanism the argument rests on, asserted directly rather than
        # inferred from the note's output.
        facts = [
            self._fact("100억", "sources/a.md"),
            self._fact(_nfd("100억"), "sources/b.md"),
            self._fact("１００억", "sources/c.md"),
        ]
        scan = check_conflicts.collect_conflicts(
            facts, {"매출"}, {"매출": common.TypedRelSpec("amount", "revenue")}
        )
        variants = scan.object_variants[("Acme", "매출")]
        assert variants["１００억"] == ["１００억"]
        assert scan.conflicts[("Acme", "매출")] == ["100억", "１００억"]

    def test_digit_note_does_not_arm_the_mixed_normalization_summary(self, monkeypatch, capsys):
        # `any_mixed` must stay driven by the spelling block alone. A plain-NFC KB
        # with a fullwidth offender has no mixed spellings at all, so the closing
        # normalization paragraph would be a false statement about the KB.
        facts = [self._fact("100억", "sources/a.md"), self._fact("１００억", "sources/b.md")]
        _, err = self._run(monkeypatch, capsys, facts, {"매출"},
                           {"매출": common.TypedRelSpec("amount", "revenue")})
        assert "does not parse" in err
        assert "more than one Unicode normalization form" not in err

    def test_second_spec_probe_is_still_load_bearing(self, monkeypatch, capsys):
        # main's `typed.get(relation) or typed.get(NFC(relation))`. #325 folded
        # relation *membership*, not this dict lookup: `typed` is keyed NFC by
        # _parse_typed_relations while the conflict key keeps the row's verbatim
        # (NFD) name, so the first probe still misses and the second still hits.
        # Dropping the second probe leaves spec None, the spec gate suppresses the
        # note, and the reader loses it entirely — measured, and previously
        # unpinned: the whole suite stayed green without it.
        nfd_relation = _nfd("매출")
        facts = [
            {"subject": "Acme", "relation": nfd_relation, "object": o,
             "source": s, "status": "confirmed", "confidence": "0.9", "note": ""}
            for o, s in (("100억", "sources/a.md"), ("１００억", "sources/b.md"))
        ]
        _, err = self._run(monkeypatch, capsys, facts, {nfd_relation},
                           {"매출": common.TypedRelSpec("amount", "revenue")})
        assert "does not parse" in err
        assert "\\uff11\\uff10\\uff10억" in err
