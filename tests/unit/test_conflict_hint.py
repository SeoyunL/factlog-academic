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

import check_conflicts
import common
import literal_types

# A typed single-valued amount relation — the only shape the note applies to.
_AMOUNT_SPEC = common.TypedRelSpec("amount", "revenue")


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
