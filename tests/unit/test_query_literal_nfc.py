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

The chokepoint only covers a comparison that actually routes through it. Two relation-
name membership tests did not: the ``classify_query`` acceptance gate compared the raw
query name against the raw ``allowed_relations()`` names (in the ``relation`` branch and
in the ``count`` branch), and ``_relation_match_count`` compared a raw stored name against
the NFC alias keys. The gate ran first, so an NFD-stored relation queried in NFC was
turned away with QUERY_RELATION_NOT_ACCEPTED and never reached the folding match count.
Both now compare canonicalised values.

Scope note: ``path`` queries answer against the engine's interned pairs, a raw
comparison that does NOT pass through ``_canonical_value``; that engine-intern path is
a separate concern and is not covered here.
"""
from __future__ import annotations

import unicodedata

from factlog import common
from factlog.common import _canonical_value, _relation_match_count, classify_query

nfc = lambda s: unicodedata.normalize("NFC", s)  # noqa: E731
nfd = lambda s: unicodedata.normalize("NFD", s)  # noqa: E731

REL = "연구유형"
OBJ = "관찰연구"
SUBJ = "P1"
CANONICAL = "출판연도"
SURFACE = "게재연도"


def _fact(subject, relation, object_):
    return {"subject": subject, "relation": relation, "object": object_, "status": "confirmed"}


def _relation_query(subject, relation, object_):
    return f'relation("{subject}", "{relation}", "{object_}")?'


def _count_query(subject, relation):
    return f'count("{subject}", "{relation}")?'


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


class TestSubjectGateFoldsForms:
    """Subject membership must fold before relation/count matching runs."""

    def test_nfd_stored_subject_passes_an_nfc_relation_query(self):
        subject = "연구자"
        facts = [_fact(nfd(subject), nfc(REL), nfc(OBJ))]
        ok, code, _reason = classify_query(
            _relation_query(nfc(subject), nfc(REL), nfc(OBJ)), facts, policy_program=""
        )
        assert ok, code

    def test_nfc_stored_subject_passes_an_nfd_relation_query(self):
        subject = "연구자"
        facts = [_fact(nfc(subject), nfc(REL), nfc(OBJ))]
        ok, code, _reason = classify_query(
            _relation_query(nfd(subject), nfc(REL), nfc(OBJ)), facts, policy_program=""
        )
        assert ok, code

    def test_nfd_stored_subject_passes_an_nfc_count_query(self):
        subject = "연구자"
        facts = [_fact(nfd(subject), nfc(REL), nfc(OBJ))]
        ok, code, _reason = classify_query(_count_query(nfc(subject), nfc(REL)), facts, policy_program="")
        assert ok, code

    def test_nfc_stored_subject_passes_an_nfd_count_query(self):
        subject = "연구자"
        facts = [_fact(nfc(subject), nfc(REL), nfc(OBJ))]
        ok, code, _reason = classify_query(_count_query(nfd(subject), nfc(REL)), facts, policy_program="")
        assert ok, code


class TestRelationNameGateFoldsForms:
    """The relation-name acceptance gate must fold too, not just the object check.

    ``allowed_relations()`` returns the raw stored ``row["relation"]`` strings, so a
    membership test against the raw query name rejects an NFD-stored relation queried
    in NFC with QUERY_RELATION_NOT_ACCEPTED — and returns before
    ``_relation_match_count`` (which does fold) is ever consulted. The gate must
    compare canonicalised values on both sides, in the ``relation`` branch and in the
    ``count`` branch alike.
    """

    def test_nfd_stored_relation_passes_an_nfc_relation_query(self):
        facts = [_fact(SUBJ, nfd(REL), nfd(OBJ))]
        ok, code, _reason = classify_query(
            _relation_query(SUBJ, nfc(REL), nfc(OBJ)), facts, policy_program=""
        )
        assert ok, code

    def test_nfc_stored_relation_passes_an_nfd_relation_query(self):
        facts = [_fact(SUBJ, nfc(REL), nfc(OBJ))]
        ok, code, _reason = classify_query(
            _relation_query(SUBJ, nfd(REL), nfd(OBJ)), facts, policy_program=""
        )
        assert ok, code

    def test_nfd_stored_relation_passes_an_nfc_count_query(self):
        facts = [_fact(SUBJ, nfd(REL), nfd(OBJ))]
        ok, code, _reason = classify_query(_count_query(SUBJ, nfc(REL)), facts, policy_program="")
        assert ok, code

    def test_nfc_stored_relation_passes_an_nfd_count_query(self):
        facts = [_fact(SUBJ, nfc(REL), nfc(OBJ))]
        ok, code, _reason = classify_query(_count_query(SUBJ, nfd(REL)), facts, policy_program="")
        assert ok, code


class TestUnacceptedRelationIsStillRejected:
    """Folding widens which forms of the SAME name are accepted — never which names.
    A relation nobody stored must still be turned away with QUERY_RELATION_NOT_ACCEPTED
    by both branches, in either normal form."""

    def test_unknown_relation_is_rejected_by_the_relation_branch(self):
        facts = [_fact(SUBJ, nfd(REL), nfd(OBJ))]
        ok, code, _reason = classify_query(
            _relation_query(SUBJ, nfc("혈액형"), nfc(OBJ)), facts, policy_program=""
        )
        assert not ok
        assert code == common.QUERY_RELATION_NOT_ACCEPTED

    def test_unknown_relation_is_rejected_by_the_count_branch(self):
        facts = [_fact(SUBJ, nfd(REL), nfd(OBJ))]
        ok, code, _reason = classify_query(
            _count_query(SUBJ, nfc("혈액형")), facts, policy_program=""
        )
        assert not ok
        assert code == common.QUERY_RELATION_NOT_ACCEPTED

    def test_a_variable_relation_still_skips_the_check(self):
        facts = [_fact(SUBJ, nfd(REL), nfd(OBJ))]
        ok, code, _reason = classify_query(
            f'count("{SUBJ}", R)?', facts, policy_program=""
        )
        assert ok, code


class TestAliasReadStaysGated:
    """The #242 optimisation must survive the fold: relation_aliases() is read at most
    once per relation query, and the gate itself reads it only when the name is not
    already an accepted relation — so a variable or known relation never reaches its
    raise-on-malformed-file through the gate.

    The read counts pinned here are the pre-fold ones: a relation query costs exactly one
    read (the lazy fetch inside _relation_match_count for a quoted relation argument), a
    count query on a known relation costs none. Folding the membership test must not add
    a read, and must not make a previously-known relation fall through to the gate's own
    read.
    """

    def _reader(self, monkeypatch, aliases=None):
        reads = []

        def _counting(*_args, **_kwargs):
            reads.append(1)
            return {} if aliases is None else aliases

        monkeypatch.setattr(common, "relation_aliases", _counting)
        return reads

    def test_a_known_nfd_relation_count_query_never_reads_the_alias_file(self, monkeypatch):
        facts = [_fact(SUBJ, nfd(REL), nfd(OBJ))]
        reads = self._reader(monkeypatch)
        ok, code, _reason = classify_query(_count_query(SUBJ, nfc(REL)), facts, policy_program="")
        assert ok, code
        assert reads == []

    def test_a_known_nfd_relation_query_reads_no_more_than_before(self, monkeypatch):
        facts = [_fact(SUBJ, nfd(REL), nfd(OBJ))]
        reads = self._reader(monkeypatch)
        ok, code, _reason = classify_query(
            _relation_query(SUBJ, nfc(REL), nfc(OBJ)), facts, policy_program=""
        )
        assert ok, code
        assert len(reads) == 1  # the lazy fetch in _relation_match_count, as before

    def test_a_variable_relation_never_reads_the_alias_file(self, monkeypatch):
        facts = [_fact(SUBJ, nfd(REL), nfd(OBJ))]
        reads = self._reader(monkeypatch)
        ok, code, _reason = classify_query(
            f'relation("{SUBJ}", R, "{nfc(OBJ)}")?', facts, policy_program=""
        )
        assert ok, code
        assert reads == []

    def test_a_canonical_relation_reads_the_alias_file_exactly_once(self, monkeypatch):
        facts = [_fact(SUBJ, nfd(SURFACE), nfc("2019"))]
        reads = []

        def _counting(*_args, **_kwargs):
            reads.append(1)
            return {nfc(SURFACE): nfc(CANONICAL)}

        monkeypatch.setattr(common, "relation_aliases", _counting)
        # CANONICAL is a declared canonical, so the gate accepts via the alias lookup
        # and hands the same map to _relation_match_count instead of re-reading it.
        ok, code, _reason = classify_query(
            _relation_query(SUBJ, nfc(CANONICAL), nfc("2019")), facts, policy_program=""
        )
        assert ok, code
        assert len(reads) == 1


class TestSurfaceVariantMatchFoldsForms:
    """A canonical-name query counts rows stored under a surface alias. The alias map is
    NFC on load (relation_aliases() normalizes keys and canonical targets), but a stored
    row need not be — so that membership test folds as well."""

    def test_nfd_stored_surface_variant_matches_a_canonical_query(self):
        aliases = {nfc(SURFACE): nfc(CANONICAL)}
        facts = [_fact(SUBJ, nfd(SURFACE), nfc("2019"))]
        query = _relation_query(SUBJ, nfc(CANONICAL), nfc("2019"))
        assert _relation_match_count(query, facts, aliases) == 1

    def test_an_unrelated_relation_is_still_not_counted(self):
        aliases = {nfc(SURFACE): nfc(CANONICAL)}
        facts = [_fact(SUBJ, nfd(REL), nfc("2019"))]
        query = _relation_query(SUBJ, nfc(CANONICAL), nfc("2019"))
        assert _relation_match_count(query, facts, aliases) == 0


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


class TestRelationGateFoldIsLoadBearing:
    """Red/green guard for the gate itself: the raw membership test the gate used to do
    rejects the NFD-stored relation, so without the fold classify_query could never have
    reached the (already folding) match count. Pinned by reproducing the raw comparison
    against the same allowed_relations() the gate reads."""

    def test_the_raw_membership_test_would_have_rejected_the_nfd_relation(self):
        facts = [_fact(SUBJ, nfd(REL), nfd(OBJ))]
        relations = common.allowed_relations(facts)
        assert nfc(REL) not in relations  # the bug: the raw test turned the query away
        assert _canonical_value(nfc(REL)) in {_canonical_value(r) for r in relations}
        for query in (_relation_query(SUBJ, nfc(REL), nfc(OBJ)), _count_query(SUBJ, nfc(REL))):
            ok, code, _reason = classify_query(query, facts, policy_program="")
            assert ok, code  # the fix
