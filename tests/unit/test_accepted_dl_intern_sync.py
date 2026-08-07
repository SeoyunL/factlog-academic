# SPDX-License-Identifier: Apache-2.0
"""`_load_accepted_facts_from` must not fold atom identity (#342).

`run_wirelog` hands the engine the accepted.dl FILE TEXT and interns symbols
from the list this loader returns. Those two must describe the same atom set.
When the loader folded canonically equivalent spellings, the losing spelling
stayed in the program the engine parsed but never reached `session.intern`, and
`decode_wirelog_value` fell through to the bare intern id — so
`facts/logic_report.txt`, the deterministic trust surface, named an entity `3`.

No hand-editing is needed to reach this: any accepted.dl compiled by a release
before the atom fold carries both spellings, so it fires on upgrade, before the
KB is ever recompiled.

Identity folding belongs at COMPILE time (`factlog/compile_facts.py`), which is
where accepted.dl is written and where nothing downstream can desynchronize from
it. This loader collapses byte-identical triples only — equal bytes intern to one
symbol, so that collapse cannot lose an interned name.
"""
from __future__ import annotations

import unicodedata

import common


def _nfc(value):
    return unicodedata.normalize("NFC", value)


def _nfd(value):
    return unicodedata.normalize("NFD", value)


def _write(tmp_path, *atoms):
    adl = tmp_path / "accepted.dl"
    adl.write_text("\n".join(atoms) + "\n", encoding="utf-8")
    return adl


class TestLoaderKeepsEverySpellingTheEngineCanSee:
    def test_both_spellings_survive_the_loader(self, tmp_path):
        # Exactly what a pre-fold release compiled for a mixed-spelling KB.
        adl = _write(
            tmp_path,
            f'relation("{_nfd("부산항만공사")}", "관할", "부산항").',
            f'relation("{_nfc("부산항만공사")}", "관할", "부산항").',
        )
        rows = common._load_accepted_facts_from(adl)
        assert len(rows) == 2
        assert {row["subject"] for row in rows} == {
            _nfd("부산항만공사"),
            _nfc("부산항만공사"),
        }

    def test_every_atom_in_the_file_is_available_to_intern(self, tmp_path):
        # The property that actually matters: every symbol the engine can parse
        # out of the file text is present in the rows run_wirelog interns from.
        atoms = [
            f'relation("{_nfd("부산항만공사")}", "관할", "{_nfc("부산항")}").',
            f'relation("{_nfc("부산항만공사")}", "관할", "{_nfd("부산항")}").',
        ]
        rows = common._load_accepted_facts_from(_write(tmp_path, *atoms))
        internable = {row[axis] for row in rows for axis in ("subject", "relation", "object")}
        for spelling in (
            _nfd("부산항만공사"), _nfc("부산항만공사"), _nfd("부산항"), _nfc("부산항"),
        ):
            assert spelling in internable, f"{ascii(spelling)} would decode as a bare intern id"

    def test_byte_identical_duplicates_still_collapse(self, tmp_path):
        # The loader's actual job — hygiene for a stale or hand-edited file.
        # Equal bytes intern to one symbol, so collapsing them desynchronizes
        # nothing.
        atom = 'relation("A", "r", "B").'
        rows = common._load_accepted_facts_from(_write(tmp_path, atom, atom))
        assert len(rows) == 1

    def test_loader_does_not_rewrite_a_spelling(self, tmp_path):
        # It must hand back what the file says, not a normalized view of it —
        # otherwise the interned name and the parsed atom differ again.
        adl = _write(tmp_path, f'relation("{_nfd("부산항만공사")}", "관할", "부산항").')
        rows = common._load_accepted_facts_from(adl)
        assert rows[0]["subject"] == _nfd("부산항만공사")
