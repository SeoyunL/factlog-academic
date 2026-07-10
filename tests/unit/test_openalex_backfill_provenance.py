# SPDX-License-Identifier: Apache-2.0
"""`factlog openalex-backfill-provenance` — the ledger a pre-#84 paper never got (#115, #105).

A paper imported before #84 has front matter and no provenance sidecar, so a re-import
short-circuits on the front-matter identity match before the sidecar writer and its ledger
is never created; `openalex-acknowledge-retraction` then refuses it and points here. This
command materializes that ledger from what the ``.md`` already asserts — ``add_source``
into a fresh sidecar, no network, no new claim — so a retraction OpenAlex flags can finally
be acknowledged and the repeat stops.

The real OpenAlex client is patched via ``_make_openalex_client`` so nothing touches the
network, and the tests prove the command itself never even *constructs* one. What is
verified here, with the real CLI:

* the graduation — a front-matter-only retracted paper is un-acknowledgeable before backfill
  (``no provenance ledger``, **0** API requests) and acknowledgeable after, and the repeat
  stops on the next refresh;
* full recoverability — the backfilled record is field-for-field what the OpenAlex importer
  writes for the same work, ``imported_at`` included. Unlike arXiv's ``submitted``, nothing
  is lost;
* ``--auto-update`` still never writes ``is_retracted`` (PR #97's guarantee) after a backfill;
* ``--dry-run`` writes nothing (no sidecar dir, ``.md`` ``mtime_ns`` unchanged) and names
  both eligible and refused ids;
* a re-run is a byte- and ``mtime_ns``-identical no-op;
* every ``.md`` is byte- and ``mtime_ns``-identical throughout (P4);
* a ``openalex_is_retracted`` outside the ledger's value space is refused *per id* — the
  value is promoted verbatim and the shared writer's ``signal_field_error`` guard (#109)
  refuses it, never coerced and never dropped — while its neighbours are backfilled;
* the porcelain contract has the same shape as ``arxiv-backfill-provenance``'s;
* "withdrawn" never appears — ``is_retracted`` is OpenAlex's word, and its own.
"""
from __future__ import annotations

import builtins
import json

import pytest

from factlog import cli
from factlog.integrations.common.provenance import read_provenance, sidecar_path
from factlog.integrations.openalex.importer import import_works
from factlog.integrations.openalex.work_parser import parse_work

IMPORTED_AT = "2026-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
def _raw_work(oid="W123", *, is_retracted=False, work_type="article", doi=None, journal=None):
    raw: dict = {
        "id": f"https://openalex.org/{oid}",
        "type": work_type,
        "is_retracted": is_retracted,
        "title": "A paper",
    }
    if doi is not None:
        raw["doi"] = f"https://doi.org/{doi}"
    if journal is not None:
        raw["primary_location"] = {"source": {"display_name": journal}}
    return raw


class FakeClient:
    """Maps requested id -> raw work dict, recording every id it was asked for."""

    def __init__(self, works=None):
        self._works = dict(works or {})
        self.calls: list[str] = []

    def get_work(self, work_id):
        self.calls.append(work_id)
        return self._works[work_id]

    @property
    def call_count(self):
        return len(self.calls)


@pytest.fixture
def fake(monkeypatch):
    def install(client):
        monkeypatch.setattr(cli, "_make_openalex_client", lambda config: client)
        return client
    return install


@pytest.fixture
def kb(tmp_path):
    (tmp_path / "sources").mkdir()
    return tmp_path


