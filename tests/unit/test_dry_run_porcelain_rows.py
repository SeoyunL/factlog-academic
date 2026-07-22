# SPDX-License-Identifier: Apache-2.0
"""The import/refresh/search commands' remaining `--porcelain` rows keep their shape (#416).

`porcelain.py` names the positional contract — a fixed field count read by column offset.
#406 closed the three search `result` rows and said in its own docstring that it was not
closing the set. This file closes what that note listed, plus one emitter the note did not
have: the `candidate` row.

Scope, stated exactly, because #406's first draft of this sentence overreached: what is
covered below is **fifteen** emitters — two `query` rows, four dry-run `item`/`work` rows,
eight `target` rows and the `candidate` row — carrying **twenty** caller-influenced columns
between them. #416 gated fourteen of the fifteen (eighteen of the twenty columns); the
remaining one, `_pubmed_finish`'s `work` row, was gated by #141 and is covered here as a
regression guard and as the control that this shape *can* be checked.

Add the four groups up rather than trusting the total: an earlier draft of this paragraph
said "eleven emitters" over a list that added to twelve, which is a small instance of this
file's own subject. This is *not* a survey — `porcelain.py` remains the one place that
records what is and is not gated.

**Two different things are tested here and the difference is load-bearing.**

*Fixing the emitter* — that the row holds its shape when a hostile value is put in it —
is what most classes below do, by injecting through `SimpleNamespace` and
`monkeypatch.setattr`. That is a real property and it is what the mutation suite kills
mutants against.

*Reaching the emitter* — that such a value can actually arrive from outside — is a
separate experiment, and injection says **nothing** about it. Where reachability is
established, it is by driving the real command with a real hostile value and no fake in
the path:

* both `query` rows — `--query $'a\\tb' --show-query --porcelain`,
* all eight `target` rows — a KB in a directory whose name holds a tab,
* the pubmed `work` row — a tab-carrying PMID in a real efetch body, which arrives and is
  neutralized to a space (what #141's gate looks like from outside),
* the `candidate` row's `existing_path` column — a real source file renamed to hold a tab,
  since that path comes from a scan of `sources/` (`TestTheCandidateRowReachesDiskFilenames`).

**Gated, with no route found — say it this way, not "measured":**

* the arXiv `work` row: `versioned_id` is only built from an `arxiv_id` that came through
  `normalize_arxiv_id`/`parse_entry_id`, and an exhaustive run (every gated character, every
  insertion point) carries none through;
* the OpenAlex `work` row: `normalize_work_id` likewise, and a hostile title is slugified
  before it reaches the filename column;
* the `candidate` row's `key` column: `normalize_work_id` again.

They are gated regardless — `outcome.key` *is* `work.openalex_id`/`work.versioned_id`, the
values `_openalex_show_results` and `_arxiv_show_results` gate one row over, and a caller
gates its value rather than reasoning about what its own parser admits. **Gating an
unreachable value is right; calling it measured is not.** An earlier revision of this
docstring listed the arXiv `work` row and the `candidate` `key` column as measured end to
end on the strength of tests that fed them fabricated values through a fake client and a
test helper — which fixes the emitter and is silent on reachability. Review caught it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from factlog import cli
from factlog.integrations.common.porcelain import _LINE_BREAKS

# Exactly the set the gate covers — tab plus every `_LINE_BREAKS` character — plus U+0020
# SPACE, which it does not. The space is a negative control: it must stay green with the
# gate reverted, which is what shows the rest of this file goes red for the gate and not
# because the assertions reject everything.
#
# Verify by code point, never by eye: `[f"U+{ord(c):04X}" for c in HOSTILE]` is 12 long and
# contains U+0020. #406 shipped a revision of this line with U+2028 where the space belongs
# — it renders as a space in a terminal — so the control it documented did not exist, and
# nothing ever collected a U+0020 case to notice. The ids below are code points for the
# same reason: a `-k "U+0020"` selection must be able to find the control and count it.
HOSTILE = sorted({"\t", " ", *_LINE_BREAKS})
CHAR_IDS = [f"U+{ord(c):04X}" for c in HOSTILE]


def _assert_row(capsys, token, *, columns, lines):
    """Assert the ``token`` row has ``columns`` fields and the output has ``lines`` lines.

    Both dimensions, always, because they fail independently and neither implies the
    other — and because checking only one is a live way to write a test that passes for
    the wrong reason. Measured, on this file: with only the column count asserted, eleven
    of the seventeen mutants below were killed by the tab case alone. A line break in the
    *last* column splits the row into a head that still has the full field count and an
    orphan tail on its own line, so the column count reads clean while a consumer is
    handed a row that is not there. The line count is what sees the orphan; it is taken
    over every line printed, never over the ``token``-prefixed ones, since a prefix filter
    counts the head and passes for exactly the same reason.

    Returns the row so a caller can assert on its content.
    """
    out = capsys.readouterr().out.splitlines()
    rows = [ln for ln in out if ln.split("\t", 1)[0] == token]
    assert len(rows) == 1, f"expected one {token} row, got {rows!r} in {out!r}"
    assert len(rows[0].split("\t")) == columns, f"field count drifted: {rows[0]!r}"
    assert len(out) == lines, f"a row split — expected {lines} lines, got {out!r}"
    return rows[0]


def _outcome(status="imported", key="W1", name="a-paper.md"):
    path = SimpleNamespace(name=name) if name is not None else None
    # `title` and `withdrawn_by` feed the stderr warning helpers the finish functions run
    # alongside the porcelain rows; a clean value there keeps stderr out of the way.
    return SimpleNamespace(status=status, key=key, path=path, title="A paper",
                           withdrawn_by=None)


def _report(outcomes=(), candidates=()):
    return SimpleNamespace(
        outcomes=list(outcomes), candidates=list(candidates),
        imported=len(outcomes), skipped=0, merged=0, errors=0,
        candidate_ledger_error=None,
    )


def _kb(tmp_path):
    (tmp_path / "sources").mkdir(exist_ok=True)
    return tmp_path


def _finish(fn, report, target):
    """Call one of the three `_*_finish` helpers; they differ only in their warning kwarg."""
    kw = {"warning": ""} if fn is cli._openalex_finish else {"warnings": []}
    return fn(report, target, dry_run=True, porcelain=True, **kw)


# --------------------------------------------------------------------------- #
# The `query` row — the most caller-influenced value on any porcelain row.
# --------------------------------------------------------------------------- #
QUERY_COMMANDS = [
    ("arxiv-search", ["arxiv-search"]),
    ("pubmed-search", ["pubmed-search"]),
]
QUERY_IDS = [name for name, _ in QUERY_COMMANDS]


@pytest.mark.parametrize("char", HOSTILE, ids=CHAR_IDS)
@pytest.mark.parametrize("name, argv", QUERY_COMMANDS, ids=QUERY_IDS)
class TestTheQueryRow:
    def test_the_row_stays_one_line_of_two_columns(self, name, argv, char, tmp_path,
                                                   capsys):
        # End to end through the real command: `--show-query` spends no request and
        # returns before any client is built, so this is the whole path a user takes.
        args = cli.build_parser().parse_args(
            [*argv, "--query", f"a{char}b", "--show-query", "--porcelain",
             "--target", str(_kb(tmp_path))])
        assert args.func(args) == 0
        # `--show-query` prints the row and nothing else, so the whole output is one line.
        _assert_row(capsys, "query", columns=2, lines=1)


# --------------------------------------------------------------------------- #
# The dry-run `item`/`work` row — one shape, four emitters, one of them gated before #416.
# --------------------------------------------------------------------------- #
DRY_RUN_ROWS = [
    ("openalex-import", "work", cli._openalex_finish),
    ("arxiv-import", "work", cli._arxiv_finish),
    ("pubmed-import", "work", cli._pubmed_finish),
]
DRY_RUN_IDS = [name for name, _, _ in DRY_RUN_ROWS]

# The two caller-influenced columns of that row, varied one at a time so a mutant that
# reverts one gate is distinguishable from a mutant that reverts the other. Tested
# together they are not: either revert fails "the key column" and "the name column" alike.
COLUMNS = ["key", "name"]


@pytest.mark.parametrize("column", COLUMNS)
@pytest.mark.parametrize("char", HOSTILE, ids=CHAR_IDS)
@pytest.mark.parametrize("name, token, fn", DRY_RUN_ROWS, ids=DRY_RUN_IDS)
class TestTheDryRunWorkRow:
    def test_the_row_stays_one_line_of_four_columns(self, name, token, fn, char, column,
                                                    tmp_path, capsys):
        hostile = f"a{char}b"
        outcome = _outcome(key=hostile if column == "key" else "W1",
                           name=hostile if column == "name" else "a-paper.md")
        _finish(fn, _report([outcome]), tmp_path)
        # The work row + imported/skipped/merged/errors/dry_run/target + candidates.
        _assert_row(capsys, token, columns=4, lines=8)

    def test_two_works_are_two_rows_and_two_lines(self, name, token, fn, char, column,
                                                  tmp_path, capsys):
        # The count is what a consumer reads to know it has every work. Two outcomes must
        # be two `work` lines, never three because one key carried a break.
        hostile = f"a{char}b"
        outcomes = [
            _outcome(key=hostile if column == "key" else f"W{i}",
                     name=hostile if column == "name" else f"p{i}.md")
            for i in (1, 2)
        ]
        _finish(fn, _report(outcomes), tmp_path)
        lines = capsys.readouterr().out.splitlines()
        assert sum(ln.startswith(f"{token}\t") for ln in lines) == 2
        assert len(lines) == 9, f"a row split: {lines!r}"


@pytest.mark.parametrize("column", COLUMNS)
@pytest.mark.parametrize("char", HOSTILE, ids=CHAR_IDS)
class TestTheZoteroItemRow:
    """`zotero-import`'s dry-run row, the fourth sibling — emitted inline, not via a helper."""

    def test_the_row_stays_one_line_of_four_columns(self, char, column, tmp_path,
                                                    monkeypatch, capsys):
        from factlog.integrations.zotero import importer as zotero_importer

        hostile = f"a{char}b"
        outcome = _outcome(key=hostile if column == "key" else "K1",
                           name=hostile if column == "name" else "a-paper.md")
        report = SimpleNamespace(
            outcomes=[outcome], imported=1, skipped=0, errors=0, pdf_outcomes=[],
            pdf_placed=0, pdf_skipped=0, pdf_errors=0, annotations_written=0,
            annotations_updated=0, annotations_skipped=0, annotation_errors=0,
        )
        monkeypatch.setattr(zotero_importer, "import_items", lambda *a, **k: report)
        monkeypatch.setattr(cli, "_make_zotero_client", lambda config: object())
        (tmp_path / "sources").mkdir()
        assert cli.main(["zotero-import", "--tag", "t", "--target", str(tmp_path),
                         "--porcelain", "--dry-run"]) == 0
        # The item row + imported/skipped/errors/dry_run/target.
        _assert_row(capsys, "item", columns=4, lines=6)


