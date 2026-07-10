# SPDX-License-Identifier: Apache-2.0
"""#112: a nested paper is a paper, and a paper that cannot have a ledger is reported.

`collect_ledger_entries` read sidecars with `rglob` and sources with a flat `glob`, while
the KB's canonical enumeration is `rglob` over `SOURCE_ROOTS`. A paper at
`sources/sub/x.md` was therefore invisible to `arxiv-check-versions` and
`openalex-refresh` — `factlog sources` listed it, `checked N/N` left it out of the
denominator, and a withdrawal on it was never reported. That is the *silent* direction.

The fix is one enumeration (`provenance_sources`), and the decision that only `sources/`
can carry a ledger: mapping `runs/sources/` into the same sidecar directory is not
injective (`sources/z.md` and `runs/sources/z.md` collide on `source-provenance/z.json`),
and giving it its own root would create ledgers no walker reads. So `runs/sources/` gets
no ledger — and every command that cannot act on such a paper says so, loudly, rather than
dropping it. These tests pin both halves: the silence closed, and the exclusion audible.
"""
from __future__ import annotations

from datetime import date

import pytest

from factlog import cli
from factlog.common import source_files
from factlog.integrations.arxiv import check_versions as cv
from factlog.integrations.arxiv.client import BatchResult
from factlog.integrations.arxiv.id_normalizer import ArxivId
from factlog.integrations.arxiv.source_writer import ArxivSourceWriter
from factlog.integrations.arxiv.work_parser import ParsedArxivWork
from factlog.integrations.common import acknowledge as ack
from factlog.integrations.common.front_matter import front_matter_block, read_scalars
from factlog.integrations.common.provenance import (
    Provenance,
    SourceRecord,
    excluded_sources_by_id,
    provenance_sources,
    read_provenance,
    sidecar_path,
    write_provenance,
)
from factlog.integrations.openalex import refresh as rf
from factlog.integrations.openalex.source_writer import OpenAlexSourceWriter
from factlog.integrations.openalex.work_parser import ParsedWork

IMPORTED_AT = "2026-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
def _work(arxiv_id="1706.03762", version=7, withdrawn_by=None) -> ParsedArxivWork:
    return ParsedArxivWork(
        arxiv_id=arxiv_id,
        version=version,
        title="A paper",
        authors=("Ann Author",),
        abstract="An abstract.",
        primary_category="cs.CL",
        categories=("cs.CL",),
        submitted=date(2017, 6, 12),
        last_updated=date(2020, 1, 1),
        withdrawn_by=withdrawn_by,
        abs_url=f"https://arxiv.org/abs/{arxiv_id}v{version}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v{version}",
    )


class FakeClient:
    def __init__(self, works):
        self._works = {w.arxiv_id: w for w in works}
        self.calls: list[list[str]] = []

    def fetch_works(self, ids):
        self.calls.append([str(i) for i in ids])
        found, missing = [], []
        for value in ids:
            work = self._works.get(str(value))
            (found.append(work) if work else missing.append(ArxivId(str(value))))
        return BatchResult(list(reversed(found)), missing)


@pytest.fixture
def fake(monkeypatch):
    def install(client):
        monkeypatch.setattr(cli, "_make_arxiv_client", lambda config: client)
        return client

    return install


def run(argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


def _arxiv_md(kb, rel, arxiv_id, version=7, *, withdrawn_by=None, imported_at=IMPORTED_AT):
    """A front-matter-only arXiv paper at *rel* (a KB-relative posix path)."""
    path = kb / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"arxiv_id: {arxiv_id}", f"arxiv_version: {version}"]
    if imported_at:
        lines.append(f"imported_at: {imported_at}")
    if withdrawn_by:
        lines += ["arxiv_withdrawn: true", f"arxiv_withdrawn_by: {withdrawn_by}"]
    path.write_text("---\n" + "\n".join(lines) + "\n---\n# body\n", encoding="utf-8")
    return path


