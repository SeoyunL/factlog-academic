# SPDX-License-Identifier: Apache-2.0
"""The shared provenance-backfill writer (issue #113, part of #105).

``common/backfill.py`` gives a front-matter-only paper (imported before the ledger
existed) the provenance ledger a re-import can never create, so a withdrawal/retraction
signal it carries can finally be acknowledged. It is ``add_source`` into a fresh sidecar
— no new primitive, no network — behind a per-integration :class:`BackfillSchema`, the
same seam ``common/acknowledge.py`` and ``common/check_log.py`` sit on so arXiv and
OpenAlex cannot drift (#64).

Every test here pins one guard that has shipped broken in this repo at least once, and is
written to FAIL if that guard is removed: no network import (this whole track's
invariant), the ``imported_at``-from-front-matter refusal (#98/#105), never nulling an
identifying field it cannot read (#111), the byte- and ``mtime_ns``-identical no-op (same
guard as ``acknowledge.py`` / ``_upsert_sidecar``), the both-boundary
``(ProvenanceError, OSError)`` guard (#65/#71/#94), a neighbour-preserving append into a
partially-populated sidecar (#65), never opening a ``.md`` (P4), and vocabulary neutrality
(#57 §6.3, #93 Q2). Membership is decided by the integration's own ``collect_ledger_entries``
and exported ``provenance_of`` — the schemas below bind the REAL ones so the seam is tested,
never a second copy of the predicate.
"""
from __future__ import annotations

import ast
from datetime import date

from factlog.integrations.arxiv import check_versions
from factlog.integrations.arxiv.source_writer import ArxivSourceWriter
from factlog.integrations.arxiv.work_parser import ParsedArxivWork
from factlog.integrations.common import backfill as bf
from factlog.integrations.common.backfill import (
    BACKFILL_ERROR,
    BACKFILL_REFUSED,
    BACKFILL_UNCHANGED,
    BACKFILL_WRITTEN,
    BackfillSchema,
    _backfill_source,
    _record_fields,
    backfill,
)
from factlog.integrations.common.provenance import (
    Provenance,
    SourceRecord,
    read_provenance,
    sidecar_path,
    write_provenance,
)
from factlog.integrations.openalex import refresh
from factlog.integrations.openalex.source_writer import OpenAlexSourceWriter
from factlog.integrations.openalex.work_parser import ParsedWork

# The two real integration seams. The field readers pull the value the integration's own
# `collect_ledger_entries` already parsed from front matter — so a ledger field can only
# ever be populated from a value the entry holds (the #111/#4 guard, structurally).
ARXIV = BackfillSchema(
    type="arxiv",
    collect_entries=check_versions.collect_ledger_entries,
    provenance_of=check_versions.provenance_of,
    id_of=lambda e: e.arxiv_id,
    fields={
        "version": lambda e: e.recorded_version,
        "withdrawn_by": lambda e: e.recorded_withdrawn_by,
    },
    # `version` is identifying and unreadable-when-None (an OpenAlex-authored .md echoes
    # arxiv_id but never emits arxiv_version); `withdrawn_by` is identifying too but its
    # None legitimately means "not withdrawn", so it is not required.
    required=("version",),
)
OPENALEX = BackfillSchema(
    type="openalex",
    collect_entries=refresh.collect_ledger_entries,
    provenance_of=refresh.provenance_of,
    id_of=lambda e: e.openalex_id,
    fields={
        "doi": lambda e: e.recorded_doi,
        "work_type": lambda e: e.recorded_work_type,
        "journal": lambda e: e.recorded_journal,
        # An import emits is_retracted only as True (else dropped), so mirror it.
        "is_retracted": lambda e: True if e.recorded_is_retracted else None,
    },
    # OpenAlex's writer has no identifying fields, and openalex_id is emitted only by its
    # own writer, so nothing is required.
    required=(),
)

IMPORTED_AT = "2026-01-01T00:00:00Z"