# --------------------------------------------------------------------------- #
# The `target` row — a path built from the user's `--target`, five emitters.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("char", HOSTILE, ids=CHAR_IDS)
@pytest.mark.parametrize("name, fn", [(n, f) for n, _, f in DRY_RUN_ROWS], ids=DRY_RUN_IDS)
class TestTheTargetRowFromAFinishHelper:
    def test_the_row_stays_one_line_of_two_columns(self, name, fn, char, tmp_path, capsys):
        # A POSIX filename may hold a tab, or a newline, outright — `mkdir` accepts both,
        # so the directory below is a real one and the path is a real path.
        target = tmp_path / f"kb{char}x"
        target.mkdir()
        _finish(fn, _report(), target)
        # imported/skipped/merged/errors/dry_run/target/candidates.
        row = _assert_row(capsys, "target", columns=2, lines=7)
        assert row.endswith("/sources"), f"the path lost its tail: {row!r}"


@pytest.mark.parametrize("char", HOSTILE, ids=CHAR_IDS)
class TestTheTargetRowFromZoteroImport:
    def test_the_row_stays_one_line_of_two_columns(self, char, tmp_path, monkeypatch,
                                                   capsys):
        from factlog.integrations.zotero import importer as zotero_importer

        target = tmp_path / f"kb{char}x"
        (target / "sources").mkdir(parents=True)
        report = SimpleNamespace(
            outcomes=[], imported=0, skipped=0, errors=0, pdf_outcomes=[],
            pdf_placed=0, pdf_skipped=0, pdf_errors=0, annotations_written=0,
            annotations_updated=0, annotations_skipped=0, annotation_errors=0,
        )
        monkeypatch.setattr(zotero_importer, "import_items", lambda *a, **k: report)
        monkeypatch.setattr(cli, "_make_zotero_client", lambda config: object())
        assert cli.main(["zotero-import", "--tag", "t", "--target", str(target),
                         "--porcelain"]) == 0
        # imported/skipped/errors/dry_run/target.
        _assert_row(capsys, "target", columns=2, lines=5)


