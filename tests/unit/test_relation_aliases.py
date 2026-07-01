# SPDX-License-Identifier: Apache-2.0
"""Unit tests for relation_aliases() parser, surface_variants(), and collision guard."""
from __future__ import annotations

import unicodedata

import pytest

import common


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_alias_file(tmp_path, text: str):
    """Write text to policy/relation-aliases.md under tmp_path and return tmp_path."""
    policy = tmp_path / "policy"
    policy.mkdir(parents=True, exist_ok=True)
    (policy / "relation-aliases.md").write_text(text, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class TestRelationAliasesParse:
    def test_parses_valid_file(self, tmp_path):
        _write_alias_file(tmp_path, (
            "# Relation aliases\n"
            "- `게재연도` -> `published_year`\n"
            "- `publication_year` -> `published_year`\n"
            "- `year` -> `published_year`\n"
        ))
        aliases = common.relation_aliases(tmp_path)
        assert aliases == {
            "게재연도": "published_year",
            "publication_year": "published_year",
            "year": "published_year",
        }

    def test_absent_file_returns_empty(self, tmp_path):
        # No policy/relation-aliases.md created
        (tmp_path / "policy").mkdir(parents=True, exist_ok=True)
        aliases = common.relation_aliases(tmp_path)
        assert aliases == {}

    def test_absent_policy_dir_returns_empty(self, tmp_path):
        # Not even a policy/ directory
        aliases = common.relation_aliases(tmp_path)
        assert aliases == {}

    def test_blank_lines_skipped(self, tmp_path):
        _write_alias_file(tmp_path, "\n\n- `a` -> `b`\n\n")
        assert common.relation_aliases(tmp_path) == {"a": "b"}

    def test_hash_comment_lines_skipped(self, tmp_path):
        _write_alias_file(tmp_path, (
            "# this is a comment\n"
            "- `a` -> `b`\n"
            "# another comment\n"
        ))
        assert common.relation_aliases(tmp_path) == {"a": "b"}

    def test_star_bullet_accepted(self, tmp_path):
        _write_alias_file(tmp_path, "* `a` -> `b`\n")
        assert common.relation_aliases(tmp_path) == {"a": "b"}

    def test_no_bullet_accepted(self, tmp_path):
        _write_alias_file(tmp_path, "`a` -> `b`\n")
        assert common.relation_aliases(tmp_path) == {"a": "b"}

    def test_line_with_not_two_backtick_groups_skipped(self, tmp_path):
        # Only one group — malformed line, should be silently skipped
        _write_alias_file(tmp_path, (
            "- `only_one_group`\n"
            "- `a` -> `b`\n"
        ))
        assert common.relation_aliases(tmp_path) == {"a": "b"}

    def test_missing_arrow_not_parsed_as_mapping(self, tmp_path):
        # Two backtick groups but NO `->` between them must NOT become a mapping
        # (the arrow is required — two stray backticks are not a declaration).
        _write_alias_file(tmp_path, (
            "- `a` `b`\n"
            "- `x` -> `y`\n"
        ))
        aliases = common.relation_aliases(tmp_path)
        assert "a" not in aliases
        assert aliases == {"x": "y"}

    def test_wrong_arrow_not_parsed_as_mapping(self, tmp_path):
        # A non-`->` separator (e.g. `<-`) must NOT become a mapping.
        _write_alias_file(tmp_path, (
            "- `a` <- `b`\n"
            "- `x` -> `y`\n"
        ))
        aliases = common.relation_aliases(tmp_path)
        assert "a" not in aliases
        assert aliases == {"x": "y"}


# ---------------------------------------------------------------------------
# surface_variants
# ---------------------------------------------------------------------------

class TestSurfaceVariants:
    def test_returns_all_raws_for_canonical(self, tmp_path):
        _write_alias_file(tmp_path, (
            "- `게재연도` -> `published_year`\n"
            "- `publication_year` -> `published_year`\n"
            "- `year` -> `published_year`\n"
        ))
        aliases = common.relation_aliases(tmp_path)
        assert common.surface_variants("published_year", aliases) == {
            "게재연도", "publication_year", "year"
        }

    def test_returns_empty_for_unknown_canonical(self, tmp_path):
        _write_alias_file(tmp_path, "- `a` -> `b`\n")
        aliases = common.relation_aliases(tmp_path)
        assert common.surface_variants("unknown", aliases) == set()

    def test_returns_empty_for_empty_aliases(self):
        assert common.surface_variants("anything", {}) == set()


# ---------------------------------------------------------------------------
# Collision / validation errors
# ---------------------------------------------------------------------------

class TestRelationAliasesCollisions:
    def test_self_map_raises(self, tmp_path):
        _write_alias_file(tmp_path, "- `same` -> `same`\n")
        with pytest.raises(common.FactlogError, match="self-map"):
            common.relation_aliases(tmp_path)

    def test_raw_mapped_to_two_canonicals_raises(self, tmp_path):
        _write_alias_file(tmp_path, (
            "- `a` -> `b`\n"
            "- `a` -> `c`\n"
        ))
        with pytest.raises(common.FactlogError, match="mapped to both"):
            common.relation_aliases(tmp_path)

    def test_raw_is_also_canonical_raises(self, tmp_path):
        # `b` is a raw that maps to `c`, and `a` maps to `b` making `b` a canonical
        _write_alias_file(tmp_path, (
            "- `a` -> `b`\n"
            "- `b` -> `c`\n"
        ))
        with pytest.raises(common.FactlogError, match="alias chains are not allowed"):
            common.relation_aliases(tmp_path)

    def test_duplicate_raw_same_canonical_allowed(self, tmp_path):
        # Same raw -> same canonical repeated is idempotent, not an error
        _write_alias_file(tmp_path, (
            "- `a` -> `b`\n"
            "- `a` -> `b`\n"
        ))
        aliases = common.relation_aliases(tmp_path)
        assert aliases == {"a": "b"}


# ---------------------------------------------------------------------------
# NFC normalization (macOS-authored NFD files must match NFC CSV relations)
# ---------------------------------------------------------------------------

class TestRelationAliasesNFC:
    """A KB file authored on macOS is often NFD (decomposed) Hangul, but the CSV
    relations cmd_provenance compares against are NFC-normalized. So the parsed
    keys/values MUST be NFC or the canonical lookup and surface match silently
    miss for exactly the Korean predicates this feature targets (#188)."""

    def test_nfd_key_normalized_to_nfc(self, tmp_path):
        nfd = unicodedata.normalize("NFD", "게재연도")
        assert nfd != unicodedata.normalize("NFC", "게재연도")  # sanity: forms differ
        _write_alias_file(tmp_path, f"- `{nfd}` -> `published_year`\n")
        key = next(iter(common.relation_aliases(tmp_path)))
        assert unicodedata.is_normalized("NFC", key)
        assert key == unicodedata.normalize("NFC", "게재연도")

    def test_nfd_canonical_normalized_to_nfc(self, tmp_path):
        nfd = unicodedata.normalize("NFD", "게재연도")
        _write_alias_file(tmp_path, f"- `publication_year` -> `{nfd}`\n")
        canonical = common.relation_aliases(tmp_path)["publication_year"]
        assert unicodedata.is_normalized("NFC", canonical)
        assert canonical == unicodedata.normalize("NFC", "게재연도")

    def test_nfc_query_matches_nfd_authored_surface(self, tmp_path):
        # A canonical queried in NFC finds an NFD-authored surface variant.
        nfd = unicodedata.normalize("NFD", "게재연도")
        _write_alias_file(tmp_path, f"- `{nfd}` -> `published_year`\n")
        aliases = common.relation_aliases(tmp_path)
        assert unicodedata.normalize("NFC", "게재연도") in common.surface_variants(
            "published_year", aliases
        )
