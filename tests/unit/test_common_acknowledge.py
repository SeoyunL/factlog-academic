# SPDX-License-Identifier: Apache-2.0
"""The shared acknowledge-ledger writer (issue #99, part of #93).

``common/acknowledge.py`` is the third authorized writer of the provenance ledger — a
human recording a decision — behind a per-integration :class:`AcknowledgeSchema`, the
same seam PR #97 gave ``common/check_log.py`` so arXiv and OpenAlex cannot drift (#64).

Every test here pins one guard the repo has shipped broken at least once, and is written
to FAIL if that guard is removed: set/clear of an identifying field (#73/#84), the
both-boundary ``(ProvenanceError, OSError)`` guard (#65/#71/#94), updating every ledger
that names the paper, a byte- and ``mtime_ns``-identical no-op, a one-record blast
radius, never opening the ``.md`` (P4), and refusing to fabricate a ledger for an id no
ledger carries. A final test pins the vocabulary neutrality (#57 §6.3, #93 Q2): neither
"withdrawn" nor "retracted" may appear in this module.
"""
from __future__ import annotations

from pathlib import Path

from factlog.integrations.common import acknowledge as ack
from factlog.integrations.common.acknowledge import (
    ACK_ERROR,
    ACK_NO_LEDGER,
    ACK_UNCHANGED,
    ACK_WRITTEN,
    AcknowledgeSchema,
    acknowledge,
    acknowledge_all,
)
from factlog.integrations.common.provenance import (
    SIDECAR_DIR,
    Provenance,
    SourceRecord,
    read_provenance,
    sidecar_path,
    write_provenance,
)

# Fixed, caller-supplied timestamp — every write stays byte-reproducible.
IMPORTED_AT = "2026-07-09T00:00:00Z"

# One schema per integration, so tests exercise the seam both integrations sit on. The
# field names are the only integration-specific values the module ever sees.
ARXIV = AcknowledgeSchema(type="arxiv", field="withdrawn_by")
OPENALEX = AcknowledgeSchema(type="openalex", field="is_retracted")


def _seed(kb: Path, name: str, records, *, make_md: bool = True) -> Path:
    """Write a source ``.md`` (optional) and a provenance sidecar holding *records*.
    Returns the sidecar path."""
    (kb / "sources").mkdir(parents=True, exist_ok=True)
    md = kb / "sources" / f"{name}.md"
    if make_md:
        md.write_text("---\narxiv_id: x\n---\n# body\n", encoding="utf-8")
    side = sidecar_path(md, kb)
    write_provenance(side, Provenance(records=list(records)))
    return side


def _ledger(side: Path):
    return {(r.type, r.id): r.to_dict() for r in read_provenance(side).records}


def _stat(path: Path):
    st = path.stat()
    return (path.read_bytes(), st.st_mtime_ns)


# --------------------------------------------------------------------------- #
# 1. set and clear — the #73/#84 identifying-field trap
# --------------------------------------------------------------------------- #
class TestSetAndClear:
    def test_set_writes_the_field(self, tmp_path):
        side = _seed(tmp_path, "p", [
            SourceRecord("arxiv", "1904.09773", IMPORTED_AT, {"version": 2}),
        ])
        result = acknowledge(tmp_path, "1904.09773", "author", ARXIV)
        assert result.status == ACK_WRITTEN
        rec = _ledger(side)[("arxiv", "1904.09773")]
        assert rec["withdrawn_by"] == "author"
        assert rec["version"] == 2  # other fields untouched

    def test_clear_to_none_removes_the_field(self, tmp_path):
        # An un-withdrawn paper whose ledger still says withdrawn_by is an identifying-
        # field divergence: re-import errors forever and no refresh may clear it. Only
        # this primitive may. Writing None must REMOVE the key, not write null.
        side = _seed(tmp_path, "p", [
            SourceRecord("arxiv", "1904.09773", IMPORTED_AT,
                         {"withdrawn_by": "author", "version": 2}),
        ])
        result = acknowledge(tmp_path, "1904.09773", None, ARXIV)
        assert result.status == ACK_WRITTEN
        rec = _ledger(side)[("arxiv", "1904.09773")]
        assert "withdrawn_by" not in rec
        assert rec["version"] == 2

    def test_clear_of_absent_field_is_a_noop(self, tmp_path):
        side = _seed(tmp_path, "p", [
            SourceRecord("arxiv", "1904.09773", IMPORTED_AT, {"version": 2}),
        ])
        before = _stat(side)
        result = acknowledge(tmp_path, "1904.09773", None, ARXIV)
        assert result.status == ACK_UNCHANGED
        assert _stat(side) == before

    def test_openalex_boolean_value_is_stored_verbatim(self, tmp_path):
        side = _seed(tmp_path, "w", [
            SourceRecord("openalex", "W1", IMPORTED_AT, {"doi": "10.1/x"}),
        ])
        acknowledge(tmp_path, "W1", True, OPENALEX)
        assert _ledger(side)[("openalex", "W1")]["is_retracted"] is True


