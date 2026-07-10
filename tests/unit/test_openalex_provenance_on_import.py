# SPDX-License-Identifier: Apache-2.0
"""OpenAlex records its provenance into a source's ledger, and merges (#73).

Part 2 of #70. #72 gave arXiv a ledger on every import and made it a §7.3 merger;
this does the same for OpenAlex, behind the same ``merges_cross_source`` flag now
lifted (with ``_record``/``_merge``/``_upsert_sidecar``/``_identity_fields``) to
``BaseSourceWriter``. The load-bearing decisions pinned here:

* ``OpenAlexSourceWriter._IDENTIFYING_FIELDS`` is EMPTY. OpenAlex has no refresh
  command, so any identifying field would be a permanently unclearable per-id
  error. Every field drifts silently, first-import-wins; ``_divergence`` (and its
  arXiv ``arxiv-check-versions`` wording) is never reached.
* The record's fields are exactly ``doi``, ``work_type``, ``journal`` and
  ``is_retracted`` (emitted True-or-absent, never ``false``).
* **Order independence**: an arXiv-then-OpenAlex import and its reverse leave the
  SAME record set ``{(type, id, fields)}`` — ``imported_at`` legitimately differs
  by arrival order and is excluded from the comparison.
"""
from __future__ import annotations

from datetime import date

from factlog.integrations.arxiv.source_writer import ArxivSourceWriter
from factlog.integrations.arxiv.work_parser import ParsedArxivWork
from factlog.integrations.common.provenance import (
    SourceRecord,
    read_provenance,
    sidecar_path,
)
from factlog.integrations.openalex.source_writer import OpenAlexSourceWriter
from factlog.integrations.openalex.work_parser import ParsedWork


def _arxiv(arxiv_id="2311.09277", version=2, **over) -> ParsedArxivWork:
    base = dict(
        arxiv_id=arxiv_id,
        version=version,
        title="A Paper",
        authors=("Ada Lovelace",),
        abstract="An abstract.",
        primary_category="cs.CL",
        categories=("cs.CL", "cs.LG"),
        submitted=date(2023, 11, 15),
        last_updated=date(2023, 11, 20),
        doi=None,
        journal_ref=None,
        comment="10 pages",
        withdrawn_by=None,
        abs_url=f"https://arxiv.org/abs/{arxiv_id}v{version}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v{version}",
    )
    return ParsedArxivWork(**{**base, **over})


def _openalex(openalex_id="W1", arxiv_id="2311.09277", **over) -> ParsedWork:
    base = dict(
        openalex_id=openalex_id,
        title="A Paper",
        authors=("Ada Lovelace",),
        year=2023,
        journal="Journal of Foo",
        doi="10.1/x",
        pmid=None,
        arxiv_id=arxiv_id,
        work_type="article",
    )
    return ParsedWork(**{**base, **over})


def _record_set(sidecar):
    """The ledger as a comparable set of ``(type, id, fields)`` — no ``imported_at``.

    ``imported_at`` records when THIS run first saw THAT source and legitimately
    differs by arrival order, so it is excluded exactly as ``_identity_fields`` and
    the acceptance criterion require.
    """
    return {
        (r.type, r.id, tuple(sorted(r.fields.items())))
        for r in read_provenance(sidecar).records
    }


