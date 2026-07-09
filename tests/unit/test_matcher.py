# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the title+author+year fallback matcher (#75, part 2 of #66).

These pin the behaviour the spike (``tools/spike_fallback_precision.py``) measured
and — crucially — the ONE place production must diverge from it: the compound
surname the spike deliberately left broken (the #45 bug class) must fold the same
way from both serializations here.
"""
from __future__ import annotations

from factlog.integrations.common import matcher
from factlog.integrations.common.matcher import (
    TITLE_SIMILARITY_THRESHOLD,
    YEAR_TOLERANCE,
    MatchInput,
    first_surname,
    normalize_title,
    score_pair,
    surname,
    surnames_agree,
    title_similarity,
    years_agree,
)


class TestThresholdConstant:
    def test_threshold_is_080(self):
        # H3: 0.80, a recall knob (precision is flat 0.50-1.00). Never 0.84.
        assert TITLE_SIMILARITY_THRESHOLD == 0.80

    def test_constant_comment_cites_the_harmful_rate_not_precision(self):
        # The docstring must argue from 2/86 (2.3%), not the retracted "0.84".
        import inspect
        src = inspect.getsource(matcher)
        assert "2 of 86" in src or "2/86" in src
        assert "2.3%" in src
        # The retracted precision figure must not be cited anywhere in the module.
        assert "0.84" not in src


class TestNormalizeTitle:
    def test_latex_math_and_commands_stripped(self):
        assert normalize_title(r"Sorting in $O(n\log n)$ Time") == "sorting in time"
        assert normalize_title(r"A $\mathcal{O}(1)$ Data Structure") == "a data structure"

    def test_accent_fold_and_lowercase(self):
        assert normalize_title("Étude Française") == "etude francaise"

    def test_subtitle_colon_erased_so_leading_tokens_shared(self):
        full = "BERT: Pre-training of Deep Bidirectional Transformers"
        assert normalize_title(full).startswith("bert pre training")
        # The acronym-only record scores far below threshold against the full title.
        assert title_similarity("BERT", full) < TITLE_SIMILARITY_THRESHOLD

    def test_none_and_empty(self):
        assert normalize_title(None) == ""
        assert normalize_title("") == ""


class TestTitleSimilarity:
    def test_identical_titles_score_one(self):
        assert title_similarity("A Study of X", "A Study of X") == 1.0

    def test_two_empty_titles_score_one(self):
        assert title_similarity("", "") == 1.0

    def test_one_empty_scores_zero(self):
        assert title_similarity("A Study", "") == 0.0

    def test_punctuation_insensitive(self):
        assert title_similarity("Deep Learning!", "deep learning") == 1.0


class TestSurname:
    def test_family_given_comma_form(self):
        assert surname("Vaswani, Ashish") == "vaswani"

    def test_given_family_display_form(self):
        assert surname("Ashish Vaswani") == "vaswani"

    def test_both_serializations_agree_simple(self):
        assert surname("Vaswani, Ashish") == surname("Ashish Vaswani")

    def test_compound_surname_both_serializations_agree(self):
        # The #45 bug the spike left exposed: these disagreed there. They must agree.
        assert surname("van der Berg, Jan") == "vanderberg"
        assert surname("Jan van der Berg") == "vanderberg"
        assert surname("van der Berg, Jan") == surname("Jan van der Berg")

    def test_compound_surname_does_not_break_a_plain_two_token_name(self):
        assert surname("Faronius, Håkan Karlsson") == "faronius"
        assert surname("Håkan Karlsson Faronius") == "faronius"

    def test_particle_run_never_swallows_the_given_name(self):
        # Even a given name that looks like a particle is not eaten (index 0 guard).
        assert surname("Della Rossi") == "rossi"

    def test_et_al_sentinel_blanks(self):
        assert surname("et al.") == ""
        assert surname("and others") == ""

    def test_non_ascii_folds(self):
        assert surname("François Fleuret") == "fleuret"
        assert surname("Kyunghyun Cho") == "cho"

    def test_empty_and_none(self):
        assert surname("") == ""
        assert surname(None) == ""

    def test_first_surname_reads_only_first_author(self):
        assert first_surname(("Ada Lovelace", "Alan Turing")) == "lovelace"
        assert first_surname(()) == ""


class TestSurnamesAgree:
    def test_agree_across_serializations(self):
        assert surnames_agree(("Jan van der Berg",), ("van der Berg, Jan",))

    def test_empty_surname_never_agrees(self):
        assert not surnames_agree(("et al.",), ("et al.",))
        assert not surnames_agree((), ("Ada Lovelace",))


class TestYearsAgree:
    def test_within_tolerance(self):
        assert years_agree(2023, 2024, YEAR_TOLERANCE)
        assert years_agree(2024, 2023, YEAR_TOLERANCE)
        assert years_agree(2023, 2023, YEAR_TOLERANCE)

    def test_outside_tolerance(self):
        assert not years_agree(2020, 2023, YEAR_TOLERANCE)

    def test_none_never_agrees(self):
        assert not years_agree(None, 2023, YEAR_TOLERANCE)
        assert not years_agree(2023, None, YEAR_TOLERANCE)


class TestScorePair:
    def _mk(self, author="Zonghai Yao", year=2025, title="A Study of X"):
        return MatchInput(author, year, title)

    def test_gate_blocks_on_surname_mismatch(self):
        assert score_pair(self._mk(author="Ada Lovelace"), self._mk()) is None

    def test_gate_blocks_on_year_mismatch(self):
        assert score_pair(self._mk(year=2019), self._mk(year=2025)) is None

    def test_gate_blocks_on_missing_year(self):
        assert score_pair(self._mk(year=None), self._mk()) is None

    def test_returns_title_jaccard_when_gate_passes(self):
        assert score_pair(self._mk(), self._mk()) == 1.0

    def test_adjacent_year_passes_gate(self):
        # The real fallback case: preprint 2025 vs publication 2026, same title/author.
        score = score_pair(self._mk(year=2025), self._mk(year=2026))
        assert score == 1.0
