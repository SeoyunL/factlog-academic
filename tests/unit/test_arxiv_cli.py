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


class FakeSearchClient:
    """Replays canned search results; records the search() call it received."""

    def __init__(self, works=None, total=None, raise_exc=None):
        self._works = works if works is not None else [_work()]
        self._total = total if total is not None else len(self._works)
        self._raise = raise_exc
        self.calls: list[dict] = []

    def search(self, query, *, categories=(), year=None, limit=None, sort=None, start=0):
        self.calls.append({
            "query": query, "categories": tuple(categories), "year": year,
            "limit": limit, "sort": sort, "start": start,
        })
        if self._raise is not None:
            raise self._raise
        return list(self._works), self._total


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
        from factlog.integrations.common.provenance import read_provenance, sidecar_path

        kb = _kb(tmp_path)
        existing = _seed_openalex(kb)
        # The OpenAlex seed wrote its own one-record ledger (#73); capture it.
        before = sidecar_path(existing).read_bytes()
        fake(FakeClient(works=[_work("1706.03762", version=5)]))
        run(["arxiv-import", "--id", "1706.03762", "--target", str(kb),
             "--dry-run", "--porcelain"])
        out = capsys.readouterr().out
        row = [line for line in out.splitlines() if line.startswith("work\t")][0]
        fields = row.split("\t")
        assert fields[1] == "merged"
        assert fields[2] == "1706.03762v5"
        # A dry run folds in no arXiv record: the ledger is byte-identical.
        assert sidecar_path(existing).read_bytes() == before
        assert not any(r.type == "arxiv" for r in read_provenance(sidecar_path(existing)).records)

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


