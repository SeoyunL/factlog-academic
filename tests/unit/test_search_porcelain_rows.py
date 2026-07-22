# SPDX-License-Identifier: Apache-2.0
"""The three search commands' `--porcelain` result rows keep their shape (#406).

`porcelain.py` names the positional contract — a fixed field count read by column
offset — and every other porcelain emitter routes caller-influenced values through
`porcelain_field`. The three `_*_show_results` printers did not, so a tab in an
upstream `title` added a column and a line break split the row, silently, in exactly
the commands used most.

The three are tested together because the bug was one bug in three copies: the rows
share a shape (`result\\t<index>\\t<id>\\t<flag>\\t<title>`), so they share the
assertions — one row, five columns, one output line per result.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from factlog import cli
from factlog.integrations.common.porcelain import _LINE_BREAKS


# Every character the gate covers, plus the three the issue names outright. U+2028 is
# the pointed one: legal XML, legal JSON, not a control character by any C0 reading,
# and `str.splitlines()` breaks on it — so a "strip control characters" gate would
# have let it through.
HOSTILE = sorted({"\t", " ", *_LINE_BREAKS})


def _openalex(title, work_id="W1"):
    return SimpleNamespace(openalex_id=work_id, openalex_is_retracted=False, title=title)


def _arxiv(title, work_id="2401.00001v1"):
    return SimpleNamespace(versioned_id=work_id, withdrawn=False, title=title)


def _pubmed(title, work_id="111"):
    return SimpleNamespace(pmid=work_id, retracted=False, title=title)


# (name, builder, show function) — the show functions differ only in their keyword-only
# extras, all of which default, so one call shape covers the three.
COMMANDS = [
    ("openalex-search", _openalex, cli._openalex_show_results),
    ("arxiv-search", _arxiv, cli._arxiv_show_results),
    ("pubmed-search", _pubmed, cli._pubmed_show_results),
]
IDS = [name for name, _, _ in COMMANDS]


def _result_rows(capsys, expected):
    """The `result` lines, asserting the block is exactly ``expected`` rows + `found`.

    Deliberately *not* a filter on the ``result\\t`` prefix: a title carrying a line
    break splits its row into a `result`-prefixed head and an orphan tail, and a filter
    would count the head, see one well-formed row, and pass. The orphan line is the
    whole bug, so the count is taken over every line printed (measured — a prefix
    filter here was green against an ungated printer).
    """
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == expected + 1, f"expected {expected} rows + found, got {lines!r}"
    assert lines[-1].startswith("found\t")
    return lines[:-1]


@pytest.mark.parametrize("name, build, show", COMMANDS, ids=IDS)
@pytest.mark.parametrize("char", HOSTILE, ids=lambda c: f"U+{ord(c):04X}")
class TestAHostileTitle:
    def test_the_row_stays_one_line_of_five_columns(self, name, build, show, char,
                                                    capsys):
        show([build(f"before{char}after")], 1, porcelain=True)
        row, = _result_rows(capsys, 1)
        assert row.startswith("result\t"), f"{name}: the title split the row"
        assert len(row.split("\t")) == 5, f"{name}: the title added a column"

    def test_one_output_line_per_result_plus_the_found_row(self, name, build, show,
                                                           char, capsys):
        # The count is what a consumer reads to know it has every result. Two results
        # must be two lines, never three because one title carried a break.
        works = [build(f"a{char}b", "X1"), build(f"c{char}d", "X2")]
        show(works, 2, porcelain=True)
        rows = _result_rows(capsys, 2)
        assert all(len(row.split("\t")) == 5 for row in rows), name


@pytest.mark.parametrize("name, build, show", COMMANDS, ids=IDS)
class TestAHostileIdentifier:
    def test_the_id_column_cannot_add_a_column_either(self, name, build, show, capsys):
        # The id is upstream data too — an OpenAlex id, a versioned arXiv id, a pmid all
        # arrive from a response this code did not write — so it is gated on the same
        # terms as the title, not trusted for being "an identifier".
        show([build("A paper", "W1\nresult\t9\tforged")], 1, porcelain=True)
        row, = _result_rows(capsys, 1)
        assert len(row.split("\t")) == 5, f"{name}: the id added a column"


@pytest.mark.parametrize("name, build, show", COMMANDS, ids=IDS)
class TestOrdinaryOutputIsUnchanged:
    def test_a_clean_title_survives_verbatim(self, name, build, show, capsys):
        # The gate replaces tabs and line breaks and nothing else: a row with neither
        # must read exactly as it did before #406, so downstream parsers see no drift.
        show([build("A paper: on 1998 Dec-1999 Jan", "W1")], 1, porcelain=True)
        assert _result_rows(capsys, 1) == [
            "result\t1\tW1\t-\tA paper: on 1998 Dec-1999 Jan"]

    def test_a_missing_title_stays_an_empty_last_field(self, name, build, show, capsys):
        show([build(None, "W1")], 1, porcelain=True)
        assert _result_rows(capsys, 1) == ["result\t1\tW1\t-\t"]
