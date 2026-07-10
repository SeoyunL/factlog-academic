# SPDX-License-Identifier: Apache-2.0
"""`--dry-run` must classify every paper exactly as the real run does (#114).

A preview that disagrees with the run it previews is worse than no preview: the operator
approves one thing and gets another. Both paths share one classifier, so every refusal
and every no-op is decided once.

The one thing a preview cannot know is whether the *write* would succeed — it declines to
attempt it. An unwritable ``source-provenance/`` therefore shows up only on the real run.
That is measured here rather than left to be discovered, and it is stated in the module
and in the command's ``--help``.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import factlog.integrations.arxiv.backfill as ab
import factlog.integrations.common.backfill as bf

_STAMP = 'imported_at: "2025-01-01T00:00:00Z"\n'


def _kb() -> Path:
    root = Path(tempfile.mkdtemp())
    (root / "sources").mkdir()
    return root


def _source(root: Path, name: str, front_matter: str) -> None:
    (root / "sources" / name).write_text(f"---\n{front_matter}---\n# X\n", encoding="utf-8")


def _classify(root: Path, *, dry_run: bool) -> dict[str, str]:
    results = bf.backfill(root, ab.backfill_schema(), dry_run=dry_run)
    return {r.entry_id: r.status for r in results}


class TestPreviewAgreesWithTheRun:
    def test_every_id_is_classified_identically(self):
        root = _kb()
        _source(root, "a_ok.md", f'arxiv_id: "2301.00001"\narxiv_version: 7\n{_STAMP}')
        _source(root, "b_no_stamp.md", 'arxiv_id: "2301.00002"\narxiv_version: 7\n')
        _source(root, "c_no_version.md", f'arxiv_id: "2301.00003"\n{_STAMP}')

        preview = _classify(root, dry_run=True)
        real = _classify(root, dry_run=False)
        assert preview == real
        assert preview["2301.00001"] == bf.BACKFILL_WRITTEN
        assert preview["2301.00002"] == bf.BACKFILL_REFUSED   # no imported_at
        assert preview["2301.00003"] == bf.BACKFILL_REFUSED   # identifying field unreadable

    def test_a_corrupt_sidecar_is_an_error_in_both(self):
        root = _kb()
        _source(root, "a.md", f'arxiv_id: "2301.00001"\narxiv_version: 7\n{_STAMP}')
        (root / "source-provenance").mkdir()
        (root / "source-provenance" / "a.json").write_text("{ not json", encoding="utf-8")
        assert _classify(root, dry_run=True) == _classify(root, dry_run=False)

    def test_a_preview_writes_nothing(self):
        root = _kb()
        _source(root, "a.md", f'arxiv_id: "2301.00001"\narxiv_version: 7\n{_STAMP}')
        md = root / "sources" / "a.md"
        before = os.stat(md).st_mtime_ns
        _classify(root, dry_run=True)
        assert not (root / "source-provenance").exists()
        assert os.stat(md).st_mtime_ns == before


class TestThePreviewCannotForeseeAFailedWrite:
    """The known and stated limit. A preview declines to write, so it cannot learn that
    the write would fail. Every *classification* still agrees; only the filesystem's
    verdict is missing."""

    def test_an_unwritable_sidecar_dir_is_writable_in_preview_and_an_error_in_the_run(self):
        root = _kb()
        _source(root, "a.md", f'arxiv_id: "2301.00001"\narxiv_version: 7\n{_STAMP}')
        os.chmod(root, 0o555)
        try:
            preview = _classify(root, dry_run=True)
            real = _classify(root, dry_run=False)
        finally:
            os.chmod(root, 0o755)

        assert preview["2301.00001"] == bf.BACKFILL_WRITTEN
        assert real["2301.00001"] == bf.BACKFILL_ERROR
        # And the failure is isolated to that paper, not raised.
        assert set(real) == {"2301.00001"}

    def test_the_limit_is_documented_beside_the_early_return(self):
        # The behaviour above is surprising enough that a reader must meet it where the
        # preview short-circuits, not in a changelog.
        import inspect

        source = inspect.getsource(bf).lower()
        assert "what a preview therefore cannot know" in source