class TestSearch:
    def test_query_is_required(self):
        with pytest.raises(SystemExit):
            run(["arxiv-search", "--target", "x"])

    def test_results_are_listed(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeSearchClient(
            works=[_work("1706.03762", title="First"),
                   _work("1810.04805", title="Second")],
            total=42))
        assert run(["arxiv-search", "--query", "attention", "--target", str(kb)]) == 0
        out = capsys.readouterr().out
        assert "Found 42 results, showing top 2:" in out
        assert '1. 1706.03762v5 "First"' in out
        assert '2. 1810.04805v5 "Second"' in out

    def test_nothing_is_imported(self, tmp_path, fake):
        # arxiv-search is non-interactive and writes no files (#80). Import is #81.
        kb = _kb(tmp_path)
        fake(FakeSearchClient())
        assert run(["arxiv-search", "--query", "q", "--target", str(kb)]) == 0
        assert sources(kb) == []

    def test_filters_are_forwarded(self, tmp_path, fake):
        kb = _kb(tmp_path)
        client = fake(FakeSearchClient())
        run(["arxiv-search", "--query", "q", "--category", "cs.CL",
             "--category", "cs.LG", "--year", "2020-2025", "--limit", "5",
             "--sort", "submitted", "--target", str(kb)])
        assert client.calls == [{
            "query": "q", "categories": ("cs.CL", "cs.LG"), "year": "2020-2025",
            "limit": 5, "sort": "submitted", "start": 0,
        }]

    def test_zero_results_is_not_an_error(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeSearchClient(works=[], total=0))
        assert run(["arxiv-search", "--query", "nomatchesatall", "--target", str(kb)]) == 0
        assert "Found 0 results." in capsys.readouterr().out

    def test_zero_results_porcelain_reports_found_zero(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeSearchClient(works=[], total=0))
        assert run(["arxiv-search", "--query", "nomatch", "--target", str(kb),
                    "--porcelain"]) == 0
        assert "found\t0" in capsys.readouterr().out

    def test_typo_category_is_rejected_before_any_request(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        monkeypatch.setattr(
            cli, "_make_arxiv_client",
            lambda config: pytest.fail("must not build a client for a bad category"),
        )
        assert run(["arxiv-search", "--query", "q", "--category", "cs.NOTAREAL",
                    "--target", str(kb)]) == 1
        assert "unknown arXiv category" in capsys.readouterr().err

    def test_unknown_query_field_is_rejected_before_any_request(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        monkeypatch.setattr(
            cli, "_make_arxiv_client",
            lambda config: pytest.fail("must not build a client for a bad query field"),
        )
        assert run(["arxiv-search", "--query", "bogusfield:x", "--target", str(kb)]) == 1
        assert "unknown arXiv search field" in capsys.readouterr().err

    def test_limit_over_200_is_refused(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        monkeypatch.setattr(
            cli, "_make_arxiv_client",
            lambda config: pytest.fail("must not build a client for an out-of-range limit"),
        )
        assert run(["arxiv-search", "--query", "q", "--limit", "201",
                    "--target", str(kb)]) == 1
        assert "--limit must be between 1 and 200" in capsys.readouterr().err

    def test_limit_200_is_accepted(self, tmp_path, fake):
        kb = _kb(tmp_path)
        client = fake(FakeSearchClient())
        assert run(["arxiv-search", "--query", "q", "--limit", "200",
                    "--target", str(kb)]) == 0
        assert client.calls[0]["limit"] == 200

    def test_reversed_year_range_is_rejected_before_any_request(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        monkeypatch.setattr(
            cli, "_make_arxiv_client",
            lambda config: pytest.fail("must not build a client for a reversed year range"),
        )
        assert run(["arxiv-search", "--query", "q", "--year", "2025-2020",
                    "--target", str(kb)]) == 1
        assert "runs backwards" in capsys.readouterr().err

    def test_out_of_range_year_is_rejected(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path)
        monkeypatch.setattr(
            cli, "_make_arxiv_client",
            lambda config: pytest.fail("must not build a client for an out-of-range year"),
        )
        assert run(["arxiv-search", "--query", "q", "--year", "2099",
                    "--target", str(kb)]) == 1
        assert "outside arXiv's range" in capsys.readouterr().err

    def test_bogus_sort_is_rejected(self):
        # argparse constrains --sort to the three known values.
        with pytest.raises(SystemExit):
            run(["arxiv-search", "--query", "q", "--sort", "citations", "--target", "x"])

    def test_connection_failure_exits_two(self, tmp_path, fake, capsys):
        fake(FakeSearchClient(raise_exc=ArxivConnectionError("cannot reach arXiv")))
        assert run(["arxiv-search", "--query", "q",
                    "--target", str(_kb(tmp_path))]) == 2
        assert "cannot reach arXiv" in capsys.readouterr().err

    def test_response_failure_exits_one(self, tmp_path, fake, capsys):
        fake(FakeSearchClient(raise_exc=ArxivResponseError("truncated feed")))
        assert run(["arxiv-search", "--query", "q",
                    "--target", str(_kb(tmp_path))]) == 1
        assert "truncated feed" in capsys.readouterr().err


class TestSearchWithdrawal:
    def test_withdrawn_result_is_flagged_in_the_listing_naming_the_agent(
            self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeSearchClient(works=[_work("1904.09773", withdrawn_by="admin")]))
        run(["arxiv-search", "--query", "q", "--target", str(kb)])
        captured = capsys.readouterr()
        assert "WITHDRAWN (by arXiv administrators)" in captured.out
        # arXiv has no retraction process; the word "retracted" must never appear
        # ("retraction" does, only to say the two are not the same).
        assert "retracted" not in captured.out.lower()

    def test_author_withdrawal_names_the_author(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeSearchClient(works=[_work("1301.4231", withdrawn_by="author")]))
        run(["arxiv-search", "--query", "q", "--target", str(kb)])
        assert "WITHDRAWN (by the author)" in capsys.readouterr().out

    def test_live_result_is_not_flagged(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeSearchClient(works=[_work("1706.03762")]))
        run(["arxiv-search", "--query", "q", "--target", str(kb)])
        assert "withdrawn" not in capsys.readouterr().out.lower()

    def test_porcelain_flag_is_on_stdout_and_agent_naming_on_stderr(
            self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeSearchClient(works=[_work("1904.09773", withdrawn_by="admin")]))
        run(["arxiv-search", "--query", "q", "--target", str(kb), "--porcelain"])
        captured = capsys.readouterr()
        assert "result\t1\t1904.09773v5\twithdrawn\t" in captured.out
        # The prose warning naming the agent stays off the machine contract.
        assert "withdrawn (by" not in captured.out
        assert "withdrawn (by arXiv administrators)" in captured.err
        assert "retracted" not in captured.err.lower()


class TestSearchPorcelain:
    def test_porcelain_line_shape_matches_openalex_search(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeSearchClient(
            works=[_work("1706.03762", version=5, title="A paper")], total=9))
        assert run(["arxiv-search", "--query", "q", "--target", str(kb),
                    "--porcelain"]) == 0
        out = capsys.readouterr().out
        assert "result\t1\t1706.03762v5\t-\tA paper" in out
        assert "found\t9" in out

    def test_searching_banner_is_suppressed_in_porcelain(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeSearchClient())
        run(["arxiv-search", "--query", "q", "--target", str(kb), "--porcelain"])
        assert "Searching arXiv" not in capsys.readouterr().out


class TestSearchDryRun:
    """`--dry-run` was registered on every arXiv subcommand and read by none of
    them for search: it spent a real request and printed results. It now shows the
    query that would be sent, and sends nothing.

    The string comes from the same composer the client uses, so what an operator
    is shown cannot drift from what a real run sends."""

    def test_dry_run_sends_no_request(self, tmp_path, fake, capsys):
        client = fake(FakeClient())
        assert run(["arxiv-search", "--query", "transformers", "--target", str(_kb(tmp_path)),
                    "--dry-run"]) == 0
        assert client.calls == [], "--dry-run reached the API"

    def test_dry_run_shows_the_query_that_would_be_sent(self, tmp_path, fake, capsys):
        fake(FakeClient())
        run(["arxiv-search", "--query", "transformers", "--category", "cs.CL",
             "--year", "2023", "--target", str(_kb(tmp_path)), "--dry-run"])
        out = capsys.readouterr().out
        assert "cat:cs.CL" in out
        assert "submittedDate:[202301010000 TO 202312312359]" in out

    def test_the_shown_query_is_the_composer_the_client_uses(self, tmp_path, fake, capsys):
        from factlog.integrations.arxiv.config import compose_search_query

        fake(FakeClient())
        run(["arxiv-search", "--query", "transformers", "--category", "cs.CL",
             "--target", str(_kb(tmp_path)), "--dry-run", "--porcelain"])
        shown = capsys.readouterr().out.strip().split("\t", 1)[1]
        assert shown == compose_search_query("transformers", ["cs.CL"], None)

    def test_dry_run_still_refuses_a_typo_before_composing(self, tmp_path, fake, capsys):
        client = fake(FakeClient())
        assert run(["arxiv-search", "--query", "x", "--category", "cs.NOPE",
                    "--target", str(_kb(tmp_path)), "--dry-run"]) == 1
        assert client.calls == []
        assert "unknown arXiv category" in capsys.readouterr().err

    def test_dry_run_porcelain_is_one_tab_separated_line(self, tmp_path, fake, capsys):
        fake(FakeClient())
        run(["arxiv-search", "--query", "transformers", "--target", str(_kb(tmp_path)),
             "--dry-run", "--porcelain"])
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 1
        assert lines[0].startswith("query\t")


class TestThePhraseRewriteIsAnnounced:
    """We quote a bare multi-word query so arXiv searches it as a phrase (#89).
    Silently rewriting what the operator typed is the same disservice as silently
    mis-searching it, so the rewrite is announced — on stderr, so `--porcelain`
    stdout stays parseable."""

    def test_the_rewrite_is_announced_on_stderr(self, tmp_path, fake, capsys):
        fake(FakeSearchClient())
        run(["arxiv-search", "--query", "chain of thought", "--target", str(_kb(tmp_path))])
        captured = capsys.readouterr()
        assert 'all:"chain of thought"' in captured.err
        assert "not searched as a phrase" in captured.err

    def test_porcelain_stdout_is_not_polluted(self, tmp_path, fake, capsys):
        fake(FakeSearchClient())
        run(["arxiv-search", "--query", "chain of thought",
             "--target", str(_kb(tmp_path)), "--porcelain"])
        captured = capsys.readouterr()
        assert "not searched as a phrase" in captured.err
        for line in captured.out.strip().splitlines():
            assert line.startswith(("result\t", "found\t")), line

    def test_no_announcement_when_nothing_was_rewritten(self, tmp_path, fake, capsys):
        fake(FakeSearchClient())
        run(["arxiv-search", "--query", 'ti:"chain of thought"',
             "--target", str(_kb(tmp_path))])
        assert "not searched as a phrase" not in capsys.readouterr().err

    def test_the_client_receives_the_raw_query_and_composes_the_phrase(self, tmp_path, fake):
        from factlog.integrations.arxiv.config import compose_search_query

        client = fake(FakeSearchClient())
        run(["arxiv-search", "--query", "chain of thought", "--target", str(_kb(tmp_path))])
        # The CLI hands the raw query to the client; `compose_search_query` — the
        # one place that builds `search_query` — is what applies the phrase form.
        assert client.calls[0]["query"] == "chain of thought"
        assert compose_search_query("chain of thought") == 'all:"chain of thought"' 

    def test_dry_run_shows_the_quoted_form(self, tmp_path, fake, capsys):
        fake(FakeSearchClient())
        run(["arxiv-search", "--query", "chain of thought",
             "--target", str(_kb(tmp_path)), "--dry-run", "--porcelain"])
        assert 'all:"chain of thought"' in capsys.readouterr().out
