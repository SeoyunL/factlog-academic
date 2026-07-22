# SPDX-License-Identifier: Apache-2.0
"""Unit tests for how much of a source ``common/front_matter`` reads (#409).

The reader used to take a fixed 2048-byte window, which a long ``authors:`` line
pushed the later keys straight out of: 50 authors already cost ``imported_from``
and 60 cost ``year`` and ``journal``. The identity keys survived because the
writers emit them first, so the damage was invisible from the ID-keyed paths and
landed on the title+author+year fallback instead.

The fix reads to the closing fence. These tests pin that, the chunking that makes
it work (a fence astride a chunk boundary), and the key set each of the eleven
call sites actually asks for — they read different keys, so a fixture that keeps
one consumer whole says nothing about the next.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from factlog.integrations.arxiv.source_writer import ArxivSourceWriter
from factlog.integrations.arxiv.work_parser import ParsedArxivWork
from factlog.integrations.common.backfill import IMPORTED_AT_KEY
from factlog.front_matter_scan import (
    FRONT_MATTER_CHUNK_CHARS,
    FRONT_MATTER_MAX_CHARS,
    front_matter_block,
)
from factlog.integrations.common.front_matter import (
    read_first_author,
    read_scalar,
    read_scalars,
)
from factlog.integrations.common.source_writer import IMPORTED_FROM_KEY
from factlog.integrations.openalex.refresh import RETRACTION_KEY as OPENALEX_RETRACTION_KEY
from factlog.integrations.openalex.source_writer import OpenAlexSourceWriter
from factlog.integrations.openalex.work_parser import ParsedWork
from factlog.integrations.pubmed.refresh import (
    RETRACTION_KEY as PUBMED_RETRACTION_KEY,
    RETRACTION_NOTICE_KEY as PUBMED_RETRACTION_NOTICE_KEY,
)
from factlog.integrations.pubmed.source_writer import PubMedSourceWriter
from factlog.integrations.pubmed.work_parser import ParsedPubMedWork
from factlog.integrations.zotero._textio import ANNOTATION_MARKER_RE
from factlog.integrations.zotero.item_parser import parse_item
from factlog.integrations.zotero.source_writer import SourceWriter as ZoteroSourceWriter

# The collaboration size the issue measured with. Every fixture below uses it, so
# the front matter under test is the one that actually broke.
N_AUTHORS = 200

# The window the reader used to take. Kept explicit: a fixture whose block fits
# inside it would pass even against the unfixed reader.
OLD_SCAN_BYTES = 2048

_AUTHORS = tuple(f"Author {i} of a large collaboration" for i in range(N_AUTHORS))


# --------------------------------------------------------------------------
# Fixtures written by the real writers, so the keys and their *order* are the
# ones a KB carries.
# --------------------------------------------------------------------------


def _arxiv_md() -> str:
    return ArxivSourceWriter().render(ParsedArxivWork(
        arxiv_id="2012.05876",
        version=2,
        title="Neurosymbolic AI: the 3rd wave",
        authors=_AUTHORS,
        abstract="An arXiv deposit.",
        primary_category="cs.AI",
        categories=("cs.AI",),
        submitted=date(2020, 12, 10),
        last_updated=date(2020, 12, 11),
        journal_ref="Nature 585, 357 (2020)",
        withdrawn_by="v3",
    ), imported_at="2026-07-22T00:00:00Z")


def _openalex_md() -> str:
    return OpenAlexSourceWriter().render(ParsedWork(
        openalex_id="W2741809807",
        title="A large collaboration",
        authors=_AUTHORS,
        year=2020,
        journal="Nature",
        doi="10.1038/s41586-020-2649-2",
        work_type="article",
        openalex_is_retracted=True,
    ), imported_at="2026-07-22T00:00:00Z")


def _pubmed_md() -> str:
    return PubMedSourceWriter().render(ParsedPubMedWork(
        pmid="16354850",
        title="A large collaboration",
        authors=_AUTHORS,
        journal="Chest",
        year=2005,
        doi="10.1378/chest.128.6.3817",
        retracted=True,
        retraction_notice_pmid="16354851",
    ), imported_at="2026-07-22T00:00:00Z")


def _zotero_md() -> str:
    return ZoteroSourceWriter().render(parse_item({
        "key": "ABCD1234",
        "data": {
            "itemType": "journalArticle",
            "title": "A large collaboration",
            "creators": [
                {"creatorType": "author", "lastName": f"Author{i}", "firstName": "A"}
                for i in range(N_AUTHORS)
            ],
            "date": "2005-03-01",
            "publicationTitle": "Chest",
            "DOI": "10.1378/chest.x",
        },
    }), imported_at="2026-07-22T00:00:00Z")


WRITERS = {
    "arxiv": _arxiv_md,
    "openalex": _openalex_md,
    "pubmed": _pubmed_md,
    "zotero": _zotero_md,
}

# The same writers as objects, so a test can ask one which keys it scans for
# instead of restating them.
WRITER_INSTANCES = {
    "arxiv": ArxivSourceWriter(),
    "openalex": OpenAlexSourceWriter(),
    "pubmed": PubMedSourceWriter(),
    "zotero": ZoteroSourceWriter(),
}


def _scalar_keys_of(kind: str) -> set[str]:
    """The top-level keys the writer emits with a *scalar* value.

    A ``[...]`` flow list (``authors``, ``tags``) is what ``read_scalars`` is
    documented not to read, so asking for one would pin the wrong contract.
    """
    return {
        line.split(":", 1)[0]
        for line in WRITERS[kind]().split("---")[1].splitlines()
        if line[:1].isalpha() and ":" in line
        and not line.split(":", 1)[1].strip().startswith("[")
    }


@pytest.fixture
def source(tmp_path):
    """Write one writer's 200-author render and hand back its path."""

    def _write(kind: str):
        path = tmp_path / f"{kind}.md"
        text = WRITERS[kind]()
        path.write_text(text, encoding="utf-8")
        # Guards the guard: a block inside the old window would pass unfixed.
        block = text.split("---")[1]
        assert len(block.encode()) > OLD_SCAN_BYTES, f"{kind} block fits the old window"
        return path

    return _write


