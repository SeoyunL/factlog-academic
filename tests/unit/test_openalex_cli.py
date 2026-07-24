# SPDX-License-Identifier: Apache-2.0
"""CLI tests for `factlog openalex-search|import|cite` (#51, spec §5.2/§5.3).

The real OpenAlex client is replaced via ``_make_openalex_client`` so the
commands run without the network. A temp KB (with sources/) is the target.
"""
from __future__ import annotations

import pytest

from factlog import cli
from factlog.integrations.openalex.api_client import (
    OpenAlexConnectionError,
    OpenAlexNotFoundError,
    RateLimit,
    SearchPage,
)


def _kb(tmp_path):
    (tmp_path / "sources").mkdir()
    return tmp_path


def _raw(work_id="W1", title="A paper", year=2023, doi=None, retracted=False, cited=7):
    return {
        "id": f"https://openalex.org/{work_id}",
        "title": title,
        "publication_year": year,
        "doi": f"https://doi.org/{doi}" if doi else None,
        "is_retracted": retracted,
        "cited_by_count": cited,
        "authorships": [{"author_position": "first", "author": {"display_name": "Ann Author"}}],
    }


class FakeClient:
    """Records calls; replays canned pages. Mirrors OpenAlexClient's surface."""

    def __init__(self, results=None, count=None, raise_exc=None, remaining=900):
        self._results = results if results is not None else [_raw()]
        self._count = len(self._results) if count is None else count
        self._raise = raise_exc
        self.rate_limit = RateLimit(limit=1000, remaining=remaining, cost=10, reset_seconds=3600)
        self.calls: list[tuple] = []

    def _page(self):
        if self._raise is not None:
            raise self._raise
        return SearchPage(results=list(self._results), count=self._count)

    def search_works(self, query, *, year=None, work_type=None, limit=None, **kw):
        self.calls.append(("search", query, year, work_type, limit))
        return self._page()

    def get_work(self, work_id):
        self.calls.append(("get_work", work_id))
        if self._raise is not None:
            raise self._raise
        return self._results[0]

    def get_work_by_doi(self, doi):
        self.calls.append(("get_doi", doi))
        if self._raise is not None:
            raise self._raise
        return self._results[0]

    def citing_works(self, work_id, *, limit=None):
        self.calls.append(("citing", work_id, limit))
        return self._page()

    def cited_works(self, work_id, *, limit=None):
        self.calls.append(("cited", work_id, limit))
        return self._page()


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def install(client):
        holder["client"] = client
        monkeypatch.setattr(cli, "_make_openalex_client", lambda config: client)
        return client

    return install


