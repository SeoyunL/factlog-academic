# SPDX-License-Identifier: Apache-2.0
"""Reject control chars dl_string encodes as wirelog-undecodable escapes (#331).

dl_string is json.dumps; wirelog decodes only \\" and \\\\, so a \\t/\\n/\\uXXXX escape
(the C0 range U+0000-U+001F) is stored by the engine as a literal backslash+letter -
Python holds 'Fig<TAB>2', the engine holds 'Fig\\t2', their intern ids never meet, and the
value silently drops out of every query (the #308 witness even decodes to a bare integer).
compile refuses such a fact up front. candidates.csv still loads (so amend/eject can fix
the row), and U+0085/U+2028/U+2029 round-trip and are never rejected (#255, verified).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from factlog import common

HEADER = "subject,relation,object,source,status,confidence,note"


class TestWirelogUndecodableChars:
    """Pure helper - no engine, runs everywhere."""

    @pytest.mark.parametrize("ch", ["\t", "\n", "\r", "\b", "\f", "\x00", "\x1f"])
    def test_c0_controls_are_flagged(self, ch):
        assert common.wirelog_undecodable_chars(f"a{ch}b") == [ch]

    @pytest.mark.parametrize("ch", ["\u0085", "\u2028", "\u2029", "\u007f", "\u009f"])
    def test_line_separators_and_high_controls_round_trip(self, ch):
        # json.dumps(ensure_ascii=False) leaves these raw and wirelog parses them fine.
        assert common.wirelog_undecodable_chars(f"a{ch}b") == []

    def test_ordinary_text_is_clean(self):
        assert common.wirelog_undecodable_chars('q"z back\\slash café a b 값') == []

    def test_reports_each_distinct_control_sorted(self):
        # Sorted by code point: TAB (U+0009) before LF (U+000A); duplicates collapsed.
        assert common.wirelog_undecodable_chars("a\tb\nc\t") == ["\t", "\n"]


def _kb(tmp_path, rows):
    kb = tmp_path / "kb"
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        capture_output=True, check=True,
    )
    (kb / "sources" / "x.md").write_text("x\n")
    (kb / "facts" / "candidates.csv").write_text(
        "\n".join([HEADER, *rows]) + "\n", encoding="utf-8"
    )
    return kb


def _compile(kb):
    return subprocess.run(
        [sys.executable, str(Path("tools") / "compile_facts.py")],
        capture_output=True, text=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
    )


@pytest.mark.parametrize("field_idx,label", [(0, "subject"), (1, "relation"), (2, "object")])
def test_compile_rejects_a_tab_in_any_field(tmp_path, field_idx, label):
    parts = ["Fig", "cites", "Smith2020"]
    parts[field_idx] = parts[field_idx][:1] + "\t" + parts[field_idx][1:]  # embed a real tab
    row = ",".join(parts) + ",sources/x.md,confirmed,0.9,"
    kb = _kb(tmp_path, [row])
    proc = _compile(kb)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "control character" in proc.stderr and label in proc.stderr, proc.stderr
    assert "#331" in proc.stderr
    # The undecodable value never reaches the engine's trusted input.
    assert not (kb / "facts" / "accepted.dl").exists()


def test_compile_accepts_line_separators_that_round_trip(tmp_path):
    # U+2028 / U+0085 are NOT rejected - json keeps them raw and wirelog parses them.
    obj = "line1\u2028line2\u0085end"
    kb = _kb(tmp_path, [f"Doc,note,{obj},sources/x.md,confirmed,0.9,"])
    proc = _compile(kb)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    accepted = (kb / "facts" / "accepted.dl").read_text(encoding="utf-8")
    assert "\u2028" in accepted and "\u0085" in accepted  # kept raw, on one physical line


def test_candidates_csv_still_loads_a_tab_bearing_row(tmp_path):
    # The reject is at COMPILE, not at load: load_facts must still return the row so the
    # human gate (amend/eject) can correct it.
    kb = _kb(tmp_path, ["Fig\t2,cites,Smith2020,sources/x.md,confirmed,0.9,"])
    out = subprocess.run(
        [sys.executable, "-c",
         "import os, sys; sys.path.insert(0, os.getcwd())\n"
         "import factlog.common as c\n"
         "rows = c.load_facts()\n"
         "print(len(rows), repr(rows[0]['subject']))"],
        capture_output=True, text=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "1 'Fig\\t2'" in out.stdout, out.stdout


class TestCanonicalNameControlChars:
    """#357: the gate must also reject a control char authored INTO a canonical relation
    name via policy/relation-aliases.md. #331 only checks a fact row's subject/relation/
    object; a canonical name is derived from the alias policy, so a tab authored there
    reaches accepted.dl as a wirelog-undecodable escape through the canonical/3 EDB atom —
    the same silent identity loss, via the policy-authoring path.
    """

    def _kb_with_alias(self, tmp_path, canonical, rows):
        kb = tmp_path / "kb"
        subprocess.run(
            [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
            capture_output=True, check=True,
        )
        (kb / "sources" / "x.md").write_text("x\n")
        # `cites` is the alias KEY; canonical is the value under test. The fact below uses
        # `cites`, so canonical_atoms emits a canonical/3 atom carrying `canonical`.
        (kb / "policy" / "relation-aliases.md").write_text(
            f"# Relation aliases\n- `cites` -> `{canonical}`\n", encoding="utf-8"
        )
        (kb / "facts" / "candidates.csv").write_text(
            "\n".join([HEADER, *rows]) + "\n", encoding="utf-8"
        )
        return kb

    def test_compile_rejects_a_tab_in_a_canonical_name(self, tmp_path):
        # The fact fields are all CLEAN — only the canonical name (from the alias policy)
        # carries a tab, so only the #357 policy-path check can catch it.
        kb = self._kb_with_alias(
            tmp_path, "canon\tname",
            ["Fig,cites,Smith2020,sources/x.md,confirmed,0.9,"],
        )
        proc = _compile(kb)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "control character" in proc.stderr, proc.stderr
        assert "canonical relation name" in proc.stderr, proc.stderr
        assert "#357" in proc.stderr and "relation-aliases.md" in proc.stderr, proc.stderr
        # The undecodable canonical atom never reaches the engine's trusted input.
        assert not (kb / "facts" / "accepted.dl").exists()

    def test_compile_rejects_a_declared_but_unused_canonical_name(self, tmp_path):
        # #363: the #357 gate lived inside the canonical/3 emission loop, so a tab-bearing
        # canonical name whose alias key no fact uses was never visited and compile passed
        # rc 0. Nothing leaked — no canonical atom is emitted without a participating fact —
        # but detection was deferred to whenever such a fact appears. The declaration alone
        # must fail loud, so the policy defect surfaces where it was authored.
        kb = self._kb_with_alias(
            tmp_path, "canon\tname",
            # `mentions` is not the alias key `cites`: no fact participates in the alias.
            ["Fig,mentions,Smith2020,sources/x.md,confirmed,0.9,"],
        )
        proc = _compile(kb)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "control character" in proc.stderr, proc.stderr
        assert "canonical relation name" in proc.stderr, proc.stderr
        assert "relation-aliases.md" in proc.stderr, proc.stderr
        assert not (kb / "facts" / "accepted.dl").exists()

    def test_compile_accepts_a_clean_canonical_name(self, tmp_path):
        # A normal alias policy compiles with no regression; the canonical/3 atom is written.
        kb = self._kb_with_alias(
            tmp_path, "cited_by_paper",
            ["Fig,cites,Smith2020,sources/x.md,confirmed,0.9,"],
        )
        proc = _compile(kb)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        accepted = (kb / "facts" / "accepted.dl").read_text(encoding="utf-8")
        assert "canonical(" in accepted and "cited_by_paper" in accepted, accepted


# --- Engine-backed reproduction: WHY the gate exists -------------------------
try:
    import pyrewire  # noqa: F401

    _HAVE_ENGINE = True
except ImportError:  # pragma: no cover - depends on the install
    _HAVE_ENGINE = False


@pytest.mark.skipif(not _HAVE_ENGINE, reason="pyrewire not installed")
def test_a_tab_value_corrupts_the_witness_the_gate_now_prevents(tmp_path):
    # Reproduce, through the production run_wirelog() path, the corruption the gate stops:
    # hand-write accepted.dl exactly as dl_string would emit a tab-bearing subject (the
    # gate is bypassed here on purpose), then read the #308 witness relation_alive. The
    # engine parsed 'Fig\\t2' (literal backslash-t), so it decodes to a bare integer id
    # instead of the Python 'Fig<TAB>2'. A plain subject decodes back to itself.
    kb = tmp_path / "kb"
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        capture_output=True, check=True,
    )
    (kb / "sources" / "x.md").write_text("x\n")
    script = (
        "import os, sys; sys.path.insert(0, os.getcwd())\n"
        "import factlog.common as c\n"
        "def witness(pyval):\n"
        "    c.ACCEPTED_DL.write_text('relation(' + c.dl_string(pyval) + ', \"cites\", \"X\").' + chr(10), encoding='utf-8')\n"
        "    alive = c.run_wirelog().get('relation_alive', set())\n"
        "    return {str(t[0]) for t in alive if t}\n"
        "tab = witness('Fig\\t2')\n"
        "plain = witness('Fig2')\n"
        "print('TAB_HOLDS_PYVAL', 'Fig\\t2' in tab)\n"
        "print('PLAIN_HOLDS_PYVAL', 'Fig2' in plain)\n"
        "print('TAB_WITNESS', sorted(tab))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
    )
    assert out.returncode == 0, out.stdout + out.stderr
    # The plain subject round-trips to the witness; the tab subject does NOT — it is lost,
    # decoded to a bare id. That silent identity loss is why compile rejects it (#331).
    assert "PLAIN_HOLDS_PYVAL True" in out.stdout, out.stdout + out.stderr
    assert "TAB_HOLDS_PYVAL False" in out.stdout, out.stdout + out.stderr