class TestOpenAlexRecordsItsOwnLedger:
    def test_a_new_import_writes_a_one_record_sidecar(self, tmp_path):
        result = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W9"), tmp_path, imported_at="2026-02-02T00:00:00Z")
        assert result.status == "imported"
        sidecar = sidecar_path(result.path, tmp_path)
        recs = read_provenance(sidecar).records
        assert len(recs) == 1
        rec = recs[0]
        assert rec.type == "openalex" and rec.id == "W9"
        assert rec.imported_at == "2026-02-02T00:00:00Z"

    def test_record_fields_are_exactly_doi_work_type_journal(self, tmp_path):
        result = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W9", doi="10.1/x", journal="Nature", work_type="article"),
            tmp_path, imported_at="t")
        rec = read_provenance(sidecar_path(result.path, tmp_path)).records[0].to_dict()
        assert rec["doi"] == "10.1/x"
        assert rec["work_type"] == "article"
        assert rec["journal"] == "Nature"
        # No content/classification/join-key/volatile fields leak into the ledger.
        # (``type`` IS present, but as the reserved record type "openalex", not the
        # work type — see the dedicated regression test below.)
        for excluded in ("openalex_url", "cited_by_count", "pmid", "arxiv_id",
                         "title", "authors", "abstract", "concepts"):
            assert excluded not in rec

    def test_the_record_type_is_openalex_not_the_work_type(self, tmp_path):
        # Regression guard: a field literally named ``type`` would overwrite the
        # reserved record type on the flat serialization. It is keyed ``work_type``.
        result = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W9", work_type="preprint"), tmp_path, imported_at="t")
        rec = read_provenance(sidecar_path(result.path, tmp_path)).records[0]
        assert rec.type == "openalex"
        assert rec.fields["work_type"] == "preprint"


class TestRetractionIsTrueOrAbsent:
    def test_not_retracted_omits_is_retracted_entirely(self, tmp_path):
        result = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W9", openalex_is_retracted=False), tmp_path, imported_at="t")
        rec = read_provenance(sidecar_path(result.path, tmp_path)).records[0].to_dict()
        # Absent, NOT ``false`` — a false would survive to_dict and break the
        # byte-determinism the ledger depends on.
        assert "is_retracted" not in rec

    def test_retracted_records_is_retracted_true(self, tmp_path):
        result = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W9", openalex_is_retracted=True), tmp_path, imported_at="t")
        rec = read_provenance(sidecar_path(result.path, tmp_path)).records[0].to_dict()
        assert rec["is_retracted"] is True


class TestDriftIsAbsorbedFirstImportWins:
    """Empty ``_IDENTIFYING_FIELDS``: no drift is ever a per-id error (there is no
    refresh command to clear one), so a re-import is always a quiet no-op."""

    def test_cited_by_count_change_does_not_error(self, tmp_path):
        # cited_by_count is not even in the record; a re-import with a moved count
        # is a plain skip that rewrites nothing.
        first = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W1", cited_by_count=10), tmp_path, imported_at="a")
        before = sidecar_path(first.path, tmp_path).read_bytes()
        again = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W1", cited_by_count=999), tmp_path, imported_at="b")
        assert again.status == "skipped"
        assert sidecar_path(first.path, tmp_path).read_bytes() == before

    def test_a_merge_absorbs_a_changed_doi_without_erroring(self, tmp_path):
        # Seed an arXiv-primary file, then merge an OpenAlex record, then merge a
        # SECOND OpenAlex import of the same id whose doi/journal moved. With no
        # identifying fields the incumbent record wins — no ProvenanceConflict.
        ax = ArxivSourceWriter().write(_arxiv(), tmp_path, imported_at="t")
        OpenAlexSourceWriter().write(
            _openalex(openalex_id="W1", journal="First"), tmp_path, imported_at="t")
        sidecar = sidecar_path(ax.path, tmp_path)
        before = _record_set(sidecar)
        # A fresh writer/index re-imports the same openalex id via the arXiv-id join.
        again = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W1", journal="Renamed Journal"), tmp_path, imported_at="t2")
        assert again.status == "merged"
        assert _record_set(sidecar) == before  # first import wins, no error


