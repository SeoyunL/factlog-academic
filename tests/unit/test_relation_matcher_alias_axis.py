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
from factlog.common import relation_row_matches

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