def _openalex_md(kb, rel, openalex_id, *, imported_at=IMPORTED_AT):
    path = kb / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'---\nopenalex_id: {openalex_id}\ntype: article\nimported_at: {imported_at}\n'
        "---\n# body\n",
        encoding="utf-8",
    )
    return path


def _ledgered(kb, rel, arxiv_id, version=7, **fields):
    """A paper with a real sidecar, so the ledger branch (not front matter) answers."""
    md = _arxiv_md(kb, rel, arxiv_id, version)
    record = SourceRecord(
        type="arxiv", id=arxiv_id, imported_at=IMPORTED_AT,
        fields={"version": version, **fields},
    )
    write_provenance(sidecar_path(md, kb), Provenance(records=[record]))
    return md


def _kb(tmp_path):
    (tmp_path / "sources").mkdir()
    return tmp_path


# --------------------------------------------------------------------------- #
# the enumeration: one walk, and it is the KB's own
# --------------------------------------------------------------------------- #
class TestOneEnumeration:
    def test_the_checked_denominator_is_every_paper_factlog_sources_lists(self, tmp_path):
        """Asserted against `source_files()` — the function `factlog sources` and coverage
        use — rather than a hand-written list, so the walker and the executor cannot drift
        apart without this going red. Every `.md` under `sources/` that names a paper is
        either checked or reported as excluded; none may be in neither set."""
        kb = _kb(tmp_path)
        _arxiv_md(kb, "sources/x.md", "2301.00001")
        _arxiv_md(kb, "sources/sub/y.md", "2301.00002")
        _arxiv_md(kb, "sources/deep/er/z.md", "2301.00003")
        _arxiv_md(kb, "runs/sources/w.md", "2301.00004")

        entries, errors = cv.collect_ledger_entries(kb)
        excluded = cv.excluded_checks(kb)

        checked_refs = {ref for e in entries for ref in e.sources}
        # The excluded check is keyed by arxiv_id (the id column is an id); the paths it
        # speaks for are in `sources`, which is what this partition is about.
        excluded_refs = {ref for e in excluded for ref in e.sources}
        every_md = {
            p.relative_to(kb).as_posix() for p in source_files(kb) if p.suffix == ".md"
        }
        assert errors == []
        assert checked_refs | excluded_refs == every_md
        assert checked_refs & excluded_refs == set()

    def test_provenance_sources_is_the_kb_enumeration_narrowed_to_one_root(self, tmp_path):
        kb = _kb(tmp_path)
        _arxiv_md(kb, "sources/x.md", "2301.00001")
        _arxiv_md(kb, "sources/sub/y.md", "2301.00002")
        _arxiv_md(kb, "runs/sources/z.md", "2301.00003")
        (kb / "sources" / ".hidden").mkdir()
        _arxiv_md(kb, "sources/.hidden/h.md", "2301.00009")

        # Nested is in; runs/sources/ is out; the hidden directory is filtered at the
        # single enumeration point (#67), not re-filtered by each caller.
        got = {p.relative_to(kb).as_posix() for p in provenance_sources(kb)}
        assert got == {"sources/x.md", "sources/sub/y.md"}


