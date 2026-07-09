# SPDX-License-Identifier: Apache-2.0
"""Unit tests for inverted-index abstract restoration (#51, spec §5.4)."""
from __future__ import annotations

import pytest

from factlog.integrations.openalex.abstract_util import has_abstract, restore_abstract


class TestRestoreAbstract:
    def test_restores_words_in_position_order(self):
        index = {"Current": [0], "advances": [1], "in": [2], "AI": [3]}
        assert restore_abstract(index) == "Current advances in AI"

    def test_ignores_json_key_order(self):
        index = {"AI": [3], "Current": [0], "in": [2], "advances": [1]}
        assert restore_abstract(index) == "Current advances in AI"

    def test_repeated_word_occupies_each_of_its_positions(self):
        index = {"the": [0, 2], "cat": [1], "hat": [3]}
        assert restore_abstract(index) == "the cat the hat"

    def test_sparse_positions_drop_tokens_without_raising(self):
        # Real payloads have small gaps (W2913668833 is missing 479, 482, 491).
        # Walking sorted(positions) rather than range(max) keeps this total.
        index = {"a": [0], "b": [1], "d": [3]}
        assert restore_abstract(index) == "a b d"

    def test_index_not_starting_at_zero_is_still_restored(self):
        assert restore_abstract({"b": [1], "c": [2]}) == "b c"

    def test_duplicate_position_keeps_the_first_word_deterministically(self):
        # Not observed live, but the same payload must always restore the same
        # text (P3), so collisions resolve first-wins rather than last-wins.
        assert restore_abstract({"first": [0], "second": [0]}) == "first"

    @pytest.mark.parametrize("empty", [None, {}, "", [], 0])
    def test_missing_or_empty_index_yields_empty_string(self, empty):
        # 37 of 100 live works had no abstract at all — a normal case, not an error.
        assert restore_abstract(empty) == ""

    def test_non_dict_index_yields_empty_string(self):
        assert restore_abstract(["not", "an", "index"]) == ""

    @pytest.mark.parametrize("bad_positions", ["0", None, 3, {"0": 1}])
    def test_non_list_positions_are_skipped(self, bad_positions):
        assert restore_abstract({"ok": [0], "bad": bad_positions}) == "ok"

    @pytest.mark.parametrize("bad_position", ["1", None, -1, 1.5, True])
    def test_non_index_positions_are_skipped(self, bad_position):
        # `True` is an int subclass and would otherwise land at index 1.
        assert restore_abstract({"ok": [0], "bad": [bad_position]}) == "ok"

    def test_non_string_words_are_skipped(self):
        assert restore_abstract({"ok": [0], 7: [1], None: [2]}) == "ok"

    def test_entirely_malformed_index_yields_empty_string(self):
        assert restore_abstract({"bad": ["x"], "worse": None}) == ""

    def test_restoration_is_stable_across_calls(self):
        index = {"b": [1], "a": [0], "c": [2, 4], "d": [3]}
        assert restore_abstract(index) == restore_abstract(index) == "a b c d c"


class TestHasAbstract:
    def test_true_for_a_populated_index(self):
        assert has_abstract({"abstract_inverted_index": {"a": [0]}}) is True

    @pytest.mark.parametrize("work", [{}, {"abstract_inverted_index": None},
                                      {"abstract_inverted_index": {}},
                                      {"abstract_inverted_index": []}])
    def test_false_when_absent_or_empty(self, work):
        assert has_abstract(work) is False

    def test_false_for_non_dict_work(self):
        assert has_abstract(None) is False
