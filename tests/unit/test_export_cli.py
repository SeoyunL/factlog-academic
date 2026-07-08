# SPDX-License-Identifier: Apache-2.0
"""CLI tests for `factlog export --bibtex` (hermetic — no Zotero)."""
from __future__ import annotations

import pytest

from factlog import cli


def _kb(tmp_path):
    (tmp_path / "sources").mkdir()
    return tmp_path


def _src(kb, name, body):
    (kb / "sources" / name).write_text(body, encoding="utf-8")


BIB = (
    '---\nzotero_key: "K1"\nitem_type: "journalArticle"\ntitle: "A Study"\n'
    'authors: ["Doe J"]\nyear: "2020"\ndoi: "10.1/x"\n---\n\n# body\n'
)
NOTES = '---\nsource_kind: annotations\nzotero_key: "K1"\ntitle: "A Study"\n---\n\n# notes\n'
PLAIN = "# just a user source, no front matter\n"


class TestExport:
    def test_emits_bibtex_to_stdout(self, tmp_path, capsys):
        kb = _kb(tmp_path)
        _src(kb, "doe-2020-a-study.md", BIB)
        rc = cli.main(["export", "--bibtex", "--target", str(kb)])
        out = capsys.readouterr().out
        assert rc == 0
        assert out.startswith("@article{doe-2020-a-study,")
        assert "author = {Doe J}," in out and "doi = {10.1/x}," in out

    def test_skips_annotation_and_plain_sources(self, tmp_path, capsys):
        kb = _kb(tmp_path)
        _src(kb, "doe-2020-a-study.md", BIB)
        _src(kb, "doe-2020-a-study-notes.md", NOTES)
        _src(kb, "user.md", PLAIN)
        rc = cli.main(["export", "--bibtex", "--target", str(kb)])
        out = capsys.readouterr().out
        assert rc == 0
        assert out.count("@") == 1  # only the bib source

    def test_deterministic_order(self, tmp_path, capsys):
        kb = _kb(tmp_path)
        _src(kb, "b.md", '---\ntitle: "B"\n---\n')
        _src(kb, "a.md", '---\ntitle: "A"\n---\n')
        cli.main(["export", "--bibtex", "--target", str(kb)])
        out = capsys.readouterr().out
        assert out.index("{a,") < out.index("{b,")

    def test_output_file(self, tmp_path, capsys):
        kb = _kb(tmp_path)
        _src(kb, "doe-2020.md", BIB)
        outfile = tmp_path / "refs.bib"
        rc = cli.main(["export", "--bibtex", "--target", str(kb), "--output", str(outfile)])
        assert rc == 0
        assert outfile.read_text(encoding="utf-8").startswith("@article{doe-2020,")
        assert capsys.readouterr().out == ""  # stdout stays clean when writing a file

    def test_requires_format(self, tmp_path, capsys):
        kb = _kb(tmp_path)
        rc = cli.main(["export", "--target", str(kb)])
        assert rc == 2
        assert "exactly one format" in capsys.readouterr().err

    def test_csl_emits_json_array(self, tmp_path, capsys):
        import json

        kb = _kb(tmp_path)
        _src(kb, "doe-2020-a-study.md", BIB)
        rc = cli.main(["export", "--csl", "--target", str(kb)])
        out = capsys.readouterr().out
        assert rc == 0
        data = json.loads(out)
        assert data[0]["id"] == "doe-2020-a-study"
        assert data[0]["type"] == "article-journal"
        assert data[0]["DOI"] == "10.1/x"

    def test_both_formats_rejected(self, tmp_path):
        kb = _kb(tmp_path)
        with pytest.raises(SystemExit):  # argparse mutually exclusive
            cli.main(["export", "--bibtex", "--csl", "--target", str(kb)])

    def test_not_a_kb(self, tmp_path, capsys):
        rc = cli.main(["export", "--bibtex", "--target", str(tmp_path)])
        assert rc == 1
        assert "not a factlog KB" in capsys.readouterr().err

    def test_empty_kb_is_ok(self, tmp_path, capsys):
        kb = _kb(tmp_path)
        rc = cli.main(["export", "--bibtex", "--target", str(kb)])
        assert rc == 0
        assert capsys.readouterr().out == ""

    def test_parser_registers_export(self):
        args = cli.build_parser().parse_args(["export", "--bibtex"])
        assert args.func is cli.cmd_export and args.bibtex is True