# --------------------------------------------------------------------------- #
# a nested paper is a paper
# --------------------------------------------------------------------------- #
class TestNestedPaperIsAPaper:
    def test_a_nested_paper_is_collected_exactly_like_a_flat_one(self, tmp_path):
        kb = _kb(tmp_path)
        _arxiv_md(kb, "sources/flat.md", "2301.00001", version=1)
        _arxiv_md(kb, "sources/sub/nested.md", "2301.00002", version=1)

        entries, _ = cv.collect_ledger_entries(kb)
        by_id = {e.arxiv_id: e for e in entries}
        assert set(by_id) == {"2301.00001", "2301.00002"}
        assert by_id["2301.00002"].sources == ("sources/sub/nested.md",)
        assert by_id["2301.00002"].recorded_version == 1

    def test_a_withdrawal_on_a_nested_paper_is_reported(self, tmp_path, fake, capsys):
        """The silent direction, closed. Before #112 this paper was absent from the run
        entirely and arXiv's withdrawal was never surfaced."""
        kb = _kb(tmp_path)
        _arxiv_md(kb, "sources/sub/nested.md", "2301.00002", version=1)
        fake(FakeClient([_work("2301.00002", version=1, withdrawn_by="author")]))

        code = run(["arxiv-check-versions", "--target", str(kb)])
        out = capsys.readouterr().out
        assert code == 0
        assert "WITHDRAWN by the author" in out
        assert "2301.00002" in out
        assert "Newly withdrawn:     1" in out

    def test_a_nested_paper_counts_in_the_denominator(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _arxiv_md(kb, "sources/flat.md", "2301.00001", version=1)
        _arxiv_md(kb, "sources/sub/nested.md", "2301.00002", version=1)
        fake(FakeClient([_work("2301.00001", version=1), _work("2301.00002", version=1)]))

        run(["arxiv-check-versions", "--target", str(kb)])
        out = capsys.readouterr().out
        assert "Checked 2 of 2 arXiv record(s)" in out
        assert "Checked:             2" in out

    def test_a_nested_paper_is_backfillable_and_then_acknowledgeable(
        self, tmp_path, fake, capsys
    ):
        """The whole chain the issue measured as broken: a nested paper's acknowledge
        answered "no arXiv record for id ... is in this KB"."""
        kb = _kb(tmp_path)
        _arxiv_md(kb, "sources/sub/nested.md", "2301.00002", version=1)

        assert run(["arxiv-backfill-provenance", "--target", str(kb)]) == 0
        capsys.readouterr()

        sidecar = kb / "source-provenance" / "sub" / "nested.json"
        assert sidecar.is_file()

        fake(FakeClient([_work("2301.00002", version=1, withdrawn_by="author")]))
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "2301.00002",
            "--target", str(kb), "--yes",
        ])
        assert code == 0, capsys.readouterr()
        assert "Recorded withdrawal by author" in capsys.readouterr().out
        record = read_provenance(sidecar).records[0]
        assert record.fields["withdrawn_by"] == "author"

    def test_a_nested_sidecar_round_trips_through_both_walkers(self, tmp_path):
        """Write it where `sidecar_path` says, and assert BOTH readers find it: the
        collector's `source-provenance/**/*.json` walk and `common/acknowledge.py`'s. A
        sidecar one reads and the other misses is a ledger nothing reads."""
        kb = _kb(tmp_path)
        _ledgered(kb, "sources/sub/nested.md", "2301.00002", version=1)

        entries, errors = cv.collect_ledger_entries(kb)
        assert errors == []
        assert [e.arxiv_id for e in entries] == ["2301.00002"]
        assert entries[0].sources == ("source-provenance/sub/nested.json",)

        schema = ack.AcknowledgeSchema(type="arxiv", field="withdrawn_by")
        result = ack.acknowledge(kb, "2301.00002", "author", schema)
        assert result.status == ack.ACK_WRITTEN
        assert result.ledgers == ("source-provenance/sub/nested.json",)

    def test_openalex_sees_a_nested_work_too(self, tmp_path):
        kb = _kb(tmp_path)
        _openalex_md(kb, "sources/sub/nested.md", "W2")
        entries, errors = rf.collect_ledger_entries(kb)
        assert errors == []
        assert [e.openalex_id for e in entries] == ["W2"]
        assert entries[0].sources == ("sources/sub/nested.md",)


