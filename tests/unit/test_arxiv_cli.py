# SPDX-License-Identifier: Apache-2.0
"""CLI tests for `factlog arxiv-import` (#60, spec §11 Step 3).

The real arXiv client is replaced via ``_make_arxiv_client`` so the command runs
without the network. A temp KB (with sources/) is the target.
"""
from __future__ import annotations

from datetime import date

import pytest

from factlog import cli
from factlog.integrations.arxiv.client import (
    ArxivConnectionError,
    ArxivResponseError,
)
from factlog.integrations.arxiv.id_normalizer import ArxivId
from factlog.integrations.arxiv.work_parser import ParsedArxivWork


def _kb(tmp_path):
    (tmp_path / "sources").mkdir()
    return tmp_path


def _work(arxiv_id="1706.03762", version=5, title="A paper", withdrawn_by=None,
          doi=None) -> ParsedArxivWork:
    return ParsedArxivWork(
        arxiv_id=arxiv_id,
        version=version,
        title=title,
        authors=("Ann Author",),
        abstract="An abstract.",
        primary_category="cs.CL",
        categories=("cs.CL",),
        submitted=date(2017, 6, 12),
        last_updated=date(2017, 6, 12),
        doi=doi,
        withdrawn_by=withdrawn_by,
        abs_url=f"https://arxiv.org/abs/{arxiv_id}v{version}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v{version}",
    )


class FakeClient:
    """Replays a canned BatchResult; records the ids it was asked for."""

    def __init__(self, works=None, missing=None, raise_exc=None):
        self._works = works if works is not None else [_work()]
        self._missing = missing if missing is not None else []
        self._raise = raise_exc
        self.calls: list[list[str]] = []

    def fetch_works(self, ids):
        from factlog.integrations.arxiv.client import BatchResult

        self.calls.append([str(i) for i in ids])
        if self._raise is not None:
            raise self._raise
        return BatchResult(list(self._works), list(self._missing))


@pytest.fixture
def fake(monkeypatch):
    def install(client):
        monkeypatch.setattr(cli, "_make_arxiv_client", lambda config: client)
        return client

    return install