class TestOrderIndependence:
    """THE acceptance criterion (#73): arXiv-then-OpenAlex and the reverse leave
    the SAME record set. Compared by ``(type, id, fields)``, never raw bytes —
    ``imported_at`` differs by arrival order by design."""

    def _forward(self, kb):
        ArxivSourceWriter().write(_arxiv(), kb, imported_at="2026-01-01T00:00:00Z")
        OpenAlexSourceWriter().write(_openalex(), kb, imported_at="2026-02-02T00:00:00Z")

    def _reverse(self, kb):
        OpenAlexSourceWriter().write(_openalex(), kb, imported_at="2026-03-03T00:00:00Z")
        ArxivSourceWriter().write(_arxiv(), kb, imported_at="2026-04-04T00:00:00Z")

    def _sole_sidecar(self, kb):
        sidecars = list((kb / "source-provenance").glob("*.json"))
        assert len(sidecars) == 1, sidecars
        return sidecars[0]

    def test_both_orders_leave_exactly_two_records_one_per_source(self, tmp_path):
        forward_kb, reverse_kb = tmp_path / "fwd", tmp_path / "rev"
        forward_kb.mkdir()
        reverse_kb.mkdir()
        self._forward(forward_kb)
        self._reverse(reverse_kb)

        fwd = _record_set(self._sole_sidecar(forward_kb))
        rev = _record_set(self._sole_sidecar(reverse_kb))

        assert fwd == rev
        assert sorted(t for t, _, _ in fwd) == ["arxiv", "openalex"]
        assert len(fwd) == 2

    def test_the_arxiv_record_is_preserved_when_openalex_merges_in(self, tmp_path):
        # Read-modify-write, not replace: the arXiv record survives the merge.
        ax = ArxivSourceWriter().write(_arxiv(), tmp_path, imported_at="t")
        merged = OpenAlexSourceWriter().write(_openalex(), tmp_path, imported_at="t2")
        assert merged.status == "merged"
        assert merged.path == ax.path
        types = sorted(r.type for r in read_provenance(sidecar_path(ax.path, tmp_path)).records)
        assert types == ["arxiv", "openalex"]


class TestDryRunAndCorruptSidecar:
    def test_dry_run_writes_no_sidecar(self, tmp_path):
        result = OpenAlexSourceWriter().plan(_openalex(openalex_id="W9"), tmp_path)
        assert result.status == "imported"
        assert not (tmp_path / "source-provenance").exists()

    def test_a_corrupt_sidecar_on_a_merge_is_a_per_id_error_not_a_crash(self, tmp_path):
        # An arXiv-primary file whose sidecar is corrupt. An OpenAlex import of the
        # same paper must report a per-id error, never raise.
        ax = ArxivSourceWriter().write(_arxiv(), tmp_path, imported_at="t")
        sidecar = sidecar_path(ax.path, tmp_path)
        sidecar.write_text("{ not json", encoding="utf-8")
        result = OpenAlexSourceWriter().write(_openalex(), tmp_path, imported_at="t2")
        assert result.status == "error"
        assert "unreadable" in result.reason

    def test_a_corrupt_sidecar_does_not_abort_the_rest_of_the_batch(self, tmp_path):
        from factlog.integrations.openalex.importer import import_works

        # One paper's sidecar is corrupt (via a pre-existing arXiv file); a second,
        # unrelated OpenAlex work must still import.
        ax = ArxivSourceWriter().write(_arxiv(arxiv_id="2311.09277"), tmp_path, imported_at="t")
        sidecar_path(ax.path, tmp_path).write_text("{ corrupt", encoding="utf-8")
        report = import_works(
            [_openalex(openalex_id="W1", arxiv_id="2311.09277"),        # error (corrupt)
             _openalex(openalex_id="W2", arxiv_id="9999.99999", doi=None, title="Fresh")],  # imported
            target=tmp_path, imported_at="t2",
        )
        statuses = {o.key: o.status for o in report.outcomes}
        assert statuses["W1"] == "error"
        assert statuses["W2"] == "imported"


