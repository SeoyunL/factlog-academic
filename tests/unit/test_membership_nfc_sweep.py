# SPDX-License-Identifier: Apache-2.0
"""Remaining single-valued MEMBERSHIP sites fold to NFC (#293).

The pure completion of #285/#292: those hardened the loader and the
``detect_conflicts`` / ``value_audit`` membership gates, but two more consumers
still compared a fact's relation name against the loaded (NFC) single-valued set
on raw bytes, so an NFD-authored fact relation silently fell through:

  - ``tools/corroboration.py`` — the single-valued *competition* view (same
    subject+relation given different objects) never noticed the contest.
  - ``factlog vocab --relations`` — the ``[single-valued]`` tag was omitted.

Both are the fact-NFD mirror of #285 (an NFC declaration meeting an NFD fact).
These tests drive the real tools as subprocesses (their KB paths bind at import
from ``FACTLOG_ROOT``, exactly as in production and the shell harnesses) and are
red without the fold, green with it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unicodedata
from pathlib import Path

# NFC single-valued declaration vs the NFD spelling a macOS-authored fact carries.
_SV_NFC = "주속성"
_SV_NFD = unicodedata.normalize("NFD", _SV_NFC)

_HEADER = "subject,relation,object,source,status,confidence,note"


def _make_kb(tmp_path: Path) -> Path:
    kb = tmp_path / "kb"
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        capture_output=True,
        check=True,
    )
    (kb / "sources" / "a.md").write_text("a\n", encoding="utf-8")
    # NFC declaration; two NFD-authored fact rows, same subject+relation, distinct
    # objects — a single-valued competition once the relation is recognised.
    (kb / "policy" / "single-valued.md").write_text(
        f"# single-valued\n- {_SV_NFC}\n", encoding="utf-8"
    )
    rows = [
        f"P,{_SV_NFD},A,sources/a.md,accepted,0.9,",
        f"P,{_SV_NFD},B,sources/a.md,accepted,0.9,",
    ]
    (kb / "facts" / "candidates.csv").write_text(
        "\n".join([_HEADER, *rows]) + "\n", encoding="utf-8"
    )
    return kb


def _run(argv: list[str], kb: Path) -> str:
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
        check=True,
    )
    return proc.stdout + proc.stderr


def test_nfd_and_nfc_are_byte_distinct():
    # Guards the premise: an NFD/NFC-insensitive comparison would pass vacuously.
    assert _SV_NFD != _SV_NFC


def test_corroboration_sees_nfd_fact_single_valued_competition(tmp_path: Path):
    kb = _make_kb(tmp_path)
    out = _run([sys.executable, str(Path("tools") / "corroboration.py")], kb)

    # Without the NFC fold the competition view is silent; with it the NFD facts
    # are recognised as competing values of the NFC-declared single-valued relation.
    assert "single-valued relation(s) with competing values" in out
    # Reported under the verbatim (NFD) relation name — provenance preserved.
    assert f"P / {_SV_NFD}:" in out


def test_vocab_tags_nfd_fact_relation_single_valued(tmp_path: Path):
    kb = _make_kb(tmp_path)
    out = _run(
        [sys.executable, "-m", "factlog", "vocab", "--relations", "--target", str(kb)],
        kb,
    )

    # The NFD-authored relation surface must carry the [single-valued] tag, matching
    # the sibling [attribute] tag which already folds to NFC (is_attribute_relation).
    tagged = [ln for ln in out.splitlines() if _SV_NFD in ln]
    assert tagged, "the NFD relation should appear in the vocab listing"
    assert any("[single-valued]" in ln for ln in tagged), out
