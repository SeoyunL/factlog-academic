# SPDX-License-Identifier: Apache-2.0
"""``FACTLOG_PREFER_INSTALLED``: let the contributor's own tree win over the bundle (#553).

A plugin ships its own copy of ``tools/``, and every wrapper in it puts its OWN
distribution root at ``sys.path[0]``. So ``${CLAUDE_PLUGIN_ROOT}/tools/run_logic_check.py``
imports the bundled ``factlog`` even when the contributor has their working tree
installed — they verify a change against code that does not contain it. Four already
closed defects (#208, #491, #527, #547) were re-diagnosed as live bugs from exactly
that shape.

``FACTLOG_PREFER_INSTALLED=1`` makes the wrappers **append** their root instead of
prepending it. Append rather than skip, deliberately: skipping would make a bare
checkout with nothing installed raise ``ImportError``, i.e. the opt-out would invent a
new failure mode. Appending means an installed package wins when one exists and the
bundle still wins when none does, so turning it on cannot break a working setup —
which case 4 below measures rather than asserts.

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
    isolated: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run ``<script_root>/tools/compile_facts.py`` and hand back its streams.

    ``compile_facts`` is the probe because it prints ``provenance_line()`` as its first
    stdout line and needs no engine, so the answer to "which package did this process
    import" is the first thing it says.

    ``isolated`` adds ``-S``, which drops ``site-packages`` from ``sys.path`` entirely.
    That is how case 4 makes "no other factlog is reachable" true on a developer machine
    that has one editably installed, instead of merely hoping for it.
    """
    argv = [sys.executable]
    if isolated:
        argv.append("-S")
    argv.append(str(script_root / "tools" / "compile_facts.py"))
    return subprocess.run(
        argv,
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

    def test_one_still_runs_when_no_other_factlog_is_reachable(self, tmp_path):
        """Append, not skip: the opt-out must not be able to break a bare checkout.

        With ``-S`` there is no site-packages and no ``PYTHONPATH``, so the bundle's own
        appended root is the only ``factlog`` in existence. A "skip the insertion"
        implementation raises ``ImportError`` here; appending exits 0 and names the
        bundle. This is the case that decides between the two designs.
        """
        bundle = _bundle(tmp_path)
        kb = _new_kb(tmp_path, "kb_fallback")
        cwd = _neutral_cwd(tmp_path)

        result = _compile(bundle, kb, cwd, prefer="1", isolated=True)

        assert result.returncode == 0, result.stdout + result.stderr
        line = _factlog_line(result)
        assert "9.9.9+" in line
        assert str(bundle / "factlog" / "__init__.py") in line


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

    def test_a_wrapper_inside_the_package_compares_the_tree_with_itself(self):
        """Why ``tools/compile_facts.py`` carries the check and ``factlog/compile_facts.py`` does not.

        Inside the package ``__file__`` IS the package file, so the comparison is the
        package tree against itself and can never fire. Pinned here so nobody "tidies"
        the check back into ``main()`` where it would be silently dead.
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


class TestTheFourBootstrapsDoNotDrift:
    """Four copies of one block, kept identical by a script rather than by memory.

    The duplication is forced — each wrapper must run before any ``factlog`` import is
    possible, so there is nowhere shared to put it. Duplication a human is asked to
    maintain drifts; this makes the drift a test failure on the commit that causes it.
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
        # The membership guard is what keeps ``test_two_trees_produce_two_different_lines``
        # working: that test fronts a tree on PYTHONPATH that already lists _ROOT, and
        # without this line the insertion below would overtake it.
        assert "if str(_ROOT) not in sys.path:" in block

    def test_only_those_four_scripts_insert_the_distribution_root(self):
        """The premise the fix rests on, measured instead of remembered.

        The other ``tools/`` scripts insert ``_TOOLS_DIR`` — their own directory, to
        resolve sibling ``tools`` modules. That is a different mechanism and is out of
        scope; they reach the bundled package transitively through ``factlog_config``,
        which is one of these four. If a fifth script ever inserts a distribution root,
        this fails and the fix has a hole.
        """
        inserting = {
            path.name
            for path in sorted(TOOLS.glob("*.py"))
            if DIST_ROOT_INSERT in path.read_text(encoding="utf-8")
        }
        assert inserting == {path.name for path in WRAPPERS}
