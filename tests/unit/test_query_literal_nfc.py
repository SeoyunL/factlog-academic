# SPDX-License-Identifier: Apache-2.0
"""A query constant meets a fact whatever unicode normal form each was authored in.

Every query-value comparison routes through ``common._canonical_value`` (the single
chokepoint #213 set up: ``_relation_match_count`` and the ``classify_query``
acceptance gate both call it). It folded ``amount`` quoting but left an ordinary
string in whatever normal form it arrived in, so an NFD-stored relation or object —
macOS text is routinely NFD — never met an NFC-typed query constant, and the
report/ask returned nothing about a fact that was right there. Folding NFC once at
that chokepoint makes both directions meet without touching any per-path code, and is
a no-op on NFC-only data.

Scope note: ``path`` queries answer against the engine's interned pairs, a raw
comparison that does NOT pass through ``_canonical_value``; that engine-intern path is
a separate concern and is not covered here.
"""
from __future__ import annotations

import unicodedata

from factlog.common import _canonical_value, _relation_match_count, classify_query

nfc = lambda s: unicodedata.normalize("NFC", s)  # noqa: E731
nfd = lambda s: unicodedata.normalize("NFD", s)  # noqa: E731

REL = "연구유형"
OBJ = "관찰연구"
SUBJ = "P1"


def _fact(subject, relation, object_):
    return {"subject": subject, "relation": relation, "object": object_, "status": "confirmed"}


def _relation_query(subject, relation, object_):
    return f'relation("{subject}", "{relation}", "{object_}")?'


class TestRelationMatchCountFoldsForms:
    """The shared match-count predicate matches a query constant to a fact across
    NFC/NFD in both the relation axis and the object axis."""

    def test_nfd_stored_relation_and_object_meet_nfc_query(self):
        facts = [_fact(SUBJ, nfd(REL), nfd(OBJ))]
        assert _relation_match_count(_relation_query(SUBJ, nfc(REL), nfc(OBJ)), facts) == 1

    def test_nfc_stored_relation_and_object_meet_nfd_query(self):
        facts = [_fact(SUBJ, nfc(REL), nfc(OBJ))]
        assert _relation_match_count(_relation_query(SUBJ, nfd(REL), nfd(OBJ)), facts) == 1

    def test_a_genuinely_different_object_still_does_not_match(self):
        facts = [_fact(SUBJ, nfc(REL), nfc(OBJ))]
        assert _relation_match_count(_relation_query(SUBJ, nfc(REL), nfc("실험연구")), facts) == 0

    def test_a_genuinely_different_relation_still_does_not_match(self):
        facts = [_fact(SUBJ, nfc(REL), nfc(OBJ))]
        assert _relation_match_count(_relation_query(SUBJ, nfc("혈액형"), nfc(OBJ)), facts) == 0


class TestCountFoldsForms:
    """A count is a relation query with a free object — same predicate, so distinct
    NFD-stored objects are each counted for an NFC query."""

    def test_count_over_nfd_facts_with_nfc_query(self):
        facts = [
            _fact(SUBJ, nfd(REL), nfd(OBJ)),
            _fact(SUBJ, nfd(REL), nfd("코호트연구")),
        ]
        matched = {
            row["object"]
            for row in facts
            if _relation_match_count(_relation_query(SUBJ, nfc(REL), row["object"]), facts) >= 1
        }
        assert len(matched) == 2


class TestGateDoesNotReject:
    """The acceptance gate (classify_query) must not turn an NFD-stored object away
    from an NFC query. Its object check folds through _canonical_value, so both sides
    land on the same NFC form and the query resolves instead of being rejected."""

    def test_nfd_object_fact_passes_the_gate(self):
        facts = [_fact(SUBJ, nfc(REL), nfd(OBJ))]
        ok, code, _reason = classify_query(
            _relation_query(SUBJ, nfc(REL), nfc(OBJ)), facts, policy_program=""
        )
        assert ok, code

    def test_nfc_object_fact_passes_an_nfd_query(self):
        facts = [_fact(SUBJ, nfc(REL), nfc(OBJ))]
        ok, code, _reason = classify_query(
            _relation_query(SUBJ, nfc(REL), nfd(OBJ)), facts, policy_program=""
        )
        assert ok, code


class TestAmountRegression:
    """The amount canonicalisation this function already did must be unchanged, and it
    must now also fold an NFD-authored unit."""

    def test_nfc_unit_quoting_still_canonicalises(self):
        assert _canonical_value(nfc("amount(100,억)")) == 'amount(100,"억")'
        assert _canonical_value(nfc('amount(100,"억")')) == 'amount(100,"억")'

    def test_nfd_unit_now_canonicalises_to_the_same_form(self):
        assert _canonical_value(nfd("amount(100,억)")) == 'amount(100,"억")'

    def test_a_different_amount_is_not_equal(self):
        assert _canonical_value("amount(100,억)") != _canonical_value("amount(200,억)")


class TestNfcOnlyIsANoOp:
    """A KB already in NFC must compare byte-identically: folding an NFC string returns
    it unchanged, so nothing about existing (NFC) data moves."""

    def test_plain_nfc_string_passes_through_unchanged(self):
        assert _canonical_value(nfc(OBJ)) == nfc(OBJ)

    def test_nfc_string_is_its_own_fold(self):
        value = nfc(REL)
        assert _canonical_value(value) == value == unicodedata.normalize("NFC", value)


class TestFoldIsLoadBearing:
    """Red/green guard: without the NFC fold the NFD case does not match. Pinned by
    computing the pre-fix comparison (raw amount canonicalisation) directly."""

    def test_the_pre_fix_comparison_would_have_missed_the_nfd_case(self):
        # What _canonical_value did before the fold: amount-only, no NFC.
        from factlog import literal_types

        pre_fix = lambda v: literal_types.canonical_amount(v) or v  # noqa: E731
        assert pre_fix(nfd(OBJ)) != pre_fix(nfc(OBJ))  # the bug: forms did not meet
        assert _canonical_value(nfd(OBJ)) == _canonical_value(nfc(OBJ))  # the fix