# --------------------------------------------------------------------------- #
# runs/sources: excluded by construction, reported by contract
# --------------------------------------------------------------------------- #
class TestRunsSourcesIsReportedNotSkipped:
    def test_collision_is_impossible_by_construction_not_improbable(self, tmp_path):
        """The adversarial pair. Two different papers, one filename. If `runs/sources/`
        mapped into `source-provenance/`, both would land on `source-provenance/z.json`
        and one ledger would silently carry two papers' records (the #258 slug collision,
        in the provenance layer). There is no second path to collide with, because the
        second root maps to nothing at all."""
        kb = _kb(tmp_path)
        flat = _arxiv_md(kb, "sources/z.md", "2301.00001")
        nested_runs = _arxiv_md(kb, "runs/sources/z.md", "2301.00002")

        assert sidecar_path(flat, kb) == kb / "source-provenance" / "z.json"
        with pytest.raises(ValueError):
            sidecar_path(nested_runs, kb)

        # And no walker ever hands the excluded path to a writer, so the ledger written
        # for `sources/z.md` names exactly one paper.
        run_id = "2301.00002"
        assert run(["arxiv-backfill-provenance", "--target", str(kb)]) == 1
        ids = [r.id for r in read_provenance(kb / "source-provenance" / "z.json").records]
        assert ids == ["2301.00001"]
        assert run_id not in ids

    def test_check_versions_reports_it_and_exits_nonzero(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _arxiv_md(kb, "sources/x.md", "2301.00001", version=1)
        _arxiv_md(kb, "runs/sources/z.md", "2301.00003", version=1)
        client = fake(FakeClient([_work("2301.00001", version=1)]))

        code = run(["arxiv-check-versions", "--target", str(kb)])
        out = capsys.readouterr().out
        assert code == 1  # never a silent 0
        assert "runs/sources/z.md" in out
        assert "Could not check:" in out
        assert "Errors:              1" in out
        # It is in the DENOMINATOR (2), and it was not checked (1). `Checked 2 of 2` beside
        # `Errors: 1` was a false statement about the run.
        assert "Checked 1 of 2 arXiv record(s)" in out
        # The paper is addressed by id, exactly as acknowledge addresses it.
        assert "✗ 2301.00003:" in out
        # The excluded paper was never sent to arXiv.
        assert client.calls == [["2301.00001"]]

    def test_porcelain_carries_the_excluded_row(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _arxiv_md(kb, "sources/x.md", "2301.00001", version=1)
        _arxiv_md(kb, "runs/sources/z.md", "2301.00003", version=1)
        fake(FakeClient([_work("2301.00001", version=1)]))

        code = run(["arxiv-check-versions", "--target", str(kb), "--porcelain"])
        out = capsys.readouterr().out
        assert code == 1
        # The id column carries an id — the value `--id` takes — never a source path.
        assert any(
            line.startswith("check\t2301.00003\terror") for line in out.splitlines()
        ), out
        assert "runs/sources/z.md" in out  # the path is in the reason column
        assert not any(
            line.startswith("check\truns/") for line in out.splitlines()
        ), "a source path must never occupy the id column"
        assert "errors\t1" in out

    def test_auto_update_never_writes_a_ledger_for_it(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _arxiv_md(kb, "runs/sources/z.md", "2301.00003", version=1)
        fake(FakeClient([_work("2301.00003", version=9)]))

        code = run(["arxiv-check-versions", "--target", str(kb), "--auto-update"])
        assert code == 1
        assert "runs/sources/z.md" in capsys.readouterr().out
        # No sidecar directory anywhere: not under the KB root, and not under runs/.
        assert not (kb / "source-provenance").exists()
        assert not (kb / "runs" / "source-provenance").exists()

    def test_a_conversion_that_names_no_paper_is_silent(self, tmp_path, fake, capsys):
        """`ingest` fills `runs/sources/` with conversions. None of them carries an
        integration id, and a warning that fires for every ingested PDF on every run is how
        an operator learns to skim past the alarm (#93). Only a source that *names a paper*
        is loud."""
        kb = _kb(tmp_path)
        _arxiv_md(kb, "sources/x.md", "2301.00001", version=1)
        conv = kb / "runs" / "sources"
        conv.mkdir(parents=True)
        (conv / "report.pdf.md").write_text(
            "<!-- ingested-by-factlog | source: report.pdf -->\n\nsome text\n",
            encoding="utf-8",
        )
        fake(FakeClient([_work("2301.00001", version=1)]))

        code = run(["arxiv-check-versions", "--target", str(kb)])
        out = capsys.readouterr().out
        assert code == 0
        assert "report.pdf.md" not in out
        assert "Errors:              0" in out

    def test_acknowledge_names_the_file_instead_of_denying_the_paper(
        self, tmp_path, fake, capsys
    ):
        """"no arXiv record for id ... is in this KB" was measured, and it was a lie: the
        paper IS in the KB. Say where it is and why it cannot hold a ledger."""
        kb = _kb(tmp_path)
        _arxiv_md(kb, "runs/sources/z.md", "2301.00003", version=1)
        client = fake(FakeClient([_work("2301.00003", version=1, withdrawn_by="author")]))

        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "2301.00003",
            "--target", str(kb), "--yes",
        ])
        err = capsys.readouterr().err
        assert code == 1
        assert "no arXiv record" not in err
        assert "runs/sources/z.md" in err
        assert client.calls == []  # zero API requests

    def test_openalex_refresh_reports_it_too(self, tmp_path, capsys, monkeypatch):
        kb = _kb(tmp_path)
        _openalex_md(kb, "runs/sources/z.md", "W3")
        monkeypatch.setattr(
            cli, "_make_openalex_client", lambda config: pytest.fail("no request expected")
        )
        code = run(["openalex-refresh", "--target", str(kb)])
        out = capsys.readouterr().out
        assert code == 1
        assert "runs/sources/z.md" in out


# --------------------------------------------------------------------------- #
# backfill: it widened with the consumers, and it still agrees with --dry-run
# --------------------------------------------------------------------------- #
class TestBackfillWidensWithItsConsumers:
    def test_a_nested_paper_gets_a_ledger_the_consumers_read(self, tmp_path, capsys):
        kb = _kb(tmp_path)
        _arxiv_md(kb, "sources/sub/nested.md", "2301.00002", version=1)
        assert run(["arxiv-backfill-provenance", "--target", str(kb)]) == 0
        capsys.readouterr()

        # Written where sidecar_path says...
        sidecar = kb / "source-provenance" / "sub" / "nested.json"
        assert sidecar.is_file()
        # ...and the consumer now reads it as a ledger, not as front matter.
        entries, _ = cv.collect_ledger_entries(kb)
        assert entries[0].sources == ("source-provenance/sub/nested.json",)
        assert cv.provenance_of(entries[0].sources) == "ledger"

    @pytest.mark.parametrize("command", ["arxiv", "openalex"])
    def test_dry_run_never_disagrees_with_the_real_run(self, tmp_path, capsys, command):
        """Every classification — written, refused, excluded — is shared, so a preview can
        only ever differ about a filesystem failure it declined to trigger."""
        from factlog.integrations.arxiv.backfill import backfill_schema as arxiv_schema
        from factlog.integrations.common.backfill import backfill
        from factlog.integrations.openalex.backfill import backfill_schema as oa_schema

        kb = _kb(tmp_path)
        if command == "arxiv":
            schema = arxiv_schema()
            no_stamp_id = "2301.00005"
            _arxiv_md(kb, "sources/flat.md", "2301.00001", version=1)
            _arxiv_md(kb, "sources/sub/nested.md", "2301.00002", version=1)
            _arxiv_md(kb, "runs/sources/excluded.md", "2301.00003", version=1)
            _arxiv_md(kb, "sources/no_stamp.md", no_stamp_id, version=1, imported_at="")
        else:
            schema = oa_schema()
            no_stamp_id = "W5"
            _openalex_md(kb, "sources/flat.md", "W1")
            _openalex_md(kb, "sources/sub/nested.md", "W2")
            _openalex_md(kb, "runs/sources/excluded.md", "W3")
            _openalex_md(kb, "sources/no_stamp.md", no_stamp_id, imported_at="")

        preview = [(r.entry_id, r.status, r.ledger) for r in backfill(kb, schema, dry_run=True)]
        assert not (kb / "source-provenance").exists()  # a preview writes nothing
        real = [(r.entry_id, r.status, r.ledger) for r in backfill(kb, schema)]
        assert preview == real

        statuses = {entry_id: status for entry_id, status, _ in real}
        # All three classifications are exercised, so the equality above is not vacuous.
        excluded_id = "W3" if command == "openalex" else "2301.00003"
        assert statuses[excluded_id] == "error"
        assert statuses[no_stamp_id] == "refused"
        assert statuses["W2" if command == "openalex" else "2301.00002"] == "backfilled"

    def test_the_excluded_paper_does_not_stop_the_papers_that_can_be_written(
        self, tmp_path, capsys
    ):
        """Unlike an unreadable ledger (#111), an excluded source contaminates nothing —
        it tells us about no paper but itself, so it must not poison the batch."""
        kb = _kb(tmp_path)
        _arxiv_md(kb, "sources/sub/nested.md", "2301.00002", version=1)
        _arxiv_md(kb, "runs/sources/excluded.md", "2301.00003", version=1)

        code = run(["arxiv-backfill-provenance", "--target", str(kb)])
        out = capsys.readouterr().out
        assert code == 1  # the excluded paper is an error
        assert (kb / "source-provenance" / "sub" / "nested.json").is_file()  # ...and this ran
        assert "Backfilled:       1" in out
        assert "Errors:           1" in out
        assert "runs/sources/excluded.md" in out


# --------------------------------------------------------------------------- #
# the importer's index: the P3 half of #112, and the hidden-file trap it opened
# --------------------------------------------------------------------------- #
def _parsed_arxiv(arxiv_id="2311.09277", version=2) -> ParsedArxivWork:
    return ParsedArxivWork(
        arxiv_id=arxiv_id, version=version, title="A Paper", authors=("Ada Lovelace",),
        abstract="An abstract.", primary_category="cs.CL", categories=("cs.CL",),
        submitted=date(2023, 11, 15), last_updated=date(2023, 11, 20),
        withdrawn_by=None, abs_url="https://arxiv.org/abs/x", pdf_url="https://arxiv.org/pdf/x",
    )


def _parsed_openalex(openalex_id="W1", arxiv_id="2311.09277") -> ParsedWork:
    return ParsedWork(
        openalex_id=openalex_id, title="A Paper", authors=("Ada Lovelace",), year=2023,
        journal="Journal of Foo", doi=None, pmid=None, arxiv_id=arxiv_id, work_type="article",
    )


def _plant(kb, rel, work=None):
    """Put a real, writer-rendered arXiv original at *rel* — as `ingest`'s subtree mirroring
    or a human's `mv` would. Nothing else in the KB."""
    work = work or _parsed_arxiv()
    path = kb / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ArxivSourceWriter().render(work, IMPORTED_AT), encoding="utf-8")
    return path


