# SPDX-License-Identifier: Apache-2.0
"""`factlog arxiv-acknowledge-withdrawal` — the human gate that stops the repeat (#100).

The real arXiv client is replaced via ``_make_arxiv_client`` so the command runs
without the network. A temp KB carries source ``.md`` originals and their provenance
ledgers.

Two things are proven here, with the real CLI (never a hand-rolled assertion of the
comparison), by patching only the upstream fetch:

1. The **presence -> value** comparison in ``check_versions._diff`` re-surfaces three
   silences measured on main: (a) ``author -> admin``, (b) ``author -> None`` (an
   un-withdrawal), and (c) a hand-typed garbage front-matter value.
2. The command live-queries arXiv, refuses without a terminal/`--yes`, rejects a
   version-pinned id, writes nothing on a failed query or a missing entry, silences the
   repeat once run, clears the identifying field on an un-withdrawal so the paper is
   re-importable, and never opens the ``.md`` (byte- and ``mtime_ns``-identical).
3. `--yes` may **set** ``withdrawn_by`` but never **clear** it (#106): a clear is only
   reachable after a human sees the note and confirms at the prompt.
"""
from __future__ import annotations

from datetime import date

import pytest

from factlog import cli
from factlog.integrations.arxiv.client import (
    ArxivConnectionError,
    ArxivServiceError,
    BatchResult,
)
from factlog.integrations.arxiv.id_normalizer import ArxivId
from factlog.integrations.arxiv.importer import import_works
from factlog.integrations.arxiv.work_parser import ParsedArxivWork
from factlog.integrations.common.provenance import (
    Provenance,
    SourceRecord,
    read_provenance,
    sidecar_path,
    write_provenance,
)


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


def _seed(kb, arxiv_id, version, *, withdrawn_by=None, name=None):
    """Write a source ``.md`` and its arXiv provenance ledger. Returns the .md path."""
    (kb / "sources").mkdir(exist_ok=True)
    name = name or arxiv_id.replace("/", "_")
    md = kb / "sources" / f"{name}.md"
    md.write_text(f"---\narxiv_id: {arxiv_id}\narxiv_version: {version}\n---\n# {name}\n")
    fields = {"version": version}
    if withdrawn_by is not None:
        fields["withdrawn_by"] = withdrawn_by
    record = SourceRecord(
        type="arxiv", id=arxiv_id, imported_at="2026-01-01T00:00:00+00:00", fields=fields
    )
    write_provenance(sidecar_path(md, kb), Provenance(records=[record]))
    return md


def _seed_front_matter_only(kb, arxiv_id, version, *, withdrawn_by, name=None):
    """A paper imported before #82: front matter carries the withdrawal, no ledger."""
    (kb / "sources").mkdir(exist_ok=True)
    name = name or arxiv_id.replace("/", "_")
    md = kb / "sources" / f"{name}.md"
    md.write_text(
        f"---\narxiv_id: {arxiv_id}\narxiv_version: {version}\n"
        f"arxiv_withdrawn: true\narxiv_withdrawn_by: {withdrawn_by}\n---\n# {name}\n"
    )
    return md


class FakeClient:
    """Maps base id -> work; returns a BatchResult (found order reversed to prove the
    code never keys on response position). A requested id with no work is `missing`."""

    def __init__(self, works, *, raise_exc=None):
        self._works = {w.arxiv_id: w for w in works}
        self._raise = raise_exc
        self.calls: list[list[str]] = []

    def fetch_works(self, ids):
        self.calls.append([str(i) for i in ids])
        if self._raise is not None:
            raise self._raise
        found, missing = [], []
        for value in ids:
            base = str(value)
            work = self._works.get(base)
            if work is None:
                missing.append(ArxivId(base))
            else:
                found.append(work)
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


def _confirm(monkeypatch, answer):
    """Put the command on an interactive terminal and answer its prompt with `answer`.

    Returns a list of the prompts `input()` was actually called with. A caller that
    asserts only on the outcome cannot tell a consulted human from a deleted
    `if not assume_yes:` block, so every test that reaches the write through this
    helper asserts the prompt was consulted exactly once (P1/P5).
    """
    prompts: list[str] = []

    def _input(prompt="", *a, **k):
        prompts.append(prompt)
        return answer

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", _input)
    return prompts


