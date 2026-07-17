# SPDX-License-Identifier: Apache-2.0
"""A pinned entity finds its rows across NFC/NFD (#320 policy filter folding).

Filtering a policy extent on the pinned entity is only an improvement if the pin can
actually meet the row. Compared verbatim it cannot: an engine-derived extent row
holding NFD "한글" never equals an NFC query constant, so the pin selects nothing and
both the report and the router answer `0 rows` — a positive claim that a subject with
rows has none (#284's verified negative), which reads exactly like an honest zero.

Parity does not rescue it. Report and router agreeing on a fabricated `0 rows` is
worse than one of them being wrong, because the disagreement was the only evidence the
NFD row existed. Hence the fold lives in `policy_row_matches`, the single predicate
both callers route through: folding in one caller alone would rebuild the divergence
#320 removed.
"""
from __future__ import annotations

import unicodedata

import run_logic_check as rlc
from common import policy_row_matches

NFD = unicodedata.normalize("NFD", "한글")
NFC = unicodedata.normalize("NFC", "한글")

_NFD_INFERRED = {"needs_review": {(NFD, "low_conf"), ("Carol", "stale")}}


def _router_rows(predicate, line, inferred):
    """The rows ask_router.evaluate keeps — via the shared predicate it calls."""
    from common import _query_args as query_args

    args = query_args(line)
    return [
        list(row)
        for row in sorted(inferred.get(predicate, set()))
        if policy_row_matches(args, row)
    ]


class TestPinMeetsRowAcrossNormalisation:
    def test_the_two_spellings_are_not_equal_raw(self):
        """Guards the premise: without folding these strings simply differ."""
        assert NFD != NFC

    def test_nfc_query_finds_an_nfd_row(self):
        line = rlc.policy_result_line("needs_review", f'needs_review("{NFC}", R)?', _NFD_INFERRED)
        assert "1 rows" in line, f"fabricated negative about a subject with rows: {line}"
        assert "low_conf" in line, line

    def test_nfd_query_finds_an_nfc_row(self):
        """The reverse direction: neither spelling is privileged."""
        inferred = {"needs_review": {(NFC, "low_conf"), ("Carol", "stale")}}
        line = rlc.policy_result_line("needs_review", f'needs_review("{NFD}", R)?', inferred)
        assert "1 rows" in line, line

    def test_folding_does_not_widen_the_pin(self):
        """The fold must not drag in other subjects' rows."""
        line = rlc.policy_result_line("needs_review", f'needs_review("{NFC}", R)?', _NFD_INFERRED)
        assert "stale" not in line, f"Carol's reason attributed to 한글: {line}"


class TestReportAgreesWithRouterAcrossNormalisation:
    def test_report_and_router_agree_and_are_both_right(self):
        line = f'needs_review("{NFC}", R)?'
        expected = len(_router_rows("needs_review", line, _NFD_INFERRED))
        rendered = rlc.policy_result_line("needs_review", line, _NFD_INFERRED)
        assert expected == 1, f"router fabricated a negative: {expected} rows"
        assert f"{expected} rows" in rendered, (
            f"report/router divergence: router={expected}, report={rendered!r}"
        )