def _arxiv_md(
    kb, name, arxiv_id, version, *, imported_at=IMPORTED_AT, withdrawn_by=None
):
    """Write a front-matter-only arXiv source exactly as the writer emits one."""
    (kb / "sources").mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"arxiv_id: {arxiv_id}",
        f"arxiv_version: {version}",
        'title: "A paper"',
        "preprint: true",
        "imported_from: arxiv",
    ]
    if imported_at is not None:
        lines.append(f'imported_at: "{imported_at}"')
    if withdrawn_by is not None:
        lines.append("arxiv_withdrawn: true")
        lines.append(f"arxiv_withdrawn_by: {withdrawn_by}")
    lines.append("---")
    md = kb / "sources" / f"{name}.md"
    md.write_text("\n".join(lines) + "\n# body\n", encoding="utf-8")
    return md


def _openalex_md(kb, name, openalex_id, *, retracted=False, imported_at=IMPORTED_AT):
    (kb / "sources").mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"openalex_id: {openalex_id}",
        "type: article",
        'title: "A paper"',
        'journal: "Nature"',
        'doi: "10.1/abc"',
        "imported_from: openalex",
    ]
    if imported_at is not None:
        lines.append(f'imported_at: "{imported_at}"')
    if retracted:
        lines.append("openalex_is_retracted: true")
    lines.append("---")
    md = kb / "sources" / f"{name}.md"
    md.write_text("\n".join(lines) + "\n# body\n", encoding="utf-8")
    return md


def _ledger(side):
    return {(r.type, r.id): r.to_dict() for r in read_provenance(side).records}


def _stat(path):
    st = path.stat()
    return (path.read_bytes(), st.st_mtime_ns)


# --------------------------------------------------------------------------- #
# 0. the editable-install trap: prove we test THIS worktree, not the main repo
# --------------------------------------------------------------------------- #
def test_module_under_test_is_this_worktree():
    # The venv's editable install points at the main checkout; without this guard a green
    # run could be testing unmodified code. Pin that the imported module is the worktree's.
    assert "worktrees" in bf.__file__