# --------------------------------------------------------------------------- #
# 2. both boundaries guarded — the #65/#71/#94 batch crash
# --------------------------------------------------------------------------- #
class TestBoundaryGuards:
    def test_corrupt_ledger_read_is_isolated_error(self, tmp_path):
        side = _seed(tmp_path, "p", [
            SourceRecord("arxiv", "1", IMPORTED_AT, {}),
        ])
        side.write_text("{ not json", encoding="utf-8")
        result = acknowledge(tmp_path, "1", "author", ARXIV)
        assert result.status == ACK_ERROR
        assert "p.json" in result.reason

    def test_read_boundary_guards_oserror(self, tmp_path, monkeypatch):
        # read_provenance can raise OSError (a permission/IO failure), not only
        # ProvenanceError. Guarding only ProvenanceError would let it crash the batch.
        _seed(tmp_path, "p", [SourceRecord("arxiv", "1", IMPORTED_AT, {})])
        monkeypatch.setattr(
            ack, "read_provenance",
            lambda _p: (_ for _ in ()).throw(OSError("permission denied")),
        )
        result = acknowledge(tmp_path, "1", "author", ARXIV)  # must NOT raise
        assert result.status == ACK_ERROR
        assert "permission denied" in result.reason

    def test_write_boundary_guards_oserror(self, tmp_path, monkeypatch):
        # THE #94 guard. write_provenance (and its mkdir) re-raise OSError; the WRITE must
        # be inside the (ProvenanceError, OSError) except or one bad disk aborts the batch.
        _seed(tmp_path, "p", [SourceRecord("arxiv", "1", IMPORTED_AT, {})])
        monkeypatch.setattr(
            ack, "write_provenance",
            lambda _p, _prov: (_ for _ in ()).throw(OSError("disk full")),
        )
        result = acknowledge(tmp_path, "1", "author", ARXIV)  # must NOT raise
        assert result.status == ACK_ERROR
        assert "disk full" in result.reason

    def test_partial_write_across_ledgers_is_an_error_not_a_success(self, tmp_path, monkeypatch):
        # Two ledgers name the paper; one write fails. The other is still written, but the
        # OUTCOME is an error — a partial write is never reported as success.
        good = _seed(tmp_path, "good", [SourceRecord("arxiv", "1", IMPORTED_AT, {})])
        bad = _seed(tmp_path, "bad", [SourceRecord("arxiv", "1", IMPORTED_AT, {})])
        real_write = ack.write_provenance

        def flaky(path, prov):
            if Path(path) == bad:
                raise OSError("disk full")
            return real_write(path, prov)

        monkeypatch.setattr(ack, "write_provenance", flaky)
        result = acknowledge(tmp_path, "1", "author", ARXIV)
        assert result.status == ACK_ERROR
        assert "bad.json" in result.reason
        # the healthy ledger really was written
        assert result.ledgers == ("source-provenance/good.json",)
        assert _ledger(good)[("arxiv", "1")]["withdrawn_by"] == "author"

    def test_one_id_failure_does_not_abort_the_others(self, tmp_path, monkeypatch):
        # Two papers, each in its own sidecar; the write for "bad" fails. Because the
        # primitive returns an error rather than raising, the batch never aborts: "ok" is
        # still written even though "bad" failed.
        ok_side = _seed(tmp_path, "ok", [SourceRecord("arxiv", "ok", IMPORTED_AT, {})])
        bad_side = _seed(tmp_path, "bad", [SourceRecord("arxiv", "bad", IMPORTED_AT, {})])
        real_write = ack.write_provenance

        def flaky(path, prov):
            if Path(path) == bad_side:
                raise OSError("disk full")
            return real_write(path, prov)

        monkeypatch.setattr(ack, "write_provenance", flaky)
        results = acknowledge_all(tmp_path, ["ok", "bad"], "author", ARXIV)
        by_id = {r.entry_id: r.status for r in results}
        assert by_id["bad"] == ACK_ERROR
        assert by_id["ok"] == ACK_WRITTEN
        assert _ledger(ok_side)[("arxiv", "ok")]["withdrawn_by"] == "author"

    def test_an_unreadable_sidecar_fails_every_acknowledge_it_might_name(self, tmp_path):
        # Deliberate and safe: an unreadable sidecar could carry ANY id, and silently
        # skipping it could miss a ledger requirement (3) says must be updated. So
        # acknowledging even an unrelated id is a reported error while a sidecar cannot be
        # read — the fail-loud, never-under-write direction, not a silent partial success.
        _seed(tmp_path, "ok", [SourceRecord("arxiv", "ok", IMPORTED_AT, {})])
        corrupt = _seed(tmp_path, "bad", [SourceRecord("arxiv", "bad", IMPORTED_AT, {})])
        corrupt.write_text("{ not json", encoding="utf-8")
        result = acknowledge(tmp_path, "ok", "author", ARXIV)
        assert result.status == ACK_ERROR
        assert "bad.json" in result.reason