@pytest.mark.parametrize("char", HOSTILE, ids=CHAR_IDS)
class TestTheTargetRowFromPubmedRefresh:
    """`pubmed-refresh --dry-run`'s `target` row, which prints the KB, not `sources/`.

    Two rows above it in the same block already went through the gate before #416; this one
    did not. A gap that narrow is the argument for one shared definition over a per-call
    judgement, and it is the emitter #416 measured first.

    Note this is only *one* of `pubmed-refresh`'s two `target` rows. The other lives in
    `refresh.porcelain_lines` and is covered by `TestTheTargetRowFromASummaryBuilder`
    below — #416's first cut gated this one and missed that one, in the same command.
    """

    def test_the_row_stays_one_line_of_two_columns(self, char, tmp_path, monkeypatch,
                                                   capsys):
        target = tmp_path / f"kb{char}x"
        (target / "sources").mkdir(parents=True)
        (target / "policy").mkdir()
        (target / "policy" / "pubmed-config.toml").write_text(
            '[client]\nemail = "test@example.com"\n', encoding="utf-8")
        (target / "sources" / "a.md").write_text(
            "---\npmid: 111\nimported_from: pubmed\njournal: J\n---\n\n# Paper\n",
            encoding="utf-8")
        # A dry run spends no request; the client must never be asked.
        monkeypatch.setattr(cli, "_make_pubmed_client", lambda config: object())
        args = cli.build_parser().parse_args(
            ["pubmed-refresh", "--target", str(target), "--dry-run", "--porcelain"])
        assert args.func(args) == 0
        # would-check (one paper) + would_check/skipped/dry_run/target.
        _assert_row(capsys, "target", columns=2, lines=5)