# --------------------------------------------------------------------------- #
# 1. no network, ever — the module imports no API client (requirement 1)
# --------------------------------------------------------------------------- #
def test_module_imports_no_network_client():
    # Querying upstream would make this a refresh (update_source), able to absorb a change
    # that appeared after the import. A backfill records what the .md asserts, offline.
    source = ast.parse(open(bf.__file__, encoding="utf-8").read())
    imported: list[str] = []
    for node in ast.walk(source):
        if isinstance(node, ast.Import):
            imported += [n.name for n in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    forbidden = ("client", "api", "requests", "urllib", "http", "socket")
    offenders = [m for m in imported if any(tok in m.lower() for tok in forbidden)]
    assert offenders == [], f"backfill must not import a network client: {offenders}"


# --------------------------------------------------------------------------- #
# 2. a front-matter-only paper gains a ledger matching its front matter
# --------------------------------------------------------------------------- #
class TestGainsLedger:
    def test_arxiv_front_matter_only_paper_gets_a_ledger(self, tmp_path):
        _arxiv_md(tmp_path, "p", "2301.00001", 3)
        results = backfill(tmp_path, ARXIV)
        assert [(r.entry_id, r.status) for r in results] == [
            ("2301.00001", BACKFILL_WRITTEN)
        ]
        side = tmp_path / "source-provenance" / "p.json"
        assert _ledger(side)[("arxiv", "2301.00001")] == {
            "type": "arxiv", "id": "2301.00001",
            "imported_at": IMPORTED_AT, "version": 3,
        }

    def test_openalex_front_matter_only_work_gets_a_ledger(self, tmp_path):
        _openalex_md(tmp_path, "w", "W1")
        results = backfill(tmp_path, OPENALEX)
        assert [r.status for r in results] == [BACKFILL_WRITTEN]
        side = tmp_path / "source-provenance" / "w.json"
        assert _ledger(side)[("openalex", "W1")] == {
            "type": "openalex", "id": "W1", "imported_at": IMPORTED_AT,
            "doi": "10.1/abc", "work_type": "article", "journal": "Nature",
        }

    def test_a_paper_that_already_has_a_ledger_is_left_mtime_identical(self, tmp_path):
        # requirement 2: a ledger-backed paper is provenance_of == "ledger" and skipped
        # entirely — never re-stamped. Assert mtime_ns, not just that nothing errored.
        md = _arxiv_md(tmp_path, "p", "2301.00001", 3)
        side = sidecar_path(md)
        write_provenance(side, Provenance(records=[
            SourceRecord("arxiv", "2301.00001", "2020-01-01T00:00:00Z", {"version": 3}),
        ]))
        before_side, before_md = _stat(side), _stat(md)
        results = backfill(tmp_path, ARXIV)
        assert results == []  # not a candidate at all
        assert _stat(side) == before_side  # bytes AND mtime_ns unchanged
        assert _stat(md) == before_md


# --------------------------------------------------------------------------- #
# 3. imported_at is read from front matter, never invented (requirement 3)
# --------------------------------------------------------------------------- #
class TestImportedAt:
    def test_imported_at_is_read_verbatim(self, tmp_path):
        _arxiv_md(tmp_path, "p", "2301.00001", 1, imported_at="1999-12-31T23:59:59Z")
        backfill(tmp_path, ARXIV)
        rec = _ledger(tmp_path / "source-provenance" / "p.json")[("arxiv", "2301.00001")]
        assert rec["imported_at"] == "1999-12-31T23:59:59Z"

    def test_missing_imported_at_is_refused_and_writes_nothing(self, tmp_path):
        # A hand-written or pre-import-command file has no imported_at. Refuse per-id;
        # inventing a timestamp — or a "unknown" sentinel, which read_provenance accepts as
        # a string — would trap the next reader. Nothing is written.
        _arxiv_md(tmp_path, "p", "2301.00001", 1, imported_at=None)
        results = backfill(tmp_path, ARXIV)
        assert [r.status for r in results] == [BACKFILL_REFUSED]
        assert results[0].reason  # reported, not a silent no-op
        assert not (tmp_path / "source-provenance").exists()

    def test_no_sentinel_timestamp_is_ever_written(self, tmp_path):
        _arxiv_md(tmp_path, "p", "2301.00001", 1, imported_at=None)
        backfill(tmp_path, ARXIV)
        # not even a file — but if a future refactor created one, it must not carry a fake.
        assert not (tmp_path / "source-provenance" / "p.json").exists()


# --------------------------------------------------------------------------- #
# 4. never populate an identifying field it cannot read (requirement 4)
# --------------------------------------------------------------------------- #
class TestIdentifyingFields:
    def test_withdrawn_paper_records_its_identifying_field(self, tmp_path):
        # withdrawn_by is an arXiv identifying field and IS in front matter, so the backfill
        # must record it. If it did not, a later re-import carrying the real "author" value
        # would diverge on the identifying fields and error — a false conflict the backfill
        # would have manufactured.
        _arxiv_md(tmp_path, "p", "2301.00002", 2, withdrawn_by="author")
        backfill(tmp_path, ARXIV)
        rec = _ledger(tmp_path / "source-provenance" / "p.json")[("arxiv", "2301.00002")]
        assert rec["withdrawn_by"] == "author"

        # The identifying fields (version, withdrawn_by) match exactly what a fresh import
        # parses, so re-import is a no-op, not a divergence. (Non-identifying fields a
        # backfill cannot recover, e.g. `submitted`, are irrelevant to that comparison.)
        stored = read_provenance(tmp_path / "source-provenance" / "p.json").records[0]
        fresh = SourceRecord("arxiv", "2301.00002", "later", {
            "version": 2, "submitted": "2023-01-01", "withdrawn_by": "author",
        })
        def ident(r):
            return {k: r.fields.get(k) for k in ("version", "withdrawn_by")}
        assert ident(stored) == ident(fresh)

    def test_absent_identifying_field_is_omitted_not_nulled(self, tmp_path):
        # A non-withdrawn paper has no arxiv_withdrawn_by; the record must not carry the key
        # at all. `_record_fields` drops a None value, exactly as an import does.
        _arxiv_md(tmp_path, "p", "2301.00001", 1)
        backfill(tmp_path, ARXIV)
        rec = _ledger(tmp_path / "source-provenance" / "p.json")[("arxiv", "2301.00001")]
        assert "withdrawn_by" not in rec

    def test_record_fields_drops_none(self):
        # The unit under the guard: a reader returning None contributes nothing.
        class E:
            recorded_version = 4
            recorded_withdrawn_by = None
        assert _record_fields(E(), ARXIV) == {"version": 4}


# --------------------------------------------------------------------------- #
# 4b. THE regression: an OpenAlex-authored .md echoes arxiv_id but carries no
#     arxiv_version. Backfilling an arXiv ledger from it once wrote a version-less
#     record whose identifying field diverged from a later merge import -> a false
#     conflict that broke re-import (#73/#84 in a new costume). It must refuse.
# --------------------------------------------------------------------------- #
def _parsed_arxiv(arxiv_id="2311.09277", version=7, withdrawn_by=None):
    return ParsedArxivWork(
        arxiv_id=arxiv_id, version=version, title="A Paper",
        authors=("Ada Lovelace",), abstract="An abstract.", primary_category="cs.CL",
        categories=("cs.CL",), submitted=date(2023, 11, 15),
        last_updated=date(2023, 11, 20), withdrawn_by=withdrawn_by,
        abs_url=f"https://arxiv.org/abs/{arxiv_id}v{version}",
    )


def _kb_with_openalex_authored_md(kb):
    """A KB whose sole source is an OpenAlex-primary .md that echoes arxiv_id but, like
    every OpenAlex-authored file, carries no arxiv_version. Uses the REAL writer."""
    result = OpenAlexSourceWriter().write(
        ParsedWork(openalex_id="W1", arxiv_id="2311.09277", doi="10.1/x",
                   work_type="article", journal="Nature", title="A Paper"),
        kb, imported_at="2025-01-01T00:00:00Z",
    )
    assert result.status == "imported"
    return result.path


class TestManufacturedConflictRegression:
    def test_backfill_refuses_the_openalex_authored_md_and_writes_no_arxiv_record(self, tmp_path):
        existing = _kb_with_openalex_authored_md(tmp_path)
        side = sidecar_path(existing)
        before = _stat(side)  # the sidecar OpenAlex wrote, with its own record

        results = backfill(tmp_path, ARXIV)
        assert [(r.entry_id, r.status) for r in results] == [
            ("2311.09277", BACKFILL_REFUSED)
        ]
        assert results[0].reason  # reported, not silent
        # No arXiv record was written; the OpenAlex sidecar is byte- and mtime_ns-identical.
        assert ("arxiv", "2311.09277") not in _ledger(side)
        assert _stat(side) == before

    def test_later_merge_import_behaves_identically_to_the_no_backfill_control(self, tmp_path):
        # CONTROL: no backfill. A fresh arXiv import of the same paper merges into the
        # OpenAlex original's sidecar.
        control = tmp_path / "control"
        control.mkdir()
        _kb_with_openalex_authored_md(control)
        ctrl = ArxivSourceWriter().write(
            _parsed_arxiv(), control, imported_at="2026-02-02T00:00:00Z")
        assert ctrl.status == "merged"

        # WITH backfill first: it must refuse, so the later import behaves IDENTICALLY —
        # merged, not the permanent "ledger records vNone, arXiv now serves v7" error the
        # version-less record used to manufacture.
        treated = tmp_path / "treated"
        treated.mkdir()
        _kb_with_openalex_authored_md(treated)
        assert [r.status for r in backfill(treated, ARXIV)] == [BACKFILL_REFUSED]
        after = ArxivSourceWriter().write(
            _parsed_arxiv(), treated, imported_at="2026-02-02T00:00:00Z")
        assert after.status == ctrl.status == "merged"

    def test_arxiv_authored_paper_still_backfills_and_reimports_cleanly(self, tmp_path):
        # The common case must still work: an arXiv-written .md always carries
        # arxiv_version, so it is NOT refused, and a later re-import stays a clean no-op.
        _arxiv_md(tmp_path, "p", "2401.00001", 2)
        assert [r.status for r in backfill(tmp_path, ARXIV)] == [BACKFILL_WRITTEN]
        side = tmp_path / "source-provenance" / "p.json"
        # A re-import of the same deposit is an idempotent no-op against the backfilled
        # ledger: identifying fields (version, withdrawn_by) match exactly.
        stored = read_provenance(side).records[0]
        fresh = ArxivSourceWriter()._provenance_record(
            _parsed_arxiv(arxiv_id="2401.00001", version=2), "later")
        ident = ("version", "withdrawn_by")
        assert {k: stored.fields.get(k) for k in ident} == {
            k: fresh.fields.get(k) for k in ident
        }


# --------------------------------------------------------------------------- #
# 5. byte- and mtime_ns-identical no-op (requirement 5)
# --------------------------------------------------------------------------- #
class TestNoOp:
    def test_identical_record_already_present_is_not_rewritten(self, tmp_path):
        # If the record the ledger would receive is already there, do NOT write: the bytes
        # are deterministic but mtime_ns would move. Same guard as acknowledge/_upsert.
        md = _arxiv_md(tmp_path, "p", "2301.00001", 3)
        side = sidecar_path(md)
        write_provenance(side, Provenance(records=[
            SourceRecord("arxiv", "2301.00001", IMPORTED_AT, {"version": 3}),
        ]))
        before = _stat(side)
        result = _backfill_source(
            tmp_path, "2301.00001", "sources/p.md", {"version": 3}, (), ARXIV
        )
        assert result.status == BACKFILL_UNCHANGED
        assert result.ledger == ""
        assert _stat(side) == before  # bytes AND mtime_ns unchanged

    def test_a_diverging_record_in_the_sidecar_is_a_per_id_error(self, tmp_path):
        # The sidecar already holds a DIFFERENT record for this (type, id): refuse to
        # overwrite an audit entry (add_source's stance), reported per-id.
        md = _arxiv_md(tmp_path, "p", "2301.00001", 3)
        side = sidecar_path(md)
        write_provenance(side, Provenance(records=[
            SourceRecord("arxiv", "2301.00001", IMPORTED_AT, {"version": 9}),
        ]))
        result = _backfill_source(
            tmp_path, "2301.00001", "sources/p.md", {"version": 3}, (), ARXIV
        )
        assert result.status == BACKFILL_ERROR
        assert "refusing to overwrite" in result.reason


# --------------------------------------------------------------------------- #
# 6. both boundaries guarded, per id (requirement 6)
# --------------------------------------------------------------------------- #
class TestBoundaryGuards:
    def test_corrupt_sidecar_read_is_isolated_error(self, tmp_path):
        # A partially-written sidecar that will not parse is that paper's problem. collect
        # skips the corrupt file (front-matter fallback makes it a candidate); the backfill
        # then re-reads the same path and reports a per-id error rather than crashing.
        md = _arxiv_md(tmp_path, "p", "2301.00001", 1)
        sidecar_path(md).parent.mkdir(parents=True, exist_ok=True)
        sidecar_path(md).write_text("{ not json", encoding="utf-8")
        results = backfill(tmp_path, ARXIV)
        assert [r.status for r in results] == [BACKFILL_ERROR]
        assert "p.json" in results[0].reason

    def test_read_boundary_guards_oserror(self, tmp_path, monkeypatch):
        # read_provenance can raise OSError (a permission/IO fault), not only
        # ProvenanceError. Guarding only ProvenanceError would let it crash the batch.
        _arxiv_md(tmp_path, "p", "2301.00001", 1)
        monkeypatch.setattr(
            bf, "read_provenance",
            lambda _p: (_ for _ in ()).throw(OSError("permission denied")),
        )
        results = backfill(tmp_path, ARXIV)  # must NOT raise
        assert [r.status for r in results] == [BACKFILL_ERROR]
        assert "permission denied" in results[0].reason

    def test_write_boundary_guards_oserror(self, tmp_path, monkeypatch):
        # THE #94 guard. write_provenance (and its mkdir) re-raise OSError; the WRITE must
        # be inside the (ProvenanceError, OSError) except or one bad disk aborts the batch.
        _arxiv_md(tmp_path, "p", "2301.00001", 1)
        monkeypatch.setattr(
            bf, "write_provenance",
            lambda _p, _prov: (_ for _ in ()).throw(OSError("disk full")),
        )
        results = backfill(tmp_path, ARXIV)  # must NOT raise
        assert [r.status for r in results] == [BACKFILL_ERROR]
        assert "disk full" in results[0].reason

    def test_one_paper_failure_does_not_abort_the_others(self, tmp_path, monkeypatch):
        _arxiv_md(tmp_path, "ok", "2301.00001", 1)
        _arxiv_md(tmp_path, "bad", "2301.00002", 1)
        bad_side = tmp_path / "source-provenance" / "bad.json"
        real_write = bf.write_provenance

        def flaky(path, prov):
            from pathlib import Path
            if Path(path) == bad_side:
                raise OSError("disk full")
            return real_write(path, prov)

        monkeypatch.setattr(bf, "write_provenance", flaky)
        results = backfill(tmp_path, ARXIV)
        by_id = {r.entry_id: r.status for r in results}
        assert by_id["2301.00002"] == BACKFILL_ERROR
        assert by_id["2301.00001"] == BACKFILL_WRITTEN
        assert _ledger(tmp_path / "source-provenance" / "ok.json")[("arxiv", "2301.00001")]


# --------------------------------------------------------------------------- #
# 7. a partially-populated sidecar: append without disturbing the neighbour
# --------------------------------------------------------------------------- #
class TestPartialSidecar:
    def test_appends_beside_a_cross_source_neighbour(self, tmp_path):
        # A paper cross-source-merged before the ledger existed already carries the OTHER
        # integration's record in its sidecar. Backfilling this integration must append its
        # record without disturbing the neighbour or its imported_at.
        md = _arxiv_md(tmp_path, "p", "2301.00001", 3)
        side = sidecar_path(md)
        neighbour = SourceRecord(
            "openalex", "W9", "2020-05-05T00:00:00Z",
            {"doi": "10.1/x", "journal": "Nature"},
        )
        write_provenance(side, Provenance(records=[neighbour]))
        results = backfill(tmp_path, ARXIV)
        assert [r.status for r in results] == [BACKFILL_WRITTEN]
        led = _ledger(side)
        # neighbour byte-for-byte identical, imported_at intact
        assert led[("openalex", "W9")] == neighbour.to_dict()
        # this integration's record appended
        assert led[("arxiv", "2301.00001")] == {
            "type": "arxiv", "id": "2301.00001",
            "imported_at": IMPORTED_AT, "version": 3,
        }


# --------------------------------------------------------------------------- #
# 8. never opens a .md for write (P4) — assert mtime_ns on every .md
# --------------------------------------------------------------------------- #
class TestNeverTouchesMd:
    def test_md_is_byte_and_mtime_identical(self, tmp_path):
        md = _arxiv_md(tmp_path, "p", "2301.00001", 1)
        before = _stat(md)
        backfill(tmp_path, ARXIV)
        assert _stat(md) == before


# --------------------------------------------------------------------------- #
# 9. vocabulary neutrality — the field name is supplied by the schema
# --------------------------------------------------------------------------- #
def test_module_never_names_a_vocabulary_word():
    text = open(bf.__file__, encoding="utf-8").read().lower()
    assert "withdrawn" not in text
    assert "retracted" not in text
