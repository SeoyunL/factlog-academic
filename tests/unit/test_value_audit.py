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

from pathlib import Path

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

    def test_different_subjects_sharing_an_identifying_value_is_a_duplicate_record(self):
        # Two papers whose titles differ only in capitalisation are not a spelling
        # split — they may be two records of one thing. Different repair, so the
        # audit must not conflate them (and --strict must not fail on it).
        #
        # This only holds because 제목 IDENTIFIES its subject. The relation must be
        # declared in policy/attribute-relations.md for the audit to know that; a
        # categorical relation with the same shape is a real query leak (see
        # TestKindIsDecidedByPolicy).
        facts = [
            _row("제목", "Omega-3 and Lung Function", "PMID_1"),
            _row("제목", "omega-3 and lung function", "PMID_2"),
        ]
        dup = value_audit.audit(facts, identity_relations={"제목"})["duplicates"][0]
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


class TestKindIsDecidedByPolicy:
    """Not by subject count — that judgement was exactly backwards.

    In a CATEGORICAL relation (an inflammation marker, a study type) values are
    shared across subjects by design, so a folded collision between two subjects
    IS the query leak: asking for `IL-8` misses the rows filed as `il 8`. Calling
    that a "duplicate record" let the leak sail through the --strict gate — the
    exact thing #212 exists to catch. The real KB's own bug had different
    subjects too; only the wrapper rule happened to save it.
    """

    def test_categorical_collision_across_subjects_is_a_leak(self):
        facts = [_row("염증지표", "IL-8", "P1"), _row("염증지표", "il 8", "P2")]
        dup = value_audit.audit(facts, identity_relations=set())["duplicates"][0]
        assert dup["kind"] == "split"

    def test_identity_relation_collision_across_subjects_is_a_duplicate_record(self):
        # A title identifies its paper. Two papers whose titles fold together are
        # probably two records of one thing — a different repair, and not a leak.
        facts = [_row("제목", "Omega-3 and Lung", "PMID_1"), _row("제목", "omega-3 and lung", "PMID_2")]
        dup = value_audit.audit(facts, identity_relations={"제목"})["duplicates"][0]
        assert dup["kind"] == "duplicate_record"

    def test_identity_relation_collision_on_one_subject_is_still_a_leak(self):
        facts = [_row("제목", "A B", "PMID_1"), _row("제목", "a  b", "PMID_1")]
        dup = value_audit.audit(facts, identity_relations={"제목"})["duplicates"][0]
        assert dup["kind"] == "split"


class TestNumbersAreNotFolded:
    """Separators between digits are load-bearing.

    Folding them together made `1.5` and `15` "the same value", so --strict failed
    on perfectly good numeric data — a gate that cries wolf gets turned off.
    """

    def test_a_decimal_is_not_an_integer(self):
        facts = [_row("복용량", "1.5", "P1"), _row("복용량", "15", "P1")]
        assert value_audit.audit(facts)["duplicates"] == []

    def test_a_date_is_not_a_number(self):
        facts = [_row("발행일", "2023-01-05", "P1"), _row("발행일", "20230105", "P1")]
        assert value_audit.audit(facts)["duplicates"] == []

    def test_an_identifier_keeps_its_dots(self):
        facts = [_row("arxiv_id", "2507.03697", "P1"), _row("arxiv_id", "250703697", "P1")]
        assert value_audit.audit(facts)["duplicates"] == []

    def test_words_still_fold(self):
        # The fold must still do its job on text.
        facts = [_row("염증지표", "IL-8", "P1"), _row("염증지표", "il 8", "P1")]
        assert len(value_audit.audit(facts)["duplicates"]) == 1


