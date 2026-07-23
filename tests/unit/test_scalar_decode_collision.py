# SPDX-License-Identifier: Apache-2.0
"""An int64 scalar must not be decoded as an interned symbol.

The root cause is a SECOND layer redoing the FIRST layer's job from the value alone.
Only the schema knows what a column means, and applying it is the engine's job:
``EasySession.step()`` runs each row through ``_decode_row`` before we see it.

That decoding takes two parties, though. ``_decode_row`` resolves a STRING column by
intern lookup and falls back SILENTLY to the raw ``int`` on a miss, and the table it
looks in is filled by factlog's own pre-interning, not by the engine. So a symbol
column arrives as ``str`` only because we interned it first — without the
pre-interning it arrives as a raw id, measured below.

Measured against the real engine (pyrewire 1.0.3): for

    .decl priority_rank(subject: symbol, r: int64)
    .decl low_rank(subject: symbol, r: int64)
    relation("alpha", "is", "thing").
    priority_rank(S, 3) :- relation(S, "is", "thing").
    low_rank(S, R) :- priority_rank(S, R), R < 5.

``session.step()`` emits ``('low_rank', ('alpha', 3), 1)`` once the symbols are
pre-interned — ``'alpha'`` is ALREADY a ``str`` and ``3`` is ALREADY the correct
``int``. Skip the pre-interning and the same row is ``('int', 0), ('int', 3)``.

``decode_wirelog_value`` (``factlog/common.py``) then re-decoded that row looking only
at the value: ``isinstance(value, int) and session._intern.contains_id(value)``. Nothing
in that test can distinguish a SYMBOL ID from a genuine ``int64`` value — and it never
needed to help a pre-interned run, since a symbol column is already ``str`` there and
fails the ``isinstance``. It could only harm: it rewrote the ``3`` into ``'beta'``.
The head is arity-2, so it renders as a normal policy finding — the report prints
``low_rank: alpha (beta)`` where the truth is ``low_rank: alpha (3)``: a fabricated
reason string on a real subject, with no warning and a clean exit.

Corruption only shows once the KB interns more symbols than the scalar's value, so small
ordinal ranks are the dangerous case and a large date like ``20300101`` passes through
unharmed — which is why this survives casual testing.
"""
from __future__ import annotations

import pytest

from common import decode_wirelog_value

try:  # pragma: no cover - environment-dependent
    import pyrewire  # noqa: F401

    _HAVE_ENGINE = True
except ImportError:  # pragma: no cover
    _HAVE_ENGINE = False


class FakeIntern:
    """The two methods the old decode_wirelog_value called on session._intern."""

    def __init__(self, symbols):
        self._symbols = list(symbols)

    def contains_id(self, value):
        return 0 <= value < len(self._symbols)

    def lookup(self, value):
        return self._symbols[value]


class FakeSession:
    def __init__(self, symbols):
        self._intern = FakeIntern(symbols)


class TestScalarIsNotRewrittenAsASymbol:
    def test_small_scalar_collides_with_a_symbol_id(self):
        session = FakeSession(["alpha", "beta", "published_year", "gamma", "delta"])
        assert decode_wirelog_value(session, 3) == 3, (
            "an int64 column value was rewritten into an interned symbol"
        )

    def test_decoded_symbol_passes_through(self):
        """step() decodes symbol columns; the decoder must not re-handle them."""
        session = FakeSession(["alpha", "beta", "gamma"])
        assert decode_wirelog_value(session, "beta") == "beta"

    def test_large_scalar_is_unharmed(self):
        """Why this hides: a date-shaped scalar exceeds the intern table and passes."""
        session = FakeSession(["alpha", "beta", "gamma"])
        assert decode_wirelog_value(session, 20300101) == 20300101

    def test_bool_is_not_looked_up_as_an_id(self):
        """bool is an int subclass, so True indexes the table as id 1."""
        session = FakeSession(["alpha", "beta", "gamma"])
        assert decode_wirelog_value(session, True) is True


@pytest.mark.skipif(not _HAVE_ENGINE, reason="pyrewire not installed")
class TestAgainstTheRealEngine:
    """The authority: what a decoded row really contains, and why."""

    _PROGRAM = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl priority_rank(subject: symbol, r: int64)\n"
        ".decl low_rank(subject: symbol, r: int64)\n"
        'relation("alpha", "is", "thing").\n'
        'priority_rank(S, 3) :- relation(S, "is", "thing").\n'
        "low_rank(S, R) :- priority_rank(S, R), R < 5.\n"
    )

    def _session(self, pre_intern=True):
        """A session that pre-interns its symbols exactly as run_wirelog does."""
        from pyrewire import EasySession

        session = EasySession(self._PROGRAM)
        if pre_intern:
            for value in ["alpha", "beta", "published_year", "gamma", "delta"]:
                session.intern(value)
        return session

    def test_engine_emits_a_raw_int_for_an_int64_column(self):
        session = self._session()
        try:
            rows = {name: row for name, row, diff in session.step() if diff > 0}
            assert "low_rank" in rows, rows
            assert rows["low_rank"][1] == 3, (
                f"expected the raw scalar 3, got {rows['low_rank'][1]!r}"
            )
        finally:
            session.close()

    def test_a_pre_interned_symbol_column_decodes_to_str(self):
        """The premise of the fix, and it takes TWO parties.

        decode_wirelog_value may pass a value through because the row arrives
        decoded — but the engine only decodes a symbol column when that symbol is in
        the intern table, and we are the ones who put it there. If this ever fails,
        the cause is EITHER a changed engine OR lost pre-interning; the companion
        test below pins the other half, so the pair says which.
        """
        session = self._session()
        try:
            rows = {name: row for name, row, diff in session.step() if diff > 0}
            subject = rows["low_rank"][0]
            assert isinstance(subject, str), (
                f"step() handed back {subject!r} ({type(subject).__name__}) for a "
                "PRE-INTERNED symbol column, so pass-through no longer renders names: "
                "either pyrewire changed how it decodes a STRING column, or the "
                "pre-interning this session does was lost"
            )
            assert subject == "alpha"
        finally:
            session.close()

    def test_an_un_interned_symbol_column_falls_back_to_a_raw_int(self):
        """Why the pre-interning is load-bearing and NOT dead code.

        pyrewire's _decode_row resolves a STRING column by intern lookup and falls
        back SILENTLY to the raw int on a miss — it does not raise. So losing the
        pre-interning would not fail loudly; reports would simply print bare ids
        where names belong. Pin that, so a reader who sees the pass-through decoder
        and concludes the interning is vestigial is contradicted by a test.
        """
        session = self._session(pre_intern=False)
        try:
            rows = {name: row for name, row, diff in session.step() if diff > 0}
            subject = rows["low_rank"][0]
            assert isinstance(subject, int) and not isinstance(subject, bool), (
                f"expected an un-interned symbol column to fall back to a raw int, got "
                f"{subject!r} ({type(subject).__name__}); if pyrewire now resolves "
                "symbols without our pre-interning, run_wirelog's interning and the "
                "notes pointing at it are stale"
            )
        finally:
            session.close()

    def test_report_does_not_render_a_fabricated_reason(self):
        session = self._session()
        try:
            rows = {name: row for name, row, diff in session.step() if diff > 0}
            decoded = [decode_wirelog_value(session, v) for v in rows["low_rank"]]
            assert decoded == ["alpha", 3], (
                f"the report would print low_rank: {decoded[0]} ({decoded[1]}) "
                "instead of low_rank: alpha (3)"
            )
        finally:
            session.close()
