# SPDX-License-Identifier: Apache-2.0
"""Unit tests for #578: the launcher's pyrewire floor must not drift.

``tools/factlog_python.sh`` ranks interpreters by whether they carry pyrewire at
or above the engine floor. It cannot import factlog to learn that floor — it runs
*before* an interpreter is chosen — so it repeats the number as a literal. This
module is the pin that keeps the repeated literal equal to the Python-side
constants; without it a floor bump in ``factlog/cli.py`` would silently leave the
launcher ranking an under-floor engine as good enough, and ``finalize`` would
degrade to "Logic check SKIPPED" instead of running the logic check.
"""
from __future__ import annotations

import re
from pathlib import Path

import factlog.common as fcommon
from factlog import cli

_LAUNCHER = Path(__file__).resolve().parents[2] / "tools" / "factlog_python.sh"


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