# --------------------------------------------------------------------------- #
# 3. a paper may be named by several ledgers — key on id, not position
# --------------------------------------------------------------------------- #
class TestSeveralLedgers:
    def test_every_ledger_naming_the_paper_is_updated(self, tmp_path):
        a = _seed(tmp_path, "a", [SourceRecord("arxiv", "1", IMPORTED_AT, {})])
        b = _seed(tmp_path, "b", [SourceRecord("arxiv", "1", IMPORTED_AT, {})])
        result = acknowledge(tmp_path, "1", "author", ARXIV)
        assert result.status == ACK_WRITTEN
        assert result.ledgers == (
            "source-provenance/a.json", "source-provenance/b.json",
        )
        assert _ledger(a)[("arxiv", "1")]["withdrawn_by"] == "author"
        assert _ledger(b)[("arxiv", "1")]["withdrawn_by"] == "author"

    def test_matched_by_id_not_by_position(self, tmp_path):
        # The target record is second in the sidecar, behind a different paper. A
        # position-based match would hit the wrong record.
        side = _seed(tmp_path, "multi", [
            SourceRecord("arxiv", "other", IMPORTED_AT, {"version": 9}),
            SourceRecord("arxiv", "target", IMPORTED_AT, {"version": 1}),
        ])
        acknowledge(tmp_path, "target", "author", ARXIV)
        led = _ledger(side)
        assert led[("arxiv", "target")]["withdrawn_by"] == "author"
        assert "withdrawn_by" not in led[("arxiv", "other")]

    def test_same_id_different_type_is_not_touched(self, tmp_path):
        # (type, id) is the key: an OpenAlex record that happens to share the id string is
        # a different record and must not be written by an arXiv acknowledgement.
        side = _seed(tmp_path, "s", [
            SourceRecord("arxiv", "1", IMPORTED_AT, {}),
            SourceRecord("openalex", "1", IMPORTED_AT, {"doi": "10.1/x"}),
        ])
        acknowledge(tmp_path, "1", "author", ARXIV)
        led = _ledger(side)
        assert led[("arxiv", "1")]["withdrawn_by"] == "author"
        assert "withdrawn_by" not in led[("openalex", "1")]