class TestMoreFalsePositives:
    def test_sodium_is_not_a_placeholder(self):
        # `Na` folds into `n/a` only if placeholders are matched through the fold.
        assert not any(value_audit.audit([_row("측정지표", "Na", "P1")]).values())

    def test_n_a_is_still_a_placeholder(self):
        found = value_audit.audit([_row("측정지표", "N/A", "P1")])
        assert [f["value"] for f in found["placeholders"]] == ["N/A"]

    def test_an_uppercase_acronym_is_not_a_wrapper(self):
        # ETC = electron transport chain. Treating `etc` as a wrapper word made
        # this audit the very noise it replaces.
        assert not any(value_audit.audit([_row("경로", "ETC (electron transport chain)", "P1")]).values())

    def test_an_nfd_wrapper_is_still_caught(self):
        # macOS writes NFD. An audit that cannot see it stays silent on exactly
        # the KBs it exists for.
        import unicodedata

        facts = [
            _row("염증지표", "IL-10", "P1"),
            _row("염증지표", unicodedata.normalize("NFD", "기타(IL-10)"), "P2"),
        ]
        assert len(value_audit.audit(facts)["splits"]) == 1

    def test_a_reversed_wrapper_is_caught(self):
        found = value_audit.audit([_row("염증지표", "TGF-β(기타)", "P1")])
        assert found["wrappers"][0]["inner"] == "TGF-β"


class TestPlaceholdersAreNotDoubleCounted:
    def test_a_placeholder_is_not_also_a_duplicate(self):
        facts = [_row("측정지표", "N/A", "P1"), _row("측정지표", "n/a", "P1")]
        found = value_audit.audit(facts)
        assert len(found["placeholders"]) == 2
        assert found["duplicates"] == []


class TestExitCodes:
    """The --strict contract, which nothing pinned before.

    Every defect this file now guards against (the inverted kind judgement, the
    number fold, the double-counted placeholder) showed up as a WRONG EXIT CODE,
    and not one test called the tool. A gate nobody tests is a gate nobody trusts.

    Driven as a SUBPROCESS on purpose: like entity_audit, the module resolves the
    KB root from argv at IMPORT time, so calling main(["--wiki", other]) in-process
    would still read whatever KB the import bound. The exit code is only meaningful
    the way CI actually invokes it.
    """

    @staticmethod
    def _run(kb, *flags):
        import subprocess
        import sys

        tool = Path(__file__).resolve().parents[2] / "tools" / "value_audit.py"
        return subprocess.run(
            [sys.executable, str(tool), "--wiki", str(kb), *flags],
            capture_output=True, text=True,
        )

    @staticmethod
    def _kb(tmp_path, rows, attribute=""):
        import subprocess
        import sys

        kb = tmp_path / "kb"
        subprocess.run(
            [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
            check=True, capture_output=True,
        )
        (kb / "sources" / "a.md").write_text("a\n", encoding="utf-8")
        header = "subject,relation,object,source,status,confidence,note\n"
        (kb / "facts" / "candidates.csv").write_text(header + rows, encoding="utf-8")
        if attribute:
            (kb / "policy" / "attribute-relations.md").write_text(attribute, encoding="utf-8")
        return kb

    def test_a_clean_kb_exits_zero_under_strict(self, tmp_path):
        kb = self._kb(tmp_path, "P1,연구유형,RCT,sources/a.md,accepted,0.90,\n")
        assert self._run(kb, "--strict").returncode == 0

    def test_a_categorical_split_fails_strict(self, tmp_path):
        kb = self._kb(
            tmp_path,
            "P1,염증지표,IL-8,sources/a.md,accepted,0.90,\n"
            "P2,염증지표,il 8,sources/a.md,accepted,0.90,\n",
        )
        assert self._run(kb, "--strict").returncode == 1

    def test_a_duplicate_record_does_not_fail_strict(self, tmp_path):
        kb = self._kb(
            tmp_path,
            "PMID_1,제목,Omega-3 and Lung,sources/a.md,accepted,0.90,\n"
            "PMID_2,제목,omega-3 and lung,sources/a.md,accepted,0.90,\n",
            attribute="제목\n",
        )
        assert self._run(kb, "--strict").returncode == 0

    def test_numeric_values_do_not_fail_strict(self, tmp_path):
        kb = self._kb(
            tmp_path,
            "P1,복용량,1.5,sources/a.md,accepted,0.90,\n"
            "P1,복용량,15,sources/a.md,accepted,0.90,\n",
        )
        assert self._run(kb, "--strict").returncode == 0

    def test_an_empty_kb_exits_zero_instead_of_raising(self, tmp_path):
        kb = self._kb(tmp_path, "")
        (kb / "facts" / "candidates.csv").unlink()
        result = self._run(kb, "--strict")
        assert result.returncode == 0
        assert "no candidate facts" in result.stdout
        assert "Traceback" not in result.stderr