def _md_count(kb):
    return len(list((kb / "sources").rglob("*.md")))


class TestTheImporterIndexSeesEveryMdOnDisk:
    """`BaseSourceWriter._index` is a duplicate guard over files that exist, not the KB's
    source enumeration. A `.md` it cannot see is a `.md` a re-import will write a second
    copy of — the silent P3 break #112 fixes for nested files, and the one it must not
    newly create for hidden ones."""

    def test_a_nested_original_is_reimported_as_skipped_not_duplicated(self, tmp_path):
        kb = _kb(tmp_path)
        _plant(kb, "sources/sub/x.md")
        result = ArxivSourceWriter().write(_parsed_arxiv(), kb, imported_at="2026-02-02T00:00:00Z")
        assert result.status == "skipped"
        assert _md_count(kb) == 1  # never a second .md for one paper

    def test_a_new_import_is_not_pushed_to_a_suffix_by_a_nested_namesake(self, tmp_path):
        """The counter-example that justifies keeping `claimed` flat. `sources/sub/x.md`
        occupies no name at the top level, so a genuinely new paper must land at its own
        slug, not at `<slug>-2.md`."""
        kb = _kb(tmp_path)
        other = _parsed_arxiv(arxiv_id="1706.03762")
        slug = ArxivSourceWriter().generate_slug(other)
        _plant(kb, f"sources/sub/{slug}", work=_parsed_arxiv(arxiv_id="2311.09277"))

        result = ArxivSourceWriter().write(other, kb, imported_at="2026-02-02T00:00:00Z")
        assert result.status == "imported"
        # Its own slug, not the `<stem>-2.md` a nested namesake would force if `claimed`
        # collected bare names from the whole subtree.
        assert result.path == kb / "sources" / slug
        assert not (kb / "sources" / f"{slug[:-3]}-2.md").exists()

    def test_a_merge_writes_the_nested_originals_sidecar_beside_it(self, tmp_path):
        """A cross-source merge must find the nested original AND land its ledger at the
        path every walker reads: `source-provenance/sub/x.json`."""
        kb = _kb(tmp_path)
        existing = _plant(kb, "sources/sub/x.md")

        result = OpenAlexSourceWriter().write(
            _parsed_openalex(), kb, imported_at="2026-02-02T00:00:00Z"
        )
        assert result.status == "merged"
        assert result.path == existing
        assert _md_count(kb) == 1

        sidecar = kb / "source-provenance" / "sub" / "x.json"
        assert sidecar.is_file()
        assert sidecar_path(existing, kb) == sidecar
        assert [r.type for r in read_provenance(sidecar).records] == ["openalex"]

    @pytest.mark.parametrize("rel", ["sources/.hidden-x.md", "sources/.h/x.md"])
    def test_a_hidden_original_still_claims_its_identity(self, tmp_path, rel):
        """`provenance_sources` excludes hidden paths because they are not *sources* (#67).
        The index must not: a hidden `.md` that already carries this arxiv_id is a file that
        would be duplicated. Filtering it here re-created, for hidden files, the exact silent
        P3 break #112 fixes for nested ones — measured: `imported`, two `.md` for one paper.
        A `skipped` the report names beats a duplicate nobody sees."""
        kb = _kb(tmp_path)
        _plant(kb, rel)
        result = ArxivSourceWriter().write(_parsed_arxiv(), kb, imported_at="2026-02-02T00:00:00Z")
        assert result.status == "skipped"
        assert _md_count(kb) == 1

    def test_a_hidden_original_is_still_not_a_source(self, tmp_path):
        """The two walks disagree on purpose, and this pins the disagreement: the index sees
        it, the ledger enumeration does not."""
        kb = _kb(tmp_path)
        _plant(kb, "sources/.hidden-x.md")
        assert provenance_sources(kb) == []
        assert cv.collect_ledger_entries(kb) == ([], [])