# --------------------------------------------------------------------------- #
# 4. byte-deterministic no-op — assert mtime_ns, not just bytes
# --------------------------------------------------------------------------- #
class TestNoOp:
    def test_acknowledging_the_held_value_is_byte_and_mtime_identical(self, tmp_path):
        side = _seed(tmp_path, "p", [
            SourceRecord("arxiv", "1", IMPORTED_AT, {"withdrawn_by": "author"}),
        ])
        before = _stat(side)
        result = acknowledge(tmp_path, "1", "author", ARXIV)
        assert result.status == ACK_UNCHANGED
        assert result.ledgers == ()
        assert _stat(side) == before  # bytes AND st_mtime_ns unchanged


# --------------------------------------------------------------------------- #
# 5. blast radius of one record
# --------------------------------------------------------------------------- #
class TestBlastRadius:
    def test_neighbouring_records_and_other_fields_untouched(self, tmp_path):
        neighbour = SourceRecord("openalex", "W9", IMPORTED_AT,
                                  {"doi": "10.1/x", "journal": "Nature"})
        target = SourceRecord("arxiv", "1", IMPORTED_AT,
                              {"version": 3, "comment": "v3 note"})
        side = _seed(tmp_path, "p", [neighbour, target])
        acknowledge(tmp_path, "1", "author", ARXIV)
        led = _ledger(side)
        # neighbour byte-for-byte identical
        assert led[("openalex", "W9")] == neighbour.to_dict()
        # target: only withdrawn_by added; imported_at and every other field preserved
        assert led[("arxiv", "1")] == {
            "type": "arxiv", "id": "1", "imported_at": IMPORTED_AT,
            "version": 3, "comment": "v3 note", "withdrawn_by": "author",
        }


# --------------------------------------------------------------------------- #
# 6. never touches any .md (P4) — assert mtime_ns on the .md too
# --------------------------------------------------------------------------- #
class TestNeverTouchesMd:
    def test_md_is_byte_and_mtime_identical(self, tmp_path):
        _seed(tmp_path, "p", [SourceRecord("arxiv", "1", IMPORTED_AT, {})])
        md = tmp_path / "sources" / "p.md"
        before = _stat(md)
        acknowledge(tmp_path, "1", "author", ARXIV)
        assert _stat(md) == before


# --------------------------------------------------------------------------- #
# 7. an id no ledger carries is a reported error, not a silent no-op / new ledger
# --------------------------------------------------------------------------- #
class TestNoLedger:
    def test_unknown_id_is_error_and_creates_no_ledger(self, tmp_path):
        _seed(tmp_path, "p", [SourceRecord("arxiv", "known", IMPORTED_AT, {})])
        before = sorted((tmp_path / SIDECAR_DIR).rglob("*.json"))
        result = acknowledge(tmp_path, "unknown", "author", ARXIV)
        assert result.status == ACK_NO_LEDGER
        assert result.ledgers == ()
        # not a silent no-op — a reason is given — and no new ledger was fabricated
        assert result.reason
        assert sorted((tmp_path / SIDECAR_DIR).rglob("*.json")) == before

    def test_empty_kb_is_error_not_crash(self, tmp_path):
        result = acknowledge(tmp_path, "anything", "author", ARXIV)
        assert result.status == ACK_NO_LEDGER
        assert not (tmp_path / SIDECAR_DIR).exists()


# --------------------------------------------------------------------------- #
# vocabulary neutrality — the field name is supplied by the caller, not hardcoded
# --------------------------------------------------------------------------- #
def test_field_name_comes_entirely_from_the_schema(tmp_path):
    # The module is neutral about "withdrawn" / "retracted" (#57 §6.3, #93 Q2): it writes
    # whatever field the schema names. A field the module has never heard of works
    # identically — proof nothing is special-cased or hardcoded.
    made_up = AcknowledgeSchema(type="arxiv", field="some_future_field")
    side = _seed(tmp_path, "p", [SourceRecord("arxiv", "1", IMPORTED_AT, {})])
    acknowledge(tmp_path, "1", "a-value", made_up)
    assert _ledger(side)[("arxiv", "1")]["some_future_field"] == "a-value"
