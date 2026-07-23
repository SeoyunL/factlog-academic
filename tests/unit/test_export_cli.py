# SPDX-License-Identifier: Apache-2.0
"""CLI tests for `factlog export --bibtex` (hermetic — no Zotero)."""
from __future__ import annotations

import pytest

from factlog import cli
from factlog.front_matter_scan import (
    FRONT_MATTER_MAX_CHARS,
    FRONT_MATTER_NO_OPENING_FENCE,
    FRONT_MATTER_UNCLOSED,
    FRONT_MATTER_UNREADABLE,
    FRONT_MATTER_UNSCANNED,
)


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


class TestSkipReason:
    """The skip notice names *why* a source could not be cited (#440).

    ``read_front_matter`` collapses several files into ``{}`` and the old notice
    reported them all as "no YAML front matter". The three that mean something an
    operator can act on -- an unclosed block (restore one ``---``), a block past the
    cap (shrink it), an undecodable body (fix the encoding) -- and a file that never
    opened a block at all now each carry their own reason. Nothing here asserted the
    message before, so a reason that regressed to the blunt string, or that borrowed
    another cause's wording, went unnoticed (the issue's premise).
    """

    def _skip_line(self, tmp_path, capsys, name, writer):
        """Export a KB holding one uncitable source; return its stderr skip line."""
        kb = _kb(tmp_path)
        writer(kb / "sources" / name)
        rc = cli.main(["export", "--bibtex", "--target", str(kb)])
        assert rc == 0
        err = capsys.readouterr().err
        rel = f"sources/{name}"
        lines = [ln for ln in err.splitlines() if rel in ln]
        assert len(lines) == 1, f"expected one skip line for {rel}, got: {err!r}"
        return lines[0]

    def test_unclosed_block_says_so_not_no_front_matter(self, tmp_path, capsys):
        """A deleted closing fence: the fix is one ``---``, not "add front matter".

        This is the case the issue calls most misleading -- the file opens with
        ``---`` and carries real keys, so "no YAML front matter" sent the operator
        looking for something that is already there.
        """
        line = self._skip_line(
            tmp_path, capsys, "unclosed.md",
            lambda p: p.write_text('---\ntitle: "T"\nyear: "2020"\n\nbody\n', encoding="utf-8"))
        assert FRONT_MATTER_UNCLOSED in line
        assert "no YAML front matter" not in line

    def test_capped_block_is_not_called_unclosed(self, tmp_path, capsys):
        """A block that *does* close, past the search cap: shrink it, do not add ``---``.

        Borrowing the unclosed wording here would tell the operator to restore a
        fence the file already has, so the two must not collapse to one message.
        """
        pad = "x" * FRONT_MATTER_MAX_CHARS
        line = self._skip_line(
            tmp_path, capsys, "huge.md",
            lambda p: p.write_text(f'---\ntitle: "T"\nauthors: {pad}\n---\n\nbody\n',
                                   encoding="utf-8"))
        assert FRONT_MATTER_UNSCANNED in line
        assert FRONT_MATTER_UNCLOSED not in line

    def test_undecodable_body_says_encoding_not_missing_fence(self, tmp_path, capsys):
        """Mojibake: the file opens with ``---`` but never decodes as UTF-8.

        A reason picked after the opening-fence test rather than in the handler
        would call this unclosed and hide that the fix is the encoding.
        """
        line = self._skip_line(
            tmp_path, capsys, "mojibake.md",
            lambda p: p.write_bytes(b'---\ntitle: "T"\n' + b"filler\n" * 1000 + b"\xff\xfe\n"))
        assert FRONT_MATTER_UNREADABLE in line
        assert FRONT_MATTER_UNCLOSED not in line

    def test_ingest_conversion_says_no_opening_fence(self, tmp_path, capsys):
        """An HTML-provenance conversion is a plain absence, not a damaged block.

        These are the ordinary majority of "no block" files, so folding them in
        with the fence complaints would warn about a fix on every one of them.
        """
        line = self._skip_line(
            tmp_path, capsys, "doc.html.md",
            lambda p: p.write_text("<!-- ingested-by-factlog -->\nbody\n", encoding="utf-8"))
        assert FRONT_MATTER_NO_OPENING_FENCE in line
        assert FRONT_MATTER_UNCLOSED not in line

    def test_present_but_keyless_block_is_its_own_note(self, tmp_path, capsys):
        """``---\\n---`` locates a block with no keys: an absence reason would misread it.

        ``front_matter_absence`` returns None for a real (empty) block, so the note
        must not claim the fence is missing from a file that closes it.
        """
        line = self._skip_line(
            tmp_path, capsys, "empty.md",
            lambda p: p.write_text("---\n---\n\nbody\n", encoding="utf-8"))
        assert "no recognizable keys" in line
        assert FRONT_MATTER_UNCLOSED not in line
        assert FRONT_MATTER_NO_OPENING_FENCE not in line

    def test_the_reasons_are_distinct_across_causes(self, tmp_path, capsys):
        """Four causes in one KB produce four different messages, none the old blunt one.

        The per-cause tests each pin one reason; this pins that a single run keeps
        them apart, which is the whole point of splitting the message.
        """
        kb = _kb(tmp_path)
        _src(kb, "unclosed.md", '---\ntitle: "T"\n\nbody\n')
        pad = "x" * FRONT_MATTER_MAX_CHARS
        _src(kb, "huge.md", f'---\ntitle: "T"\nauthors: {pad}\n---\n\nbody\n')
        (kb / "sources" / "mojibake.md").write_bytes(b'---\ntitle: "T"\n\xff\xfe\n')
        _src(kb, "plain.md", "just prose, no fence\n")
        rc = cli.main(["export", "--bibtex", "--target", str(kb)])
        assert rc == 0
        err = capsys.readouterr().err
        reasons = {FRONT_MATTER_UNCLOSED, FRONT_MATTER_UNSCANNED,
                   FRONT_MATTER_UNREADABLE, FRONT_MATTER_NO_OPENING_FENCE}
        assert all(r in err for r in reasons), err
        assert "no YAML front matter" not in err
