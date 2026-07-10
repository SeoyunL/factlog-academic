# SPDX-License-Identifier: Apache-2.0
"""`factlog arxiv-backfill-provenance` — the ledger a pre-#82 paper never got (#114, #105).

A paper imported before #82 has front matter and no provenance sidecar, so a re-import
short-circuits on the front-matter identity match before the sidecar writer and its ledger
is never created; both acknowledge commands then refuse it and point here. This command
materializes that ledger from what the ``.md`` already asserts — ``add_source`` into a
fresh sidecar, no network, no new claim — so the withdrawal signal can finally be
acknowledged and the repeat stops.

The real arXiv client is patched via ``_make_arxiv_client`` so nothing touches the network,
and the tests prove the command itself never even *constructs* one. What is verified here,
with the real CLI:

* the graduation — a front-matter-only withdrawn paper is un-acknowledgeable before backfill
  (``no provenance ledger``, **0** API requests) and acknowledgeable after, and the repeat
  stops on the next check;
* the note graduation — the un-withdrawal note's front-matter branch (#105) switches to the
  ledger branch prescribing the acknowledge command;
* the self-healing typo — a hand-typed ``arxiv_withdrawn_by`` is backfilled, flagged by
  check-versions, and overwritten by acknowledge's live value; it must not persist;
* the false-conflict refusal — an OpenAlex-authored ``.md`` carrying ``arxiv_id`` but no
  ``arxiv_version`` is refused, gets no sidecar, and a later arXiv merge import behaves
  identically to a no-backfill control (``merged``, not ``error``);
* ``--dry-run`` writes nothing (no sidecar dir, ``.md`` ``mtime_ns`` unchanged) and names
  both eligible and refused ids;
* a re-run is a byte- and ``mtime_ns``-identical no-op;
* every ``.md`` is byte- and ``mtime_ns``-identical throughout (P4), instrumenting ``open``;
* the command constructs no API client.
"""
from __future__ import annotations

import builtins
from datetime import date

import pytest

from factlog import cli
from factlog.integrations.arxiv.client import BatchResult
from factlog.integrations.arxiv.id_normalizer import ArxivId
from factlog.integrations.arxiv.work_parser import ParsedArxivWork
from factlog.integrations.common.provenance import read_provenance, sidecar_path


# --------------------------------------------------------------------------- #
# fixtures / helpers
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
    """Maps base id -> work; records every id it was asked for so a test can assert on
    the API-request budget (0 before backfill)."""

    def __init__(self, works):
        self._works = {w.arxiv_id: w for w in works}
        self.calls: list[list[str]] = []

    def fetch_works(self, ids):
        self.calls.append([str(i) for i in ids])
        found, missing = [], []
        for value in ids:
            base = str(value)
            work = self._works.get(base)
            if work is None:
                missing.append(ArxivId(base))
            else:
                found.append(work)
        return BatchResult(found, missing)


@pytest.fixture
def fake(monkeypatch):
    def install(client):
        monkeypatch.setattr(cli, "_make_arxiv_client", lambda config: client)
        return client

    return install


