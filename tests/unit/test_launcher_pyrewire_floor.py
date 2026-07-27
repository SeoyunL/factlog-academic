# SPDX-License-Identifier: Apache-2.0
"""Unit tests for #578: the launcher's pyrewire floor must not drift.

``tools/factlog_python.sh`` ranks interpreters by whether they carry pyrewire at
or above the engine floor. It cannot import factlog to learn that floor — it runs
*before* an interpreter is chosen — so it repeats the number as a literal. This
module is the pin that keeps the repeated literal equal to the Python-side
constants; without it a floor bump in ``factlog/cli.py`` would silently leave the
launcher ranking an under-floor engine as good enough, and ``finalize`` would
degrade to "Logic check SKIPPED" instead of running the logic check.

``docs/reference/windows.md`` and its English pair repeat the number a fourth
time, in prose, and were outside the pin — so a floor bump left the documented
selection rule describing a threshold the launcher no longer used. Same drift,
one file further out; they are inside the pin now.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import factlog.common as fcommon
from factlog import cli

_REPO = Path(__file__).resolve().parents[2]
_LAUNCHER = _REPO / "tools" / "factlog_python.sh"

# Prose that states the launcher's ranking floor as a literal. The floor is a
# fourth copy of the same number, in a fourth language, and nothing but this pin
# keeps it honest: bump the floor and these two files go quietly stale, which is
# the exact failure the module already guards against on the code side.
#
# The other `pyrewire 1.0.3` literals in the tree (README, docs/guide/install,
# pyproject) are deliberately NOT here. Those state the *packaging* requirement,
# which happens to be the same number today but is a different constant with a
# different owner; folding them in would pin two things that are free to diverge.
_FLOOR_DOCS = (
    Path("docs") / "reference" / "windows.md",
    Path("docs") / "reference" / "windows.en.md",
)

# "pyrewire 1.0.3 이상을" / "pyrewire 1.0.3 or newer" — the version right after
# the engine's name, so `py -3.12` and `Python 3.11+` in the same files are not
# swept up.
_DOC_FLOOR_RE = re.compile(r"pyrewire\s+(\d+(?:\.\d+)+)")


def _launcher_floor() -> tuple[int, ...]:
    """Read ``_PYREWIRE_FLOOR='x.y.z'`` out of the launcher as a version tuple."""
    match = re.search(r"^_PYREWIRE_FLOOR='([^']+)'$", _LAUNCHER.read_text(encoding="utf-8"), re.M)
    assert match is not None, "tools/factlog_python.sh no longer defines _PYREWIRE_FLOOR"
    return cli._version_tuple(match.group(1))


class TestLauncherPyrewireFloor:
    """The floor is stated in three places; all three must agree."""

    def test_matches_cli_min_pyrewire(self):
        assert _launcher_floor() == cli.MIN_PYREWIRE

    def test_matches_common_min_pyrewire_version(self):
        assert _launcher_floor() == fcommon.MIN_PYREWIRE_VERSION

    def test_floor_is_a_three_part_version(self):
        """A truncated literal like '1.0' would compare below '1.0.3' and pass
        the equality checks only if the constants were truncated too."""
        assert len(_launcher_floor()) == 3


class TestDocumentedFloor:
    """The user-facing prose states the same floor; it is inside the pin too."""

    @pytest.mark.parametrize("relative", _FLOOR_DOCS, ids=lambda p: p.name)
    def test_doc_states_the_floor(self, relative):
        found = _DOC_FLOOR_RE.findall((_REPO / relative).read_text(encoding="utf-8"))
        assert found, (
            f"{relative} no longer states the launcher's pyrewire floor. If the number was "
            "removed on purpose (e.g. reworded to 'the engine floor'), drop the file from "
            "_FLOOR_DOCS as well — leaving it here turns an intentional edit into a red suite."
        )
        for literal in found:
            assert cli._version_tuple(literal) == _launcher_floor(), (
                f"{relative} says pyrewire {literal}, launcher floor is "
                f"{'.'.join(str(p) for p in _launcher_floor())}"
            )

    def test_both_translations_agree(self):
        """ko/en are a pair: a floor bump applied to one file only is the same
        drift one step smaller."""
        literals = {
            relative: _DOC_FLOOR_RE.findall((_REPO / relative).read_text(encoding="utf-8"))
            for relative in _FLOOR_DOCS
        }
        assert len(set(map(tuple, literals.values()))) == 1, literals