class TestReadsToTheClosingFence:
    @pytest.mark.parametrize("kind", sorted(WRITERS))
    def test_every_emitted_key_survives_a_large_collaboration(self, kind, source):
        """No key the writer emitted falls off the end of the read.

        The old window cut at a byte count, so which keys survived depended on
        where the writer happened to put its ``authors:`` line. Asked key by key
        against the writer's own output, nothing is left to that accident.
        """
        path = source(kind)
        block = front_matter_block(path)
        assert block is not None
        emitted = sorted(_scalar_keys_of(kind))
        assert emitted, "fixture emitted no scalar keys"
        found = read_scalars(path, emitted)
        missing = [key for key in emitted if key not in found]
        assert not missing, f"{kind}: lost to truncation: {missing}"

    def test_body_below_the_fence_is_not_front_matter(self, tmp_path):
        """A ``key:`` line in the body never becomes a value, and costs nothing.

        Pins both halves of a fence-terminated read: the block ends at the fence,
        and a body far larger than any fixed window is not read to reach that
        conclusion.
        """
        path = tmp_path / "fenced.md"
        body = "\n".join(f"body_key_{i}: not front matter" for i in range(20_000))
        path.write_text(f'---\ntitle: "T"\nyear: "2020"\n---\n\n{body}\n', encoding="utf-8")
        assert path.stat().st_size > FRONT_MATTER_CHUNK_CHARS

        assert read_scalars(path, ("title", "year", "body_key_0")) == {
            "title": "T", "year": "2020",
        }

    def test_missing_opening_fence_does_not_read_the_body(self, tmp_path, monkeypatch):
        """The opening-fence check is a read budget, not just a shortcut.

        The writers' caches walk every ``.md`` under the source root, including
        ingest conversions that carry an HTML provenance comment instead of YAML.
        With no opening fence there is no closing fence to find either, so without
        this check the search would run to the cap on every such file.
        """
        path = tmp_path / "converted.md"
        path.write_text("<!-- provenance -->\n" + "filler line\n" * 200_000, encoding="utf-8")
        size = path.stat().st_size
        assert size > FRONT_MATTER_MAX_CHARS

        read = _chars_read(monkeypatch)
        assert front_matter_block(path) is None
        assert read[0] < size / 10, f"read {read[0]} chars of a {size}-byte body"

    def test_unreadable_path_has_no_front_matter(self, tmp_path):
        """OSError degrades to "no front matter", it does not raise at the caller."""
        assert front_matter_block(tmp_path / "does-not-exist.md") is None
        assert read_scalars(tmp_path / "does-not-exist.md", ("title",)) == {}


def _chars_read(monkeypatch) -> list[int]:
    """Instrument ``Path.open`` so a test can assert how much a read cost."""
    total = [0]
    real_open = Path.open

    def counting_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        real_read = handle.read

        def read(size=-1):
            data = real_read(size)
            total[0] += len(data)
            return data

        handle.read = read
        return handle

    monkeypatch.setattr(Path, "open", counting_open)
    return total