def run(argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


IMPORTED_AT = "2026-01-01T00:00:00+00:00"


def _fm_only(kb, arxiv_id, version, *, withdrawn_by=None, imported_at=IMPORTED_AT, name=None):
    """A pre-#82 paper: front matter, no ledger. Returns the .md path."""
    (kb / "sources").mkdir(exist_ok=True)
    name = name or arxiv_id.replace("/", "_")
    lines = ["---", f"arxiv_id: {arxiv_id}", f"arxiv_version: {version}"]
    if imported_at is not None:
        lines.append(f'imported_at: "{imported_at}"')
    if withdrawn_by is not None:
        lines.append("arxiv_withdrawn: true")
        lines.append(f"arxiv_withdrawn_by: {withdrawn_by}")
    lines.append("---")
    md = kb / "sources" / f"{name}.md"
    md.write_text("\n".join(lines) + f"\n# {name}\n", encoding="utf-8")
    return md


def _ledger_withdrawn_by(kb, arxiv_id):
    for path in (kb / "source-provenance").rglob("*.json"):
        for record in read_provenance(path).records:
            if record.type == "arxiv" and record.id == arxiv_id:
                return record.fields.get("withdrawn_by")
    return None


def _md_snapshot(kb):
    snap = {}
    root = kb / "sources"
    if root.is_dir():
        for path in root.rglob("*.md"):
            st = path.stat()
            snap[path] = (path.read_bytes(), st.st_mtime_ns)
    return snap


# --------------------------------------------------------------------------- #
# 1. the graduation: refused before backfill, acknowledgeable after
# --------------------------------------------------------------------------- #
class TestGraduation:
    def test_front_matter_only_paper_graduates_to_acknowledgeable(self, tmp_path, fake, capsys):
        # A paper imported before it was withdrawn: front matter carries version, no
        # withdrawal, no ledger. arXiv now reports it withdrawn by the author.
        _fm_only(tmp_path, "1706.03762", 7)
        client = fake(FakeClient([_work("1706.03762", version=7, withdrawn_by="author")]))

        # BEFORE: acknowledge refuses (no ledger) and spends ZERO API requests.
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path), "--yes",
        ])
        err = capsys.readouterr().err
        assert code == 1
        assert "no provenance ledger" in err
        assert client.calls == []  # not one request was spent

        # The signal is live and repeats on every check.
        run(["arxiv-check-versions", "--target", str(tmp_path)])
        assert "WITHDRAWN by the author" in capsys.readouterr().out

        # BACKFILL: gives the paper a ledger, offline.
        code = run(["arxiv-backfill-provenance", "--target", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "1706.03762" in out
        assert (tmp_path / "source-provenance" / "1706.03762.json").is_file()

        # AFTER: acknowledge now succeeds, writing arXiv's live value.
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path), "--yes",
        ])
        out = capsys.readouterr().out
        assert code == 0
        assert "Recorded withdrawal by author" in out
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") == "author"

        # The repeat stops on the next check.
        run(["arxiv-check-versions", "--target", str(tmp_path)])
        assert "Newly withdrawn:     0" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# 2. the note graduation: the un-withdrawal note switches branches
# --------------------------------------------------------------------------- #
class TestNoteGraduation:
    def test_un_withdrawal_note_switches_from_105_to_the_acknowledge_command(
        self, tmp_path, fake, capsys
    ):
        # Front matter records a withdrawal arXiv has since reversed (un_withdrawn).
        _fm_only(tmp_path, "1706.03762", 7, withdrawn_by="author")
        fake(FakeClient([_work("1706.03762", version=7, withdrawn_by=None)]))

        # BEFORE backfill: the front-matter branch — points at #105, not a command.
        run(["arxiv-check-versions", "--target", str(tmp_path), "--older-than", "0"])
        out = capsys.readouterr().out
        assert "No longer withdrawn" in out
        assert "#105" in out
        assert "arxiv-acknowledge-withdrawal --id 1706.03762" not in out

        # BACKFILL, then re-check.
        run(["arxiv-backfill-provenance", "--target", str(tmp_path)])
        capsys.readouterr()
        run(["arxiv-check-versions", "--target", str(tmp_path), "--older-than", "0"])
        out = capsys.readouterr().out
        # AFTER: the ledger branch — prescribes the acknowledge command, no longer #105.
        assert "No longer withdrawn" in out
        assert "arxiv-acknowledge-withdrawal --id 1706.03762" in out
        assert "#105" not in out


# --------------------------------------------------------------------------- #
# 3. the self-healing typo: a hand-typed withdrawn_by must not persist
# --------------------------------------------------------------------------- #
class TestSelfHealingTypo:
    def test_typo_is_backfilled_flagged_then_overwritten_by_the_live_value(
        self, tmp_path, fake, capsys
    ):
        # Front matter carries a hand-typed garbage withdrawal agent (#98's shape).
        _fm_only(tmp_path, "1706.03762", 7, withdrawn_by="typo")
        fake(FakeClient([_work("1706.03762", version=7, withdrawn_by="author")]))

        # BACKFILL records the typo verbatim (it is what the KB believed at import).
        run(["arxiv-backfill-provenance", "--target", str(tmp_path)])
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") == "typo"

        # check-versions flags it: a value comparison ("author" != "typo") resurfaces it,
        # and the note names the recorded garbage rather than hiding it.
        capsys.readouterr()
        run(["arxiv-check-versions", "--target", str(tmp_path), "--older-than", "0"])
        out = capsys.readouterr().out
        assert "WITHDRAWN by the author" in out
        assert "'typo'" in out

        # acknowledge writes arXiv's LIVE value, overwriting the typo — it self-heals.
        run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path), "--yes",
        ])
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") == "author"

        # And the signal is silenced.
        capsys.readouterr()
        run(["arxiv-check-versions", "--target", str(tmp_path), "--older-than", "0"])
        assert "Newly withdrawn:     0" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# 4. the false-conflict refusal still holds through the CLI
