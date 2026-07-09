# SPDX-License-Identifier: Apache-2.0
"""Unit tests for inverted-index abstract restoration (#51, spec §5.4)."""
from __future__ import annotations

import pytest

from factlog.integrations.openalex.abstract_util import (
    has_abstract,
    index_is_complete,
    restore_abstract,
)


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


class TestIndexIsComplete:
    """Feeds the `abstract_complete` front-matter flag."""

    def test_true_for_a_contiguous_index(self):
        assert index_is_complete({"a": [0], "b": [1], "c": [2]}) is True

    def test_true_when_a_word_repeats_at_distinct_positions(self):
        assert index_is_complete({"the": [0, 2], "cat": [1], "hat": [3]}) is True

    def test_false_for_a_gap(self):
        assert index_is_complete({"a": [0], "b": [1], "d": [3]}) is False

    def test_false_when_the_index_does_not_start_at_zero(self):
        assert index_is_complete({"b": [1], "c": [2]}) is False

    def test_false_for_a_duplicate_position(self):
        # Two words claiming one slot means restore_abstract dropped one.
        assert index_is_complete({"first": [0], "second": [0]}) is False

    @pytest.mark.parametrize("empty", [None, {}, "", [], 0])
    def test_false_for_a_missing_or_malformed_index(self, empty):
        assert index_is_complete(empty) is False

    def test_false_when_every_entry_is_junk(self):
        assert index_is_complete({"bad": ["x"], "worse": None}) is False

    def test_junk_entries_do_not_make_a_complete_index_look_gapped(self):
        # The junk position is skipped by both functions, so they agree.
        index = {"a": [0], "b": [1], "junk": ["x"]}
        assert restore_abstract(index) == "a b"
        assert index_is_complete(index) is True


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
