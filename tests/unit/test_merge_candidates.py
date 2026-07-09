# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the KB-level merge-candidate ledger (#75).

Mirrors the posture the provenance sidecar and arXiv check-log are tested to: the
naming rule, order-normalized pairing, idempotent add, byte determinism, a missing
file reading empty, and — the load-bearing part — every corrupt shape RAISING rather
than reading as empty (which would let the next write erase real candidates). No
ledger is written by production code in these tests; they exercise the module.
"""
from __future__ import annotations

import json

import pytest

from factlog.integrations.common import merge_candidates as mc
from factlog.integrations.common.merge_candidates import (
    LEGAL_STATES,
    MERGE_CANDIDATES_DIR,
    MERGE_CANDIDATES_NAME,
    SCHEMA_VERSION,
    STATE_PENDING,
    STATE_REJECTED,
    CandidatePair,
    MergeCandidates,
    MergeCandidatesError,
    add_candidate,
    candidates_path,
    read_candidates,
    write_candidates,
)

T = "2026-07-09T00:00:00Z"


def _pair(incoming=("openalex", "W_MEDRXIV"), existing=("openalex", "W_AAAI"),
          state=STATE_PENDING, score=1.0, recorded_at=T):
    return CandidatePair.create(incoming, existing, state=state, score=score,
                                recorded_at=recorded_at)


class TestPath:
    def test_naming_rule(self, tmp_path):
        assert candidates_path(tmp_path) == tmp_path / MERGE_CANDIDATES_DIR / MERGE_CANDIDATES_NAME

    def test_sibling_of_sources(self, tmp_path):
        # Beside sources/, never inside it — that is what hides it from enumeration.
        assert candidates_path(tmp_path).parent.name == "merge-candidates"


class TestOrderNormalization:
    def test_pair_is_order_independent(self):
        a = _pair(("openalex", "W2"), ("arxiv", "1"))
        b = _pair(("arxiv", "1"), ("openalex", "W2"))
        assert a.key == b.key

    def test_has_pair_order_independent(self):
        led = MergeCandidates()
        add_candidate(led, _pair(("openalex", "W2"), ("arxiv", "1")))
        assert led.has_pair(("arxiv", "1"), ("openalex", "W2"))
        assert led.has_pair(("openalex", "W2"), ("arxiv", "1"))

    def test_score_is_rounded_for_determinism(self):
        p = _pair(score=0.833333333)
        assert p.score == 0.8333


class TestAddIdempotent:
    def test_second_add_of_same_pair_is_noop(self):
        led = MergeCandidates()
        add_candidate(led, _pair())
        add_candidate(led, _pair())
        assert len(led.pairs) == 1

    def test_rejected_state_is_not_overwritten_by_pending(self):
        led = MergeCandidates()
        add_candidate(led, _pair(state=STATE_REJECTED))
        add_candidate(led, _pair(state=STATE_PENDING))  # same pair
        assert len(led.pairs) == 1
        assert led.pairs[0].state == STATE_REJECTED  # human's rejection survives


class TestRoundTripAndDeterminism:
    def test_round_trip(self, tmp_path):
        led = MergeCandidates()
        add_candidate(led, _pair(("openalex", "W2"), ("arxiv", "1")))
        add_candidate(led, _pair(("zotero", "K9"), ("openalex", "W2")))
        path = candidates_path(tmp_path)
        write_candidates(path, led)
        back = read_candidates(path)
        assert {p.key for p in back.pairs} == {p.key for p in led.pairs}

    def test_byte_determinism_independent_of_insertion_order(self, tmp_path):
        p1 = _pair(("openalex", "W2"), ("arxiv", "1"))
        p2 = _pair(("zotero", "K9"), ("openalex", "W2"))
        a = MergeCandidates()
        add_candidate(a, p1)
        add_candidate(a, p2)
        b = MergeCandidates()
        add_candidate(b, p2)
        add_candidate(b, p1)
        pa, pb = tmp_path / "a.json", tmp_path / "b.json"
        write_candidates(pa, a)
        write_candidates(pb, b)
        assert pa.read_bytes() == pb.read_bytes()

    def test_trailing_newline_and_indent(self, tmp_path):
        led = MergeCandidates()
        add_candidate(led, _pair())
        path = candidates_path(tmp_path)
        write_candidates(path, led)
        text = path.read_text()
        assert text.endswith("\n")
        assert "  " in text  # indent=2


class TestMissingFileReadsEmpty:
    def test_missing_file_is_empty_not_error(self, tmp_path):
        led = read_candidates(candidates_path(tmp_path))
        assert led.pairs == []
        assert led.schema_version == SCHEMA_VERSION


class TestNeverWritesAccepted:
    def test_accepted_is_not_a_legal_state(self):
        assert "accepted" not in LEGAL_STATES
        assert LEGAL_STATES == {STATE_PENDING, STATE_REJECTED}

    def test_reading_an_accepted_state_raises(self, tmp_path):
        # #76 owns accept; this ledger must refuse it at the boundary.
        path = candidates_path(tmp_path)
        path.parent.mkdir(parents=True)
        rec = _pair().to_dict()
        rec["state"] = "accepted"
        path.write_text(json.dumps({"schema_version": 1, "candidates": [rec]}))
        with pytest.raises(MergeCandidatesError, match="illegal state"):
            read_candidates(path)


class TestCorruptShapesRaise:
    """Every corrupt shape must RAISE, never read as an empty ledger."""

    def _write(self, tmp_path, text):
        path = candidates_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_invalid_json(self, tmp_path):
        p = self._write(tmp_path, "{not json")
        with pytest.raises(MergeCandidatesError, match="not valid JSON"):
            read_candidates(p)

    def test_top_level_array(self, tmp_path):
        p = self._write(tmp_path, "[]")
        with pytest.raises(MergeCandidatesError, match="not a JSON object"):
            read_candidates(p)

    def test_missing_schema_version(self, tmp_path):
        p = self._write(tmp_path, json.dumps({"candidates": []}))
        with pytest.raises(MergeCandidatesError, match="schema_version"):
            read_candidates(p)

    def test_future_schema_version(self, tmp_path):
        p = self._write(tmp_path, json.dumps({"schema_version": 999, "candidates": []}))
        with pytest.raises(MergeCandidatesError, match="newer than this factlog"):
            read_candidates(p)

    def test_non_int_schema_version(self, tmp_path):
        p = self._write(tmp_path, json.dumps({"schema_version": "1", "candidates": []}))
        with pytest.raises(MergeCandidatesError, match="not an integer"):
            read_candidates(p)

    def test_missing_candidates_key(self, tmp_path):
        p = self._write(tmp_path, json.dumps({"schema_version": 1}))
        with pytest.raises(MergeCandidatesError, match="no 'candidates'"):
            read_candidates(p)

    def test_candidates_not_a_list(self, tmp_path):
        p = self._write(tmp_path, json.dumps({"schema_version": 1, "candidates": {}}))
        with pytest.raises(MergeCandidatesError, match="not a list"):
            read_candidates(p)

    def test_record_not_object(self, tmp_path):
        p = self._write(tmp_path, json.dumps({"schema_version": 1, "candidates": ["x"]}))
        with pytest.raises(MergeCandidatesError, match="not a JSON object"):
            read_candidates(p)

    def test_missing_field(self, tmp_path):
        rec = _pair().to_dict()
        del rec["a_id"]
        p = self._write(tmp_path, json.dumps({"schema_version": 1, "candidates": [rec]}))
        with pytest.raises(MergeCandidatesError, match="missing"):
            read_candidates(p)

    def test_unexpected_extra_key(self, tmp_path):
        rec = _pair().to_dict()
        rec["extra"] = 1
        p = self._write(tmp_path, json.dumps({"schema_version": 1, "candidates": [rec]}))
        with pytest.raises(MergeCandidatesError, match="unexpected key"):
            read_candidates(p)

    def test_wrong_typed_string_field(self, tmp_path):
        rec = _pair().to_dict()
        rec["a_id"] = 5
        p = self._write(tmp_path, json.dumps({"schema_version": 1, "candidates": [rec]}))
        with pytest.raises(MergeCandidatesError, match="must be a string"):
            read_candidates(p)

    def test_score_not_a_number(self, tmp_path):
        rec = _pair().to_dict()
        rec["score"] = "high"
        p = self._write(tmp_path, json.dumps({"schema_version": 1, "candidates": [rec]}))
        with pytest.raises(MergeCandidatesError, match="must be a number"):
            read_candidates(p)

    def test_score_bool_rejected(self, tmp_path):
        rec = _pair().to_dict()
        rec["score"] = True
        p = self._write(tmp_path, json.dumps({"schema_version": 1, "candidates": [rec]}))
        with pytest.raises(MergeCandidatesError, match="must be a number"):
            read_candidates(p)

    def test_duplicate_pair(self, tmp_path):
        rec = _pair().to_dict()
        p = self._write(tmp_path, json.dumps({"schema_version": 1, "candidates": [rec, dict(rec)]}))
        with pytest.raises(MergeCandidatesError, match="two records for pair"):
            read_candidates(p)

    def test_duplicate_json_key_inside_record(self, tmp_path):
        # Two "state" keys: json.loads would silently keep the last one.
        rec = _pair().to_dict()
        body = json.dumps(rec)
        body = body[:-1] + ', "state": "rejected"}'
        p = self._write(tmp_path, '{"schema_version": 1, "candidates": [' + body + ']}')
        with pytest.raises(MergeCandidatesError, match="duplicate key"):
            read_candidates(p)


def test_import_factlog_does_not_load_merge_candidates():
    """`import factlog` must not pull the ledger module into a production import path."""
    import os
    import pathlib
    import subprocess
    import sys

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(repo_root)}
    code = (
        "import sys, factlog; "
        "assert 'factlog.integrations.common.merge_candidates' not in sys.modules, "
        "'merge_candidates was imported by import factlog'"
    )
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


def test_module_exports():
    assert mc.MERGE_CANDIDATES_DIR == "merge-candidates"
    assert callable(mc.candidates_path)
