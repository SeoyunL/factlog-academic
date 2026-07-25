# SPDX-License-Identifier: Apache-2.0
"""Which factlog is running, and whether pip agrees (#554).

``factlog.runtime`` answers two questions that nothing in the toolchain used to
answer: what version/path is executing, and does the distribution pip recorded
still describe it. The first is one string, pinned here as a WHOLE line — a
substring check would pass on a mutant that dropped the path or the parentheses,
which are the only parts a reader can act on.

The second is a judgement with four states, and every one of them is pinned by
argument injection rather than by arranging a venv (the shape
``factlog.cli.rename_migration_check`` established). The state that matters most
is the one that is deliberately NOT a warning: a version bump with no reinstall
is every maintainer's daily state, and a row that fires there is a row people
learn to scroll past.

The severity contract is pinned separately and unconditionally: this diagnostic
may never return FAIL, because doctor's exit code is gated on FAIL alone and
smoke.sh/setup.sh depend on a healthy environment exiting 0.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import factlog
from factlog.runtime import (
    UNKNOWN,
    installation_skew,
    installed_factlog_dist,
    provenance_line,
    running_factlog,
)


class TestRunningFactlog:
    def test_reports_the_imported_packages_version_and_file(self):
        version, path = running_factlog()
        assert version == factlog.__version__
        assert path == factlog.__file__

    def test_path_is_not_resolved(self):
        """``__file__`` verbatim, symlinks and worktrees intact.

        A resolved path stops matching ``python -c "import factlog;
        print(factlog.__file__)"`` — the one command a reader runs to check the
        line, and the command #553's reproduction uses. Pinned by identity with
        ``__file__``, since resolving only differs when a link is in play and the
        test machine may have none.
        """
        _, path = running_factlog()
        assert path == factlog.__file__
        # And it is the module file, not the package directory.
        assert Path(path).name == "__init__.py"

    def test_missing_attributes_degrade_instead_of_raising(self, monkeypatch):
        # doctor is what you run when the install is broken; a namespace package or a
        # stripped __file__ must not take the diagnostic down with it.
        monkeypatch.delattr(factlog, "__file__", raising=False)
        monkeypatch.setattr(factlog, "__version__", None, raising=False)
        assert running_factlog() == (UNKNOWN, UNKNOWN)


class TestProvenanceLine:
    def test_whole_line_shape(self):
        """Whole-line equality, never ``in``.

        ``"factlog:" in line`` survives a mutant that drops the version, the path
        or the parentheses — i.e. every part of the line that carries information.
        """
        assert provenance_line() == f"factlog: {factlog.__version__} ({factlog.__file__})"

    def test_prefix_is_configurable_and_nothing_else_moves(self):
        assert provenance_line("run_logic_check") == (
            f"run_logic_check: {factlog.__version__} ({factlog.__file__})"
        )

    def test_line_is_single_and_has_no_trailing_whitespace(self):
        # It is inserted into a byte-compared report; a stray newline would shift
        # every line beneath it.
        line = provenance_line()
        assert "\n" not in line
        assert line == line.strip()


IMPORTED = "/work/tree/factlog/__init__.py"
IMPORTED_ROOT = "/work/tree"


class TestInstallationSkewStates:
    """The four states, one test each, all argument-injected."""

    def test_no_distribution_is_silent(self):
        # A plain source checkout with nothing installed. An absent yardstick is
        # not a discrepancy: warning here would fire for every `git clone`.
        assert installation_skew(None, None, None, "0.7.0", IMPORTED) is None

    def test_matching_root_and_version_is_silent(self):
        # The ordinary healthy install (wheel or editable, both land here).
        assert installation_skew(
            "factlog-academic", "0.7.0", IMPORTED_ROOT, "0.7.0", IMPORTED
        ) is None

    def test_different_root_is_warned(self):
        # The #554 hazard itself: pip's record points at one tree, `import factlog`
        # landed in another, and the report you are holding came from the second.
        skew = installation_skew(
            "factlog-academic", "0.6.0", "/bundled/copy", "0.7.0", IMPORTED
        )
        assert skew is not None
        assert skew.severity == "WARN"
        # Both sides are named — a warning that says "they differ" without saying
        # which two places differ is unactionable.
        assert "/bundled/copy" in skew.title
        assert IMPORTED_ROOT in skew.title
        assert skew.hints, "a skew WARN must carry a remedy"

    def test_different_root_is_warned_even_when_versions_match(self):
        # Two checkouts of the same release is the shape that fooled #208/#491/#527/
        # #547 triage: identical version strings, different code.
        skew = installation_skew(
            "factlog-academic", "0.7.0", "/bundled/copy", "0.7.0", IMPORTED
        )
        assert skew is not None and skew.severity == "WARN"

    def test_same_root_version_mismatch_is_info_not_warn(self):
        # A version bump with no reinstall: pip's metadata is stale, the code is
        # not. WARN here would cry wolf in every maintainer's clone, which is how a
        # diagnostic gets ignored on the day it is right.
        skew = installation_skew(
            "factlog-academic", "0.6.0", IMPORTED_ROOT, "0.7.0", IMPORTED
        )
        assert skew is not None
        assert skew.severity == "INFO"
        assert "0.6.0" in skew.title and "0.7.0" in skew.title


@pytest.mark.parametrize(
    "dist_name,dist_version,dist_root",
    [
        (None, None, None),
        ("factlog", "0.6.0", "/bundled/copy"),
        ("factlog-academic", "0.6.0", "/bundled/copy"),
        ("factlog-academic", "0.6.0", IMPORTED_ROOT),
        ("factlog-academic", "0.7.0", IMPORTED_ROOT),
        ("factlog-academic", UNKNOWN, IMPORTED_ROOT),
    ],
)
def test_skew_is_never_fail(dist_name, dist_version, dist_root):
    """No input may produce a FAIL.

    doctor's exit code is gated on FAIL alone, and smoke.sh/setup.sh depend on a
    healthy environment exiting 0. A diagnostic added to make skew *observable*
    must not be able to turn a working install into a failing one — so this is
    asserted over every state, not just the ones with a hand-written expectation.
    """
    skew = installation_skew(dist_name, dist_version, dist_root, "0.7.0", IMPORTED)
    assert skew is None or skew.severity in ("INFO", "WARN")
    assert skew is None or skew.severity != "FAIL"


class TestInstallationSkewDegradations:
    def test_unknown_imported_file_is_silent(self):
        # No path to compare: silence beats a warning derived from "<unknown>".
        assert installation_skew(
            "factlog-academic", "0.6.0", "/bundled/copy", "0.7.0", UNKNOWN
        ) is None

    def test_symlinked_root_is_not_a_mismatch(self, tmp_path):
        """A venv reached through a symlink is the commonest false positive going.

        The lexical comparison would call these two different directories; the
        realpath second chance is what keeps the row quiet. It can only ever turn
        a WARN into silence, never the reverse.
        """
        real = tmp_path / "real"
        (real / "factlog").mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(real)
        imported = str(link / "factlog" / "__init__.py")
        assert installation_skew("factlog-academic", "0.7.0", str(real), "0.7.0", imported) is None

    def test_trailing_separator_is_not_a_mismatch(self):
        # `locate_file("")` can come back with a trailing separator; that is not a
        # different directory.
        assert installation_skew(
            "factlog-academic", "0.7.0", IMPORTED_ROOT + "/", "0.7.0", IMPORTED
        ) is None


class _Dist:
    """The slice of ``importlib.metadata.Distribution`` the lookup touches."""

    def __init__(self, name, version, path, *, direct_url=None, located=None):
        self.metadata = {"Name": name}
        self.version = version
        self._path = Path(path)
        self._direct_url = direct_url
        self._located = located

    def read_text(self, filename):
        return self._direct_url if filename == "direct_url.json" else None

    def locate_file(self, _relative):
        return self._located


_SITE = "/venv/lib/python3.12/site-packages"


def _wheel(name, version):
    return _Dist(name, version, f"{_SITE}/{name.replace('-', '_')}-{version}.dist-info", located=_SITE)


def _editable(name, version, checkout):
    return _Dist(
        name,
        version,
        f"{_SITE}/{name.replace('-', '_')}-{version}.dist-info",
        direct_url='{"dir_info": {"editable": true}, "url": "file://%s"}' % checkout,
        located=_SITE,
    )


class TestInstalledFactlogDist:
    def test_wheel_install_reports_site_packages(self):
        found = installed_factlog_dist(lambda: [_wheel("factlog-academic", "0.7.0")])
        assert found == ("factlog-academic", "0.7.0", _SITE)

    def test_editable_install_reports_the_checkout_not_site_packages(self):
        """THE reason ``direct_url.json`` is read at all.

        An editable install puts only a ``.pth`` shim in site-packages, so
        ``locate_file("")`` answers site-packages for it too. Taking that at face
        value would report a mismatch for every editable install in existence —
        i.e. for every developer, permanently.
        """
        found = installed_factlog_dist(lambda: [_editable("factlog-academic", "0.7.0", "/work/tree")])
        assert found == ("factlog-academic", "0.7.0", "/work/tree")
        assert installation_skew(*found, "0.7.0", IMPORTED) is None

    def test_editable_url_is_percent_decoded(self):
        found = installed_factlog_dist(
            lambda: [_editable("factlog-academic", "0.7.0", "/work/my%20tree")]
        )
        assert found is not None and found[2] == "/work/my tree"

    def test_non_editable_direct_url_falls_back_to_locate_file(self):
        # `pip install .` records direct_url.json too, with editable absent — that
        # one really did copy the code into site-packages.
        dist = _Dist(
            "factlog-academic",
            "0.7.0",
            f"{_SITE}/factlog_academic-0.7.0.dist-info",
            direct_url='{"dir_info": {}, "url": "file:///work/tree"}',
            located=_SITE,
        )
        assert installed_factlog_dist(lambda: [dist]) == ("factlog-academic", "0.7.0", _SITE)

    def test_a_leftover_egg_info_is_not_an_installed_dist(self):
        # The false positive `installed_distributions` already learned about: a
        # checkout's stale factlog.egg-info/ would report the checkout's OLD version
        # against the very tree it sits in — a permanent INFO row in every clone.
        stale = _Dist("factlog", "0.5.0", "/repo/factlog.egg-info", located="/repo")
        assert installed_factlog_dist(lambda: [stale]) is None

    def test_factlog_academic_wins_when_both_are_installed(self):
        # The double install is already reported, once, by rename_migration_check.
        # This row picks the current distribution and says nothing about the pair,
        # so nobody hears the same warning twice.
        dists = [_wheel("factlog", "0.6.0"), _editable("factlog-academic", "0.7.0", "/work/tree")]
        found = installed_factlog_dist(lambda: dists)
        assert found == ("factlog-academic", "0.7.0", "/work/tree")

    def test_legacy_factlog_alone_is_still_compared(self):
        assert installed_factlog_dist(lambda: [_wheel("factlog", "0.6.0")]) == (
            "factlog", "0.6.0", _SITE,
        )

    def test_unrelated_distributions_are_ignored(self):
        assert installed_factlog_dist(lambda: [_wheel("pyrewire", "1.0.3")]) is None

    def test_a_dist_without_metadata_does_not_crash(self):
        class _Broken:
            metadata = None
            _path = Path(f"{_SITE}/x.dist-info")

        assert installed_factlog_dist(lambda: [_Broken()]) is None

    def test_unreadable_direct_url_does_not_crash(self):
        dist = _Dist(
            "factlog-academic",
            "0.7.0",
            f"{_SITE}/factlog_academic-0.7.0.dist-info",
            direct_url="{not json",
            located=_SITE,
        )
        assert installed_factlog_dist(lambda: [dist]) == ("factlog-academic", "0.7.0", _SITE)


class TestSkewOnThisEnvironment:
    def test_the_real_lookup_never_raises_and_never_fails(self):
        """Whatever this machine's install looks like, the row is advisory.

        The unit states above are injected; this one runs the real metadata I/O,
        because a lookup that raises would take `factlog doctor` — the command you
        run when things are broken — down with it.
        """
        dist = installed_factlog_dist()
        name, version, root = dist if dist else (None, None, None)
        imported_version, imported_file = running_factlog()
        skew = installation_skew(name, version, root, imported_version, imported_file)
        assert skew is None or skew.severity in ("INFO", "WARN")