def _md_snapshot(kb):
    """Bytes and mtime_ns for every source .md — what P4 must hold identical."""
    snap = {}
    root = kb / "sources"
    if root.is_dir():
        for path in root.rglob("*.md"):
            st = path.stat()
            snap[path] = (path.read_bytes(), st.st_mtime_ns)
    return snap


def _ledger_withdrawn_by(kb, arxiv_id):
    """The `withdrawn_by` the ledger currently records for `arxiv_id` (or None)."""
    for path in (kb / "source-provenance").rglob("*.json"):
        prov = read_provenance(path)
        for record in prov.records:
            if record.type == "arxiv" and record.id == arxiv_id:
                return record.fields.get("withdrawn_by")
    return None


# --------------------------------------------------------------------------- #
# (1) the presence -> value comparison closes three measured silences
# --------------------------------------------------------------------------- #
class TestResurfacing:
    def test_author_to_admin_resurfaces(self, tmp_path, fake, capsys):
        # (a) A paper acknowledged as author-withdrawn that arXiv administrators
        # later withdraw. A presence test ("admin" is also not None) silenced it.
        _seed(tmp_path, "1904.09773", 1, withdrawn_by="author")
        fake(FakeClient([_work("1904.09773", version=1, withdrawn_by="admin")]))
        code = run(["arxiv-check-versions", "--target", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "WITHDRAWN by arXiv administrators" in out
        assert "Newly withdrawn:     1" in out
        assert "retracted" not in out.lower()

    def test_un_withdrawal_resurfaces_and_is_not_a_withdrawal_warning(
        self, tmp_path, fake, capsys
    ):
        # (b) A paper coming back is news. The note is NOT a withdrawal warning: it
        # reports a withdrawal upstream no longer reports and asks the human to clear it.
        _seed(tmp_path, "1904.09773", 1, withdrawn_by="author")
        fake(FakeClient([_work("1904.09773", version=1, withdrawn_by=None)]))
        code = run(["arxiv-check-versions", "--target", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "No longer withdrawn" in out
        assert "No longer withdrawn: 1" in out
        assert "arxiv-acknowledge-withdrawal --id 1904.09773" in out
        # It must not masquerade as a fresh withdrawal.
        assert "WITHDRAWN by" not in out
        assert "Newly withdrawn:     0" in out
        assert "retracted" not in out.lower()

    def test_garbage_front_matter_value_resurfaces(self, tmp_path, fake, capsys):
        # (c) A hand-typed garbage value ("typo") suppressed a real withdrawal under a
        # presence test. Under a value comparison ("author" != "typo") it resurfaces.
        _seed_front_matter_only(tmp_path, "1904.09773", 1, withdrawn_by="typo")
        fake(FakeClient([_work("1904.09773", version=1, withdrawn_by="author")]))
        code = run(["arxiv-check-versions", "--target", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "WITHDRAWN by the author" in out
        assert "Newly withdrawn:     1" in out
        # The note names the recorded garbage rather than hiding it behind "arXiv".
        assert "'typo'" in out
        assert "retracted" not in out.lower()

    def test_matching_agent_does_not_resurface(self, tmp_path, fake, capsys):
        # The equal case still silences: "admin" recorded, "admin" upstream.
        _seed(tmp_path, "1904.09773", 1, withdrawn_by="admin")
        fake(FakeClient([_work("1904.09773", version=1, withdrawn_by="admin")]))
        run(["arxiv-check-versions", "--target", str(tmp_path)])
        out = capsys.readouterr().out
        assert "Newly withdrawn:     0" in out
        assert "No longer withdrawn: 0" in out


# --------------------------------------------------------------------------- #
# (2) the command: live query, gate, write, silence
# --------------------------------------------------------------------------- #
class TestAcknowledgeCommand:
    def test_records_live_withdrawal_and_silences_the_repeat(self, tmp_path, fake, capsys):
        # Not recorded as withdrawn; arXiv now reports it withdrawn by the author.
        _seed(tmp_path, "1706.03762", 7)
        fake(FakeClient([_work("1706.03762", version=7, withdrawn_by="author")]))

        # It surfaces on check-versions.
        run(["arxiv-check-versions", "--target", str(tmp_path)])
        assert "WITHDRAWN by the author" in capsys.readouterr().out

        # Acknowledge it (non-interactively, with --yes and an explicit --id).
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path), "--yes",
        ])
        out = capsys.readouterr().out
        assert code == 0
        assert "Recorded withdrawal by author" in out
        assert "retracted" not in out.lower()
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") == "author"

        # The repeat is silenced on the next check.
        run(["arxiv-check-versions", "--target", str(tmp_path)])
        out = capsys.readouterr().out
        assert "Newly withdrawn:     0" in out

    def test_prints_the_note_being_silenced_from_the_live_value(
        self, tmp_path, fake, capsys
    ):
        _seed(tmp_path, "1706.03762", 7)
        fake(FakeClient([_work("1706.03762", version=7, withdrawn_by="admin")]))
        run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path), "--yes",
        ])
        out = capsys.readouterr().out
        # The exact withdrawal note, naming arXiv's live agent, before the write.
        assert "arXiv now reports 1706.03762 (v7) as WITHDRAWN by arXiv administrators" in out

    def test_un_withdrawal_clears_the_field_and_silences(
        self, tmp_path, fake, monkeypatch, capsys
    ):
        # A clear is the silencing direction, so it only happens after a human sees the
        # note and confirms at the prompt (#106). `--yes` cannot reach this write.
        _seed(tmp_path, "1706.03762", 7, withdrawn_by="author")
        fake(FakeClient([_work("1706.03762", version=7, withdrawn_by=None)]))
        prompts = _confirm(monkeypatch, "y")
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path),
        ])
        out = capsys.readouterr().out
        assert code == 0
        assert len(prompts) == 1  # the human was asked, not assumed
        assert "Cleared the withdrawal" in out
        # The identifying field is gone from the ledger — not left as "author".
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") is None
        # And the un-withdrawal no longer surfaces.
        run(["arxiv-check-versions", "--target", str(tmp_path)])
        assert "No longer withdrawn: 0" in capsys.readouterr().out

    def test_failed_live_query_writes_nothing_and_exits_nonzero(
        self, tmp_path, fake
    ):
        _seed(tmp_path, "1706.03762", 7, withdrawn_by="author")
        before_md = _md_snapshot(tmp_path)
        before_ledger = _ledger_withdrawn_by(tmp_path, "1706.03762")
        fake(FakeClient([], raise_exc=ArxivConnectionError("cannot reach arXiv")))
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path), "--yes",
        ])
        assert code == 2  # a connection failure is a hard, retryable non-zero
        assert _md_snapshot(tmp_path) == before_md
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") == before_ledger == "author"

    def test_rate_limit_writes_nothing_and_exits_nonzero(self, tmp_path, fake):
        _seed(tmp_path, "1706.03762", 7, withdrawn_by="author")
        before = _ledger_withdrawn_by(tmp_path, "1706.03762")
        fake(FakeClient([], raise_exc=ArxivServiceError("HTTP 429")))
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path), "--yes",
        ])
        assert code == 1
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") == before == "author"

    def test_missing_entry_writes_nothing_and_exits_nonzero(self, tmp_path, fake, capsys):
        _seed(tmp_path, "1706.03762", 7, withdrawn_by="author")
        before = _ledger_withdrawn_by(tmp_path, "1706.03762")
        fake(FakeClient([]))  # arXiv returns nothing for the id
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path), "--yes",
        ])
        assert code == 1
        assert "no entry" in capsys.readouterr().err
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") == before == "author"

    def test_no_tty_without_yes_refuses_and_writes_nothing(self, tmp_path, fake, capsys):
        # Under pytest stdin is not a TTY, so this is the pipe/script case.
        _seed(tmp_path, "1706.03762", 7, withdrawn_by="author")
        before = _md_snapshot(tmp_path)
        client = fake(FakeClient([_work("1706.03762", version=7, withdrawn_by=None)]))
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path),
        ])
        assert code == 1
        assert "refusing to acknowledge without a terminal" in capsys.readouterr().err
        assert client.calls == []  # it refuses BEFORE hitting the API
        assert _md_snapshot(tmp_path) == before
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") == "author"

    def test_version_pinned_id_is_rejected(self, tmp_path, fake, capsys):
        _seed(tmp_path, "1706.03762", 7, withdrawn_by="author")
        client = fake(FakeClient([_work("1706.03762", version=7, withdrawn_by=None)]))
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762v5",
            "--target", str(tmp_path), "--yes",
        ])
        assert code == 1
        err = capsys.readouterr().err
        assert "identity is the base id" in err
        assert "1706.03762" in err
        assert client.calls == []  # rejected before any network call
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") == "author"

    def test_interactive_yes_writes(self, tmp_path, fake, monkeypatch, capsys):
        _seed(tmp_path, "1706.03762", 7)
        fake(FakeClient([_work("1706.03762", version=7, withdrawn_by="author")]))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path),
        ])
        assert code == 0
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") == "author"

    def test_interactive_no_aborts_and_writes_nothing(
        self, tmp_path, fake, monkeypatch, capsys
    ):
        _seed(tmp_path, "1706.03762", 7)
        fake(FakeClient([_work("1706.03762", version=7, withdrawn_by="author")]))
        before = _ledger_withdrawn_by(tmp_path, "1706.03762")
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path),
        ])
        assert code == 0
        assert "Aborted" in capsys.readouterr().out
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") == before  # unchanged (None)

    def test_md_is_byte_and_mtime_identical_through_the_whole_cycle(
        self, tmp_path, fake, monkeypatch, capsys
    ):
        # A paper imported before it was withdrawn has no withdrawal notice in its .md,
        # and acknowledge must not rewrite it (P4). The ledger becomes the sole audit
        # record; the .md never moves a byte or an mtime across withdraw/un-withdraw.
        _seed(tmp_path, "1706.03762", 7)
        before = _md_snapshot(tmp_path)

        fake(FakeClient([_work("1706.03762", version=7, withdrawn_by="author")]))
        run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path), "--yes",
        ])
        assert _md_snapshot(tmp_path) == before

        fake(FakeClient([_work("1706.03762", version=7, withdrawn_by=None)]))
        prompts = _confirm(monkeypatch, "y")  # the clear needs a human (#106)
        run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path),
        ])
        assert len(prompts) == 1  # the clear ran through the prompt, not around it
        assert _md_snapshot(tmp_path) == before


