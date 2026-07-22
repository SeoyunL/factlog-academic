# SPDX-License-Identifier: Apache-2.0
"""`factlog openalex-acknowledge-retraction` — the human gate that stops the repeat (#101).

The mirror of `arxiv-acknowledge-withdrawal` on the shared acknowledge primitive, in
OpenAlex's vocabulary only. The real OpenAlex client is replaced via
`_make_openalex_client` so the command runs without the network; a temp KB carries source
`.md` originals and their provenance ledgers.

Proven here, with the real CLI (never a hand-rolled assertion of the comparison), by
patching only `OpenAlexClient.get_work`:

1. The **presence -> value** comparison in `refresh._diff` now surfaces an *un-retraction*
   (recorded True, OpenAlex now False), which the old `current and not recorded` form
   silently lost — with its own signal, note, summary line and `--porcelain` column.
2. The command live-queries OpenAlex (0 credits), refuses without a terminal/`--yes`,
   refuses BEFORE the query (zero API requests) when there is no ledger / an unreadable
   ledger / a front-matter-only work, writes nothing on a failed query or a merged-away
   id, refuses to acknowledge under an old key when OpenAlex answers a merged id, silences
   the repeat once run, refuses to CLEAR under `--yes` (#106/#414 — a clear needs a human
   at the prompt), clears the flag by REMOVING the key on an un-retraction, keeps
   re-import `errors == 0` in either direction (is_retracted is not identifying), and never
   opens the `.md` (byte- and `mtime_ns`-identical). "Withdrawn" never appears.
"""
from __future__ import annotations

import pytest

from factlog import cli
from factlog.integrations.common.provenance import (
    Provenance,
    SourceRecord,
    read_provenance,
    sidecar_path,
    write_provenance,
)
from factlog.integrations.openalex.api_client import (
    OpenAlexConnectionError,
    OpenAlexNotFoundError,
    OpenAlexRateLimitError,
)
from factlog.integrations.openalex.importer import import_works
from factlog.integrations.openalex.work_parser import parse_work

IMPORTED_AT = "2026-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
def _raw_work(oid="W123", *, is_retracted=False, work_type="article",
              doi=None, journal=None, returned_id=None):
    """A raw /works payload. `returned_id` models a merged work answering under a
    different id (get_work follows redirects); it defaults to `oid`."""
    raw: dict = {
        "id": f"https://openalex.org/{returned_id or oid}",
        "type": work_type,
        "is_retracted": is_retracted,
    }
    if doi is not None:
        raw["doi"] = f"https://doi.org/{doi}"
    if journal is not None:
        raw["primary_location"] = {"source": {"display_name": journal}}
    return raw


def _seed(kb, oid="W123", *, is_retracted=False, work_type="article", doi=None,
          journal=None, name=None, extra_records=()):
    """Write a source .md and its OpenAlex provenance ledger. Returns the .md path."""
    (kb / "sources").mkdir(exist_ok=True)
    name = name or oid
    md = kb / "sources" / f"{name}.md"
    fm = [f"openalex_id: {oid}", f"type: {work_type}"]
    if journal:
        fm.append(f"journal: {journal}")
    if doi:
        fm.append(f"doi: {doi}")
    if is_retracted:
        fm.append("openalex_is_retracted: true")
    md.write_text("---\n" + "\n".join(fm) + "\n---\n# body\n", encoding="utf-8")
    fields = {
        "doi": doi,
        "work_type": work_type,
        "journal": journal,
        "is_retracted": True if is_retracted else None,
    }
    records = [SourceRecord(type="openalex", id=oid, imported_at=IMPORTED_AT, fields=fields),
               *extra_records]
    write_provenance(sidecar_path(md, kb), Provenance(records=records))
    return md


def _seed_front_matter_only(kb, oid="W123", *, is_retracted=False, name=None):
    """A work imported before #84: front matter only, no provenance ledger/sidecar."""
    (kb / "sources").mkdir(exist_ok=True)
    name = name or oid
    md = kb / "sources" / f"{name}.md"
    fm = [f"openalex_id: {oid}", "type: article", "imported_from: openalex"]
    if is_retracted:
        fm.append("openalex_is_retracted: true")
    md.write_text("---\n" + "\n".join(fm) + "\n---\n# body\n", encoding="utf-8")
    return md


