# SPDX-License-Identifier: Apache-2.0
"""The PubMed backfill schema and command — what it records, refuses, and never touches (#172, #105).

What is pinned here, exercised against the real shared writer and the real CLI (never a
hand-rolled transcription of either):

* **A front-matter-only PubMed paper gets a sidecar with no network.** A fake ``efetch``
  transport that fails if called proves the command never goes upstream; a KB with no NCBI
  email configured proves it needs none.
* **``imported_at`` is the front matter's, verbatim.**
* **A ``.md`` with no readable ``pmid`` is left alone** — no ledger is invented that asserts
  an identity the front matter never carried (#73/#84).
* **A PMID two ``.md`` share gets a sidecar for each, order-independent** (#117).
* **A nested paper is covered** (the shared #112 walker).
* **After a backfill, ``pubmed-acknowledge-retraction`` can record the retraction** the
  paper had no ledger to hold before (#110/#171) — the whole point of the bootstrap.
* **``--dry-run`` writes nothing.**
* **The retraction signal is reproduced in the import's shape** (``retracted`` /
  ``retraction_notice_pmid``, #166) and **refused, never coerced,** when hand-typed as a
  non-boolean (#109).
* **No ``.md`` is opened for write** (P4).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from factlog import cli
import factlog.integrations.common.backfill as bf
import factlog.integrations.pubmed.backfill as pb
from factlog.integrations.common.provenance import (
    ProvenanceError,
    SourceRecord,
    read_provenance,
    sidecar_path,
)
from factlog.integrations.pubmed import refresh
from factlog.integrations.pubmed.source_writer import PubMedSourceWriter

_STAMP = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    (tmp_path / "sources").mkdir()
    return tmp_path


def _source(kb: Path, name: str, front_matter: str) -> Path:
    path = kb / "sources" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front_matter}---\n\n# A paper\n", encoding="utf-8")
    return path


def _run(kb: Path, dry_run: bool = False) -> dict[str, bf.BackfillResult]:
    return {r.entry_id: r for r in bf.backfill(kb, pb.backfill_schema(), dry_run=dry_run)}


def _records(kb: Path, md_name: str) -> list[dict]:
    sidecar = sidecar_path(kb / "sources" / md_name, kb)
    return [r.to_dict() for r in read_provenance(sidecar).records]


# --------------------------------------------------------------------------- #
# it writes a ledger from front matter, without a network
# --------------------------------------------------------------------------- #
class TestABackfilledRecordMaterializesTheFrontMatter:
    def test_a_plain_paper_gets_a_sidecar_with_imported_at_verbatim(self, kb: Path):
        _source(kb, "a.md", f'pmid: 32738937\nimported_from: pubmed\nimported_at: "{_STAMP}"\n')
        assert _run(kb)["32738937"].status == bf.BACKFILL_WRITTEN
        (record,) = _records(kb, "a.md")
        assert record == {"type": "pubmed", "id": "32738937", "imported_at": _STAMP}

    def test_doi_and_journal_are_recorded_when_the_front_matter_carries_them(self, kb: Path):
        _source(
            kb, "a.md",
            f'pmid: 32738937\njournal: "Nature"\ndoi: "10.1/x"\n'
            f'imported_from: pubmed\nimported_at: "{_STAMP}"\n',
        )
        _run(kb)
        (record,) = _records(kb, "a.md")
        assert record["journal"] == "Nature"
        assert record["doi"] == "10.1/x"

    def test_a_field_the_md_lacks_is_omitted_not_nulled(self, kb: Path):
        _source(kb, "a.md", f'pmid: 32738937\nimported_at: "{_STAMP}"\n')
        _run(kb)
        (record,) = _records(kb, "a.md")
        assert set(record) == {"type", "id", "imported_at"}  # no doi/journal/retracted keys

    def test_the_retraction_signal_is_reproduced_in_the_imports_shape(self, kb: Path):
        """#166: the import writes `retracted` and, when linkable, `retraction_notice_pmid`.
        A backfill reproduces both verbatim so the ledger has the same field shape."""
        _source(
            kb, "a.md",
            f'pmid: 32738937\njournal: "J"\nimported_from: pubmed\nimported_at: "{_STAMP}"\n'
            "pubmed_retracted: true\npubmed_retraction_notice_pmid: 18842931\n",
        )
        assert _run(kb)["32738937"].status == bf.BACKFILL_WRITTEN
        (record,) = _records(kb, "a.md")
        assert record["retracted"] is True  # a real bool, not the string "true"
        assert record["retraction_notice_pmid"] == "18842931"

    def test_retracted_is_true_or_absent_never_false(self, kb: Path):
        _source(kb, "plain.md", f'pmid: 1\nimported_at: "{_STAMP}"\n')
        _source(kb, "flagged.md", f'pmid: 2\nimported_at: "{_STAMP}"\npubmed_retracted: true\n')
        _run(kb)
        assert "retracted" not in _records(kb, "plain.md")[0]  # absence *means* not retracted
        assert _records(kb, "flagged.md")[0]["retracted"] is True

    def test_a_notice_pmid_without_a_retraction_is_dropped(self, kb: Path):
        """The writer emits the notice only alongside a retraction; a stray notice on a
        non-retracted paper is not a shape the import produces, so it is not recorded."""
        _source(
            kb, "a.md",
            f'pmid: 1\nimported_at: "{_STAMP}"\npubmed_retraction_notice_pmid: 999\n',
        )
        _run(kb)
        (record,) = _records(kb, "a.md")
        assert "retraction_notice_pmid" not in record
        assert "retracted" not in record

    def test_retraction_verified_at_is_not_invented(self, kb: Path):
        """The import clock is not a front-matter key. A backfill consulted PubMed at no
        time, so it writes no `retraction_verified_at` — the documented asymmetry."""
        _source(
            kb, "a.md",
            f'pmid: 1\nimported_at: "{_STAMP}"\npubmed_retracted: true\n',
        )
        _run(kb)
        assert "retraction_verified_at" not in _records(kb, "a.md")[0]


# --------------------------------------------------------------------------- #
# no network, ever
# --------------------------------------------------------------------------- #
class TestNoNetwork:
    def test_the_module_imports_no_pubmed_client(self):
        """A backfill that queried PubMed would be a refresh, whose write is
        ``update_source``. This module has no import that could reach the transport."""
        source = Path(pb.__file__).read_text(encoding="utf-8")
        imports = [
            line for line in source.splitlines() if line.startswith(("import ", "from "))
        ]
        assert imports and not any("client" in line for line in imports)

    def test_the_command_writes_without_an_efetch_and_without_an_email(
        self, kb: Path, monkeypatch, capsys
    ):
        """A fake transport that fails if constructed proves the command never goes
        upstream; the KB has no pubmed-config.toml, so it also proves no email is needed."""
        def _explode(config):
            raise AssertionError("pubmed-backfill-provenance must not touch the network")

        monkeypatch.setattr(cli, "_make_pubmed_client", _explode)
        _source(kb, "a.md", f'pmid: 32738937\nimported_from: pubmed\nimported_at: "{_STAMP}"\n')

        args = cli.build_parser().parse_args(["pubmed-backfill-provenance", "--target", str(kb)])
        assert args.func(args) == 0
        assert _records(kb, "a.md")[0]["id"] == "32738937"


# --------------------------------------------------------------------------- #
# #73/#84 — a source with no readable pmid gets no invented ledger
# --------------------------------------------------------------------------- #
class TestARefusalToInventAnIdentity:
    def test_a_source_with_no_pmid_gets_no_sidecar(self, kb: Path):
        """A `.md` that names a PubMed origin but carries no readable `pmid` cannot be given
        a ledger asserting a PMID it never held — a wrong ledger is worse than none."""
        _source(kb, "no-pmid.md", f'imported_from: pubmed\nimported_at: "{_STAMP}"\n')
        _source(kb, "ok.md", f'pmid: 32738937\nimported_at: "{_STAMP}"\n')

        results = _run(kb)

        assert list(results) == ["32738937"]  # only the paper with a real id is acted on
        assert not sidecar_path(kb / "sources" / "no-pmid.md", kb).exists()
        assert sidecar_path(kb / "sources" / "ok.md", kb).exists()

    def test_a_paper_without_imported_at_is_refused_and_nothing_is_written(self, kb: Path):
        _source(kb, "a.md", "pmid: 32738937\nimported_from: pubmed\n")
        result = _run(kb)["32738937"]
        assert result.status == bf.BACKFILL_REFUSED
        assert "imported_at" in result.reason
        assert not (kb / "source-provenance").exists()


# --------------------------------------------------------------------------- #
# #117 — a shared PMID, one sidecar per .md, order-independent
# --------------------------------------------------------------------------- #
class TestASharedPmidIsCoveredPerSourceAndDeterministically:
    def test_both_sources_sharing_a_pmid_get_their_own_sidecar(self, kb: Path):
        _source(kb, "a_first.md", f'pmid: 32738937\nimported_at: "{_STAMP}"\n')
        _source(kb, "z_second.md", f'pmid: 32738937\nimported_at: "{_STAMP}"\n')

        results = _run(kb)

        assert results["32738937"].status == bf.BACKFILL_WRITTEN
        for name in ("a_first.md", "z_second.md"):
            assert sidecar_path(kb / "sources" / name, kb).exists()
            assert _records(kb, name)[0]["id"] == "32738937"

    def test_coverage_does_not_depend_on_walk_order(self, kb: Path, monkeypatch):
        """Reversing the enumeration must not change which files are covered (#117, P3)."""
        _source(kb, "a_first.md", f'pmid: 32738937\nimported_at: "{_STAMP}"\n')
        _source(kb, "z_second.md", f'pmid: 32738937\nimported_at: "{_STAMP}"\n')

        real = pb.provenance_sources
        forward = {(r.entry_id, r.ledger, r.status) for r in bf.backfill(kb, pb.backfill_schema(), dry_run=True)}
        monkeypatch.setattr(pb, "provenance_sources", lambda root: list(reversed(real(root))))
        reversed_ = {(r.entry_id, r.ledger, r.status) for r in bf.backfill(kb, pb.backfill_schema(), dry_run=True)}

        assert forward == reversed_
        assert len(forward) == 2  # both files, either way


class TestANestedPaperIsCovered:
    def test_a_source_in_a_subdirectory_gets_a_sidecar(self, kb: Path):
        _source(kb, "nested/deep/paper.md", f'pmid: 32738937\nimported_at: "{_STAMP}"\n')
        assert _run(kb)["32738937"].status == bf.BACKFILL_WRITTEN
        sidecar = sidecar_path(kb / "sources" / "nested" / "deep" / "paper.md", kb)
        assert sidecar.exists()
        assert read_provenance(sidecar).records[0].id == "32738937"


# --------------------------------------------------------------------------- #
# the retraction flag is never coerced (#98/#109)
# --------------------------------------------------------------------------- #
class TestTheRetractionFlagIsNeverCoerced:
    @pytest.mark.parametrize("literal", ["1", "0", "yes", "no", "on", "off", "maybe"])
    def test_a_non_boolean_flag_is_refused_by_id_and_nothing_is_written(
        self, kb: Path, literal: str
    ):
        md = _source(kb, "a.md", f'pmid: 1\nimported_at: "{_STAMP}"\npubmed_retracted: {literal}\n')
        before = os.stat(md).st_mtime_ns

        result = _run(kb)["1"]

        assert result.status == bf.BACKFILL_REFUSED
        assert "retracted" in result.reason
        assert not (kb / "source-provenance").exists()  # neither true nor false guessed at
        assert os.stat(md).st_mtime_ns == before

    def test_the_raw_value_reaches_the_view_verbatim(self, kb: Path):
        _source(kb, "a.md", f'pmid: 1\nimported_at: "{_STAMP}"\npubmed_retracted: yes\n')
        (entry,) = pb._collect_entries(kb)[0]
        (view,) = entry.per_source
        assert view.retracted == "yes"

    def test_one_bad_paper_does_not_block_its_neighbours(self, kb: Path):
        _source(kb, "clean.md", f'pmid: 1\nimported_at: "{_STAMP}"\n')
        _source(kb, "dirty.md", f'pmid: 2\nimported_at: "{_STAMP}"\npubmed_retracted: yes\n')

        results = _run(kb)

        assert results["1"].status == bf.BACKFILL_WRITTEN
        assert results["2"].status == bf.BACKFILL_REFUSED
        assert sidecar_path(kb / "sources" / "clean.md", kb).exists()
        assert not sidecar_path(kb / "sources" / "dirty.md", kb).exists()

    def test_the_ledger_value_space_rejects_a_non_boolean_pubmed_retracted(self, kb: Path):
        """The refusal above has teeth because ``read_provenance`` fixes the value space for
        ``("pubmed", "retracted")`` — a hand-written string ledger is refused on read, so a
        backfill that promoted one would write a file the reader below rejects."""
        sidecar = kb / "source-provenance" / "hand.json"
        sidecar.parent.mkdir()
        sidecar.write_text(
            '{"schema_version": 1, "records": [{"type": "pubmed", "id": "1", '
            f'"imported_at": "{_STAMP}", "retracted": "yes"}}]}}\n',
            encoding="utf-8",
        )
        with pytest.raises(ProvenanceError):
            read_provenance(sidecar)


# --------------------------------------------------------------------------- #
# no-ops and neighbours
# --------------------------------------------------------------------------- #
class TestNoOpsAndNeighbours:
    def test_a_ledger_backed_paper_is_skipped(self, kb: Path):
        _source(kb, "has-ledger.md", f'pmid: 1\nimported_at: "{_STAMP}"\n')
        sidecar = sidecar_path(kb / "sources" / "has-ledger.md", kb)
        sidecar.parent.mkdir()
        sidecar.write_text(
            '{"schema_version": 1, "records": [{"type": "pubmed", "id": "1", '
            f'"imported_at": "{_STAMP}"}}]}}\n',
            encoding="utf-8",
        )
        _source(kb, "clean.md", f'pmid: 2\nimported_at: "{_STAMP}"\n')

        results = _run(kb)

        assert "1" not in results  # classified "ledger", skipped entirely
        assert results["2"].status == bf.BACKFILL_WRITTEN

    def test_a_second_backfill_is_a_byte_and_mtime_identical_no_op(self, kb: Path):
        _source(kb, "a.md", f'pmid: 1\nimported_at: "{_STAMP}"\n')
        _run(kb)
        sidecar = sidecar_path(kb / "sources" / "a.md", kb)
        before_bytes, before_mtime = sidecar.read_bytes(), os.stat(sidecar).st_mtime_ns

        second = _run(kb)

        assert "1" not in second  # now ledger-backed
        assert sidecar.read_bytes() == before_bytes
        assert os.stat(sidecar).st_mtime_ns == before_mtime

    def test_a_neighbours_record_is_left_alone(self, kb: Path):
        """A pre-ledger cross-source merge may already carry another integration's record."""
        _source(
            kb, "a.md",
            f'pmid: 1\nopenalex_id: "W1"\nimported_at: "{_STAMP}"\n',
        )
        sidecar = sidecar_path(kb / "sources" / "a.md", kb)
        sidecar.parent.mkdir()
        sidecar.write_text(
            '{"schema_version": 1, "records": [{"type": "openalex", "id": "W1", '
            '"imported_at": "2024-01-01T00:00:00Z"}]}\n',
            encoding="utf-8",
        )

        assert _run(kb)["1"].status == bf.BACKFILL_WRITTEN

        records = {r["type"]: r for r in _records(kb, "a.md")}
        assert records["openalex"] == {
            "type": "openalex", "id": "W1", "imported_at": "2024-01-01T00:00:00Z",
        }
        assert records["pubmed"]["imported_at"] == _STAMP

    def test_a_corrupt_sidecar_is_a_per_id_error_not_a_crash(self, kb: Path):
        _source(kb, "a.md", f'pmid: 1\nimported_at: "{_STAMP}"\n')
        (kb / "source-provenance").mkdir()
        (kb / "source-provenance" / "a.json").write_text("{ not json", encoding="utf-8")

        results = _run(kb)

        assert [r.status for r in results.values()] == [bf.BACKFILL_ERROR]


# --------------------------------------------------------------------------- #
# --dry-run writes nothing and agrees with the run
# --------------------------------------------------------------------------- #
class TestPreviewWritesNothing:
    def test_a_preview_writes_nothing(self, kb: Path):
        md = _source(kb, "a.md", f'pmid: 1\nimported_at: "{_STAMP}"\n')
        before = os.stat(md).st_mtime_ns
        _run(kb, dry_run=True)
        assert not (kb / "source-provenance").exists()
        assert os.stat(md).st_mtime_ns == before

    def test_every_id_is_classified_identically(self, kb: Path):
        _source(kb, "a_ok.md", f'pmid: 1\nimported_at: "{_STAMP}"\n')
        _source(kb, "b_no_stamp.md", "pmid: 2\n")
        _source(kb, "c_bad_flag.md", f'pmid: 3\nimported_at: "{_STAMP}"\npubmed_retracted: yes\n')

        preview = {i: r.status for i, r in _run(kb, dry_run=True).items()}
        assert not (kb / "source-provenance").exists()
        real = {i: r.status for i, r in _run(kb).items()}

        assert preview == real
        assert preview["1"] == bf.BACKFILL_WRITTEN
        assert preview["2"] == bf.BACKFILL_REFUSED  # no imported_at
        assert preview["3"] == bf.BACKFILL_REFUSED  # non-boolean flag


# --------------------------------------------------------------------------- #
# the schema itself
# --------------------------------------------------------------------------- #
class TestTheSchemaItself:
    def test_required_is_empty_because_pubmed_declares_no_identifying_field(self):
        assert PubMedSourceWriter._IDENTIFYING_FIELDS == ()
        assert pb.backfill_schema().required == ()

    def test_the_fields_are_the_front_matter_backed_ledger_fields(self):
        assert set(pb.backfill_schema().fields) == {
            "doi", "journal", "retracted", "retraction_notice_pmid"
        }

    def test_membership_is_decided_by_pubmeds_own_predicate(self):
        assert pb.backfill_schema().provenance_of is refresh.provenance_of

    def test_the_collector_reuses_pubmeds_own_collect_ledger_entries(self, kb, monkeypatch):
        seen: list[Path] = []
        real = refresh.collect_ledger_entries
        monkeypatch.setattr(
            refresh, "collect_ledger_entries",
            lambda root: (seen.append(Path(root)), real(root))[1],
        )
        _run(kb)
        assert seen == [kb]

    def test_the_front_matter_keys_are_source_scoped(self):
        assert pb.RETRACTION_KEY == "pubmed_retracted"
        assert pb.RETRACTION_NOTICE_KEY == "pubmed_retraction_notice_pmid"


# --------------------------------------------------------------------------- #
# the bootstrap this command exists for: acknowledge works after a backfill
# --------------------------------------------------------------------------- #
def _retracted_efetch(pmid: str, notice_pmid: str = "18842931") -> str:
    notice = (
        f'<CommentsCorrectionsList><CommentsCorrections RefType="RetractionIn">'
        f"<PMID>{notice_pmid}</PMID></CommentsCorrections></CommentsCorrectionsList>"
    )
    return (
        "<PubmedArticleSet><PubmedArticle><MedlineCitation>"
        f"<PMID>{pmid}</PMID><Article><Journal><Title>J</Title>"
        "<JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue></Journal>"
        "<ArticleTitle>A retracted paper</ArticleTitle>"
        "<AuthorList><Author><LastName>A</LastName><ForeName>B</ForeName></Author></AuthorList>"
        "<PublicationTypeList>"
        '<PublicationType UI="D016428">Journal Article</PublicationType>'
        '<PublicationType UI="D016441">Retracted Publication</PublicationType>'
        f"</PublicationTypeList></Article>{notice}</MedlineCitation>"
        f'<PubmedData><ArticleIdList><ArticleId IdType="pubmed">{pmid}</ArticleId>'
        "</ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>"
    )


class _FakeEfetch:
    def __init__(self, xml: str):
        self._xml = xml
        self.calls: list[list[str]] = []

    def efetch(self, pmids):
        self.calls.append([str(p) for p in pmids])
        return self._xml


class TestTheAcknowledgeBootstrap:
    def test_a_backfilled_paper_can_have_its_retraction_acknowledged(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """A pre-ledger paper PubMed later retracts: acknowledge refuses it (no ledger, #110),
        the backfill builds one from front matter, and acknowledge then records the retraction
        — the repeat is silenced. The whole reason this command exists."""
        kb = tmp_path
        (kb / "sources").mkdir()
        (kb / "policy").mkdir()
        (kb / "policy" / "pubmed-config.toml").write_text(
            '[client]\nemail = "test@example.com"\n', encoding="utf-8"
        )
        _source(kb, "paper.md", f'pmid: 32738937\nimported_from: pubmed\nimported_at: "{_STAMP}"\n')

        client = _FakeEfetch(_retracted_efetch("32738937"))
        monkeypatch.setattr(cli, "_make_pubmed_client", lambda config: client)

        def run(argv):
            return cli.build_parser().parse_args(argv).func(cli.build_parser().parse_args(argv))

        # Before the backfill, acknowledge refuses and spends no request (#110).
        assert run(["pubmed-acknowledge-retraction", "--id", "32738937",
                    "--target", str(kb), "--yes"]) == 1
        assert client.calls == []
        assert "pubmed-backfill-provenance" in capsys.readouterr().err

        # Backfill builds the ledger from front matter, no network.
        assert run(["pubmed-backfill-provenance", "--target", str(kb)]) == 0
        assert read_provenance(sidecar_path(kb / "sources" / "paper.md", kb)).records[0].id == "32738937"

        # Now the retraction can be acknowledged: one live efetch, `retracted: True` recorded.
        assert run(["pubmed-acknowledge-retraction", "--id", "32738937",
                    "--target", str(kb), "--yes"]) == 0
        assert client.calls == [["32738937"]]
        record = {(r.type, r.id): r.to_dict()
                  for r in read_provenance(sidecar_path(kb / "sources" / "paper.md", kb)).records}
        assert record[("pubmed", "32738937")]["retracted"] is True
