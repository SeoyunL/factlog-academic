# SPDX-License-Identifier: Apache-2.0
"""Value-vocabulary hygiene audit (#212).

The relation vocabulary is curated by policy; the VALUE vocabulary is not. Values
arrive one extraction at a time and nothing notices when the same thing lands
twice under two strings — observed in a real KB with `IL-10` and `기타(IL-10)`
both accepted, so `relation(P, "염증지표", "IL-10")?` silently returned 3 of 4
rows.

The audit must be precise, not a similarity firehose: `entity_audit.py` reports
2275 candidates on that KB (every `IL-*` pairs with every other by shared token),
which buries the real finding. So these tests pin BOTH that the real problems are
caught AND that legitimate values are left alone.
"""
from __future__ import annotations

import value_audit


def _row(relation, object_, subject="S1"):
    return {"subject": subject, "relation": relation, "object": object_, "status": "accepted"}


class TestSplitWrapper:
    def test_wrapper_beside_its_twin_is_a_split(self):
        facts = [
            _row("염증지표", "IL-10", "P1"),
            _row("염증지표", "IL-10", "P2"),
            _row("염증지표", "기타(IL-10)", "P3"),
        ]
        found = value_audit.audit(facts)
        assert len(found["splits"]) == 1
        split = found["splits"][0]
        assert split["value"] == "기타(IL-10)"
        assert split["twin"] == "IL-10"
        assert split["rows"] == "1" and split["twin_rows"] == "2"

    def test_wrapper_without_a_twin_is_a_wrapper_not_a_split(self):
        facts = [_row("염증지표", "기타(INFLA-score)")]
        found = value_audit.audit(facts)
        assert found["splits"] == []
        assert found["wrappers"][0]["inner"] == "INFLA-score"

    def test_a_split_is_scoped_to_its_relation(self):
        # `IL-10` under a DIFFERENT relation is not the twin of this wrapper.
        facts = [
            _row("염증지표", "기타(IL-10)"),
            _row("측정지표", "IL-10"),
        ]
        found = value_audit.audit(facts)
        assert found["splits"] == []
        assert len(found["wrappers"]) == 1


class TestFalsePositives:
    def test_a_legitimate_parenthetical_is_not_a_wrapper(self):
        # The paren carries an abbreviation, not a junk-drawer label. Flagging
        # this would make the audit noise, which is the whole failure of the
        # existing entity fragmentation heuristic.
        facts = [_row("대상질환", "hyperoxia-induced lung injury (HLI)")]
        found = value_audit.audit(facts)
        assert not any(found.values())

    def test_distinct_values_sharing_a_token_are_not_reported(self):
        # `entity_audit` pairs these by the shared 'IL' token; this audit must not.
        facts = [
            _row("염증지표", "IL-10"),
            _row("염증지표", "IL-13"),
            _row("염증지표", "IL-1β"),
            _row("염증지표", "IL-4"),
        ]
        assert not any(value_audit.audit(facts).values())


class TestSpellingDuplicate:
    def test_one_subject_spelled_two_ways_is_a_split(self):
        facts = [_row("염증지표", "IL-8", "P1"), _row("염증지표", "il 8", "P1")]
        dup = value_audit.audit(facts)["duplicates"][0]
        assert dup["kind"] == "split"
        assert dup["subjects"] == "P1"

    def test_different_subjects_sharing_a_value_is_a_duplicate_record(self):
        # Two papers whose titles differ only in capitalisation are not a spelling
        # split — they may be two records of one thing. Different repair, so the
        # audit must not conflate them (and --strict must not fail on it).
        facts = [
            _row("제목", "Omega-3 and Lung Function", "PMID_1"),
            _row("제목", "omega-3 and lung function", "PMID_2"),
        ]
        dup = value_audit.audit(facts)["duplicates"][0]
        assert dup["kind"] == "duplicate_record"
        assert dup["subjects"] == "PMID_1, PMID_2"

    def test_a_wrapper_split_is_not_double_counted_as_a_duplicate(self):
        facts = [_row("염증지표", "IL-10"), _row("염증지표", "기타(IL-10)")]
        found = value_audit.audit(facts)
        assert len(found["splits"]) == 1
        assert found["duplicates"] == []


class TestPlaceholder:
    def test_bare_junk_values_are_reported(self):
        facts = [_row("연구유형", "기타"), _row("대상질환", "N/A"), _row("염증지표", "불명")]
        found = value_audit.audit(facts)
        assert {f["value"] for f in found["placeholders"]} == {"기타", "N/A", "불명"}

    def test_a_real_value_is_not_a_placeholder(self):
        facts = [_row("연구유형", "코호트연구")]
        assert not any(value_audit.audit(facts).values())


class TestClean:
    def test_a_healthy_vocabulary_reports_nothing(self):
        facts = [
            _row("연구유형", "RCT", "P1"),
            _row("연구유형", "코호트연구", "P2"),
            _row("대상질환", "COPD", "P1"),
        ]
        assert not any(value_audit.audit(facts).values())
