# SPDX-License-Identifier: Apache-2.0
"""`ask`'s provenance annotation must survive the engine-atom fold (#342).

`fact_signals` and the renderer's lookup are two halves of one key space. Before
the atom fold both halves were raw triples and every atom matched: a mixed-spelling
KB rendered two identical-looking rows — wrong, but nothing was MISSING; every
source path and staleness marker was on screen.

Folding the atom at compile time while leaving this map raw moves only one half.
The compiled atom then finds no entry, and `render_engine_answer` falls to its
`[no extraction backing]` branch — which drops the source count, every backing
source path, and the `[stale: source missing]` marker for a fact that has all
three. For a provenance tool, losing a source is worse than showing it twice.

So both sides key on `common.engine_atom_key` / `common.fold_atom_triple`.
"""
from __future__ import annotations

import unicodedata

import common
from ask_router import render_engine_answer


def _nfc(value):
    return unicodedata.normalize("NFC", value)


def _nfd(value):
    return unicodedata.normalize("NFD", value)


def _fact(subject, relation, object_, source, confidence="0.90"):
    return {
        "subject": subject,
        "relation": relation,
        "object": object_,
        "source": source,
        "status": "confirmed",
        "confidence": confidence,
        "note": "",
    }


def _kb(tmp_path, *names):
    (tmp_path / "sources").mkdir(exist_ok=True)
    for name in names:
        (tmp_path / "sources" / name).write_text("x\n", encoding="utf-8")
    return tmp_path


class TestFactSignalsFoldsLikeTheAtom:
    def test_two_spellings_two_sources_report_one_atom_with_both(self, tmp_path):
        _kb(tmp_path, "a.md", "b.md")
        facts = [
            _fact("연구소", "소속", _nfc("한국대학교"), "sources/a.md"),
            _fact("연구소", "소속", _nfd("한국대학교"), "sources/b.md", "0.95"),
        ]
        signals = common.fact_signals(facts, root=tmp_path)
        assert len(signals) == 1
        entry = signals[common.engine_atom_key(common.dedup_engine_atoms(facts)[0])]
        assert entry["sources"] == 2
        assert entry["source_paths"] == ["sources/a.md", "sources/b.md"]
        assert entry["confidence"] == "0.95"

    def test_the_compiled_atom_finds_its_own_entry(self, tmp_path):
        # The exact desync: whatever dedup writes must resolve in this map.
        _kb(tmp_path, "a.md", "b.md")
        facts = [
            _fact(_nfc("삼성"), "대표", _nfd("이재용"), "sources/a.md"),
            _fact(_nfd("삼성"), "대표", _nfc("이재용"), "sources/b.md"),
        ]
        atom = common.dedup_engine_atoms(facts)[0]
        signals = common.fact_signals(facts, root=tmp_path)
        assert common.engine_atom_key(atom) in signals

    def test_staleness_is_not_lost_when_one_spelling_backs_it(self, tmp_path):
        # b.md is never created, so the NFD row's source has vanished.
        _kb(tmp_path, "a.md")
        facts = [
            _fact("연구소", "소속", _nfc("한국대학교"), "sources/a.md"),
            _fact("연구소", "소속", _nfd("한국대학교"), "sources/b.md"),
        ]
        signals = common.fact_signals(facts, root=tmp_path)
        assert len(signals) == 1
        assert next(iter(signals.values()))["stale"] is True


class TestRendererLooksUpThroughTheSameFold:
    def _signals(self, tmp_path):
        _kb(tmp_path, "a.md", "b.md")
        facts = [
            _fact("연구소", "소속", _nfc("한국대학교"), "sources/a.md"),
            _fact("연구소", "소속", _nfd("한국대학교"), "sources/b.md"),
        ]
        return common.fact_signals(facts, root=tmp_path)

    def test_engine_row_keeps_its_annotation(self, tmp_path):
        out = render_engine_answer(
            "relation('연구소', '소속', O)?",
            [["연구소", "소속", _nfc("한국대학교")]],
            self._signals(tmp_path),
            annotate_objects=True,
        )
        assert "sources: 2" in out
        assert "← sources/a.md" in out
        assert "← sources/b.md" in out
        assert "[no extraction backing]" not in out

    def test_a_decomposed_engine_row_still_finds_its_entry(self, tmp_path):
        # An accepted.dl written by an earlier release can hand the engine the
        # decomposed spelling; the annotation must not depend on which one.
        out = render_engine_answer(
            "relation('연구소', '소속', O)?",
            [["연구소", "소속", _nfd("한국대학교")]],
            self._signals(tmp_path),
            annotate_objects=True,
        )
        assert "sources: 2" in out
        assert "[no extraction backing]" not in out

    def test_a_genuinely_unbacked_row_is_still_marked(self, tmp_path):
        # The fold must not turn the [no extraction backing] branch into dead
        # code: a row with no candidate behind it still has to say so.
        out = render_engine_answer(
            "relation(S, R, O)?",
            [["없는것", "소속", "어딘가"]],
            self._signals(tmp_path),
            annotate_objects=True,
        )
        assert "[no extraction backing]" in out