# --------------------------------------------------------------------------- #
# re-importability: the un-withdrawal clear repairs the identifying-field divergence
# --------------------------------------------------------------------------- #
class TestReimportable:
    def _seed_openalex_primary_with_arxiv_withdrawal(self, kb):
        """A paper in the KB via OpenAlex, whose ledger's arXiv record was seen
        withdrawn (``withdrawn_by="author"``). A later arXiv import folds into this
        original's ledger via §7.3 merge — the path where the identifying-field
        comparison runs, and where a stale ``withdrawn_by`` errors re-import.
        """
        (kb / "sources").mkdir(exist_ok=True)
        md = kb / "sources" / "paper.md"
        md.write_text(
            "---\nimported_from: openalex\nopenalex_id: W123\n"
            "arxiv_id: 1706.03762\ntitle: A paper\n---\n# paper\n"
        )
        records = [
            SourceRecord(type="openalex", id="W123",
                         imported_at="2026-01-01T00:00:00+00:00", fields={}),
            SourceRecord(type="arxiv", id="1706.03762",
                         imported_at="2026-01-01T00:00:00+00:00",
                         fields={"version": 7, "withdrawn_by": "author"}),
        ]
        write_provenance(sidecar_path(md, kb), Provenance(records=records))
        return md

    def test_acknowledged_then_unwithdrawn_then_acknowledged_is_reimportable(
        self, tmp_path, fake, monkeypatch, capsys
    ):
        self._seed_openalex_primary_with_arxiv_withdrawal(tmp_path)
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") == "author"

        # arXiv un-withdraws it. WITHOUT clearing, a fresh arXiv import merges into
        # the original's ledger, diverges in an identifying field (recorded "author"
        # vs parsed None), and errors permanently (P3 broken) — a refresh may not
        # clear an identifying field either.
        un_withdrawn = _work("1706.03762", version=7, withdrawn_by=None)
        divergent = import_works(
            [un_withdrawn], target=tmp_path, imported_at="2026-02-01T00:00:00+00:00"
        )
        assert divergent.errors == 1  # the trap, measured

        # The human acknowledges the un-withdrawal at the prompt (a clear is never
        # confirmed by --yes, #106); the identifying field is CLEARED, not left as
        # "author".
        fake(FakeClient([un_withdrawn]))
        prompts = _confirm(monkeypatch, "y")
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path),
        ])
        assert code == 0
        assert len(prompts) == 1  # a human confirmed the clear that repaired re-import
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") is None

        # Now the same fresh import merges cleanly again — the divergence is gone.
        repaired = import_works(
            [un_withdrawn], target=tmp_path, imported_at="2026-03-01T00:00:00+00:00"
        )
        assert repaired.errors == 0