class TestSameSourceDuplicateStillSkips:
    """Two OpenAlex records sharing a DOI is a *same-source* duplicate (#71), not a
    §7.3 cross-source merge. It stays ``skipped`` and folds nothing."""

    def test_two_openalex_sharing_a_doi_skip_and_write_no_second_record(self, tmp_path):
        first = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W1", doi="10.1/x"), tmp_path, imported_at="t")
        second = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W2", doi="10.1/x", title="Preprint"), tmp_path, imported_at="t")
        assert second.status == "skipped"
        assert second.path == first.path
        # W1's ledger is untouched: no W2 record folded in.
        ids = [r.id for r in read_provenance(sidecar_path(first.path, tmp_path)).records]
        assert ids == ["W1"]

    def test_a_reimport_of_the_same_id_stays_skipped_despite_a_typoed_provenance(self, tmp_path):
        # openalex_id is emitted by no other database, so an identity match is always
        # this writer's own record: a mistyped imported_from is corruption, not a
        # cross-source view. P3 must not turn re-import into a merge (that would
        # write a sidecar on re-import, breaking "re-run leaves the fs unchanged").
        (tmp_path / "sources").mkdir()
        (tmp_path / "sources" / "existing.md").write_text(
            '---\nopenalex_id: "W1"\nimported_from: openalexx\n---\n', encoding="utf-8")
        result = OpenAlexSourceWriter().plan(_openalex(openalex_id="W1"), tmp_path)
        assert result.status == "skipped"
        assert "openalex_id match" in result.reason


class TestNoArxivCheckVersionsWordingLeaks:
    def test_openalex_never_reaches_divergence_and_never_names_arxiv_check_versions(self, tmp_path):
        # Force a same-id merge whose fields moved; with empty identifying fields
        # this is a no-op, never a divergence error. And the base divergence text,
        # were it ever produced, names no command.
        ArxivSourceWriter().write(_arxiv(), tmp_path, imported_at="t")
        OpenAlexSourceWriter().write(_openalex(openalex_id="W1"), tmp_path, imported_at="t")
        again = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W1", journal="Different"), tmp_path, imported_at="t2")
        assert again.status == "merged"
        assert "arxiv-check-versions" not in (again.reason or "")

    def test_base_divergence_message_names_no_command(self):
        existing = SourceRecord(type="openalex", id="W1", imported_at="a", fields={"x": 1})
        incoming = SourceRecord(type="openalex", id="W1", imported_at="b", fields={"x": 2})
        msg = OpenAlexSourceWriter()._divergence(existing, incoming)
        assert "arxiv-check-versions" not in msg


class TestAStaleSidecarIsReplacedForOpenAlexToo:
    """`_record` was lifted to the base class, so the replace-not-append rule that
    #72 established for arXiv now governs OpenAlex as well. Pin it here: a shared
    implementation is exactly where a fix silently stops applying to one caller."""

    def test_a_deleted_papers_ledger_is_not_inherited_by_its_slug_successor(self, tmp_path):
        (tmp_path / "sources").mkdir()
        # Same author/year/title -> same base slug; different OpenAlex identity.
        first = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W1", arxiv_id=None), tmp_path, imported_at="t1")
        assert first.status == "imported"
        sidecar = sidecar_path(first.path, tmp_path)
        assert [r.id for r in read_provenance(sidecar).records] == ["W1"]

        first.path.unlink()  # the user deletes the source; the ledger is left behind

        second = OpenAlexSourceWriter().write(
            _openalex(openalex_id="W2", arxiv_id=None), tmp_path, imported_at="t2")
        assert second.status == "imported"
        assert second.path.name == first.path.name  # the slug really is reused

        ids = [r.id for r in read_provenance(sidecar_path(second.path, tmp_path)).records]
        assert ids == ["W2"], (
            "the new original's ledger claims it also came from the deleted paper"
        )

    def test_a_merge_still_preserves_the_foreign_record(self, tmp_path):
        # The other half of the same shared machinery: `_merge` read-modify-writes.
        (tmp_path / "sources").mkdir()
        existing = ArxivSourceWriter().write(_arxiv(), tmp_path, imported_at="t1")
        assert existing.status == "imported"

        merged = OpenAlexSourceWriter().write(_openalex(), tmp_path, imported_at="t2")
        assert merged.status == "merged"
        assert merged.path == existing.path

        records = read_provenance(sidecar_path(existing.path, tmp_path)).records
        assert sorted((r.type, r.id) for r in records) == [
            ("arxiv", "2311.09277"), ("openalex", "W1")
        ]
