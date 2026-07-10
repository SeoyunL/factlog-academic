# SPDX-License-Identifier: Apache-2.0
"""The ledger's signal fields have a value space, enforced at the read boundary (#109).

`read_provenance` type-checked `schema_version`, `type`, `id` and `imported_at`, and let
everything in `fields` through. So a ledger could hold `"is_retracted": "true"` — a string,
which is not `True` — and the value became **invisible in one direction only**:

* upstream retracted -> `newly_retracted` fires anyway, so the retraction stayed loud;
* upstream *un*-retracted -> `un_retracted = (not current) and recorded` is `False`, so
  nothing surfaced and `openalex-acknowledge-retraction` exited 0 with "nothing to
  acknowledge" while the string stayed in the ledger forever.

That is a source silently disappearing (P4), and a swallowed signal must fail loud. Proven
here: the reader refuses the value (it is never coerced — `"true"` does not become `True`),
the writer refuses to create what the reader would reject, and every command that reads a
ledger lands in a corrupt-ledger path that already existed. The two `*-acknowledge-*`
commands, which must *write*, refuse for **zero** API requests and no prompt; `-refresh` /
`-check-versions`, which only report, surface the bad ledger as a loud per-file error and
never use the value that would not parse. arXiv's `withdrawn_by` is the same shape: any
value but `author` / `admin` is not "some withdrawal was recorded".
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from factlog import cli
from factlog.integrations.arxiv.client import BatchResult
from factlog.integrations.arxiv.id_normalizer import ArxivId
from factlog.integrations.arxiv.work_parser import (
    WITHDRAWN_BY_ADMIN,
    WITHDRAWN_BY_AUTHOR,
    ParsedArxivWork,
)
from factlog.integrations.common.provenance import (
    Provenance,
    ProvenanceError,
    SourceRecord,
    read_provenance,
    write_provenance,
)

IMPORTED_AT = "2026-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _write_raw(path, record_fields, *, type_="openalex", id_="W1"):
    """Write a sidecar by hand — `write_provenance` refuses the very values under test."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"type": type_, "id": id_, "imported_at": IMPORTED_AT, **record_fields}
    path.write_text(
        json.dumps({"schema_version": 1, "records": [record]}, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def run(argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


# --------------------------------------------------------------------------- #
# 1. the read boundary: is_retracted
# --------------------------------------------------------------------------- #
class TestIsRetractedValueSpace:
    @pytest.mark.parametrize("bad", ["true", "false", "", 1, 0, 1.0, [], {}])
    def test_a_non_bool_is_refused_at_read(self, tmp_path, bad):
        # `isinstance(True, int)` is True; the mirror trap is admitting `1`/`0` as booleans.
        path = _write_raw(tmp_path / "source-provenance" / "a.json", {"is_retracted": bad})
        with pytest.raises(ProvenanceError):
            read_provenance(path)

    def test_the_error_names_the_file_the_field_and_the_value(self, tmp_path):
        path = _write_raw(tmp_path / "source-provenance" / "a.json", {"is_retracted": "true"})
        with pytest.raises(ProvenanceError) as exc:
            read_provenance(path)
        message = str(exc.value)
        assert "a.json" in message
        assert "is_retracted" in message
        assert "'true'" in message

    def test_the_value_is_never_coerced(self, tmp_path):
        # "true" must not become True. A ledger records what a source said; inventing a
        # value for a corrupt one is the write this project forbids.
        path = _write_raw(tmp_path / "source-provenance" / "a.json", {"is_retracted": "true"})
        with pytest.raises(ProvenanceError):
            read_provenance(path)
        assert json.loads(path.read_text())["records"][0]["is_retracted"] == "true"

    @pytest.mark.parametrize("good", [True, False])
    def test_a_real_bool_still_passes(self, tmp_path, good):
        path = _write_raw(tmp_path / "source-provenance" / "a.json", {"is_retracted": good})
        assert read_provenance(path).records[0].fields["is_retracted"] is good

    def test_absent_and_null_pass(self, tmp_path):
        # Absence *is* the value (not retracted), and `to_dict` drops a None on write.
        assert read_provenance(_write_raw(tmp_path / "s-p" / "a.json", {})).records
        path = _write_raw(tmp_path / "s-p" / "b.json", {"is_retracted": None})
        assert read_provenance(path).records[0].fields["is_retracted"] is None

    def test_the_field_is_owned_by_openalex_not_by_every_record_type(self, tmp_path):
        # `is_retracted` is OpenAlex's word. An arXiv record carrying the name is not
        # judged by OpenAlex's rule — the table is keyed by (record type, field).
        path = _write_raw(
            tmp_path / "s-p" / "a.json", {"is_retracted": "true"}, type_="arxiv", id_="1706.03762"
        )
        assert read_provenance(path).records[0].fields["is_retracted"] == "true"


# --------------------------------------------------------------------------- #
# 2. the read boundary: withdrawn_by
# --------------------------------------------------------------------------- #
class TestWithdrawnByValueSpace:
    @pytest.mark.parametrize("bad", ["maintainer", "AUTHOR", "", True, 1, ["author"]])
    def test_an_unknown_agent_is_refused_at_read(self, tmp_path, bad):
        path = _write_raw(
            tmp_path / "s-p" / "a.json", {"withdrawn_by": bad}, type_="arxiv", id_="1706.03762"
        )
        with pytest.raises(ProvenanceError):
            read_provenance(path)

    def test_the_error_names_the_file_the_field_and_the_value(self, tmp_path):
        path = _write_raw(
            tmp_path / "s-p" / "a.json",
            {"withdrawn_by": "maintainer"},
            type_="arxiv",
            id_="1706.03762",
        )
        with pytest.raises(ProvenanceError) as exc:
            read_provenance(path)
        message = str(exc.value)
        assert "a.json" in message
        assert "withdrawn_by" in message
        assert "'maintainer'" in message

    @pytest.mark.parametrize("good", [WITHDRAWN_BY_AUTHOR, WITHDRAWN_BY_ADMIN, None])
    def test_a_known_agent_and_null_pass(self, tmp_path, good):
        path = _write_raw(
            tmp_path / "s-p" / "a.json", {"withdrawn_by": good}, type_="arxiv", id_="1706.03762"
        )
        assert read_provenance(path).records[0].fields["withdrawn_by"] == good

    def test_an_absent_key_passes(self, tmp_path):
        path = _write_raw(tmp_path / "s-p" / "a.json", {}, type_="arxiv", id_="1706.03762")
        assert "withdrawn_by" not in read_provenance(path).records[0].fields


# --------------------------------------------------------------------------- #
# 3. the write boundary is the symmetric half
# --------------------------------------------------------------------------- #
class TestWriteRefusesWhatReadWouldReject:
    def test_write_provenance_refuses_a_bad_value_and_creates_nothing(self, tmp_path):
        sidecar = tmp_path / "source-provenance" / "a.json"
        record = SourceRecord("openalex", "W1", IMPORTED_AT, {"is_retracted": "true"})
        with pytest.raises(ProvenanceError):
            write_provenance(sidecar, Provenance(records=[record]))
        assert not sidecar.exists()
        assert not sidecar.parent.exists()  # not even the directory

    def test_a_good_record_still_round_trips(self, tmp_path):
        sidecar = tmp_path / "source-provenance" / "a.json"
        record = SourceRecord("openalex", "W1", IMPORTED_AT, {"is_retracted": True})
        write_provenance(sidecar, Provenance(records=[record]))
        assert read_provenance(sidecar).records[0].fields["is_retracted"] is True


# --------------------------------------------------------------------------- #
# 4. OpenAlex: the un-retraction direction surfaces instead of exiting 0
# --------------------------------------------------------------------------- #
class FakeOpenAlexClient:
    def __init__(self, works):
        self._works = dict(works)
        self.calls: list[str] = []

    def get_work(self, work_id):
        self.calls.append(work_id)
        return self._works[work_id]

    @property
    def call_count(self):
        return len(self.calls)


def _openalex_kb(tmp_path, is_retracted):
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "W1.md").write_text(
        "---\nopenalex_id: W1\ntype: article\n---\n# body\n", encoding="utf-8"
    )
    _write_raw(tmp_path / "source-provenance" / "W1.json", {"is_retracted": is_retracted})
    return tmp_path


def _raw_work(oid="W1", is_retracted=False):
    return {"id": f"https://openalex.org/{oid}", "type": "article", "is_retracted": is_retracted}


class TestOpenAlexUnRetractionNoLongerSilent:
    def test_refresh_reports_the_corrupt_ledger_rather_than_reading_it_as_not_retracted(
        self, tmp_path, monkeypatch, capsys
    ):
        # OpenAlex has reversed the retraction the ledger recorded as the string "true".
        # Before #109 this exited 0 with `Retracted (reversed): 0` — the signal vanished.
        _openalex_kb(tmp_path, "true")
        client = FakeOpenAlexClient({"W1": _raw_work(is_retracted=False)})
        monkeypatch.setattr(cli, "_make_openalex_client", lambda config: client)

        code = run(["openalex-refresh", "--target", str(tmp_path), "--older-than", "0"])
        out = capsys.readouterr().out
        assert code != 0
        assert "corrupt provenance ledger" in out
        assert "is_retracted" in out
        # The ledger's value is never used: the file did not parse, so the work falls out of
        # the ledger scan and is checked from front matter (which records no retraction).
        # The refusal is loud and per-file, not a batch crash.
        assert "Retracted (reversed): 0" in out

    def test_acknowledge_refuses_before_the_query_instead_of_nothing_to_acknowledge(
        self, tmp_path, monkeypatch, capsys
    ):
        _openalex_kb(tmp_path, "true")
        client = FakeOpenAlexClient({"W1": _raw_work(is_retracted=False)})
        monkeypatch.setattr(cli, "_make_openalex_client", lambda config: client)
        before = (tmp_path / "source-provenance" / "W1.json").read_bytes()

        code = run(
            ["openalex-acknowledge-retraction", "--id", "W1", "--target", str(tmp_path), "--yes"]
        )
        err = capsys.readouterr().err
        assert code == 1
        assert "cannot read every provenance ledger" in err
        assert "nothing to acknowledge" not in capsys.readouterr().out
        assert client.call_count == 0  # ZERO API requests, no prompt
        assert (tmp_path / "source-provenance" / "W1.json").read_bytes() == before

    def test_a_real_bool_still_surfaces_the_un_retraction(self, tmp_path, monkeypatch, capsys):
        # The control: the same KB with a proper `true` still reports the reversal.
        _openalex_kb(tmp_path, True)
        client = FakeOpenAlexClient({"W1": _raw_work(is_retracted=False)})
        monkeypatch.setattr(cli, "_make_openalex_client", lambda config: client)

        assert run(["openalex-refresh", "--target", str(tmp_path), "--older-than", "0"]) == 0
        out = capsys.readouterr().out
        assert "Retracted (reversed): 1" in out


# --------------------------------------------------------------------------- #
# 5. arXiv: the same gate, in arXiv's vocabulary
# --------------------------------------------------------------------------- #
class FakeArxivClient:
    def __init__(self, works):
        self._works = {w.arxiv_id: w for w in works}
        self.calls: list[list[str]] = []

    def fetch_works(self, ids):
        self.calls.append([str(i) for i in ids])
        found, missing = [], []
        for value in ids:
            work = self._works.get(str(value))
            (found if work else missing).append(work or ArxivId(str(value)))
        return BatchResult(found, missing)

    @property
    def call_count(self):
        return len(self.calls)


def _arxiv_work(arxiv_id="1706.03762", withdrawn_by=None):
    return ParsedArxivWork(
        arxiv_id=arxiv_id,
        version=7,
        title="A paper",
        authors=("Ann Author",),
        abstract="An abstract.",
        primary_category="cs.CL",
        categories=("cs.CL",),
        submitted=date(2017, 6, 12),
        last_updated=date(2020, 1, 1),
        withdrawn_by=withdrawn_by,
        abs_url=f"https://arxiv.org/abs/{arxiv_id}v7",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v7",
    )


def _arxiv_kb(tmp_path, withdrawn_by):
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "p.md").write_text(
        "---\narxiv_id: 1706.03762\narxiv_version: 7\n---\n# body\n", encoding="utf-8"
    )
    _write_raw(
        tmp_path / "source-provenance" / "p.json",
        {"version": 7, "withdrawn_by": withdrawn_by},
        type_="arxiv",
        id_="1706.03762",
    )
    return tmp_path


class TestArxivUnknownAgentIsRefused:
    def test_check_versions_reports_the_corrupt_ledger(self, tmp_path, monkeypatch, capsys):
        _arxiv_kb(tmp_path, "maintainer")
        client = FakeArxivClient([_arxiv_work(withdrawn_by=None)])
        monkeypatch.setattr(cli, "_make_arxiv_client", lambda config: client)

        code = run(["arxiv-check-versions", "--target", str(tmp_path), "--older-than", "0"])
        out = capsys.readouterr().out
        assert code != 0
        assert "corrupt provenance ledger" in out
        assert "withdrawn_by" in out
        # As for openalex-refresh: the unreadable ledger is a loud per-file error and the
        # paper is checked from front matter instead, never from the value that would not
        # parse. Only the acknowledge commands, which must *write*, gate at zero requests.
        assert "Newly withdrawn:     0" in out

    def test_acknowledge_withdrawal_refuses_before_the_query(self, tmp_path, monkeypatch, capsys):
        _arxiv_kb(tmp_path, "maintainer")
        client = FakeArxivClient([_arxiv_work(withdrawn_by=None)])
        monkeypatch.setattr(cli, "_make_arxiv_client", lambda config: client)
        before = (tmp_path / "source-provenance" / "p.json").read_bytes()

        code = run(
            [
                "arxiv-acknowledge-withdrawal",
                "--id",
                "1706.03762",
                "--target",
                str(tmp_path),
                "--yes",
            ]
        )
        assert code == 1
        assert "cannot read every provenance ledger" in capsys.readouterr().err
        assert client.call_count == 0  # ZERO API requests, no prompt
        assert (tmp_path / "source-provenance" / "p.json").read_bytes() == before

    def test_a_known_agent_still_passes_through_to_the_check(self, tmp_path, monkeypatch, capsys):
        _arxiv_kb(tmp_path, WITHDRAWN_BY_AUTHOR)
        client = FakeArxivClient([_arxiv_work(withdrawn_by=WITHDRAWN_BY_AUTHOR)])
        monkeypatch.setattr(cli, "_make_arxiv_client", lambda config: client)

        assert run(["arxiv-check-versions", "--target", str(tmp_path), "--older-than", "0"]) == 0
        out = capsys.readouterr().out
        assert "corrupt provenance ledger" not in out
        assert client.call_count == 1