# --------------------------------------------------------------------------- #
# a fresh withdrawal still surfaces (regression guard on the None-recorded path)
# --------------------------------------------------------------------------- #
def test_fresh_withdrawal_none_recorded_still_surfaces(tmp_path, fake, capsys):
    _seed(tmp_path, "1904.09773", 1)  # no recorded withdrawal
    fake(FakeClient([_work("1904.09773", version=1, withdrawn_by="author")]))
    run(["arxiv-check-versions", "--target", str(tmp_path)])
    out = capsys.readouterr().out
    assert "WITHDRAWN by the author" in out
    assert "which the ledger did not record" in out
    assert "Newly withdrawn:     1" in out


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


# --------------------------------------------------------------------------- #
# #107 item 2 — the notes must not claim "ledger" or divergence for a front-matter value
# --------------------------------------------------------------------------- #
class TestNoteProvenance:
    def test_front_matter_withdrawal_note_says_front_matter_not_ledger(
        self, tmp_path, fake, capsys
    ):
        # A pre-#82 paper whose front matter records "author"; arXiv now reports "admin".
        _seed_front_matter_only(tmp_path, "1904.09773", 1, withdrawn_by="author")
        fake(FakeClient([_work("1904.09773", version=1, withdrawn_by="admin")]))
        run(["arxiv-check-versions", "--target", str(tmp_path)])
        out = capsys.readouterr().out
        assert "WITHDRAWN by arXiv administrators" in out
        # It attributes the recorded value to the front matter, never to a ledger.
        assert "where the front matter recorded a withdrawal by the author" in out
        assert "the ledger recorded" not in out

    def test_front_matter_un_withdrawal_note_makes_no_false_claim(
        self, tmp_path, fake, capsys
    ):
        # A pre-#82 paper whose front matter records a withdrawal arXiv has reversed.
        _seed_front_matter_only(tmp_path, "1904.09773", 1, withdrawn_by="author")
        fake(FakeClient([_work("1904.09773", version=1, withdrawn_by=None)]))
        run(["arxiv-check-versions", "--target", str(tmp_path)])
        out = capsys.readouterr().out
        assert "No longer withdrawn" in out
        assert "its front matter still records a withdrawal by the author" in out
        # It must NOT claim a divergence/re-import error (there is no ledger to diverge),
        # nor prescribe the acknowledge command (which would exit 1 for this paper).
        assert "re-import will error" not in out
        assert "diverges from a fresh import" not in out
        assert "arxiv-acknowledge-withdrawal --id" not in out
        assert "#105" in out