class FakeClient:
    """Maps requested id -> raw work dict. Records every id it was asked for. A listed id
    in `not_found` raises OpenAlexNotFoundError; `raise_exc` raises unconditionally."""

    def __init__(self, works=None, *, not_found=(), raise_exc=None):
        self._works = dict(works or {})
        self._not_found = set(not_found)
        self._raise = raise_exc
        self.calls: list[str] = []

    def get_work(self, work_id):
        self.calls.append(work_id)
        if self._raise is not None:
            raise self._raise
        if work_id in self._not_found:
            raise OpenAlexNotFoundError(f"no record for {work_id}")
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


def run(argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


def _md_snapshot(kb):
    """Bytes and mtime_ns for every source .md — what P4 must hold identical."""
    snap = {}
    root = kb / "sources"
    if root.is_dir():
        for path in root.rglob("*.md"):
            st = path.stat()
            snap[path] = (path.read_bytes(), st.st_mtime_ns)
    return snap


def _kb_snapshot(kb):
    """Bytes and mtime_ns for every file under sources/ and source-provenance/."""
    snap = {}
    for sub in ("sources", "source-provenance"):
        root = kb / sub
        if root.is_dir():
            for path in root.rglob("*"):
                if path.is_file():
                    st = path.stat()
                    snap[path] = (path.read_bytes(), st.st_mtime_ns)
    return snap


def _ledger_record(kb, oid):
    """The serialized OpenAlex record for `oid`, or None (the field is absent when not
    retracted — retraction absent from the JSON *means* not retracted)."""
    root = kb / "source-provenance"
    if root.is_dir():
        for path in root.rglob("*.json"):
            for record in read_provenance(path).records:
                if record.type == "openalex" and record.id == oid:
                    return record.to_dict()
    return None


def _ledger_is_retracted(kb, oid):
    """True iff the ledger currently records `oid` as retracted."""
    record = _ledger_record(kb, oid)
    return bool(record and record.get("is_retracted") is True)


# --------------------------------------------------------------------------- #
# (1) the presence -> value comparison surfaces an un-retraction (via openalex-refresh)
# --------------------------------------------------------------------------- #
class TestUnRetractionResurfaces:
    def test_un_retraction_surfaces_and_is_not_a_retraction_warning(
        self, tmp_path, fake, capsys
    ):
        # A retraction the ledger records that OpenAlex has reversed. It is news, but NOT a
        # retraction warning: it asks the human to clear the stale flag, in OpenAlex's
        # vocabulary, and prescribes the acknowledge command.
        _seed(tmp_path, "W1", is_retracted=True)
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=False)}))
        code = run(["openalex-refresh", "--target", str(tmp_path), "--older-than", "0"])
        out = capsys.readouterr().out
        assert code == 0
        assert "No longer flagged as retracted" in out
        assert "Retracted (reversed): 1" in out
        assert "openalex-acknowledge-retraction --id W1" in out
        # It must not masquerade as a fresh retraction.
        assert "RETRACTED" not in out
        assert "Retracted (new):      0" in out
        # OpenAlex vocabulary only.
        assert "withdraw" not in out.lower()

    def test_still_retracted_does_not_resurface(self, tmp_path, fake, capsys):
        # The equal case still silences: recorded True, upstream True.
        _seed(tmp_path, "W1", is_retracted=True)
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=True)}))
        run(["openalex-refresh", "--target", str(tmp_path), "--older-than", "0"])
        out = capsys.readouterr().out
        assert "Retracted (new):      0" in out
        assert "Retracted (reversed): 0" in out

    def test_fresh_retraction_still_surfaces(self, tmp_path, fake, capsys):
        # Regression guard: recorded False, upstream True still surfaces as newly retracted.
        _seed(tmp_path, "W1", is_retracted=False)
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=True)}))
        run(["openalex-refresh", "--target", str(tmp_path), "--older-than", "0"])
        out = capsys.readouterr().out
        assert "RETRACTED" in out
        assert "Retracted (new):      1" in out
        assert "Retracted (reversed): 0" in out


