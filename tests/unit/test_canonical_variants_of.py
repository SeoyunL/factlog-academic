# SPDX-License-Identifier: Apache-2.0
"""Unit tests for canonical_variants_of() — the DRY helper (#242) that consolidates
the canonical reverse-lookup + NFC normalization shared by classify_query,
_relation_match_count, and ask_router.

These tests are engine-free and read-only — the aliases dict (raw -> canonical) is
built directly, so no policy file / accepted.dl is required.
"""
from __future__ import annotations

import unicodedata

import factlog.common as fcommon


class TestCanonicalVariantsOf:
    """Reverse lookup of surface variants for a declared canonical relation."""

    def test_declared_canonical_returns_variant_set(self):
        """A canonical returns exactly the raw predicates that map to it."""
        aliases = {
            "게재연도": "published_year",
            "publication_year": "published_year",
            "저자": "author",
        }
        assert fcommon.canonical_variants_of("published_year", aliases) == {
            "게재연도",
            "publication_year",
        }

    def test_nfd_input_relation_matches_nfc_alias_value(self):
        """An NFD-composed relation arg still matches an NFC canonical alias value.

        relation_aliases() NFC-normalizes on load, so both alias keys and values are
        NFC. A query may arrive NFD-composed; the helper NFC-normalizes the relation
        arg so the reverse lookup against the NFC canonical value succeeds.
        """
        canonical_nfc = unicodedata.normalize("NFC", "게재연도")  # NFC canonical value
        aliases = {"publication_year": canonical_nfc, "pub_yr": canonical_nfc}
        query_nfd = unicodedata.normalize("NFD", "게재연도")  # NFD-composed query name
        assert query_nfd != canonical_nfc  # precondition: forms genuinely differ
        # Without normalization an NFD arg would miss the NFC value; the helper fixes it.
        assert fcommon.canonical_variants_of(query_nfd, aliases) == {
            "publication_year",
            "pub_yr",
        }

    def test_unknown_relation_returns_empty_set(self):
        """A relation that is not a declared canonical yields an empty (falsy) set."""
        aliases = {"게재연도": "published_year"}
        result = fcommon.canonical_variants_of("not_a_canonical", aliases)
        assert result == set()
        assert not result  # the boolean "not a declared canonical" use

    def test_empty_aliases_returns_empty_set(self):
        """No aliases -> empty set regardless of the relation name."""
        assert fcommon.canonical_variants_of("published_year", {}) == set()