# --------------------------------------------------------------------------- #
# #107 items 1 & 3 — refuse BEFORE the live query, for zero API requests
# --------------------------------------------------------------------------- #
class TestRefusesBeforeQuery:
    def test_front_matter_only_refuses_before_query_zero_api(
        self, tmp_path, fake, capsys
    ):
        # A pre-#82 paper: front matter carries the withdrawal, no sidecar. acknowledge()
        # writes only sidecars, so this can never be written; the command must refuse
        # before the fetch (not after spending a request and prompting) and point at #105.
        _seed_front_matter_only(tmp_path, "1706.03762", 7, withdrawn_by="author")
        before = _kb_snapshot(tmp_path)
        client = fake(FakeClient([_work("1706.03762", version=7, withdrawn_by="admin")]))
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path), "--yes",
        ])
        assert code == 1
        err = capsys.readouterr().err
        assert "front matter" in err and "#105" in err
        assert client.calls == []  # ZERO API requests
        assert not (tmp_path / "source-provenance").exists()  # no ledger fabricated
        assert _kb_snapshot(tmp_path) == before  # .md byte- and mtime_ns-identical

    def test_absent_id_refuses_before_query_zero_api(self, tmp_path, fake, capsys):
        _seed(tmp_path, "1706.03762", 7, withdrawn_by="author")  # a different paper
        before = _kb_snapshot(tmp_path)
        client = fake(FakeClient([_work("2005.13421", version=1, withdrawn_by="admin")]))
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "2005.13421",
            "--target", str(tmp_path), "--yes",
        ])
        assert code == 1
        assert "no arXiv record" in capsys.readouterr().err
        assert client.calls == []  # ZERO API requests
        assert _kb_snapshot(tmp_path) == before

    def test_unreadable_ledger_refuses_before_query_zero_api_mtime_identical(
        self, tmp_path, fake, capsys
    ):
        # One clean sidecar (no withdrawn_by) for the target id, plus one corrupt sidecar
        # that could carry it: the recorded value is unknowable, so refuse before the
        # fetch rather than assert "the ledger did not record" on an incomplete view.
        _seed(tmp_path, "1706.03762", 7, name="good")
        (tmp_path / "source-provenance" / "bad.json").write_text("{ broken")
        before = _kb_snapshot(tmp_path)
        client = fake(FakeClient([_work("1706.03762", version=7, withdrawn_by="admin")]))
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path), "--yes",
        ])
        assert code == 1
        err = capsys.readouterr().err
        assert "cannot read every provenance ledger" in err
        assert client.calls == []  # ZERO API requests
        assert _kb_snapshot(tmp_path) == before  # every sidecar byte- and mtime-identical