# --------------------------------------------------------------------------- #
# the --porcelain column names WHICH work was un-retracted (#107 item 5)
# --------------------------------------------------------------------------- #
class TestPorcelainUnRetracted:
    def test_porcelain_carries_un_retracted_column_and_tally(self, tmp_path, fake, capsys):
        # An un-retracted work's row differs from an unchanged one only by the id; a count
        # without an id is useless. The appended column distinguishes them.
        _seed(tmp_path, "W1", is_retracted=True, name="a")
        _seed(tmp_path, "W2", is_retracted=False, name="b")
        fake(FakeClient({
            "W1": _raw_work("W1", is_retracted=False),
            "W2": _raw_work("W2", is_retracted=False)}))
        run(["openalex-refresh", "--target", str(tmp_path), "--porcelain",
             "--older-than", "0"])
        out = capsys.readouterr().out
        checks, tallies = {}, {}
        for line in out.strip().splitlines():
            fields = line.split("\t")
            if fields[0] == "check":
                checks[fields[1]] = fields
            else:
                tallies[fields[0]] = fields[1]
        # un_retracted is the last, appended column (index 8); W1 un-retracted, W2 not.
        assert checks["W1"][8] == "1"
        assert checks["W2"][8] == "0"
        # A #83 parser keying the earlier fixed columns is unaffected.
        assert checks["W1"][5] == "0"  # retracted (new)
        assert tallies["un_retracted"] == "1"


