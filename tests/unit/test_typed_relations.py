# SPDX-License-Identifier: Apache-2.0
"""Unit tests for typed-relations.md parsing + validation (#118)."""
from __future__ import annotations

import unicodedata

import common
import pytest

EXAMPLE = (
    "# typed relations\n"
    "- `정식_운영` : date    as launch_date     # a date -> yyyymmdd\n"
    "- `버전`      : number  as version_num\n"
    "- `우선순위`  : ordinal as priority_rank\n"
)


class TestParse:
    def test_parses_example(self):
        specs = common._parse_typed_relations(EXAMPLE)
        assert specs["정식_운영"] == common.TypedRelSpec("date", "launch_date")
        assert specs["버전"].type == "number" and specs["버전"].alias == "version_num"
        assert set(specs) == {"정식_운영", "버전", "우선순위"}

    def test_backtick_name_with_spaces(self):
        specs = common._parse_typed_relations("- `정식 운영` : date as d1\n")
        assert "정식 운영" in specs

    def test_comments_and_blanks_ignored(self):
        specs = common._parse_typed_relations("\n# only comments\n   \n")
        assert specs == {}

    def test_nfc_normalizes_name(self):
        nfd = unicodedata.normalize("NFD", "정식_운영")
        assert nfd != "정식_운영"  # precondition: input is decomposed
        specs = common._parse_typed_relations(f"- `{nfd}` : date as d1\n")
        assert "정식_운영" in specs  # stored NFC


class TestValidation:
    def test_unknown_type_warns_and_skips(self, capsys):
        specs = common._parse_typed_relations("- `x` : weirdtype as ax\n- `y` : date as ay\n")
        assert set(specs) == {"y"}
        assert "unknown type" in capsys.readouterr().err

    def test_malformed_line_warns_and_skips(self, capsys):
        specs = common._parse_typed_relations("- `x` : date\n- `y` : date as ay\n")  # missing 'as alias'
        assert set(specs) == {"y"}
        assert "malformed" in capsys.readouterr().err

    def test_non_ascii_alias_errors(self):
        with pytest.raises(common.FactlogError, match="ASCII"):
            common._parse_typed_relations("- `x` : date as 별칭\n")

    def test_alias_collides_with_builtin(self):
        with pytest.raises(common.FactlogError, match="collides"):
            common._parse_typed_relations("- `x` : date as path\n")

    def test_alias_collides_with_reserved_relation(self):
        with pytest.raises(common.FactlogError, match="collides"):
            common._parse_typed_relations("- `x` : date as existing_rel\n", reserved={"existing_rel"})

    def test_duplicate_alias_errors(self):
        with pytest.raises(common.FactlogError, match="duplicate"):
            common._parse_typed_relations("- `x` : date as a\n- `y` : number as a\n")


class TestKbContext:
    def _kb(self, tmp_path, *, typed=None, attrs=None):
        (tmp_path / "policy").mkdir(parents=True, exist_ok=True)
        if typed is not None:
            (tmp_path / "policy" / "typed-relations.md").write_text(typed, encoding="utf-8")
        if attrs is not None:
            (tmp_path / "policy" / "attribute-relations.md").write_text(attrs, encoding="utf-8")
        return common.KbContext.for_root(tmp_path)

    def test_absent_file_is_empty(self, tmp_path):
        assert self._kb(tmp_path).typed_relations() == {}

    def test_all_comments_file_is_empty(self, tmp_path):
        ctx = self._kb(tmp_path, typed="# nothing declared\n# - `x` : date as ax\n")
        assert ctx.typed_relations() == {}

    def test_reads_its_own_root(self, tmp_path):
        ctx = self._kb(tmp_path, typed=EXAMPLE, attrs="- `정식_운영`\n- `버전`\n- `우선순위`\n")
        specs = ctx.typed_relations()
        assert specs["정식_운영"].alias == "launch_date"

    def test_warns_when_not_in_attribute_relations(self, tmp_path, capsys):
        ctx = self._kb(tmp_path, typed="- `정식_운영` : date as launch_date\n", attrs="")
        ctx.typed_relations()
        assert "attribute-relations.md" in capsys.readouterr().err