# --------------------------------------------------------------------------- #
# #107 item 7 — nothing to acknowledge exits 0 without a note or a prompt
# --------------------------------------------------------------------------- #
class TestNothingToAcknowledge:
    def test_both_none_exits_zero_without_prompting(
        self, tmp_path, fake, monkeypatch, capsys
    ):
        # Ledger records no withdrawal, arXiv reports none: nothing to silence. It must
        # not print a divergence note, must not prompt, and must not claim it silenced
        # a signal that never existed.
        _seed(tmp_path, "1706.03762", 7)  # not withdrawn
        fake(FakeClient([_work("1706.03762", version=7, withdrawn_by=None)]))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        def _boom(*a, **k):
            raise AssertionError("must not prompt when there is nothing to acknowledge")

        monkeypatch.setattr("builtins.input", _boom)
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path),
        ])
        out = capsys.readouterr().out
        assert code == 0
        assert "nothing to acknowledge" in out
        assert "no longer repeat this signal" not in out
        assert "WITHDRAWN by" not in out

    def test_same_agent_exits_zero_without_writing(self, tmp_path, fake, capsys):
        _seed(tmp_path, "1706.03762", 7, withdrawn_by="admin")
        fake(FakeClient([_work("1706.03762", version=7, withdrawn_by="admin")]))
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path), "--yes",
        ])
        out = capsys.readouterr().out
        assert code == 0
        assert "already records" in out
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") == "admin"


