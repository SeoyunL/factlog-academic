# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the shared DOI prefix fold (#420).

The grammar and the prefix/suffix asymmetry used to live privately in
``source_writer``; #420 needed the same fold at the Zotero import boundary and
moved it here rather than copying it. These tests pin the shared contract, and
the last class pins that the two call sites really do share it — a second copy
would pass every other test in this file while drifting.
"""
from __future__ import annotations

from factlog.integrations.common.doi import fold_doi_prefix
from factlog.integrations.common.source_writer import normalize_cross_id


class TestPrefixIsFolded:
    def test_full_width_registrant_code_becomes_ascii(self):
        assert fold_doi_prefix("10.１２３４/abc") == "10.1234/abc"

    def test_ascii_value_is_unchanged(self):
        assert fold_doi_prefix("10.1234/abc") == "10.1234/abc"

    def test_other_decimal_scripts_fold_too(self):
        # `Nd` is wider than full-width forms, and NFKC would leave these alone.
        assert fold_doi_prefix("10.٢٠٢٠/abc") == "10.2020/abc"
        assert fold_doi_prefix("10.२०२०/abc") == "10.2020/abc"

    def test_subdivided_registrant_code_folds_whole(self):
        # DOI Handbook 2.2.2: a registrant may subdivide its code, and each part
        # is decimal. A grammar of `10\.[0-9]+` would reject this head and leave
        # a legitimate DOI unfolded.
        assert fold_doi_prefix("10.１０００.１０/abc") == "10.1000.10/abc"


class TestSuffixIsPreserved:
    def test_suffix_digits_are_not_folded(self):
        # The suffix is opaque under ISO 26324: respelling a character there
        # would name a different paper, not the same one differently.
        assert fold_doi_prefix("10.１２３４/abc１２") == "10.1234/abc１２"

    def test_only_the_first_slash_splits(self):
        # A suffix may contain slashes, and all of them are the opaque half.
        #
        # The prefix is full-width on purpose. With an all-ASCII prefix this
        # assertion passes under a `partition` -> `rpartition` mutant for the
        # wrong reason: the head becomes `10.1002/x`, fails the guard, and the
        # value comes back unchanged — which is what was expected anyway. A
        # prefix that must *change* makes the mutant produce an unfolded value,
        # so the assertion fails, as its name promises.
        assert fold_doi_prefix("10.１２３４/x/y") == "10.1234/x/y"
        assert fold_doi_prefix("10.１２３４/x/１") == "10.1234/x/１"

    def test_case_is_preserved(self):
        # Callers that want a comparison form lowercase around this; a caller
        # storing a value keeps the case its source spelled.
        assert fold_doi_prefix("10.1378/CHEST.128") == "10.1378/CHEST.128"


class TestUnrecognizedHeadIsReturnedUnchanged:
    def test_label_prefix_is_not_rewritten(self):
        # Folding this head *does* change it, so the value differs with and
        # without the guard: the assertion fails if the fullmatch check is
        # dropped, rather than passing for an unrelated reason.
        assert fold_doi_prefix("doi:10.１２３４/abc") == "doi:10.１２３４/abc"

    def test_url_wrapper_is_not_rewritten(self):
        assert (
            fold_doi_prefix("https://doi.org/10.１２３４/abc")
            == "https://doi.org/10.１２３４/abc"
        )

    def test_a_value_with_no_slash_is_returned_unchanged(self):
        # With no suffix to delimit it there is nothing separating a bare prefix
        # from junk that happens to start with digits.
        assert fold_doi_prefix("10.１２３４") == "10.１２３４"

    def test_empty_and_junk(self):
        assert fold_doi_prefix("") == ""
        assert fold_doi_prefix("not a doi/at all") == "not a doi/at all"


class TestTheJoinKeyDelegatesHere:
    """The single-source claim: ``normalize_cross_id`` is this fold + lowercase."""

    def test_join_key_differs_from_the_fold_only_by_case(self):
        for value in (
            "10.１２３４/abc",
            "10.1378/CHEST.128.6.3817",
            "10.１０００.１０/AbC",
            "doi:10.１２３４/abc",
            "10.１２３４",
            "10.1002/x/Y１",
        ):
            assert normalize_cross_id("doi", value) == fold_doi_prefix(value).lower()

    def test_lowercasing_first_would_give_the_same_answer(self):
        # Why the delegation is safe in either order: no decimal digit has a
        # case, so folding and lowercasing commute.
        for value in ("10.１２３４/AbC", "10.1378/CHEST.128"):
            assert fold_doi_prefix(value).lower() == fold_doi_prefix(value.lower())