# --------------------------------------------------------------------------- #
# (2) the command: live query, gate, write, silence
# --------------------------------------------------------------------------- #
class TestAcknowledgeCommand:
    def test_records_live_retraction_and_silences_the_repeat(self, tmp_path, fake, capsys):
        _seed(tmp_path, "W1", is_retracted=False)
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=True)}))

        # It surfaces on refresh.
        run(["openalex-refresh", "--target", str(tmp_path), "--older-than", "0"])
        assert "RETRACTED" in capsys.readouterr().out

        code = run(["openalex-acknowledge-retraction", "--id", "W1",
                    "--target", str(tmp_path), "--yes"])
        out = capsys.readouterr().out
        assert code == 0
        assert "Recorded OpenAlex's retraction flag for W1" in out
        assert "withdraw" not in out.lower()
        assert _ledger_is_retracted(tmp_path, "W1") is True

        # Silenced on the next refresh.
        run(["openalex-refresh", "--target", str(tmp_path), "--older-than", "0"])
        assert "Retracted (new):      0" in capsys.readouterr().out

    def test_prints_the_note_from_the_live_value(self, tmp_path, fake, capsys):
        _seed(tmp_path, "W1", is_retracted=False)
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=True)}))
        run(["openalex-acknowledge-retraction", "--id", "W1",
             "--target", str(tmp_path), "--yes"])
        out = capsys.readouterr().out
        assert "OpenAlex now flags W1 as RETRACTED" in out
        # OpenAlex's opinion, never a bare fact.
        assert "OpenAlex's opinion" in out
        assert "PubMed" in out

    def test_un_retraction_clears_the_key_and_silences(self, tmp_path, fake, capsys,
                                                       monkeypatch):
        # Interactive, not `--yes`: a clear is gated on a human (#106, see TestYesCannotClear).
        _seed(tmp_path, "W1", is_retracted=True)
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=False)}))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
        code = run(["openalex-acknowledge-retraction", "--id", "W1",
                    "--target", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0
        # The note comes FIRST, before the prompt: it is the whole reason `--yes` may not
        # clear (#106/#414). If it stopped being printed, the interactive path would ask a
        # human to confirm a clear they were never shown, and the gate would guard nothing.
        assert "no longer flags W1 as retracted" in out
        assert out.index("no longer flags") < out.index("Cleared the retraction")
        assert "Cleared the retraction" in out
        # The clear REMOVES the key — never a literal False or "".
        record = _ledger_record(tmp_path, "W1")
        assert record is not None
        assert "is_retracted" not in record
        # And the un-retraction no longer surfaces.
        run(["openalex-refresh", "--target", str(tmp_path), "--older-than", "0"])
        assert "Retracted (reversed): 0" in capsys.readouterr().out

    def test_failed_live_query_writes_nothing_and_exits_nonzero(self, tmp_path, fake):
        _seed(tmp_path, "W1", is_retracted=True)
        before = _kb_snapshot(tmp_path)
        fake(FakeClient(raise_exc=OpenAlexConnectionError("cannot reach OpenAlex")))
        code = run(["openalex-acknowledge-retraction", "--id", "W1",
                    "--target", str(tmp_path), "--yes"])
        assert code == 2  # a connection failure is a hard, retryable non-zero
        assert _kb_snapshot(tmp_path) == before
        assert _ledger_is_retracted(tmp_path, "W1") is True

    def test_rate_limit_writes_nothing_and_exits_nonzero(self, tmp_path, fake):
        _seed(tmp_path, "W1", is_retracted=True)
        before = _kb_snapshot(tmp_path)
        fake(FakeClient(raise_exc=OpenAlexRateLimitError("HTTP 429")))
        code = run(["openalex-acknowledge-retraction", "--id", "W1",
                    "--target", str(tmp_path), "--yes"])
        assert code == 2
        assert _kb_snapshot(tmp_path) == before

    def test_missing_record_writes_nothing_and_exits_nonzero(self, tmp_path, fake, capsys):
        _seed(tmp_path, "W1", is_retracted=True)
        before = _kb_snapshot(tmp_path)
        fake(FakeClient(not_found=["W1"]))
        code = run(["openalex-acknowledge-retraction", "--id", "W1",
                    "--target", str(tmp_path), "--yes"])
        assert code == 1
        assert "no record" in capsys.readouterr().err.lower()
        assert _kb_snapshot(tmp_path) == before

    def test_no_tty_without_yes_refuses_and_writes_nothing(self, tmp_path, fake, capsys):
        # Under pytest stdin is not a TTY, so this is the pipe/script case.
        _seed(tmp_path, "W1", is_retracted=False)
        before = _kb_snapshot(tmp_path)
        client = fake(FakeClient({"W1": _raw_work("W1", is_retracted=True)}))
        code = run(["openalex-acknowledge-retraction", "--id", "W1",
                    "--target", str(tmp_path)])
        assert code == 1
        assert "refusing to acknowledge without a terminal" in capsys.readouterr().err
        assert client.call_count == 0  # it refuses BEFORE hitting the API
        assert _kb_snapshot(tmp_path) == before

    def test_invalid_id_is_rejected_before_any_call(self, tmp_path, fake, capsys):
        _seed(tmp_path, "W1", is_retracted=True)
        client = fake(FakeClient({"W1": _raw_work("W1", is_retracted=False)}))
        code = run(["openalex-acknowledge-retraction", "--id", "W0",
                    "--target", str(tmp_path), "--yes"])
        assert code == 1
        assert "invalid OpenAlex work id" in capsys.readouterr().err
        assert client.call_count == 0

    def test_id_is_required(self, tmp_path):
        # --yes is only ever paired with the required, explicit --id: there is no way to
        # run without one. argparse exits 2 on the missing required argument.
        with pytest.raises(SystemExit):
            run(["openalex-acknowledge-retraction", "--target", str(tmp_path), "--yes"])

    def test_interactive_yes_writes(self, tmp_path, fake, monkeypatch):
        _seed(tmp_path, "W1", is_retracted=False)
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=True)}))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
        code = run(["openalex-acknowledge-retraction", "--id", "W1",
                    "--target", str(tmp_path)])
        assert code == 0
        assert _ledger_is_retracted(tmp_path, "W1") is True

    def test_interactive_no_aborts_and_writes_nothing(self, tmp_path, fake, monkeypatch, capsys):
        _seed(tmp_path, "W1", is_retracted=False)
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=True)}))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
        code = run(["openalex-acknowledge-retraction", "--id", "W1",
                    "--target", str(tmp_path)])
        assert code == 0
        assert "Aborted" in capsys.readouterr().out
        assert _ledger_is_retracted(tmp_path, "W1") is False

    def test_accepts_openalex_url_and_lowercase(self, tmp_path, fake):
        _seed(tmp_path, "W1", is_retracted=False)
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=True)}))
        code = run(["openalex-acknowledge-retraction",
                    "--id", "https://openalex.org/w1", "--target", str(tmp_path), "--yes"])
        assert code == 0
        assert _ledger_is_retracted(tmp_path, "W1") is True

    def test_md_is_byte_and_mtime_identical_through_the_whole_cycle(self, tmp_path, fake):
        # acknowledge must never rewrite the .md (P4). The ledger becomes the sole audit
        # record; the .md never moves a byte or an mtime across retract/un-retract.
        _seed(tmp_path, "W1", is_retracted=False)
        before = _md_snapshot(tmp_path)

        fake(FakeClient({"W1": _raw_work("W1", is_retracted=True)}))
        run(["openalex-acknowledge-retraction", "--id", "W1",
             "--target", str(tmp_path), "--yes"])
        assert _md_snapshot(tmp_path) == before

        fake(FakeClient({"W1": _raw_work("W1", is_retracted=False)}))
        run(["openalex-acknowledge-retraction", "--id", "W1",
             "--target", str(tmp_path), "--yes"])
        assert _md_snapshot(tmp_path) == before


