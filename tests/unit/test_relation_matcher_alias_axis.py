# SPDX-License-Identifier: Apache-2.0
"""relation_row_matches must fold the ALIAS axis to NFC (#324, #325).

The alias map is keyed by NFC names (relation_aliases normalizes on load,
common.py:1546-1547), but relation_row_matches probed it with the raw stored
relation. An NFD-authored alias row therefore missed the map and fell through to
its own surface name, so two axes silently split by Unicode form:

* #325 — the variable-relation hierarchy lookup at :1349 resolved the row's
  relation raw, so an NFD alias row never reached a value-hierarchy declaration
  written on the canonical name: `코호트연구` stopped matching `관찰연구`.
* #324 — the pinned-relation membership test at :1340 compared the raw row
  relation against the NFC-keyed `variants` set, so an NFD alias row was a
  verified negative for a pinned canonical query.

Both are the same root cause on the same function, different lines / symptoms.
Each fold mirrors _canonicalize (:942): NFC before the NFC-keyed lookup.
"""
from __future__ import annotations

import unicodedata

import pytest
from factlog.common import QUERY_OK, classify_query, relation_row_matches

nfc = lambda s: unicodedata.normalize("NFC", s)  # noqa: E731

STUDY_TYPE = "연구유형"  # alias raw name (NFC key in the map)
COHORT = "코호트연구"  # narrow value stored on the row
OBSERVATIONAL = "관찰연구"  # broad value the query asks for
STUDY_HIERARCHY = {"study_type": {COHORT: {OBSERVATIONAL}}}
STUDY_ALIASES = {nfc(STUDY_TYPE): "study_type"}


def _study_row(form: str) -> dict[str, str]:
    return {
        "subject": "p1",
        "relation": unicodedata.normalize(form, STUDY_TYPE),
        "object": nfc(COHORT),
        "status": "accepted",
    }


class TestVariableRelationKeepsAliasSubsumption:
    """relation("p1", R, "관찰연구")? — the row is filed as `코호트연구` under an
    alias relation, and the declaration lives on the canonical name. The variable
    relation forces the row's OWN name (:1349) to be resolved through the alias
    map, so an NFD row must fold to NFC before that lookup to reach the
    declaration. This is the line #325 fixes; the pinned-relation membership on the
    same row is #324's line, exercised separately."""

    @pytest.mark.parametrize("form", ["NFC", "NFD"])
    def test_broad_query_subsumes_narrow_row(self, form):
        matched = relation_row_matches(
            ['"p1"', "R", f'"{nfc(OBSERVATIONAL)}"'],
            _study_row(form),
            STUDY_ALIASES,
            STUDY_HIERARCHY,
        )
        assert matched, (
            f"{form}-authored alias row lost value-hierarchy subsumption: "
            f"코호트연구 is a 관찰연구, but the variable-relation query could not see it"
        )

    def test_nfc_and_nfd_agree(self):
        args = ['"p1"', "R", f'"{nfc(OBSERVATIONAL)}"']
        nfc_ans = relation_row_matches(args, _study_row("NFC"), STUDY_ALIASES, STUDY_HIERARCHY)
        nfd_ans = relation_row_matches(args, _study_row("NFD"), STUDY_ALIASES, STUDY_HIERARCHY)
        assert nfc_ans == nfd_ans is True

    def test_nfc_pinned_regression_still_subsumes(self):
        """The NFC alias row already subsumed under a pinned canonical query (the
        relation is drawn from the query constant, not the row). #325 must not
        break that path. The NFD pinned case needs #324 and is asserted there."""
        assert relation_row_matches(
            ['"p1"', '"study_type"', f'"{nfc(OBSERVATIONAL)}"'],
            _study_row("NFC"),
            STUDY_ALIASES,
            STUDY_HIERARCHY,
        )


# --- #324: pinned-relation membership across the alias axis --------------------

PUB = "게재연도"  # alias raw name; NFD is what macOS types
PUB_ALIASES = {nfc(PUB): "published_year"}


def _pub_row(form: str, value: str) -> dict[str, str]:
    return {
        "subject": "paper1",
        "relation": unicodedata.normalize(form, PUB),
        "object": value,
        "status": "accepted",
    }


