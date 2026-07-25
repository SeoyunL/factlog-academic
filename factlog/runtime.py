# SPDX-License-Identifier: Apache-2.0
"""Which factlog code is running — version and import path (#554).

Every deterministic step reports the engine it ran (``engine: wirelog / pyrewire``)
and the policy it loaded, but nothing reported *itself*. In an environment where a
bundled 0.6.0 and a working tree 0.7.0 both exist — the ordinary state of a plugin
that ships its own copy of ``tools/`` — a report could not be attributed to the code
that produced it, and four already-closed defects (#208, #491, #527, #547) were
re-diagnosed as live bugs from artifacts one line would have disambiguated.

Why this line belongs *in* ``facts/logic_report.txt`` while ``target KB`` deliberately
does not (``tests/unit/test_unaimed_engine_step_guard.py:348`` asserts
``"target KB" not in report``):

* ``target KB`` names an **input location** — where the file being read lives. A
  reader holding the report already knows that: they found the file. Putting it in
  the artifact adds a developer's absolute home path to a byte-compared golden and
  tells the reader nothing they did not know.
* ``factlog:`` names the **producer's identity** — the same category as the
  ``engine:`` line directly beneath it, which has always been in the report. It is
  not recoverable from the artifact's location, and it is exactly what makes the
  artifact attributable evidence rather than an unsigned text file.

Scope limit, deliberate: this reports the identity of the factlog **package** that
is imported, not of the **script** that is executing. A bundled ``tools/run_logic_check.py``
driving a working-tree ``factlog`` package is a real shape, and naming the running
script is #553's territory — the block that would have to change is the very sys.path
bootstrap #553 rewrites, so entangling the two here would make both harder to land.

Dependencies are kept to ``importlib.metadata`` / ``pathlib`` / ``factlog.__version__``
on purpose: this module is imported by ``factlog/cli.py``, whose whole design is to
defer heavy imports (``factlog.common`` and friends) until a subcommand needs them.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

UNKNOWN = "<unknown>"


class Advisory(NamedTuple):
    """A severity/title/hints triple the doctor renders as a :class:`factlog.cli.Check`.

    Deliberately NOT ``factlog.cli.Check``: cli imports this module, so importing
    back would be circular, and the judgement below has no business knowing about
    ``blocks_setup`` — it must never gate anything (see :func:`installation_skew`).
    """

    severity: str
    title: str
    hints: tuple[str, ...] = ()


def running_factlog() -> tuple[str, str]:
    """``(__version__, __file__)`` of the factlog package this process imported.

    ``__file__`` is returned **verbatim, not resolved**. Resolving would rewrite a
    symlinked or worktree path into its target, and the value would then no longer
    match what ``python -c "import factlog; print(factlog.__file__)"`` prints — the
    one command a reader runs to check the line, and the exact command #553's
    reproduction uses. A path that disagrees with the reader's own measurement is
    worse than no path.

    Both fields degrade to ``<unknown>`` rather than raising: this is diagnostic
    output, and it is consulted precisely when the installation is odd (a namespace
    package, a zipimport, a stripped ``__file__``).
    """
    import factlog

    version = str(getattr(factlog, "__version__", None) or UNKNOWN)
    path = getattr(factlog, "__file__", None)
    return version, str(path) if path else UNKNOWN


def provenance_line(prefix: str = "factlog") -> str:
    """``factlog: 0.7.0 (/abs/path/to/factlog/__init__.py)``.

    One shape, one producer, so the logic report header, the doctor row and the three
    engine-step stdout lines cannot drift into three dialects of the same fact.
    """
    version, path = running_factlog()
    return f"{prefix}: {version} ({path})"


def _package_parent(imported_file: str) -> Path | None:
    """The directory that *contains* the ``factlog`` package, from its ``__init__.py``.

    That is the level a distribution root is comparable with: an editable install
    records the checkout root (which holds ``factlog/``), and a wheel install's
    ``locate_file("")`` is site-packages (which also holds ``factlog/``).
    """
    if not imported_file or imported_file == UNKNOWN:
        return None
    try:
        return Path(imported_file).parent.parent
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def _same_dir(left: Path | str | None, right: Path | str | None) -> bool:
    """Whether two directory paths name the same place, symlinks tolerated.

    Lexical first (so the judgement stays testable with paths that do not exist),
    then ``realpath`` as a *second chance*. The fallback only ever turns a WARN
    into silence, never the other way round: a venv reached through a symlinked
    home directory is the commonest false positive available, and a diagnostic
    that cries wolf is one users learn to ignore (the lesson ``installed_distributions``
    already carries).
    """
    if left is None or right is None:
        return False
    import os

    a, b = str(left), str(right)
    if os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b)):
        return True
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def installation_skew(
    dist_name: str | None,
    dist_version: str | None,
    dist_root: str | None,
    imported_version: str,
    imported_file: str,
) -> Advisory | None:
    """Does the distribution pip installed differ from the package actually imported?

    Pure and argument-injected (the shape ``factlog.cli.rename_migration_check`` uses)
    so all four states are pinned by unit tests instead of by a venv dance. The caller
    does the metadata I/O; this function only judges.

    The four states:

    * **no distribution** — nothing to compare against (a plain source checkout with
      no install at all). Silence: an absent yardstick is not a discrepancy.
    * **different roots** — pip's record points at one tree and ``import factlog``
      landed in another. This is the #554 hazard itself: the report you are reading
      was produced by code pip does not know about. ``WARN``.
    * **same root, same version** — the ordinary healthy install. Silence.
    * **same root, version differs** — the everyday developer state right after a
      version bump with no reinstall: pip's metadata is stale, the code is not. This
      is ``INFO``, not ``WARN``, because warning on it would fire in every maintainer's
      clone and train them to ignore the row that matters.

    It **never returns FAIL**. ``doctor``'s exit code is gated on FAIL alone, and
    smoke.sh/setup.sh depend on a healthy environment exiting 0; a diagnostic added
    to make skew *observable* must not be able to turn a working install into a
    failing one.
    """
    if dist_root is None or dist_version is None:
        return None
    imported_root = _package_parent(imported_file)
    if imported_root is None:
        return None

    label = dist_name or "factlog"
    if not _same_dir(dist_root, imported_root):
        # Distinct from cli._shadow_factlog_dir's warning on purpose: that one is about
        # a stray ./factlog directory on sys.path[0] in the *current working directory*.
        # This one is about the installed distribution disagreeing with the imported
        # package, wherever either lives.
        return Advisory(
            "WARN",
            (
                f"설치된 배포판({label} {dist_version}, {dist_root})과 "
                f"실행 중인 factlog 패키지({imported_version}, {imported_root})의 "
                "위치가 다릅니다"
            ),
            (
                "[터미널] 지금 실행되는 코드는 pip이 등록한 배포판이 아닙니다 — "
                "실행하려는 트리에서 pip install -e . 로 다시 설치하세요",
            ),
        )
    if dist_version != imported_version:
        return Advisory(
            "INFO",
            (
                f"설치된 배포판 {label} {dist_version} 과 실행 중인 factlog 패키지 "
                f"{imported_version} 의 버전 기록이 다릅니다 (같은 위치: {imported_root})"
            ),
            (
                "[터미널] 버전을 올린 뒤 재설치하지 않았다면 정상입니다 — "
                "pip install -e . 로 메타데이터만 갱신하면 사라집니다",
            ),
        )
    return None


def file_url_to_path(url: str) -> str | None:
    """A PEP 610 ``file://`` URL as a filesystem path — on any platform, from any.

    A pure string transform, deliberately, and NOT ``urllib.request.url2pathname``:
    that function dispatches on the *running* platform, so its Windows branch can
    neither execute nor be tested here — all three CI jobs are ``ubuntu-latest``
    (.github/workflows/ci.yml), and neither Windows nor macOS runs. Fixing a
    Windows-only defect with a call no CI can enter would leave the defect exactly
    as unobservable as it was. As a string function, the Windows input is a unit
    test that runs on Linux.

    The transform that matters: ``urlparse`` gives ``file:///C:/Users/me/factlog``
    the path ``/C:/Users/me/factlog``, and that leading slash belongs to the URL
    grammar, not to any Windows path. Left in, it makes the comparison in
    :func:`_same_dir` unsatisfiable —

        ntpath.normcase(ntpath.normpath("/C:/Users/me/factlog"))  -> "\\\\c:\\\\users\\\\me\\\\factlog"
        ntpath.normcase(ntpath.normpath(r"C:\\Users\\me\\factlog")) -> "c:\\\\users\\\\me\\\\factlog"

    — and no realpath second chance can rescue it, so EVERY Windows editable
    install would carry a permanent WARN. That is precisely the cry-wolf failure
    this module's docstring names as the thing to avoid, and it would have been a
    false positive introduced by the fix rather than found by it.

    Returns None for anything that is not a usable ``file://`` URL, so the caller
    falls back to ``locate_file`` rather than comparing against a guess.
    """
    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    if not path:
        return None
    host = parsed.netloc
    if host and host.lower() != "localhost":
        # file://server/share — a UNC location. Preserved rather than dropped so
        # such an install at least compares as itself instead of as its share path.
        return f"//{host}{path}"
    if len(path) > 2 and path[0] == "/" and path[1].isalpha() and path[2] == ":":
        # "/C:/Users/me" -> "C:/Users/me". A drive letter, not a root directory.
        return path[1:]
    return path


def _dist_root(dist) -> str | None:
    """Where a distribution's code lives, editable installs included.

    A wheel install answers ``locate_file("")`` — site-packages, the directory that
    holds the installed ``factlog/``. An **editable** install answers site-packages
    too (only a ``.pth`` shim lives there), so taking that at face value would report
    a mismatch for every editable install in existence. ``direct_url.json``
    (PEP 610) carries the real answer for those: ``url`` is the checkout it points at.

    A non-editable ``pip install .`` also records ``direct_url.json``, with
    ``dir_info.editable`` absent/false — that one really did copy the code into
    site-packages, so it falls through to ``locate_file``.
    """
    import json

    try:
        raw = dist.read_text("direct_url.json")
    except Exception:  # pragma: no cover - unreadable metadata is not a crash
        raw = None
    if raw:
        try:
            record = json.loads(raw)
            url = record.get("url") or ""
            if record.get("dir_info", {}).get("editable"):
                # None (not a usable file:// URL) falls through to locate_file rather
                # than being compared: a guessed root is what produces a false WARN.
                converted = file_url_to_path(url)
                if converted:
                    return converted
        except (ValueError, AttributeError, TypeError):
            pass
    try:
        located = dist.locate_file("")
    except Exception:  # pragma: no cover - defensive
        return None
    return str(located) if located is not None else None


def installed_factlog_dist(distributions=None) -> tuple[str, str, str] | None:
    """``(name, version, root)`` of the factlog distribution pip installed, or None.

    Only a real ``.dist-info`` under site-packages counts, for the reason
    ``factlog.cli.installed_distributions`` documents: ``importlib.metadata`` walks
    ``sys.path``, so a source checkout's leftover ``factlog.egg-info/`` reads as an
    installed dist — and here that stale record would report the checkout's OLD
    version against the very tree it sits in, i.e. a permanent false INFO row in
    every developer's clone.

    ``factlog-academic`` wins over the legacy ``factlog`` when both are installed.
    That double-install hazard is already reported, once, by
    ``factlog.cli.rename_migration_check``; this row is a different axis and says
    nothing about it, so nobody hears the same warning twice.

    ``distributions`` is injectable so the lookup is testable without a venv.
    """
    import importlib.metadata as _md

    found: dict[str, tuple[str, str, str]] = {}
    for dist in (distributions or _md.distributions)():
        meta = getattr(dist, "metadata", None)
        name = meta["Name"] if meta else None
        path = getattr(dist, "_path", None)
        if not name or path is None:
            continue
        if name not in ("factlog", "factlog-academic"):
            continue
        if not (Path(path).name.endswith(".dist-info") and "site-packages" in Path(path).parts):
            continue
        root = _dist_root(dist)
        if root is None:
            continue
        found[name] = (name, str(getattr(dist, "version", "") or UNKNOWN), root)
    return found.get("factlog-academic") or found.get("factlog")
