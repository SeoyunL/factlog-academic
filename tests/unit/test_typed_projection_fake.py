# SPDX-License-Identifier: Apache-2.0
"""Engine-independent coverage of the typed-projection insert loop (#126).

`_project_typed_relations(session, specs, accepted)` is the pure-Python core of
run_wirelog()'s typed-relation side-projection: it touches the session only via
intern()/insert(), never step()/close(). That lets these tests drive it with a
FakeSession that merely RECORDS inserts — no pyrewire, no engine, no monkeypatch,
no path globals. They pin the projection contract: which
(alias, intern(subject), scalar) tuples reach session.insert(), that a non-parsing
object is skipped + warned (the fact still loads untyped), that the scalar is a
bare int (never interned), and that the deterministic (relation, subject, object)
sort is load-bearing.

The pyrewire-gated e2e in test_typed_projection.py stays the authority on real
engine inference; this file owns the pure projection logic.
"""
from __future__ import annotations

import random
import unicodedata

import common


class FakeSession:
    """Records inserts; never touches engine internals.

    intern() hands back a stable, distinguishable id per value (same value ->
    same id, distinct values -> distinct ids) so a test can reverse-map an
    interned id back to its source string. step()/close() are intentionally
    omitted — _project_typed_relations never calls them.
    """

    def __init__(self):
        self.inserts: list[tuple[str, tuple]] = []
        self._ids: dict[object, int] = {}

    def intern(self, value):
        return self._ids.setdefault(value, len(self._ids))

    def insert(self, alias, payload):
        self.inserts.append((alias, payload))


# Two typed relations: a date (정식_운영 -> launch_date) and a number
# (버전 -> version_num). TypedRelSpec is (type, alias, units=None).
SPECS = {
    "정식_운영": common.TypedRelSpec("date", "launch_date"),
    "버전": common.TypedRelSpec("number", "version_num"),
}


def _row(subject: str, relation: str, object_: str) -> dict[str, str]:
    # Same dict shape _load_accepted_facts_from produces (subject/relation/object).
    return {"subject": subject, "relation": relation, "object": object_}


def _decode_inserts(session: FakeSession) -> set[tuple[str, str, int]]:
    """Reverse-map each recorded (alias, (interned_subject, scalar)) insert back to
    (alias, subject_string, scalar) so a test asserts the REAL projected identity,
    not whatever happened to be interned. Also proves payload[0] is a genuine
    intern() output and payload[1] is a bare int that was NOT interned."""
    id_to_value = {v: k for k, v in session._ids.items()}
    decoded: set[tuple[str, str, int]] = set()
    for alias, payload in session.inserts:
        subject_id, scalar = payload
        assert subject_id in id_to_value          # payload[0] is an interned id
        assert isinstance(scalar, int) and not isinstance(scalar, bool)
        assert scalar not in session._ids         # payload[1] was never interned
        decoded.add((alias, id_to_value[subject_id], scalar))
    return decoded


def test_insert_set_skips_nonparsing_row_and_warns(capsys):
    # A parseable date, a NON-parseable date (미정 -> None -> skip+warn), and a
    # parseable number. Only the two parseable rows should project.
    accepted = [
        _row("갑서비스", "정식_운영", "2030.1"),   # date 20300101
        _row("병서비스", "정식_운영", "미정"),      # does NOT parse -> skip + warn
        _row("appA", "버전", "2.5"),                # number 2500 (×1000)
    ]
    fake = FakeSession()

    common._project_typed_relations(fake, SPECS, accepted)

    decoded = _decode_inserts(fake)
    # EXACT set: only the two parseable rows project.
    assert decoded == {
        ("launch_date", "갑서비스", 20300101),
        ("version_num", "appA", 2500),
    }
    # Anti-masking: the skipped subject appears in NO insert.
    assert all(subject != "병서비스" for _alias, subject, _scalar in decoded)

    err = capsys.readouterr().err
    assert "does not parse" in err   # the skip warning fired
    assert "미정" in err              # names the offending value
    assert "병서비스" in err           # names the offending subject


def test_shuffled_accepted_inserts_in_sorted_key_order():
    # The projection sorts by (relation, subject, object); the input order does
    # not. Shuffle a list whose sorted order differs from its input order so that,
    # if sorted(...) were dropped, the insert order would flip.
    accepted = [
        _row("z앱", "버전", "2.5"),                 # number 2500
        _row("을서비스", "정식_운영", "2029.6"),    # date 20290601
        _row("갑서비스", "정식_운영", "2030.1"),    # date 20300101
    ]
    # Deterministic non-sorted input order (seed chosen so input != sorted).
    random.Random(7).shuffle(accepted)
    fake = FakeSession()

    common._project_typed_relations(fake, SPECS, accepted)

    id_to_value = {v: k for k, v in fake._ids.items()}
    ordered = [
        (alias, id_to_value[payload[0]], payload[1])
        for alias, payload in fake.inserts
    ]
    # LIST equality (ordered): inserts come out in sorted (relation, subject,
    # object) order regardless of input order. 버전 < 정식_운영 (ASCII < Hangul);
    # within 정식_운영, 갑서비스 < 을서비스 in Python string order.
    assert ordered == [
        ("version_num", "z앱", 2500),
        ("launch_date", "갑서비스", 20300101),
        ("launch_date", "을서비스", 20290601),
    ]


# --- The engine folds nowhere (#325) -----------------------------------------
# check_conflicts folds the object to NFC *before* literal_types.normalize, so a
# decomposed typed literal parses there and not here. Its exit-0 advisory makes a
# claim about this function, and the earlier wording ("loads every one of them
# untyped") was true of only one of the two branches below.
#
# CONTROLS: both pass before that wording was corrected — they pin the engine
# behaviour the message describes, which is unchanged. Their job is to make the
# next edit to that sentence fail here if it drifts from what the loop does.


def _nfd(value: str) -> str:
    return unicodedata.normalize("NFD", value)


_ORDINAL = {"순위": common.TypedRelSpec("ordinal", "rank")}


def test_composed_literal_still_loads_typed_when_the_relation_is_composed(capsys):
    # The projection runs per row and the spec lookup hits, so '3위' IS inserted
    # as rank 3; only the decomposed twin degrades. The two notations land on
    # opposite sides of the typed table, which is why the checker's merge is not
    # reproducible here — not because nothing loads.
    accepted = [_row("갑", "순위", _nfd("제3호")), _row("갑", "순위", "3위")]
    fake = FakeSession()
    common._project_typed_relations(fake, _ORDINAL, accepted)
    id_to_value = {v: k for k, v in fake._ids.items()}
    assert [(a, id_to_value[p[0]], p[1]) for a, p in fake.inserts] == [("rank", "갑", 3)]
    err = capsys.readouterr().err
    assert "does not parse as ordinal" in err
    assert err.count("does not parse") == 1  # the composed row did not warn


def test_nothing_loads_typed_when_the_relation_name_is_decomposed(capsys):
    # specs.get(row["relation"]) is a raw lookup, so a decomposed relation name
    # misses its spec on EVERY row — including the composed literal's. This is the
    # branch where "every one of them untyped" holds, and it warns about none of
    # them because the spec is never found.
    accepted = [
        _row("갑", _nfd("순위"), _nfd("제3호")),
        _row("갑", _nfd("순위"), "3위"),
    ]
    fake = FakeSession()
    common._project_typed_relations(fake, _ORDINAL, accepted)
    assert fake.inserts == []
    assert capsys.readouterr().err == ""