class TestPinnedCanonicalQueryMatchesAliasRow:
    """relation("paper1", "published_year", "2020")? — the row is stored under the
    surface variant `게재연도`. The pinned canonical name is looked up in the
    NFC-keyed variants set (:1340); an NFD row must fold to NFC to be recognised,
    or the query is a verified negative about a fact the engine holds (#324)."""

    @pytest.mark.parametrize("form", ["NFC", "NFD"])
    def test_pinned_canonical_matches(self, form):
        matched = relation_row_matches(
            ['"paper1"', '"published_year"', '"2020"'],
            _pub_row(form, "2020"),
            PUB_ALIASES,
            None,
        )
        assert matched, (
            f"{form}-authored alias row was a verified negative for the pinned "
            f"canonical query — the engine proves the fact, the matcher denied it"
        )

    def test_nfc_and_nfd_agree(self):
        args = ['"paper1"', '"published_year"', '"2020"']
        nfc_ans = relation_row_matches(args, _pub_row("NFC", "2020"), PUB_ALIASES, None)
        nfd_ans = relation_row_matches(args, _pub_row("NFD", "2020"), PUB_ALIASES, None)
        assert nfc_ans == nfd_ans is True

    def test_direct_canonical_name_match_unaffected(self):
        """The direct-match clause (:1339, both sides canonicalized) is untouched:
        a row stored under the canonical name itself still matches, so #324's fold
        only ADDS the alias arm, never widening the direct comparison."""
        row = {
            "subject": "paper1",
            "relation": unicodedata.normalize("NFD", "published_year"),
            "object": "2020",
            "status": "accepted",
        }
        assert relation_row_matches(['"paper1"', '"published_year"', '"2020"'], row, {}, None)


class TestDetectConflictsAndMatcherAgree:
    """The detectable asymmetry #324 names: detect_conflicts folds the alias axis
    (via _canonicalize) and reports a cross-variant contradiction on the canonical
    name; the matcher must SEE the very rows that conflict. Before the fold one
    reported the conflict while the other found neither row — that disagreement was
    the only signal the NFD row existed at all."""

    def test_conflict_rows_are_matchable(self):
        import common

        facts = [_pub_row("NFD", "2020"), _pub_row("NFD", "2021")]
        conflicts = common.detect_conflicts(facts, {"published_year"}, aliases=PUB_ALIASES)
        assert ("paper1", "published_year") in conflicts, (
            f"detect_conflicts folds the alias axis and should report the "
            f"contradiction on the canonical name -> {conflicts}"
        )
        # The matcher must agree those rows exist under the canonical query — the
        # symmetry #324 restores. Before the fold this side answered False.
        assert all(
            relation_row_matches(
                ['"paper1"', '"published_year"', f'"{row["object"]}"'], row, PUB_ALIASES, None
            )
            for row in facts
        )


# --- gate/matcher parity (C2): the gate routes through the same predicate -------


@pytest.fixture
def pub_kb(tmp_path, monkeypatch):
    """Point the lazily-resolved POLICY_DIR at a temp KB carrying the alias
    declaration, so classify_query reads it (mirrors test_query_gate_nfc)."""
    import factlog.common as fc

    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text(
        f"# relation aliases\n\n- `{nfc(PUB)}` -> `published_year`\n", encoding="utf-8"
    )
    monkeypatch.setattr(fc, "POLICY_DIR", policy)
    return tmp_path


class TestGateAgreesWithMatcher:
    """The pinned canonical query is accepted (QUERY_OK) by the gate for every
    normal form of the alias row — the gate's _relation_match_count and the matcher
    are the same predicate (the callers converge on relation_row_matches), so the
    gate never answers a verified negative where the matcher matches. Before #324
    the NFD row gave QUERY_FACT_ABSENT: the gate asserting no such fact about a fact
    the engine holds."""

    @pytest.mark.parametrize("form", ["NFC", "NFD"])
    def test_pinned_query_is_query_ok(self, pub_kb, form):
        ok, code, _ = classify_query(
            'relation("paper1", "published_year", "2020")?', [_pub_row(form, "2020")]
        )
        assert (ok, code) == (True, QUERY_OK), code
