# SPDX-License-Identifier: Apache-2.0
"""An int64 scalar must not be decoded as an interned symbol (#120 prose rule).

The root cause is a SECOND layer redoing the FIRST layer's job from the value alone.
Only the schema knows what a column means, and applying it is the engine's job:
``EasySession.step()`` runs each row through ``_decode_row`` before we see it.

That decoding takes two parties, though. ``_decode_row`` resolves a STRING column by
intern lookup and falls back SILENTLY to the raw ``int`` on a miss, and the table it
looks in is filled by factlog's own pre-interning (#250), not by the engine. So a
symbol column arrives as ``str`` only because we interned it first — without the
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
The head is arity-2, so it
renders as a normal policy finding — the report prints ``low_rank: alpha (beta)`` where
the truth is ``low_rank: alpha (3)``: a fabricated reason string on a real subject, with
no warning and a clean exit.

The scalar-free-head convention this relies on existed only as prose (next to
``_project_typed_relations``); the policy-load guard accepted an ``int64`` column.
Corruption only shows once the KB interns more symbols than the scalar's value, so small
ordinal ranks are the dangerous case and a large date like ``20300101`` passes through
unharmed — which is why this survives casual testing.
"""
from __future__ import annotations

import pytest

import common
import factlog.common as fcommon
from common import decode_wirelog_value

try:  # pragma: no cover - environment-dependent
    import pyrewire  # noqa: F401

    _HAVE_ENGINE = True
except ImportError:  # pragma: no cover
    _HAVE_ENGINE = False


class FakeIntern:
    """The two methods decode_wirelog_value calls on session._intern."""

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
        """A session that pre-interns its symbols exactly as run_wirelog does (#250)."""
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


class TestGuardRejectsNonSymbolPolicyColumn:
    """The cheap fix, shared with #322: reject an unrenderable head at policy load.

    NOT gated on the engine: _assert_no_canonical_head is pure text parsing, and the
    CI harness job runs without pyrewire. Inherited a skipif from the issue write-up;
    under it the whole guard ran zero times in CI.
    """

    def test_policy_load_rejects_an_int64_column(self):
        policy = (
            ".decl low_rank(subject: symbol, r: int64)\n"
            "low_rank(S, R) :- priority_rank(S, R), R < 5.\n"
        )
        with pytest.raises(common.FactlogError):
            common._assert_no_canonical_head(policy)

    @pytest.mark.parametrize("coltype", ["int32", "float", "unsigned"])
    def test_policy_load_rejects_every_scalar_column_type(self, coltype):
        """int64 is not special: no scalar column can render as a reason."""
        with pytest.raises(common.FactlogError):
            common._assert_no_canonical_head(f".decl p(subject: symbol, v: {coltype})\n")

    def test_error_names_the_column_and_offers_the_body_alternative(self):
        """A loud break must say what to do instead, not just what is wrong."""
        policy = ".decl low_rank(subject: symbol, r: int64)\n"
        with pytest.raises(common.FactlogError) as exc:
            common._assert_no_canonical_head(policy)
        message = str(exc.value)
        assert "'r'" in message and "int64" in message
        assert "body" in message.lower()
        assert "docs/typed-relations.md" in message

    def test_error_points_at_typed_relations_for_a_real_scalar_relation(self):
        """The body alternative is not the only one: a scalar RELATION is #116's job.

        Someone declaring `.decl low_rank(subject: symbol, r: int64)` may actually
        want a comparable scalar relation, and typed-relations.md is the supported way
        to get one (projected into an int64 side-relation OUTSIDE the policy text, so
        it is legitimately exempt from this guard). A message that only says "put it
        in the body" sends that author looking for a workaround.
        """
        with pytest.raises(common.FactlogError) as exc:
            common._assert_no_canonical_head(
                ".decl low_rank(subject: symbol, r: int64)\n"
            )
        message = str(exc.value)
        assert "typed-relations.md" in message
        assert "#116" in message

    def test_allows_symbol_columns(self):
        """The correct form: compare in the body, head a quoted reason."""
        common._assert_no_canonical_head(
            ".decl low_rank(subject: symbol, reason: symbol)\n"
            'low_rank(S, "rank below 5") :- priority_rank(S, R), R < 5.\n'
        )

    def test_allows_string_columns(self):
        """symbol and string both map to ColumnType.STRING, so both render."""
        common._assert_no_canonical_head(".decl note(subject: symbol, t: string)\n")

    def test_arity_three_is_now_rejected(self):
        """#322 promoted the arity-2 convention to a load-time guard in this same pass,
        so an arity-3 policy .decl — even one whose columns are all TYPE-valid symbols —
        is now rejected on arity. The type check and the arity check coexist here."""
        with pytest.raises(common.FactlogError, match="two-column head"):
            common._assert_no_canonical_head(
                ".decl triple(a: symbol, b: symbol, c: symbol)\n"
            )


