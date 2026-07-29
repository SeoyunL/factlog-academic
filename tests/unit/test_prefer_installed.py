# SPDX-License-Identifier: Apache-2.0
"""``FACTLOG_PREFER_INSTALLED``: let the contributor's own tree win over the bundle (#553).

A plugin ships its own copy of ``tools/``, and every wrapper in it puts its OWN
distribution root at ``sys.path[0]``. So ``${CLAUDE_PLUGIN_ROOT}/tools/run_logic_check.py``
imports the bundled ``factlog`` even when the contributor has their working tree
installed — they verify a change against code that does not contain it. Four already
closed defects (#208, #491, #527, #547) were re-diagnosed as live bugs from exactly
that shape.

``FACTLOG_PREFER_INSTALLED=1`` makes the wrappers **look up** whether an installed
``factlog`` exists and leave ``sys.path`` alone when one does, falling back to
**appending** their own root when none does.

All three halves are load-bearing, and each was measured rather than reasoned:

* **Probe, not append-only.** ``pip install -e .`` on this project emits a setuptools
  ``_TopLevelFinder`` — the checkout is reachable only through a finder appended to
  ``sys.meta_path``, *behind* the builtin ``PathFinder``. Appending ``_ROOT`` still
  leaves it on ``sys.path``, so ``PathFinder`` answers with the bundle and the editable
  finder is never asked. An append-only opt-out is a silent no-op in that shape, and it
  takes the split warning down with it (both trees agree once the bundle wins twice).
  ``TestInstallForms`` pins all three shapes.
* **Append, not skip.** Skipping the insertion outright would make a bare checkout with
  nothing installed raise ``ImportError`` — the opt-out would invent a new failure mode.
  The ``none`` form pins that it does not.
* **Look up, not import.** ``except ImportError`` is wider than "there is no factlog":
  an installed package whose ``__init__`` imports a missing dependency raises
  ``ModuleNotFoundError``, the fallback swallows it, and the bundle wins with ``=1`` set
  and nothing printed — the no-signal state rebuilt inside the fix.
  ``test_a_broken_install_is_not_silently_routed_around`` pins that.

Everything runs in a **subprocess**. The question is literally "which package did this
process import", and an in-process assertion could only ever describe the test runner's
own import. Two determinism rules the runs obey, both of which silently invert the
result when broken:

* ``cwd`` is a directory holding no ``factlog/``. A script run puts the script's own
  directory on ``sys.path[0]``, and a stray ``factlog/`` next to the run would decide
  the winner before either mechanism under test got a vote.
* the bundle's root is **never** placed on ``PYTHONPATH``. The bundle must win (or lose)
  purely by the wrapper's own insertion, which is the field shape; naming it in the
  environment would test the environment instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools"

# The four wrappers that insert the DISTRIBUTION root. The other eleven ``tools/``
# scripts insert ``_TOOLS_DIR`` instead, which resolves sibling ``tools`` modules and is
# a different mechanism — see ``TestOnlyTheWrappersInsertTheDistributionRoot``.
WRAPPERS = (
    TOOLS / "compile_facts.py",
    TOOLS / "common.py",
    TOOLS / "literal_types.py",
    TOOLS / "factlog_config.py",
)

BOOTSTRAP_START = "# --- factlog bootstrap (#553): keep byte-identical across the tools/ wrappers ---"
BOOTSTRAP_END = "# --- end factlog bootstrap ---"
DIST_ROOT_INSERT = "_ROOT = Path(__file__).resolve().parent.parent"


def _env(kb: Path, *, package_root: Path | None = None, prefer: str | None = None) -> dict[str, str]:
    """A run's environment, with every input to the decision stated explicitly.

    ``FACTLOG_PREFER_INSTALLED`` and ``PYTHONPATH`` are **popped** before anything is
    set: a developer who exported either one while debugging would otherwise silently
    decide the outcome of every case here, including the ones asserting the variable is
    OFF.
    """
    env = dict(os.environ)
    env.pop("FACTLOG_PREFER_INSTALLED", None)
    env.pop("PYTHONPATH", None)
    if prefer is not None:
        env["FACTLOG_PREFER_INSTALLED"] = prefer
    if package_root is not None:
        env["PYTHONPATH"] = str(package_root)
    env["FACTLOG_ROOT"] = str(kb)
    return env


def _bundle(tmp_path: Path) -> Path:
    """A real, runnable copy of the tree at a made-up version — the plugin's bundle.

    ``shutil.copytree`` rather than a stub package, for the reason
    ``test_report_factlog_provenance._second_tree`` gives: the claim is that two
    *working* installations are told apart, which a fake could not demonstrate. The
    version is prefixed rather than replaced so the copy is unmistakable and can never
    collide with a real release number.
    """
    root = tmp_path / "bundle"
    shutil.copytree(REPO_ROOT / "factlog", root / "factlog")
    shutil.copytree(REPO_ROOT / "tools", root / "tools")
    init = root / "factlog" / "__init__.py"
    init.write_text(
        init.read_text(encoding="utf-8").replace('__version__ = "', '__version__ = "9.9.9+'),
        encoding="utf-8",
    )
    return root


def _new_kb(tmp_path: Path, name: str) -> Path:
    kb = tmp_path / name
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        check=True,
        capture_output=True,
        env=_env(tmp_path, package_root=REPO_ROOT),
    )
    return kb


def _neutral_cwd(tmp_path: Path) -> Path:
    """A working directory containing no ``factlog/`` — see the module docstring."""
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    assert not (cwd / "factlog").exists()
    return cwd


def _compile(
    script_root: Path,
    kb: Path,
    cwd: Path,
    *,
    package_root: Path | None = None,
    prefer: str | None = None,
    python: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``<script_root>/tools/compile_facts.py`` and hand back its streams.

    ``compile_facts`` is the probe because it prints ``provenance_line()`` as its first
    stdout line and needs no engine, so the answer to "which package did this process
    import" is the first thing it says.

    *python* selects the interpreter, which is how ``TestInstallForms`` puts a
    purpose-built venv — and only that venv's ``site-packages`` — behind the run.
    """
    return subprocess.run(
        [str(python or sys.executable), str(script_root / "tools" / "compile_facts.py")],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=_env(kb, package_root=package_root, prefer=prefer),
    )