# --------------------------------------------------------------------------- #
# refuse BEFORE the live query, for zero API requests (#107 items 1 & 3)
# --------------------------------------------------------------------------- #
class TestRefusesBeforeQuery:
    def test_front_matter_only_refuses_before_query_zero_api(self, tmp_path, fake, capsys):
        # A pre-#84 work: front matter carries the retraction, no sidecar. acknowledge()
        # writes only sidecars, so this can never be written; refuse before the fetch and
        # prescribe openalex-backfill-provenance (not a command that would exit 1).
        _seed_front_matter_only(tmp_path, "W1", is_retracted=True)
        before = _kb_snapshot(tmp_path)
        client = fake(FakeClient({"W1": _raw_work("W1", is_retracted=False)}))
        code = run(["openalex-acknowledge-retraction", "--id", "W1",
                    "--target", str(tmp_path), "--yes"])
        assert code == 1
        err = capsys.readouterr().err
        assert "front matter" in err and "openalex-backfill-provenance" in err
        assert client.call_count == 0  # ZERO API requests
        assert not (tmp_path / "source-provenance").exists()  # no ledger fabricated
        assert _kb_snapshot(tmp_path) == before

    def test_absent_id_refuses_before_query_zero_api(self, tmp_path, fake, capsys):
        _seed(tmp_path, "W1", is_retracted=True)  # a different work
        before = _kb_snapshot(tmp_path)
        client = fake(FakeClient({"W2": _raw_work("W2", is_retracted=False)}))
        code = run(["openalex-acknowledge-retraction", "--id", "W2",
                    "--target", str(tmp_path), "--yes"])
        assert code == 1
        assert "no OpenAlex record" in capsys.readouterr().err
        assert client.call_count == 0  # ZERO API requests
        assert _kb_snapshot(tmp_path) == before

    def test_unreadable_ledger_refuses_before_query_zero_api_mtime_identical(
        self, tmp_path, fake, capsys
    ):
        # One clean sidecar (not retracted) for the target id, plus one corrupt sidecar
        # that could carry it: the recorded value is unknowable, so refuse before the fetch
        # rather than assert "the ledger did not record" on an incomplete view.
        _seed(tmp_path, "W1", is_retracted=False, name="good")
        (tmp_path / "source-provenance" / "bad.json").write_text("{ broken")
        before = _kb_snapshot(tmp_path)
        client = fake(FakeClient({"W1": _raw_work("W1", is_retracted=True)}))
        code = run(["openalex-acknowledge-retraction", "--id", "W1",
                    "--target", str(tmp_path), "--yes"])
        assert code == 1
        assert "cannot read every provenance ledger" in capsys.readouterr().err
        assert client.call_count == 0  # ZERO API requests
        assert _kb_snapshot(tmp_path) == before


# --------------------------------------------------------------------------- #
# H3 — get_work follows redirects; a merged id must not be acknowledged under the old key
# --------------------------------------------------------------------------- #
class TestMergedIdRefused:
    def test_merged_id_refuses_and_writes_nothing(self, tmp_path, fake, capsys):
        # Request W1; OpenAlex answers a merged work whose id is W2. Acknowledging under W1
        # would record a decision about the wrong identity.
        _seed(tmp_path, "W1", is_retracted=False)
        before = _kb_snapshot(tmp_path)
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=True, returned_id="W2")}))
        code = run(["openalex-acknowledge-retraction", "--id", "W1",
                    "--target", str(tmp_path), "--yes"])
        assert code == 1
        err = capsys.readouterr().err
        assert "different id" in err and "W2" in err
        assert _kb_snapshot(tmp_path) == before
        assert _ledger_is_retracted(tmp_path, "W1") is False


