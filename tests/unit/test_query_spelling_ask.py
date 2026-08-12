# SPDX-License-Identifier: Apache-2.0
"""``ask`` must answer a query written in either spelling of a mixed-spelling KB.

``dedup_engine_atoms`` collapses canonically equivalent atoms and picks ONE
spelling per value KB-wide, so an ``accepted.dl`` can end up addressable by no
single normalization form: in the KB below 삼성 and 이재용 land composed and
서울 stays decomposed. The reviewer's reproduction, kept here as rows so the
pins do not need a compiled KB on disk.

``count`` is the sharpest case and the reason ``evaluate`` is fixed on its own:
it answered ``0`` for a subject the KB has a fact about, and the router presents
that as a verified aggregate — the output a reader is least able to check by eye.

FACTLOG_ROOT is bound to a throwaway dir by the repo-root ``conftest.py`` before
any tool module is imported, so ``relation_aliases()`` in the count branch reads
that dir and not the developer's real knowledge base.
"""
from __future__ import annotations

import unicodedata

import pytest

import ask_router


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def nfd(value: str) -> str:
    return unicodedata.normalize("NFD", value)


def rows(*triples: tuple[str, str, str]) -> list[dict[str, str]]:
    return [{"subject": s, "relation": r, "object": o} for s, r, o in triples]


MIXED = rows(
    (nfc("삼성"), "대표", nfc("이재용")),
    (nfc("이재용"), "거주", nfd("서울")),
)


class TestEvaluateResolvesSpellings:
    def test_count_reaches_the_fact_in_either_spelling(self) -> None:
        """RED before this fix: the decomposed form returned
        ``{"rows": [["0"]], "count": 0}`` — a verified-looking zero for a fact
        the KB holds."""
        for subject in (nfd("삼성"), nfc("삼성")):
            result = ask_router.evaluate(f'count("{subject}", "대표")?', MIXED)
            assert result == {"rows": [["1"]], "count": 1}, subject

    def test_path_joins_across_the_spelling_seam(self) -> None:
        """The two endpoints are stored in DIFFERENT forms, so no single form a
        user could type addressed both. Both single-form queries must now find
        the same two-hop path."""
        for form in (nfd, nfc):
            result = ask_router.evaluate(
                f'path("{form("삼성")}", "{form("서울")}")?', MIXED
            )
            assert result["count"] == 1, form
            assert result["rows"] == [[nfc("삼성"), nfc("이재용"), nfd("서울")]], form

    @pytest.mark.parametrize("form", [nfc, nfd])
    def test_relation_branch_is_a_guard_not_evidence(self, form) -> None:
        """GUARD, not evidence — both of these passed before the fix, measured.
        ``evaluate_relation`` already folds all three positions through
        ``canonical_value``, so the relation branch was never the broken one and
        no ``ask`` + ``relation`` assertion can be evidence for this change. They
        are pinned so a later refactor cannot quietly lose what already worked —
        in particular, so resolving the constants cannot narrow a match the fold
        used to make."""
        assert ask_router.evaluate(
            f'relation("{form("이재용")}", "거주", "{form("서울")}")?', MIXED
        )["count"] == 1
        assert ask_router.evaluate(
            f'relation("{form("삼성")}", "대표", O)?', MIXED
        )["rows"] == [[nfc("삼성"), "대표", nfc("이재용")]]

    @pytest.mark.parametrize("form", [nfc, nfd])
    def test_uniform_kb_is_unaffected(self, form) -> None:
        """GUARD, not evidence. A KB written one way resolves every constant to
        itself, so nothing about its answers may change."""
        uniform = rows(
            (form("삼성"), "대표", form("이재용")),
            (form("이재용"), "거주", form("서울")),
        )
        assert ask_router.evaluate(f'count("{form("삼성")}", "대표")?', uniform) == {
            "rows": [["1"]],
            "count": 1,
        }
        assert ask_router.evaluate(
            f'path("{form("삼성")}", "{form("서울")}")?', uniform
        )["count"] == 1

    def test_absent_fact_is_still_a_verified_negative(self) -> None:
        """Resolution must not invent reach. A subject the KB has no such
        relation for still answers 0, in either spelling."""
        for subject in (nfd("삼성"), nfc("삼성")):
            assert ask_router.evaluate(f'count("{subject}", "거주")?', MIXED) == {
                "rows": [["0"]],
                "count": 0,
            }