# --------------------------------------------------------------------------- #
# The three `target` rows built by `rows.append` in the integration packages.
# --------------------------------------------------------------------------- #
# These are not in `cli.py` and they are not printed by a `print(f"…)`: each is appended
# to a list that the CLI later prints line by line. That is exactly why #416's first cut
# missed all three — it searched for `print(f"`, a shape-based search that cannot see a
# row assembled anywhere else. The lesson is recorded in `porcelain.py`: search the AST
# for tab-carrying f-strings, not the source text for one spelling of "print".
#
# All three run end to end here: no fake client and no network, because a KB with nothing
# to check reaches the summary rows and returns without a request.
SUMMARY_COMMANDS = [
    ("pubmed-refresh", ["pubmed-refresh"], True),
    ("openalex-refresh", ["openalex-refresh"], False),
    ("arxiv-check-versions", ["arxiv-check-versions"], False),
]
SUMMARY_IDS = [name for name, _, _ in SUMMARY_COMMANDS]


@pytest.mark.parametrize("char", HOSTILE, ids=CHAR_IDS)
@pytest.mark.parametrize("name, argv, needs_config", SUMMARY_COMMANDS, ids=SUMMARY_IDS)
class TestTheTargetRowFromASummaryBuilder:
    def test_the_row_stays_one_line_of_two_columns(self, name, argv, needs_config, char,
                                                   tmp_path, capsys):
        target = tmp_path / f"kb{char}x"
        (target / "sources").mkdir(parents=True)
        if needs_config:
            (target / "policy").mkdir()
            (target / "policy" / "pubmed-config.toml").write_text(
                '[client]\nemail = "test@example.com"\n', encoding="utf-8")
        args = cli.build_parser().parse_args([*argv, "--target", str(target),
                                              "--porcelain"])
        assert args.func(args) == 0
        # The row count differs per command (each has its own tallies), so assert the
        # `target` row's shape and that nothing split, without pinning a total.
        out = capsys.readouterr().out.splitlines()
        rows = [ln for ln in out if ln.split("\t", 1)[0] == "target"]
        assert len(rows) == 1, f"expected one target row, got {rows!r} in {out!r}"
        assert len(rows[0].split("\t")) == 2, f"field count drifted: {rows[0]!r}"
        # Every line is a well-formed row: a split leaves an orphan with no tab at all.
        assert all("\t" in ln for ln in out), f"a row split into {out!r}"


# --------------------------------------------------------------------------- #
# The `candidate` row (#75) — the emitter `porcelain.py`'s #406 note did not list.
# --------------------------------------------------------------------------- #
CANDIDATE_COLUMNS = ["key", "existing_path"]