# --------------------------------------------------------------------------- #
# nothing to acknowledge exits 0 without a note or a prompt (#107 item 7)
# --------------------------------------------------------------------------- #
class TestNothingToAcknowledge:
    def test_both_not_retracted_exits_zero_without_prompting(
        self, tmp_path, fake, monkeypatch, capsys
    ):
        _seed(tmp_path, "W1", is_retracted=False)
        before = _kb_snapshot(tmp_path)
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=False)}))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        def _boom(*a, **k):
            raise AssertionError("must not prompt when there is nothing to acknowledge")

        monkeypatch.setattr("builtins.input", _boom)
        code = run(["openalex-acknowledge-retraction", "--id", "W1",
                    "--target", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "nothing to acknowledge" in out
        assert "RETRACTED" not in out
        # A no-op leaves every sidecar byte- and mtime_ns-identical.
        assert _kb_snapshot(tmp_path) == before

    def test_both_retracted_exits_zero_without_writing(self, tmp_path, fake, capsys):
        _seed(tmp_path, "W1", is_retracted=True)
        before = _kb_snapshot(tmp_path)
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=True)}))
        code = run(["openalex-acknowledge-retraction", "--id", "W1",
                    "--target", str(tmp_path), "--yes"])
        out = capsys.readouterr().out
        assert code == 0
        assert "already records" in out
        assert _ledger_is_retracted(tmp_path, "W1") is True
        assert _kb_snapshot(tmp_path) == before  # no-op: sidecar untouched


# --------------------------------------------------------------------------- #
# the notes must attribute a front-matter value to front matter, not the ledger (#107 #2)
# --------------------------------------------------------------------------- #
class TestNoteProvenance:
    def test_front_matter_retraction_note_says_front_matter_not_ledger(
        self, tmp_path, fake, capsys
    ):
        # A pre-#84 work with no ledger, not recorded retracted; OpenAlex now flags it.
        _seed_front_matter_only(tmp_path, "W1", is_retracted=False)
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=True)}))
        run(["openalex-refresh", "--target", str(tmp_path), "--older-than", "0"])
        out = capsys.readouterr().out
        assert "RETRACTED" in out
        assert "which the front matter did not record" in out
        assert "which the ledger did not record" not in out

    def test_front_matter_un_retraction_note_makes_no_false_claim(
        self, tmp_path, fake, capsys
    ):
        # A pre-#84 work whose front matter records a retraction OpenAlex has reversed.
        _seed_front_matter_only(tmp_path, "W1", is_retracted=True)
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=False)}))
        run(["openalex-refresh", "--target", str(tmp_path), "--older-than", "0"])
        out = capsys.readouterr().out
        assert "No longer flagged as retracted" in out
        assert "its front matter still records a retraction" in out
        # No ledger to diverge, so no divergence/re-import claim, and no prescription of a
        # command that would exit 1 for this work.
        assert "re-import will error" not in out
        assert "openalex-acknowledge-retraction --id" not in out
        assert "openalex-backfill-provenance" in out


