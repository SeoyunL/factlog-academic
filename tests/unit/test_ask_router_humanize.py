# SPDX-License-Identifier: Apache-2.0
"""Integration assertions: render_engine_answer annotates compound objects (#188)."""
from __future__ import annotations

import ask_router


class TestRenderEngineAnswerHumanize:
    def test_compound_object_gets_annotation(self):
        # A relation row whose object is a compound amount term must be annotated
        # with the human-friendly form appended as "  (= <pretty>)".
        row = ["을서비스", "정식운영", 'amount(7,"억")']
        out = ask_router.render_engine_answer("relation(을서비스, 정식운영, X)", [row], signals=None)
        assert "(= 7억)" in out

    def test_plain_object_no_annotation(self):
        # A relation row with a plain (non-compound) object must NOT gain an
        # annotation — a KB with no compound objects renders byte-identically.
        row = ["을서비스", "정식운영", "Chest"]
        out = ask_router.render_engine_answer("relation(을서비스, 정식운영, X)", [row], signals=None)
        assert "(= " not in out

    def test_compound_date_gets_annotation(self):
        row = ["프로젝트", "완료일", "date(2030,1,15)"]
        out = ask_router.render_engine_answer("relation(프로젝트, 완료일, X)", [row], signals=None)
        assert "(= 2030-01-15)" in out

    def test_compound_number_gets_annotation(self):
        row = ["항목", "수치", "number(2.5)"]
        out = ask_router.render_engine_answer("relation(항목, 수치, X)", [row], signals=None)
        assert "(= 2.5)" in out

    def test_stored_form_still_present(self):
        # The canonical stored string must still appear in the output (the pretty
        # form is appended, never substituted — the user can still copy-paste it).
        row = ["을서비스", "정식운영", 'amount(7,"억")']
        out = ask_router.render_engine_answer("relation(을서비스, 정식운영, X)", [row], signals=None)
        assert 'amount(7,"억")' in out
