# SPDX-License-Identifier: Apache-2.0
"""The three search commands' `--porcelain` result rows keep their shape (#406).

`porcelain.py` names the positional contract — a fixed field count read by column
offset — and the three `_*_show_results` printers did not honour it, so a tab in an
upstream `title` added a column and a line break split the row, silently, in exactly
the commands used most.

Scope, stated exactly, because an earlier draft of this docstring overreached: what is
covered below is the three `result` rows and nothing else. It is *not* a survey of the
porcelain emitters — the eleven this file leaves out were still ungated when it landed,
and #416 closed them in `test_dry_run_porcelain_rows.py`. `porcelain.py` remains the one
place that records what is and is not gated; read that note, not this one, for the state
of the set. Read this file as "these three rows hold their shape".

The three are tested together because the bug was one bug in three copies: the rows
share a shape (`result\\t<index>\\t<id>\\t<flag>\\t<title>`), so they share the
assertions — one row, five columns, one output line per result.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from factlog import cli
from factlog.integrations.common.porcelain import _LINE_BREAKS


# Exactly the set the gate covers — tab plus every `_LINE_BREAKS` character — plus U+0020
# SPACE, which it does not. The space is a negative control: it must stay green even with
# the gate disabled, which is what shows the rest of the suite goes red for the gate and
# not because these assertions reject everything. Twelve members; U+0020 is the ninth.
#
# Write that space as a literal space and check it stayed one. An earlier revision of this
# line held U+2028 where the space belongs — it renders as a space in a terminal, so the
# comment above it described a control that did not exist, and the "measured" evidence for
# it (no U+0020 among the failures) held only because no U+0020 case was ever collected.
# Verify by code point, never by eye: `[hex(ord(c)) for c in HOSTILE]` is 12 long and
# contains 0x20. Mistaking an unchecked path for a checked one is this file's own subject.
#
# The issue names tab, newline and U+2028: all three are in `_LINE_BREAKS` or added by the
# tab above, so none needs adding here. U+2028 is the pointed member — legal XML, legal
# JSON, not a control character by any C0 reading, and `str.splitlines()` breaks on it, so
# a "strip control characters" gate would have let it through.
HOSTILE = sorted({"\t", " ", *_LINE_BREAKS})


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


def _result_rows(capsys, expected, columns=5):
    """The `result` lines: exactly ``expected`` rows + `found`, each of ``columns`` fields.

    Both halves of the contract are checked here because they fail independently and
    neither implies the other:

    * **Line count** is deliberately *not* a filter on the ``result\\t`` prefix. A title
      carrying a line break splits its row into a `result`-prefixed head and an orphan
      tail, and a filter would count the head, see one well-formed row, and pass. The
      orphan line is the whole bug, so the count is taken over every line printed
      (measured — a prefix filter here was green against an ungated printer).
    * **Column count** catches what the line count cannot: a tab adds a field without
      splitting the row, so the line count stays right while a positional consumer reads
      the wrong field. That dimension lived in each caller until it was folded in here.

    ``columns`` is a parameter rather than a literal 5 because ``openalex-cite`` emits the
    same row with a leading ``scope`` field, six wide.
    """
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == expected + 1, f"expected {expected} rows + found, got {lines!r}"
    assert lines[-1].startswith("found\t")
    rows = lines[:-1]
    for row in rows:
        assert row.startswith("result\t"), f"not a result row: {row!r}"
        assert len(row.split("\t")) == columns, f"field count drifted: {row!r}"
    return rows


@pytest.mark.parametrize("name, build, show", COMMANDS, ids=IDS)
@pytest.mark.parametrize("char", HOSTILE, ids=lambda c: f"U+{ord(c):04X}")
class TestAHostileTitle:
    def test_the_row_stays_one_line_of_five_columns(self, name, build, show, char,
                                                    capsys):
        show([build(f"before{char}after")], 1, porcelain=True)
        _result_rows(capsys, 1)

    def test_one_output_line_per_result_plus_the_found_row(self, name, build, show,
                                                           char, capsys):
        # The count is what a consumer reads to know it has every result. Two results
        # must be two lines, never three because one title carried a break.
        works = [build(f"a{char}b", "X1"), build(f"c{char}d", "X2")]
        show(works, 2, porcelain=True)
        _result_rows(capsys, 2)


@pytest.mark.parametrize("name, build, show", COMMANDS, ids=IDS)
class TestAHostileIdentifier:
    def test_the_id_column_cannot_add_a_column_either(self, name, build, show, capsys):
        # The id is upstream data too — an OpenAlex id, a versioned arXiv id, a pmid all
        # arrive from a response this code did not write — so it is gated on the same
        # terms as the title, not trusted for being "an identifier".
        show([build("A paper", "W1\nresult\t9\tforged")], 1, porcelain=True)
        _result_rows(capsys, 1)


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
