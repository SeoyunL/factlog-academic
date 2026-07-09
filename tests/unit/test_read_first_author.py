# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``front_matter.read_first_author`` (#75).

``read_scalars`` is scalar-only and drops the ``authors`` YAML list entirely — a
naive reader yields an empty surname and silently disables the fallback for every
paper. This reader is the tested fix. It must read the flow form the writers emit,
a hand-written block form, decode the double-quote escapes, and fail closed (return
``""``) on anything it cannot parse — never raise.
"""
from __future__ import annotations

import re

from factlog.integrations.common._textio import yaml_list
from factlog.integrations.common.front_matter import read_first_author


def _write(tmp_path, block):
    path = tmp_path / "s.md"
    path.write_text(f"---\n{block}\n---\n\n# body\n", encoding="utf-8")
    return path


def test_the_measured_trap_read_scalars_drops_authors(tmp_path):
    # The exact measurement from the issue: read_scalars returns no 'authors'.
    from factlog.integrations.common.front_matter import read_scalars
    path = _write(tmp_path, 'title: "T"\nauthors: ["Ada Lovelace", "Alan Turing"]\nyear: 2023')
    scalars = read_scalars(path, ("title", "authors", "year"))
    assert "authors" not in scalars  # dropped entirely
    # ...and read_first_author recovers what read_scalars could not.
    assert read_first_author(path) == "Ada Lovelace"


class TestFlowForm:
    def test_writer_emitted_flow_list(self, tmp_path):
        block = f"title: \"T\"\nauthors: {yaml_list(['Ada Lovelace', 'Alan Turing'])}"
        assert read_first_author(_write(tmp_path, block)) == "Ada Lovelace"

    def test_single_element_flow_list(self, tmp_path):
        block = f"authors: {yaml_list(['Zonghai Yao'])}"
        assert read_first_author(_write(tmp_path, block)) == "Zonghai Yao"

    def test_empty_flow_list(self, tmp_path):
        assert read_first_author(_write(tmp_path, "authors: []")) == ""

    def test_escapes_are_decoded(self, tmp_path):
        # A name with a quote and a backslash, escaped by yaml_scalar.
        name = 'O"Brien \\ X'
        block = f"authors: {yaml_list([name, 'Second'])}"
        assert read_first_author(_write(tmp_path, block)) == name

    def test_non_ascii_name(self, tmp_path):
        block = f"authors: {yaml_list(['François Fleuret'])}"
        assert read_first_author(_write(tmp_path, block)) == "François Fleuret"

    def test_compound_surname_name(self, tmp_path):
        block = f"authors: {yaml_list(['Jan van der Berg'])}"
        assert read_first_author(_write(tmp_path, block)) == "Jan van der Berg"


class TestBlockForm:
    def test_block_sequence(self, tmp_path):
        block = "title: \"T\"\nauthors:\n  - Ada Lovelace\n  - Alan Turing"
        assert read_first_author(_write(tmp_path, block)) == "Ada Lovelace"

    def test_block_sequence_quoted(self, tmp_path):
        block = 'authors:\n  - "Ada Lovelace"\n  - "Alan Turing"'
        assert read_first_author(_write(tmp_path, block)) == "Ada Lovelace"


class TestDegradesClosed:
    def test_no_authors_key(self, tmp_path):
        assert read_first_author(_write(tmp_path, 'title: "T"')) == ""

    def test_no_front_matter(self, tmp_path):
        path = tmp_path / "s.md"
        path.write_text("# just a body\n", encoding="utf-8")
        assert read_first_author(path) == ""

    def test_ignore_re_blanks_companion_file(self, tmp_path):
        block = 'note_of: "K1"\nauthors: ["Ada Lovelace"]'
        path = _write(tmp_path, block)
        assert read_first_author(path, re.compile(r"^note_of:", re.MULTILINE)) == ""

    def test_single_scalar_author(self, tmp_path):
        # Not what the writers emit, but must not be mistaken for an empty list.
        assert read_first_author(_write(tmp_path, 'authors: "Solo Author"')) == "Solo Author"