# --------------------------------------------------------------------------- #
# #106 — `--yes` may SET a withdrawal, never CLEAR one
#
# `detect_withdrawal` returning None cannot distinguish "arXiv reversed the withdrawal"
# from "we failed to read the withdrawal sentence" (a truncated abstract, an unmatched
# phrasing). Setting the field is the loud direction; clearing it is the silencing
# direction, gated on a human (#93). Under `--yes` no human sees the note, so the clear
# is refused — and the remedy is an interactive re-run, never a wider regex (#79).
# --------------------------------------------------------------------------- #
class TestYesCannotClear:
    def test_yes_refuses_the_clear_and_leaves_the_ledger_byte_identical(
        self, tmp_path, fake, capsys
    ):
        _seed(tmp_path, "1706.03762", 7, withdrawn_by="author")
        before = _kb_snapshot(tmp_path)
        fake(FakeClient([_work("1706.03762", version=7, withdrawn_by=None)]))
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path), "--yes",
        ])
        err = capsys.readouterr().err
        assert code == 1
        assert "refusing to clear the withdrawal" in err
        # It names the recorded value it declines to erase. Asserted as the whole
        # rendered fragment: a bare `"author" in err` would also match the note's
        # "withdrawal by the author", so it would pass a message that dropped the value.
        assert "1706.03762 (author)" in err
        # It names the working path: an interactive re-run without --yes.
        assert "without --yes" in err and "terminal" in err and "prompt" in err
        assert "Nothing written." in err
        # Byte- and mtime_ns-identical: no ledger write, no .md write.
        assert _kb_snapshot(tmp_path) == before
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") == "author"

    def test_yes_refusal_says_why_the_absence_is_not_trustworthy(
        self, tmp_path, fake, capsys
    ):
        # A parser miss and a real un-withdrawal are indistinguishable here; the message
        # must say so rather than assert arXiv reversed the withdrawal.
        _seed(tmp_path, "1706.03762", 7, withdrawn_by="admin")
        fake(FakeClient([_work("1706.03762", version=7, withdrawn_by=None)]))
        run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path), "--yes",
        ])
        err = capsys.readouterr().err
        assert "could not be read" in err
        # Names the value it is refusing to erase. `"admin" in err` would be a no-op
        # assertion: "admin" is a substring of the "arXiv administrators" this command
        # renders elsewhere, so it would hold even if the message named no value at all.
        assert "1706.03762 (admin)" in err
        assert "retracted" not in err.lower()

    def test_yes_still_sets_a_withdrawal(self, tmp_path, fake, capsys):
        # The loud direction is untouched: --yes records a fresh withdrawal.
        _seed(tmp_path, "1706.03762", 7)
        fake(FakeClient([_work("1706.03762", version=7, withdrawn_by="author")]))
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path), "--yes",
        ])
        assert code == 0
        assert "Recorded withdrawal by author" in capsys.readouterr().out
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") == "author"

    def test_yes_still_changes_the_agent_author_to_admin(self, tmp_path, fake, capsys):
        # An agent change is also a set, not a clear: the field stays populated.
        _seed(tmp_path, "1706.03762", 7, withdrawn_by="author")
        fake(FakeClient([_work("1706.03762", version=7, withdrawn_by="admin")]))
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path), "--yes",
        ])
        assert code == 0
        assert "Recorded withdrawal by admin" in capsys.readouterr().out
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") == "admin"

    def test_interactive_confirm_still_clears(self, tmp_path, fake, monkeypatch, capsys):
        _seed(tmp_path, "1706.03762", 7, withdrawn_by="author")
        fake(FakeClient([_work("1706.03762", version=7, withdrawn_by=None)]))
        prompts = _confirm(monkeypatch, "y")
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path),
        ])
        out = capsys.readouterr().out
        assert code == 0
        # The human saw the un-withdrawal note, was asked exactly once, and answered.
        assert "arXiv no longer reports 1706.03762 as withdrawn" in out
        assert len(prompts) == 1
        assert "Cleared the withdrawal" in out
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") is None

    def test_interactive_decline_clears_nothing(self, tmp_path, fake, monkeypatch, capsys):
        _seed(tmp_path, "1706.03762", 7, withdrawn_by="author")
        before = _kb_snapshot(tmp_path)
        fake(FakeClient([_work("1706.03762", version=7, withdrawn_by=None)]))
        prompts = _confirm(monkeypatch, "n")
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path),
        ])
        assert code == 0
        assert len(prompts) == 1
        assert "Aborted; nothing written." in capsys.readouterr().out
        assert _kb_snapshot(tmp_path) == before
        assert _ledger_withdrawn_by(tmp_path, "1706.03762") == "author"

    def test_yes_with_nothing_to_acknowledge_still_exits_zero(
        self, tmp_path, fake, capsys
    ):
        # Ledger records no withdrawal and arXiv reports none: there is no clear to
        # refuse, because there is no recorded signal to erase.
        _seed(tmp_path, "1706.03762", 7)
        fake(FakeClient([_work("1706.03762", version=7, withdrawn_by=None)]))
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path), "--yes",
        ])
        captured = capsys.readouterr()
        assert code == 0
        assert "nothing to acknowledge" in captured.out
        assert "refusing to clear" not in captured.err

    def test_the_refusal_costs_exactly_one_request_because_only_arxiv_reveals_the_clear(
        self, tmp_path, fake, capsys
    ):
        # The ledger alone cannot tell a clear from an agent change; only arXiv's live
        # answer does. So unlike the front-matter/absent-id/unreadable-ledger refusals
        # (#107), this one cannot be made before the fetch. It must not cost more.
        _seed(tmp_path, "1706.03762", 7, withdrawn_by="author")
        client = fake(FakeClient([_work("1706.03762", version=7, withdrawn_by=None)]))
        code = run([
            "arxiv-acknowledge-withdrawal", "--id", "1706.03762",
            "--target", str(tmp_path), "--yes",
        ])
        assert code == 1
        assert client.calls == [["1706.03762"]]