def run(argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


def _front_matter_only(kb, oid="W123", *, is_retracted=False, imported_at=IMPORTED_AT,
                       name=None, work_type="article", journal=None, doi=None,
                       retraction_literal=None):
    """A work imported before #84: front matter, no sidecar."""
    md = kb / "sources" / f"{name or oid}.md"
    fm = [f"openalex_id: {oid}", f"type: {work_type}", "imported_from: openalex"]
    if journal:
        fm.append(f"journal: {journal}")
    if doi:
        fm.append(f"doi: {doi}")
    if imported_at:
        fm.append(f'imported_at: "{imported_at}"')
    if retraction_literal is not None:
        fm.append(f"openalex_is_retracted: {retraction_literal}")
    elif is_retracted:
        fm.append("openalex_is_retracted: true")
    md.write_text("---\n" + "\n".join(fm) + "\n---\n# body\n", encoding="utf-8")
    return md


def _md_snapshot(kb):
    """Bytes and mtime_ns for every source .md — what P4 must hold identical."""
    return {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (kb / "sources").rglob("*.md")
    }


def _records(md):
    return {r.type: r.to_dict() for r in read_provenance(sidecar_path(md)).records}


def _backfill(kb, *extra):
    return run(["openalex-backfill-provenance", "--target", str(kb), *extra])


# --------------------------------------------------------------------------- #
# (1) the graduation: un-acknowledgeable -> acknowledgeable, and the repeat stops
# --------------------------------------------------------------------------- #
class TestTheGraduation:
    def test_acknowledge_refuses_before_backfill_for_zero_requests(self, kb, fake, capsys):
        _front_matter_only(kb, is_retracted=True)
        client = fake(FakeClient({"W123": _raw_work(is_retracted=True)}))

        code = run(["openalex-acknowledge-retraction", "--target", str(kb),
                    "--id", "W123", "--yes"])

        assert code == 1
        assert "front matter" in capsys.readouterr().err
        assert client.call_count == 0

    def test_after_backfill_it_is_acknowledgeable_and_the_repeat_stops(self, kb, fake, capsys):
        md = _front_matter_only(kb, is_retracted=True)
        client = fake(FakeClient({"W123": _raw_work(is_retracted=True)}))

        assert _backfill(kb) == 0
        capsys.readouterr()

        # The ledger now exists and carries OpenAlex's flag, read from front matter.
        assert _records(md)["openalex"]["is_retracted"] is True

        # openalex-refresh had nothing to acknowledge before (no ledger); now the flag is
        # recorded, so the "newly retracted" signal does not fire at all.
        assert run(["openalex-refresh", "--target", str(kb), "--older-than", "0"]) == 0
        assert "Retracted (new):      0" in capsys.readouterr().out

        # And an un-retraction is now acknowledgeable: OpenAlex reverses its flag, the
        # human clears it, and the signal stops repeating.
        client._works["W123"] = _raw_work(is_retracted=False)
        assert run(["openalex-acknowledge-retraction", "--target", str(kb),
                    "--id", "W123", "--yes"]) == 0
        capsys.readouterr()
        assert "is_retracted" not in _records(md)["openalex"]

        assert run(["openalex-refresh", "--target", str(kb), "--older-than", "0"]) == 0
        out = capsys.readouterr().out
        assert "Retracted (reversed): 0" in out


# --------------------------------------------------------------------------- #
# (2) full recoverability — unlike arXiv, nothing is lost
# --------------------------------------------------------------------------- #
class TestTheBackfilledRecordIsTheImportsRecord:
    def test_it_is_field_for_field_what_an_import_writes(self, kb, tmp_path):
        """Import the same work into a *second* KB and compare the two ledgers. The only
        difference an import could introduce is ``imported_at``, and front matter carries
        that too — so the records are identical.

        The DOI must be a real one. ``parse_work`` returns ``doi=None`` for a registrant with
        fewer than four digits, so a fixture like ``10.1/x`` makes both sidecars omit ``doi``
        and the equality holds while proving nothing about the ``doi`` <- ``doi`` recovery
        this issue singles out. Hence the explicit ``doi`` assertions below."""
        doi = "10.1016/S0140-6736(20)30367-6"
        raw = _raw_work("W99", is_retracted=True, work_type="article",
                        doi=doi, journal="The Lancet")
        assert parse_work(raw).doi == doi.lower()  # the fixture reaches the writer

        imported_kb = tmp_path / "imported"
        (imported_kb / "sources").mkdir(parents=True)
        report = import_works([parse_work(raw)], target=imported_kb, imported_at=IMPORTED_AT)
        assert report.imported == 1
        imported_md = next((imported_kb / "sources").glob("*.md"))

        # The same paper as a pre-#84 deposit: its own front matter, no sidecar.
        backfilled_md = kb / "sources" / imported_md.name
        text = imported_md.read_text(encoding="utf-8")
        backfilled_md.write_text(text, encoding="utf-8")
        assert _backfill(kb) == 0

        backfilled = _records(backfilled_md)
        assert backfilled == _records(imported_md)
        assert backfilled["openalex"]["imported_at"] == IMPORTED_AT
        # Every recoverable field, named — an omission on both sides would pass the equality.
        assert backfilled["openalex"]["doi"] == doi.lower()
        assert backfilled["openalex"]["work_type"] == "article"
        assert backfilled["openalex"]["journal"] == "The Lancet"
        assert backfilled["openalex"]["is_retracted"] is True
        # ...and the bytes, not just the parse.
        assert sidecar_path(backfilled_md).read_bytes() == sidecar_path(imported_md).read_bytes()

    def test_auto_update_still_never_writes_is_retracted_after_a_backfill(self, kb, fake, capsys):
        """PR #97's guarantee. A backfill does not weaken it: --auto-update learns the new
        journal and leaves OpenAlex's retraction flag for a human."""
        md = _front_matter_only(kb, is_retracted=True, journal="Old Venue")
        fake(FakeClient({"W123": _raw_work(is_retracted=False, journal="New Venue")}))

        assert _backfill(kb) == 0
        assert _records(md)["openalex"]["is_retracted"] is True

        run(["openalex-refresh", "--target", str(kb), "--older-than", "0", "--auto-update"])
        capsys.readouterr()

        record = _records(md)["openalex"]
        assert record["journal"] == "New Venue"          # the refresh learned this
        assert record["is_retracted"] is True            # never rewritten, even to clear it


# --------------------------------------------------------------------------- #
# (3) refusals, dry-run, idempotence, P4
# --------------------------------------------------------------------------- #
class TestWhatItRefusesAndWhatItLeavesAlone:
    def test_a_paper_without_imported_at_is_reported_and_skipped(self, kb, capsys):
        _front_matter_only(kb, "W1", imported_at=None)
        _front_matter_only(kb, "W2")

        assert _backfill(kb) == 0
        out = capsys.readouterr().out

        assert "✗ W1" in out and "imported_at" in out
        assert "✎ W2" in out
        assert not sidecar_path(kb / "sources" / "W1.md").exists()
        assert sidecar_path(kb / "sources" / "W2.md").exists()

    @pytest.mark.parametrize("literal", ["1", "yes", "on"])
    def test_an_uninterpretable_retraction_flag_is_refused_per_id(self, kb, capsys, literal):
        _front_matter_only(kb, "W1")
        _front_matter_only(kb, "W2", retraction_literal=literal)

        assert _backfill(kb) == 0
        out = capsys.readouterr().out

        assert "✎ W1" in out                       # the neighbour is unaffected
        assert "✗ W2" in out and "is_retracted" in out
        assert f"{literal!r}" in out               # the offending value, named verbatim
        # Neither dropped nor coerced: W2 gets no ledger at all.
        assert sidecar_path(kb / "sources" / "W1.md").exists()
        assert not sidecar_path(kb / "sources" / "W2.md").exists()

    def test_the_refusal_agrees_between_preview_and_run(self, kb, capsys):
        _front_matter_only(kb, "W1")
        _front_matter_only(kb, "W2", retraction_literal="yes")

        assert _backfill(kb, "--dry-run", "--porcelain") == 0
        preview = _porcelain(capsys.readouterr().out)
        assert not (kb / "source-provenance").exists()

        assert _backfill(kb, "--porcelain") == 0
        assert _porcelain(capsys.readouterr().out) == preview == {
            "W1": "backfilled", "W2": "refused",
        }

    def test_the_dry_run_agrees_with_the_real_run_paper_for_paper(self, kb, capsys):
        _front_matter_only(kb, "W1")
        _front_matter_only(kb, "W2", imported_at=None)
        _front_matter_only(kb, "W3", is_retracted=True)

        assert _backfill(kb, "--dry-run", "--porcelain") == 0
        preview = _porcelain(capsys.readouterr().out)
        assert not (kb / "source-provenance").exists()

        assert _backfill(kb, "--porcelain") == 0
        real = _porcelain(capsys.readouterr().out)

        assert preview == real == {
            "W1": "backfilled", "W2": "refused", "W3": "backfilled",
        }

    def test_a_dry_run_writes_nothing(self, kb):
        _front_matter_only(kb, "W1", is_retracted=True)
        before = _md_snapshot(kb)

        assert _backfill(kb, "--dry-run") == 0

        assert not (kb / "source-provenance").exists()
        assert _md_snapshot(kb) == before

    def test_a_re_run_is_a_byte_and_mtime_identical_no_op(self, kb, capsys):
        md = _front_matter_only(kb, "W1", is_retracted=True, journal="The Lancet")
        assert _backfill(kb) == 0
        sidecar = sidecar_path(md)
        before = (sidecar.read_bytes(), sidecar.stat().st_mtime_ns)
        md_before = _md_snapshot(kb)
        capsys.readouterr()

        assert _backfill(kb) == 0
        out = capsys.readouterr().out

        # Now ledger-backed, so it is not even a candidate: nothing is opened for write.
        assert "No front-matter-only OpenAlex papers found" in out
        assert (sidecar.read_bytes(), sidecar.stat().st_mtime_ns) == before
        assert _md_snapshot(kb) == md_before

    def test_an_empty_kb_says_so_and_exits_zero(self, kb, capsys):
        assert _backfill(kb) == 0
        assert "No front-matter-only OpenAlex papers found" in capsys.readouterr().out


class TestItNeverOpensAnMdForWriteAndNeverBuildsAClient:
    def test_no_md_is_opened_for_write(self, kb, monkeypatch):
        _front_matter_only(kb, "W1", is_retracted=True)
        _front_matter_only(kb, "W2", imported_at=None)
        real_open = builtins.open
        opened: list[tuple[str, str]] = []

        def spy(file, mode="r", *args, **kwargs):
            opened.append((str(file), mode))
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", spy)
        before = _md_snapshot(kb)
        assert _backfill(kb) == 0
        monkeypatch.undo()

        writes = [f for f, mode in opened if "sources/" in f and set(mode) & set("wax+")]
        assert writes == []
        assert _md_snapshot(kb) == before

    def test_the_command_constructs_no_api_client(self, kb, monkeypatch):
        _front_matter_only(kb, "W1", is_retracted=True)

        def explode(config):
            raise AssertionError("openalex-backfill-provenance must never go to the network")

        monkeypatch.setattr(cli, "_make_openalex_client", explode)
        assert _backfill(kb) == 0


# --------------------------------------------------------------------------- #
# (4) the porcelain contract, and the vocabulary
# --------------------------------------------------------------------------- #
def _porcelain(out: str) -> dict[str, str]:
    return {
        row.split("\t")[2]: row.split("\t")[1]
        for row in out.splitlines() if row.startswith("result\t")
    }


class TestThePorcelainContract:
    def test_it_has_the_same_shape_as_arxivs(self, kb, capsys):
        _front_matter_only(kb, "W1", is_retracted=True)
        _front_matter_only(kb, "W2", imported_at=None)

        assert _backfill(kb, "--porcelain") == 0
        lines = capsys.readouterr().out.splitlines()

        results = [line for line in lines if line.startswith("result\t")]
        assert len(results) == 2
        assert all(len(line.split("\t")) == 5 for line in results)
        summary = dict(line.split("\t", 1) for line in lines if not line.startswith("result\t"))
        assert summary["backfilled"] == "1"
        assert summary["refused"] == "1"
        assert summary["errors"] == "0"
        assert summary["dry_run"] == "0"
        assert summary["target"].endswith("sources")

    def test_a_tab_in_an_id_cannot_shift_a_column(self, kb, capsys):
        md = kb / "sources" / "odd.md"
        md.write_text(
            '---\nopenalex_id: "W1\tW2"\ntype: article\n'
            f'imported_at: "{IMPORTED_AT}"\n---\n# body\n', encoding="utf-8"
        )

        assert _backfill(kb, "--porcelain") == 0
        results = [r for r in capsys.readouterr().out.splitlines() if r.startswith("result\t")]

        assert len(results) == 1
        assert len(results[0].split("\t")) == 5
        assert results[0].split("\t")[2] == "W1 W2"

    def test_dry_run_is_flagged_in_the_porcelain(self, kb, capsys):
        _front_matter_only(kb, "W1")
        assert _backfill(kb, "--dry-run", "--porcelain") == 0
        assert "dry_run\t1" in capsys.readouterr().out


class TestVocabulary:
    def test_withdrawn_never_appears_in_any_output(self, kb, capsys):
        _front_matter_only(kb, "W1", is_retracted=True)
        _front_matter_only(kb, "W2", imported_at=None)
        _backfill(kb)
        captured = capsys.readouterr()
        # The KB path is pytest's, and carries this test's own name — judge only our text.
        text = (captured.out + captured.err).replace(str(kb), "<kb>").lower()
        assert "withdrawn" not in text

    def test_the_help_names_the_source_scoped_key_not_a_bare_retracted(self, capsys):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["openalex-backfill-provenance", "--help"])
        help_text = capsys.readouterr().out.lower()
        assert "withdrawn" not in help_text

    def test_the_ledger_records_a_real_bool_that_read_provenance_accepts(self, kb):
        md = _front_matter_only(kb, "W1", retraction_literal='"true"')
        assert _backfill(kb) == 0
        raw = json.loads(sidecar_path(md).read_text(encoding="utf-8"))
        # Never the string "true": a reader that validates the type must accept it.
        assert raw["records"][0]["is_retracted"] is True