# --------------------------------------------------------------------------- #
class TestFalseConflictRefusal:
    def _openalex_authored_md(self, kb):
        """An OpenAlex-primary .md that echoes arxiv_id but, like every OpenAlex file,
        carries no arxiv_version. Uses the real writer."""
        from factlog.integrations.openalex.source_writer import OpenAlexSourceWriter
        from factlog.integrations.openalex.work_parser import ParsedWork

        result = OpenAlexSourceWriter().write(
            ParsedWork(openalex_id="W1", arxiv_id="2311.09277", doi="10.1/x",
                       work_type="article", journal="Nature", title="A Paper"),
            kb, imported_at="2025-01-01T00:00:00Z",
        )
        assert result.status == "imported"
        return result.path

    def test_openalex_authored_md_is_refused_and_gets_no_arxiv_sidecar(
        self, tmp_path, fake, capsys
    ):
        existing = self._openalex_authored_md(tmp_path)
        side = sidecar_path(existing)
        before = side.read_bytes(), side.stat().st_mtime_ns

        code = run(["arxiv-backfill-provenance", "--target", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "2311.09277" in out
        assert "Refused" in out
        # No arxiv record was written; the OpenAlex sidecar is byte- and mtime_ns-identical.
        keys = {(r.type, r.id) for r in read_provenance(side).records}
        assert ("arxiv", "2311.09277") not in keys
        assert (side.read_bytes(), side.stat().st_mtime_ns) == before

    def test_later_merge_import_matches_the_no_backfill_control(self, tmp_path, fake):
        from factlog.integrations.arxiv.source_writer import ArxivSourceWriter

        def _parsed():
            return ParsedArxivWork(
                arxiv_id="2311.09277", version=7, title="A Paper",
                authors=("Ada Lovelace",), abstract="An abstract.",
                primary_category="cs.CL", categories=("cs.CL",),
                submitted=date(2023, 11, 15), last_updated=date(2023, 11, 20),
                withdrawn_by=None,
                abs_url="https://arxiv.org/abs/2311.09277v7",
            )

        # CONTROL: no backfill; a fresh arXiv import merges into the OpenAlex sidecar.
        control = tmp_path / "control"
        control.mkdir()
        self._openalex_authored_md(control)
        ctrl = ArxivSourceWriter().write(_parsed(), control, imported_at="2026-02-02T00:00:00Z")
        assert ctrl.status == "merged"

        # TREATED: backfill first (which refuses), then import behaves IDENTICALLY.
        treated = tmp_path / "treated"
        treated.mkdir()
        self._openalex_authored_md(treated)
        run(["arxiv-backfill-provenance", "--target", str(treated)])
        after = ArxivSourceWriter().write(_parsed(), treated, imported_at="2026-02-02T00:00:00Z")
        assert after.status == ctrl.status == "merged"


# --------------------------------------------------------------------------- #
# 5. --dry-run: writes nothing, names both eligible and refused ids
# --------------------------------------------------------------------------- #
class TestDryRun:
    def test_dry_run_writes_nothing_and_names_both_sets(self, tmp_path, fake, capsys):
        # One eligible (arXiv-authored, has version) and one refused (OpenAlex-authored,
        # no version).
        _fm_only(tmp_path, "1706.03762", 7)
        from factlog.integrations.openalex.source_writer import OpenAlexSourceWriter
        from factlog.integrations.openalex.work_parser import ParsedWork
        OpenAlexSourceWriter().write(
            ParsedWork(openalex_id="W1", arxiv_id="2311.09277", doi="10.1/x",
                       work_type="article", journal="Nature", title="A Paper"),
            tmp_path, imported_at="2025-01-01T00:00:00Z",
        )
        before = _md_snapshot(tmp_path)

        code = run(["arxiv-backfill-provenance", "--target", str(tmp_path), "--dry-run"])
        out = capsys.readouterr().out
        assert code == 0
        # Both ids are named: the eligible one and the refused one.
        assert "1706.03762" in out
        assert "2311.09277" in out
        assert "Would backfill" in out
        assert "Refused" in out

        # Nothing was written: no arXiv sidecar for the eligible paper, and every .md is
        # byte- and mtime_ns-identical.
        assert not (tmp_path / "source-provenance" / "1706.03762.json").exists()
        assert _md_snapshot(tmp_path) == before

    def test_dry_run_leaves_no_new_sidecar_dir_when_kb_had_none(self, tmp_path, fake, capsys):
        _fm_only(tmp_path, "1706.03762", 7)
        run(["arxiv-backfill-provenance", "--target", str(tmp_path), "--dry-run"])
        assert not (tmp_path / "source-provenance").exists()


# --------------------------------------------------------------------------- #
# 6. re-run is a byte- and mtime_ns-identical no-op
# --------------------------------------------------------------------------- #
class TestIdempotent:
    def test_second_run_is_byte_and_mtime_identical(self, tmp_path, fake, capsys):
        _fm_only(tmp_path, "1706.03762", 7)
        run(["arxiv-backfill-provenance", "--target", str(tmp_path)])
        side = tmp_path / "source-provenance" / "1706.03762.json"
        first = side.read_bytes(), side.stat().st_mtime_ns

        # Once backfilled, the sidecar makes the paper ledger-classified, so the second run
        # skips it entirely (never re-stamps) — a byte- and mtime_ns-identical no-op.
        capsys.readouterr()
        code = run(["arxiv-backfill-provenance", "--target", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "No front-matter-only arXiv papers found" in out
        assert (side.read_bytes(), side.stat().st_mtime_ns) == first


# --------------------------------------------------------------------------- #
# 7. every .md is byte- and mtime_ns-identical (P4) — instrument open, not just diff
# --------------------------------------------------------------------------- #
class TestNeverOpensMdForWrite:
    def test_no_md_is_opened_for_write(self, tmp_path, fake, monkeypatch):
        md = _fm_only(tmp_path, "1706.03762", 7)
        before = md.read_bytes(), md.stat().st_mtime_ns

        real_open = builtins.open
        offenders: list[str] = []

        def watched_open(file, mode="r", *args, **kwargs):
            try:
                path = str(file)
            except Exception:
                path = ""
            if path.endswith(".md") and any(c in mode for c in ("w", "a", "x", "+")):
                offenders.append(f"{path}:{mode}")
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", watched_open)
        run(["arxiv-backfill-provenance", "--target", str(tmp_path)])
        monkeypatch.setattr(builtins, "open", real_open)

        assert offenders == []
        assert (md.read_bytes(), md.stat().st_mtime_ns) == before


# --------------------------------------------------------------------------- #
# 8. no network: the command constructs no API client
# --------------------------------------------------------------------------- #
class TestNoNetwork:
    def test_command_never_constructs_an_arxiv_client(self, tmp_path, monkeypatch, capsys):
        _fm_only(tmp_path, "1706.03762", 7)

        def _boom(config):
            raise AssertionError("arxiv-backfill-provenance must not construct an API client")

        monkeypatch.setattr(cli, "_make_arxiv_client", _boom)
        code = run(["arxiv-backfill-provenance", "--target", str(tmp_path)])
        assert code == 0
        assert (tmp_path / "source-provenance" / "1706.03762.json").is_file()

    def test_dry_run_also_constructs_no_client(self, tmp_path, monkeypatch):
        _fm_only(tmp_path, "1706.03762", 7)
        monkeypatch.setattr(
            cli, "_make_arxiv_client",
            lambda config: (_ for _ in ()).throw(AssertionError("no client")),
        )
        assert run(["arxiv-backfill-provenance", "--target", str(tmp_path), "--dry-run"]) == 0


# --------------------------------------------------------------------------- #
# 9. porcelain contract
# --------------------------------------------------------------------------- #
class TestPorcelain:
    def test_porcelain_rows_and_summary(self, tmp_path, fake, capsys):
        _fm_only(tmp_path, "1706.03762", 7)
        code = run(["arxiv-backfill-provenance", "--target", str(tmp_path), "--porcelain"])
        out = capsys.readouterr().out
        assert code == 0
        lines = out.splitlines()
        assert any(line.startswith("result\tbackfilled\t1706.03762\t") for line in lines)
        assert "backfilled\t1" in lines
        assert "refused\t0" in lines
        assert "errors\t0" in lines
        assert "dry_run\t0" in lines
