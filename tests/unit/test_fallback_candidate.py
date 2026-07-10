# SPDX-License-Identifier: Apache-2.0
"""The title+author+year fallback surfaces a candidate and NEVER merges (#75).

The load-bearing behaviours, each verified against real writer/importer/CLI code:

* a fallback match imports the paper as a NEW file — never merged, never skipped;
* the existing source is byte- AND mtime-unchanged (nothing is folded into it);
* the surfaced pair is recorded ``pending`` and never re-proposed on re-import;
* ``--dry-run`` writes neither a source nor a candidate record;
* Zotero surfaces nothing, ever;
* ``merge-candidates/`` is invisible to ``sources``/``status``/``export`` (measured
  with the real CLI), and a SOURCE_ROOTS guard proves that placement is the reason;
* a corrupt ledger fails closed — no surface, no crash, no erase;
* the two surname serializations agree through the writer;
* the false-merge regression: the real ``arXiv:2509.00891`` AAAI-vs-medRxiv pair.
"""
from __future__ import annotations

from datetime import date

import pytest

from factlog import cli, common
from factlog.integrations.arxiv.importer import import_works as arxiv_import
from factlog.integrations.arxiv.work_parser import ParsedArxivWork
from factlog.integrations.common.merge_candidates import (
    STATE_PENDING,
    STATE_REJECTED,
    CandidatePair,
    MergeCandidates,
    add_candidate,
    candidates_path,
    read_candidates,
    write_candidates,
)
from factlog.integrations.common.provenance import sidecar_path
from factlog.integrations.openalex.importer import import_works as oa_import
from factlog.integrations.openalex.work_parser import ParsedWork
from factlog.integrations.zotero.source_writer import SourceWriter as ZoteroWriter

T = "2026-07-09T00:00:00Z"

# The real harmful pair the spike named (docs/spike-fallback-precision.md): one
# arXiv paper by Zonghai Yao that appears at AAAI and, as a genuinely different
# work of the same title, on medRxiv. Byte-identical title, same first-author
# surname, adjacent years, DIFFERENT DOIs — so priorities 1-3 (DOI/PMID/arXiv id)
# all miss and only the fallback fires.
YAO_TITLE = "Large language models for clinical note understanding"
AAAI_DOI = "10.1609/aaai.v40i46.41305"
MEDRXIV_DOI = "10.1101/2025.09.02.25334973"


def _oa(openalex_id, doi, *, title=YAO_TITLE, authors=("Zonghai Yao", "A Coauthor"),
        year=2025, arxiv_id=None):
    return ParsedWork(openalex_id=openalex_id, title=title, authors=authors,
                      year=year, doi=doi, arxiv_id=arxiv_id, work_type="article")


