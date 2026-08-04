# SPDX-License-Identifier: Apache-2.0
"""The conditional guidance a conflict on a non-ASCII-digit value carries (#331).

``check_conflicts`` resolves conflicts by telling the user to mark the outdated
row ``status='superseded'``. That advice assumes one of the two values is out of
date. When one of them carries non-ASCII digits it does not parse as a typed
literal at all, so it degrades to a raw-string key — and following the generic
advice on the *ASCII* row clears the gate while leaving the KB holding the value
the engine cannot read (measured: ``check_conflicts`` then reports
``0 conflicts`` and exits 0).

What these tests hold down is the *shape* of the extra note, not its prose:

* it fires only when a value actually carries non-ASCII digits (the negative
  controls below are what make that real — a note printed unconditionally would
  satisfy the positive cases alone);
* it names the offending characters as escapes, because ``repr('１００억')`` is
  ``'１００억'`` — visually identical to ``'100억'`` in most fonts, so a bare repr
  would name a value the reader cannot pick out;
* it does NOT claim supersession cannot resolve the conflict. Superseding the
  full-width row *does* resolve it, correctly and durably. Only superseding the
  ASCII row is harmful, so the wording says supersession *can* leave the bad
  value behind.
"""
from __future__ import annotations

import check_conflicts


class TestAsciiCleanGroupsGetNoNote:
    """Negative controls. Without these the positive cases below would pass just
    as well against a note that was printed for every conflict."""

    def test_two_ascii_amounts_get_no_note(self):
        assert check_conflicts.non_ascii_digit_note(["100억", "200억"]) is None

    def test_unparseable_ascii_strings_get_no_note(self):
        # Unparseable is not the trigger — non-ASCII digits are. Both of these
        # degrade to raw keys exactly like a full-width value does.
        assert check_conflicts.non_ascii_digit_note(["n/a", "unknown"]) is None

    def test_ascii_ordinal_forms_get_no_note(self):
        assert check_conflicts.non_ascii_digit_note(["3rd", "제3호"]) is None

    def test_empty_group_gets_no_note(self):
        assert check_conflicts.non_ascii_digit_note([]) is None


class TestNonAsciiDigitGroupsGetTheNote:
    def test_note_names_the_offender_as_escapes(self):
        note = "\n".join(check_conflicts.non_ascii_digit_note(["100억", "１００억"]))
        # The escaped codepoints, NOT the raw glyph: a reader must be able to see
        # WHICH characters are wrong.
        assert "\\uff11\\uff10\\uff10억" in note
        assert "'１００억'" not in note

    def test_note_states_the_cause_and_the_fix(self):
        note = "\n".join(check_conflicts.non_ascii_digit_note(["100억", "１００억"]))
        assert "does not parse" in note
        assert "re-collect" in note
        assert "docs/reference/typed-relations.md" in note

    def test_note_does_not_claim_supersede_cannot_work(self):
        # Superseding the full-width row DOES resolve the conflict. The note must
        # hedge ("can leave") rather than assert supersession is useless, or the
        # gate would be printing something false at the moment it fails.
        note = "\n".join(check_conflicts.non_ascii_digit_note(["100억", "１００억"]))
        assert "can leave" in note
        assert "cannot" not in note

    def test_only_the_offending_value_is_named(self):
        note = "\n".join(check_conflicts.non_ascii_digit_note(["100억", "１００억"]))
        # The ASCII twin is not the problem and must not be pointed at.
        assert "'100억'" not in note

    def test_fires_for_digit_systems_other_than_full_width(self):
        # Arabic-Indic. The policy is all non-ASCII Nd, not only U+FF10-FF19.
        note = check_conflicts.non_ascii_digit_note(["100", "١٠٠"])
        assert note is not None
        assert "\\u0661\\u0660\\u0660" in "\n".join(note)

    def test_fires_for_devanagari(self):
        note = check_conflicts.non_ascii_digit_note(["123", "१२३"])
        assert note is not None
        assert "\\u0967\\u0968\\u0969" in "\n".join(note)

    def test_every_offender_is_named(self):
        note = "\n".join(check_conflicts.non_ascii_digit_note(["１００억", "２００억"]))
        assert "\\uff11\\uff10\\uff10억" in note
        assert "\\uff12\\uff10\\uff10억" in note

    def test_returns_lines_without_trailing_newlines(self):
        # main() prints these one per call; embedded newlines would double-space.
        for line in check_conflicts.non_ascii_digit_note(["100억", "１００억"]):
            assert "\n" not in line