def run(argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


def sources(kb):
    return sorted(p.name for p in (kb / "sources").glob("*.md"))


class TestImport:
    def test_import_by_id_writes_a_source(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        client = fake(FakeClient())
        assert run(["arxiv-import", "--id", "1706.03762", "--target", str(kb)]) == 0
        assert client.calls == [["1706.03762"]]
        assert len(sources(kb)) == 1
        assert "Imported: 1" in capsys.readouterr().out

    def test_inline_version_is_forwarded(self, tmp_path, fake):
        kb = _kb(tmp_path)
        client = fake(FakeClient())
        run(["arxiv-import", "--id", "2311.09277v2", "--target", str(kb)])
        assert client.calls == [["2311.09277v2"]]

    def test_id_is_required(self):
        with pytest.raises(SystemExit):
            run(["arxiv-import", "--target", "x"])

    def test_imported_at_is_a_real_utc_timestamp(self, tmp_path, fake):
        # The importer's unit tests inject `imported_at` as a literal, so the
        # format the CLI actually stamps is only covered here.
        from datetime import datetime, timezone

        kb = _kb(tmp_path)
        fake(FakeClient())
        run(["arxiv-import", "--id", "1706.03762", "--target", str(kb)])
        written = next((kb / "sources").glob("*.md")).read_text()
        line = next(ln for ln in written.splitlines()
                    if ln.startswith("imported_at:"))
        stamped = datetime.fromisoformat(line.split(": ", 1)[1].strip().strip('"'))
        assert stamped.tzinfo is not None
        assert stamped.utcoffset() == timezone.utc.utcoffset(None)

    def test_dry_run_writes_nothing(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient())
        assert run(["arxiv-import", "--id", "1706.03762", "--target", str(kb),
                    "--dry-run"]) == 0
        assert sources(kb) == []
        assert "Would import: 1" in capsys.readouterr().out

    def test_reimport_is_skipped(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient())
        run(["arxiv-import", "--id", "1706.03762", "--target", str(kb)])
        assert run(["arxiv-import", "--id", "1706.03762", "--target", str(kb)]) == 0
        assert "Skipped:  1" in capsys.readouterr().out
        assert len(sources(kb)) == 1

    def test_batch_with_one_good_and_one_missing(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(works=[_work("1706.03762")],
                        missing=[ArxivId("9999.99999")]))
        code = run(["arxiv-import", "--id", "1706.03762", "--id", "9999.99999",
                    "--target", str(kb)])
        assert code == 1  # a per-id error sets exit 1
        out = capsys.readouterr().out
        assert "Imported: 1" in out
        assert "Errors:   1" in out
        assert len(sources(kb)) == 1

    def test_a_syntactically_bad_id_is_non_fatal(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        client = fake(FakeClient(works=[_work("1706.03762")]))
        code = run(["arxiv-import", "--id", "1706.03762", "--id", "notanid",
                    "--target", str(kb)])
        assert code == 1
        # The bad id never reached the network; only the valid one was requested.
        assert client.calls == [["1706.03762"]]
        assert len(sources(kb)) == 1
        assert "Errors:   1" in capsys.readouterr().out

    def test_all_ids_invalid_never_calls_the_client(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        monkeypatch.setattr(
            cli, "_make_arxiv_client",
            lambda config: pytest.fail("must not build a client with no valid ids"),
        )
        assert run(["arxiv-import", "--id", "notanid", "--target", str(kb)]) == 1
        assert sources(kb) == []
        assert "Errors:   1" in capsys.readouterr().out

    def test_connection_failure_exits_two(self, tmp_path, fake, capsys):
        fake(FakeClient(raise_exc=ArxivConnectionError("cannot reach arXiv")))
        assert run(["arxiv-import", "--id", "1706.03762",
                    "--target", str(_kb(tmp_path))]) == 2
        assert "cannot reach arXiv" in capsys.readouterr().err

    def test_response_failure_exits_one(self, tmp_path, fake, capsys):
        fake(FakeClient(raise_exc=ArxivResponseError("truncated feed")))
        assert run(["arxiv-import", "--id", "1706.03762",
                    "--target", str(_kb(tmp_path))]) == 1
        assert "truncated feed" in capsys.readouterr().err


class TestWithdrawalWarning:
    def test_withdrawn_import_warns_on_stderr_naming_the_agent(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(works=[_work("1904.09773", withdrawn_by="admin")]))
        assert run(["arxiv-import", "--id", "1904.09773", "--target", str(kb)]) == 0
        captured = capsys.readouterr()
        assert "withdrawn (by arXiv administrators)" in captured.err
        assert "retracted" not in captured.err.lower()
        # The warning never leaks onto stdout (the tmp path may contain the word,
        # so match the warning phrase, not the bare token).
        assert "withdrawn (by" not in captured.out

    def test_author_withdrawal_names_the_author(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(works=[_work("1301.4231", withdrawn_by="author")]))
        run(["arxiv-import", "--id", "1301.4231", "--target", str(kb)])
        assert "withdrawn (by the author)" in capsys.readouterr().err

    def test_a_live_paper_produces_no_warning(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(works=[_work("1706.03762")]))
        run(["arxiv-import", "--id", "1706.03762", "--target", str(kb)])
        assert "withdrawn" not in capsys.readouterr().err.lower()

    def test_skipped_withdrawn_paper_is_not_warned_about(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(works=[_work("1904.09773", withdrawn_by="admin")]))
        run(["arxiv-import", "--id", "1904.09773", "--target", str(kb)])
        capsys.readouterr()
        run(["arxiv-import", "--id", "1904.09773", "--target", str(kb)])
        assert "withdrawn" not in capsys.readouterr().err.lower()


def _seed_openalex(kb, *, arxiv_id="1706.03762"):
    """Put an OpenAlex-primary original of the paper in the KB, return its path."""
    from factlog.integrations.openalex.source_writer import OpenAlexSourceWriter
    from factlog.integrations.openalex.work_parser import ParsedWork

    result = OpenAlexSourceWriter().write(
        ParsedWork(openalex_id="W1", title="A paper", authors=("Ann Author",),
                   year=2017, journal="J", arxiv_id=arxiv_id),
        kb, imported_at="2026-01-01T00:00:00Z")
    assert result.status == "imported"
    return result.path


class TestMerge:
    def test_merge_is_labelled_merged_in_human_output_not_error(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        existing = _seed_openalex(kb)
        before = existing.stat().st_mtime_ns
        fake(FakeClient(works=[_work("1706.03762")]))
        code = run(["arxiv-import", "--id", "1706.03762", "--target", str(kb)])
        assert code == 0  # a merge is a success
        captured = capsys.readouterr()
        out = captured.out
        assert "⇄" in out and "merged" in out
        assert "Merged:   1" in out
        # Never mislabelled: the per-work line is not "- error", and no glyph is
        # the unknown-status '?'. (The "Errors:   0" summary line is expected.)
        assert "?" not in out
        assert "- error" not in out
        assert "Errors:   0" in out
        # stderr carries no spurious error either.
        assert "error" not in captured.err.lower()
        # The original .md was not rewritten.
        assert existing.stat().st_mtime_ns == before
        assert len(sources(kb)) == 1

    def test_merge_porcelain_reports_merged_line(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _seed_openalex(kb)
        fake(FakeClient(works=[_work("1706.03762")]))
        code = run(["arxiv-import", "--id", "1706.03762", "--target", str(kb),
                    "--porcelain"])
        assert code == 0
        captured = capsys.readouterr()
        rows = dict(line.split("\t", 1)
                    for line in captured.out.strip().splitlines())
        assert rows["merged"] == "1"
        assert rows["imported"] == "0"
        assert rows["errors"] == "0"
        assert "?" not in captured.out

    def test_dry_run_porcelain_work_row_is_merged(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _seed_openalex(kb)
        fake(FakeClient(works=[_work("1706.03762", version=5)]))
        run(["arxiv-import", "--id", "1706.03762", "--target", str(kb),
             "--dry-run", "--porcelain"])
        out = capsys.readouterr().out
        row = [line for line in out.splitlines() if line.startswith("work\t")][0]
        fields = row.split("\t")
        assert fields[1] == "merged"
        assert fields[2] == "1706.03762v5"
        # No sidecar was written by a dry run.
        assert not (kb / "source-provenance").exists()

    def test_idempotent_merge_via_cli(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        existing = _seed_openalex(kb)
        fake(FakeClient(works=[_work("1706.03762")]))
        run(["arxiv-import", "--id", "1706.03762", "--target", str(kb)])
        sidecar = kb / "source-provenance" / (existing.stem + ".json")
        first = sidecar.read_bytes()
        capsys.readouterr()
        code = run(["arxiv-import", "--id", "1706.03762", "--target", str(kb)])
        assert code == 0
        assert sidecar.read_bytes() == first  # byte-identical no-op


class TestPorcelain:
    def test_porcelain_contract(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient())
        assert run(["arxiv-import", "--id", "1706.03762", "--target", str(kb),
                    "--porcelain"]) == 0
        rows = dict(
            line.split("\t", 1) for line in capsys.readouterr().out.strip().splitlines()
        )
        assert rows["imported"] == "1"
        assert rows["skipped"] == "0"
        assert rows["errors"] == "0"
        assert rows["dry_run"] == "0"
        assert rows["target"].endswith("sources")

    def test_dry_run_porcelain_emits_a_work_row(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(works=[_work("1706.03762", version=5)]))
        run(["arxiv-import", "--id", "1706.03762", "--target", str(kb),
             "--dry-run", "--porcelain"])
        out = capsys.readouterr().out
        row = [line for line in out.splitlines() if line.startswith("work\t")][0]
        fields = row.split("\t")
        assert fields[1] == "imported"
        assert fields[2] == "1706.03762v5"
        assert fields[3].endswith(".md")

    def test_withdrawal_warning_never_pollutes_porcelain_stdout(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(works=[_work("1904.09773", withdrawn_by="admin")]))
        run(["arxiv-import", "--id", "1904.09773", "--target", str(kb), "--porcelain"])
        captured = capsys.readouterr()
        assert "withdrawn" in captured.err.lower()
        assert "withdrawn" not in captured.out.lower()

    def test_error_in_porcelain_sets_exit_one(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(works=[], missing=[ArxivId("9999.99999")]))
        code = run(["arxiv-import", "--id", "9999.99999", "--target", str(kb),
                    "--porcelain"])
        assert code == 1
        rows = dict(
            line.split("\t", 1) for line in capsys.readouterr().out.strip().splitlines()
        )
        assert rows["errors"] == "1"