# --------------------------------------------------------------------------- #
# the remedy must be the one that works
# --------------------------------------------------------------------------- #
class TestTheRemedyIsMeasuredNotPlausible:
    def test_a_re_import_after_the_move_writes_no_ledger(self, tmp_path):
        """Why the message may not say "re-import". The identity match returns before the
        sidecar writer, so the re-import is a no-op that spends an API request."""
        kb = _kb(tmp_path)
        _plant(kb, "sources/x.md")  # already "moved" here
        result = ArxivSourceWriter().write(_parsed_arxiv(), kb, imported_at="2026-02-02T00:00:00Z")
        assert result.status == "skipped"
        assert not (kb / "source-provenance").exists()  # no ledger, ever

    def test_move_then_backfill_then_acknowledge_is_the_path_that_works(
        self, tmp_path, fake, capsys
    ):
        """End-to-end, the sequence the message prescribes. If this breaks, the message is
        a lie and the operator is sent nowhere."""
        kb = _kb(tmp_path)
        _arxiv_md(kb, "runs/sources/z.md", "2301.00003", version=1)

        # 1. the command names the working remedy, and never prescribes a re-import
        code = run(["arxiv-backfill-provenance", "--target", str(kb)])
        out = capsys.readouterr().out
        assert code == 1
        assert "re-import" in out and "no-op" in out  # named as what does NOT work
        assert "Move it under sources/" in out

        # 2. the move
        (kb / "sources").mkdir(exist_ok=True)
        (kb / "runs" / "sources" / "z.md").rename(kb / "sources" / "z.md")

        # 3. backfill (no network: a client would raise)
        assert run(["arxiv-backfill-provenance", "--target", str(kb)]) == 0
        capsys.readouterr()
        assert (kb / "source-provenance" / "z.json").is_file()

        # 4. acknowledge now succeeds
        fake(FakeClient([_work("2301.00003", version=1, withdrawn_by="author")]))
        assert run([
            "arxiv-acknowledge-withdrawal", "--id", "2301.00003",
            "--target", str(kb), "--yes",
        ]) == 0
        record = read_provenance(kb / "source-provenance" / "z.json").records[0]
        assert record.fields["withdrawn_by"] == "author"

    def test_backfill_does_not_tell_you_to_run_backfill(self, tmp_path, capsys):
        """The message prints inside this command's own output. Naming it as the fix sends
        the operator to the thing they have just run."""
        kb = _kb(tmp_path)
        _arxiv_md(kb, "runs/sources/z.md", "2301.00003", version=1)
        run(["arxiv-backfill-provenance", "--target", str(kb)])
        out = capsys.readouterr().out
        assert "re-run `factlog arxiv-backfill-provenance`" in out
        assert "and run `factlog arxiv-backfill-provenance`" not in out

    @pytest.mark.parametrize(
        "argv, command",
        [
            (["arxiv-check-versions"], "arxiv-backfill-provenance"),
            (["openalex-refresh"], "openalex-backfill-provenance"),
        ],
    )
    def test_a_check_command_names_the_backfill_command(
        self, tmp_path, capsys, monkeypatch, argv, command
    ):
        kb = _kb(tmp_path)
        _arxiv_md(kb, "runs/sources/z.md", "2301.00003", version=1)
        _openalex_md(kb, "runs/sources/w.md", "W3")
        monkeypatch.setattr(cli, "_make_arxiv_client", lambda c: pytest.fail("no request"))
        monkeypatch.setattr(cli, "_make_openalex_client", lambda c: pytest.fail("no request"))

        assert run(argv + ["--target", str(kb)]) == 1
        out = capsys.readouterr().out
        assert f"run `factlog {command}` (no network)" in out
        assert "no-op" in out