# --------------------------------------------------------------------------- #
# is_retracted is not identifying: re-import errors == 0 in either direction
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# #106 / #414 — `--yes` may RECORD a retraction, never CLEAR one
#
# The rule was established (31138a2) after this command was written (52f146e) and never
# applied back to it; #414 closed that gap. The gate does not rest on a parsing weakness —
# `is_retracted` is a structured boolean — but on direction: recording makes noise,
# clearing creates silence, and silence needs a human at the prompt (#93).
# --------------------------------------------------------------------------- #
class TestYesCannotClear:
    def test_yes_refuses_the_clear_and_leaves_the_kb_byte_identical(
        self, tmp_path, fake, capsys
    ):
        _seed(tmp_path, "W1", is_retracted=True)
        before = _kb_snapshot(tmp_path)
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=False)}))
        code = run(["openalex-acknowledge-retraction", "--id", "W1",
                    "--target", str(tmp_path), "--yes"])
        err = capsys.readouterr().err
        assert code == 1
        assert "refusing to clear the retraction recorded for W1 with --yes" in err
        # It names the working path: an interactive re-run without --yes.
        assert "without --yes" in err and "terminal" in err and "prompt" in err
        assert "Nothing written." in err
        # Byte- and mtime_ns-identical: no ledger write, no .md write.
        assert _kb_snapshot(tmp_path) == before
        assert _ledger_is_retracted(tmp_path, "W1") is True

    def test_the_refusal_does_not_claim_openalex_misread_anything(
        self, tmp_path, fake, capsys
    ):
        # Unlike arXiv's, this refusal must not blame a parse miss: OpenAlex's answer is a
        # structured boolean. It concedes the flag may be OpenAlex correcting a false
        # positive and still refuses, because --yes means nobody reads the note.
        _seed(tmp_path, "W1", is_retracted=True)
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=False)}))
        code = run(["openalex-acknowledge-retraction", "--id", "W1",
                    "--target", str(tmp_path), "--yes"])
        captured = capsys.readouterr()
        assert code == 1
        assert "false positive" in captured.err
        assert "nobody sees it" in captured.err
        # The note a human would have read is never printed under --yes.
        assert captured.out == ""

    def test_yes_still_records_a_retraction(self, tmp_path, fake, capsys):
        # The loud direction is untouched: the gate is on the clear only.
        _seed(tmp_path, "W1", is_retracted=False)
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=True)}))
        code = run(["openalex-acknowledge-retraction", "--id", "W1",
                    "--target", str(tmp_path), "--yes"])
        assert code == 0
        assert "refusing to clear" not in capsys.readouterr().err
        assert _ledger_is_retracted(tmp_path, "W1") is True

    def test_yes_on_an_already_matching_clear_is_not_refused(self, tmp_path, fake, capsys):
        # Ledger records no retraction and OpenAlex flags none. This input never reaches the
        # gate at all — the "nothing to acknowledge" equality check above it returns 0 first
        # — so what this pins is only that agreement still exits 0 quietly under `--yes`,
        # not anything about how the gate discriminates.
        _seed(tmp_path, "W1", is_retracted=False)
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=False)}))
        code = run(["openalex-acknowledge-retraction", "--id", "W1",
                    "--target", str(tmp_path), "--yes"])
        captured = capsys.readouterr()
        assert code == 0
        assert "refusing to clear" not in captured.err
        assert "nothing to acknowledge" in captured.out

    def test_the_three_closing_commands_agree_on_the_yes_help(self, tmp_path):
        # #414's regression guard: the asymmetry started as an undocumented help string.
        parser = cli.build_parser()
        actions = parser._subparsers._group_actions[0].choices
        for name in ("openalex-acknowledge-retraction", "arxiv-acknowledge-withdrawal",
                     "pubmed-acknowledge-retraction"):
            help_text = next(a.help for a in actions[name]._actions if a.dest == "yes")
            assert "never clear one" in help_text, name


class TestReimportStaysClean:
    def test_reimport_after_un_retraction_clear_has_zero_errors(self, tmp_path, fake,
                                                                monkeypatch):
        # Seed a ledger-backed work recorded as retracted; OpenAlex reverses it.
        _seed(tmp_path, "W1", is_retracted=True, doi="10.1/a", journal="J")
        un_retracted = parse_work(_raw_work("W1", is_retracted=False, doi="10.1/a",
                                            journal="J"))

        # Unlike arXiv's identifying withdrawn_by, a diverging is_retracted never errors a
        # re-import — even BEFORE acknowledging.
        divergent = import_works([un_retracted], target=tmp_path,
                                 imported_at="2026-02-01T00:00:00+00:00")
        assert divergent.errors == 0

        # The human acknowledges the un-retraction at the prompt (a clear is never `--yes`);
        # the flag is cleared by removing the key.
        fake(FakeClient({"W1": _raw_work("W1", is_retracted=False, doi="10.1/a",
                                         journal="J")}))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
        code = run(["openalex-acknowledge-retraction", "--id", "W1",
                    "--target", str(tmp_path)])
        assert code == 0
        assert _ledger_is_retracted(tmp_path, "W1") is False

        # And a fresh re-import still lands cleanly.
        repaired = import_works([un_retracted], target=tmp_path,
                                imported_at="2026-03-01T00:00:00+00:00")
        assert repaired.errors == 0