@pytest.mark.parametrize("column", CANDIDATE_COLUMNS)
@pytest.mark.parametrize("char", HOSTILE, ids=CHAR_IDS)
class TestTheCandidateRow:
    def test_the_row_stays_one_line_of_four_columns(self, char, column, tmp_path, capsys):
        hostile = f"a{char}b"
        candidate = SimpleNamespace(
            existing_path=SimpleNamespace(
                name=hostile if column == "existing_path" else "existing.md"),
            score=1.0,
        )
        surfaced = SimpleNamespace(key=hostile if column == "key" else "W1",
                                   candidate=candidate)
        _finish(cli._openalex_finish, _report(candidates=[surfaced]), tmp_path)
        # imported/skipped/merged/errors/dry_run/target + candidate + candidates.
        _assert_row(capsys, "candidate", columns=4, lines=8)


class TestTheCandidateRowReachesDiskFilenames:
    """The one `candidate` column with a measured route: `existing_path.name`.

    The class above fixes the emitter by injecting both columns, which says nothing about
    whether either value can actually arrive. For `key` it cannot — `normalize_work_id`
    admits no hostile character (measured, all twelve). For `existing_path` it can: the
    path comes from a scan of `sources/`, and a POSIX filename may hold a tab outright.

    So this test renames a real source file rather than injecting a value, and it is the
    evidence behind the word "measured" for this row. With the gate reverted it emits five
    columns; with it, four.
    """

    def test_a_tab_in_a_source_filename_does_not_add_a_column(self, tmp_path, capsys):
        from test_fallback_candidate import (
            AAAI_DOI,
            MEDRXIV_DOI,
            T,
            _oa,
            _seed_kb,
            oa_import,
        )

        kb = _seed_kb(tmp_path)
        oa_import([_oa("W_AAAI", AAAI_DOI)], target=kb, imported_at=T)
        (existing,) = list((kb / "sources").glob("*.md"))
        existing.rename(existing.with_name("tab\tname.md"))

        report = oa_import([_oa("W_MEDRXIV", MEDRXIV_DOI)], target=kb, imported_at=T)
        cli._openalex_finish(report, kb, dry_run=False, porcelain=True, warning="")
        rows = [ln for ln in capsys.readouterr().out.splitlines()
                if ln.startswith("candidate\t")]
        assert len(rows) == 1, f"expected one candidate row, got {rows!r}"
        assert len(rows[0].split("\t")) == 4, f"field count drifted: {rows[0]!r}"
        assert "tab name.md" in rows[0], f"the filename was not neutralized: {rows[0]!r}"


# --------------------------------------------------------------------------- #
# Ordinary output is byte-unchanged.
# --------------------------------------------------------------------------- #
class TestOrdinaryOutputIsUnchanged:
    """The gate replaces tabs and line breaks and nothing else.

    A row with neither must read exactly as it did before #416, so a consumer parsing the
    six summary tokens sees no drift. This is the half of the contract the hostile cases
    cannot check: a gate that replaced every character would satisfy every assertion above.
    """

    def test_a_clean_work_row_survives_verbatim(self, tmp_path, capsys):
        _finish(cli._arxiv_finish, _report([_outcome(key="2401.00001v1")]), tmp_path)
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == "work\timported\t2401.00001v1\ta-paper.md"

    def test_a_clean_target_row_survives_verbatim(self, tmp_path, capsys):
        _finish(cli._arxiv_finish, _report(), tmp_path)
        rows = dict(ln.split("\t", 1) for ln in capsys.readouterr().out.splitlines())
        assert rows["target"] == str(tmp_path / "sources")

    def test_a_clean_query_row_survives_verbatim(self, tmp_path, capsys):
        args = cli.build_parser().parse_args(
            ["pubmed-search", "--query", "crispr gene editing", "--show-query",
             "--porcelain", "--target", str(_kb(tmp_path))])
        args.func(args)
        assert _assert_row(capsys, "query", columns=2, lines=1) == \
            "query\tcrispr gene editing"

    def test_a_missing_path_stays_an_empty_last_field(self, tmp_path, capsys):
        _finish(cli._arxiv_finish, _report([_outcome(key="W1", name=None)]), tmp_path)
        assert capsys.readouterr().out.splitlines()[0] == "work\timported\tW1\t"