def _factlog_line(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert lines and lines[0].startswith("factlog: "), result.stdout
    return lines[0]


def _measure(package_root: Path, cwd: Path) -> str:
    """What that tree answers when asked directly — the reader's own one-line check."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import factlog; print(f'factlog: {factlog.__version__} ({factlog.__file__})')",
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env=_env(cwd, package_root=package_root),
    )
    return proc.stdout.strip()


class TestWhichTreeWins:
    def test_unset_keeps_the_bundle_winning(self, tmp_path):
        """No-regression gate: today's behaviour is the default and does not move.

        Everything shipped depends on a bundled run being self-contained. If this test
        can be made to pass by an implementation that changed the default, the opt-in
        was not an opt-in.
        """
        bundle = _bundle(tmp_path)
        kb = _new_kb(tmp_path, "kb_default")
        cwd = _neutral_cwd(tmp_path)

        line = _factlog_line(_compile(bundle, kb, cwd, package_root=REPO_ROOT))

        assert line == _measure(bundle, cwd)
        assert "9.9.9+" in line
        assert str(bundle / "factlog" / "__init__.py") in line

    def test_one_lets_the_reachable_installed_tree_win(self, tmp_path):
        """The whole point of the issue, in one assertion.

        Same bundle, same script, same KB — only the variable differs from the test
        above, and the package that answers changes. Checked against that tree's own
        measurement rather than a literal, so it pins the mechanism and not this machine.
        """
        bundle = _bundle(tmp_path)
        kb = _new_kb(tmp_path, "kb_prefer")
        cwd = _neutral_cwd(tmp_path)

        line = _factlog_line(_compile(bundle, kb, cwd, package_root=REPO_ROOT, prefer="1"))

        assert line == _measure(REPO_ROOT, cwd)
        assert "9.9.9+" not in line
        assert str(REPO_ROOT / "factlog" / "__init__.py") in line

    @pytest.mark.parametrize("value", ["0", "", "true", "TRUE", "yes", "on", "2"])
    def test_only_the_exact_string_one_opts_in(self, tmp_path, value):
        """Every other value is OFF — the semantics are enumerated, not described.

        A truthiness dialect invented here ("true", "yes", any non-empty string) would
        be a second contract nobody can look up, and ``""``/``0`` in particular are what
        a shell produces from an unset variable expanded into an export. One spelling
        works; the rest are indistinguishable from not asking.
        """
        bundle = _bundle(tmp_path)
        kb = _new_kb(tmp_path, "kb_off")
        cwd = _neutral_cwd(tmp_path)

        line = _factlog_line(_compile(bundle, kb, cwd, package_root=REPO_ROOT, prefer=value))

        assert line == _measure(bundle, cwd)
        assert "9.9.9+" in line


def _installed_tree(tmp_path: Path) -> Path:
    """The contributor's own tree, at a version neither the repo nor the bundle uses.

    Three distinct versions are in play in ``TestInstallForms`` — repo ``0.7.0``, bundle
    ``9.9.9+``, installed ``8.8.8+`` — so "the installed one won" can never be satisfied
    by accident by the interpreter finding the repo through some path nobody named.
    """
    root = tmp_path / "installed"
    shutil.copytree(REPO_ROOT / "factlog", root / "factlog")
    init = root / "factlog" / "__init__.py"
    init.write_text(
        init.read_text(encoding="utf-8").replace('__version__ = "', '__version__ = "8.8.8+'),
        encoding="utf-8",
    )
    return root


# The finder setuptools>=64 writes for an editable install, reduced to the part that
# decides this test: a MetaPathFinder APPENDED to ``sys.meta_path``, therefore consulted
# only after the builtin ``PathFinder`` has failed. Reproduced rather than imported so
# the shape under test is visible in the file that tests it.
_EDITABLE_FINDER = '''
import sys
from importlib.machinery import PathFinder
from importlib.util import spec_from_file_location
from pathlib import Path

MAPPING = {{"factlog": r"{package}"}}


class _EditableFinder:
    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        if fullname in MAPPING:
            init = Path(MAPPING[fullname]) / "__init__.py"
            if init.exists():
                return spec_from_file_location(
                    fullname, init, submodule_search_locations=[MAPPING[fullname]]
                )
            return None
        parent, _, _child = fullname.rpartition(".")
        if parent and parent in MAPPING:
            return PathFinder.find_spec(fullname, path=[MAPPING[parent]])
        return None


def install():
    if not any(f is _EditableFinder for f in sys.meta_path):
        sys.meta_path.append(_EditableFinder)
'''


@pytest.mark.skipif(os.name == "nt", reason="venv layout probing here is posix-shaped")
class TestInstallForms:
    """The issue's own verification: a plugin bundle and an install, side by side.

    #553 says to "check that the opt-out path runs the working tree in an environment
    where the plugin install and an editable install coexist". Four coexistences are
    built here from scratch — no pip, no network, ~2s each — because *how* the install
    was recorded changes the answer, and only one of the four was ever reasoned about:

    * ``wheel``    — the package copied into ``site-packages`` (a plain ``pip install .``)
    * ``pth``      — a ``.pth`` naming the checkout (pre-PEP-660 ``setup.py develop``)
    * ``metapath`` — a ``sys.meta_path`` finder (``pip install -e .``, setuptools>=64,
      **measured to be what this project's own pyproject produces**)
    * ``none``     — nothing installed at all (a bare checkout)

    ``metapath`` is the one that broke an append-only implementation, and it is the
    shape a contributor actually gets today, so a passing ``wheel`` proved nothing about
    the case the issue was filed for.
    """

    def _venv(self, tmp_path: Path, form: str) -> tuple[Path, Path]:
        root = tmp_path / f"venv_{form}"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(root)],
            check=True,
            capture_output=True,
        )
        (site_packages,) = (root / "lib").glob("python3.*/site-packages")
        return root / "bin" / "python", site_packages

    def _install(self, site_packages: Path, form: str, installed: Path) -> Path:
        """Install *installed* into *site_packages* in the given shape.

        Returns the ``__init__.py`` the run must end up importing. It differs by form
        and that is the point: ``wheel`` COPIES the code into site-packages, so a test
        asserting the checkout path would be asserting something only editable installs
        can satisfy — and would quietly stop testing ``wheel`` at all.
        """
        if form == "wheel":
            shutil.copytree(installed / "factlog", site_packages / "factlog")
            return site_packages / "factlog" / "__init__.py"
        if form == "pth":
            (site_packages / "factlog.pth").write_text(f"{installed}\n", encoding="utf-8")
        elif form == "metapath":
            (site_packages / "_editable_probe_finder.py").write_text(
                _EDITABLE_FINDER.format(package=installed / "factlog"), encoding="utf-8"
            )
            (site_packages / "__editable__.probe.pth").write_text(
                "import _editable_probe_finder; _editable_probe_finder.install()\n",
                encoding="utf-8",
            )
        elif form != "none":  # pragma: no cover - guards a typo'd parametrisation
            raise AssertionError(form)
        return installed / "factlog" / "__init__.py"

    @pytest.mark.parametrize("form", ["wheel", "pth", "metapath", "none"])
    def test_unset_keeps_the_bundle_winning_in_every_install_form(self, tmp_path, form):
        """The default is the default no matter how the other tree got installed."""
        bundle = _bundle(tmp_path)
        installed = _installed_tree(tmp_path)
        kb = _new_kb(tmp_path, f"kb_{form}_default")
        cwd = _neutral_cwd(tmp_path)
        python, site_packages = self._venv(tmp_path, form)
        self._install(site_packages, form, installed)

        result = _compile(bundle, kb, cwd, python=python)

        assert "9.9.9+" in _factlog_line(result)
        assert "warning:" not in result.stderr

    @pytest.mark.parametrize("form", ["wheel", "pth", "metapath"])
    def test_one_hands_the_run_to_the_installed_tree_in_every_install_form(self, tmp_path, form):
        """The claim the issue asks to verify, in all three shapes an install can take.

        Before the probe, ``metapath`` failed here *and said nothing*: the bundle won
        both the import and the split comparison, so the run looked exactly like a
        healthy one. That is the same unattributable state #553 exists to remove, which
        is why the warning is asserted alongside the version.
        """
        bundle = _bundle(tmp_path)
        installed = _installed_tree(tmp_path)
        kb = _new_kb(tmp_path, f"kb_{form}_prefer")
        cwd = _neutral_cwd(tmp_path)
        python, site_packages = self._venv(tmp_path, form)
        expected = self._install(site_packages, form, installed)

        result = _compile(bundle, kb, cwd, prefer="1", python=python)

        line = _factlog_line(result)
        assert "8.8.8+" in line, f"{form}: the installed tree did not win"
        assert "9.9.9+" not in line
        assert str(expected) in line
        # Both trees are named, and the warning fires: a split that the opt-out CREATED
        # is exactly the split a reader has to be told about.
        assert "warning:" in result.stderr
        assert str(bundle / "tools" / "compile_facts.py") in result.stderr

    def test_one_still_runs_when_nothing_is_installed_anywhere(self, tmp_path):
        """Append, not skip: the opt-out must not be able to break a bare checkout.

        An empty venv means the bundle's own appended root is the only ``factlog`` in
        existence. An implementation that merely *skips* the insertion raises
        ``ImportError`` here; appending exits 0 and names the bundle. This is the case
        that keeps the fallback in the design.
        """
        bundle = _bundle(tmp_path)
        kb = _new_kb(tmp_path, "kb_fallback")
        cwd = _neutral_cwd(tmp_path)
        python, _ = self._venv(tmp_path, "none")

        result = _compile(bundle, kb, cwd, prefer="1", python=python)

        assert result.returncode == 0, result.stdout + result.stderr
        line = _factlog_line(result)
        assert "9.9.9+" in line
        assert str(bundle / "factlog" / "__init__.py") in line
        assert "warning:" not in result.stderr

    def test_a_broken_install_is_not_silently_routed_around(self, tmp_path):
        """The fallback must not rebuild the no-signal state it exists to remove.

        ``try: import factlog / except ImportError`` passes every other test in this
        class. It also swallows ``ModuleNotFoundError`` — an ``ImportError`` subclass —
        raised by an installed package whose ``__init__`` imports something missing. The
        fallback then appends, the bundle wins, and the run prints a clean report with
        ``FACTLOG_PREFER_INSTALLED=1`` set and no warning: precisely the unattributable
        state #553 was filed about, reconstructed inside #553's own fix.

        Asking ``find_spec`` instead answers "a factlog exists here" without executing
        it, so a broken install fails on its own terms. What is asserted is therefore
        not "it succeeds" but "it does not quietly become a bundle run" — the failure is
        the correct outcome, and the traceback names the real cause.
        """
        bundle = _bundle(tmp_path)
        installed = _installed_tree(tmp_path)
        (installed / "factlog" / "__init__.py").write_text(
            (installed / "factlog" / "__init__.py").read_text(encoding="utf-8")
            + "\nimport factlog_missing_dep_xyz  # noqa: F401\n",
            encoding="utf-8",
        )
        kb = _new_kb(tmp_path, "kb_broken")
        cwd = _neutral_cwd(tmp_path)
        python, site_packages = self._venv(tmp_path, "metapath")
        self._install(site_packages, "metapath", installed)

        result = _compile(bundle, kb, cwd, prefer="1", python=python)

        assert result.returncode != 0, result.stdout + result.stderr
        assert "9.9.9+" not in result.stdout, "the bundle silently took over the run"
        assert "factlog_missing_dep_xyz" in result.stderr


class TestSplitTreeWarning:
    """A split installation is announced, and announced somewhere that cannot break stdout."""

    def test_a_split_warns_on_stderr_and_leaves_stdout_positions_alone(self, tmp_path):
        bundle = _bundle(tmp_path)
        kb = _new_kb(tmp_path, "kb_split")
        cwd = _neutral_cwd(tmp_path)

        result = _compile(bundle, kb, cwd, package_root=REPO_ROOT, prefer="1")

        assert result.returncode == 0, result.stdout + result.stderr
        # Both trees are named: a warning that says "these differ" without saying which
        # two things differ leaves the reader exactly where they started.
        assert "warning:" in result.stderr
        assert str(bundle / "tools" / "compile_facts.py") in result.stderr
        assert str(REPO_ROOT / "factlog" / "__init__.py") in result.stderr
        # stdout keeps its shape: #554 pins these two lines BY POSITION, so a warning
        # that leaked one line into stdout would break the provenance contract instead
        # of supporting it.
        out = result.stdout.splitlines()
        assert out[0] == _measure(REPO_ROOT, cwd)
        assert out[1].startswith("compile_facts: target KB ")
        assert "warning:" not in result.stdout

    def test_one_tree_is_silent(self, tmp_path):
        """The healthy case says nothing.

        A diagnostic that fires on the ordinary installation is one every reader learns
        to scroll past, and then it is not there when it matters.
        """
        kb = _new_kb(tmp_path, "kb_same")
        cwd = _neutral_cwd(tmp_path)

        result = _compile(REPO_ROOT, kb, cwd, package_root=REPO_ROOT)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "warning:" not in result.stderr
        out = result.stdout.splitlines()
        assert out[0] == _measure(REPO_ROOT, cwd)
        assert out[1].startswith("compile_facts: target KB ")


class TestScriptTreeSplit:
    """The judgement itself, argument-injected — every state without a venv dance."""

    def test_different_trees_produce_a_warning_naming_both(self):
        from factlog.runtime import script_tree_split

        message = script_tree_split("/bundle/tools/run_logic_check.py", "/repo/factlog/__init__.py")

        assert message is not None
        assert "/bundle/tools/run_logic_check.py" in message
        assert "/repo/factlog/__init__.py" in message
        assert "/bundle" in message and "/repo" in message

    def test_the_same_tree_is_silent(self):
        from factlog.runtime import script_tree_split

        assert script_tree_split("/repo/tools/run_logic_check.py", "/repo/factlog/__init__.py") is None

    def test_a_package_file_compares_its_tree_with_itself(self):
        """Records WHY ``tools/compile_facts.py`` carries the check — it does not enforce it.

        Inside the package ``__file__`` IS the package file, so the comparison is the
        package tree against itself and can never fire; moving the call into
        ``factlog.compile_facts.main()`` would make it silently dead code. That reasoning
        is worth keeping next to the function, which is all this test does.

        It is explicitly NOT the guard on the placement: this assertion holds no matter
        where the call lives, so it survives a mutant that moves it. The placement is
        pinned by ``test_a_split_warns_on_stderr_and_leaves_stdout_positions_alone``,
        which runs a real bundled ``tools/compile_facts.py`` and dies when the check is
        moved into the package. No source-scanning guard is added here on purpose —
        enforcing one behaviour twice only manufactures equivalent mutants.
        """
        from factlog.runtime import script_tree_split

        assert script_tree_split("/repo/factlog/compile_facts.py", "/repo/factlog/__init__.py") is None

    @pytest.mark.parametrize("bad", ["", "<unknown>"])
    def test_an_unreadable_path_is_not_a_discrepancy(self, bad):
        """Silence, not a warning, when a measurement is missing.

        ``running_factlog`` degrades ``__file__`` to ``<unknown>`` for namespace packages
        and zipimports; inventing a split out of an absent value would fire hardest in
        the odd installations this exists to describe.
        """
        from factlog.runtime import script_tree_split

        assert script_tree_split(bad, "/repo/factlog/__init__.py") is None
        assert script_tree_split("/repo/tools/x.py", bad) is None

    def test_it_defaults_to_the_running_package(self):
        """Called with one argument it measures the real import, not a placeholder."""
        import factlog
        from factlog.runtime import script_tree_split

        assert script_tree_split(factlog.__file__) is None
        assert script_tree_split("/nowhere/tools/x.py") is not None


# The made-up integration that tells the two trees apart below. Made-up on purpose:
# ``_tree_with_a_fifth_integration`` asserts this string is absent from this tree's
# writers, so the day a real fifth integration lands the helper says so instead of
# quietly measuring nothing.
FIFTH_IDENTITY_KEY = "fifthsource_id"

# A source whose opening ``---`` a human deleted, in the shape ``tools/validate.py``
# reports: no opening fence, first line an importer identity key. See
# ``test_validate_front_matter.OPENING_DELETED``, which is the same file with a real key.
PROBE_SOURCE = f'{FIFTH_IDENTITY_KEY}: "X1"\ntitle: "A paper"\n---\n\nAbstract.\n'

# The two files ``factlog init`` does not write. Filled in so a validate run over the
# fixture below PASSES: the answer then arrives as one warning line above an otherwise
# identical verdict, and an unrelated failure cannot be mistaken for the signal.
CANDIDATES_HEADER = "subject,relation,object,source,status,confidence,note\n"
REVIEW_SECTIONS = "# Open Questions\n\n## 중복 개념 후보\n\n## 모호한 관계명\n\n## 출처 부족\n\n## 충돌\n"


def _tree_with_a_fifth_integration(tmp_path: Path, name: str, *, with_tools: bool) -> Path:
    """A runnable copy of this tree whose writers know one more integration.

    A copy rather than a stub, for the reason ``_bundle`` gives. It differs from this
    tree by one entry in ``IDENTITY_KEYS_BY_SOURCE``, which is a documented extension
    point: ``tools/validate.py`` builds ``_OPENING_IDENTITY_KEY_RE`` from that map at
    import — "a fifth integration's key is covered here the day it is added there" — so
    a source opening with that key is flagged by a validate run that imported THIS copy
    and by no other.

    That is what makes "which factlog did this process import" answerable for a tool
    that prints no provenance line: the answer is a behaviour of the run, read off
    validate's own stdout, and nothing is added to ``validate.py`` to obtain it.
    """
    root = tmp_path / name
    shutil.copytree(REPO_ROOT / "factlog", root / "factlog")
    if with_tools:
        shutil.copytree(REPO_ROOT / "tools", root / "tools")
    writer = root / "factlog" / "integrations" / "common" / "source_writer.py"
    text = writer.read_text(encoding="utf-8")
    assert FIFTH_IDENTITY_KEY not in text, f"{FIFTH_IDENTITY_KEY} is no longer a made-up key"
    anchor = "IDENTITY_KEYS_BY_SOURCE = {"
    assert text.count(anchor) == 1, "the writers' identity map moved; this copy patches nothing"
    writer.write_text(
        text.replace(anchor, f'{anchor}\n    "fifthsource": "{FIFTH_IDENTITY_KEY}",'),
        encoding="utf-8",
    )
    return root


def _kb_with_a_probe_source(tmp_path: Path, name: str) -> Path:
    """A KB that validates cleanly, holding one source that opens with the fifth key."""
    kb = _new_kb(tmp_path, name)
    (kb / "sources" / "probe.md").write_text(PROBE_SOURCE, encoding="utf-8")
    (kb / "facts" / "candidates.csv").write_text(CANDIDATES_HEADER, encoding="utf-8")
    (kb / "decisions" / "open-questions.md").write_text(REVIEW_SECTIONS, encoding="utf-8")
    return kb


def _validate(
    script_root: Path,
    kb: Path,
    cwd: Path,
    *,
    package_roots: tuple[Path, ...],
    prefer: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``<script_root>/tools/validate.py`` over *kb* and hand back its streams.

    *package_roots* is the whole ``PYTHONPATH``, in order, because the question here is
    which entry wins — a single root could not pose it. ``--target`` is named on the
    command line rather than left to ``$FACTLOG_ROOT`` or the active-KB config, so no
    machine's ``factlog use`` can decide what this run validated.
    """
    env = _env(kb, prefer=prefer)
    env["PYTHONPATH"] = os.pathsep.join(str(root) for root in package_roots)
    return subprocess.run(
        [sys.executable, str(script_root / "tools" / "validate.py"), "--target", str(kb)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )


def _flagged(result: subprocess.CompletedProcess[str]) -> list[str]:
    """The sources this run read as having had their opening fence deleted.

    Non-empty only when the factlog the run imported carries the fifth integration's
    key. The exit code is asserted here so a run that failed for some other reason
    cannot be read as "the key was not recognised".
    """
    assert result.returncode == 0, result.stdout + result.stderr
    return [line for line in result.stdout.splitlines() if line.startswith("warning: no_opening_fence: ")]


def _verdict(result: subprocess.CompletedProcess[str]) -> list[str]:
    """Everything the run said apart from warnings — its judgement on the KB."""
    return [line for line in result.stdout.splitlines() if not line.startswith("warning: ")]


class TestWhichTreeValidatePyImports:
    """The path ``tools/common.py``'s copy of the block decides ALONE (#621).

    ``TestTheFourBootstrapsDoNotDrift`` below compares the four copies as TEXT, so a
    mutant in any one of them fails it whatever the mutant does. That reads as coverage
    and is not: before this class, the only copy whose BEHAVIOUR a test measured was
    ``factlog_config.py``'s, through ``test_two_trees_produce_two_different_lines``
    (measured in #617 at bb3909c by replacing the membership guard with ``if True:`` one
    wrapper at a time — dropping it from ``common.py``, ``compile_facts.py`` or
    ``literal_types.py`` left every behaviour test green).

    The reason is import ORDER, not redundancy: every other ``tools/`` script that
    imports ``common`` imports ``factlog_config`` FIRST (measured at 3e6aafc, first
    import line of each: ``run_logic_check`` 34 against 80, ``merge_candidates`` 65
    against 102, ``finalize`` 34 against 35, and so on), so ``factlog`` is already in
    ``sys.modules`` when ``common``'s copy runs and its ``sys.path`` work cannot move it.
    ``tools/validate.py`` is the exception — the only script that imports ``common``
    (line 15) and no ``factlog_config`` at all (``grep -c factlog_config``: validate 0,
    compile_facts 4, literal_types 4, merge_candidates 3, source_coverage 2) — so
    ``common.py``'s copy decides which factlog it gets, on its own.

    Every run is a subprocess, for the module docstring's reason, and each is compared
    against the same tools running with a different ``PYTHONPATH`` — never against a
    literal, which would pin this machine instead of the mechanism.
    """

    def test_a_fronted_tree_wins_over_validate_s_own_root(self, tmp_path):
        """The membership guard, measured through the tool it decides for.

        Same ``tools/validate.py``, same KB, same interpreter: only the order of
        ``PYTHONPATH`` differs, and the package that answers changes with it. That is
        the shape the guard exists for — a contributor's tree named ahead of the root
        this script sits in — and ``if str(_ROOT) not in sys.path`` in ``common.py`` is
        the whole of what keeps the insertion below from overtaking it.
        """
        fronted = _tree_with_a_fifth_integration(tmp_path, "fronted", with_tools=False)
        kb = _kb_with_a_probe_source(tmp_path, "kb_fronted")
        cwd = _neutral_cwd(tmp_path)

        here = _validate(REPO_ROOT, kb, cwd, package_roots=(REPO_ROOT,))
        there = _validate(REPO_ROOT, kb, cwd, package_roots=(fronted, REPO_ROOT))

        assert len(_flagged(there)) == 1, there.stdout
        assert f"identity key {FIFTH_IDENTITY_KEY!r}" in _flagged(there)[0]
        # The control that keeps the assertion above from passing on any run at all:
        # this tree does not know that key, so its own run must stay silent about it.
        assert _flagged(here) == []
        # And the two runs agree on everything else, so the flagged line is carrying the
        # whole distinction — the proof it is the imported package talking and not the KB.
        assert _verdict(there) == _verdict(here)

    def test_validate_s_own_root_wins_when_nothing_fronts_it(self, tmp_path):
        """No-regression gate: a bundled ``validate.py`` still validates with its bundle.

        The insertion is what makes a shipped plugin self-contained. Nothing names the
        bundle on ``PYTHONPATH`` here — it wins purely by ``common.py``'s own insert,
        which is the field shape — while this repo IS named there and still loses.
        """
        bundle = _tree_with_a_fifth_integration(tmp_path, "bundle", with_tools=True)
        kb = _kb_with_a_probe_source(tmp_path, "kb_bundled")
        cwd = _neutral_cwd(tmp_path)

        bundled = _validate(bundle, kb, cwd, package_roots=(REPO_ROOT,))
        repo = _validate(REPO_ROOT, kb, cwd, package_roots=(REPO_ROOT,))

        assert len(_flagged(bundled)) == 1, bundled.stdout
        assert _flagged(repo) == []
        assert _verdict(bundled) == _verdict(repo)

    def test_one_hands_validate_to_the_installed_tree(self, tmp_path):
        """``FACTLOG_PREFER_INSTALLED=1`` reaches ``validate.py`` too.

        The opt-out is documented for the wrappers as a set, and this is the tool where
        no earlier copy has already settled the question — so if ``common.py``'s copy
        did not honour it, the variable would be silently ineffective for exactly the
        run a contributor is most likely to be debugging. Both values are exercised in
        one test: the absence of the flag is only evidence next to its presence.
        """
        bundle = _tree_with_a_fifth_integration(tmp_path, "bundle_prefer", with_tools=True)
        kb = _kb_with_a_probe_source(tmp_path, "kb_prefer")
        cwd = _neutral_cwd(tmp_path)

        default = _validate(bundle, kb, cwd, package_roots=(REPO_ROOT,))
        opted_out = _validate(bundle, kb, cwd, package_roots=(REPO_ROOT,), prefer="1")

        assert len(_flagged(default)) == 1, default.stdout
        assert _flagged(opted_out) == []
        assert _verdict(opted_out) == _verdict(default)


class TestTheFourBootstrapsDoNotDrift:
    """Four copies of one block, kept identical by a script rather than by memory.

    The duplication is forced — each wrapper must run before any ``factlog`` import is
    possible, so there is nowhere shared to put it. Duplication a human is asked to
    maintain drifts; this makes the drift a test failure on the commit that causes it.

    **These are TEXT checks, not behaviour checks**, and the distinction is worth more
    than it looks. Comparing the copies byte-for-byte means a mutant in any single copy
    fails this class whatever the mutant does — so "the mutant died" says nothing about
    whether that copy's behaviour is measured anywhere. What kills a mutant is the
    reading, not that it was killed (#617, #621). The behaviour of ``factlog_config``'s
    copy is measured by ``test_two_trees_produce_two_different_lines``, and of
    ``common.py``'s by ``TestWhichTreeValidatePyImports`` above. No test measures
    ``compile_facts.py``'s or ``literal_types.py``'s: the membership guard only changes
    an outcome when ``_ROOT`` is ALREADY on ``sys.path`` behind another tree, and no test
    drives either of those two in that shape — dropping it from them moves no behaviour
    test (measured at 3e6aafc + #621, one wrapper at a time; the bootstrap comment in
    ``tools/common.py`` carries all four runs).
    """

    def _block(self, path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        start = text.index(BOOTSTRAP_START)
        end = text.index(BOOTSTRAP_END, start) + len(BOOTSTRAP_END)
        return text[start:end]

    def test_all_four_wrappers_carry_the_byte_identical_block(self):
        blocks = {path.name: self._block(path) for path in WRAPPERS}
        reference = blocks[WRAPPERS[0].name]
        for name, block in blocks.items():
            assert block == reference, f"{name} drifted from {WRAPPERS[0].name}"

    def test_the_block_is_the_real_one(self):
        """Guard the guard: an emptied block would satisfy "all four are identical"."""
        block = self._block(WRAPPERS[0])
        assert 'os.environ.get("FACTLOG_PREFER_INSTALLED") == "1"' in block
        assert "sys.path.append(str(_ROOT))" in block
        assert "sys.path.insert(0, str(_ROOT))" in block
        # A LOOKUP, not an execution. ``try: import factlog / except ImportError`` passes
        # every install-form test above and then swallows the ``ModuleNotFoundError`` of
        # an installed package with a missing dependency — handing the run back to the
        # bundle with ``=1`` set and nothing printed. See
        # ``test_a_broken_install_is_not_silently_routed_around``.
        assert 'importlib.util.find_spec("factlog") is None' in block
        # The membership guard is what keeps two behaviour tests working, one copy each:
        # ``test_two_trees_produce_two_different_lines`` through ``factlog_config.py``'s,
        # and ``TestWhichTreeValidatePyImports`` through ``common.py``'s. Both front a
        # tree on PYTHONPATH that already lists _ROOT, and without this line the insertion
        # below would overtake it. Dropping the guard from ``compile_facts.py`` or
        # ``literal_types.py`` instead moves no behaviour test at all (measured at
        # 3e6aafc + #621, one wrapper at a time; the bootstrap comment in
        # ``tools/factlog_config.py`` carries the four runs and the import-order reason).
        # The assertion below is textual, so it fires for all four regardless of that.
        assert "if str(_ROOT) not in sys.path:" in block

    def test_only_those_four_scripts_insert_the_distribution_root(self):
        """The premise the fix rests on, measured instead of remembered.

        The other ``tools/`` scripts insert ``_TOOLS_DIR`` — their own directory, to
        resolve sibling ``tools`` modules. That is a different mechanism and is out of
        scope; they reach the bundled package transitively through ``factlog_config``,
        which is one of these four.

        Scope, stated so the guarantee is not read as wider than it is: this matches one
        **literal line**. A fifth script that reached the distribution root by another
        spelling — ``Path(__file__).parents[1]``, a helper, a relative walk — would pass
        this test while opening exactly the hole it is here to catch. It defends against
        copy-paste, which is how the existing four arrived, and not against invention.
        """
        inserting = {
            path.name
            for path in sorted(TOOLS.glob("*.py"))
            if DIST_ROOT_INSERT in path.read_text(encoding="utf-8")
        }
        assert inserting == {path.name for path in WRAPPERS}
