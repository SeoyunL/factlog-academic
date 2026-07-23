# SPDX-License-Identifier: Apache-2.0
"""compile_facts writes facts/accepted.dl atomically.

A plain ``write_text`` interrupted mid-write leaves accepted.dl truncated at a byte
boundary. Because the truncation lands on a *line* boundary the file still parses
cleanly, so the engine evaluates over the surviving facts and the report passes with
``errors: 0`` while a confirmed fact silently answers ``0 rows``. The temp+replace
pattern makes that impossible: a reader sees either the prior snapshot or the complete new
file, never a partial one.

The pure-helper tests below run everywhere; the compile-level test drives the real
compile_facts through a simulated crash and pins that accepted.dl keeps its complete
prior snapshot rather than a truncated one.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from factlog import common


class TestAtomicWriteHelper:
    def test_normal_write_replaces_content(self, tmp_path):
        target = tmp_path / "accepted.dl"
        target.write_text("OLD\n", encoding="utf-8")
        common._atomic_write_text(target, "NEW\nCONTENT\n")
        assert target.read_text(encoding="utf-8") == "NEW\nCONTENT\n"
        # No temp file is left behind on the happy path.
        assert not (tmp_path / "accepted.dl.tmp").exists()

    def test_crash_before_replace_leaves_prior_snapshot(self, tmp_path, monkeypatch):
        target = tmp_path / "accepted.dl"
        target.write_text("COMPLETE\nSNAPSHOT\n", encoding="utf-8")

        def _boom(src, dst):
            raise OSError("simulated crash before replace")

        monkeypatch.setattr(common.os, "replace", _boom)
        with pytest.raises(OSError):
            common._atomic_write_text(target, "PARTIAL")
        # The destination is only ever touched by the atomic os.replace, so a crash
        # before it leaves the complete prior snapshot — never a truncated file.
        assert target.read_text(encoding="utf-8") == "COMPLETE\nSNAPSHOT\n"


def _seed_kb(tmp_path):
    kb = tmp_path / "kb"
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        capture_output=True, check=True,
    )
    (kb / "sources" / "a.md").write_text("a\n")
    rows = [(f"S{i}", "uses", f"O{i}") for i in range(6)]
    lines = ["subject,relation,object,source,status,confidence,note"]
    lines += [f"{s},{r},{o},sources/a.md,confirmed,0.9," for s, r, o in rows]
    (kb / "facts" / "candidates.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return kb


def _compile(kb, extra_script=""):
    return subprocess.run(
        [sys.executable, "-c",
         "import os, sys; sys.path.insert(0, os.getcwd())\n" + extra_script +
         "import factlog.compile_facts as cf\n"
         "try:\n"
         "    cf.main()\n"
         "except RuntimeError:\n"
         "    pass\n"],
        capture_output=True, text=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
    )


def test_interrupted_compile_never_leaves_a_truncated_accepted_dl(tmp_path):
    kb = _seed_kb(tmp_path)
    # A first, clean compile establishes the complete snapshot (6 relation rows).
    ok = _compile(kb)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    accepted = kb / "facts" / "accepted.dl"
    complete = accepted.read_text(encoding="utf-8")
    assert complete.count("relation(") == 6

    # A second compile crashes mid-write after only half the payload is persisted.
    # With a plain write_text that half lands in accepted.dl itself (truncated but
    # cleanly parseable); with temp+replace it lands only in accepted.dl.tmp and the
    # real file is untouched.
    crash = (
        "import pathlib\n"
        "_orig = pathlib.Path.write_text\n"
        "def _trunc(self, data, *a, **k):\n"
        "    if self.name in ('accepted.dl', 'accepted.dl.tmp'):\n"
        "        parts = data.split(chr(10))\n"
        "        half = chr(10).join(parts[: max(1, len(parts) // 2)])\n"
        "        _orig(self, half, *a, **k)\n"
        "        raise RuntimeError('simulated crash mid-write')\n"
        "    return _orig(self, data, *a, **k)\n"
        "pathlib.Path.write_text = _trunc\n"
    )
    _compile(kb, crash)

    # The invariant: accepted.dl is either the complete prior snapshot or a complete
    # new file — never a truncated one with fewer facts.
    after = accepted.read_text(encoding="utf-8")
    assert after == complete, (
        "interrupted compile left a truncated accepted.dl "
        f"({after.count('relation(')} relation rows, expected the complete 6)"
    )