class TestRunWirelogRequiresALiveSchema:
    """run_wirelog must refuse to run when the engine has no schema to decode with.

    EasySession builds its schema by RE-PARSING the program; if wirelog's parser and
    the easy facade disagree about what is well-formed, it keeps None and runs on.
    Decoding then has nothing to consult and every column comes back as a raw id —
    measured on pyrewire 1.0.3, `flagged("alpha", "needs review")` emits
    ('alpha', 'needs review') normally and (0, 3) with the schema gone. That report
    asserts a subject the KB does not contain, and rc stays 0; an over-claim is worse
    than silence.

    Enforced rather than documented because the <2.0 pin cannot catch it: a 1.x MINOR
    may legally introduce such a disagreement and the fallback is silent.

    Engine-free by design (the guard is a None check), so it runs in the CI job that
    has no pyrewire.
    """

    class _FakeSession:
        """EasySession's surface as run_wirelog uses it, with a settable schema."""

        def __init__(self, schema_program):
            self._schema_program = schema_program
            self.closed = False

        def intern(self, value):
            pass

        def step(self):
            return []

        def close(self):
            self.closed = True

    def _arrange(self, tmp_path, monkeypatch, *, schema_program):
        """A minimal KB whose EasySession reports the given schema state.

        Patches `factlog.common`, NOT the bare `common` seen elsewhere in this file:
        tools/common.py is a wrapper that COPIES names via globals().update, so
        run_wirelog resolves its globals in factlog.common and a patch on the wrapper
        is invisible to it (the real require_pyrewire_version then fires).
        """
        accepted = tmp_path / "accepted.dl"
        accepted.write_text("", encoding="utf-8")
        made = []

        def _factory(program):
            session = self._FakeSession(schema_program)
            made.append(session)
            return session

        monkeypatch.setattr(fcommon, "ACCEPTED_DL", accepted)
        monkeypatch.setattr(fcommon, "require_pyrewire_version", lambda: None)
        monkeypatch.setattr(fcommon, "load_logic_policy", lambda: "")
        monkeypatch.setattr(fcommon, "typed_relations", lambda: {})
        monkeypatch.setattr(fcommon, "load_accepted_facts", lambda: [])
        monkeypatch.setattr(fcommon, "relation_aliases", lambda: {})
        monkeypatch.setattr(fcommon, "EasySession", _factory)
        return made

    def test_raises_when_the_engine_has_no_schema(self, tmp_path, monkeypatch):
        self._arrange(tmp_path, monkeypatch, schema_program=None)
        with pytest.raises(fcommon.FactlogError, match="schema"):
            fcommon.run_wirelog()

    def test_the_error_explains_the_raw_id_consequence_and_what_to_do(
        self, tmp_path, monkeypatch
    ):
        """A refusal has to say why it refused and where to look."""
        self._arrange(tmp_path, monkeypatch, schema_program=None)
        with pytest.raises(fcommon.FactlogError) as exc:
            fcommon.run_wirelog()
        message = str(exc.value)
        assert "raw intern id" in message
        assert "doctor" in message
        assert "pyrewire>=1.0.3,<2.0" in message

    def test_the_session_is_closed_before_raising(self, tmp_path, monkeypatch):
        """Refusing must not leak the engine handle."""
        made = self._arrange(tmp_path, monkeypatch, schema_program=None)
        with pytest.raises(fcommon.FactlogError):
            fcommon.run_wirelog()
        assert made and made[0].closed, "the engine session was left open"

    def test_a_live_schema_runs_normally(self, tmp_path, monkeypatch):
        """The control: a session WITH a schema must pass the guard untouched."""
        self._arrange(tmp_path, monkeypatch, schema_program=object())
        assert fcommon.run_wirelog() == {}


class TestLoadLogicPolicyScalarColumnGuard:
    """The guard must fire through the real loader, as every other guard here does."""

    def _make_kb(self, tmp_path, *, dl_text="", extra_text=None):
        policy_dir = tmp_path / "policy"
        policy_dir.mkdir(parents=True, exist_ok=True)
        dl = policy_dir / "logic-policy.dl"
        dl.write_text(dl_text, encoding="utf-8")
        if extra_text is not None:
            (policy_dir / "logic-policy.extra.dl").write_text(extra_text, encoding="utf-8")
        return dl

    def test_raises_when_logic_policy_dl_has_a_scalar_column(self, tmp_path):
        dl = self._make_kb(
            tmp_path, dl_text=".decl low_rank(subject: symbol, r: int64)\n"
        )
        with pytest.raises(common.FactlogError, match="symbol/string"):
            common._load_logic_policy_from(dl)

    def test_raises_when_extra_dl_has_a_scalar_column(self, tmp_path):
        """The realistic case: a hand-authored comparison predicate in extra.dl."""
        dl = self._make_kb(
            tmp_path,
            dl_text=".decl conflict(entity: symbol, reason: symbol)\n",
            extra_text=(
                ".decl low_rank(subject: symbol, r: int64)\n"
                "low_rank(S, R) :- priority_rank(S, R), R < 5.\n"
            ),
        )
        with pytest.raises(common.FactlogError, match="symbol/string"):
            common._load_logic_policy_from(dl)

    def test_ok_when_every_policy_column_is_symbol(self, tmp_path):
        dl = self._make_kb(
            tmp_path,
            dl_text=".decl conflict(entity: symbol, reason: symbol)\n",
            extra_text=(
                ".decl low_rank(subject: symbol, reason: symbol)\n"
                'low_rank(S, "rank below 5") :- priority_rank(S, R), R < 5.\n'
            ),
        )
        result = common._load_logic_policy_from(dl)
        assert "low_rank" in result