def run(argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


def sources(kb):
    return sorted(p.name for p in (kb / "sources").glob("*.md"))


# -- openalex-import -------------------------------------------------------
class TestImport:
    def test_import_by_work_id_writes_a_source(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        client = fake(FakeClient())
        assert run(["openalex-import", "--work-id", "W1", "--target", str(kb)]) == 0
        assert client.calls == [("get_work", "W1")]
        assert len(sources(kb)) == 1
        assert "Imported: 1" in capsys.readouterr().out

    def test_import_by_doi(self, tmp_path, fake):
        kb = _kb(tmp_path)
        client = fake(FakeClient())
        assert run(["openalex-import", "--doi", "10.1/x", "--target", str(kb)]) == 0
        assert client.calls == [("get_doi", "10.1/x")]

    def test_work_id_and_doi_are_mutually_exclusive(self, tmp_path):
        with pytest.raises(SystemExit):
            run(["openalex-import", "--work-id", "W1", "--doi", "10.1/x"])

    def test_one_selector_is_required(self, tmp_path):
        with pytest.raises(SystemExit):
            run(["openalex-import", "--target", str(tmp_path)])

    def test_dry_run_writes_nothing(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient())
        assert run(["openalex-import", "--work-id", "W1", "--target", str(kb), "--dry-run"]) == 0
        assert sources(kb) == []
        assert "Would import: 1" in capsys.readouterr().out

    def test_reimport_is_skipped(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient())
        run(["openalex-import", "--work-id", "W1", "--target", str(kb)])
        assert run(["openalex-import", "--work-id", "W1", "--target", str(kb)]) == 0
        assert "Skipped:  1" in capsys.readouterr().out
        assert len(sources(kb)) == 1

    def test_not_found_exits_one(self, tmp_path, fake, capsys):
        fake(FakeClient(raise_exc=OpenAlexNotFoundError("no record")))
        assert run(["openalex-import", "--work-id", "W1", "--target", str(_kb(tmp_path))]) == 1
        assert "no record" in capsys.readouterr().err

    def test_connection_failure_exits_two(self, tmp_path, fake, capsys):
        fake(FakeClient(raise_exc=OpenAlexConnectionError("cannot reach")))
        assert run(["openalex-import", "--work-id", "W1", "--target", str(_kb(tmp_path))]) == 2
        assert "cannot reach" in capsys.readouterr().err

    def test_invalid_work_id_is_reported_and_writes_nothing(self, tmp_path, monkeypatch, capsys):
        # The W0 coercion trap. Uses the *real* client (with a transport that
        # must never be called) so the rejection is proven, not stubbed.
        from factlog.integrations.openalex.api_client import OpenAlexClient

        kb = _kb(tmp_path)
        transport = lambda path, params: pytest.fail(f"must not request {path}")  # noqa: E731
        monkeypatch.setattr(
            cli, "_make_openalex_client", lambda config: OpenAlexClient(config, transport=transport)
        )
        assert run(["openalex-import", "--work-id", "W000000000000", "--target", str(kb)]) == 1
        assert "invalid OpenAlex work id" in capsys.readouterr().err
        assert sources(kb) == []

    def test_porcelain_contract(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient())
        assert run(["openalex-import", "--work-id", "W1", "--target", str(kb), "--porcelain"]) == 0
        rows = dict(
            line.split("\t", 1) for line in capsys.readouterr().out.strip().splitlines()
        )
        assert rows["imported"] == "1"
        assert rows["skipped"] == "0"
        assert rows["errors"] == "0"
        assert rows["dry_run"] == "0"
        assert rows["target"].endswith("sources")


# -- openalex-search -------------------------------------------------------
class TestSearch:
    def test_query_is_required(self):
        with pytest.raises(SystemExit):
            run(["openalex-search"])

    def test_filters_are_forwarded(self, tmp_path, fake):
        kb = _kb(tmp_path)
        client = fake(FakeClient())
        run(["openalex-search", "--query", "q", "--year", "2020-2025", "--type", "article",
             "--limit", "5", "--target", str(kb), "--dry-run"])
        assert client.calls == [("search", "q", "2020-2025", "article", 5)]

    def test_results_are_listed(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(results=[_raw("W1", "First"), _raw("W2", "Second")], count=42))
        run(["openalex-search", "--query", "q", "--target", str(kb), "--dry-run"])
        out = capsys.readouterr().out
        assert "Found 42 results, showing top 2:" in out
        assert '1. W1 "First"' in out and '2. W2 "Second"' in out

    def test_retracted_results_are_flagged_as_unverified(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(results=[_raw("W1", "Bad", retracted=True)]))
        run(["openalex-search", "--query", "q", "--target", str(kb), "--dry-run"])
        out = capsys.readouterr().out
        assert "RETRACTED" in out and "unverified" in out

    def test_without_a_tty_nothing_is_imported(self, tmp_path, fake, capsys):
        # A non-interactive search must not guess; --all is the explicit opt-in.
        kb = _kb(tmp_path)
        fake(FakeClient())
        assert run(["openalex-search", "--query", "q", "--target", str(kb)]) == 0
        assert sources(kb) == []
        assert "Nothing selected" in capsys.readouterr().out

    def test_all_imports_every_result(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(results=[_raw("W1", "A"), _raw("W2", "B")]))
        assert run(["openalex-search", "--query", "q", "--target", str(kb), "--all"]) == 0
        assert len(sources(kb)) == 2
        assert "Imported: 2" in capsys.readouterr().out

    def test_dry_run_with_all_writes_nothing(self, tmp_path, fake):
        kb = _kb(tmp_path)
        fake(FakeClient())
        assert run(["openalex-search", "--query", "q", "--target", str(kb),
                    "--all", "--dry-run"]) == 0
        assert sources(kb) == []

    def test_interactive_selection_imports_chosen_numbers(self, tmp_path, fake, monkeypatch):
        kb = _kb(tmp_path)
        fake(FakeClient(results=[_raw("W1", "A"), _raw("W2", "B"), _raw("W3", "C")]))
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: "1,3")
        assert run(["openalex-search", "--query", "q", "--target", str(kb)]) == 0
        assert len(sources(kb)) == 2

    def test_interactive_none_writes_nothing(self, tmp_path, fake, monkeypatch):
        kb = _kb(tmp_path)
        fake(FakeClient())
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: "none")
        assert run(["openalex-search", "--query", "q", "--target", str(kb)]) == 0
        assert sources(kb) == []

    def test_interactive_all_imports_everything(self, tmp_path, fake, monkeypatch):
        kb = _kb(tmp_path)
        fake(FakeClient(results=[_raw("W1"), _raw("W2")]))
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: "all")
        run(["openalex-search", "--query", "q", "--target", str(kb)])
        assert len(sources(kb)) == 2

    def test_out_of_range_selection_is_ignored(self, tmp_path, fake, monkeypatch, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(results=[_raw("W1")]))
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: "1,9,abc")
        run(["openalex-search", "--query", "q", "--target", str(kb)])
        assert len(sources(kb)) == 1
        err = capsys.readouterr().err
        assert "ignoring '9'" in err and "ignoring 'abc'" in err

    def test_duplicate_selection_imports_once(self, tmp_path, fake, monkeypatch):
        kb = _kb(tmp_path)
        fake(FakeClient(results=[_raw("W1")]))
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: "1,1")
        run(["openalex-search", "--query", "q", "--target", str(kb)])
        assert len(sources(kb)) == 1

    def test_limit_is_validated_before_any_request(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        client = fake(FakeClient())
        assert run(["openalex-search", "--query", "q", "--limit", "999", "--target", str(kb)]) == 1
        assert client.calls == []
        assert "--limit must be between 1 and 200" in capsys.readouterr().err

    def test_porcelain_lists_results_and_never_prompts(self, tmp_path, fake, monkeypatch, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(results=[_raw("W1", "A", retracted=True)], count=9))
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("must not prompt"))
        assert run(["openalex-search", "--query", "q", "--target", str(kb), "--porcelain"]) == 0
        out = capsys.readouterr().out
        assert "result\t1\tW1\tretracted\tA" in out
        assert "found\t9" in out
        assert sources(kb) == []

    def test_connection_failure_exits_two(self, tmp_path, fake):
        fake(FakeClient(raise_exc=OpenAlexConnectionError("down")))
        assert run(["openalex-search", "--query", "q", "--target", str(_kb(tmp_path))]) == 2