# --------------------------------------------------------------------------- #
# why an ingest conversion names no paper
# --------------------------------------------------------------------------- #
class TestAConversionCarriesNoFrontMatter:
    def test_the_header_not_the_extension_is_what_silences_a_conversion(self, tmp_path):
        """`SOURCE_SUFFIX` is `.md` and pandoc's `out_suffix` is `.md` too (cli.py:2121), so
        the extension filter is NOT what keeps a conversion quiet. `ingest` writes its
        provenance header on line 1, so `front_matter_block` (which requires the file to
        *start* with `---`) returns None and the conversion names no paper."""
        kb = _kb(tmp_path)
        conv = kb / "runs" / "sources"
        conv.mkdir(parents=True)
        # A conversion of a paper whose text happens to open with a front-matter block.
        conv_md = conv / "report.pdf.md"
        conv_md.write_text(
            "<!-- ingested-by-factlog | source: report.pdf | converter: pandoc -->\n\n"
            "---\narxiv_id: 2301.00099\n---\n\nbody\n",
            encoding="utf-8",
        )
        assert conv_md.suffix == ".md"  # the filter does not exclude it
        assert front_matter_block(conv_md) is None  # the header does
        assert read_scalars(conv_md, ("arxiv_id",)) == {}
        assert excluded_sources_by_id(kb, "arxiv_id") == {}