class TestChunking:
    # A body far larger than the cap, so "kept reading" and "stopped at the fence"
    # differ by two orders of magnitude rather than by a rounding error.
    _BIG_BODY = "leak: leaked\n" * 300_000

    @classmethod
    def _fenced_at(cls, offset: int) -> str:
        """A source whose closing fence starts exactly at character ``offset``."""
        head = '---\ntitle: "T"\n'
        pad = "p: " + "x" * (offset - len(head) - 4) + "\n"
        assert len(head + pad) == offset
        return head + pad + '\n---\n\n' + cls._BIG_BODY

    @pytest.mark.parametrize("offset", range(-3, 4))
    def test_fence_straddling_a_chunk_boundary_stops_the_read(self, tmp_path, offset,
                                                              monkeypatch):
        """A fence astride a chunk boundary still ends the read at that boundary.

        The reader pulls ``FRONT_MATTER_CHUNK_CHARS`` at a time, so a ``\\n---``
        sitting across a boundary is split between two of them. The loop tests the
        *accumulated* text for exactly this case.

        What that buys is a **read budget, not the fence**. The extraction after
        the loop searches all of ``head`` regardless, so a loop that only looked at
        the latest chunk would still find this fence and return the same block —
        it would just keep reading to the cap first. Asserting on the block alone
        therefore pins nothing: only the read volume separates the two.

        The offsets are computed *from the constant*, so retuning the chunk size
        moves the fixture with it instead of silently aiming at nothing. They span
        the fence split across the boundary (-3..-1) and the fence landing at the
        head of the second chunk, where the ``[3:]`` slice would swallow it (0..2).
        ``+3`` is the control: it is the offset where nothing is sliced away, so a
        latest-chunk-only loop gets that one right too. How many of the others it
        also survives depends on the shape of the mutation, which is the point of
        keeping the whole span rather than a single case.
        """
        path = tmp_path / f"straddle{offset}.md"
        path.write_text(self._fenced_at(FRONT_MATTER_CHUNK_CHARS + offset), encoding="utf-8")
        assert path.stat().st_size > FRONT_MATTER_MAX_CHARS

        read = _chars_read(monkeypatch)
        found = read_scalars(path, ("title", "leak"))
        assert found.get("title") == "T"
        assert "leak" not in found, "body key past the closing fence leaked in"
        # The fence is at most one character past the first boundary, so two chunks
        # always suffice. Reading a third means the loop stopped noticing it.
        assert read[0] <= 2 * FRONT_MATTER_CHUNK_CHARS, (
            f"read {read[0]} chars for a fence at {FRONT_MATTER_CHUNK_CHARS + offset}")

    def test_chunk_size_must_cover_the_opening_fence(self):
        """A chunk under 3 chars cannot see ``---``, so every source reads as empty.

        Pins the lower bound the constant's comment documents: the opening-fence
        test runs on the *first* read alone, so a chunk of 1 or 2 makes
        ``startswith("---")`` false for a perfectly well-formed file.
        """
        assert FRONT_MATTER_CHUNK_CHARS >= 3

    def test_the_search_for_a_fence_stops_at_the_cap(self, tmp_path, monkeypatch):
        """A file with no closing fence is read to the cap and no further.

        The cap bounds the *search*; the loop checks the length before reading, so
        the ceiling is the cap rounded up to a whole chunk.
        """
        path = tmp_path / "unclosed.md"
        path.write_text('---\ntitle: "T"\n' + "filler line\n" * 400_000, encoding="utf-8")
        assert path.stat().st_size > 3 * FRONT_MATTER_MAX_CHARS

        read = _chars_read(monkeypatch)
        assert front_matter_block(path) is None
        ceiling = -(-FRONT_MATTER_MAX_CHARS // FRONT_MATTER_CHUNK_CHARS) * FRONT_MATTER_CHUNK_CHARS
        assert read[0] <= ceiling


class TestUnclosedBlockCarriesNothing:
    """A block with no closing fence yields nothing, in both directions.

    Its extent is unknowable, so a key read out of it may be a body line. Handing
    those to the writers' caches registers a stranger's note under a paper's
    identity; the recoverable cost of refusing is a duplicate ``.md``.
    """

    @staticmethod
    def _unclosed(tmp_path, body: str) -> Path:
        tmp_path.mkdir(parents=True, exist_ok=True)
        path = tmp_path / "note.md"
        path.write_text(f'---\ntitle: "A user note"\n\n{body}\n', encoding="utf-8")
        return path

    @pytest.mark.parametrize("key,value", [
        ("arxiv_id", "2012.05876"),
        ("doi", "10.1038/s41586-020-2649-2"),
        ("openalex_id", "W2741809807"),
        ("zotero_key", "ABCD1234"),
        ("pmid", "16354850"),
    ])
    def test_a_body_identity_key_is_not_an_identity(self, tmp_path, key, value):
        """The registration path: `by_identity`/`by_cross_id` must not see these.

        `common/source_writer.py:442` registers whatever identity `read_scalars`
        reports. A user's own note that opens with `---` and never closes it would
        otherwise bind a real paper's id to that note, and the next import of the
        real paper is skipped or paired against it.
        """
        path = self._unclosed(tmp_path, f"Quoting a paper:\n{key}: {value}")
        assert read_scalars(path, (key,)) == {}
        assert read_scalar(path, key) == ""

    def test_the_matcher_gets_no_row_either(self, tmp_path):
        """The fallback path: no title/year/author, so `_match_row` fails closed."""
        path = self._unclosed(
            tmp_path, 'year: 2020\nimported_from: arxiv\nauthors: ["Ada Lovelace"]')
        assert read_scalars(path, ("year", IMPORTED_FROM_KEY, "title")) == {}
        assert read_first_author(path) == ""

    def test_an_ignore_marker_in_the_body_cannot_be_reached(self, tmp_path):
        """`ignore_re` no longer depends on where in the body a marker line sits.

        Widening the read made a body `source_kind: annotations` visible to
        Zotero's `ANNOTATION_MARKER_RE`, which reads the whole file as carrying
        nothing — while the same file with that line inside the old 2048-byte
        window read as nothing before the change too. The answer was decided by an
        offset. It is now decided by the missing fence: nothing, either way.
        """
        near = self._unclosed(tmp_path / "near", "source_kind: annotations")
        far = self._unclosed(tmp_path / "far", "x\n" * 5_000 + "source_kind: annotations")
        for path in (near, far):
            assert read_scalar(path, "zotero_key", ANNOTATION_MARKER_RE) == ""

    def test_a_closed_block_is_unaffected_by_its_body(self, tmp_path):
        """The counterpart: close the fence and every one of those reads works.

        Without this the tests above would also pass if the reader had simply
        stopped working.

        This is a control, not a repaired case. A closed block never contained its
        body under *any* version of the reader — the marker below the fence has
        never been reachable by `ignore_re`, and this assertion held before the
        fence-terminated read and before fail-closed alike. The only behaviour
        `read_scalar` changed here is on the unclosed files above.
        """
        path = tmp_path / "closed.md"
        path.write_text(
            '---\ntitle: "A user note"\nzotero_key: "ABCD1234"\n---\n\n'
            "arxiv_id: 2012.05876\nsource_kind: annotations\n", encoding="utf-8")
        assert read_scalar(path, "zotero_key", ANNOTATION_MARKER_RE) == "ABCD1234"
        assert read_scalar(path, "arxiv_id") == ""

    def test_undecodable_bytes_are_no_front_matter(self, tmp_path):
        """Mojibake past the first chunk must not raise at the caller.

        Reading further than the old window puts more of a file through the codec,
        so a source whose head is valid UTF-8 and whose body is not used to decode
        (the bad bytes sat past 2048) and would now raise `UnicodeDecodeError` —
        which is a `ValueError`, not the `OSError` the callers are shielded from.
        """
        path = tmp_path / "mojibake.md"
        path.write_bytes(b'---\ntitle: "T"\n' + b"filler line\n" * 1_000 + b"\xff\xfe\n")
        assert front_matter_block(path) is None
        assert read_scalars(path, ("title",)) == {}
        assert read_first_author(path) == ""


class TestEveryConsumer:
    """Each call site's own key set, on its own source's 200-author front matter.

    Eleven call sites across ten files read eleven different key sets, so keeping
    one whole says nothing about the next — these ask for exactly what each one
    asks for.
    """

    def test_openalex_importer_reads_the_work_id(self, source):
        # factlog/integrations/openalex/importer.py:156
        assert read_scalar(source("openalex"), "openalex_id") == "W2741809807"

    def test_openalex_backfill_reads_the_retraction_flag(self, source):
        # factlog/integrations/openalex/backfill.py:148
        assert read_scalar(source("openalex"), OPENALEX_RETRACTION_KEY) == "true"

    def test_openalex_refresh_reads_its_compare_keys(self, source):
        # factlog/integrations/openalex/refresh.py:327
        keys = ("openalex_id", "type", "doi", "journal", OPENALEX_RETRACTION_KEY)
        assert set(read_scalars(source("openalex"), keys)) == set(keys)

    def test_pubmed_refresh_reads_its_compare_keys(self, source):
        # factlog/integrations/pubmed/refresh.py:333
        keys = ("pmid", "doi", "journal",
                PUBMED_RETRACTION_KEY, PUBMED_RETRACTION_NOTICE_KEY)
        assert set(read_scalars(source("pubmed"), keys)) == set(keys)

    def test_pubmed_backfill_reads_its_view_keys(self, source):
        # factlog/integrations/pubmed/backfill.py:230
        keys = ("pmid", "doi", "journal",
                PUBMED_RETRACTION_KEY, PUBMED_RETRACTION_NOTICE_KEY)
        assert set(read_scalars(source("pubmed"), keys)) == set(keys)

    def test_zotero_source_writer_reads_the_item_key(self, source):
        # factlog/integrations/zotero/source_writer.py:58 — with the annotation
        # marker, which must not match a full-length imported record.
        assert read_scalar(source("zotero"), "zotero_key", ANNOTATION_MARKER_RE) == "ABCD1234"

    def test_arxiv_check_versions_reads_the_version_keys(self, source):
        # factlog/integrations/arxiv/check_versions.py:472
        keys = ("arxiv_id", "arxiv_version", "arxiv_withdrawn_by")
        found = read_scalars(source("arxiv"), keys)
        assert found == {"arxiv_id": "2012.05876", "arxiv_version": "2",
                         "arxiv_withdrawn_by": "v3"}

    @pytest.mark.parametrize("kind,id_key", [
        ("openalex", "openalex_id"), ("pubmed", "pmid"),
        ("arxiv", "arxiv_id"), ("zotero", "zotero_key"),
    ])
    def test_provenance_reads_each_source_id(self, kind, id_key, source):
        # factlog/integrations/common/provenance.py:416
        assert read_scalars(source(kind), (id_key,)).get(id_key)

    @pytest.mark.parametrize("kind", sorted(WRITERS))
    def test_common_backfill_reads_the_import_timestamp(self, kind, source):
        # factlog/integrations/common/backfill.py:300 — `imported_at` is emitted
        # after the author list by every writer, so it was the first key lost.
        assert read_scalars(source(kind), (IMPORTED_AT_KEY,)).get(IMPORTED_AT_KEY)

    @pytest.mark.parametrize("kind", sorted(WRITERS))
    def test_source_writer_cache_reads_the_matcher_keys(self, kind, source):
        """factlog/integrations/common/source_writer.py:442, with its own key set.

        The keys come from the writer's ``_scan_keys()`` rather than a copy of
        today's list, so extending the scan extends this test. Only the ones the
        writer actually emits can be asserted — a Zotero record carries no
        ``arxiv_id`` — so the expectation is the intersection, checked non-empty.
        """
        path = source(kind)
        keys = WRITER_INSTANCES[kind]._scan_keys()
        emitted = _scalar_keys_of(kind)
        expected = {key for key in keys if key in emitted}
        assert expected, f"{kind}: scan keys and emitted keys do not overlap"
        found = read_scalars(path, keys)
        assert expected <= set(found), f"{kind}: missing {expected - set(found)}"

    @pytest.mark.parametrize("kind", sorted(WRITERS))
    def test_the_fallback_keys_survive(self, kind, source):
        """`imported_from` and `year` specifically — the title+author+year fallback.

        These are the keys the issue measured as lost (`imported_from` at 50
        authors, `year` at 60), and the only reason the defect was not visible
        from the ID-keyed paths. `_scan_keys()` above would still pass if the
        matcher stopped asking for them.
        """
        found = read_scalars(source(kind), (IMPORTED_FROM_KEY, "title", "year"))
        assert set(found) == {IMPORTED_FROM_KEY, "title", "year"}

    @pytest.mark.parametrize("kind", sorted(WRITERS))
    def test_source_writer_cache_reads_the_first_author(self, kind, source):
        # factlog/integrations/common/source_writer.py:405
        assert read_first_author(source(kind)) == _first_author_of(kind)


def _first_author_of(kind: str) -> str:
    """The name the writer put first — Zotero renders its own author strings."""
    return "Author0, A" if kind == "zotero" else _AUTHORS[0]