def _arxiv(arxiv_id, *, title=YAO_TITLE, authors=("Zonghai Yao",), doi=None, year=2025):
    return ParsedArxivWork(
        arxiv_id=arxiv_id, version=1, title=title, authors=authors,
        abstract="An abstract.", primary_category="cs.CL", categories=("cs.CL",),
        submitted=date(year, 9, 1), last_updated=date(year, 9, 1), doi=doi,
        journal_ref=None, comment=None, withdrawn_by=None,
        abs_url=f"https://arxiv.org/abs/{arxiv_id}v1",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v1")


def _md_files(kb):
    return sorted(p.name for p in (kb / "sources").glob("*.md"))


def _seed_kb(tmp_path):
    (tmp_path / "sources").mkdir(parents=True)
    return tmp_path


# --------------------------------------------------------------------------- #
# The false-merge regression, on the real pair.
# --------------------------------------------------------------------------- #
class TestFalseMergeRegression:
    def _seed_and_import(self, kb):
        oa_import([_oa("W_AAAI", AAAI_DOI)], target=kb, imported_at=T)
        existing = (kb / "sources").glob("*.md").__next__()
        before_bytes = existing.read_bytes()
        before_mtime = existing.stat().st_mtime_ns
        sidecar = sidecar_path(existing, kb)
        before_sidecar = sidecar.read_bytes()
        before_sidecar_mtime = sidecar.stat().st_mtime_ns
        report = oa_import([_oa("W_MEDRXIV", MEDRXIV_DOI)], target=kb, imported_at=T)
        return report, existing, (before_bytes, before_mtime, before_sidecar,
                                  before_sidecar_mtime)

    def test_imports_a_new_file_never_merged_or_skipped(self, tmp_path):
        kb = _seed_kb(tmp_path)
        report, _existing, _ = self._seed_and_import(kb)
        (outcome,) = report.outcomes
        assert outcome.status == "imported"  # not "merged", not "skipped"
        assert report.merged == 0 and report.skipped == 0
        assert len(_md_files(kb)) == 2  # a genuinely new file

    def test_existing_source_is_byte_and_mtime_unchanged(self, tmp_path):
        kb = _seed_kb(tmp_path)
        _report, existing, (b_bytes, b_mtime, s_bytes, s_mtime) = self._seed_and_import(kb)
        assert existing.read_bytes() == b_bytes
        assert existing.stat().st_mtime_ns == b_mtime
        sidecar = sidecar_path(existing, tmp_path)
        assert sidecar.read_bytes() == s_bytes
        assert sidecar.stat().st_mtime_ns == s_mtime

    def test_candidate_is_surfaced(self, tmp_path):
        kb = _seed_kb(tmp_path)
        report, _existing, _ = self._seed_and_import(kb)
        (outcome,) = report.outcomes
        assert outcome.candidate is not None
        assert outcome.candidate.incoming == ("openalex", "W_MEDRXIV")
        assert outcome.candidate.existing == ("openalex", "W_AAAI")
        assert outcome.candidate.score == 1.0  # byte-identical title
        assert len(report.candidates) == 1
        # ...and recorded pending in the ledger.
        ledger = read_candidates(candidates_path(kb))
        assert len(ledger.pairs) == 1
        assert ledger.pairs[0].state == STATE_PENDING

    def test_reimport_does_not_repropose(self, tmp_path):
        kb = _seed_kb(tmp_path)
        self._seed_and_import(kb)
        before = candidates_path(kb).read_bytes()
        report = oa_import([_oa("W_MEDRXIV", MEDRXIV_DOI)], target=kb, imported_at=T)
        (outcome,) = report.outcomes
        assert outcome.status == "skipped"  # identity dedup
        assert outcome.candidate is None
        assert candidates_path(kb).read_bytes() == before  # byte-unchanged


# --------------------------------------------------------------------------- #
# Never merges / never skips, and suppression by the ledger directly.
# --------------------------------------------------------------------------- #
class TestNeverMerges:
    def test_a_pending_pair_is_not_reproposed_even_for_a_fresh_import(self, tmp_path):
        # Directly exercise ledger suppression: the pair is already pending (e.g. the
        # medRxiv file was deleted), so importing it again surfaces NOTHING.
        kb = _seed_kb(tmp_path)
        oa_import([_oa("W_AAAI", AAAI_DOI)], target=kb, imported_at=T)
        led = MergeCandidates()
        add_candidate(led, CandidatePair.create(
            ("openalex", "W_MEDRXIV"), ("openalex", "W_AAAI"),
            state=STATE_PENDING, score=1.0, recorded_at=T))
        write_candidates(candidates_path(kb), led)
        report = oa_import([_oa("W_MEDRXIV", MEDRXIV_DOI)], target=kb, imported_at=T)
        (outcome,) = report.outcomes
        assert outcome.status == "imported"
        assert outcome.candidate is None  # suppressed by the ledger

    def test_a_rejected_pair_is_not_reproposed(self, tmp_path):
        kb = _seed_kb(tmp_path)
        oa_import([_oa("W_AAAI", AAAI_DOI)], target=kb, imported_at=T)
        led = MergeCandidates()
        add_candidate(led, CandidatePair.create(
            ("openalex", "W_MEDRXIV"), ("openalex", "W_AAAI"),
            state=STATE_REJECTED, score=1.0, recorded_at=T))
        write_candidates(candidates_path(kb), led)
        report = oa_import([_oa("W_MEDRXIV", MEDRXIV_DOI)], target=kb, imported_at=T)
        assert report.outcomes[0].candidate is None

    def test_cross_source_candidate_arxiv_vs_openalex(self, tmp_path):
        # arXiv incoming resembles an OpenAlex-imported source; no shared id.
        kb = _seed_kb(tmp_path)
        oa_import([_oa("W_AAAI", AAAI_DOI, arxiv_id=None)], target=kb, imported_at=T)
        report = arxiv_import([_arxiv("2509.00891")], target=kb, imported_at=T)
        (outcome,) = report.outcomes
        assert outcome.status == "imported"
        assert outcome.candidate is not None
        assert outcome.candidate.existing == ("openalex", "W_AAAI")
        assert outcome.candidate.incoming == ("arxiv", "2509.00891")

    def test_surname_serializations_agree_through_the_writer(self, tmp_path):
        # Existing file stores the comma form; incoming is the display form.
        kb = _seed_kb(tmp_path)
        oa_import([_oa("W_AAAI", AAAI_DOI, authors=("Yao, Zonghai",))],
                  target=kb, imported_at=T)
        report = oa_import([_oa("W_MEDRXIV", MEDRXIV_DOI, authors=("Zonghai Yao",))],
                           target=kb, imported_at=T)
        assert report.outcomes[0].candidate is not None  # "yao" == "yao"

    def test_no_candidate_when_titles_differ(self, tmp_path):
        kb = _seed_kb(tmp_path)
        oa_import([_oa("W_AAAI", AAAI_DOI, title="A totally unrelated paper")],
                  target=kb, imported_at=T)
        report = oa_import([_oa("W_MEDRXIV", MEDRXIV_DOI, title="Something else entirely")],
                           target=kb, imported_at=T)
        assert report.outcomes[0].candidate is None
        assert not candidates_path(kb).exists()  # nothing surfaced -> no ledger


# --------------------------------------------------------------------------- #
# Dry run writes nothing.
# --------------------------------------------------------------------------- #
class TestDryRun:
    def test_dry_run_writes_neither_source_nor_candidate(self, tmp_path):
        kb = _seed_kb(tmp_path)
        oa_import([_oa("W_AAAI", AAAI_DOI)], target=kb, imported_at=T)
        before = _md_files(kb)
        report = oa_import([_oa("W_MEDRXIV", MEDRXIV_DOI)], target=kb, imported_at=T,
                           dry_run=True)
        # The candidate is still PREVIEWED (a pure read)...
        assert report.outcomes[0].candidate is not None
        # ...but no file and no ledger were written.
        assert _md_files(kb) == before
        assert not candidates_path(kb).exists()


# --------------------------------------------------------------------------- #
# Zotero surfaces nothing (H2).
# --------------------------------------------------------------------------- #
class TestZoteroNeverSurfaces:
    def test_zotero_writer_does_not_opt_in(self):
        assert ZoteroWriter().surfaces_candidates is False

    def test_zotero_import_surfaces_no_candidate(self, tmp_path):
        kb = _seed_kb(tmp_path)
        # Seed an OpenAlex source, then import a Zotero item of the same work.
        oa_import([_oa("W_AAAI", AAAI_DOI)], target=kb, imported_at=T)
        item = {
            "zotero_key": "K1", "item_type": "journalArticle", "title": YAO_TITLE,
            "authors": [{"name": "Zonghai Yao"}], "year": "2025", "doi": "10.9/other",
        }
        result = ZoteroWriter().write(item, kb, imported_at=T)
        assert result.status == "imported"
        assert result.candidate is None
        assert not candidates_path(kb).exists()


# --------------------------------------------------------------------------- #
# The ledger is invisible to source enumeration (MEASURED with the real CLI).
# --------------------------------------------------------------------------- #
_BIB = (
    '---\nzotero_key: "K1"\nitem_type: "journalArticle"\ntitle: "A Study"\n'
    'authors: ["Doe J"]\nyear: "2020"\ndoi: "10.1/x"\n---\n\n# body\n'
)


def _cli_kb(tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "doe-2020-a-study.md").write_text(_BIB, encoding="utf-8")
    return tmp_path


def _add_ledger(kb):
    led = MergeCandidates()
    add_candidate(led, CandidatePair.create(
        ("openalex", "W1"), ("arxiv", "2509.00891"),
        state=STATE_PENDING, score=1.0, recorded_at=T))
    write_candidates(candidates_path(kb), led)


class TestCliInvisibility:
    @pytest.mark.parametrize("argv_tail", [
        ["sources"], ["status"], ["export", "--bibtex"], ["export", "--csl"],
    ])
    def test_output_identical_with_and_without_ledger(self, tmp_path, capsys, argv_tail):
        kb = _cli_kb(tmp_path)
        argv = [*argv_tail, "--target", str(kb)]
        rc_without = cli.main(argv)
        without = capsys.readouterr()
        _add_ledger(kb)
        assert candidates_path(kb).is_file()
        rc_with = cli.main(argv)
        with_ = capsys.readouterr()
        assert rc_without == rc_with
        assert without.out == with_.out
        assert without.err == with_.err

    def test_sources_still_reports_one_source(self, tmp_path, capsys):
        kb = _cli_kb(tmp_path)
        _add_ledger(kb)
        rc = cli.main(["sources", "--target", str(kb)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "1 source(s)" in out
        assert "merge-candidates" not in out


class TestGuardOnSourceRoots:
    """Invisibility must rest on ``merge-candidates`` being outside SOURCE_ROOTS,
    not on luck (#58/#63): adding it must surface the file."""

    def test_source_files_ignores_the_ledger(self, tmp_path):
        kb = _cli_kb(tmp_path)
        _add_ledger(kb)
        assert candidates_path(kb) not in common.source_files(kb)

    def test_adding_dir_to_source_roots_surfaces_it(self, tmp_path, monkeypatch):
        kb = _cli_kb(tmp_path)
        _add_ledger(kb)
        monkeypatch.setattr(common, "SOURCE_ROOTS", (*common.SOURCE_ROOTS, "merge-candidates"))
        assert candidates_path(kb) in common.source_files(kb)


# --------------------------------------------------------------------------- #
# A corrupt ledger fails closed.
# --------------------------------------------------------------------------- #
class TestCorruptLedgerFailsClosed:
    def test_find_does_not_surface_and_does_not_crash(self, tmp_path):
        kb = _seed_kb(tmp_path)
        oa_import([_oa("W_AAAI", AAAI_DOI)], target=kb, imported_at=T)
        path = candidates_path(kb)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ corrupt")  # invalid JSON
        report = oa_import([_oa("W_MEDRXIV", MEDRXIV_DOI)], target=kb, imported_at=T)
        (outcome,) = report.outcomes
        assert outcome.status == "imported"        # the paper still imports
        assert outcome.candidate is None            # nothing surfaced
        assert path.read_text() == "{ corrupt"      # the corrupt file is NOT erased


# --------------------------------------------------------------------------- #
# CLI output: the candidate line in human and --porcelain form.
# --------------------------------------------------------------------------- #
class TestCliCandidateOutput:
    def _report(self, tmp_path):
        kb = _seed_kb(tmp_path)
        oa_import([_oa("W_AAAI", AAAI_DOI)], target=kb, imported_at=T)
        return kb, oa_import([_oa("W_MEDRXIV", MEDRXIV_DOI)], target=kb, imported_at=T)

    def test_porcelain_has_candidate_token_and_count(self, tmp_path, capsys):
        kb, report = self._report(tmp_path)
        cli._openalex_finish(report, kb, dry_run=False, porcelain=True, warning="")
        out = capsys.readouterr().out
        lines = out.splitlines()
        # The six summary tokens are present and unchanged in shape.
        first_fields = {ln.split("\t")[0] for ln in lines}
        assert {"imported", "skipped", "merged", "errors", "dry_run", "target"} <= first_fields
        # The new tokens are added.
        cand = [ln for ln in lines if ln.startswith("candidate\t")]
        assert len(cand) == 1
        parts = cand[0].split("\t")
        assert parts[1] == "W_MEDRXIV"
        assert parts[2].endswith(".md")
        assert parts[3] == "1.0000"
        assert "candidates\t1" in lines

    def test_porcelain_six_summary_lines_byte_unchanged_vs_no_candidate(self, tmp_path, capsys):
        # A run with no candidate and a run with one share the six summary lines.
        kb = _seed_kb(tmp_path / "a")
        r0 = oa_import([_oa("W_SOLO", "10.1/solo", title="Unique title here")],
                       target=kb, imported_at=T)
        cli._openalex_finish(r0, kb, dry_run=False, porcelain=True, warning="")
        base = {ln.split("\t")[0]: ln for ln in capsys.readouterr().out.splitlines()}
        _kb, report = self._report(tmp_path / "b")
        cli._openalex_finish(report, _kb, dry_run=False, porcelain=True, warning="")
        withc = {ln.split("\t")[0]: ln for ln in capsys.readouterr().out.splitlines()}
        for tok in ("imported", "skipped", "merged", "errors", "dry_run"):
            assert base[tok] == withc[tok]

    def test_human_note_goes_to_stderr(self, tmp_path, capsys):
        kb, report = self._report(tmp_path)
        cli._openalex_finish(report, kb, dry_run=False, porcelain=False, warning="")
        captured = capsys.readouterr()
        assert "resembles an existing source" in captured.err
        assert "merge-candidates/candidates.json" in captured.err
        # The machine token never pollutes the human stdout.
        assert "candidate\t" not in captured.out

    def test_arxiv_porcelain_also_surfaces(self, tmp_path, capsys):
        kb = _seed_kb(tmp_path)
        oa_import([_oa("W_AAAI", AAAI_DOI, arxiv_id=None)], target=kb, imported_at=T)
        report = arxiv_import([_arxiv("2509.00891")], target=kb, imported_at=T)
        cli._arxiv_finish(report, kb, dry_run=False, porcelain=True, warnings=[])
        out = capsys.readouterr().out
        # arXiv's porcelain key is the versioned id.
        assert any(ln.startswith("candidate\t2509.00891v1\t") for ln in out.splitlines())
        assert "candidates\t1" in out.splitlines()


def _seeded_kb(tmp_path):
    """A KB holding the AAAI record of the spike's real false-merge pair."""
    (tmp_path / "sources").mkdir(exist_ok=True)
    oa_import([_oa("W_AAAI", "10.1609/aaai.v40i46.41305")], target=tmp_path,
                 imported_at="2026-07-09T00:00:00Z")
    return tmp_path


def _import_the_near_duplicate(kb):
    """The medRxiv record: same title, same first author, adjacent year, other DOI."""
    return oa_import([_oa("W_MEDRXIV", "10.1101/2025.09.02.25334973")], target=kb,
                        imported_at="2026-07-09T00:00:00Z")


class TestADisabledCheckIsNeverSilent:
    """A corrupt candidate ledger must not crash the batch, and it must not silently
    turn the fallback off. The paper the user asked for should arrive (P1); what it
    may not do is arrive with a duplicate check the operator believes ran."""

    def _corrupt(self, kb, text="{ corrupt"):
        path = candidates_path(kb)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_import_still_succeeds(self, tmp_path):
        kb = _seeded_kb(tmp_path)
        self._corrupt(kb)
        report = _import_the_near_duplicate(kb)
        assert report.imported == 1
        assert report.errors == 0

    def test_the_disabled_check_is_reported(self, tmp_path):
        kb = _seeded_kb(tmp_path)
        self._corrupt(kb)
        report = _import_the_near_duplicate(kb)
        assert report.candidate_ledger_error
        assert "not valid JSON" in report.candidate_ledger_error

    def test_no_candidate_is_surfaced_from_an_unknown_state(self, tmp_path):
        # A pair a human already rejected must not be re-proposed, and a corrupt
        # ledger cannot tell us whether they did.
        kb = _seeded_kb(tmp_path)
        self._corrupt(kb)
        report = _import_the_near_duplicate(kb)
        assert report.candidates == []

    def test_the_corrupt_ledger_is_left_exactly_as_found(self, tmp_path):
        kb = _seeded_kb(tmp_path)
        path = self._corrupt(kb)
        _import_the_near_duplicate(kb)
        assert path.read_text(encoding="utf-8") == "{ corrupt"

    def test_a_healthy_ledger_reports_no_error(self, tmp_path):
        kb = _seeded_kb(tmp_path)
        report = _import_the_near_duplicate(kb)
        assert report.candidate_ledger_error is None
        assert len(report.candidates) == 1