# -- the shared selector ---------------------------------------------------
class TestSharedSelector:
    """`_select_search_results` is shared with arxiv-search (#81). These pins fail
    if the prompt text or the openalex-search "ignoring" prefix ever drift — the
    exact drift that let two literals disagree in #64.
    """

    _PROMPT = "\nImport which? (comma-separated numbers, or 'all', or 'none')\n> "

    def test_prompt_text_is_exact(self, monkeypatch):
        seen = {}
        monkeypatch.setattr("builtins.input", lambda prompt="": seen.setdefault("p", prompt) or "none")
        cli._select_search_results(["a", "b"], interactive=True, command="openalex-search")
        assert seen["p"] == self._PROMPT

    def test_openalex_ignoring_prefix_is_exact(self, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", lambda *_: "9")
        cli._select_search_results(["a"], interactive=True, command="openalex-search")
        assert "factlog openalex-search: ignoring '9'" in capsys.readouterr().err

    def test_command_name_selects_the_prefix(self, monkeypatch, capsys):
        # The same selector, a different command, names that command instead.
        monkeypatch.setattr("builtins.input", lambda *_: "9")
        cli._select_search_results(["a"], interactive=True, command="arxiv-search")
        assert "factlog arxiv-search: ignoring '9'" in capsys.readouterr().err

    def test_non_interactive_never_prompts(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("must not prompt"))
        assert cli._select_search_results(["a"], interactive=False, command="openalex-search") == []


# -- openalex-cite ---------------------------------------------------------
class TestCite:
    def _seeded(self, tmp_path, fake):
        kb = _kb(tmp_path)
        fake(FakeClient())
        run(["openalex-import", "--work-id", "W1", "--target", str(kb)])
        return kb, sources(kb)[0]

    def test_citing_direction_uses_the_filter_endpoint(self, tmp_path, fake):
        kb, slug = self._seeded(tmp_path, fake)
        client = fake(FakeClient(results=[_raw("W9", "Citer")]))
        assert run(["openalex-cite", "--for", slug, "--target", str(kb)]) == 0
        assert client.calls == [("citing", "W1", None)]

    def test_both_directions_query_each_once(self, tmp_path, fake, capsys):
        kb, slug = self._seeded(tmp_path, fake)
        client = fake(FakeClient(results=[_raw("W9")]))
        run(["openalex-cite", "--for", slug, "--direction", "both", "--target", str(kb)])
        assert [c[0] for c in client.calls] == ["citing", "cited"]
        out = capsys.readouterr().out
        assert "Works that cite it" in out and "Works that it cites" in out

    def test_slug_accepts_a_bare_stem(self, tmp_path, fake):
        kb, slug = self._seeded(tmp_path, fake)
        client = fake(FakeClient())
        assert run(["openalex-cite", "--for", slug[:-3], "--target", str(kb)]) == 0
        assert client.calls[0] == ("citing", "W1", None)

    def test_nothing_is_written_without_auto_import(self, tmp_path, fake, capsys):
        kb, slug = self._seeded(tmp_path, fake)
        before = sources(kb)
        fake(FakeClient(results=[_raw("W9")]))
        run(["openalex-cite", "--for", slug, "--target", str(kb)])
        assert sources(kb) == before
        assert "Re-run with --auto-import" in capsys.readouterr().out

    def test_auto_import_writes_the_neighbourhood(self, tmp_path, fake):
        kb, slug = self._seeded(tmp_path, fake)
        fake(FakeClient(results=[_raw("W9", "Citer")]))
        assert run(["openalex-cite", "--for", slug, "--target", str(kb), "--auto-import"]) == 0
        assert len(sources(kb)) == 2

    def test_both_directions_import_each_work_once(self, tmp_path, fake):
        kb, slug = self._seeded(tmp_path, fake)
        fake(FakeClient(results=[_raw("W9", "Shared")]))
        run(["openalex-cite", "--for", slug, "--direction", "both", "--target", str(kb),
             "--auto-import"])
        assert len(sources(kb)) == 2  # the seed plus W9, not W9 twice

    def test_unknown_slug_is_an_error(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient())
        assert run(["openalex-cite", "--for", "nope", "--target", str(kb)]) == 1
        assert "no source nope.md" in capsys.readouterr().err

    def test_source_without_openalex_id_is_an_error(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient())
        (kb / "sources" / "plain.md").write_text("# just a source\n", encoding="utf-8")
        assert run(["openalex-cite", "--for", "plain", "--target", str(kb)]) == 1
        assert "records no openalex_id" in capsys.readouterr().err

    def test_porcelain_rows_are_direction_scoped(self, tmp_path, fake, capsys):
        kb, slug = self._seeded(tmp_path, fake)
        fake(FakeClient(results=[_raw("W9", "X")], count=3))
        run(["openalex-cite", "--for", slug, "--direction", "both", "--target", str(kb),
             "--porcelain"])
        out = capsys.readouterr().out
        assert "result\tciting\t1\tW9\t-\tX" in out
        assert "found\tciting\t3" in out
        assert "found\tcited\t3" in out


# -- credit budget ---------------------------------------------------------
class TestPlaceholderTitleWarning:
    """#54: W2567289819's title is the literal string "null". Warn, never drop."""

    def test_null_titled_work_is_imported_and_warned_about(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(results=[_raw("W1", title="null")]))
        assert run(["openalex-import", "--work-id", "W1", "--target", str(kb)]) == 0
        assert len(sources(kb)) == 1  # imported, not rejected
        assert 'has the literal title "null"' in capsys.readouterr().err

    def test_a_real_title_produces_no_warning(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(results=[_raw("W1", title="On Null Hypotheses")]))
        run(["openalex-import", "--work-id", "W1", "--target", str(kb)])
        assert "literal title" not in capsys.readouterr().err

    def test_warning_never_pollutes_the_porcelain_contract(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(results=[_raw("W1", title="null")]))
        run(["openalex-import", "--work-id", "W1", "--target", str(kb), "--porcelain"])
        captured = capsys.readouterr()
        assert "literal title" in captured.err
        assert "literal title" not in captured.out

    def test_skipped_work_is_not_warned_about(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(results=[_raw("W1", title="null")]))
        run(["openalex-import", "--work-id", "W1", "--target", str(kb)])
        capsys.readouterr()
        run(["openalex-import", "--work-id", "W1", "--target", str(kb)])
        assert "literal title" not in capsys.readouterr().err


class TestUnknownWorkType:
    def test_bogus_type_is_rejected_before_the_search_is_charged(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        client = fake(FakeClient())
        assert run(["openalex-search", "--query", "q", "--type", "artikle",
                    "--target", str(kb)]) == 1
        assert client.calls == []
        assert "unknown work type" in capsys.readouterr().err

    def test_known_type_is_forwarded(self, tmp_path, fake):
        kb = _kb(tmp_path)
        client = fake(FakeClient())
        run(["openalex-search", "--query", "q", "--type", "review",
             "--target", str(kb), "--dry-run"])
        assert client.calls == [("search", "q", None, "review", None)]


class TestBudgetWarning:
    def test_low_budget_warns_on_stderr_without_failing(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(remaining=5))
        assert run(["openalex-search", "--query", "q", "--target", str(kb), "--all"]) == 0
        captured = capsys.readouterr()
        assert "daily credit budget is nearly spent: 5 left" in captured.err
        assert len(sources(kb)) == 1  # warned, not blocked

    def test_healthy_budget_is_silent(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(remaining=900))
        run(["openalex-search", "--query", "q", "--target", str(kb), "--all"])
        assert "credit budget" not in capsys.readouterr().err

    def test_unknown_budget_is_silent(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        client = FakeClient()
        client.rate_limit = RateLimit()  # no headers seen
        fake(client)
        run(["openalex-search", "--query", "q", "--target", str(kb), "--all"])
        assert "credit budget" not in capsys.readouterr().err

    def test_warning_is_emitted_in_porcelain_mode_too(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(remaining=1))
        run(["openalex-search", "--query", "q", "--target", str(kb), "--all", "--porcelain"])
        captured = capsys.readouterr()
        assert "credit budget" in captured.err
        assert "credit budget" not in captured.out  # never pollutes the machine contract


class _BufferedStdout:
    """Models a buffered stdout: writes accrue and only surface on flush().

    capsys snapshots each stream separately, so it cannot see the interleave a
    redirect (2>&1) shows. A shared event log, committed to on flush, reproduces
    the real terminal ordering. Mirrors #457's fake in test_zotero_cli.py.
    """

    def __init__(self, events):
        self._events = events
        self._pending = []

    def write(self, s):
        self._pending.append(s)
        return len(s)

    def flush(self):
        if self._pending:
            self._events.append("".join(self._pending))
            self._pending = []


class _UnbufferedStderr:
    """Models unbuffered stderr: every write surfaces immediately."""

    def __init__(self, events):
        self._events = events

    def write(self, s):
        self._events.append(s)
        return len(s)

    def flush(self):
        pass


class TestSearchNarrationOrdering:
    """The #457 ordering fix, generalized to openalex-search by #472's _narrate."""

    def test_searching_line_precedes_connection_error_under_redirect(
        self, tmp_path, fake, monkeypatch
    ):
        # Same latent bug as #457, other command: the "Searching OpenAlex" line goes
        # to buffered stdout and the connection error to unbuffered stderr, so under
        # a 2>&1 redirect an unflushed narration lands *after* the error it precedes.
        # _narrate flushes at the shared seam, so it must surface first.
        import sys

        kb = _kb(tmp_path)
        fake(FakeClient(raise_exc=OpenAlexConnectionError("cannot reach OpenAlex")))
        events: list[str] = []
        monkeypatch.setattr(sys, "stdout", _BufferedStdout(events))
        monkeypatch.setattr(sys, "stderr", _UnbufferedStderr(events))

        rc = run(["openalex-search", "--query", "q", "--target", str(kb)])
        # The interpreter flushes stdout at exit, so unflushed narration is not lost
        # — it surfaces last. Mimic that so an unfixed narration fails as the real
        # symptom (progress after the error) rather than as absence.
        sys.stdout.flush()

        merged = "".join(events)
        assert rc == 2
        assert "Searching OpenAlex" in merged
        assert "cannot reach OpenAlex" in merged
        assert merged.index("Searching OpenAlex") < merged.index("cannot reach OpenAlex")
