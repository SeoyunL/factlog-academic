# SPDX-License-Identifier: Apache-2.0
"""Regression tests: unicode line separators in a fact must survive accepted.dl
round-trip (#255).

`dl_atom` serialises a fact to a single physical `.dl` line, and the wirelog
engine parses a raw U+2028 / U+2029 / U+0085 inside a quoted string correctly.
But `_load_accepted_facts_from` split the file with `str.splitlines()`, which —
unlike `.split("\\n")` — also breaks on U+2028/U+2029/U+0085. So a single fact
whose object contains one of those code points was cut into two physical lines,
neither matching `RELATION_FACT_RE`, and `load_accepted_facts` raised
`FactlogError` for the WHOLE file — one bad fact bricked every consumer
(check / ask / entity_set / status). These characters appear routinely in text
copied from PDFs and the web, so a legitimately-extracted object could brick a KB.
"""
from __future__ import annotations

import common


class TestAcceptedDlLineSeparators:
    def _roundtrip(self, value):
        # dl_atom -> a single .dl line (+ trailing newline, as compile_facts writes) ->
        # parse back through the accepted.dl loader.
        return common.dl_atom(
            {"subject": "S", "relation": "note", "object": value}
        )

    def test_line_separator_u2028_survives_roundtrip(self, tmp_path):
        value = f"line one{chr(0x2028)}line two"
        adl = tmp_path / "accepted.dl"
        adl.write_text(self._roundtrip(value) + "\n", encoding="utf-8")
        facts = common._load_accepted_facts_from(adl)
        assert len(facts) == 1
        assert facts[0]["object"] == value

    def test_paragraph_separator_u2029_survives_roundtrip(self, tmp_path):
        value = f"para one{chr(0x2029)}para two"
        adl = tmp_path / "accepted.dl"
        adl.write_text(self._roundtrip(value) + "\n", encoding="utf-8")
        facts = common._load_accepted_facts_from(adl)
        assert facts[0]["object"] == value

    def test_next_line_u0085_survives_roundtrip(self, tmp_path):
        value = f"nel one{chr(0x0085)}nel two"
        adl = tmp_path / "accepted.dl"
        adl.write_text(self._roundtrip(value) + "\n", encoding="utf-8")
        facts = common._load_accepted_facts_from(adl)
        assert facts[0]["object"] == value

    def test_ordinary_newline_object_is_still_one_line_in_dl(self, tmp_path):
        # Sanity anchor: a real '\n' in an object is escaped by dl_string (json),
        # so it stays one physical line and round-trips — unchanged by the fix.
        value = "has\nnewline"
        adl = tmp_path / "accepted.dl"
        adl.write_text(self._roundtrip(value) + "\n", encoding="utf-8")
        facts = common._load_accepted_facts_from(adl)
        assert facts[0]["object"] == value
