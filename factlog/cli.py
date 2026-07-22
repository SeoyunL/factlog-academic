# SPDX-License-Identifier: Apache-2.0
"""factlog command-line helper.

The skill itself is installed as a Claude Code **plugin** (see README), so this
CLI does not install the skill. It provides environment and knowledge-base
helpers for the deterministic engine:

- `doctor`  — verify Python and pyrewire meet factlog's requirements.
- `init`    — scaffold an empty knowledge base layout (stub; see plan).
- `setup`   — one-shot bootstrap: doctor, ensure deps, init KB, re-check.
- `ingest`  — convert a binary/office file (docx, pdf, ...) into a text source
              under sources/ so fact extraction can read it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path as _Path
from typing import Callable, NamedTuple

from factlog import __version__, ingest
from factlog import config as factlog_config
from factlog.common import _atomic_write_text

MIN_PYTHON = (3, 11)
MIN_PYREWIRE = (1, 0, 3)  # bundles wirelog v0.52.0 with \" escape support (wirelog#924)


# _atomic_write_text (temp + os.replace) now lives in factlog.common so
# compile_facts.py can write accepted.dl atomically too (#329). Re-exported here
# unchanged: run-file JSON writers below still call it as a module-level name, and
# an interrupted/`amend`/`eject` run can never leave a truncated runs/*.json behind.


def _atomic_write_csv(csv_path, rows, fieldnames) -> None:
    """Write candidate *rows* to *csv_path* atomically (temp + os.replace).

    Uses extrasaction="ignore" so extra row keys are dropped, matching what every
    candidates.csv writer relied on. Mirrors _atomic_write_text for run-file JSON.
    """
    import csv
    import os

    tmp = csv_path.with_name(csv_path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, csv_path)


def _require_kb(target, command: str, *, suffix: str = "") -> bool:
    """True if *target* is a factlog KB (has sources/); else print the standard
    error to stderr and return False so the caller can pick its own exit code.

    *command* is the subcommand name in the message ("factlog <command>: ...").
    *suffix* appends command-specific guidance (e.g. an ingest hint).
    """
    if (_Path(target) / "sources").is_dir():
        return True
    tail = f" {suffix}" if suffix else ""
    print(f"factlog {command}: {target} is not a factlog KB (no sources/).{tail}", file=sys.stderr)
    return False


def _recompile_accepted(target, command: str) -> bool:
    """Recompile facts/accepted.dl after a candidates.csv change.

    Returns True on success; on failure prints the standard "compile_facts failed"
    error (tagged with *command*) and returns False. Callers add their own
    command-specific follow-up messaging.
    """
    import os
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "factlog.compile_facts"],
        env=dict(os.environ, FACTLOG_ROOT=str(target)),
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return True
    print(f"factlog {command}: compile_facts failed: {(proc.stderr or proc.stdout).strip()}", file=sys.stderr)
    return False


def _version_tuple(value: str) -> tuple[int, ...]:
    import re

    return tuple(int(part) for part in re.findall(r"\d+", value)[:3])


def _pyrewire_ok() -> bool:
    """Return True iff pyrewire is importable and meets the version floor."""
    try:
        import pyrewire  # type: ignore
    except ImportError:
        return False
    return _version_tuple(str(getattr(pyrewire, "__version__", "0"))) >= MIN_PYREWIRE


class Check(NamedTuple):
    """A single doctor diagnostic.

    * severity — one of ``OK`` / ``INFO`` / ``WARN`` / ``FAIL``. Only ``FAIL``
      flips the doctor exit code; ``INFO``/``WARN`` are advisory and must never
      change exit status (smoke.sh/setup.sh depend on exit 0 in a healthy env).
    * title    — the one-line status shown after the severity tag.
    * hints    — follow-up guidance lines. Each hint is prefixed at render time
      with ``→`` and already carries an execution-location tag such as
      ``[터미널]`` (a shell) or ``[Claude Code]`` (inside the assistant).
    * blocks_setup — whether a ``FAIL`` here should gate ``factlog setup``. The
      standalone ``doctor`` gates on *every* FAIL, but ``setup`` only performs
      pip install + KB init, which do not use git — so a git FAIL is reported by
      doctor yet must not flip setup's exit code. Diagnostics setup genuinely
      needs (Python floor, pyrewire) keep the default ``True``.
    """

    severity: str
    title: str
    hints: tuple[str, ...] = ()
    blocks_setup: bool = True


def _harden_stdout() -> None:
    """Best-effort: make stdout/stderr tolerate non-ASCII on C/ASCII locales.

    doctor prints Korean text and an em-dash (U+2014). On a stream whose encoding
    is ``ascii`` (e.g. ``LC_ALL=C`` or ``PYTHONIOENCODING=ascii``) that would raise
    ``UnicodeEncodeError`` and crash the very tool meant to diagnose broken
    environments. Switching the error handler to ``backslashreplace`` degrades
    gracefully — non-ASCII shows as escapes, but the exit code, the diagnostic
    lines and the ASCII ``Python`` token still come through, and nothing crashes.

    Guarded so it is a harmless no-op where ``reconfigure`` is missing (pre-3.7,
    or a stream that is not a ``TextIOWrapper`` such as a captured buffer).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (ValueError, OSError, AttributeError):
            pass


def _shadow_factlog_dir() -> str | None:
    """Return the path of a shadowing ``./factlog`` folder, or None.

    Heuristic (all three must hold, so this is WARN-only and false-positive shy):
    the cwd has a ``factlog`` subdirectory, the cwd has *no* ``pyproject.toml``
    (so it is not the repo checkout), and that subdirectory is not the actually
    imported ``factlog`` package. Such a stray folder shadows the installed
    package on ``sys.path[0]`` and makes ``python -m factlog`` import the wrong
    code.

    Known limitations (documented, behaviour intentionally left as-is):

    * The ``candidate.resolve() == pkg_dir`` guard means that when a stray
      ``./factlog`` has *already* hijacked the import (so the imported package
      *is* the stray folder), this returns None and the warning is suppressed —
      exactly the case where it would be most useful, but distinguishing it
      reliably from a legitimate in-repo run is not possible from cwd alone.
    * Conversely, any unrelated directory that merely happens to be named
      ``factlog`` (and sits next to no ``pyproject.toml``) yields a false-positive
      WARN. This stays WARN-only precisely so such a false positive never affects
      the exit code.
    """
    import factlog as _pkg

    cwd = _Path.cwd()
    candidate = cwd / "factlog"
    if not candidate.is_dir():
        return None
    if (cwd / "pyproject.toml").exists():
        return None
    try:
        pkg_dir = _Path(_pkg.__file__).resolve().parent
    except (AttributeError, TypeError):
        return None
    if candidate.resolve() == pkg_dir:
        return None
    return str(candidate)


def _collect_doctor_checks() -> list[Check]:
    """Gather doctor diagnostics as structured :class:`Check` rows.

    Pure data: builds and returns the checks without printing, so unit tests can
    assert severities directly. Rendering/exit-code logic lives in
    :func:`_render_doctor`.
    """
    import os
    import shutil

    checks: list[Check] = []

    # (1)+(2) Python version floor + interpreter surfacing (WindowsApps stub).
    interp = sys.executable or "?"
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    if sys.version_info[:2] < MIN_PYTHON:
        checks.append(
            Check("FAIL", f"Python {py} < 3.11 필요 ({interp})",
                  ("[터미널] Python 3.11 이상을 설치한 뒤 다시 실행하세요",))
        )
    elif "WindowsApps" in interp:
        # Microsoft Store Python stub: often a non-functional launcher shim.
        checks.append(
            Check("WARN", f"Python {py} (Store stub: {interp})",
                  ("[터미널] python.org 정식 배포판 설치를 권장합니다",))
        )
    else:
        checks.append(Check("OK", f"Python {py} ({interp})"))

    # pyrewire engine floor (unchanged behaviour/message intent).
    try:
        import pyrewire  # type: ignore

        version = str(getattr(pyrewire, "__version__", "?"))
        if _version_tuple(version) >= MIN_PYREWIRE:
            checks.append(Check("OK", f"pyrewire {version}"))
        else:
            floor = ".".join(map(str, MIN_PYREWIRE))
            checks.append(
                Check("FAIL", f"pyrewire {version} < {floor}",
                      ("[터미널] pip install -r requirements.txt",))
            )
    except ImportError:
        checks.append(
            Check("FAIL", "pyrewire not installed",
                  ("[터미널] pip install -r requirements.txt",))
        )

    # (1) git availability. macOS ships it via the Command Line Tools.
    # FAIL for doctor's sake, but blocks_setup=False: `setup` (pip + KB init)
    # does not touch git, so a missing git must not flip setup's exit code.
    if shutil.which("git"):
        checks.append(Check("OK", "git"))
    elif sys.platform == "darwin":
        checks.append(
            Check("FAIL", "git이 없습니다", ("[터미널] xcode-select --install",),
                  blocks_setup=False)
        )
    else:
        checks.append(
            Check("FAIL", "git이 없습니다",
                  ("[터미널] 패키지 매니저로 git을 설치하세요 (예: apt install git)",),
                  blocks_setup=False)
        )

    # (3) shadowing ./factlog folder (WARN-only, false-positive shy).
    shadow = _shadow_factlog_dir()
    if shadow is not None:
        checks.append(
            Check("WARN", f"이 폴더에 factlog/ 폴더가 있어 패키지를 가릴 수 있습니다 ({shadow})",
                  ("[터미널] 다른 위치에서 실행하거나 이 폴더 이름을 바꾸세요",))
        )

    # (4) FACTLOG_PYTHON override.
    fp = os.environ.get("FACTLOG_PYTHON")
    perm_hint = "[터미널] 영구 등록: echo 'export FACTLOG_PYTHON=…' >> ~/.zshrc"
    if not fp:
        checks.append(
            Check("INFO", "FACTLOG_PYTHON 미설정 (시스템 python3 사용)", (perm_hint,))
        )
    elif os.path.exists(fp):
        checks.append(Check("OK", f"FACTLOG_PYTHON = {fp} (존재함)"))
    else:
        checks.append(
            Check("WARN", f"FACTLOG_PYTHON = {fp} (경로 없음)",
                  ("[터미널] 경로를 고치거나 unset FACTLOG_PYTHON 하세요", perm_hint))
        )

    checks.extend(_rename_migration_checks())

    return checks


def installed_distributions(distributions=None) -> set[str]:
    """Names of distributions pip actually installed.

    NOT every entry importlib.metadata reports: it walks sys.path, and a source
    checkout carries a leftover `factlog.egg-info/` from before the rename, which
    it happily counts as an installed `factlog` dist. That made the migration
    check fire in every developer's clone — a diagnostic that cries wolf is one
    users learn to ignore, so trust only a real `.dist-info` under site-packages.

    `distributions` is injectable so the check can be tested without a venv.
    """
    import importlib.metadata as _md

    found: set[str] = set()
    for dist in (distributions or _md.distributions)():
        meta = dist.metadata
        name = meta["Name"] if meta else None
        path = getattr(dist, "_path", None)
        if not name or path is None:
            continue
        if path.name.endswith(".dist-info") and "site-packages" in path.parts:
            found.add(name)
    return found


def rename_migration_check(installed: set[str], factlog_on_path: bool) -> Check | None:
    """The `factlog` → `factlog-academic` rename hazard (#228).

    Both distributions own the same `factlog` module AND the same `factlog` console
    script, so pip installs them side by side without a word. Two states follow, and
    the second is the one users actually land in:

    * both installed — uninstalling the old one will DELETE the shared command;
    * only the new one installed, but no `factlog` command — that already happened,
      and pip still cheerfully reports factlog-academic as installed.

    Pure, so both states are pinned by unit tests rather than by a venv dance.
    """
    if "factlog" in installed and "factlog-academic" in installed:
        return Check(
            "WARN",
            "옛 배포판 factlog 과 factlog-academic 이 함께 설치돼 있습니다",
            (
                "[터미널] pip uninstall factlog 뒤 반드시 재설치하세요 "
                "(둘이 같은 factlog 명령을 공유하므로, 옛 것만 지우면 그 명령이 사라집니다)",
            ),
        )
    if "factlog-academic" in installed and not factlog_on_path:
        return Check(
            "WARN",
            "factlog-academic 은 설치돼 있는데 factlog 명령이 없습니다",
            (
                "[터미널] pip install -e . 로 재설치하세요 "
                "(옛 factlog 배포판을 지울 때 공유하던 factlog 명령까지 삭제된 상태입니다)",
            ),
        )
    return None


def _rename_migration_checks() -> list[Check]:
    import shutil

    found = rename_migration_check(installed_distributions(), shutil.which("factlog") is not None)
    return [found] if found else []


def _render_doctor(checks: list[Check], emit_summary: bool = False, gate: str = "all") -> bool:
    """Print *checks* in the rich doctor layout and return the pass/fail gate.

    *emit_summary* prints a concluding banner (only the standalone `cmd_doctor`
    does this; `cmd_setup` calls the doctor twice and renders lines without a
    banner to avoid duplication).

    *gate* selects which FAIL rows count against the returned bool:

    * ``"all"``   — any FAIL fails (doctor's own exit code).
    * ``"setup"`` — only FAIL rows with ``blocks_setup=True`` fail, so a missing
      git (which setup does not use) never flips setup's exit code.

    The summary banner always reports the *total* FAIL count regardless of gate.
    """
    _harden_stdout()

    print("factlog doctor — 설치 점검")
    print()

    fails = 0
    for check in checks:
        if check.severity == "FAIL":
            fails += 1
        print(f"{check.severity:<6}{check.title}")
        for hint in check.hints:
            print(f"      → {hint}")

    if emit_summary:
        print("─" * 28)
        if fails == 0:
            print("결과: 이상 없음")
        else:
            print(f"결과: FAIL {fails}개. 위 → 안내를 처리한 뒤 doctor를 다시 실행하세요.")

    if gate == "setup":
        return not any(c.severity == "FAIL" and c.blocks_setup for c in checks)
    return fails == 0


def _run_doctor_checks(emit_summary: bool = False, gate: str = "all") -> bool:
    """Collect and render the doctor checks. Returns the gate result (see
    :func:`_render_doctor`).

    Shared by `cmd_doctor` (gate="all") and `cmd_setup` (gate="setup") so setup
    reports the exact same diagnostics the standalone doctor would, while only
    gating on the checks it actually depends on.
    """
    return _render_doctor(_collect_doctor_checks(), emit_summary=emit_summary, gate=gate)


def cmd_doctor(_args: argparse.Namespace) -> int:
    return 0 if _run_doctor_checks(emit_summary=True) else 1


_TEMPLATES: dict[str, str] = {
    "policy/prompts/text_to_fact.md": """\
# Text-to-Fact Extraction Prompt

You are a fact extraction assistant. Given the source text below, extract
atomic, verifiable facts in the form (subject, relation, object).

## Source text

{source_text}

## Output format

Return one fact per line as CSV with columns:
subject,relation,object,source,status,confidence,note

For typed literal objects, you may use compact compound terms when they preserve
structure better than prose strings: date(2030), date(2030,1), date(2030,1,15),
number(2.5), ordinal(3), amount(100,"억"). Keep entity objects as plain names.
A year-only date must still carry the wrapper — date(2030), not a bare 2030.
""",
    "policy/prompts/text_to_datalog.md": """\
# Text-to-Datalog Query Prompt

Given the following schema context and natural-language question, produce a
valid Datalog query that answers the question.

## Schema context

{{SCHEMA_CONTEXT}}

## Question

{{QUESTION}}

## Output

Return only the Datalog query, no explanation.
""",
    "policy/prompts/self_correct.md": """\
# Self-Correction Prompt

The Datalog query below produced errors. Fix the query so it is valid.

## Schema context

{{SCHEMA_CONTEXT}}

## Logic report

{{LOGIC_REPORT}}

## Draft query

{{DRAFT_QUERY}}

## Output

Return only the corrected Datalog query, no explanation.
""",
    "policy/prompts/natural_language_to_policy.md": """\
# Natural Language to Policy Prompt

Convert the following natural-language policy description into Datalog rules.

## Policy text

{{POLICY_TEXT}}

## Output

Return only valid Datalog rules, one per line, no explanation.
""",
    "policy/questions.md": """\
# Research questions

- [q1] What are the key facts to extract from this knowledge base?
""",
    "policy/logic-policy.md": """\
# Logic policy

This file describes the Datalog rules used to reason over the knowledge base.

## Rules

Add your policy rules here. Each rule should be documented with a brief
explanation of its purpose.
""",
    "policy/single-valued.md": """\
# Single-valued (functional) relations
#
# List relation names that may hold AT MOST ONE object per subject. One relation
# NAME per line; '#' comment lines and '-' bullets are allowed; quote a name
# containing spaces in backticks.
#
# This is what turns a contradiction into an ERROR instead of two facts sitting
# quietly side by side. If two distinct objects are asserted for the same
# (subject, single-valued relation) it is reported as a CONFLICT and the KB
# refuses to compile until a human resolves it.
#
# To SEE conflicts:
#   factlog status              -> `conflicts: N`
#   tools/check_conflicts.py    -> each conflict, and the resolution steps
#   /factlog check              -> the same, inside Claude Code
#
# To RESOLVE one (never by hand-editing facts/candidates.csv, which bypasses the
# gate this KB is built around):
#   factlog eject --fact SUBJECT RELATION OBJECT    retire a row
#   factlog amend SUBJECT RELATION OBJECT --set-object NEW    correct one
#
# If the two values are a supertype and its subtype (a cohort study IS an
# observational study), neither is wrong: declare the relationship in
# policy/value-hierarchy.md and both rows are kept.
#
# A relation you do NOT list may hold many objects per subject, which is the
# right default for things like `cites` or `mentions`.
#
# Example (remove the leading '# ' to activate):
# published_year
# `연구 유형`
""",
    "policy/relation-aliases.md": """\
# Relation aliases
#
# Map a SURFACE relation name to the CANONICAL one you want the engine to use, so
# facts written `게재연도` and `발행년도` are treated as the one relation
# `published_year`. Without this, the engine sees them as two unrelated relations
# and a query for one misses facts stored under the other.
#
# Format: one mapping per line, `raw` -> `canonical`. BACKTICKS ARE REQUIRED around
# BOTH names (unlike the other policy files, where they are optional) -- a line with
# an arrow but no backticks is reported as malformed and skipped, not applied. `#`
# comment lines and a leading `-`/`*` bullet are allowed.
#
# The canonical name is the one you declare in the OTHER policy files
# (attribute-relations.md, single-valued.md, typed-relations.md); aliases are folded
# to it before those are applied.
#
# Example (remove the leading '# ' to activate):
# - `게재연도` -> `published_year`
# - `publication_year` -> `published_year`
""",
    "policy/attribute-relations.md": """\
# Attribute (literal-valued) relations
#
# List relation names whose OBJECT is a literal value (a date, number, ordinal,
# ...) rather than a first-class entity. One relation NAME per line; '#' comment
# lines and '-' bullets are allowed; quote a name containing spaces in backticks.
#
# An object at the OBJECT end of these relations is a value, not a thing: it is
# kept out of the entity set, and no dependency path runs THROUGH it (an edge is
# never drawn along an attribute relation). It remains a valid, verifiable
# relation-query object.
#
# Precisely, so you can rely on it:
#   - no edge is drawn ALONG an attribute relation, so no path reaches the value by
#     way of one. That is the guarantee; it is about the relation, not the value.
#   - a value that only ever appears at the object end of attribute relations is
#     therefore not an entity: not listed, not a path node, not a count subject.
#   - the value is still an ENTITY if it appears as a subject anywhere, so it can be
#     named in a query. Being an entity is not the same as being on a path.
#   - a path may START at it only if it is the subject of a NON-attribute relation
#     (that is the only way it gets an outgoing edge).
#   - a path may END at it only if a NON-attribute relation has it as its object
#     (the only way it gets an incoming edge).
#   - a path RUNS THROUGH it only when both of the above hold.
#
# Declaring the relation does not make the value invisible; it stops the attribute
# assertion from being treated as a dependency.
#
# Leave this file with no declarations if every object is a first-class entity.
#
# Example (remove the leading '# ' to activate):
# operates_since
# ranked
""",
    "policy/identity-relations.md": """\
# Identity relations (the OBJECT identifies the SUBJECT)
#
# A title or a DOI names exactly one paper. A publication year or a study type
# does not — many papers share 2023, many share "cohort study".
#
# The distinction changes what a VALUE COLLISION means, and `tools/value_audit.py`
# uses it. When two values of a relation are equal after folding case, spaces and
# punctuation:
#
#   * in an IDENTITY relation, two different subjects sharing it are probably two
#     records of ONE thing — a duplicate record. A different repair, and NOT a
#     query leak, so `value_audit --strict` does not fail on it.
#   * in any other relation values are shared across subjects by design, so the
#     collision is one value split across two spellings — asking for `IL-8` misses
#     the rows filed as `il 8`. That IS a leak, and --strict fails on it.
#
# Declare it here rather than letting the tool guess: inferring identity from the
# data (every value has exactly one subject) breaks precisely when a real duplicate
# record exists, and is true by accident in a small KB.
#
# One relation NAME per line; '#' comments and '-' bullets are allowed; quote a
# name containing spaces in backticks. Absent/empty file → no identity relations,
# so every collision is reported as a leak (the safe direction).
#
# Example (remove the leading '# ' to activate):
# 제목
# DOI
""",
    "policy/value-hierarchy.md": """\
# Value hierarchy (one OBJECT value is a kind of another)
#
# Without this, two values of the same relation are unrelated strings. A cohort
# study IS an observational study, but `relation(P, "연구유형", "관찰연구")?`
# would return only the rows spelled exactly "관찰연구" and silently miss every
# row filed as "코호트연구" — a quiet omission, which is the one thing this KB
# exists to prevent.
#
# Format, one declaration per line ('#' comments and '-' bullets allowed;
# backtick-quote a value containing spaces or a '<'):
#
#   <relation>: <narrower value> ⊂ <broader value>
#
# '<:' and '<' are accepted as ASCII spellings of '⊂'. Ancestors are transitive
# (a ⊂ b and b ⊂ c means a query for c also matches a).
#
# Subsumption applies when a query's OBJECT is matched — asking for the broader
# value returns the narrower rows too. It is one-way: asking for the narrower
# value never returns the broader one. Facts are never rewritten; accepted.dl
# stays a 1:1 projection of the accepted candidate rows.
#
# Example (remove the leading '# ' to activate):
# 연구유형: 코호트연구 ⊂ 관찰연구
# 연구유형: 단면연구 ⊂ 관찰연구
# 대상질환: emphysema <: COPD
""",
    "policy/typed-relations.md": """\
# Typed (comparable-literal) relations
#
# Declare relations whose literal object should be COMPARED, not just matched —
# so the deterministic engine can order them, threshold them, or range over them
# (e.g. "launched after 2030", "rank <= 3"). A relation listed here should ALSO
# be declared in attribute-relations.md (its object is a literal, not an entity).
#
# One declaration per line:
#   - `relation name` : <type> as <ascii_alias>
# where <type> is one of: date | number | ordinal | amount, and <ascii_alias>
# names the engine side-relation that holds the comparable value. The alias must
# be an ASCII identifier ([A-Za-z_][A-Za-z0-9_]*); it is author-chosen so it
# stays a legal engine name even when the relation name is non-ASCII. Quote a
# relation name containing spaces in `backticks`.
#
# Type meanings:
#   date     2030.1 / 2030-01-15  -> sortable yyyymmdd
#   number   1,000 / 3.5 / -2.5   -> fixed-point int64, scaled ×1000 (3 decimals;
#                                    negatives parse — a loss or a delta may be
#                                    negative); thresholds in scaled units
#                                    (e.g. `V >= 2.0` -> `V >= 2000`)
#   ordinal  3rd / 3위 / 제3호      -> int rank (must START with the number:
#                                    `rank 3` does not parse)
#   amount   100억 / 1,000원       -> integer base unit (needs a unit table)
#
# An `amount` line MAY carry an inline unit table; values must be positive ints:
#   - `relation name` : amount as <ascii_alias> (억=1e8, 만=1e4, 원=1)
# Omit the clause to use the built-in default unit table.
#
# Examples (remove the leading '# ' to activate — all-synthetic):
# - `released_on` : date as release_date
# - `headcount` : number as headcount_value
# - `league_rank` : ordinal as rank_value
# - `valuation` : amount as valuation_won (억=1e8, 만=1e4, 원=1)
""",
    "policy/sync-ignore.md": """\
# Sync-ignore list
#
# Source files matching these glob patterns are SKIPPED by `/factlog sync`
# (re-extraction), `factlog ingest --scan`, and coverage gap reporting — even
# when modified. Their already-merged facts are KEPT (use `factlog eject` to
# remove those). Manage with `factlog ignore [--remove] <pattern>`.
#
# One pattern per line; '#' comments and '-' bullets allowed; quote a pattern
# with spaces (or one starting with '#') in `backticks`. A pattern matches a
# source by its full ref (sources/... or runs/sources/...) OR its path within
# the source root, so `drafts/*.md` matches `sources/drafts/x.md`.
#
# Glob: '*' and '?' stay within one path segment (do NOT cross '/'); '**'
# crosses segments; a trailing '/' means the whole subtree. So:
#   drafts/*.md   -> drafts/x.md      (not drafts/sub/x.md)
#   drafts/**     -> everything under drafts/
#   **/*.md       -> any .md at any depth
#
# Example (remove the leading '# ' to activate):
# - drafts/*.md
# - sources/wip-notes.md
""",
    # Concept-page layout used by `/factlog sync` (tools/merge_candidates.py).
    # Edit this file to change how pages/<entity>.md is generated. Placeholders:
    #   {{ENTITY}} {{SOURCES}} {{RELATIONS}} {{REVIEW}}
    # IMPORTANT: keep byte-identical to merge_candidates.DEFAULT_PAGE_TEMPLATE;
    # tests/test_page_template.sh pins the two together.
    "templates/pages.md": """\
<!-- generated-by-factlog -->
# {{ENTITY}}

## 요약
- `sources/`에서 추출된 candidate fact를 기준으로 정리한 개념입니다.

## 출처
{{SOURCES}}

## 관련 페이지
{{RELATIONS}}

## 확인 필요
{{REVIEW}}
""",
}


def _init_kb(target) -> bool:
    """Scaffold the KB layout under ``target``, printing what it did.

    Returns True iff something was actually created (dirs or files), False if
    the layout already existed and nothing was changed. The printed output and
    semantics are identical to the original ``cmd_init`` body; only the
    created-vs-existing signal is surfaced for callers (e.g. ``cmd_setup``).
    """
    created_dirs: list[str] = []
    dirs = ["sources", "pages", "facts", "decisions", "policy", "policy/prompts", "templates", "runs", "runs/sources"]
    for dirname in dirs:
        d = target / dirname
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created_dirs.append(dirname + "/")

    created_files: list[str] = []
    for rel_path, content in _TEMPLATES.items():
        dest = target / rel_path
        if not dest.exists():
            dest.write_text(content, encoding="utf-8")
            created_files.append(rel_path)

    if created_dirs or created_files:
        print(f"factlog init: created {target}")
        for name in created_dirs:
            print(f"  {name}")
        for name in created_files:
            print(f"  {name}")
        return True

    print(f"factlog init: {target} already exists, nothing to do")
    return False


def active_kb_is_usable(current: str | None) -> bool:
    """Is the configured active KB a directory that still exists?

    A config pointing at a deleted KB must not be defended: keeping it would trap
    the user, since `init` would then refuse to adopt any new KB forever (#210).
    """
    if current is None:
        return False
    from pathlib import Path

    return Path(current).is_dir()


def init_adopts_target(current: str | None, target, activate: bool = False) -> bool:
    """Should `init` make `target` the active KB?

    Yes when nothing usable is configured (the first-run convenience), when the
    target already IS the active KB (re-init is a no-op), or when the user asked
    for it with --activate. Otherwise NO: scaffolding a KB must not silently
    retarget accept/reject/amend/sync at it (#210).

    Pure so it can be pinned without running the CLI.
    """
    if activate:
        return True
    if not active_kb_is_usable(current):
        return True
    return current == str(target)


def setup_active_kb_action(previous: str | None, target) -> str:
    """The summary line `setup` prints for its active-KB adoption.

    `setup` IS the "make this my KB" onboarding command, so unlike `init` it does
    adopt its target. But replacing a *different* active KB has to be stated, not
    slipped into a success summary (#210).
    """
    if active_kb_is_usable(previous) and previous != str(target):
        return f"CHANGED active KB: {previous} -> {target} (was pointing elsewhere)"
    return f"set active KB to {target} (ingest/ask/sync default here from any directory)"


def _init_target(cli_value: str | None) -> _Path:
    """The KB an `init`/`setup` scaffolds: --target, else $FACTLOG_ROOT, else ~/wiki.

    Unlike the other commands, config (the active KB) is NOT a fallback: `init` with no
    args must not silently re-scaffold the KB you are working in. But a session that set
    $FACTLOG_ROOT pointed at a location on purpose, and ignoring it created an unwanted
    ~/wiki while the user believed they were initializing $FACTLOG_ROOT -- a silent
    mismatch every later command then inherited (#247).
    """
    import os
    from pathlib import Path

    if cli_value:
        return Path(cli_value).expanduser().resolve()
    env = os.environ.get("FACTLOG_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path("~/wiki").expanduser().resolve()


def cmd_init(args: argparse.Namespace) -> int:
    target = _init_target(args.target)
    _init_kb(target)

    current = factlog_config.read_root()
    if init_adopts_target(current, target, getattr(args, "activate", False)):
        factlog_config.write_root(target)
        # --activate is an opt-in, not a licence to be silent: replacing a KB the
        # user was working in has to name what it displaced, exactly as setup does.
        # Otherwise `init --target X --activate` (which the README suggests) moves
        # the active KB without a word — the very thing #210 is about.
        action = setup_active_kb_action(current, target)
        print(f"factlog init: {action}")
        if action.startswith("CHANGED"):
            print(f"factlog init: warning — {action}", file=sys.stderr)
        return 0

    # Say it on stderr too: a script that only checks the exit code would
    # otherwise carry on ingesting into the OLD KB believing init "worked".
    print(f"factlog init: active KB left unchanged at {current}")
    print(f"  {target} was created but is NOT active.")
    print(f"  To switch: factlog use {target}   (or re-run init with --activate)")
    print(
        f"factlog init: warning — {target} was created but the active KB is still {current}",
        file=sys.stderr,
    )
    return 0


_LANG_MAX_LEN = 32


def _normalize_lang(code: str) -> tuple[str | None, str | None]:
    """Validate a narration-language value the same way for every entry point.

    Shared by `factlog lang`, `factlog use --lang`, and `factlog setup --lang` so
    a single contract governs what is accepted, rather than each command re-deciding
    (the asymmetry #269 review flagged). Returns ``(normalized, error)``:

    * ``normalized`` is the trimmed code, or ``""`` to mean *clear* — an empty or
      whitespace-only value removes the setting and reverts to conversation-language
      auto-detection (a legitimate "unset" action, not an error).
    * ``error`` is a message string when the value is invalid (too long, or it
      contains control characters); when set, ``normalized`` is ``None`` and the
      caller rejects with exit code 2.
    """
    normalized = code.strip()
    # Reject interior control characters (newline/tab/CR/etc.). `.strip()` only
    # trims leading/trailing whitespace, so an interior newline survives — and
    # `factlog lang` (no arg) is a one-line porcelain contract SKILL.md parses, plus
    # the value is fed back as a narration-language instruction, so a multi-line
    # value both breaks the contract and is a self-config prose-injection vector
    # (#274). A whitespace-only value already collapsed to "" (clear) above, so this
    # never blocks the legitimate unset action.
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in normalized):
        return None, (
            "language code must not contain control characters (e.g. newlines or "
            "tabs); give a short code such as 'ko' or 'en'."
        )
    if len(normalized) > _LANG_MAX_LEN:
        return None, (
            f"language code too long (max {_LANG_MAX_LEN} chars); give a short code "
            "such as 'ko' or 'en', or an empty value to clear it."
        )
    return normalized, None


def _apply_lang(normalized: str) -> str:
    """Persist an already-validated *normalized* language and return the one-line
    confirmation phrase. An empty string clears the setting. Centralised so all
    three entry points word the set/clear outcome identically."""
    factlog_config.write_lang(normalized or None)
    if normalized:
        return f"narration language set to {normalized}"
    return "narration language cleared"


def cmd_use(args: argparse.Namespace) -> int:
    from pathlib import Path

    target = Path(args.target).expanduser().resolve()
    if not target.is_dir():
        print(f"factlog use: {target} does not exist. Run 'factlog init --target {args.target}' first.", file=sys.stderr)
        return 1
    # Validate --lang BEFORE writing the root, so an invalid value never leaves a
    # half-applied config (root changed, lang rejected). Same contract/rc as
    # `factlog lang`.
    lang = getattr(args, "lang", None)
    normalized: str | None = None
    if lang is not None:
        normalized, error = _normalize_lang(lang)
        if error is not None:
            print(f"factlog use: {error}", file=sys.stderr)
            return 2
    factlog_config.write_root(target)
    # --lang, when given, is set (or cleared) alongside the root in the same config
    # file; when omitted the existing language is preserved by write_root, so `use`
    # never silently drops a configured narration language.
    phrase: str | None = None
    if normalized is not None:
        phrase = _apply_lang(normalized)
    note = "" if (target / "sources").is_dir() else "  (warning: no sources/ — not a factlog KB yet; run 'factlog init')"
    print(f"factlog use: active KB set to {target}{note}")
    if phrase is not None:
        print(f"  {phrase}")
    print(f"  config: {factlog_config.config_path()}")
    return 0


def cmd_lang(args: argparse.Namespace) -> int:
    """Get or set the assistant's human-facing narration language.

    No argument: print the configured language on a single line (empty line when
    unset). This is a porcelain contract — the skill parses exactly this shape to
    decide the narration language — so it never carries a label, matching
    `factlog where --porcelain`. It affects ONLY the assistant's prose (narration,
    summaries, 'needs review' framing); engine reports, CLI stdout, and fact data
    stay verbatim in their source language.

    With a CODE: store it (validated via `_normalize_lang`, the shared contract) in
    the active-KB config, leaving the root untouched, then confirm. An empty/blank
    CODE clears the setting (reverts to conversation-language auto-detection).
    """
    code = getattr(args, "code", None)
    if code is None:
        # Query mode: one line, no label (empty line when unset).
        print(factlog_config.read_lang() or "")
        return 0
    normalized, error = _normalize_lang(code)
    if error is not None:
        print(f"factlog lang: {error}", file=sys.stderr)
        return 2
    phrase = _apply_lang(normalized)
    print(f"factlog lang: {phrase}")
    print(f"  config: {factlog_config.config_path()}")
    return 0


def cmd_where(args: argparse.Namespace) -> int:
    root, source = factlog_config.resolve_root()
    # --porcelain: emit ONLY the active KB root (absolute path), one line, no
    # label. This is the machine-parseable contract for `export FACTLOG_ROOT=...`
    # in SKILL.md / hooks — pin exactly this shape so LLMs never parse the prose
    # form. It stays root-only on purpose (never mix in lang); the narration
    # language has its own porcelain contract in `factlog lang`.
    if getattr(args, "porcelain", False):
        print(root)
        return 0
    label = {"env": "env ($FACTLOG_ROOT)", "config": "config file", "cwd": "current directory"}.get(source, source)
    print(f"active KB: {root}")
    print(f"resolved from: {label} (precedence: --flag > $FACTLOG_ROOT > config > cwd)")
    print(f"config file: {factlog_config.config_path()}")
    lang = factlog_config.read_lang()
    if lang:
        print(f"narration language: {lang} (assistant prose only; set with `factlog lang`)")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    """List registered sources: original file, its conversion, and fact count."""
    import csv
    import unicodedata
    from pathlib import Path

    from factlog.common import (
        conversion_body_is_empty,
        is_hidden_source,
        is_sync_ignored,
        paired_conversion,
        source_rel_key,
        sync_ignore_patterns,
    )

    def nfc(s: str) -> str:
        return unicodedata.normalize("NFC", s)

    target_str, _ = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if not _require_kb(target, "sources"):
        return 1

    # fact count per cited source (NFC-normalised, anchor stripped)
    counts: dict[str, int] = {}
    csv_path = target / "facts" / "candidates.csv"
    if csv_path.is_file():
        with csv_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ref = nfc((row.get("source") or "").partition("#")[0])
                if ref:
                    counts[ref] = counts.get(ref, 0) + 1

    # conversions in runs/sources/, keyed by their subdir-aware rel key so a
    # nested original pairs with runs/sources/<same-subdir>/<stem> (ingest mirrors
    # the original's subtree), not just any same-stem file.
    conv: dict[str, str] = {}
    runs_dir = target / "runs" / "sources"
    if runs_dir.is_dir():
        for p in sorted(runs_dir.rglob("*")):
            # hidden = any dot-prefixed component under the root (#67), matching
            # source_files()/coverage so counts agree — not just p.name.
            if p.is_file() and not is_hidden_source(p, runs_dir):
                ref = nfc(p.relative_to(target).as_posix())
                conv.setdefault(source_rel_key(ref), ref)

    entries: list[tuple[int, str, str]] = []  # (facts, original-ref, conversion-ref or "")
    listed: set[str] = set()
    sources_dir = target / "sources"
    for p in sorted(sources_dir.rglob("*")):
        if not p.is_file() or is_hidden_source(p, sources_dir):
            continue
        orig_ref = nfc(p.relative_to(target).as_posix())
        # Match on the full-name key (#213), with a provenance-verified legacy
        # stem-key fallback so a pre-#213 conversion still pairs — but never
        # mispairs a same-stem/different-extension sibling (see paired_conversion).
        conv_ref = paired_conversion(orig_ref, conv, lambda ref: target / ref) or ""
        fact_ref = conv_ref or orig_ref  # facts attach to the conversion when present
        entries.append((counts.get(fact_ref, 0), orig_ref, conv_ref))
        listed.add(orig_ref)
        if conv_ref:
            listed.add(conv_ref)
    # conversions / text files under runs/sources/ with no original in sources/
    for ref in sorted(set(conv.values())):
        if ref not in listed:
            entries.append((counts.get(ref, 0), ref, ""))

    patterns = sync_ignore_patterns(target)
    total = sum(n for n, _, _ in entries)
    n_ignored = sum(
        1 for _, orig, conv_ref in entries
        if is_sync_ignored(orig, patterns) or (conv_ref and is_sync_ignored(conv_ref, patterns))
    )
    suffix = f", {n_ignored} sync-ignored" if n_ignored else ""
    print(f"factlog sources (active KB: {target}): {len(entries)} source(s), {total} fact(s){suffix}")
    for facts, orig, conv_ref in sorted(entries, key=lambda e: (-e[0], e[1])):
        ext = Path(orig).suffix.lstrip(".") or "?"
        arrow = f"  →  {conv_ref}" if conv_ref else ""
        ignored = is_sync_ignored(orig, patterns) or (conv_ref and is_sync_ignored(conv_ref, patterns))
        flags = ""
        if ignored:
            flags += "   [ignored — excluded from sync]"
        elif not facts and conv_ref and conversion_body_is_empty(target / conv_ref):
            # #229: a conversion that ran but has only a provenance header (a
            # scanned/image PDF) is a silent 0-facts source. Distinguish it from
            # a normal source that simply has not been synced yet.
            flags += "   [converted-but-empty — likely scanned PDF; needs OCR]"
        elif not facts:
            flags += "   [no facts — run /factlog sync or factlog ingest]"
        print(f"  [{facts:>3}] {orig}  ({ext}){arrow}{flags}")
    return 0


def _triple_filter(terms: list[str]) -> dict[str, str] | None:
    """Map a (subject, relation, object) positional prefix to a field filter.

    A literal '-' wildcards that position; omitted trailing positions are
    wildcards too. NFC-normalised. Returns None when no non-wildcard term is
    given (the caller treats that as a usage error). Callers reject >3 terms
    separately. Shared by provenance / review / accept / reject.
    """
    import unicodedata

    fields = ("subject", "relation", "object")
    filt = {fields[i]: unicodedata.normalize("NFC", t) for i, t in enumerate(terms) if t != "-"}
    return filt or None


def cmd_review(args: argparse.Namespace) -> int:
    """List facts awaiting a human decision (status candidate/needs_review).

    Grouped by (subject, relation, object) with each backing row's source,
    status, confidence, and note — the queue for `factlog accept` / `reject`.
    --status narrows to one of the two pending statuses.
    """
    import csv
    import unicodedata
    from pathlib import Path

    from factlog.common import REVIEW_STATUSES, normalize_confidence

    def nfc(s: str) -> str:
        return unicodedata.normalize("NFC", s)

    target_str, _ = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if not _require_kb(target, "review"):
        return 1

    want = {args.status} if args.status else set(REVIEW_STATUSES)
    csv_path = target / "facts" / "candidates.csv"
    rows: list[dict[str, str]] = []
    if csv_path.is_file():
        with csv_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    pending = [r for r in rows if (r.get("status") or "").strip() in want]
    if not pending:
        print(f"factlog review (KB: {target}): no pending facts ({'/'.join(sorted(want))})")
        return 0

    def fld(r: dict, k: str) -> str:
        return nfc((r.get(k) or "").strip())

    groups: dict[tuple[str, str, str], list[dict]] = {}
    for r in pending:
        groups.setdefault((fld(r, "subject"), fld(r, "relation"), fld(r, "object")), []).append(r)

    print(f"factlog review (KB: {target}): {len(groups)} pending fact(s), {len(pending)} row(s)")
    for (s, rel, o), grp in groups.items():
        print(f"  {s} / {rel} / {o}")
        for r in sorted(grp, key=lambda r: fld(r, "source")):
            src = fld(r, "source")
            status = (r.get("status") or "").strip()
            conf = normalize_confidence((r.get("confidence") or "").strip())
            note = (r.get("note") or "").strip()
            print(f"    ← {src or '(no source)'}  [{status}, conf {conf}]")
            if note:
                print(f"        note: {note}")
    print("  decide with: factlog accept <subject> <relation> <object>   (or: factlog reject ...)")
    return 0


def _apply_status_to_runs(
    target, filt: dict, from_statuses: set, new_status: str, nfc
) -> int:
    """Write a status decision into runs/*.json, so accept/reject are durable.

    A human decision lived only in candidates.csv, which merge REBUILDS from runs/*.json.
    So deleting candidates.csv and re-merging silently downgraded an accepted fact back
    to candidate -- the decision was never in the source of truth. amend already writes
    to runs; accept/reject did not (#233). This is a status-only change (no value edit),
    so the matching run item is updated in place.

    Returns the number of run rows changed. Import-local like the callers.
    """
    import json

    from factlog.common import KNOWN_STATUSES

    runs_dir = target / "runs"
    if not runs_dir.is_dir():
        return 0
    changed = 0
    for jp in sorted(runs_dir.glob("*.json")):
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # merge fails loudly on a corrupt run file; here, skipping it silently would
            # let accept report success while the decision never reached the file that
            # holds this triple -- the durability the change promises, quietly lost.
            print(
                f"factlog: warning — could not read {jp.name} to record the decision "
                f"({exc}); if it holds this fact, re-run after fixing the file.",
                file=sys.stderr,
            )
            continue
        if not isinstance(data, list):
            continue
        dirty = False
        for item in data:
            if not isinstance(item, dict):
                continue
            fields = {
                "subject": nfc(str(item.get("subject", "")).strip()),
                "relation": nfc(str(item.get("relation", "")).strip()),
                "object": nfc(str(item.get("object", "")).strip()),
            }
            if not all(fields.get(k) == v for k, v in filt.items()):
                continue
            st = str(item.get("status", "")).strip()
            # This runs only AFTER the CSV gate found a genuinely-pending match, so the
            # decision is real. Mirror merge's normalization: a blank or unrecognized
            # status is coerced to needs_review (PENDING) when candidates.csv is rebuilt,
            # so the run item merge will treat as pending must be flipped here too --
            # otherwise the decision vanishes on the next re-merge, the exact silent
            # downgrade this fix is about, in a row the extractor mis-stamped or an edit
            # left blank.
            if st not in from_statuses and st in KNOWN_STATUSES:
                continue
            item["status"] = new_status
            dirty = True
            changed += 1
        if dirty:
            _atomic_write_text(jp, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return changed


def _apply_review_status(args: argparse.Namespace, new_status: str, verb: str) -> int:
    """Shared body of `accept` (-> accepted) and `reject` (-> superseded).

    Changes only rows currently pending (candidate/needs_review) that match the
    triple filter; a confirmed/accepted/superseded row is reported as skipped and
    left untouched (use `factlog eject` to retire a confirmed fact). Atomic CSV
    write; recompiles accepted.dl. --dry-run previews.
    """
    import csv
    import unicodedata
    from pathlib import Path

    from factlog.common import FACT_HEADER, REVIEW_STATUSES

    def nfc(s: str) -> str:
        return unicodedata.normalize("NFC", s)

    target_str, _ = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if not _require_kb(target, verb):
        return 1
    if len(args.terms) > 3:
        print(
            f"factlog {verb}: too many terms — give at most SUBJECT RELATION OBJECT "
            "(quote a value that contains spaces)",
            file=sys.stderr,
        )
        return 2
    filt = _triple_filter(args.terms)
    if filt is None:
        print(
            f"factlog {verb}: give at least one of SUBJECT RELATION OBJECT "
            "(use '-' to wildcard a position)",
            file=sys.stderr,
        )
        return 2

    csv_path = target / "facts" / "candidates.csv"
    rows: list[dict[str, str]] = []
    fieldnames: list[str] = []
    if csv_path.is_file():
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)

    def fld(r: dict, k: str) -> str:
        return nfc((r.get(k) or "").strip())

    matched = [r for r in rows if all(fld(r, k) == v for k, v in filt.items())]
    if not matched:
        shown = ", ".join(f"{k}={v}" for k, v in filt.items())
        print(f"factlog {verb}: no fact matches ({shown})", file=sys.stderr)
        return 1
    pending = [r for r in matched if (r.get("status") or "").strip() in REVIEW_STATUSES]
    skipped = len(matched) - len(pending)
    if not pending:
        print(
            f"factlog {verb}: {len(matched)} matching row(s) are not pending "
            "(already confirmed/accepted/superseded); nothing to change. "
            "Use `factlog eject` to retire a non-pending fact.",
            file=sys.stderr,
        )
        return 1

    note = f" ({skipped} non-pending skipped)" if skipped else ""
    print(f"factlog {verb} (KB: {target}): {len(pending)} pending row(s) → {new_status}{note}")
    for r in pending:
        print(
            f"  {fld(r, 'subject')} / {fld(r, 'relation')} / {fld(r, 'object')}  "
            f"[{(r.get('status') or '').strip()} → {new_status}]  ← {fld(r, 'source') or '(no source)'}"
        )
    if args.dry_run:
        print(f"factlog {verb}: --dry-run, no changes made")
        return 0

    out_fields = fieldnames or list(FACT_HEADER)
    if "status" not in out_fields:
        out_fields = [*out_fields, "status"]
    changed = 0
    for r in rows:
        if all(fld(r, k) == v for k, v in filt.items()) and (r.get("status") or "").strip() in REVIEW_STATUSES:
            r["status"] = new_status
            changed += 1
    _atomic_write_csv(csv_path, rows, out_fields)

    # Durability: write the decision into runs/*.json too, the source of truth merge
    # rebuilds candidates.csv from. Without this, deleting candidates.csv and re-merging
    # silently downgraded the decision (#233).
    runs_changed = _apply_status_to_runs(target, filt, set(REVIEW_STATUSES), new_status, nfc)

    recompile_failed = not _recompile_accepted(target, verb)
    recompiled = "accepted.dl NOT recompiled" if recompile_failed else "accepted.dl recompiled"
    print(
        f"factlog {verb}: {changed} candidate row(s) → {new_status}, "
        f"{runs_changed} runs/*.json row(s) updated; {recompiled}"
    )
    if recompile_failed:
        print(
            f"factlog {verb}: the status change WAS saved to candidates.csv; "
            "re-run `/factlog check` (or compile_facts.py) to refresh accepted.dl.",
            file=sys.stderr,
        )
    print("factlog review: note — pages/ may be stale; run /factlog sync to regenerate them.")
    return 1 if recompile_failed else 0


def cmd_accept(args: argparse.Namespace) -> int:
    """Promote matching pending fact(s) to engine input (status → accepted)."""
    return _apply_review_status(args, "accepted", "accept")


def cmd_reject(args: argparse.Namespace) -> int:
    """Retire matching pending fact(s) (status → superseded, kept for audit)."""
    return _apply_review_status(args, "superseded", "reject")


def cmd_amend(args: argparse.Namespace) -> int:
    """Correct a fact's subject / relation / object / note (durable).

    The positional triple identifies the fact (exact NFC match, any status); the
    --set-* flags give the new values (at least one required, or --accept). A
    fact's values live in runs/*.json (merge rebuilds candidates.csv from it), so
    amend updates BOTH the matching candidates.csv rows AND their backing
    runs/*.json rows — otherwise the edit would vanish on the next sync.
    --accept also promotes to accepted (durable via the merge engine-preservation
    pass). confidence is intentionally not editable. --dry-run previews.
    """
    import csv
    import json
    import unicodedata
    from pathlib import Path

    from factlog.common import FACT_HEADER

    def nfc(s: str) -> str:
        return unicodedata.normalize("NFC", s)

    target_str, _ = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if not _require_kb(target, "amend"):
        return 1

    old = (nfc(args.subject), nfc(args.relation), nfc(args.object))
    sets: dict[str, str] = {}
    for field, val in (
        ("subject", args.set_subject),
        ("relation", args.set_relation),
        ("object", args.set_object),
        ("note", args.set_note),
    ):
        if val is None:
            continue
        v = nfc(val)
        if field in ("subject", "relation", "object") and not v.strip():
            print(f"factlog amend: --set-{field} must not be empty", file=sys.stderr)
            return 2
        sets[field] = v
    if not sets and not args.accept:
        print("factlog amend: give at least one --set-subject/--set-relation/--set-object/--set-note (or --accept)", file=sys.stderr)
        return 2

    csv_path = target / "facts" / "candidates.csv"
    rows: list[dict[str, str]] = []
    fieldnames: list[str] = []
    if csv_path.is_file():
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)

    def fld(r: dict, k: str) -> str:
        return nfc((r.get(k) or "").strip())

    SUPERSEDED = "superseded"

    def is_old(d: dict) -> bool:
        return (fld(d, "subject"), fld(d, "relation"), fld(d, "object")) == old

    def is_live_old(d: dict) -> bool:
        # Only live (non-superseded) rows are amendable. A prior amend leaves the
        # old triple as a `superseded` tombstone; re-targeting it would revive the
        # retired value and duplicate the accepted row on a repeated amend (#220
        # defect 2), so tombstones are never touched.
        return is_old(d) and (d.get("status") or "").strip() != SUPERSEDED

    matched = [r for r in rows if is_live_old(r)]
    if not matched:
        print(f"factlog amend: no fact matches ({old[0]} / {old[1]} / {old[2]})", file=sys.stderr)
        return 1

    print(f"factlog amend (KB: {target}): {len(matched)} row(s) for {old[0]} / {old[1]} / {old[2]}")
    for field in ("subject", "relation", "object", "note"):
        if field in sets:
            print(f"  set {field}: → {sets[field] or '(empty)'}")
    if args.accept:
        print("  status → accepted")
    for r in matched:
        print(f"    ← {fld(r, 'source') or '(no source)'}  [{(r.get('status') or '').strip()}]")
    if args.dry_run:
        print("factlog amend: --dry-run, no changes made")
        return 0

    # 1. candidates.csv (immediate) — atomic write, status-column guard
    out_fields = fieldnames or list(FACT_HEADER)
    if args.accept and "status" not in out_fields:
        out_fields = [*out_fields, "status"]

    # When the triple (subject/relation/object) actually changes, the ORIGINAL
    # source text still carries the old value, so the next sync re-extracts it.
    # Leave a `superseded` tombstone for the old triple (per source) so merge's
    # existing_superseded_keys pass retires the re-asserted old value instead of
    # letting it come back as a live candidate (#220). A note-only / --accept-only
    # edit leaves the triple intact, so no tombstone is needed.
    new_triple = (
        sets.get("subject", old[0]),
        sets.get("relation", old[1]),
        sets.get("object", old[2]),
    )
    triple_changed = new_triple != old

    # Tombstones that already exist (old triple, per source) — snapshot BEFORE the
    # rewrite so a repeated amend doesn't append a duplicate (#220 defect 2).
    existing_tombs = {
        (fld(r, "subject"), fld(r, "relation"), fld(r, "object"), fld(r, "source"))
        for r in rows
        if (r.get("status") or "").strip() == SUPERSEDED
    }

    changed = 0
    tombstones: list[dict[str, str]] = []
    seen_tomb_src: set[str] = set()
    for r in rows:
        if not is_live_old(r):
            continue
        if triple_changed:
            # Snapshot the old triple (before rewrite) as a superseded row, once
            # per source, skipping sources already retired.
            src = fld(r, "source")
            key = (old[0], old[1], old[2], src)
            if src not in seen_tomb_src and key not in existing_tombs:
                seen_tomb_src.add(src)
                tomb = dict(r)
                tomb["subject"], tomb["relation"], tomb["object"] = old
                tomb["status"] = SUPERSEDED
                tombstones.append(tomb)
        for k, v in sets.items():
            r[k] = v
        if args.accept:
            r["status"] = "accepted"
        changed += 1
    rows.extend(tombstones)

    _atomic_write_csv(csv_path, rows, out_fields)

    # 2. runs/*.json (durability) — a value lives here; merge rebuilds from it.
    # For a triple change, do NOT rewrite the old run item in place: candidates.csv
    # is rebuilt from runs/*.json every merge, so a candidates-only tombstone is
    # lost the first time a merge doesn't re-extract the old value, and the bug
    # comes back (#220 defect 1). Instead give the tombstone RUN BACKING — leave
    # the old triple as a `superseded` run item (re-asserted, so merge keeps it
    # retired every rebuild) and add the corrected triple as a separate item so
    # the new value keeps its own run backing (engine-preservation keeps it
    # accepted). A note-only / --accept-only edit has no triple change and is
    # applied in place as before.
    runs_changed = 0
    runs_dir = target / "runs"
    if runs_dir.is_dir():
        for jp in sorted(runs_dir.glob("*.json")):
            try:
                data = json.loads(jp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, list):
                continue
            dirty = False
            new_items: list[dict] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                itriple = (
                    nfc(str(item.get("subject", "")).strip()),
                    nfc(str(item.get("relation", "")).strip()),
                    nfc(str(item.get("object", "")).strip()),
                )
                if itriple != old or str(item.get("status", "")).strip() == SUPERSEDED:
                    continue
                if triple_changed:
                    corrected = dict(item)
                    for k, v in sets.items():
                        corrected[k] = v
                    new_items.append(corrected)
                    item["status"] = SUPERSEDED
                else:
                    for k, v in sets.items():
                        item[k] = v
                dirty = True
                runs_changed += 1
            if new_items:
                data.extend(new_items)
            if dirty:
                _atomic_write_text(jp, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    # 3. recompile accepted.dl
    recompile_failed = False
    if csv_path.is_file():
        recompile_failed = not _recompile_accepted(target, "amend")

    recompiled = "accepted.dl NOT recompiled" if recompile_failed else "accepted.dl recompiled"
    print(
        f"factlog amend: {changed} candidate row(s) updated, {runs_changed} runs/*.json row(s) updated; "
        f"{recompiled}"
    )
    if recompile_failed:
        print(
            "factlog amend: the edit WAS saved to candidates.csv/runs; "
            "re-run `/factlog check` (or compile_facts.py) to refresh accepted.dl.",
            file=sys.stderr,
        )
    if changed and not runs_changed:
        print(
            "factlog amend: note — no runs/*.json backing was found; the edit will NOT survive a "
            "re-merge (/factlog sync rebuilds candidates.csv from runs/*.json).",
            file=sys.stderr,
        )
    print("factlog amend: note — pages/ may be stale; run /factlog sync to regenerate them.")
    return 1 if recompile_failed else 0


def cmd_search(args: argparse.Namespace) -> int:
    """Find facts by a case-insensitive substring across subject/relation/object.

    The "I don't know the exact name" discovery tool — complements `vocab`
    (which lists names) and `provenance` (precise field-targeted exact trace).
    Reads candidates.csv across all statuses; groups distinct matching facts with
    their statuses and distinct-source count.
    """
    import csv
    import unicodedata
    from pathlib import Path

    def nfc(s: str) -> str:
        return unicodedata.normalize("NFC", s)

    target_str, _ = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if not _require_kb(target, "search"):
        return 1

    term = nfc(args.term).strip().casefold()
    if not term:
        print("factlog search: give a non-empty search term", file=sys.stderr)
        return 2

    csv_path = target / "facts" / "candidates.csv"
    rows: list[dict[str, str]] = []
    if csv_path.is_file():
        with csv_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

    def fld(r: dict, k: str) -> str:
        return nfc((r.get(k) or "").strip())

    matched = [r for r in rows if any(term in fld(r, k).casefold() for k in ("subject", "relation", "object"))]
    if not matched:
        print(f"factlog search: no fact matches '{args.term}'", file=sys.stderr)
        return 1

    groups: dict[tuple[str, str, str], dict[str, set]] = {}
    for r in matched:
        key = (fld(r, "subject"), fld(r, "relation"), fld(r, "object"))
        g = groups.setdefault(key, {"statuses": set(), "sources": set()})
        g["statuses"].add((r.get("status") or "").strip() or "?")
        src_file = fld(r, "source").partition("#")[0]
        if src_file:
            g["sources"].add(src_file)

    print(f"factlog search (KB: {target}): {len(groups)} fact(s) matching '{args.term}'")
    for (s, rel, o), g in sorted(groups.items()):
        statuses = ", ".join(sorted(g["statuses"]))
        n = len(g["sources"])
        print(f"  {s} / {rel} / {o}   [{statuses}]  ({n} source{'' if n == 1 else 's'})")
    print("  full detail: factlog provenance <subject> <relation> <object>")
    return 0


def cmd_provenance(args: argparse.Namespace) -> int:
    """Trace a fact to its source(s).

    For a matching (subject, relation, object), list every candidate row that
    backs it: the source path, status, confidence, the note (the extracted
    excerpt/rationale), and a [stale] marker when the source file is missing on
    disk. Positional terms are a (subject, relation, object) prefix; a literal
    '-' wildcards that position and omitted trailing positions are wildcards too
    (at least one non-wildcard term is required). All statuses are shown —
    including superseded/needs_review — so retired backing stays visible.

    Alias expansion (requires policy/relation-aliases.md): when the RELATION
    term is a declared canonical, rows stored under surface variant predicates
    are also included and labelled with ``surface: <raw>``.  When the RELATION
    term is itself a surface predicate, a ``canonical: <name>`` context line is
    shown.  Absent alias file → byte-identical behaviour to today.
    """
    import csv
    import unicodedata
    from pathlib import Path

    from factlog.common import (
        normalize_confidence,
        relation_aliases,
        source_file_refs,
        surface_variants,
    )

    def nfc(s: str) -> str:
        return unicodedata.normalize("NFC", s)

    target_str, _ = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if not _require_kb(target, "provenance"):
        return 1

    if len(args.terms) > 3:
        print(
            "factlog provenance: too many terms — give at most SUBJECT RELATION OBJECT "
            "(quote a value that contains spaces)",
            file=sys.stderr,
        )
        return 2

    filt = _triple_filter(args.terms)
    if filt is None:
        print(
            "factlog provenance: give at least one of SUBJECT RELATION OBJECT "
            "(use '-' to wildcard a position)",
            file=sys.stderr,
        )
        return 2

    csv_path = target / "facts" / "candidates.csv"
    rows: list[dict[str, str]] = []
    if csv_path.is_file():
        with csv_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

    def field(r: dict, k: str) -> str:
        return nfc((r.get(k) or "").strip())

    # --- alias expansion (no-op when relation-aliases.md is absent) ----------
    aliases = relation_aliases(target)
    relation_term = filt.get("relation")  # None when relation position is wildcarded
    variants: set[str] = set()
    canonical_for_term: str | None = None

    if relation_term is not None and aliases:
        # Is the queried relation a declared canonical?  Expand to surface variants.
        variants = surface_variants(relation_term, aliases)
        # Is the queried relation itself a surface predicate?  Surface its canonical.
        canonical_for_term = aliases.get(relation_term)

    # Build extended filter: rows matching the base filter OR rows where the
    # relation is one of the surface variants (all other fields still match).
    if variants:
        base_filt = {k: v for k, v in filt.items() if k != "relation"}

        def _matches_extended(r: dict) -> bool:
            rel = field(r, "relation")
            if rel == relation_term:
                return all(field(r, k) == v for k, v in base_filt.items())
            if rel in variants:
                return all(field(r, k) == v for k, v in base_filt.items())
            return False

        matched = [r for r in rows if _matches_extended(r)]
    else:
        matched = [r for r in rows if all(field(r, k) == v for k, v in filt.items())]

    if not matched:
        shown = ", ".join(f"{k}={v}" for k, v in filt.items())
        print(f"factlog provenance: no fact matches ({shown})", file=sys.stderr)
        return 1

    on_disk = source_file_refs(target)  # NFC-normalised refs of files that exist

    # When a canonical was queried, bucket rows by the raw relation they were
    # stored under so each surface variant gets its own labelled group.
    # When no alias expansion applies, bucket_key is always relation_term (or
    # the actual relation value for wildcard queries) — identical to today.
    if variants:
        # Group by (subject, raw_relation, object) so surface variants are separate.
        groups: dict[tuple[str, str, str], list[dict[str, str]]] = {}
        for r in matched:
            groups.setdefault(
                (field(r, "subject"), field(r, "relation"), field(r, "object")), []
            ).append(r)
    else:
        groups = {}
        for r in matched:
            groups.setdefault(
                (field(r, "subject"), field(r, "relation"), field(r, "object")), []
            ).append(r)

    distinct_sources: set[str] = set()
    stale_rows = 0

    print(f"factlog provenance (KB: {target}): {len(groups)} fact(s), {len(matched)} source row(s)")
    # Print canonical context line when the user queried a surface predicate.
    if canonical_for_term:
        print(f"  canonical: {canonical_for_term}")
    for (s, rel, o), grp in groups.items():
        # Label surface-variant groups so the original raw predicate is explicit.
        if variants and rel != relation_term:
            print(f"  {s} / {rel} / {o}  [surface: {rel}]")
        else:
            print(f"  {s} / {rel} / {o}")
        for r in sorted(grp, key=lambda r: field(r, "source")):
            src = field(r, "source")
            src_file = src.partition("#")[0]
            stale = bool(src_file) and src_file not in on_disk
            stale_rows += 1 if stale else 0
            if src_file:
                distinct_sources.add(src_file)
            status = (r.get("status") or "").strip()
            conf = normalize_confidence((r.get("confidence") or "").strip())  # match ask's .2f format
            note = (r.get("note") or "").strip()
            staletag = "  [stale: source missing]" if stale else ""
            print(f"    ← {src or '(no source)'}  [{status}, conf {conf}]{staletag}")
            if note:
                print(f"        note: {note}")
    print(f"  {len(distinct_sources)} distinct source(s); {stale_rows} stale row(s)")
    return 0


def cmd_ignore(args: argparse.Namespace) -> int:
    """Manage policy/sync-ignore.md — glob patterns of sources excluded from sync.

    No patterns: list current entries and the on-disk sources each matches.
    With pattern(s): add them, or remove them with --remove. Excluding a source
    only stops its re-extraction (ingest --scan / sync / coverage); its already-
    merged facts are untouched (use `factlog eject` to remove those).
    """
    import re
    import unicodedata
    from pathlib import Path

    from factlog.common import is_sync_ignored, source_files, sync_ignore_patterns

    def nfc(s: str) -> str:
        return unicodedata.normalize("NFC", s)

    target_str, _ = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if not _require_kb(target, "ignore"):
        return 1

    policy_file = target / "policy" / "sync-ignore.md"
    current = sync_ignore_patterns(target)
    requested = [nfc(p.strip()) for p in (args.patterns or []) if p.strip()]

    if args.remove and not requested:
        print("factlog ignore --remove: give at least one pattern to remove", file=sys.stderr)
        return 2

    if not requested:  # list mode
        if not current:
            print(f"factlog ignore (KB: {target}): no sync-ignore patterns")
            print(f"  add one with: factlog ignore <glob>   (file: {policy_file})")
            return 0
        refs = sorted(nfc(p.relative_to(target).as_posix()) for p in source_files(target))
        print(f"factlog ignore (KB: {target}): {len(current)} pattern(s):")
        for pat in current:
            hits = [r for r in refs if is_sync_ignored(r, [pat])]
            shown = (": " + ", ".join(hits[:5]) + (" ..." if len(hits) > 5 else "")) if hits else ""
            print(f"  - {pat}   ({len(hits)} match{'' if len(hits) == 1 else 'es'}){shown}")
        return 0

    policy_file.parent.mkdir(parents=True, exist_ok=True)

    if args.remove:
        if not policy_file.is_file():
            print("factlog ignore: removed 0 pattern(s)")
            for p in requested:
                print(f"  (not present: {p})", file=sys.stderr)
            return 0
        existing_text = policy_file.read_text(encoding="utf-8")
        removable = set(requested)
        kept_lines: list[str] = []
        removed = 0
        for line in existing_text.splitlines():
            stripped = re.sub(r"^\s*-\s+", "", line.strip()).strip()
            pat = None
            if stripped and not stripped.startswith("#"):
                m = re.fullmatch(r"`([^`]+)`", stripped)
                pat = nfc((m.group(1) if m else stripped).strip())
            if pat is not None and pat in removable:
                removed += 1
                continue
            kept_lines.append(line)
        policy_file.write_text("\n".join(kept_lines).rstrip("\n") + "\n", encoding="utf-8")
        print(f"factlog ignore: removed {removed} pattern(s)")
        for p in (p for p in requested if p not in set(current)):
            print(f"  (not present: {p})", file=sys.stderr)
        return 0

    # add mode
    to_add = [p for p in requested if p not in set(current)]
    if not to_add:
        print("factlog ignore: all given pattern(s) already present")
        return 0
    needs_header = not policy_file.is_file() or not policy_file.read_text(encoding="utf-8").strip()
    with policy_file.open("a", encoding="utf-8") as f:
        if needs_header:
            f.write("# Sync-ignore list — sources skipped by /factlog sync (manage with `factlog ignore`)\n")
        for p in to_add:
            f.write(f"- `{p}`\n" if " " in p else f"- {p}\n")
    print(f"factlog ignore: added {len(to_add)} pattern(s): {', '.join(to_add)}")
    return 0


def cmd_vocab(args: argparse.Namespace) -> int:
    """List the KB vocabulary: entity and relation names with usage counts.

    Names come from the *engine* facts (what `ask`/`provenance` can query); pass
    --all to include candidate-only names. Objects of declared attribute
    relations are literals, not entities, so they are excluded from the entity
    list (consistent with `status`). --entities / --relations show one section;
    default shows both. Relations are tagged [attribute]/[single-valued]/[typed:<type>].
    """
    import unicodedata
    from collections import Counter
    from pathlib import Path

    import factlog.common as common

    target_str, _ = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if not _require_kb(target, "vocab"):
        return 1
    # A KbContext bound to the requested KB — no need to mutate FACTLOG_ROOT and
    # importlib.reload(common) just to read a non-default root in-process.
    ctx = common.KbContext.for_root(target_str)

    facts = ctx.load_facts() if ctx.candidates_csv.is_file() else []
    scope = facts if args.all else common.engine_facts(facts)
    scope_label = "all candidate" if args.all else "engine"
    attr = ctx.attribute_relations()
    attr_forms = common.attribute_relation_forms(attr, ctx.relation_aliases())
    sv = ctx.single_valued_relations()
    typed = ctx.typed_relations()  # {name: TypedRelSpec}; {} when no typed-relations.md

    show_e = args.entities or not args.relations
    show_r = args.relations or not args.entities

    ent_counts: Counter = Counter()
    rel_counts: Counter = Counter()
    for row in scope:
        s, rel, o = row["subject"], row["relation"], row["object"]
        if rel:
            rel_counts[rel] += 1
        if s:
            ent_counts[s] += 1
        # Surface forms, not raw declarations: a KB that declares the canonical while its
        # facts carry an alias had vocab call the literal an entity while status and the
        # engine called it a literal (#226).
        if o and not common.is_attribute_relation(rel, attr_forms):
            ent_counts[o] += 1

    print(f"factlog vocab (KB: {target}) — {scope_label} facts")
    if show_e:
        print(f"  entities ({len(ent_counts)}):")
        for name, n in sorted(ent_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"    [{n:>3}] {name}")
        if not ent_counts:
            print("    (none)")
    if show_r:
        print(f"  relations ({len(rel_counts)}):")
        for name, n in sorted(rel_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            # The same shared predicate the entity count above uses: comparing the raw
            # declaration left vocab tagging no relation as [attribute] on an alias KB
            # while status counted one (#226).
            tags = [
                t
                for t, on in (
                    ("attribute", common.is_attribute_relation(name, attr_forms)),
                    # sv is loaded NFC-normalized; the CSV-sourced name may be NFD.
                    # Fold to match, homomorphic with is_attribute_relation (#293).
                    ("single-valued", unicodedata.normalize("NFC", name) in sv),
                )
                if on
            ]
            # typed_relations() keys are NFC-normalized; the CSV-sourced name may be NFD.
            tname = unicodedata.normalize("NFC", name)
            if tname in typed:
                tags.append(f"typed:{typed[tname].type}")
            tagstr = f"  [{', '.join(tags)}]" if tags else ""
            print(f"    [{n:>3}] {name}{tagstr}")
        if not rel_counts:
            print("    (none)")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Summarise the active KB's state: sources, facts by status, vocabulary,
    conflicts, logic-report freshness, and engine availability."""
    import unicodedata
    from collections import Counter
    from pathlib import Path

    import factlog.common as common

    target_str, source = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if not _require_kb(target, "status", suffix="Run 'factlog init'/'use'."):
        return 1
    # KbContext bound to the requested KB — no FACTLOG_ROOT mutation / reload(common).
    ctx = common.KbContext.for_root(target_str)

    src_label = {"flag": "--target", "env": "$FACTLOG_ROOT", "config": "config", "cwd": "cwd"}.get(source, source)
    print(f"factlog status — active KB: {target}  (from {src_label})")

    # Engine
    try:
        import pyrewire  # type: ignore

        ver = str(getattr(pyrewire, "__version__", "?"))
        engine = f"pyrewire {ver}" + ("" if _version_tuple(ver) >= MIN_PYREWIRE else f" (< {'.'.join(map(str, MIN_PYREWIRE))} — run setup)")
    except ImportError:
        engine = "pyrewire NOT installed (run /factlog setup; checks degrade gracefully)"
    print(f"  engine:     {engine}")

    # Facts
    facts = ctx.load_facts() if ctx.candidates_csv.is_file() else []
    by_status = Counter(r["status"] for r in facts)
    engine_rows = common.engine_facts(facts)
    # Display the DEDUPED engine-atom count, the same basis the report's `engine facts:`
    # (accepted.dl is deduped) and the freshness mismatch check below both use. Showing
    # raw len(engine_rows) let a duplicate-triple KB print "7 engine fact(s)" next to a
    # report reading "engine facts: 6" while the dedup-aware mismatch stayed silent
    # (6==6) — the two on-screen numbers disagreed with no explanation (#355/#330 AC2).
    n_engine = len(common.dedup_engine_atoms(engine_rows))
    if facts:
        order = ["confirmed", "accepted", "needs_review", "candidate", "superseded"]
        seen = [f"{s}={by_status[s]}" for s in order if by_status.get(s)]
        extra = [f"{s}={n}" for s, n in by_status.items() if s not in order]
        print(f"  facts:      {len(facts)} candidate(s) [{', '.join(seen + extra)}]; {n_engine} engine fact(s)")
    else:
        print("  facts:      none (no facts/candidates.csv — run /factlog sync)")

    # Vocabulary
    attr = ctx.attribute_relations()
    sv = ctx.single_valued_relations()
    # Pass attr AND this KB's aliases so entity_set reads THIS KB throughout, not the
    # module default (cmd_status may target a KB other than the ambient FACTLOG_ROOT).
    ent, val = common.entity_set(facts, attr, ctx.relation_aliases()), common.value_set(facts)
    # Literals are values appearing only as attribute-relation objects; with no
    # attribute-relations.md declared, entity_set == value_set so there are none.
    literals = f"{len(val) - len(ent)} literal(s)" if attr else "0 literal(s) — none declared"
    print(
        f"  vocabulary: {len(ent)} entit(y/ies), {literals}, "
        # engine-scoped, like entity_set/value_set above — so the counts agree
        # with `factlog vocab` (which lists the same engine vocabulary).
        f"{len(common.allowed_relations(engine_rows))} relation(s) "
        f"({len(attr)} attribute, {len(sv)} single-valued declared)"
    )

    # Sources (NFC-matched, like coverage): a binary original is "covered via
    # conversion" when its runs/sources/<rel> text conversion carries facts
    # (facts attach to the conversion, not the binary original).
    cited = {unicodedata.normalize('NFC', r['source'].partition('#')[0]) for r in engine_rows if r.get('source')}
    patterns = common.sync_ignore_patterns(target)
    refs: dict = {}
    n_ignored = 0
    for p in common.source_files(target):
        # source_files() already drops hidden paths (any dot-prefixed component
        # under the source root — .DS_Store, .git/, .obsidian/, .provenance/),
        # so every enumerator shares one definition and the counts agree (#67).
        ref = unicodedata.normalize('NFC', p.relative_to(target).as_posix())
        if common.is_sync_ignored(ref, patterns):
            n_ignored += 1  # excluded from sync on purpose — not a gap
            continue
        refs[p] = ref
    # only a *text* conversion under runs/sources/ backs an original (a stray
    # binary there is an anomaly, not a usable conversion — matches coverage).
    # Conversions that are cited AND text back a binary original "via conversion".
    covered_conv_by_key: dict[str, str] = {}
    path_by_ref = {ref: p for p, ref in refs.items()}
    for p, ref in refs.items():
        if ref.startswith("runs/sources/") and ref in cited and common.is_text_source(p):
            covered_conv_by_key.setdefault(common.source_rel_key(ref), ref)
    direct = sum(1 for ref in refs.values() if ref in cited)
    via = sum(
        1
        for p, ref in refs.items()
        if ref not in cited
        and ref.startswith("sources/")
        and not common.is_text_source(p)
        # Match on the full-name key (#213), with a provenance-verified legacy
        # stem-key fallback so a pre-#213 conversion still pairs without
        # mispairing a same-stem sibling (see common.paired_conversion).
        and common.paired_conversion(ref, covered_conv_by_key, lambda r: path_by_ref[r])
        is not None
    )
    covered = direct + via
    total = len(refs)
    # #229: count conversions whose body is blank (scanned/image PDF, etc.). They
    # are "with none" but for a distinct reason — the converter ran and produced
    # no text — so call them out separately from unconverted / not-yet-synced.
    empty_conv = sum(
        1
        for p, ref in refs.items()
        if ref.startswith("runs/sources/") and common.conversion_body_is_empty(p)
    )
    via_note = f" ({via} via conversion)" if via else ""
    excl_note = f", {n_ignored} sync-ignored" if n_ignored else ""
    empty_note = f", {empty_conv} converted-but-empty (likely scanned/needs OCR)" if empty_conv else ""
    print(f"  sources:    {total} file(s), {covered} with facts{via_note}, {total - covered} with none{excl_note}{empty_note}")

    # Conflicts (single-valued relations with >1 distinct object)
    if sv:
        # The gate's own function, not a private counter. The inline one knew nothing of
        # the value hierarchy, relation aliases or typed grouping, so status told the
        # user "1 conflict" about a KB check_conflicts had just cleared -- and told them
        # to fix it by hand-editing candidates.csv (#219).
        conflicts = common.detect_conflicts(
            engine_rows, sv, ctx.typed_relations(), common.relation_aliases(ctx.root), common.value_hierarchy(ctx.root)
        )
        msg = f"  conflicts:  {len(conflicts)} (over {len(sv)} single-valued relation(s))"
        if conflicts:
            msg += "  ⚠ run tools/check_conflicts.py for the resolution steps"
        print(msg)
    else:
        print("  conflicts:  n/a (no single-valued relations declared in policy/single-valued.md)")

    # Logic report freshness
    report = ctx.facts_dir / "logic_report.txt"
    if report.is_file():
        text = report.read_text(encoding="utf-8", errors="ignore")
        # Lower-case `errors:`/`warnings:` are the summary lines in
        # run_logic_check's report (the `Errors:`/`Warnings:` headers are capitalised).
        errors = next((ln.split(":", 1)[1].strip() for ln in text.splitlines() if ln.startswith("errors:")), "?")
        warnings = next((ln.split(":", 1)[1].strip() for ln in text.splitlines() if ln.startswith("warnings:")), "?")
        report_engine = next(
            (ln.split(":", 1)[1].strip() for ln in text.splitlines() if ln.startswith("engine facts:")), None
        )
        rep_mtime = report.stat().st_mtime
        # The report is a function of run_logic_check's inputs AND, transitively, of
        # candidates.csv — accepted.dl is compiled from it. Without candidates.csv here,
        # editing it without recompiling left accepted.dl (and the report) untouched, so
        # status called a report predating the edit "fresh" (#330).
        inputs = [
            p
            for p in (ctx.candidates_csv, ctx.accepted_dl, ctx.facts_dir / "query.dl", ctx.logic_policy_dl)
            if p.is_file()
        ]
        stale = any(p.stat().st_mtime > rep_mtime for p in inputs)
        fresh = "STALE (inputs changed since last check — run /factlog check)" if stale else "fresh"
        line = f"  logic:      report {fresh}; errors={errors}, warnings={warnings}"
        # status prints two engine-fact counts — its own (from candidates.csv, above) and
        # the report's `engine facts:` (from accepted.dl) — and used to never compare them,
        # so a truncated accepted.dl (#328/#329) showed "7 engine fact(s)" one line above a
        # report that checked 3, and still called it fresh. Compare dedup-aware (the report
        # counts deduped accepted.dl rows, so dedup the candidate side too — legitimate
        # duplicate triples must not false-alarm) and say so on mismatch.
        expected_engine = n_engine  # same deduped basis as the `engine fact(s)` line printed above
        if report_engine is not None and report_engine.isdigit() and int(report_engine) != expected_engine:
            line += (
                f"\n              ⚠ engine-input mismatch: {expected_engine} confirmed fact(s) in "
                f"candidates.csv but the report checked {report_engine} — accepted.dl is out of "
                "date; run /factlog check"
            )
        print(line)
    else:
        print("  logic:      no logic_report.txt yet (run /factlog check)")
    return 0


def _find_requirements():
    """Locate requirements.txt.

    Resolution order:
      1. ``$CLAUDE_PLUGIN_ROOT/requirements.txt`` (set when running as a
         Claude Code plugin).
      2. The repo/package root, i.e. the parent of this package directory.

    Returns a ``pathlib.Path`` if found, else ``None``.
    """
    import os
    from pathlib import Path

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        candidate = Path(plugin_root).expanduser() / "requirements.txt"
        if candidate.is_file():
            return candidate

    # factlog/cli.py → factlog/ → repo root
    repo_candidate = Path(__file__).resolve().parent.parent / "requirements.txt"
    if repo_candidate.is_file():
        return repo_candidate

    return None


def _install_requirements(requirements) -> int:
    """Attempt ``sys.executable -m pip install -r <requirements>``.

    PEP 668 handling: if pip refuses because the environment is
    externally-managed, DO NOT pass --break-system-packages. Print actionable
    venv guidance and return a non-zero exit. Never silently mutate a system
    Python.

    Returns 0 on success, non-zero otherwise.
    """
    import subprocess

    print(f"factlog setup: installing requirements from {requirements}")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
        capture_output=True,
        text=True,
    )
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.returncode == 0:
        return 0

    combined = (proc.stdout or "") + (proc.stderr or "")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)

    # PEP 668: externally-managed-environment. pip prints this marker.
    if "externally-managed-environment" in combined or "externally managed" in combined:
        print(
            "\n"
            "factlog setup: this Python is externally managed (PEP 668), so pip\n"
            "refused to install into it. factlog will NOT override this with\n"
            "--break-system-packages. Create and activate a virtual environment,\n"
            "then re-run setup:\n"
            "\n"
            "    python -m venv ~/.factlog-venv\n"
            "    source ~/.factlog-venv/bin/activate\n"
            "    python -m factlog setup --target <kb>\n",
            file=sys.stderr,
        )
    else:
        print(
            "\nfactlog setup: pip install failed (see output above). Resolve the\n"
            "dependency issue, or install pyrewire manually, then re-run setup.\n",
            file=sys.stderr,
        )
    return proc.returncode or 1


def cmd_setup(args: argparse.Namespace) -> int:
    """One-shot bootstrap: doctor → ensure deps → init KB → re-doctor.

    Idempotent and safe to re-run: deps are only installed when pyrewire is
    missing/too old, and `cmd_init` skips files/dirs that already exist.
    """

    actions: list[str] = []

    # Validate --lang up front (same contract/rc as `factlog lang`) so an invalid
    # value fails fast, before any install / KB scaffolding side effects.
    lang = getattr(args, "lang", None)
    lang_normalized: str | None = None
    if lang is not None:
        lang_normalized, error = _normalize_lang(lang)
        if error is not None:
            print(f"factlog setup: {error}", file=sys.stderr)
            return 2

    print("=== factlog setup: initial environment check ===")
    _run_doctor_checks()

    deps_already_ok = _pyrewire_ok()
    install_attempted = False
    if deps_already_ok:
        print("\nfactlog setup: pyrewire already satisfied, skipping install")
    else:
        print("\n=== factlog setup: installing engine dependency ===")
        requirements = _find_requirements()
        if requirements is None:
            print(
                "factlog setup: could not locate requirements.txt. Set "
                "CLAUDE_PLUGIN_ROOT to the plugin directory, or run from the "
                "factlog repo, then re-run setup.",
                file=sys.stderr,
            )
            return 1
        rc = _install_requirements(requirements)
        if rc != 0:
            return rc
        install_attempted = True

    print("\n=== factlog setup: initialise knowledge base ===")
    target = _init_target(args.target)
    kb_created = _init_kb(target)
    previous = factlog_config.read_root()
    factlog_config.write_root(target)
    if kb_created:
        actions.append(f"created KB layout at {target}")
    else:
        actions.append(f"KB already present at {target}")
    action = setup_active_kb_action(previous, target)
    actions.append(action)
    if action.startswith("CHANGED"):
        # Also on stderr: buried in a success summary, a retarget is easy to miss.
        print(f"factlog setup: warning — {action}", file=sys.stderr)
    # Optional narration language: applied only when --lang is given, so an existing
    # language survives a re-run of setup that omits the flag (write_root above
    # already preserves it). Uses the shared validate/apply path, so an empty value
    # clears the setting with the same wording as `factlog lang`.
    if lang_normalized is not None:
        phrase = _apply_lang(lang_normalized)
        actions.append(f"{phrase} (assistant prose only)")

    print("\n=== factlog setup: final environment check ===")
    # gate="setup": a missing git is reported but does not fail setup, whose
    # real work (pip install + KB init) does not use git.
    final_ok = _run_doctor_checks(gate="setup")

    # Only claim the dependency was installed/satisfied when the FINAL doctor
    # confirms it. If pip returned 0 but pyrewire is still unusable (a "lying
    # pip"), word it as an attempt, not a success. The exit code below stays
    # non-zero in that case via final_ok.
    if deps_already_ok:
        actions.insert(0, "engine dependency (pyrewire) already satisfied")
    elif install_attempted and final_ok:
        actions.insert(0, "installed engine dependency (pyrewire)")
    elif install_attempted:
        actions.insert(0, "attempted dependency install (pyrewire) — still not satisfied")

    print("\n=== factlog setup: summary ===")
    if actions:
        for action in actions:
            print(f"  done: {action}")
    else:
        print("  done: nothing to change (already set up)")

    if final_ok:
        print(
            "\nfactlog setup complete. Next: run /factlog sync (and then query, "
            "check, repair) inside your knowledge base."
        )
        return 0

    print(
        "\nfactlog setup: environment still not satisfied (see FAIL lines "
        "above). Resolve the reported issue, then re-run setup.",
        file=sys.stderr,
    )
    return 1


# ---------------------------------------------------------------------------
# `ingest` — convert a binary/office source file into text under sources/
# ---------------------------------------------------------------------------
#
# Fact extraction reads sources/ files as text, so binary formats (docx, pdf,
# ...) must be converted first (see issue #1's non-text warning). `ingest`
# wraps the common system converters and writes the converted text, with a
# provenance header, into <target>/sources/ so /factlog sync can read it.


# The source-file converters (per-extension chains, built-in hwpx/pptx/hwp
# converters, install hints) live in factlog/ingest.py; cmd_ingest drives them
# via the ingest.* public surface.


def _looks_binary(path, sniff: int = 8192) -> bool:
    """Strict boolean inverse of ``common.is_text_source`` for --scan discovery:
    ``_looks_binary(p) == (not is_text_source(p))`` for every file.

    Delegating keeps that invariant true BY CONSTRUCTION. It used to be a parallel
    implementation of the same content sniff, and when the text-container exception
    (#222) was added to one and not the other, --scan and coverage/status quietly
    answered differently about the same file.
    """
    from factlog import common as _common

    return not _common.is_text_source(path, sniff=sniff)

def cmd_ingest(args: argparse.Namespace) -> int:
    """Convert binary/office file(s) into text source(s) under <target>/sources/.

    The original file is left untouched; the converted text (with a provenance
    header recording the source, converter, and date) is written under the KB's
    runs/sources/ directory — alongside the other generated run artifacts, never
    into sources/, which holds the user's originals.

    With --scan, every binary file under sources/ is auto-discovered (the
    deterministic pre-step /factlog sync runs) and converted. Conversion is
    idempotent: an up-to-date conversion is skipped, a stale one (original newer)
    is refreshed.

    Returns non-zero only on a genuine conversion failure; unconvertible formats
    found by --scan are reported but do not fail the run.
    """
    import shutil
    import subprocess
    import unicodedata
    from datetime import datetime, timezone
    from pathlib import Path

    from factlog.common import is_hidden_source, is_sync_ignored, sync_ignore_patterns

    target_str, source = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if source in ("config", "cwd"):
        print(f"factlog ingest: target KB {target} (from {source})")
    hint = (
        "Run 'factlog init --target <kb>' (or 'factlog use <kb>') first."
        if source in ("config", "cwd")
        else f"Run 'factlog init --target {args.target}' first."
    )
    if not _require_kb(target, "ingest", suffix=hint):
        return 1
    # Converted files are *derived* artifacts, so they collect with the other
    # generated run outputs under runs/sources/ — never in sources/, which holds
    # the user's originals. sync reads both sources/ and runs/sources/.
    derived = target / "runs" / "sources"
    derived.mkdir(parents=True, exist_ok=True)
    sources_dir = (target / "sources").resolve()

    # Build the work list: explicit paths, plus (with --scan) every binary file
    # found under sources/. --scan honors the sync-ignore list (an explicitly
    # named path is always converted — the user asked for it directly).
    work: list[Path] = [Path(p).expanduser() for p in args.paths]
    # #215: a --scan discovery that a file was NOT a binary can no longer drop it
    # silently. A file whose extension has a recognized converter but whose
    # content is not binary (a plaintext .hwpx, a 0-byte .pdf) would otherwise
    # vanish from every count while an explicit `ingest <file>` reports it as
    # failed — an inconsistency the operator can't see. Surface both classes.
    scan_nonbinary_refs: list[str] = []  # recognized ext, but non-binary content
    scan_empty_refs: list[str] = []  # 0-byte file with a recognized ext
    if args.scan:
        patterns = sync_ignore_patterns(target)
        ignored = 0
        scan_root = target / "sources"
        for path in sorted(p for p in scan_root.rglob("*") if p.is_file()):
            # hidden = any dot-prefixed component under sources/ (#67), so a file
            # in sources/.git/ or sources/.obsidian/ is skipped like sync is.
            if is_hidden_source(path, scan_root):
                continue
            ref = unicodedata.normalize("NFC", path.relative_to(target).as_posix())
            if not _looks_binary(path):
                # Only a recognized *conversion target* is worth flagging: a plain
                # .txt/.md source is read directly by sync as text and is correctly
                # not a conversion job. (Text containers are no longer "text" —
                # is_text_source owns that call now, see #222.)
                if path.suffix.lower() not in ingest.INGEST_CONVERTERS:
                    continue
                if is_sync_ignored(ref, patterns):
                    ignored += 1
                    continue
                try:
                    empty = path.stat().st_size == 0
                except OSError:
                    empty = False
                (scan_empty_refs if empty else scan_nonbinary_refs).append(ref)
                continue
            if is_sync_ignored(ref, patterns):
                ignored += 1
                continue
            work.append(path)
        if ignored:
            print(f"factlog ingest --scan: skipped {ignored} sync-ignored source(s)")
        if scan_nonbinary_refs:
            print(
                f"factlog ingest --scan: {len(scan_nonbinary_refs)} ignored "
                "(binary extension, non-binary content — not converted; "
                "sync reads it as text if it is a valid source):",
                file=sys.stderr,
            )
            for ref in scan_nonbinary_refs:
                print(f"    - {ref}", file=sys.stderr)
        if scan_empty_refs:
            print(
                f"factlog ingest --scan: {len(scan_empty_refs)} ignored "
                "(empty file, 0 bytes — nothing to convert):",
                file=sys.stderr,
            )
            for ref in scan_empty_refs:
                print(f"    - {ref}", file=sys.stderr)
    if not work:
        if args.scan:
            # Even with nothing to convert, report the ignored counts so the
            # summary arithmetic (converted+skipped+failed+ignored == discovered)
            # holds when every discovered conversion target was set aside (#215):
            # a per-file warning above is not a count line.
            tail = []
            if scan_nonbinary_refs:
                tail.append(f"{len(scan_nonbinary_refs)} ignored (binary extension, non-binary content)")
            if scan_empty_refs:
                tail.append(f"{len(scan_empty_refs)} ignored (empty file)")
            note = (" (" + ", ".join(tail) + ")") if tail else ""
            print(f"factlog ingest --scan: no binary source files to convert{note}")
            return 0
        print("factlog ingest: no input files (give file paths or --scan)", file=sys.stderr)
        return 2

    converted = 0
    empty_converted = 0  # #229: converter ran but the output body is blank
    warned_converted = 0  # #239: converter exited 0 but wrote quality warnings to stderr
    skipped = 0
    failures = 0
    scan_nonbinary = len(scan_nonbinary_refs)  # #215: surfaced in the summary
    scan_empty = len(scan_empty_refs)
    for src in work:
        if not src.is_file():
            print(f"factlog ingest: not a file: {src}", file=sys.stderr)
            failures += 1
            continue

        suffix = src.suffix.lower()
        chain = ingest.INGEST_CONVERTERS.get(suffix)
        if not chain:
            hint = ingest.INGEST_HINTS.get(suffix, "no converter available for this format")
            print(
                f"factlog ingest: skip {src.name} ({suffix or 'no extension'}): {hint}",
                file=sys.stderr,
            )
            # In --scan a stray unconvertible file should not fail sync; an
            # explicitly-named one is a user error and does count as a failure.
            skipped += 1 if args.scan else 0
            failures += 0 if args.scan else 1
            continue

        chosen = next(
            ((t, out, build) for (t, out, build) in chain if t in ingest.BUILTIN_CONVERTERS or shutil.which(t)),
            None,
        )
        if chosen is None:
            tools = ", ".join(t for (t, _, _) in chain)
            hints = "; ".join(ingest.INSTALL_HINTS.get(t, t) for (t, _, _) in chain)
            print(
                f"factlog ingest: no converter on PATH for {suffix} (tried: {tools}). {hints}",
                file=sys.stderr,
            )
            skipped += 1 if args.scan else 0
            failures += 0 if args.scan else 1
            continue

        tool, out_suffix, build = chosen
        # Mirror the original's subdirectory under runs/sources/ so a nested
        # source (sources/sub/x.pdf) converts to runs/sources/sub/x.pdf.md —
        # never a flat name that would collide with a same-name file in another
        # subdir. An explicitly-named path outside sources/ has no subtree to
        # mirror, so it falls back to a flat output name.
        try:
            src_rel = src.resolve().relative_to(sources_dir)
            rel_parent = src_rel.parent
            # #214: record the source's path *relative to sources/* in the
            # provenance header, so same-name originals in different subdirs
            # (sources/sub_a/data.hwpx, sources/sub_b/data.hwpx) get distinct
            # `source:` values (sub_a/data.hwpx vs sub_b/data.hwpx) instead of a
            # colliding basename. A root-direct original stays a bare basename
            # (relative_to(sources) == the filename), so its header is unchanged.
            source_label = src_rel.as_posix()
        except (ValueError, OSError):
            rel_parent = Path()
            # An explicit path outside sources/ has no sources-relative form;
            # fall back to the basename (matches the flat output name below).
            source_label = src.name
        # Keep the original's *full* filename (extension included) and append the
        # out-suffix, so same-stem/different-extension originals (report.hwpx,
        # report.pptx) convert to distinct outputs (report.hwpx.md,
        # report.pptx.md) instead of colliding on one file and silently dropping
        # the loser (#213). source_rel_key() mirrors this to pair each original
        # with exactly its own conversion.
        dst = derived / rel_parent / (src.name + out_suffix)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and not args.force and dst.stat().st_mtime >= src.stat().st_mtime:
            print(f"factlog ingest: {dst.relative_to(target).as_posix()} up to date; skipping {source_label}")
            skipped += 1
            continue

        conv_warnings = ""  # #239: quality warnings an external converter emits on success
        if tool in ingest.BUILTIN_CONVERTERS:
            try:
                ok = bool(build(src, dst))
                detail = "could not extract text (empty, corrupt, or unsupported file)"
            except ingest.MissingTool as exc:
                # required external tool absent: like a missing PATH converter —
                # soft-skip under --scan, count as failure when named explicitly.
                print(f"factlog ingest: skip {src.name} ({suffix}): {exc}", file=sys.stderr)
                skipped += 1 if args.scan else 0
                failures += 0 if args.scan else 1
                continue
            except Exception as exc:  # defensive: a built-in must never crash the run
                ok = False
                detail = str(exc)
            if not ok or not dst.is_file():
                print(f"factlog ingest: {tool} failed on {src.name}: {detail}", file=sys.stderr)
                failures += 1
                continue
        else:
            proc = subprocess.run(build(src, dst), capture_output=True, text=True)
            if proc.returncode != 0 or not dst.is_file():
                detail = (proc.stderr or proc.stdout or "").strip()
                print(f"factlog ingest: {tool} failed on {src.name}: {detail}", file=sys.stderr)
                failures += 1
                continue
            # #239: a converter can exit 0 yet warn on stderr about a quality
            # problem it could not fix (pandoc's "Unsupported code page 949. Text
            # will likely be garbled." on a cp949 RTF). returncode-only success
            # detection swallowed that warning, so mojibake entered extraction as
            # prose silently — the same class of harm #222 killed, wearing a
            # different mask. Keep the stderr so the success path can surface it.
            conv_warnings = proc.stderr.strip()

        when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = dst.read_text(encoding="utf-8", errors="replace")
        if out_suffix == ".md":
            header = f"<!-- ingested-by-factlog | source: {source_label} | converter: {tool} | date: {when} -->\n\n"
        else:
            header = f"[ingested-by-factlog] source: {source_label} | converter: {tool} | date: {when}\n\n"
        dst.write_text(header + body, encoding="utf-8")

        dst_rel = dst.relative_to(target).as_posix()
        # #229: the converter exited 0 and wrote a file, but if its body (before
        # the header we just added) is blank, the input had no extractable text —
        # a scanned/image PDF, an empty doc, etc. Counting it as `converted` hides
        # a silent 0-facts source, so split it out and warn (the merge un-converted
        # warning only sees a *missing* conversion, never an empty one).
        if body.strip() == "":
            empty_converted += 1
            print(
                f"factlog ingest: {source_label} -> {dst_rel} converted-but-empty "
                "(likely scanned/needs OCR)",
                file=sys.stderr,
            )
            # empty is the louder signal, so it wins the bucket — but a converter
            # can be empty *and* have warned (a cp949 doc that produced no text and
            # a code-page warning). Still echo the warning here so #239's fix is not
            # re-swallowed in that narrow overlap, and the "needs OCR" label does not
            # silently mis-attribute an encoding failure.
            for line in conv_warnings.splitlines():
                print(f"    {line}", file=sys.stderr)
        elif conv_warnings:
            # #239: the file has text but the converter flagged it. Split it out
            # of `converted` (as #229 does for empty) and echo every warning line
            # so a garbled-encoding conversion is visible, not silent.
            warned_converted += 1
            print(
                f"factlog ingest: {source_label} -> {dst_rel} converted-with-warnings "
                f"(via {tool}):",
                file=sys.stderr,
            )
            for line in conv_warnings.splitlines():
                print(f"    {line}", file=sys.stderr)
        else:
            converted += 1
            print(f"factlog ingest: {source_label} -> {dst_rel} (via {tool})")

    # warned/empty are split out of `converted` but still discovered conversions,
    # so the #215 balance widens to
    #   discovered == converted + warned + empty + skipped + failed + ignored.
    # (Treating any non-empty converter stderr on a zero exit as a warning is
    # deliberate per #239 — better an over-surfaced benign note than a swallowed
    # garble; the configured converters emit clean stderr on a clean conversion.)
    summary = f"{converted} converted, {skipped} skipped, {failures} failed"
    if warned_converted:
        summary += f", {warned_converted} converted-with-warnings"
    if empty_converted:
        summary += f", {empty_converted} converted-but-empty (likely scanned/needs OCR)"
    if scan_nonbinary:
        summary += f", {scan_nonbinary} ignored (binary extension, non-binary content)"
    if scan_empty:
        summary += f", {scan_empty} ignored (empty file)"
    print(f"factlog ingest: {summary}")
    return 1 if failures else 0


class _EjectSelection(NamedTuple):
    """What an eject mode selected: the predicate that decides which candidate
    rows / runs/*.json items are retired, plus the source-mode-only file actions
    (empty in fact mode, which never touches source files)."""

    match_row: Callable[[dict], bool]
    conv_to_delete: list[str]
    orig_on_disk: list[str]
    strip_runs: bool


def _select_eject_facts(args, rows, fact_specs, target, nfc):
    """Fact mode: select candidate rows matching the given (subject, relation,
    object) triple(s). Returns an _EjectSelection, or an int exit code when there
    is nothing to do. Prints the plan exactly as cmd_eject used to inline."""
    targets = {(nfc(s), nfc(rel), nfc(o)) for s, rel, o in fact_specs}

    def match_row(d: dict) -> bool:
        return (
            nfc(str(d.get("subject", ""))),
            nfc(str(d.get("relation", ""))),
            nfc(str(d.get("object", ""))),
        ) in targets

    affected = [r for r in rows if match_row(r)]
    if not affected:
        print("factlog eject: no candidate fact matches the given triple(s):", file=sys.stderr)
        for s, rel, o in sorted(targets):
            print(f"  - ({s}, {rel}, {o})", file=sys.stderr)
        return 1
    print(
        f"factlog eject (KB: {target}): fact mode — {len(affected)} candidate row(s) to "
        f"{'purge' if args.purge else 'supersede'}:"
    )
    for r in affected:
        print(
            f"  - ({nfc(r.get('subject', ''))}, {nfc(r.get('relation', ''))}, "
            f"{nfc(r.get('object', ''))})  [source: {r.get('source', '')}]"
        )
    # Keep runs/*.json on a supersede: the source stays, so the run keeps
    # re-asserting the fact and merge_candidates' superseded-preservation holds the
    # retirement durably across the next sync. Only --purge strips the run row too.
    return _EjectSelection(match_row, [], [], args.purge)


def _select_eject_sources(args, rows, disk_refs, all_refs, target, nfc):
    """Source / --orphans mode: select source refs to retire (and their on-disk
    conversions/originals). Returns an _EjectSelection, or an int exit code when
    nothing matches. Prints the plan exactly as cmd_eject used to inline."""
    import re
    from pathlib import Path, PurePosixPath

    # Tie each runs/sources/ conversion to the original it was made from, read
    # from the ingest provenance header ("... | source: <name> | ..."). Two
    # originals can share a stem (report.pptx + report.docx both -> report.md),
    # so a stem guess would let `eject report.docx` wrongly pull report.pptx's
    # conversion; the recorded origin name disambiguates. Falls back to a stem
    # match only when no header is present (a hand-made conversion).
    conv_origin: dict[str, str] = {}       # ref -> origin BASENAME
    conv_origin_raw: dict[str, str] = {}   # ref -> origin as the header wrote it
    for ref, p in disk_refs.items():
        if not ref.startswith("runs/sources/"):
            continue
        try:
            head = p.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
        except OSError:
            head = ""
        # Exclude the field delimiters from the capture so an empty/malformed
        # `source:` value (e.g. `... | source:  | converter: ...`) can't let
        # the lazy group swallow the `|`/`-->` and capture a garbage origin.
        # Also drop a whitespace-only capture (strips to "") — an empty origin
        # is "no reliable origin", not "an original named ''"; in --orphans
        # mode either misread would become an autonomous false deletion.
        m = re.search(r"source:\s*([^|>]+?)\s*(?:\||-->|$)", head)
        if m:
            origin = nfc(m.group(1).strip())
            if origin:
                # #214: the header may now record a sources/-relative path
                # (sub_a/data.hwpx) rather than a bare basename. Reduce it to the
                # basename so the pairing/orphan reconstruction below — which
                # rebuilds sources/<subdir>/<origin> from the conversion's own
                # mirrored subdir — stays correct for both header formats and no
                # legacy basename header regresses.
                # Keep BOTH: the basename (what pairing/--orphans want) and the
                # header value as written. #214 headers may carry a sources/-
                # relative path; discarding it forced conv_source_path to
                # re-derive the subdir from the conversion's mirror, which only
                # works for mirrored conversions and broke legacy flat ones (#221).
                conv_origin[ref] = PurePosixPath(origin).name
                conv_origin_raw[ref] = origin

    def conv_source_path(ref: str) -> str | None:
        """The KB-relative original a `runs/sources/` conversion was made from.

        Returns None when it CANNOT be known, so the caller falls back rather than
        inventing one:

        * header carries a path (#214) -> `sources/<header>` verbatim;
        * header is a bare name and the conversion is MIRRORED -> the mirrored
          subdir supplies the missing part (`runs/sources/sub/report.docx.md` ->
          `sources/sub/report.docx`);
        * header is a bare name and the conversion is FLAT (a pre-mirroring KB) ->
          **unknowable**. A flat conversion may well come from a nested original
          (README documents exactly that upgrade state), so reconstructing
          `sources/<name>` would be a guess. Deriving it anyway made `eject
          sub/report.html` silently match nothing on such KBs (#221 review);
        * no readable header -> the conversion's own mirrored path + stem.

        Getting this right is the whole point: comparing BASENAMES let `eject
        sub/report.docx` delete a top-level `report.docx`'s conversion — a source
        the user never named (#221).
        """
        raw = conv_origin_raw.get(ref)
        rel_parent = PurePosixPath(ref).relative_to("runs/sources").parent
        if raw and "/" in raw:
            return str(PurePosixPath("sources") / raw)
        name_part = raw or PurePosixPath(ref).stem
        candidate = str(PurePosixPath("sources") / rel_parent / name_part)
        if candidate in disk_refs:
            return candidate
        if str(rel_parent) != ".":
            # A mirrored conversion whose original is gone: the mirror still tells
            # us where it WAS, and that is the honest answer (an orphan).
            return candidate
        # Flat conversion with no original of that name at the top level: this is a
        # pre-mirroring KB whose original lives in a subdirectory the header never
        # recorded. Reconstructing `sources/<name>` would name a file that does not
        # exist, so say so and let the caller fall back.
        return None

    # Flat conversions whose origin cannot be attributed to a requested PATH.
    # Reported instead of guessed at (see the path branch of matches()).
    unattributable: set[str] = set()

    def matches(ref: str, name: str) -> bool:
        name = nfc(name)
        rp, np_ = Path(ref), Path(name)
        if ref == name:  # exact KB-relative ref
            return True
        is_conv = ref.startswith("runs/sources/")
        if "/" in name:
            # A path was given. Match the original at that path, and the conversion
            # whose ORIGIN IS THAT PATH — never a same-named file elsewhere (#221).
            # Both sides go through PurePosixPath so `./sub/x` and `sub//x` compare
            # equal to `sub/x` instead of missing.
            wanted = str(PurePosixPath(name if name.startswith("sources/") else f"sources/{name}"))
            if not is_conv:
                return ref == wanted
            origin = conv_source_path(ref)
            if origin is not None:
                return origin == wanted
            # Origin unknowable: a FLAT conversion with a bare-name header. Falling
            # back to a basename compare here re-created #221 — the very bug this
            # branch exists to kill. A flat conversion's original may sit in a
            # subdir the header never recorded (a pre-mirroring KB), but it may
            # equally have been ingested from OUTSIDE sources/ (README documents
            # exactly that: `factlog ingest report.docx --target ~/wiki`), or have
            # been deleted. Those states are indistinguishable, so a basename match
            # would delete the conversion of a document the user never named, with
            # exit 0.
            #
            # So do not guess. Silent under-ejection beats silent over-ejection: the
            # ref is reported afterwards (unattributable) so the user can eject it
            # by its own name (ingest --scan --force does NOT migrate it -- it only adds
            # a mirrored conversion beside the flat one and leaves the flat one).
            # Warn for a HEADERLESS flat conversion too: conv_origin has no entry for
            # it, so keying the warning on conv_origin left the commonest legacy shape
            # silently un-ejected. Compare its own name (report.md -> report.docx.md
            # both stem to the original's name under the two ingest namings).
            own = conv_origin.get(ref) or PurePosixPath(PurePosixPath(ref).name).stem
            if own == PurePosixPath(name).name or own == PurePosixPath(name).stem:
                unattributable.add(ref)
            return False
        # A bare NAME or STEM. An original that bears it is matched -- it genuinely IS
        # named that, no guess. A conversion is matched only when it can be ATTRIBUTED
        # to a source (conv_source_path), by that source's name/stem; an unattributable
        # flat conversion is reported, not matched. The path branch already refuses to
        # guess a flat conversion's origin (#221); the bare-name branch used to compare
        # basenames instead, so `eject report.html` deleted the conversion of a document
        # ingested from OUTSIDE sources/ that merely shared the name (#243).
        def _conv_bare_matches(want_name: str | None, want_stem: str | None) -> bool:
            origin_path = conv_source_path(ref)
            if origin_path is None:
                # Unattributable: only report it if it actually bears the requested name/
                # stem, so an unrelated conversion is not named.
                base = conv_origin.get(ref) or PurePosixPath(PurePosixPath(ref).name).stem
                shares = (want_name is not None and base == want_name) or (
                    want_stem is not None and PurePosixPath(base).stem == want_stem
                )
                if shares:
                    unattributable.add(ref)
                return False
            base = PurePosixPath(origin_path).name
            if want_name is not None:
                return base == want_name
            return PurePosixPath(base).stem == want_stem

        if np_.suffix:  # a bare filename with an extension
            if not is_conv:
                return rp.name == np_.name  # an original with that filename
            return _conv_bare_matches(np_.name, None)
        # bare stem
        if is_conv:
            return _conv_bare_matches(None, np_.stem)
        return rp.stem == np_.stem

    def _report_unattributable(refs: set[str]) -> None:
        """Name each conversion a PATH request refused to guess about.

        Silent under-ejection is just a quieter kind of wrong, so say which file was
        left and how to remove it. Naming the ref directly is the one route measured
        to work; `ingest --scan --force` only ADDS a mirrored conversion beside the
        flat one, so advertising it as a migration sent the user back to this same
        warning.
        """
        for ref in sorted(refs):
            print(
                f"factlog eject: NOT ejecting {ref} — its provenance cannot be tied to "
                f"a path (a flat conversion records only a basename, and may have been "
                f"made from a document outside sources/). Guessing here is what #221 "
                f"reported. To remove it, name it directly: factlog eject {ref}",
                file=sys.stderr,
            )

    matched: set[str] = set()
    if args.orphans:
        # Auto-detect orphaned sources — a source whose backing original is
        # gone. For a runs/sources/ conversion the origin is the file named
        # in its provenance header (conv_origin); it is an orphan when no
        # source under sources/ still bears that basename. A hand-placed
        # conversion (no header → no conv_origin entry) is kept. A cited ref
        # whose file is simply missing on disk is also an orphan. Only refs
        # under the two source roots are considered, so a malformed citation
        # is never auto-ejected.
        # Pairing a conversion with its backing original:
        #  - a *mirrored* conversion (runs/sources/<sub>/x.<ext>.md) carries the
        #    original's subdir, so the original it was made from lives at
        #    sources/<same-subdir>/<provenance-origin>. Verify that exact original
        #    is present. This is extension-aware and works for both the new naming
        #    (report.pptx.md) and the legacy stem naming (report.md): a same-stem
        #    sibling of another extension can neither mask a real orphan (#213
        #    MINOR) nor, across subtrees, hide a deleted original (#103).
        #  - a *flat* conversion (runs/sources/x.md — an original ingested without
        #    a subtree to mirror, so the subdir is unknown) has only the
        #    provenance basename as an origin signal; match by basename and keep
        #    erring toward retention.
        from pathlib import PurePosixPath

        src_basenames = {Path(r).name for r in disk_refs if not r.startswith("runs/sources/")}
        for ref in all_refs:
            if ref.startswith("runs/sources/"):
                if ref in disk_refs:
                    origin = conv_origin.get(ref)
                    # origin is not None == has a factlog provenance header
                    # (hand-placed conversions are kept).
                    if origin is not None:
                        conv_rel = ref[len("runs/sources/"):]
                        subdir = PurePosixPath(conv_rel).parent
                        if subdir.as_posix() != ".":
                            expected = (PurePosixPath("sources") / subdir / origin).as_posix()
                            paired = expected in disk_refs
                        else:
                            paired = origin in src_basenames
                        if not paired:
                            matched.add(ref)  # the original it was made from is gone
                else:
                    matched.add(ref)  # cited conversion whose file is already gone
            elif ref.startswith("sources/") and ref not in disk_refs:
                matched.add(ref)  # a directly-cited source whose file is gone
        if not matched:
            print(
                "factlog eject: no orphaned sources found "
                "(every cited source's original is present)."
            )
            return 0
        print(f"factlog eject (KB: {target}): orphan scan — {len(matched)} orphaned source(s)")
    else:
        for name in args.sources:
            hits = {ref for ref in all_refs if matches(ref, name)}
            if hits:
                matched |= hits
            else:
                print(f"factlog eject: no source matches '{name}'", file=sys.stderr)
        # Name what we deliberately did NOT match BEFORE any early return: when the
        # path matched nothing else -- the commonest state on a legacy KB -- a
        # warning printed after `return 1` is dead code, and the user gets a bare
        # "nothing to eject" with no hint that a conversion is sitting right there.
        _report_unattributable(unattributable - matched)
        if not matched:
            print("factlog eject: nothing to eject", file=sys.stderr)
            return 1

    def match_row(d: dict) -> bool:
        return nfc(str(d.get("source", "")).partition("#")[0]) in matched

    matched_sorted = sorted(matched)
    print(f"factlog eject (KB: {target}): {len(matched_sorted)} matched source ref(s):")
    for ref in matched_sorted:
        print(f"  - {ref}  [{'on disk' if ref in disk_refs else 'cited only (no file)'}]")

    conv_to_delete = [r for r in matched_sorted if r.startswith("runs/sources/") and r in disk_refs]
    orig_on_disk = [r for r in matched_sorted if not r.startswith("runs/sources/") and r in disk_refs]
    affected = [r for r in rows if match_row(r)]

    # Refuse to do the IRREVERSIBLE half of a job whose reversible half we just
    # declined. Deleting the original while leaving a conversion we could not
    # attribute -- and the facts citing it -- would strand those facts in
    # accepted.dl with their source file gone, and --purge would take the audit
    # trail with it. main deleted both; this branch must not delete only the one
    # the user cannot get back.
    # On disk only: a conversion that is merely still CITED cannot be stranded by
    # deleting the original -- its file is already gone, and its rows were retired
    # by whatever removed it.
    stranded = sorted(r for r in (unattributable - matched) if r in disk_refs)
    # If the named original's OWN conversion was matched, this eject is complete and
    # an unrelated flat conversion that merely shares the name is not this request's
    # business -- blocking then would refuse a job that strands nothing.
    if any(r.startswith("runs/sources/") for r in matched):
        stranded = []
    if args.delete_original and stranded and orig_on_disk:
        print(
            "factlog eject: refusing --delete-original — "
            f"{len(stranded)} conversion(s) of this name cannot be attributed to the "
            "path you gave. One of them MAY be this original's pre-mirroring "
            "conversion, in which case deleting the original would strand its facts "
            "with no source file; it may equally belong to another document, in which "
            "case ejecting it retires THAT document's facts. Decide, then re-run:",
            file=sys.stderr,
        )
        for ref in stranded:
            print(f"    factlog eject {ref}", file=sys.stderr)
        return 1

    action = "purge" if args.purge else "supersede"
    print(f"  candidates.csv: {len(affected)} row(s) to {action}")
    print(f"  runs/sources conversion(s) to delete: {len(conv_to_delete)}")
    if args.delete_original:
        print(f"  original(s) to delete (--delete-original): {len(orig_on_disk)}")
    elif orig_on_disk:
        print(f"  original(s) kept: {len(orig_on_disk)} (pass --delete-original to remove)")
    return _EjectSelection(match_row, conv_to_delete, orig_on_disk, True)


def cmd_eject(args: argparse.Namespace) -> int:
    """Inverse of `ingest`: remove a source — or a single fact — from the KB.

    Two mutually exclusive modes:

    Source mode (`eject <source>...`) — for each named source:
      - deletes its runs/sources/ conversion (the ingest output);
      - strips the source's extracted rows from every runs/*.json (removing a
        now-empty run file) so a later merge stays consistent;
      - retires the source's rows in facts/candidates.csv — marked `superseded`
        by default (kept for audit), or removed entirely with --purge;
      - optionally deletes the user's original under sources/ with
        --delete-original (off by default: ingest never created it).
    A source is named by its filename, stem, or KB-relative path. Naming the
    binary original (e.g. report.pptx) also matches its runs/sources/<stem>
    conversion; a bare stem matches every source with that stem. eject also
    catches a source cited only in candidates.csv (an already-orphaned ref).

    Orphan mode (`eject --orphans`) selects every orphaned source automatically
    instead of naming each one: a runs/sources/ conversion whose ingest original
    under sources/ is gone (read from the provenance header), or a cited source
    whose file no longer exists on disk. This reconciles deletions made directly
    in sources/ in one pass. A hand-placed runs/sources/ file (no provenance
    header) has no original to track and is never treated as an orphan. Honours
    --purge / --delete-original / --dry-run like an explicit source list.
    Detection pairs a conversion in a subdir (runs/sources/a/report.md, which
    ingest mirrors from sources/a/report.*) with its original by subdir-aware rel
    key, so same-name originals in different subtrees no longer mask each other; a
    flat conversion (runs/sources/report.md) keeps the legacy basename match since
    its path records no subdir. Either way it errs toward keeping. Renaming an
    original on disk without re-ingesting counts as orphaning its old conversion.

    Fact mode (`eject --fact SUBJECT RELATION OBJECT`, repeatable) — retires
    candidate rows matching the given (subject, relation, object) triple(s)
    across all sources, leaving the source files in place. The default
    `superseded` keeps runs/*.json untouched so the retirement survives a later
    sync (merge_candidates preserves it); --purge deletes the rows and strips
    runs/*.json. --delete-original is rejected in fact mode.

    Both modes recompile facts/accepted.dl so the engine input drops the retired
    facts. With --dry-run nothing changes; the planned actions are printed.
    """
    import csv
    import json
    import unicodedata
    from pathlib import Path

    from factlog.common import FACT_HEADER, is_hidden_source

    def nfc(s: str) -> str:
        return unicodedata.normalize("NFC", s)

    target_str, _ = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if not _require_kb(target, "eject"):
        return 1

    # Known source refs come from both the candidates table (cited sources) and
    # the two source roots on disk, so eject works even for an already-orphaned
    # citation whose file is gone.
    csv_path = target / "facts" / "candidates.csv"
    cited_refs: set[str] = set()
    rows: list[dict[str, str]] = []
    fieldnames: list[str] = []
    if csv_path.is_file():
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
        for row in rows:
            ref = nfc((row.get("source") or "").partition("#")[0])
            if ref:
                cited_refs.add(ref)

    disk_refs: dict[str, Path] = {}  # KB-relative ref -> path
    for base in ("sources", "runs/sources"):
        d = target / base
        if d.is_dir():
            for p in sorted(d.rglob("*")):
                # hidden = any dot-prefixed component under the root (#67), so a
                # file in sources/.git/ isn't treated as an ejectable source.
                if p.is_file() and not is_hidden_source(p, d):
                    disk_refs[nfc(p.relative_to(target).as_posix())] = p

    all_refs = set(disk_refs) | cited_refs

    fact_specs: list[list[str]] = list(args.fact or [])
    fact_mode = bool(fact_specs)
    orphan_mode = bool(args.orphans)

    # Exactly one selector: a source list, --orphans, OR --fact triples.
    if fact_mode and args.sources:
        print("factlog eject: give either source(s) or --fact, not both", file=sys.stderr)
        return 2
    if orphan_mode and (fact_mode or args.sources):
        print("factlog eject: --orphans cannot be combined with source(s) or --fact", file=sys.stderr)
        return 2
    if not fact_mode and not orphan_mode and not args.sources:
        print("factlog eject: nothing to eject (give a source, --orphans, or --fact S R O)", file=sys.stderr)
        return 2
    if fact_mode and args.delete_original:
        print("factlog eject: --delete-original is only valid when ejecting a source", file=sys.stderr)
        return 2

    # Selection differs by mode; the retirement tail below is shared.
    if fact_mode:
        sel = _select_eject_facts(args, rows, fact_specs, target, nfc)
    else:
        sel = _select_eject_sources(args, rows, disk_refs, all_refs, target, nfc)
    if isinstance(sel, int):
        return sel  # nothing matched / orphan scan empty — code already printed
    match_row, conv_to_delete, orig_on_disk, strip_runs = sel

    if args.dry_run:
        print("factlog eject: --dry-run, no changes made")
        return 0

    # 1. delete the ingest conversion(s) (source mode only)
    deleted_conv = 0
    for ref in conv_to_delete:
        try:
            disk_refs[ref].unlink()
            deleted_conv += 1
        except OSError as exc:
            print(f"factlog eject: could not delete {ref}: {exc}", file=sys.stderr)

    # 2. strip the retired rows from runs/*.json (drop now-empty run files)
    stripped_rows = 0
    removed_files = 0
    runs_dir = target / "runs"
    if strip_runs and runs_dir.is_dir():
        for jp in sorted(runs_dir.glob("*.json")):
            try:
                data = json.loads(jp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                # surface it: a corrupt run file left behind could still hold the
                # retired rows and resurrect them on a later merge.
                print(f"factlog eject: skipping unreadable {jp.name}: {exc}", file=sys.stderr)
                continue
            if not isinstance(data, list):
                continue  # non-candidate run JSON (e.g. a policy-gen object): leave it
            kept = [item for item in data if not (isinstance(item, dict) and match_row(item))]
            if len(kept) != len(data):
                stripped_rows += len(data) - len(kept)
                if kept:
                    _atomic_write_text(jp, json.dumps(kept, ensure_ascii=False, indent=2) + "\n")
                else:
                    jp.unlink()
                    removed_files += 1

    # 3. retire candidate rows: supersede (default) or purge
    changed = 0
    if rows:
        # Guard the supersede path against a malformed/legacy header missing the
        # status column — without this, DictWriter would raise mid-write on a
        # truncated ("w") file and lose every row. Fall back to the canonical
        # FACT_HEADER, and ensure 'status' exists when we set it.
        out_fields = fieldnames or list(FACT_HEADER)
        if not args.purge and "status" not in out_fields:
            out_fields = [*out_fields, "status"]
        new_rows: list[dict[str, str]] = []
        for r in rows:
            if match_row(r):
                changed += 1
                if args.purge:
                    continue  # drop the row entirely
                r["status"] = "superseded"
            new_rows.append(r)
        # Atomic temp+replace (see _atomic_write_csv) so an interrupted run can't
        # leave a half-written candidates.csv.
        _atomic_write_csv(csv_path, new_rows, out_fields)

    # 4. optionally delete the user's original(s) (source mode only)
    deleted_orig = 0
    if args.delete_original:
        for ref in orig_on_disk:
            try:
                disk_refs[ref].unlink()
                deleted_orig += 1
            except OSError as exc:
                print(f"factlog eject: could not delete {ref}: {exc}", file=sys.stderr)

    # 5. recompile accepted.dl so the engine input drops the retired facts
    recompile_failed = False
    if csv_path.is_file():
        recompile_failed = not _recompile_accepted(target, "eject")

    verb = "purged" if args.purge else "superseded"
    recompiled = "accepted.dl NOT recompiled" if recompile_failed else "accepted.dl recompiled"
    if fact_mode:
        print(
            f"factlog eject: {changed} candidate row(s) {verb}, {stripped_rows} run row(s) "
            f"stripped ({removed_files} run file(s) removed); {recompiled}"
        )
    else:
        print(
            f"factlog eject: {deleted_conv} conversion(s) deleted, {stripped_rows} run row(s) "
            f"stripped ({removed_files} run file(s) removed), {changed} candidate row(s) {verb}, "
            f"{deleted_orig} original(s) deleted; {recompiled}"
        )
    if changed:
        print(
            "factlog eject: note — pages/ may still reference the removed facts; "
            "run /factlog sync to regenerate them."
        )
    if fact_mode and args.purge:
        print(
            "factlog eject: note — the source remains; a later /factlog sync may re-extract "
            "this fact. Use the default (supersede) to keep it retired durably."
        )
    if not fact_mode and orig_on_disk and not args.delete_original:
        print(
            "factlog eject: note — kept original(s) will be re-converted on the next "
            "`factlog ingest --scan` / `/factlog sync`; pass --delete-original to remove them."
        )
    return 1 if recompile_failed else 0


def _make_zotero_client(config):
    """Build the real Zotero client. Indirected so tests can inject a fake."""
    from factlog.integrations.zotero.api_client import ZoteroClient

    return ZoteroClient(config)


def _convert_placed_pdfs(target, paths, *, quiet: bool) -> int:
    """Convert exactly the given PDF paths via the existing ingest pipeline.

    Reuses `factlog ingest <paths>` (the same converter + provenance header
    /factlog sync's ingest step uses) so placed PDFs become runs/sources/*.txt.
    Passing explicit paths — rather than --scan — keeps the scope and the exit
    code tied to *this import's* PDFs, not other binaries already in sources/.
    Conversion is idempotent (an up-to-date one is skipped). Indirected so tests
    can stub it; in quiet (porcelain) mode ingest's narration is suppressed.
    Returns ingest's exit code (non-zero only on a genuine conversion failure).
    """
    argv = ["ingest", *[str(p) for p in paths], "--target", str(target)]
    ingest_args = build_parser().parse_args(argv)
    if not quiet:
        return cmd_ingest(ingest_args)
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return cmd_ingest(ingest_args)


def cmd_zotero_import(args: argparse.Namespace) -> int:
    """Import Zotero bibliographic metadata into the active KB's sources/ (phase 1).

    Fetches the selected items (one of --collection/--tag/--items) over the Local
    API and writes one source markdown per item. Imported items remain plain
    sources — they still pass the usual sync -> review -> accept gate before
    becoming facts (P1/P2). Zotero is read-only (P4).
    """
    from datetime import datetime, timezone
    from pathlib import Path

    from factlog.integrations.zotero.api_client import ZoteroConnectionError, ZoteroError
    from factlog.integrations.zotero.config import ZoteroConfigError, load_config
    from factlog.integrations.zotero.importer import import_items
    from factlog.integrations.common.porcelain import porcelain_field

    porcelain = getattr(args, "porcelain", False)
    dry_run = getattr(args, "dry_run", False)
    pdf = getattr(args, "pdf", False)
    annotations = getattr(args, "annotations", False)

    def _human(*a, **k):
        # Suppress human narration in porcelain mode; errors still go to stderr.
        if not porcelain:
            print(*a, **k)

    target_str, source = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if source in ("config", "cwd"):
        _human(f"factlog zotero-import: target KB {target} (from {source})")
    if not _require_kb(target, "zotero-import"):
        return 1

    # A malformed KB policy file is a user error, not a crash.
    try:
        config = load_config(kb_root=target)
    except ZoteroConfigError as exc:
        print(f"factlog zotero-import: {exc}", file=sys.stderr)
        return 1

    # Exactly one selector is set (argparse mutually-exclusive, required). Reject a
    # blank value for whichever was chosen, uniformly, before touching Zotero.
    if args.items is not None:
        items = [s.strip() for s in args.items.split(",") if s.strip()]
        if not items:
            print("factlog zotero-import: --items needs at least one item key", file=sys.stderr)
            return 1
        label = f"items ({len(items)} requested)"
    elif args.collection is not None:
        if not args.collection.strip():
            print("factlog zotero-import: --collection needs a non-empty name", file=sys.stderr)
            return 1
        items = None
        label = f'collection "{args.collection}"'
    else:
        if not args.tag.strip():
            print("factlog zotero-import: --tag needs a non-empty value", file=sys.stderr)
            return 1
        items = None
        label = f'tag "{args.tag}"'

    _human("Connecting to Zotero (Local API)...")
    # No microseconds: a tidy, stable provenance timestamp in the front matter.
    imported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    try:
        report = import_items(
            _make_zotero_client(config),
            target=target,
            config=config,
            collection=args.collection,
            tag=args.tag,
            items=items,
            imported_at=imported_at,
            dry_run=dry_run,
            pdf=pdf,
            annotations=annotations,
        )
    except ZoteroConnectionError as exc:
        print(f"factlog zotero-import: {exc}", file=sys.stderr)
        return 2
    except (ZoteroError, ValueError) as exc:
        print(f"factlog zotero-import: {exc}", file=sys.stderr)
        return 1

    # Convert this import's PDFs to text via the existing ingest pipeline. The
    # set is every PDF now present for these items (placed or already-there), so a
    # PDF whose conversion failed on a prior run is retried; ingest skips
    # up-to-date ones. Skipped on a dry run. A conversion failure adds to the exit
    # code but never aborts — the bibliographic import already succeeded.
    pdf_files = [
        o.path for o in report.pdf_outcomes
        if o.status in ("placed", "skipped") and o.path is not None
    ]

    def _run_conversion(quiet: bool) -> int:
        if pdf and not dry_run and pdf_files:
            return _convert_placed_pdfs(target, pdf_files, quiet=quiet)
        return 0

    if porcelain:
        # Stable machine contract, tab-separated, LF-terminated. Order-independent
        # (parse by first field). Count/target rows always present:
        #   imported\t<n> / skipped\t<n> / errors\t<n> / dry_run\t<0|1>
        #   target\t<abs sources dir>
        # With --pdf, PDF placement counts are added:
        #   pdf_placed\t<n> / pdf_skipped\t<n> / pdf_errors\t<n>
        # In --dry-run only, a per-item row precedes them so scripts can read the
        # prospective filenames the human output shows:
        #   item\t<status>\t<zotero_key>\t<would-be filename>
        # On a hard error (connection/config) nothing is written to stdout and the
        # exit code is non-zero — the error goes to stderr.
        # The key and the filename go through :func:`porcelain_field` so a stray
        # tab/newline can never split a row (#416).
        convert_rc = _run_conversion(quiet=True)
        if dry_run:
            for outcome in report.outcomes:
                name = outcome.path.name if outcome.path is not None else ""
                print(
                    f"item\t{outcome.status}\t{porcelain_field(outcome.key)}\t"
                    f"{porcelain_field(name)}"
                )
        print(f"imported\t{report.imported}")
        print(f"skipped\t{report.skipped}")
        print(f"errors\t{report.errors}")
        if pdf:
            print(f"pdf_placed\t{report.pdf_placed}")
            print(f"pdf_skipped\t{report.pdf_skipped}")
            print(f"pdf_errors\t{report.pdf_errors}")
        if annotations:
            print(f"annotations_written\t{report.annotations_written}")
            print(f"annotations_updated\t{report.annotations_updated}")
            print(f"annotations_skipped\t{report.annotations_skipped}")
            print(f"annotation_errors\t{report.annotation_errors}")
        print(f"dry_run\t{'1' if dry_run else '0'}")
        print(f"target\t{porcelain_field(str(target / 'sources'))}")
        return 1 if (report.errors or report.pdf_errors or report.annotation_errors or convert_rc) else 0

    verb = "Would import" if dry_run else "Imported"
    if dry_run:
        _human("Dry run: no files will be created.")
    _human(f"Found {label}: {len(report.outcomes)} item(s)")
    _human(f"{'Would import to' if dry_run else 'Importing to'} KB: {target}\n")
    marks = {"imported": "✓", "skipped": "↷", "merged": "⇄", "error": "⚠"}
    for outcome in report.outcomes:
        name = outcome.path.name if outcome.path is not None else "-"
        status = (
            ("would import" if dry_run else "imported") if outcome.status == "imported"
            else ("would skip" if dry_run else "skipped") if outcome.status == "skipped"
            else ("would merge" if dry_run else "merged") if outcome.status == "merged"
            else "error"
        )
        detail = f" ({outcome.reason})" if outcome.reason else ""
        ident = f" ({outcome.key})" if outcome.key else ""
        suffix = f" -> {name}" if dry_run and outcome.status == "imported" else ""
        _human(f"  {marks.get(outcome.status, '?')} {outcome.title}{ident} - {status}{detail}{suffix}")

    _human("\nSummary:")
    _human(f"  {verb}: {report.imported}")
    _human(f"  {'Would skip' if dry_run else 'Skipped'}:  {report.skipped}")
    _human(f"  Errors:   {report.errors}")
    if pdf:
        pdf_verb = "Would fetch" if dry_run else "PDFs"
        _human(f"  {pdf_verb}:     placed {report.pdf_placed}, "
               f"skipped {report.pdf_skipped}, errors {report.pdf_errors}")
    if annotations:
        ann_verb = "Would write" if dry_run else "Annotations"
        _human(f"  {ann_verb}: written {report.annotations_written}, "
               f"updated {report.annotations_updated}, skipped {report.annotations_skipped}, "
               f"errors {report.annotation_errors}")

    # Convert after the import summary so the narration reads in order.
    convert_rc = 0
    if pdf and not dry_run and pdf_files:
        _human("\nConverting PDFs to text (ingest)...")
        convert_rc = _convert_placed_pdfs(target, pdf_files, quiet=False)

    if report.imported and not dry_run:
        _human("\nNext step: run '/factlog sync' to extract candidate facts.")
    return 1 if (report.errors or report.pdf_errors or report.annotation_errors or convert_rc) else 0


def _make_openalex_client(config):
    """Build the real OpenAlex client. Indirected so tests can inject a fake."""
    from factlog.integrations.openalex.api_client import OpenAlexClient

    return OpenAlexClient(config)


def _openalex_budget_warning(client) -> str:
    """A warning when too little of the daily credit budget remains, else "".

    OpenAlex bills a search 10 credits against a ~1000/day budget, so roughly a
    hundred searches a day (#51). The operator is told, never blocked.
    """
    rate = getattr(client, "rate_limit", None)
    if rate is None or not rate.is_low:
        return ""
    return (
        f"⚠ OpenAlex daily credit budget is nearly spent: {rate.remaining} left "
        f"(a search costs 10). It refills about {round((rate.reset_seconds or 0) / 3600)}h "
        "from now."
    )


def _openalex_report_lines(report, dry_run: bool) -> list[str]:
    """The per-work narration shared by the openalex-* and arXiv commands.

    ``merged`` (an arXiv deposit folded into an existing original's sidecar, §7.3)
    needs its own glyph and label: without them the status ternary's ``else``
    branch mislabels it as ``error`` and ``marks.get`` yields ``?``.
    """
    marks = {"imported": "✓", "skipped": "↷", "merged": "⇄", "error": "⚠"}
    lines = []
    for outcome in report.outcomes:
        status = (
            ("would import" if dry_run else "imported") if outcome.status == "imported"
            else ("would skip" if dry_run else "skipped") if outcome.status == "skipped"
            else ("would merge" if dry_run else "merged") if outcome.status == "merged"
            else "error"
        )
        detail = f" ({outcome.reason})" if outcome.reason else ""
        name = outcome.path.name if outcome.path is not None else "-"
        suffix = f" -> {name}" if dry_run and outcome.status == "imported" else ""
        lines.append(
            f"  {marks.get(outcome.status, '?')} {outcome.title} ({outcome.key})"
            f" - {status}{detail}{suffix}"
        )
    return lines


def _openalex_placeholder_warnings(report) -> list[str]:
    """Warn about works OpenAlex titled ``"null"`` — imported, but suspect.

    The record is real and is not dropped (a paper could legitimately be titled
    "Null"), but its source file is slugged from that string, so the operator
    should look at it.
    """
    from factlog.integrations.openalex.work_parser import is_placeholder_title

    return [
        f'⚠ {o.key} has the literal title "null"; OpenAlex records no real title '
        f"for it. Check {o.path.name if o.path else 'the source'}."
        for o in report.outcomes
        if o.status == "imported" and is_placeholder_title(o.title)
    ]


def _candidate_porcelain_lines(report) -> list[str]:
    """The ``candidate``/``candidates`` porcelain rows for surfaced merge candidates (#75).

    A NEW leading token, added to stdout without touching the six summary lines a
    consumer already parses (imported/skipped/merged/errors/dry_run/target). Existing
    consumers parse by first field and ignore an unknown token (the contract stated
    at the porcelain blocks below), so this is compatible:

        candidate\t<new-source-id>\t<existing-source-filename>\t<title-similarity>
        candidates\t<n>

    One ``candidate`` row per surfaced pair, in report order, then the count. Nothing
    is emitted through the per-work status ternary — a candidate is never a status
    (the #65 trap, where an unhandled status fell to ``error`` with a ``?`` glyph).

    The key and the existing filename go through :func:`porcelain_field`: both are
    upstream data (``o.key`` is the same ``openalex_id``/``versioned_id`` the search
    ``result`` row already gates), and a tab in either added a column here (#416,
    measured). The score is formatted from a float, so it needs no gate.
    """
    from factlog.integrations.common.porcelain import porcelain_field

    lines = [
        f"candidate\t{porcelain_field(o.key)}\t"
        f"{porcelain_field(o.candidate.existing_path.name)}\t{o.candidate.score:.4f}"
        for o in report.candidates
    ]
    lines.append(f"candidates\t{len(report.candidates)}")
    return lines


def _candidate_notes(report) -> list[str]:
    """Human-readable stderr notes for surfaced merge candidates (#75).

    The paper imported as a new file; the note points a human at the existing source
    it resembles and at the ledger, where a pair is rejected by hand-editing the JSON
    (there is no ``reject`` command in this release, by design — #75 H4)."""
    notes = []
    if report.candidate_ledger_error:
        # The import succeeded, but the duplicate check that would have caught a
        # near-identical existing source never ran. Saying nothing would leave the
        # operator believing it did.
        notes.append(
            "⚠ merge-candidate detection was disabled for this run: "
            f"{report.candidate_ledger_error}. Repair or delete "
            "merge-candidates/candidates.json, then re-import to re-check."
        )
    for o in report.candidates:
        notes.append(
            f"⚠ {o.key} resembles an existing source "
            f"({o.candidate.existing_path.name}, title similarity "
            f"{o.candidate.score:.2f}) but shares no DOI/PMID/arXiv id. Imported as a "
            "new file; nothing was merged. Review the pair in "
            "merge-candidates/candidates.json (reject it by hand-editing its state)."
        )
    return notes


def _openalex_finish(report, target, *, dry_run: bool, porcelain: bool, warning: str) -> int:
    """Emit the shared summary for an openalex import and return the exit code.

    Every caller-influenced field on a porcelain row goes through
    :func:`porcelain_field` so a stray tab/newline can never split a row (#416).
    """
    from factlog.integrations.common.porcelain import porcelain_field

    notes = _openalex_placeholder_warnings(report) + _candidate_notes(report)
    if porcelain:
        # Stable machine contract, tab-separated, LF-terminated. Order-independent
        # (parse by first field):
        #   imported\t<n> / skipped\t<n> / errors\t<n> / dry_run\t<0|1>
        #   target\t<abs sources dir>
        # In --dry-run only, a per-work row precedes them:
        #   work\t<status>\t<openalex_id>\t<would-be filename>
        if dry_run:
            for outcome in report.outcomes:
                name = outcome.path.name if outcome.path is not None else ""
                print(
                    f"work\t{outcome.status}\t{porcelain_field(outcome.key)}\t"
                    f"{porcelain_field(name)}"
                )
        print(f"imported\t{report.imported}")
        print(f"skipped\t{report.skipped}")
        print(f"merged\t{report.merged}")
        print(f"errors\t{report.errors}")
        print(f"dry_run\t{'1' if dry_run else '0'}")
        print(f"target\t{porcelain_field(str(target / 'sources'))}")
        # Surfaced merge candidates (#75): a new token on stdout, after the six
        # summary lines those stay byte-unchanged.
        for line in _candidate_porcelain_lines(report):
            print(line)
        # Warnings go to stderr so they never pollute the machine contract.
        for note in notes:
            print(note, file=sys.stderr)
        if warning:
            print(warning, file=sys.stderr)
        return 1 if report.errors else 0

    print(f"\n{'Would import to' if dry_run else 'Importing to'} KB: {target}\n")
    for line in _openalex_report_lines(report, dry_run):
        print(line)
    print("\nSummary:")
    print(f"  {'Would import' if dry_run else 'Imported'}: {report.imported}")
    print(f"  {'Would skip' if dry_run else 'Skipped'}:  {report.skipped}")
    # A merge is a success (this OpenAlex view recorded against an existing
    # original, §7.3), so it is its own line, never folded into errors.
    print(f"  {'Would merge' if dry_run else 'Merged'}:   {report.merged}")
    print(f"  Errors:   {report.errors}")
    for note in notes:
        print(f"\n{note}", file=sys.stderr)
    if warning:
        print(f"\n{warning}", file=sys.stderr)
    if report.imported and not dry_run:
        print("\nNext step: run '/factlog sync' to extract candidate facts.")
    return 1 if report.errors else 0


def _openalex_prepare(args, command: str):
    """Resolve the target KB and OpenAlex settings, or None on a user error."""
    from pathlib import Path

    from factlog.integrations.openalex.config import OpenAlexConfigError, load_config

    porcelain = getattr(args, "porcelain", False)
    target_str, source = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if source in ("config", "cwd") and not porcelain:
        print(f"factlog {command}: target KB {target} (from {source})")
    if not _require_kb(target, command):
        return None
    try:
        return target, load_config(kb_root=target)
    except OpenAlexConfigError as exc:
        print(f"factlog {command}: {exc}", file=sys.stderr)
        return None


def _select_search_results(works, *, interactive: bool, command: str) -> list:
    """Ask which of the search results to import (spec §5.3).

    Shared by ``openalex-search`` and ``arxiv-search`` so the prompt, the
    ``none``/``all``/number parsing and the no-TTY rule cannot drift between the
    two — the drift that let ``source_name`` and ``imported_from`` disagree
    (#64). The only source-specific thing is the command name in the "ignoring"
    diagnostic, passed in as ``command``.

    Without a terminal — a pipe, a script, ``--porcelain``, ``--dry-run`` — no
    prompt is issued and nothing is selected: a command that cannot ask must not
    guess, and writing every hit would be a surprise. ``--all`` is the explicit
    way to import a whole result set.
    """
    if not works or not interactive:
        return []
    try:
        answer = input("\nImport which? (comma-separated numbers, or 'all', or 'none')\n> ").strip()
    except EOFError:
        return []

    if not answer or answer.lower() == "none":
        return []
    if answer.lower() == "all":
        return list(works)

    chosen = []
    for token in answer.split(","):
        token = token.strip()
        if not token.isdigit() or not 1 <= int(token) <= len(works):
            print(f"factlog {command}: ignoring '{token}'", file=sys.stderr)
            continue
        candidate = works[int(token) - 1]
        if candidate not in chosen:
            chosen.append(candidate)
    return chosen


def _openalex_check_limit(args, config, command: str) -> bool:
    """Reject an out-of-range --limit before any network call is made."""
    limit = getattr(args, "limit", None)
    if limit is None:
        return True
    if limit < 1 or limit > config.max_limit:
        print(
            f"factlog {command}: --limit must be between 1 and {config.max_limit}, got {limit}",
            file=sys.stderr,
        )
        return False
    return True


def _openalex_show_results(works, count: int, *, porcelain: bool, scope: str = "",
                           heading: str = "") -> None:
    from factlog.integrations.common.porcelain import porcelain_field

    if porcelain:
        # `scope` distinguishes the two directions of openalex-cite, which emit
        # two result blocks into one stream; openalex-search leaves it empty.
        prefix = f"\t{scope}" if scope else ""
        for index, work in enumerate(works, 1):
            flag = "retracted" if work.openalex_is_retracted else "-"
            # Id and title are upstream data: a tab adds a column and a line break
            # splits the row, either way a positional consumer reads the wrong
            # field (#406).
            print(f"result{prefix}\t{index}\t{porcelain_field(work.openalex_id)}\t{flag}\t"
                  f"{porcelain_field(work.title or '')}")
        print(f"found{prefix}\t{count}")
        return

    print(heading or f"Found {count} results, showing top {len(works)}:\n")
    for index, work in enumerate(works, 1):
        authors = f"{work.authors[0]}" if work.authors else "anonymous"
        year = work.year or "n.d."
        cites = f", cited by {work.cited_by_count}" if work.cited_by_count is not None else ""
        print(f"  {index}. {work.openalex_id} \"{work.title or '(untitled)'}\" "
              f"({authors} {year}{cites})")
        if work.openalex_is_retracted:
            print("      ⚠ OpenAlex flags this as RETRACTED (unverified; confirm against PubMed)")


def cmd_openalex_search(args: argparse.Namespace) -> int:
    """Search OpenAlex and import the chosen works into the active KB's sources/.

    Costs 10 credits of the ~1000/day budget per search (#51). Results are shown,
    then imported only on an explicit selection — or wholesale with --all.
    Imported works remain plain sources: they still pass the usual
    sync -> review -> accept gate (P1/P2). OpenAlex is read-only (P4).
    """
    from datetime import datetime, timezone

    from factlog.integrations.openalex.api_client import (
        OpenAlexConnectionError,
        OpenAlexError,
    )
    from factlog.integrations.openalex.importer import import_works, parse_works

    prepared = _openalex_prepare(args, "openalex-search")
    if prepared is None:
        return 1
    target, config = prepared

    porcelain = getattr(args, "porcelain", False)
    dry_run = getattr(args, "dry_run", False)
    if not _openalex_check_limit(args, config, "openalex-search"):
        return 1
    # A bogus --type would be answered with 200 and zero results, after the search
    # was charged 10 credits. Reject it at the boundary, before anything is spent.
    if args.type is not None:
        from factlog.integrations.openalex.api_client import validate_work_type

        try:
            validate_work_type(args.type)
        except OpenAlexError as exc:
            print(f"factlog openalex-search: {exc}", file=sys.stderr)
            return 1

    client = _make_openalex_client(config)
    if not porcelain:
        print(f'Searching OpenAlex: "{args.query}"...')
    try:
        page = client.search_works(
            args.query, year=args.year, work_type=args.type, limit=args.limit
        )
    except OpenAlexConnectionError as exc:
        print(f"factlog openalex-search: {exc}", file=sys.stderr)
        return 2
    except OpenAlexError as exc:
        print(f"factlog openalex-search: {exc}", file=sys.stderr)
        return 1

    works = parse_works(page.results)
    _openalex_show_results(works, page.count, porcelain=porcelain)
    warning = _openalex_budget_warning(client)

    if args.all:
        chosen = works
    else:
        interactive = not porcelain and not dry_run and sys.stdin.isatty()
        chosen = _select_search_results(works, interactive=interactive, command="openalex-search")
        if not chosen and not porcelain:
            hint = " Re-run with --all to import every result." if works and not dry_run else ""
            print(f"\nNothing selected; no files written.{hint}")
            if warning:
                print(f"\n{warning}", file=sys.stderr)
            return 0
    if not chosen:
        if warning:
            print(warning, file=sys.stderr)
        return 0

    imported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    report = import_works(
        chosen, target=target, config=config, imported_at=imported_at, dry_run=dry_run
    )
    return _openalex_finish(report, target, dry_run=dry_run, porcelain=porcelain, warning=warning)


def cmd_openalex_import(args: argparse.Namespace) -> int:
    """Import one OpenAlex work by id or DOI. Costs no credits (#51)."""
    from datetime import datetime, timezone

    from factlog.integrations.openalex.api_client import (
        OpenAlexConnectionError,
        OpenAlexError,
    )
    from factlog.integrations.openalex.importer import fetch_work, import_works

    prepared = _openalex_prepare(args, "openalex-import")
    if prepared is None:
        return 1
    target, config = prepared

    porcelain = getattr(args, "porcelain", False)
    dry_run = getattr(args, "dry_run", False)

    client = _make_openalex_client(config)
    try:
        work = fetch_work(client, work_id=args.work_id or "", doi=args.doi or "")
    except OpenAlexConnectionError as exc:
        print(f"factlog openalex-import: {exc}", file=sys.stderr)
        return 2
    except OpenAlexError as exc:
        print(f"factlog openalex-import: {exc}", file=sys.stderr)
        return 1

    imported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    report = import_works(
        [work], target=target, config=config, imported_at=imported_at, dry_run=dry_run
    )
    return _openalex_finish(
        report, target, dry_run=dry_run, porcelain=porcelain,
        warning=_openalex_budget_warning(client),
    )


def _make_arxiv_client(config):
    """Build the real arXiv client. Indirected so tests can inject a fake."""
    from factlog.integrations.arxiv.client import ArxivClient

    return ArxivClient(config)


def _arxiv_prepare(args, command: str):
    """Resolve the target KB and arXiv settings, or None on a user error."""
    from pathlib import Path

    from factlog.integrations.arxiv.config import (
        ArxivConfigError,
        load_config,
        low_delay_warning,
    )

    porcelain = getattr(args, "porcelain", False)
    target_str, source = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if source in ("config", "cwd") and not porcelain:
        print(f"factlog {command}: target KB {target} (from {source})")
    if not _require_kb(target, command):
        return None
    try:
        config = load_config(kb_root=target)
    except ArxivConfigError as exc:
        print(f"factlog {command}: {exc}", file=sys.stderr)
        return None
    # A below-recommendation request_delay is honoured, but never in silence: name
    # it once here, at the single per-run config-resolution choke point, so it is
    # said once rather than per request. stderr, so --porcelain stdout stays clean.
    warning = low_delay_warning(config.request_delay)
    if warning is not None:
        print(f"factlog {command}: {warning}", file=sys.stderr)
    return target, config


def _arxiv_withdrawal_warnings(report, works) -> list[str]:
    """One stderr line per *imported* withdrawn paper, naming the agent.

    Withdrawal is arXiv's own signal, not a fact: the note says so, names who
    withdrew the paper, and never uses the word "retracted" (#57). Skipped and
    errored records carry no such warning. Warnings go to stderr only, never
    stdout or the ``--porcelain`` contract.
    """
    from factlog.integrations.arxiv.source_writer import withdrawal_agent

    by_key = {w.versioned_id: w for w in works}
    lines = []
    for outcome in report.outcomes:
        if outcome.status != "imported":
            continue
        work = by_key.get(outcome.key)
        if work is not None and work.withdrawn:
            agent = withdrawal_agent(work.withdrawn_by)
            lines.append(
                f"⚠ arXiv reports {outcome.key} as withdrawn (by {agent}). Withdrawal "
                "is not retraction; this unverified signal flags the paper for human "
                "review before any claim from it is trusted."
            )
    return lines


def _arxiv_finish(report, target, *, dry_run: bool, porcelain: bool, warnings) -> int:
    """Emit the shared summary for an arXiv import and return the exit code.

    Mirrors :func:`_openalex_finish`'s porcelain contract exactly; the only
    difference is the warning source (withdrawal notes, not placeholder titles or
    a credit budget — arXiv is free), and it mirrors the :func:`porcelain_field`
    gate on every caller-influenced field with it (#416).
    """
    from factlog.integrations.common.porcelain import porcelain_field

    if porcelain:
        # Stable machine contract, tab-separated, LF-terminated. Order-independent
        # (parse by first field):
        #   imported\t<n> / skipped\t<n> / merged\t<n> / errors\t<n>
        #   dry_run\t<0|1> / target\t<abs sources dir>
        # In --dry-run only, a per-work row precedes them:
        #   work\t<status>\t<versioned arxiv id>\t<would-be or existing filename>
        if dry_run:
            for outcome in report.outcomes:
                name = outcome.path.name if outcome.path is not None else ""
                print(
                    f"work\t{outcome.status}\t{porcelain_field(outcome.key)}\t"
                    f"{porcelain_field(name)}"
                )
        print(f"imported\t{report.imported}")
        print(f"skipped\t{report.skipped}")
        print(f"merged\t{report.merged}")
        print(f"errors\t{report.errors}")
        print(f"dry_run\t{'1' if dry_run else '0'}")
        print(f"target\t{porcelain_field(str(target / 'sources'))}")
        # Surfaced merge candidates (#75): a new token on stdout, after the six
        # summary lines those stay byte-unchanged.
        for line in _candidate_porcelain_lines(report):
            print(line)
        # Warnings go to stderr so they never pollute the machine contract.
        for warning in warnings:
            print(warning, file=sys.stderr)
        for note in _candidate_notes(report):
            print(note, file=sys.stderr)
        return 1 if report.errors else 0

    print(f"\n{'Would import to' if dry_run else 'Importing to'} KB: {target}\n")
    for line in _openalex_report_lines(report, dry_run):
        print(line)
    print("\nSummary:")
    print(f"  {'Would import' if dry_run else 'Imported'}: {report.imported}")
    print(f"  {'Would skip' if dry_run else 'Skipped'}:  {report.skipped}")
    # A merge is a success (an arXiv deposit recorded against an existing
    # original), so it is reported on its own line, never folded into errors.
    print(f"  {'Would merge' if dry_run else 'Merged'}:   {report.merged}")
    print(f"  Errors:   {report.errors}")
    for warning in warnings:
        print(f"\n{warning}", file=sys.stderr)
    for note in _candidate_notes(report):
        print(f"\n{note}", file=sys.stderr)
    if report.imported and not dry_run:
        print("\nNext step: run '/factlog sync' to extract candidate facts.")
    return 1 if report.errors else 0


def cmd_arxiv_import(args: argparse.Namespace) -> int:
    """Import arXiv papers by id into sources/. Free; up to 100 ids per run (§11)."""
    from datetime import datetime, timezone

    from factlog.integrations.arxiv.client import (
        ArxivConnectionError,
        ArxivError,
    )
    from factlog.integrations.arxiv.id_normalizer import ArxivIdError, normalize_arxiv_id
    from factlog.integrations.arxiv.importer import import_works

    prepared = _arxiv_prepare(args, "arxiv-import")
    if prepared is None:
        return 1
    target, config = prepared

    porcelain = getattr(args, "porcelain", False)
    dry_run = getattr(args, "dry_run", False)

    # Normalize each id individually: a syntactically bad id is a per-id error,
    # not a batch failure. Only a transport failure (below) is fatal for the run.
    valid, invalid = [], []
    for raw in args.id:
        try:
            valid.append(normalize_arxiv_id(raw))
        except ArxivIdError as exc:
            invalid.append((raw, str(exc)))

    works, missing = [], []
    if valid:
        client = _make_arxiv_client(config)
        try:
            # dry-run still hits the network: the title, withdrawal signal and slug
            # are all needed to predict the outcome. It writes no files. The client
            # re-normalizes, so the already-canonical string form is passed.
            batch = client.fetch_works([str(identifier) for identifier in valid])
        except ArxivConnectionError as exc:
            print(f"factlog arxiv-import: {exc}", file=sys.stderr)
            return 2
        except ArxivError as exc:
            # Service, response, >100-ids and other transport failures are fatal
            # for the whole request — the batch cannot be trusted partial.
            print(f"factlog arxiv-import: {exc}", file=sys.stderr)
            return 1
        works, missing = batch.works, batch.missing

    imported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    report = import_works(
        works, missing, invalid,
        target=target, config=config, imported_at=imported_at, dry_run=dry_run,
    )
    warnings = _arxiv_withdrawal_warnings(report, works)
    return _arxiv_finish(
        report, target, dry_run=dry_run, porcelain=porcelain, warnings=warnings
    )


def _make_pubmed_client(config):
    """Build the real PubMed client. Indirected so tests can inject a fake."""
    from factlog.integrations.pubmed.client import PubMedClient

    return PubMedClient(config)


def _pubmed_prepare(args, command: str):
    """Resolve the target KB and PubMed settings, or None on a user error.

    Mirrors :func:`_arxiv_prepare` / :func:`_openalex_prepare`. The integration
    package is imported here, inside the handler path, not at module top level, so
    ``import factlog`` stays light for a user who never installed the ``pubmed``
    extra (lazy-import discipline).
    """
    from pathlib import Path

    from factlog.integrations.pubmed.config import PubMedConfigError, load_config

    porcelain = getattr(args, "porcelain", False)
    target_str, source = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if source in ("config", "cwd") and not porcelain:
        print(f"factlog {command}: target KB {target} (from {source})")
    if not _require_kb(target, command):
        return None
    try:
        config = load_config(kb_root=target)
    except PubMedConfigError as exc:
        print(f"factlog {command}: {exc}", file=sys.stderr)
        return None
    # NCBI expects a contact email echoed into every request; an import run must
    # not be anonymous. Completeness is checked here, at the one per-run choke
    # point, rather than in the pure settings reader.
    if not config.email:
        print(
            f"factlog {command}: no NCBI contact email configured. Set client.email in "
            "~/.config/factlog/pubmed.toml (or the KB's policy/pubmed-config.toml); NCBI "
            "throttles or blocks unidentified traffic.",
            file=sys.stderr,
        )
        return None
    return target, config


def _pubmed_retraction_warnings(report, works) -> list[str]:
    """One stderr line per *imported/merged* retracted paper, naming the signal.

    Retraction is PubMed's own signal, not an absorbed fact: the note says so and
    points at the notice PMID when one is linkable. Skipped and errored records
    carry no such warning. Warnings go to stderr only, never the ``--porcelain``
    contract.
    """
    by_pmid = {w.pmid: w for w in works}
    lines = []
    for outcome in report.outcomes:
        if outcome.status not in ("imported", "merged"):
            continue
        work = by_pmid.get(outcome.key)
        if work is not None and work.retracted:
            notice = (
                f" See the retraction notice (PMID {work.retraction_notice_pmid})."
                if work.retraction_notice_pmid
                else ""
            )
            lines.append(
                f"⚠ PubMed reports {outcome.key} as retracted. This is an unverified "
                f"signal that flags the paper for human review before any claim from it "
                f"is trusted.{notice}"
            )
    return lines


def _pubmed_finish(report, target, *, dry_run: bool, porcelain: bool, warnings) -> int:
    """Emit the shared summary for a PubMed import and return the exit code.

    Mirrors :func:`_arxiv_finish`'s porcelain contract exactly. Every
    caller-influenced field in a porcelain row (the PMID, which comes from the
    efetch XML, and the would-be/existing filename) is passed through
    :func:`porcelain_field` so a stray tab/newline can never split a row (#141).

    That sentence read "every" before the ``target`` row below was gated, while the
    row printed a path built from the user's ``--target`` — a POSIX filename may hold
    a tab outright, and one did, measured (#416). A docstring claiming a gate is worse
    than no docstring: it is the ungated path a reader mistakes for a checked one.
    """
    from factlog.integrations.common.porcelain import porcelain_field

    if porcelain:
        # Stable machine contract, tab-separated, LF-terminated. Order-independent
        # (parse by first field):
        #   imported\t<n> / skipped\t<n> / merged\t<n> / errors\t<n>
        #   dry_run\t<0|1> / target\t<abs sources dir>
        # In --dry-run only, a per-record row precedes them:
        #   work\t<status>\t<pmid>\t<would-be or existing filename>
        if dry_run:
            for outcome in report.outcomes:
                name = outcome.path.name if outcome.path is not None else ""
                print(
                    f"work\t{outcome.status}\t{porcelain_field(outcome.key)}\t"
                    f"{porcelain_field(name)}"
                )
        print(f"imported\t{report.imported}")
        print(f"skipped\t{report.skipped}")
        print(f"merged\t{report.merged}")
        print(f"errors\t{report.errors}")
        print(f"dry_run\t{'1' if dry_run else '0'}")
        print(f"target\t{porcelain_field(str(target / 'sources'))}")
        for line in _candidate_porcelain_lines(report):
            print(line)
        for warning in warnings:
            print(warning, file=sys.stderr)
        for note in _candidate_notes(report):
            print(note, file=sys.stderr)
        return 1 if report.errors else 0

    print(f"\n{'Would import to' if dry_run else 'Importing to'} KB: {target}\n")
    for line in _openalex_report_lines(report, dry_run):
        print(line)
    print("\nSummary:")
    print(f"  {'Would import' if dry_run else 'Imported'}: {report.imported}")
    print(f"  {'Would skip' if dry_run else 'Skipped'}:  {report.skipped}")
    print(f"  {'Would merge' if dry_run else 'Merged'}:   {report.merged}")
    print(f"  Errors:   {report.errors}")
    for warning in warnings:
        print(f"\n{warning}", file=sys.stderr)
    for note in _candidate_notes(report):
        print(f"\n{note}", file=sys.stderr)
    if report.imported and not dry_run:
        print("\nNext step: run '/factlog sync' to extract candidate facts.")
    return 1 if report.errors else 0


def cmd_pubmed_import(args: argparse.Namespace) -> int:
    """Import PubMed records by PMID into sources/. Free; up to 200 ids per run."""
    from datetime import datetime, timezone

    from factlog.integrations.pubmed.client import (
        PubMedConnectionError,
        PubMedError,
        normalize_pmid,
    )
    from factlog.integrations.pubmed.importer import import_outcome
    from factlog.integrations.pubmed.work_parser import (
        PubMedParseError,
        parse_efetch_response,
    )

    prepared = _pubmed_prepare(args, "pubmed-import")
    if prepared is None:
        return 1
    target, config = prepared

    porcelain = getattr(args, "porcelain", False)
    dry_run = getattr(args, "dry_run", False)

    # Normalize each PMID individually: a syntactically bad id is a per-id error,
    # not a batch failure. Only a transport/parse failure (below) is fatal.
    valid, invalid = [], []
    for raw in args.pmid:
        try:
            valid.append(normalize_pmid(raw))
        except PubMedError as exc:
            invalid.append((str(raw), str(exc)))

    outcome = None
    if valid:
        client = _make_pubmed_client(config)
        try:
            # The client owns the inter-request delay (single-flight rate limiter);
            # a batch efetch is one request. dry-run still hits the network: the
            # title, retraction signal and slug are all needed to predict the
            # outcome. It writes no files.
            xml = client.efetch(valid)
        except PubMedConnectionError as exc:
            print(f"factlog pubmed-import: {exc}", file=sys.stderr)
            return 2
        except PubMedError as exc:
            # Service, request (bad id), >200-ids and other transport failures are
            # fatal for the whole request — the batch cannot be trusted partial.
            print(f"factlog pubmed-import: {exc}", file=sys.stderr)
            return 1
        try:
            outcome = parse_efetch_response(xml, valid)
        except PubMedParseError as exc:
            print(f"factlog pubmed-import: {exc}", file=sys.stderr)
            return 1

    imported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    report = import_outcome(
        outcome, invalid,
        target=target, config=config, imported_at=imported_at, dry_run=dry_run,
    )
    works = list(outcome.works) if outcome is not None else []
    warnings = _pubmed_retraction_warnings(report, works)
    return _pubmed_finish(
        report, target, dry_run=dry_run, porcelain=porcelain, warnings=warnings
    )


def cmd_pubmed_refresh(args: argparse.Namespace) -> int:
    """Has any PubMed record in the KB gained (or lost) a retraction, or an identifier/journal
    correction, since import? (#168, #169).

    Reads the provenance ledgers and a KB-level check-log, re-fetches each record by PMID
    (``efetch``), re-runs the two-marker retraction detector, and reports records whose
    ``doi``/``journal`` or retraction status has drifted from what PubMed now serves.

    Without ``--auto-update`` this is **report-only**: it never writes to sources/ or a
    ledger; only the check-log's last-checked timestamps advance. With ``--auto-update`` it
    records the narrow identifier/journal fields (``doi``, ``journal`` — the enumeration in
    ``refresh.AUTO_UPDATE_FIELDS``) into each changed record's provenance ledger, and nothing
    else: it never opens the original ``.md`` (P4 holds byte- and mtime_ns-identical), never
    rewrites any other ledger field, and **never writes retraction** — a newly-reported
    retraction is surfaced for a human to act on under both modes and nothing is absorbed
    (recording an acknowledged status is ``pubmed-acknowledge-retraction``; following a
    merged/deleted PMID is #170). A front-matter-only paper (no PubMed ledger) is still read,
    and a fresh retraction — or an auto-update — on it points at ``pubmed-backfill-provenance``
    (#172), not a refusal.

    Before spending the (rate-limited) requests, it prints an estimate of how long the run
    will take and — when no NCBI API key is configured — what it would cost with one, then
    asks for confirmation on an interactive terminal. ``--dry-run`` shows that plan and the
    ETA without touching the network or writing anything.
    """
    from datetime import datetime, timezone

    from factlog.integrations.common.porcelain import porcelain_field
    from factlog.integrations.pubmed import refresh as rf
    from factlog.integrations.pubmed.client import (
        PubMedClient,
        PubMedConnectionError,
        PubMedError,
        PubMedServiceError,
    )
    from factlog.integrations.pubmed.check_log import (
        CheckLogError,
        check_log_path,
        read_check_log,
        record_check,
        write_check_log,
    )

    prepared = _pubmed_prepare(args, "pubmed-refresh")
    if prepared is None:
        return 1
    target, config = prepared

    # A refresh hits the network, so NCBI's contact-email policy applies exactly as it does
    # to an import: unidentified traffic is throttled/blocked. Checked here at the one
    # per-run choke point (the shared prepare intentionally stays a pure resolver).
    if not config.email:
        print(
            "factlog pubmed-refresh: no NCBI contact email configured. Set client.email in "
            "~/.config/factlog/pubmed.toml (or the KB's policy/pubmed-config.toml); NCBI "
            "throttles or blocks unidentified traffic.",
            file=sys.stderr,
        )
        return 1

    porcelain = getattr(args, "porcelain", False)
    dry_run = getattr(args, "dry_run", False)
    only_flagged = getattr(args, "only_flagged", False)
    auto_update = getattr(args, "auto_update", False)
    older_than_days = args.older_than

    # A corrupt check-log is one KB-level file; surface it as a clear failure rather than a
    # traceback, and never as an empty log (which the next write would persist).
    log_path = check_log_path(target)
    try:
        check_log = read_check_log(log_path)
    except CheckLogError as exc:
        print(f"factlog pubmed-refresh: {exc}", file=sys.stderr)
        return 1

    # A corrupt *ledger* is one source's problem (a per-id error), never a crash.
    entries, ledger_errors = rf.collect_ledger_entries(target)
    # A record named by a source outside the provenance root can never be refreshed (#112);
    # reported per-id rather than dropped from the denominator.
    excluded = rf.excluded_checks(target)
    if only_flagged:
        entries = rf.flagged_only(entries)
    if not entries and not ledger_errors and not excluded:
        if not porcelain:
            scope = " flagged" if only_flagged else ""
            print(f"factlog pubmed-refresh: no{scope} PubMed records in {target}")
        else:
            for line in rf.porcelain_lines([], [], rf.summarize([], []), target=target):
                print(line)
        return 0

    now = datetime.now(timezone.utc).replace(microsecond=0)
    to_check, skipped = rf.partition_by_freshness(entries, check_log, older_than_days, now)

    # The pre-wait estimate (§1.3), derived from the client's real cadence — no rate
    # constant is copied. Shown even in --dry-run so an operator can size the real run.
    for line in rf.estimate_lines(
        len(to_check),
        interval=PubMedClient.min_interval(has_api_key=bool(config.api_key)),
        keyed_interval=PubMedClient.min_interval(has_api_key=True),
        has_key=bool(config.api_key),
    ):
        print(line, file=sys.stderr)

    if dry_run:
        # A preview: no network, no writes (not even the check-log). It reports the plan so
        # the ETA above is actionable, then stops.
        if porcelain:
            for entry in to_check:
                print(f"would-check\t{porcelain_field(entry.pmid)}")
            for check in skipped:
                print(f"skipped\t{porcelain_field(check.pmid)}")
            print(f"would_check\t{len(to_check)}")
            print(f"skipped\t{len(skipped)}")
            print("dry_run\t1")
            # Gated on the same terms as the two rows above it: the path comes from
            # the user's --target and a POSIX filename may hold a tab (#416, measured
            # — a tab-carrying --target reached this row and added a column).
            print(f"target\t{porcelain_field(str(target))}")
        else:
            print(
                f"\nDry run: would refresh retraction status for {len(to_check)} PubMed "
                f"record(s); {len(skipped)} skipped as checked within the last "
                f"{rf._days(older_than_days)}. Nothing was fetched or written."
            )
        return 0

    results: list = []
    if to_check:
        # Confirm on an interactive terminal before spending the requests; a non-interactive
        # or --porcelain run proceeds (this command writes nothing under sources/, so the
        # prompt is a courtesy, not a safety gate).
        if not porcelain and sys.stdin.isatty():
            answer = input("Proceed? [Y/n] ").strip().lower()
            if answer in ("n", "no"):
                print(
                    "factlog pubmed-refresh: aborted; nothing was checked or written.",
                    file=sys.stderr,
                )
                return 0

        client = _make_pubmed_client(config)

        def _progress(done: int, total: int) -> None:
            print(f"  checked {done}/{total}", file=sys.stderr)

        try:
            results = rf.check_entries(to_check, client, progress=_progress)
        except (PubMedConnectionError, PubMedServiceError) as exc:
            print(f"factlog pubmed-refresh: {exc}", file=sys.stderr)
            return 2
        except PubMedError as exc:
            # A transport/service failure cannot be trusted partial; the check-log is left
            # untouched so a re-run starts clean.
            print(f"factlog pubmed-refresh: {exc}", file=sys.stderr)
            return 1

    # Record what was actually observed this run: only a record whose state was confirmed
    # under the requested PMID gets a fresh timestamp. A per-id error, a merged PMID, and a
    # deleted PMID (#170) are deliberately left unadvanced so they keep surfacing every run
    # until a human acts — the same "never silently drop" contract a retraction has. Nothing
    # under sources/ is touched — the check-log is the only thing report-only ever writes.
    now_iso = now.isoformat()
    recorded_any = False
    for result in results:
        if result.status in (rf.STATUS_UNCHANGED, rf.STATUS_CHANGED):
            record_check(check_log, result.pmid, now_iso)
            recorded_any = True
    if recorded_any:
        write_check_log(log_path, check_log)

    # --auto-update writes only doi/journal into each changed record's ledger. It never
    # opens a source .md; a record whose fields already match is a byte-identical no-op; a
    # front-matter-only record has no ledger and is reported, not fabricated; a corrupt
    # ledger is a per-id error. A retraction is surfaced under both modes and is never
    # written. Only `results` (records actually checked this run) are eligible.
    updates = rf.apply_auto_update(results, target) if auto_update else []

    all_results = results + ledger_errors + excluded
    summary = rf.summarize(all_results, skipped)
    if porcelain:
        for line in rf.porcelain_lines(
            all_results, skipped, summary, target=target, updates=updates
        ):
            print(line)
    else:
        for line in rf.report_lines(
            all_results, skipped, summary, target=target,
            older_than_days=older_than_days, updates=updates,
        ):
            print(line)
    update_errors = any(u.status == rf.UPDATE_ERROR for u in updates)
    # A merged or deleted PMID (#170) reaches the exit code like a per-id error (#112's
    # principle): the KB holds a PMID PubMed no longer serves under, so its state could not
    # be confirmed and a human must act. A command returning 0 while that is true is the
    # silent direction a script keying only on the exit status would misread as healthy.
    return 1 if summary.errors or update_errors or summary.merged or summary.deleted else 0


def cmd_pubmed_acknowledge_retraction(args: argparse.Namespace) -> int:
    """Record a human's decision about one PubMed record's retraction signal (#171).

    The PubMed sibling of ``arxiv-acknowledge-withdrawal`` / ``openalex-acknowledge-
    retraction`` on the shared acknowledge primitive, in PubMed's own vocabulary — its own
    identity command, never a unified verb. A ``pubmed-refresh`` surfaces a retraction that
    appears between imports on every run until a human records it; this is the verb that
    records it — the same human gate (P1) as ``accept`` / ``reject``: one explicit ``--id``,
    never a sweep, no ``--all`` and no wildcard. Running it *is* the decision. Retraction is
    a **source-scoped signal**, not a fact: this writes the ledger's ``retracted`` field
    under a ``pubmed`` record, never the merged top-level ``retracted:`` claim (§6.4/§7.2).

    A **live** efetch of the one PMID is mandatory: the value to write lives only upstream,
    and an acknowledgement from a stale cache would be a lie (PubMed may already have
    reversed the retraction). On a connection failure or a missing/merged/deleted record:
    non-zero exit, nothing written.

    Two lessons are load-bearing here:

    * **#107 — verify before the request.** The TTY/``--yes`` gate, the presence of a
      writable ledger, and the readability of every ledger are all checked *before* the
      efetch, via the read-only :func:`acknowledge.lookup`. A paper with no ``pubmed``
      ledger record (imported before the ledger existed, or never imported) is refused for
      **zero** requests and pointed at ``pubmed-backfill-provenance``; ``acknowledge()``
      never fabricates a ledger. An unreadable ledger is refused rather than have its
      recorded value asserted from an incomplete view.
    * **#106 — ``--yes`` may record a retraction, never clear one.** Setting ``retracted``
      is the loud direction; clearing it (writing ``None`` when the live record no longer
      reads as retracted) is the silencing direction, and a "no longer retracted" reading
      can also be a curation lag or a marker PubMed has not yet emitted — the code cannot
      tell an honest reversal from a miss. Under ``--yes`` no human sees the note, so a
      clear is refused: re-run in a terminal and confirm at the prompt. A reversed
      retraction is a human's call, never a silent un-retraction.

    It never opens the ``.md`` (P4): after acknowledgement the ledger is the sole audit
    record; the source page is byte- and ``mtime_ns``-identical.
    """
    from factlog.integrations.common.acknowledge import (
        ACK_ERROR,
        ACK_UNCHANGED,
        ACK_WRITTEN,
        AcknowledgeSchema,
        acknowledge,
        lookup,
    )
    from factlog.integrations.common.provenance import (
        backfill_remedy,
        excluded_reason,
        excluded_sources_by_id,
    )
    from factlog.integrations.pubmed.client import (
        PubMedConnectionError,
        PubMedError,
        normalize_pmid,
    )
    from factlog.integrations.pubmed.source_writer import retraction_warning
    from factlog.integrations.pubmed.work_parser import (
        PubMedParseError,
        parse_efetch_response,
    )

    command = "pubmed-acknowledge-retraction"
    backfill = "pubmed-backfill-provenance"

    # Identity is the bare PMID. `normalize_pmid` accepts a `pmid:` prefix and rejects
    # zero-padding — the same validation the import path uses, so the id keyed here matches
    # the ledger's. A single id: no --all, no wildcard.
    try:
        pmid = normalize_pmid(args.id)
    except PubMedError as exc:
        print(f"factlog {command}: {exc}", file=sys.stderr)
        return 1

    prepared = _pubmed_prepare(args, command)
    if prepared is None:
        return 1
    target, config = prepared

    schema = AcknowledgeSchema(type="pubmed", field="retracted")

    # A command that cannot ask must not guess. Without a terminal — a pipe, a script —
    # there is no one to confirm to, and silencing a signal is exactly the write that must
    # never be guessed. `--yes` (only ever paired with the required, explicit `--id`) is the
    # deliberate act that stands in for the prompt; without it a non-interactive run refuses
    # and writes nothing. This refuses BEFORE any request is spent.
    assume_yes = getattr(args, "yes", False)
    if not assume_yes and not sys.stdin.isatty():
        print(
            f"factlog {command}: refusing to acknowledge without a terminal to confirm "
            "at. This silences PubMed's retraction signal for one id. Re-run in a "
            "terminal, or pass --yes with --id to confirm non-interactively. Nothing "
            "written.",
            file=sys.stderr,
        )
        return 1

    # Resolve ledger presence BEFORE the live query (#107), so a paper this command cannot
    # write is refused for ZERO efetch requests and with no prompt. `lookup` reads only
    # source-provenance/ sidecars; nothing under sources/ is opened for writing (P4).
    found = lookup(target, pmid, schema)

    # An unreadable ledger might be the one that carries this id — its `retracted` value
    # cannot be read, so the recorded value is unknown. Refuse rather than assert "the
    # ledger did not record" on an incomplete view, and do it before spending a request.
    if found.unreadable:
        bad = ", ".join(found.unreadable)
        print(
            f"factlog {command}: cannot read every provenance ledger ({bad}); one of them "
            "may carry this id, so its recorded value is unknown. Repair or remove the "
            "unreadable ledger(s) and retry. No request was made; nothing written.",
            file=sys.stderr,
        )
        return 1

    # `acknowledge()` writes only provenance sidecars and never fabricates a ledger. A
    # paper with no `pubmed` record — imported before its ledger existed, or absent from the
    # KB — has nothing to write, so querying PubMed first would burn a request and the
    # operator's attention on a warning no human can turn off here. Refuse before the fetch
    # and name the command that builds the ledger (never this one; that is an import's write).
    if not found.found:
        excluded = excluded_sources_by_id(target, "pmid").get(pmid, ())
        if excluded:
            reason = (
                f"{pmid!r} is named by {', '.join(excluded)}, which "
                f"{'is' if len(excluded) == 1 else 'are'} outside the provenance root, so "
                "no ledger can record a decision about it. "
                + excluded_reason(", ".join(excluded), backfill_remedy(backfill))
            )
        else:
            reason = (
                f"no PubMed provenance ledger carries id {pmid!r}. If the paper was "
                f"imported before its ledger was written, run `factlog {backfill}` to "
                "create one from its front matter, then acknowledge; if it was never "
                "imported, import it first."
            )
        print(f"factlog {command}: {reason} No request was made; nothing written.",
              file=sys.stderr)
        return 1

    # The recorded value comes from the ledger we just confirmed. Import writes
    # `retracted: True` only when PubMed flagged a retraction (absent means not-retracted),
    # so a truthy value in any covering ledger means the retraction is already recorded.
    recorded_retracted = any(value is True for value in found.values)

    # The live query is mandatory: the value to write lives only upstream.
    client = _make_pubmed_client(config)
    try:
        xml = client.efetch([pmid])
    except PubMedConnectionError as exc:
        # Cannot reach PubMed: an acknowledgement from a stale cache would be a lie.
        print(f"factlog {command}: {exc} Nothing written.", file=sys.stderr)
        return 2
    except PubMedError as exc:
        print(f"factlog {command}: {exc} Nothing written.", file=sys.stderr)
        return 1
    try:
        outcome = parse_efetch_response(xml, [pmid])
    except PubMedParseError as exc:
        print(f"factlog {command}: {exc} Nothing written.", file=sys.stderr)
        return 1

    present = next((r for r in outcome.present if r.requested_pmid == pmid), None)
    if present is None:
        # efetch returns by omission (deleted) or under a different id (merged); either way
        # there is no live record for THIS pmid to acknowledge against.
        if outcome.merged:
            returned = outcome.merged[0].returned_pmid
            reason = (
                f"PubMed answered under a different PMID {returned!r} (the record was "
                f"merged upstream). Refusing to acknowledge under {pmid!r}; re-import to "
                "follow the new id."
            )
        elif pmid in outcome.deleted:
            reason = f"PubMed has no record for {pmid!r} (deleted or withdrawn from the index)."
        else:
            reason = (
                f"PubMed returned no record for {pmid!r} (nonexistent id, or a transient "
                "empty response)."
            )
        print(f"factlog {command}: {reason} Nothing written.", file=sys.stderr)
        return 1

    work = present.work
    # PubMed's live opinion — False when the record is not (or no longer) retracted.
    current_retracted = bool(work.retracted)

    # Nothing to acknowledge when the ledger already matches PubMed. Say so and exit 0
    # without a note or a prompt — there is no divergence to record and no signal to silence.
    if current_retracted == recorded_retracted:
        if current_retracted:
            print(
                f"The ledger already records PubMed's retraction for {pmid}; nothing to "
                "acknowledge."
            )
        else:
            print(
                f"PubMed does not flag {pmid} as retracted and the ledger records no "
                "retraction; nothing to acknowledge."
            )
        return 0

    # #106: a CLEAR (recorded retracted, PubMed no longer reads as retracted) may not be
    # confirmed by `--yes`. Recording a retraction is the loud direction `--yes` may do;
    # clearing one is the silencing direction, and a "no longer retracted" reading can be a
    # curation lag or a marker not yet emitted, not a genuine reversal. Under `--yes` no
    # human sees the note, so the clear is refused. The interactive path prints the note
    # first so a human can catch exactly that. This is knowable only after the fetch.
    is_clear = recorded_retracted and not current_retracted
    if assume_yes and is_clear:
        print(
            f"factlog {command}: refusing to clear the retraction recorded for {pmid} with "
            "--yes. PubMed no longer flags it as retracted, but that also happens when a "
            "retraction marker has not been emitted yet (curation lag) — the code cannot "
            "tell a genuine reversal from a miss, and --yes means no human sees the note. "
            "Clearing silences a recorded signal, so it needs a human: re-run in a terminal "
            "without --yes and confirm at the prompt. Nothing written.",
            file=sys.stderr,
        )
        return 1

    # Show the operator exactly what they are about to record (or clear).
    if current_retracted:
        print(retraction_warning(work.retraction_notice_pmid))
    else:
        print(
            f"PubMed no longer flags {pmid} as retracted, but the ledger records a "
            "retraction. Confirming clears it (removing the field), so pubmed-refresh stops "
            "repeating a retraction PubMed has reversed."
        )

    if not assume_yes:
        try:
            answer = input(
                f"\nRecord PubMed's live retraction status for {pmid} in the ledger? [y/N] "
            ).strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted; nothing written.")
            return 0

    # Write the live value: `True` records a retraction, `None` clears one (removing the
    # field — never a literal `False`, which would diverge from an import's
    # absent-means-not-retracted convention and change the JSON bytes).
    #
    # PubMed's retraction is a TWO-field signal (#202): `retracted` and the notice PMID that
    # links to *why*. The import ledger writes both (source_writer `_provenance_record`), so
    # acknowledge must manage both as one signal, or it writes a self-inconsistent record —
    # on a clear, an orphaned `retraction_notice_pmid` beside a dropped `retracted`; on a
    # record, a `retracted: True` with no audit link, diverging from the import ledger.
    # The companion rides the SAME atomic write as `retracted` (never a second uncoordinated
    # writer): on a record it carries the live notice PMID (`None` when the retraction has no
    # linkable notice, exactly as the import omits it); on a clear both drop together.
    value = True if current_retracted else None
    notice_pmid = work.retraction_notice_pmid if current_retracted else None
    result = acknowledge(
        target, pmid, value, schema, {"retraction_notice_pmid": notice_pmid}
    )

    if result.status == ACK_WRITTEN:
        if current_retracted:
            print(
                f"Recorded PubMed's retraction for {pmid} in {', '.join(result.ledgers)}."
            )
        else:
            print(
                f"Cleared the retraction recorded for {pmid} in "
                f"{', '.join(result.ledgers)}."
            )
        print(
            "pubmed-refresh will no longer repeat this signal. The ledger is the audit "
            "record; the source .md is not rewritten (P4)."
        )
        return 0
    if result.status == ACK_UNCHANGED:
        print(
            f"The ledger for {pmid} already holds PubMed's live retraction status; nothing "
            "changed. pubmed-refresh will not repeat this signal."
        )
        return 0
    if result.status == ACK_ERROR:
        print(f"factlog {command}: {result.reason}", file=sys.stderr)
        return 1
    # ACK_NO_LEDGER cannot occur — ledger presence was confirmed before the fetch — but is
    # handled defensively rather than mis-reported as success.
    print(f"factlog {command}: {result.reason or result.status}", file=sys.stderr)
    return 1


def _arxiv_show_results(works, total: int, *, porcelain: bool) -> None:
    """Render arxiv-search results. Mirrors :func:`_openalex_show_results`.

    The ``--porcelain`` line shape is identical to openalex-search's:
    ``result\t<index>\t<id>\t<flag>\t<title>`` then ``found\t<total>``. The id is
    the versioned arXiv id, and the flag is ``withdrawn`` or ``-`` — never
    ``retracted`` (arXiv has no retraction process; #57). Zero results is a
    legitimate answer for a search, so it prints a plain "0 results" rather than
    an error (contrast an id_list miss).
    """
    from factlog.integrations.arxiv.source_writer import withdrawal_agent
    from factlog.integrations.common.porcelain import porcelain_field

    if porcelain:
        # The agent that withdrew a paper is a prose warning, so it goes to
        # stderr, keeping the machine contract on stdout clean (see below).
        for index, work in enumerate(works, 1):
            flag = "withdrawn" if work.withdrawn else "-"
            # Id and title are upstream data: a tab adds a column and a line break
            # splits the row, either way a positional consumer reads the wrong
            # field (#406).
            print(f"result\t{index}\t{porcelain_field(work.versioned_id)}\t{flag}\t"
                  f"{porcelain_field(work.title or '')}")
        print(f"found\t{total}")
        return

    if total == 0:
        print("Found 0 results.")
        return

    print(f"Found {total} results, showing top {len(works)}:\n")
    for index, work in enumerate(works, 1):
        authors = work.authors[0] if work.authors else "anonymous"
        year = work.year or "n.d."
        category = work.primary_category or "?"
        print(f"  {index}. {work.versioned_id} \"{work.title or '(untitled)'}\" "
              f"({authors} {year}, {category})")
        if work.withdrawn:
            agent = withdrawal_agent(work.withdrawn_by)
            # Flagged in the listing, agent named; withdrawal is not retraction.
            print(f"      ⚠ arXiv reports this as WITHDRAWN (by {agent}); withdrawal "
                  "is not retraction, and this signal is unverified — confirm before "
                  "trusting any claim from it.")


def _arxiv_search_withdrawal_warnings(works) -> list[str]:
    """One stderr line per withdrawn result, naming the agent (never "retracted").

    In ``--porcelain`` mode the flag field alone cannot name the agent, so the
    naming lives here on stderr — warnings never pollute the machine contract.
    """
    from factlog.integrations.arxiv.source_writer import withdrawal_agent

    lines = []
    for work in works:
        if work.withdrawn:
            agent = withdrawal_agent(work.withdrawn_by)
            lines.append(
                f"⚠ arXiv reports {work.versioned_id} as withdrawn (by {agent}). "
                "Withdrawal is not retraction; this unverified signal flags the paper "
                "for human review before any claim from it is trusted."
            )
    return lines


def cmd_arxiv_search(args: argparse.Namespace) -> int:
    """Search arXiv, list the results, then import the ones chosen (#80, #81).

    Free (no credits, no key). Every filter arXiv would silently ignore —
    an unknown category, an unknown query field, a bare/reversed/out-of-range
    --year — is rejected before a request is spent, because arXiv answers all of
    them with 200 and zero results, which reads as "no such literature exists"
    (#57). Zero results is nonetheless a legitimate answer for a search, so an
    empty hit prints "0 results" and exits 0.

    Selection mirrors ``openalex-search`` exactly (the selector is shared): an
    interactive prompt on a TTY, or ``--all`` to take the whole set. Anything
    that cannot ask — ``--porcelain``, a pipe, no TTY — selects nothing rather
    than guessing. Chosen works go through the *same* ``import_works`` the
    single-id path uses, so a paper already in the KB via OpenAlex merges (#65)
    instead of writing a second file, duplicates and per-id errors are handled
    (#64), the withdrawal front matter is written (#60) and merge candidates are
    surfaced (#75). ``--dry-run`` keeps its #80 meaning: it previews the query
    that would be sent and imports nothing.
    """
    from datetime import datetime, timezone

    from factlog.integrations.arxiv.importer import import_works
    from factlog.integrations.common.porcelain import porcelain_field
    from factlog.integrations.arxiv.client import (
        ArxivConnectionError,
        ArxivError,
    )
    from factlog.integrations.arxiv.config import (
        ArxivValidationError,
        as_phrase,
        build_submitted_date,
        compose_search_query,
        validate_category,
        validate_search_query,
        validate_sort,
    )

    prepared = _arxiv_prepare(args, "arxiv-search")
    if prepared is None:
        return 1
    target, config = prepared

    porcelain = getattr(args, "porcelain", False)
    dry_run = getattr(args, "dry_run", False)
    categories = tuple(args.category or ())

    # --limit is factlog policy (max 200), not an API constraint; reject an
    # out-of-range value at the boundary, before the client is even built.
    if args.limit is not None and (args.limit < 1 or args.limit > config.max_limit):
        print(f"factlog arxiv-search: --limit must be between 1 and {config.max_limit}, "
              f"got {args.limit}", file=sys.stderr)
        return 1

    # Validate every filter up front so a typo never reaches the transport. The
    # client re-validates as defence in depth, but doing it here keeps the check
    # network-free and gives one consistent error surface.
    try:
        validate_search_query(args.query)
        # Also settles whether the query can be quoted safely: an unbalanced quote
        # or a backslash is refused here, not at the transport.
        phrased = as_phrase(args.query)
        for category in categories:
            validate_category(category)
        if args.sort:
            validate_sort(args.sort)
        if args.year:
            build_submitted_date(args.year)
    except ArxivValidationError as exc:
        print(f"factlog arxiv-search: {exc}", file=sys.stderr)
        return 1

    # We quoted a bare multi-word query so arXiv searches it as a phrase (#89).
    # Say so: silently rewriting what the operator typed is the same disservice as
    # silently mis-searching it. stderr, so --porcelain stdout stays parseable, and
    # before --dry-run so the notice accompanies the query it explains.
    if phrased != args.query.strip():
        print(
            f"factlog arxiv-search: searching the phrase {phrased} "
            "(a bare multi-word query is not searched as a phrase by arXiv; "
            "supply your own field prefix or quotes to override, and widen it that "
            "way if the phrase returns fewer results than you expect)",
            file=sys.stderr,
        )

    # --dry-run shows the query that WOULD be sent and spends no request. The
    # string comes from the same composer the client uses, so it cannot drift from
    # what a real run sends — and it makes arXiv's own reading of the query
    # visible, which matters because a bare multi-word phrase is not searched as a
    # phrase (#89).
    # --show-query spends no request; it prints the exact `search_query` that would
    # be sent. It used to be `--dry-run`, back when this command imported nothing
    # (#80). Now that it does, `--dry-run` must mean what it means everywhere else
    # in factlog and in `openalex-search`: do the work, write nothing. Two sibling
    # commands whose identical `--dry-run` help hid different behaviour is the trap
    # this splits (#81).
    if args.show_query:
        composed = compose_search_query(args.query, categories, args.year)
        if porcelain:
            # The most caller-influenced value on any porcelain row: it is the user's
            # own --query, so an arbitrary tab or line break goes in by hand rather
            # than arriving from upstream (#416, measured — `--query $'a\tb'` emitted
            # three columns where the contract says two). Only the porcelain branch
            # is gated; the human branch below prints prose, whose shape carries no
            # meaning a break could destroy.
            print(f"query\t{porcelain_field(composed)}")
        else:
            print("Would search arXiv (no request sent):")
            print(f"  search_query: {composed}")
            print(f"  max_results:  {args.limit or config.default_limit}")
            if args.sort:
                print(f"  sortBy:       {validate_sort(args.sort)}")
        return 0

    client = _make_arxiv_client(config)
    if not porcelain:
        print(f'Searching arXiv: "{args.query}"...')
    try:
        works, total = client.search(
            args.query, categories=categories, year=args.year,
            limit=args.limit, sort=args.sort,
        )
    except ArxivConnectionError as exc:
        print(f"factlog arxiv-search: {exc}", file=sys.stderr)
        return 2
    except (ArxivError, ArxivValidationError) as exc:
        print(f"factlog arxiv-search: {exc}", file=sys.stderr)
        return 1

    _arxiv_show_results(works, total, porcelain=porcelain)
    # Flag every withdrawn result *before* the user selects it. In --porcelain the
    # withdrawn flag is a stdout field but its agent is prose, so the naming goes
    # to stderr; in human mode the listing already named it inline.
    if porcelain:
        for warning in _arxiv_search_withdrawal_warnings(works):
            print(warning, file=sys.stderr)

    # Selection, then import through the single-id path's importer. --all takes the
    # whole set (the explicit opt-in a non-interactive run needs); otherwise the
    # shared selector prompts on a TTY and selects nothing when it cannot ask.
    if args.all:
        chosen = works
    else:
        interactive = not porcelain and not dry_run and sys.stdin.isatty()
        chosen = _select_search_results(
            works, interactive=interactive, command="arxiv-search"
        )
    if not chosen:
        if not porcelain:
            hint = " Re-run with --all to import every result." if works else ""
            print(f"\nNothing selected; no files written.{hint}")
        return 0

    imported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    report = import_works(
        chosen, target=target, config=config, imported_at=imported_at, dry_run=dry_run
    )
    # Warn on stderr for each *imported* withdrawn paper, naming the agent — the
    # same contract arxiv-import honours. The pre-selection listing above already
    # flagged all withdrawn results; this fires only for the ones actually taken.
    warnings = _arxiv_withdrawal_warnings(report, works)
    return _arxiv_finish(
        report, target, dry_run=dry_run, porcelain=porcelain, warnings=warnings
    )


def _pubmed_show_results(works, count: int, *, porcelain: bool,
                         query_translation: str | None = None) -> None:
    """Render pubmed-search results. Mirrors :func:`_arxiv_show_results`.

    The ``--porcelain`` line shape matches the sibling search commands:
    ``result\t<index>\t<pmid>\t<flag>\t<title>`` then ``found\t<count>``. The flag
    is ``retracted`` (PubMed's own dual-marker signal, #164) or ``-``. Zero results
    is a legitimate answer, so it prints a plain "0 results"; whether that zero is
    *suspicious* is surfaced separately on stderr by the silent-zero guard.
    """
    from factlog.integrations.common.porcelain import porcelain_field

    if porcelain:
        for index, work in enumerate(works, 1):
            flag = "retracted" if work.retracted else "-"
            # Pmid and title are upstream data: a tab adds a column and a line break
            # splits the row, either way a positional consumer reads the wrong
            # field (#406).
            print(f"result\t{index}\t{porcelain_field(work.pmid)}\t{flag}\t"
                  f"{porcelain_field(work.title or '')}")
        print(f"found\t{count}")
        return

    if count == 0:
        print("Found 0 results.")
        # A zero is exactly where "how did PubMed read my words" matters most: the
        # QueryTranslation shows whether ATM widened, narrowed, or dropped the query
        # that returned nothing. Surfacing it only on non-empty results would hide
        # the one diagnostic that explains an honest zero — so it rides the zero path
        # too, keeping the module's "a real search surfaces QueryTranslation" promise.
        _pubmed_show_query_translation(query_translation)
        return

    print(f"Found {count} results, showing top {len(works)}:\n")
    for index, work in enumerate(works, 1):
        authors = work.authors[0] if work.authors else "anonymous"
        year = work.year or "n.d."
        journal = work.journal or "?"
        print(f"  {index}. PMID {work.pmid} \"{work.title or '(untitled)'}\" "
              f"({authors} {year}, {journal})")
        if work.retracted:
            # PubMed flags retraction with two co-occurring markers (spike §3); it
            # is a source signal, not a human-accepted claim (§6.4). Name it, and
            # never conflate it with a mere correction.
            print("      ⚠ PubMed reports this as RETRACTED; this signal is unverified "
                  "— confirm before trusting any claim from it.")
    _pubmed_show_query_translation(query_translation)


def _pubmed_show_query_translation(query_translation: str | None) -> None:
    """Print PubMed's own reading of the query, when it volunteered one.

    Makes PubMed's Automatic Term Mapping visible rather than rewriting the
    operator's words (#89's answer for PubMed). It rides stdout in human mode —
    stderr would hide it from a scrolled-back human — and is shown at any count,
    zero included, because a zero is where "how did PubMed read this" matters most.
    """
    if query_translation is not None:
        print(f"\nPubMed read the query as: {query_translation}")


def _pubmed_search_retraction_warnings(works) -> list[str]:
    """One stderr line per retracted result, for --porcelain where prose can't ride stdout."""
    lines = []
    for work in works:
        if work.retracted:
            lines.append(
                f"⚠ PubMed reports PMID {work.pmid} as retracted (unverified). "
                "Confirm before trusting any claim from it."
            )
    return lines


def cmd_pubmed_search(args: argparse.Namespace) -> int:
    """Search PubMed, list the results, and surface a suspicious zero (#167).

    Free (a key only raises the rate limit). It searches, applies the silent-zero
    guard, lists the results, and imports the chosen ones through #166's shared
    import path (:func:`_pubmed_import_selected` -> ``import_outcome``) — the same
    ``sources/`` writer, candidate boundary and cross-source merge ``pubmed-import``
    uses, never a second writer. Selection mirrors ``arxiv-search``: a TTY prompt, or
    ``--all`` to take the whole set; a run that cannot ask selects nothing.

    The silent-zero guard is the point (spec, #167). ``esearch`` answers a
    malformed field tag or a nonexistent MeSH term with HTTP 200 and zero results,
    indistinguishable from an honest empty set on the count alone. So an unknown
    ``[field tag]`` is rejected before a request is spent; PubMed's own
    ``ErrorList``/``WarningList`` (``PhraseNotFound`` / ``QuotedPhraseNotFound`` /
    ``FieldNotFound``) is surfaced verbatim; and a *filtered* zero is surfaced with
    the ``--year``/``--mesh`` filters named — see
    :mod:`factlog.integrations.pubmed.search`. The same surfacing principle covers a
    result whose *recorded* year falls outside ``--year``: PubMed's date filter also
    matches an electronic publication date, so a Print-Electronic paper is a genuine
    hit that lands with a later issue year, and ``year_range_report`` names it rather
    than letting it appear in the KB unannounced (#387) — as it does a result that
    will be recorded with no year at all, which ``--year`` can check against even
    less (#389). ``--show-query`` prints the composed
    ``term`` and sends nothing; ``--dry-run`` sends the search and declines to write
    (they are different, per the issue).
    """
    from factlog.integrations.common.porcelain import porcelain_field
    from factlog.integrations.pubmed.client import (
        PubMedConnectionError,
        PubMedError,
    )
    from factlog.integrations.pubmed.search import (
        DEFAULT_LIMIT,
        MAX_LIMIT,
        PubMedSearchValidationError,
        build_year_filter,
        compose_query,
        mesh_clause,
        parse_esearch,
        silent_zero_report,
        validate_field_tags,
        year_range_report,
    )
    from factlog.integrations.pubmed.work_parser import (
        PubMedParseError,
        parse_efetch_response,
    )

    porcelain = getattr(args, "porcelain", False)
    dry_run = getattr(args, "dry_run", False)
    mesh = tuple(args.mesh or ())
    limit = args.limit if args.limit is not None else DEFAULT_LIMIT

    # --limit is factlog policy; reject an out-of-range value before anything else.
    if args.limit is not None and (args.limit < 1 or args.limit > MAX_LIMIT):
        print(f"factlog pubmed-search: --limit must be between 1 and {MAX_LIMIT}, "
              f"got {args.limit}", file=sys.stderr)
        return 1

    # Validate every filter up front so a typo never reaches the transport: an
    # unknown field tag, a quote-broken mesh term, a reversed/out-of-range year are
    # all silences PubMed would answer with a zero, so they are refused here. This is
    # pure (network-free), so it runs before `_pubmed_prepare` — a query typo is told
    # to the operator without first demanding a KB or a contact email.
    try:
        validate_field_tags(args.query)
        for term in mesh:
            mesh_clause(term)
        if args.year:
            build_year_filter(args.year)
        composed = compose_query(args.query, year=args.year, mesh=mesh)
    except PubMedSearchValidationError as exc:
        print(f"factlog pubmed-search: {exc}", file=sys.stderr)
        return 1

    # --show-query prints the exact term that WOULD be sent and spends no request.
    # It is NOT --dry-run: --dry-run sends the search and declines to write. Because
    # it sends nothing, it needs nothing — no KB, no NCBI email — so it returns here,
    # before `_pubmed_prepare` (which requires both for a request-spending run).
    if args.show_query:
        if porcelain:
            # Same gate, same reason as `arxiv-search`'s query row (#416): the value
            # is the user's own --query, and both rows were measured emitting three
            # columns for a tab-carrying one.
            print(f"query\t{porcelain_field(composed)}")
        else:
            print("Would search PubMed (no request sent):")
            print(f"  term:   {composed}")
            print(f"  retmax: {limit}")
        return 0

    # From here on the command spends requests, so it now demands the KB and the NCBI
    # contact email `_pubmed_prepare` enforces (the same gate `pubmed-import` uses).
    prepared = _pubmed_prepare(args, "pubmed-search")
    if prepared is None:
        return 1
    target, config = prepared

    client = _make_pubmed_client(config)
    if not porcelain:
        print(f'Searching PubMed: "{args.query}"...')
    try:
        raw = client.esearch(composed, retmax=limit)
    except PubMedConnectionError as exc:
        print(f"factlog pubmed-search: {exc}", file=sys.stderr)
        return 2
    except PubMedError as exc:
        print(f"factlog pubmed-search: {exc}", file=sys.stderr)
        return 1

    result = parse_esearch(raw)

    # Surface the silent-zero guard BEFORE the count, on stderr, so --porcelain
    # stdout stays parseable. This is what keeps a nonexistent-MeSH zero from
    # reading as an honest empty set (#167's whole point).
    # The raw `args.query`, not the composed term: the guard's one question about the
    # user's input is "did they quote?", and only the query they typed answers it (#272).
    # The CLI decides nothing here — it passes the fact down.
    for line in silent_zero_report(result, year=args.year, mesh=mesh, query=args.query):
        print(f"factlog pubmed-search: {line}", file=sys.stderr)

    # A whole-request rejection (bad db, unparseable body) leaves no trustworthy
    # count or ids; it was surfaced above, so stop with a non-zero exit.
    if result.top_level_error:
        return 1

    # Fetch the returned PMIDs so the listing carries titles/authors/year — reusing
    # #163's parser rather than re-reading the XML. esearch returns only ids+count.
    # The whole classified outcome is kept (not just `.works`) so a selection can be
    # imported through #166's `import_outcome` without a second efetch.
    outcome = None
    works: tuple = ()
    if result.ids:
        try:
            fetched = client.efetch(result.ids)
        except PubMedConnectionError as exc:
            print(f"factlog pubmed-search: {exc}", file=sys.stderr)
            return 2
        except PubMedError as exc:
            print(f"factlog pubmed-search: {exc}", file=sys.stderr)
            return 1
        try:
            outcome = parse_efetch_response(fetched, result.ids)
        except PubMedParseError as exc:
            print(f"factlog pubmed-search: {exc}", file=sys.stderr)
            return 1
        works = outcome.works

    _pubmed_show_results(works, result.count, porcelain=porcelain,
                         query_translation=result.query_translation)
    if porcelain:
        for warning in _pubmed_search_retraction_warnings(works):
            print(warning, file=sys.stderr)

    # A result whose recorded year falls outside --year (#387). PubMed's
    # [Date - Publication] filter also matches a record's electronic publication
    # date, while front matter carries the journal issue's year, so a
    # Print-Electronic paper legitimately matches --year 2022-2025 and lands as
    # `year: 2026` — or, when its PubDate carries no parseable year, with no `year`
    # at all (#389), which a --year search is no less entitled to hear about.
    # Surfaced, never blocked: the record is a real match and the
    # operator decides. On stderr in both modes — like the silent-zero guard — so
    # --porcelain stdout stays parseable, and before selection so the fact is
    # known while there is still a choice to make.
    for line in year_range_report(works, year=args.year):
        print(f"factlog pubmed-search: {line}", file=sys.stderr)

    # Selection, then import through #166's importer. A command that cannot ask must
    # not guess: --all is the explicit opt-in a non-interactive run needs, and a
    # search that silently imports nothing in CI is the same silent-zero failure
    # wearing another hat (#167). The write itself is the single seam below.
    if args.all:
        chosen = list(works)
    else:
        interactive = not porcelain and not dry_run and sys.stdin.isatty()
        chosen = _select_search_results(works, interactive=interactive, command="pubmed-search")
    if not chosen:
        if not porcelain and works:
            print("\nNothing selected; no files written. Re-run with --all to select "
                  "every result.")
        return 0
    return _pubmed_import_selected(chosen, outcome, target=target, config=config,
                                   dry_run=dry_run, porcelain=porcelain)


def _pubmed_import_selected(chosen, outcome, *, target, config, dry_run: bool, porcelain: bool) -> int:
    """Import the selected search results through #166's shared import path.

    The single seam where a ``pubmed-search`` selection becomes ``sources/`` files.
    It reuses :func:`~factlog.integrations.pubmed.importer.import_outcome` (and, under
    it, :class:`~factlog.integrations.pubmed.source_writer.PubMedSourceWriter`) rather
    than a second writer, so the candidate boundary (P1/P2), the ``--dry-run``
    no-write, the ``--porcelain`` contract and cross-source merge (§7.3) are all
    inherited exactly as ``pubmed-import`` gets them.

    The chosen works are a subset of the efetch ``outcome``; the outcome is filtered
    down to the selected PMIDs and handed to ``import_outcome`` — no second request.
    A selection can only ever be *present*/*merged* records (a deleted/unparseable id
    never appeared in the listing to be picked), so no per-id errors are synthesised
    here. ``--dry-run`` plans without writing; the report is summarised either way.
    """
    from datetime import datetime, timezone

    from factlog.integrations.pubmed.importer import import_outcome
    from factlog.integrations.pubmed.work_parser import PubMedFetchOutcome

    if outcome is None:  # defensive: an empty selection returns before this call
        return 0

    chosen_pmids = {work.pmid for work in chosen}
    selected = PubMedFetchOutcome(
        present=tuple(p for p in outcome.present if p.work.pmid in chosen_pmids),
        merged=tuple(m for m in outcome.merged if m.work.pmid in chosen_pmids),
    )

    imported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    report = import_outcome(
        selected, target=target, config=config, imported_at=imported_at, dry_run=dry_run,
    )
    warnings = _pubmed_retraction_warnings(report, list(chosen))
    return _pubmed_finish(
        report, target, dry_run=dry_run, porcelain=porcelain, warnings=warnings
    )


def cmd_pubmed_mesh(args: argparse.Namespace) -> int:
    """Propose canonical-alias *candidates* from a KB paper's PubMed MeSH (#173).

    Reads the PMID a source recorded in its provenance ledger, fetches that
    record's MeSH headings live, and proposes the descriptor names as candidate
    aliases split into *major* / *minor* topics. It writes nothing to the
    canonical vocabulary: a human (P1) decides which, if any, become aliases.

    The major/minor split is the point (#53/#165): OpenAlex's flat ``mesh_terms``
    drops qualifier-level majorness, so a pre-2010 paper's true major topic reads
    as minor there; PubMed's own feed keeps it, and an alias mined from a *minor*
    term is a bad alias. So a qualifier-only major descriptor is flagged as the
    place OpenAlex would disagree.

    Four outcomes, kept apart: a nonexistent slug is an *error* (exit 1); a slug
    whose ledger records no PMID is reported as that fact, with its reason, and is
    *not* an empty MeSH list (exit 0); a real PMID that carries no MeSH is reported
    as an unindexed record (exit 0); a PMID PubMed did not return a record for
    (deleted/merged) is a signal, reported on stderr (exit 1).
    """
    from factlog.integrations.pubmed.client import (
        PubMedConnectionError,
        PubMedError,
    )
    from factlog.integrations.pubmed.mesh_suggest import (
        MeshSuggestError,
        build_proposal,
        no_pmid_line,
        no_pmid_porcelain_line,
        proposal_lines,
        proposal_porcelain_lines,
        resolve_pmid,
    )
    from factlog.integrations.pubmed.work_parser import (
        PubMedParseError,
        parse_efetch_response,
    )

    prepared = _pubmed_prepare(args, "pubmed-mesh")
    if prepared is None:
        return 1
    target, config = prepared
    porcelain = getattr(args, "porcelain", False)

    # Resolve the PMID from the provenance ledger. A nonexistent slug (or a corrupt
    # ledger) is a user error and stops here; "no PMID" is a value reported below.
    try:
        resolution = resolve_pmid(target, args.for_slug)
    except MeshSuggestError as exc:
        print(f"factlog pubmed-mesh: {exc}", file=sys.stderr)
        return 1

    if resolution.pmid is None:
        # Reported as its own fact, with the reason, never as an empty MeSH list.
        if porcelain:
            print(no_pmid_porcelain_line(resolution.slug))
        else:
            print(no_pmid_line(resolution.slug))
        return 0

    client = _make_pubmed_client(config)
    try:
        # One efetch for the single PMID; the client owns the rate limiter.
        xml = client.efetch([resolution.pmid])
    except PubMedConnectionError as exc:
        print(f"factlog pubmed-mesh: {exc}", file=sys.stderr)
        return 2
    except PubMedError as exc:
        print(f"factlog pubmed-mesh: {exc}", file=sys.stderr)
        return 1
    try:
        outcome = parse_efetch_response(xml, [resolution.pmid])
    except PubMedParseError as exc:
        print(f"factlog pubmed-mesh: {exc}", file=sys.stderr)
        return 1

    # Find the returned record. A deleted PMID is an empty set (no present record);
    # a merged one comes back under a different PMID. Either way there is no MeSH to
    # read for the requested id — a signal, surfaced, not silently a zero-MeSH.
    work = next((w for w in outcome.works if w.pmid == resolution.pmid), None)
    if work is None:
        if outcome.merged:
            new_pmid = outcome.merged[0].returned_pmid
            print(
                f"factlog pubmed-mesh: PMID {resolution.pmid} was merged into "
                f"{new_pmid} upstream; re-import to follow the pointer before "
                "reading its MeSH.",
                file=sys.stderr,
            )
        else:
            print(
                f"factlog pubmed-mesh: PubMed returned no record for PMID "
                f"{resolution.pmid} (it may be deleted). Nothing to propose.",
                file=sys.stderr,
            )
        return 1

    proposal = build_proposal(resolution.slug, resolution.pmid, work.mesh_headings)
    lines = proposal_porcelain_lines(proposal) if porcelain else proposal_lines(proposal)
    for line in lines:
        print(line)
    return 0


def cmd_arxiv_check_versions(args: argparse.Namespace) -> int:
    """Is any arXiv record in the KB behind arXiv's latest? (#78/#79, §11 Step 6).
    Reads the provenance ledgers and a KB-level check-log, queries arXiv, and
    reports version divergences, records that carry no version at all (#121), and
    newly-withdrawn papers.

    Without ``--auto-update`` this is report-only: it never writes to sources/ or a
    ledger; only the check-log's last-checked timestamps advance (#78).

    With ``--auto-update`` (#79) it additionally records the three version-tracking
    fields — version, last_updated, comment — into each changed paper's provenance
    ledger via the one legitimate caller of ``provenance.update_source``. It never
    opens the original ``.md`` (P4 holds byte- and mtime_ns-identical), never
    rewrites any other ledger field, and never absorbs a withdrawal: a
    newly-withdrawn paper is surfaced for human review under both modes and its
    ``withdrawn_by`` is never written, so it keeps surfacing until a human acts.
    """
    from datetime import datetime, timezone

    from factlog.integrations.arxiv import check_versions as cv
    from factlog.integrations.arxiv.check_log import (
        CheckLogError,
        check_log_path,
        read_check_log,
        record_check,
        write_check_log,
    )
    from factlog.integrations.arxiv.client import ArxivConnectionError, ArxivError

    prepared = _arxiv_prepare(args, "arxiv-check-versions")
    if prepared is None:
        return 1
    target, config = prepared
    porcelain = getattr(args, "porcelain", False)
    older_than_days = args.older_than
    auto_update = getattr(args, "auto_update", False)

    # A corrupt check-log is one KB-level file; surface it as a clear failure
    # rather than a traceback, and never as an empty log (which the next write
    # would then persist over the real one).
    log_path = check_log_path(target)
    try:
        check_log = read_check_log(log_path)
    except CheckLogError as exc:
        print(f"factlog arxiv-check-versions: {exc}", file=sys.stderr)
        return 1

    # A corrupt *ledger* is one source's problem (a per-id error), never a crash.
    entries, ledger_errors = cv.collect_ledger_entries(target)
    # A paper named by a source outside the provenance root can never be checked (#112).
    # It joins the per-file errors so it is named in the report and counted in the exit
    # code — the one thing it must not do is quietly leave the denominator.
    excluded = cv.excluded_checks(target)
    if not entries and not ledger_errors and not excluded:
        if not porcelain:
            print(f"factlog arxiv-check-versions: no arXiv records in {target}")
        else:
            for line in cv.porcelain_lines([], [], cv.summarize([], []), target=target):
                print(line)
        return 0

    now = datetime.now(timezone.utc).replace(microsecond=0)
    to_check, skipped = cv.partition_by_freshness(entries, check_log, older_than_days, now)

    results: list = []
    if to_check:
        client = _make_arxiv_client(config)
        eta = cv.format_eta(len(to_check), cv.BATCH_SIZE, config.request_delay)
        print(
            f"Checking {len(to_check)} arXiv record(s) against the API ({eta})...",
            file=sys.stderr,
        )

        def _progress(done: int, total: int) -> None:
            print(f"  checked {done}/{total}", file=sys.stderr)

        try:
            results = cv.check_entries(to_check, client, progress=_progress)
        except ArxivConnectionError as exc:
            print(f"factlog arxiv-check-versions: {exc}", file=sys.stderr)
            return 2
        except ArxivError as exc:
            # Service/response/transport failures cannot be trusted partial; the
            # check-log is left untouched so a re-run starts clean.
            print(f"factlog arxiv-check-versions: {exc}", file=sys.stderr)
            return 1

    # Record what was actually observed this run: a paper the API answered gets a
    # fresh timestamp and its current (int) version. Missing/errored papers and the
    # skipped ones are left as they were. Nothing under sources/ is touched.
    now_iso = now.isoformat()
    recorded_any = False
    for result in results:
        if result.current_version is not None:
            record_check(check_log, result.arxiv_id, now_iso, result.current_version)
            recorded_any = True
    if recorded_any:
        write_check_log(log_path, check_log)

    # --auto-update writes only the three version-tracking fields into each changed
    # paper's ledger (the sole legitimate caller of update_source). It never opens a
    # source .md; a paper whose fields already match is a byte-identical no-op; a
    # front-matter-only paper has no ledger and is reported, not fabricated; a
    # corrupt ledger is a per-id error, not a crash. A withdrawal is surfaced by the
    # report under both modes and is never written, so it keeps surfacing for human
    # review. Only `results` (papers actually checked this run) are eligible.
    updates = cv.apply_auto_update(results, target) if auto_update else []

    all_results = results + ledger_errors + excluded
    summary = cv.summarize(all_results, skipped)
    if porcelain:
        for line in cv.porcelain_lines(
            all_results, skipped, summary, target=target, updates=updates
        ):
            print(line)
    else:
        for line in cv.report_lines(
            all_results, skipped, summary, target=target,
            older_than_days=older_than_days, updates=updates,
        ):
            print(line)
    update_errors = any(u.status == cv.UPDATE_ERROR for u in updates)
    # A version conflict reaches the exit code, like a per-id error (#112's principle): the
    # KB's own sources contradict each other about a recorded version, and a command that
    # returns 0 while that is true is the silent direction #137 exists to close — a script
    # keying only on the exit status would read a self-contradicting KB as healthy. A
    # version-less record (#121) is self-consistent and has a working remedy, so it does not;
    # a conflict has neither.
    return 1 if summary.errors or update_errors or summary.version_conflict else 0


def cmd_arxiv_acknowledge_withdrawal(args: argparse.Namespace) -> int:
    """Record a human's decision about one arXiv record's withdrawal signal (#100).

    ``arxiv-check-versions`` surfaces a withdrawal on every run until a human records
    it; this is the verb that records it, the same human gate (P1) as ``accept`` /
    ``reject``: one explicit ``--id``, never a sweep. Running it *is* the decision.

    A **live** upstream query is mandatory. The check-log stores only
    ``{arxiv_id, last_checked_at, version}`` — not ``withdrawn_by`` — so the command
    cannot know what to write without asking arXiv. An acknowledgement written from a
    stale cache is a lie: upstream may already have reversed the withdrawal. On a
    connection/rate-limit failure or a missing entry: non-zero exit, nothing written.

    It writes arXiv's **live** value into the ledger's ``withdrawn_by`` via the shared
    acknowledge primitive. Two directions, gated differently (#106):

    * **Setting** it — a fresh withdrawal, or a change of agent (``author`` -> ``admin``)
      — is the loud direction. ``--yes`` may do it.
    * **Clearing** it — writing ``None`` when arXiv reports the paper is no longer
      withdrawn — is the silencing direction, which this project gates on a human (#93).
      Only a human at the prompt, who has seen the printed note, may do it. A ``--yes``
      clear is refused: non-zero exit, nothing written, and the message names the
      interactive re-run as the working path.

    The clear itself is necessary, which is why it exists at all: ``withdrawn_by`` is an
    identifying field, so a stale withdrawal left after an un-withdrawal makes re-import
    error permanently and a refresh may not clear it; only this human write may.

    It never opens the ``.md`` (P4): a paper imported before it was withdrawn keeps showing
    nothing on its KB page, so after acknowledgement the ledger becomes the sole audit
    record.

    Withdrawal is not retraction: arXiv has no peer-reviewed retraction process, and the
    word "retracted" never appears here.
    """
    from factlog.integrations.arxiv import check_versions as cv
    from factlog.integrations.arxiv.client import ArxivConnectionError, ArxivError
    from factlog.integrations.arxiv.id_normalizer import (
        ArxivIdError,
        normalize_arxiv_id,
    )
    from factlog.integrations.common.acknowledge import (
        ACK_ERROR,
        ACK_UNCHANGED,
        ACK_WRITTEN,
        AcknowledgeSchema,
        acknowledge,
    )
    from factlog.integrations.common.provenance import (
        backfill_remedy,
        excluded_reason,
        excluded_sources_by_id,
    )

    command = "arxiv-acknowledge-withdrawal"

    # Identity is the base id. A version-pinned id (`1706.03762v5`) names a version,
    # not the paper the ledger records `withdrawn_by` against, so it is rejected rather
    # than silently stripped — an operator who pinned a version has the wrong mental
    # model of what is being acknowledged and should be told.
    try:
        identifier = normalize_arxiv_id(args.id)
    except ArxivIdError as exc:
        print(f"factlog {command}: {exc}", file=sys.stderr)
        return 1
    if identifier.version is not None:
        print(
            f"factlog {command}: identity is the base id, not a version. Drop the "
            f"version pin (got {args.id!r}; acknowledge {identifier.base!r}).",
            file=sys.stderr,
        )
        return 1
    arxiv_id = identifier.base

    prepared = _arxiv_prepare(args, command)
    if prepared is None:
        return 1
    target, config = prepared

    # A command that cannot ask must not guess (the `_select_search_results` rule).
    # Without a terminal — a pipe, a script — there is no one to confirm to, and
    # silencing a signal is exactly the write that must never be guessed. `--yes`
    # (only ever paired with the required, explicit `--id`) is the deliberate act that
    # stands in for the prompt; without it, a non-interactive run refuses and writes
    # nothing. There is no `--all` and no wildcard: the blast radius is one id.
    assume_yes = getattr(args, "yes", False)
    if not assume_yes and not sys.stdin.isatty():
        print(
            f"factlog {command}: refusing to acknowledge without a terminal to confirm "
            "at. This silences arXiv's withdrawal signal for one id. Re-run in a "
            "terminal, or pass --yes with --id to confirm non-interactively. Nothing "
            "written.",
            file=sys.stderr,
        )
        return 1

    # Resolve ledger presence BEFORE the live query, so a paper this command cannot
    # write is refused for **zero** API requests and with no prompt — the same shape as
    # the TTY gate above (#107 items 1, 3). `collect_ledger_entries` opens only
    # source-provenance/ and sources/ front matter; nothing under sources/ is written or
    # opened for writing (P4).
    entries, ledger_errors = cv.collect_ledger_entries(target)

    # An unreadable ledger might be the one that carries this id — its `withdrawn_by`
    # cannot be read, so the recorded value is unknown. Refuse rather than assert "the
    # ledger did not record" on an incomplete view, and do it before spending a request
    # or prompting (#107 item 3).
    if ledger_errors:
        bad = ", ".join(sorted(e.arxiv_id for e in ledger_errors))
        print(
            f"factlog {command}: cannot read every provenance ledger ({bad}); one of "
            "them may carry this id, so its recorded value is unknown. Repair or remove "
            "the unreadable ledger(s) and retry. No request was made; nothing written.",
            file=sys.stderr,
        )
        return 1

    entry = next((e for e in entries if e.arxiv_id == arxiv_id), None)

    # `acknowledge()` writes only provenance sidecars. A paper known only from
    # front matter (imported before #82, #98), or absent from the KB, has no sidecar to
    # write, so acknowledging it can only ever fail — and querying arXiv first would burn
    # a request and the operator's attention on a warning no human can turn off here
    # (#107 item 1). Refuse before the fetch. Backfilling a ledger is #105, not this
    # command.
    # A ledger-backed paper's sources are `source-provenance/*.json`; a front-matter-only
    # paper's are the `sources/*.md` themselves (`collect_ledger_entries` never mixes the
    # two for one id). Ask the module that builds `sources` rather than re-deriving the
    # rule here: a second copy of one predicate is how #64 and #98 happened, and the two
    # copies already disagreed on an empty tuple.
    front_matter_only = entry is not None and cv.provenance_of(entry.sources) == "front-matter"
    if entry is None or front_matter_only:
        if entry is None:
            # "Not in this KB" was measured to be a lie for a paper that IS in the KB, in a
            # place no ledger can describe (#112). Name the files rather than deny the paper.
            excluded = excluded_sources_by_id(target, "arxiv_id").get(arxiv_id, ())
            if excluded:
                reason = (
                    f"{arxiv_id!r} is named by {', '.join(excluded)}, which "
                    f"{'is' if len(excluded) == 1 else 'are'} outside the provenance root, "
                    "so no ledger can record a decision about it. "
                    + excluded_reason(
                        ", ".join(excluded),
                        backfill_remedy("arxiv-backfill-provenance"),
                    )
                )
            else:
                reason = (
                    f"no arXiv record for id {arxiv_id!r} is in this KB, so there is nothing "
                    "to acknowledge."
                )
        else:
            # A front-matter paper (four kinds, #98) has no *arXiv ledger record* to write a
            # decision into. Which command gives it one is not a function of `arxiv_version`
            # alone — a readable sidecar with no arXiv record is repaired by `arxiv-import`,
            # an absent one by `arxiv-backfill-provenance` only when a version is present, an
            # unreadable one by nothing (#132). The one classifier in check_versions decides,
            # so this refusal never denies a command that repairs the paper nor prescribes
            # one that errors. The human gate stays: this branch writes nothing.
            reason = cv.front_matter_acknowledge_refusal(
                arxiv_id,
                sidecar_state=entry.sidecar_state,
                has_recorded_version=entry.recorded_version is not None,
            )
        print(f"factlog {command}: {reason} No request was made; nothing written.",
              file=sys.stderr)
        return 1

    # The recorded value comes from the ledger we just confirmed (never front matter).
    recorded_by = entry.recorded_withdrawn_by

    # The live query is mandatory: the value to write lives only upstream.
    client = _make_arxiv_client(config)
    try:
        batch = client.fetch_works([arxiv_id])
    except ArxivConnectionError as exc:
        # Cannot reach arXiv: an acknowledgement from a stale cache would be a lie.
        print(f"factlog {command}: {exc} Nothing written.", file=sys.stderr)
        return 2
    except ArxivError as exc:
        print(f"factlog {command}: {exc} Nothing written.", file=sys.stderr)
        return 1
    work = next((w for w in batch.works if w.arxiv_id == arxiv_id), None)
    if work is None:
        print(
            f"factlog {command}: arXiv returned no entry for {arxiv_id!r} "
            "(nonexistent id, or a transient empty response). Nothing written.",
            file=sys.stderr,
        )
        return 1

    # arXiv's live value — `None` when the paper is not (or no longer) withdrawn.
    upstream_by = work.withdrawn_by

    # Nothing to acknowledge when the ledger already matches arXiv (both `None`, or the
    # same agent). Say so and exit 0 without a note or a prompt — the divergence-phrased
    # note would be a lie here, and there is no signal to silence (#107 item 7).
    if upstream_by == recorded_by:
        if upstream_by is None:
            print(
                f"arXiv reports {arxiv_id} is not withdrawn and the ledger records no "
                "withdrawal; nothing to acknowledge."
            )
        else:
            print(
                f"The ledger already records arXiv's live value ({upstream_by}) for "
                f"{arxiv_id}; nothing to acknowledge."
            )
        return 0

    # A **clear** may not be confirmed by `--yes` (#106). Setting `withdrawn_by` is the
    # loud direction; clearing it is the silencing direction, and this project gates the
    # silencing direction on a human (#93). `detect_withdrawal` returning `None` cannot
    # distinguish "arXiv reversed the withdrawal" from "we failed to read the withdrawal
    # sentence" — a truncated abstract, or a phrasing outside the regex, reads as an
    # un-withdrawal. Under `--yes` the operator never sees the note, so a parser miss
    # would silently erase a recorded withdrawal. The interactive path prints the note
    # first, so a human can catch exactly that. The fix is not a wider regex: #79 measured
    # that trade (a silent clear for silent false positives) and rejected it.
    #
    # This cannot be refused before the fetch: `recorded_by` is known from the ledger, but
    # only arXiv's live answer distinguishes a clear (`None`) from a legitimate agent
    # change (`author` -> `admin`), which `--yes` still performs. One request is spent.
    if assume_yes and upstream_by is None:
        print(
            f"factlog {command}: refusing to clear the withdrawal recorded for "
            f"{arxiv_id} ({recorded_by}) with --yes. arXiv reports no withdrawal, but "
            "that also happens when the withdrawal sentence could not be read (a "
            "truncated abstract, an unmatched phrasing) — the code cannot tell the two "
            "apart, and --yes means no human sees the note. Clearing silences a recorded "
            "signal, so it needs a human: re-run in a terminal without --yes and confirm "
            "at the prompt. Nothing written.",
            file=sys.stderr,
        )
        return 1

    note_source = cv.VersionCheck(
        arxiv_id=arxiv_id,
        status=cv.STATUS_UNCHANGED,
        current_version=work.version,
        newly_withdrawn=upstream_by is not None,
        un_withdrawn=upstream_by is None,
        withdrawn_by=upstream_by,
        recorded_withdrawn_by=recorded_by,
        recorded_from="ledger",
    )

    # Show the operator exactly what they are about to silence (or clear).
    if upstream_by is not None:
        print(cv.withdrawal_note(note_source))
    else:
        print(cv.un_withdrawal_note(note_source))

    if not assume_yes:
        try:
            answer = input(
                f"\nRecord arXiv's live value for {arxiv_id} in the ledger? [y/N] "
            ).strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted; nothing written.")
            return 0

    # Write the live value — including `None`, which clears the identifying field.
    schema = AcknowledgeSchema(type="arxiv", field="withdrawn_by")
    result = acknowledge(target, arxiv_id, upstream_by, schema)

    if result.status == ACK_WRITTEN:
        if upstream_by is None:
            print(
                f"Cleared the withdrawal recorded for {arxiv_id} in "
                f"{', '.join(result.ledgers)}."
            )
        else:
            print(
                f"Recorded withdrawal by {upstream_by} for {arxiv_id} in "
                f"{', '.join(result.ledgers)}."
            )
        print(
            "arxiv-check-versions will no longer repeat this signal. The ledger is the "
            "audit record; the source .md is not rewritten (P4)."
        )
        return 0
    if result.status == ACK_UNCHANGED:
        print(
            f"The ledger for {arxiv_id} already holds arXiv's live value; nothing "
            "changed. arxiv-check-versions will not repeat this signal."
        )
        return 0
    if result.status == ACK_ERROR:
        print(f"factlog {command}: {result.reason}", file=sys.stderr)
        return 1
    # ACK_NO_LEDGER cannot occur — ledger presence was confirmed before the fetch — but
    # is handled defensively rather than mis-reported as success.
    print(f"factlog {command}: {result.reason or result.status}", file=sys.stderr)
    return 1


def cmd_arxiv_backfill_provenance(args: argparse.Namespace) -> int:
    """Materialize the provenance ledger a front-matter-only arXiv paper already implies (#114, #105).

    A paper imported before #82 has front matter and no sidecar, so a re-import
    short-circuits on the front-matter identity match before the sidecar writer, its
    ledger is never created, and its withdrawal signal can never be acknowledged (both
    acknowledge commands refuse a paper with no ledger, and point here). This builds that
    ledger from what the ``.md`` already asserts — ``add_source`` into a fresh sidecar, no
    new claim — so acknowledge can then silence the repeat.

    **No network.** This makes no new claim: the record is derived deterministically from
    front matter, which a human reviewed at import. It changes where a belief is stored,
    not what is believed — so, unlike acknowledge, there is **no confirmation prompt, no
    ``--yes`` and no TTY gate**. It never constructs an API client. It only *reads* front
    matter (P4: every ``.md`` stays byte- and ``mtime_ns``-identical).

    ``--dry-run`` writes nothing and names both the ids that would get a ledger and the ids
    that are refused (a missing ``imported_at``, or an unreadable identifying ``version`` —
    an OpenAlex-authored ``.md`` echoing ``arxiv_id`` but carrying no ``arxiv_version``).
    ``submitted``, ``last_updated`` and ``comment`` are not in front matter; the first
    ``arxiv-check-versions --auto-update`` fills the latter two, but ``submitted`` is not in
    ``AUTO_UPDATE_FIELDS`` and is unrecoverable — it is not invented and not queried for.

    Re-running is a byte- and ``mtime_ns``-identical no-op (a record already present is not
    rewritten). A per-id read/write fault is reported for that paper, never a batch crash.
    """
    from pathlib import Path

    from factlog.integrations.arxiv.backfill import backfill_schema
    from factlog.integrations.common.backfill import (
        BACKFILL_ERROR,
        BACKFILL_REFUSED,
        BACKFILL_WRITTEN,
        backfill,
    )

    command = "arxiv-backfill-provenance"
    porcelain = getattr(args, "porcelain", False)
    dry_run = getattr(args, "dry_run", False)

    target_str, source = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if source in ("config", "cwd") and not porcelain:
        print(f"factlog {command}: target KB {target} (from {source})")
    if not _require_kb(target, command):
        return 1

    # Reads front matter and the ledgers only; constructs no API client (a test asserts it).
    results = backfill(target, backfill_schema(), dry_run=dry_run)

    # BACKFILL_UNCHANGED is intentionally not handled: it can only be produced by a direct
    # _backfill_source call, never by backfill(). A paper whose sidecar already holds an
    # identical arXiv record is classified "ledger" by provenance_of and skipped before any
    # write is attempted, so backfill() never yields it — a status the command cannot
    # receive gets no dead section here.
    written = [r for r in results if r.status == BACKFILL_WRITTEN]
    refused = [r for r in results if r.status == BACKFILL_REFUSED]
    errors = [r for r in results if r.status == BACKFILL_ERROR]

    if porcelain:
        # Stable machine contract, tab-separated, LF-terminated, order-independent (parse
        # by the first field). A per-id row for every paper acted on or refused:
        #   result\t<status>\t<arxiv id>\t<ledger path or empty>\t<reason or empty>
        # then the summary counts, then dry_run and the sources dir.
        #
        # EVERY field a caller can influence is neutralized at emission, not just `reason`:
        # `entry_id` is an arxiv_id read verbatim from front matter, `ledger` is a filename,
        # and `target` is `--target` — any of them can carry a tab or a line break that would
        # otherwise shift or split the columns after it (the #111 column-shift, whose danger
        # is that a *document* — an `arxiv_id: "x<TAB>y"` — not just an exotic filename can
        # drive it). Neutralizing only the last column leaves the earlier ones exploitable.
        # Delegates to the shared rule rather than restating it: a local copy is how the
        # gate silently narrows. These three backfill emitters each carried their own
        # tab/CR/LF copy and were left behind when #396 widened the shared set to every
        # line break, so the eight characters porcelain_field had started neutralizing
        # still split these rows in two (measured).
        from factlog.integrations.common.porcelain import porcelain_field

        def _f(value: object) -> str:
            return porcelain_field(str(value))

        for r in results:
            print(
                f"result\t{r.status}\t{_f(r.entry_id)}\t{_f(r.ledger)}\t{_f(r.reason)}"
            )
        print(f"backfilled\t{len(written)}")
        print(f"refused\t{len(refused)}")
        print(f"errors\t{len(errors)}")
        print(f"dry_run\t{'1' if dry_run else '0'}")
        print(f"target\t{_f(target / 'sources')}")
        return 1 if errors else 0

    verb = "Would backfill" if dry_run else "Backfilling"
    print(
        f"\n{verb} provenance ledgers for front-matter-only arXiv papers in KB: {target}\n"
    )
    if not results:
        print(
            "No front-matter-only arXiv papers found (every arXiv paper the check "
            "commands can read already has a provenance ledger)."
        )
        return 0

    if written:
        label = "Would get a provenance ledger:" if dry_run else "Provenance ledger written:"
        print(label)
        for r in written:
            arrow = "would write" if dry_run else "wrote"
            print(f"  ✎ {r.entry_id}  ({arrow} {r.ledger})")
    if refused:
        print("\nRefused (front matter cannot supply a truthful ledger):")
        for r in refused:
            print(f"  ✗ {r.entry_id}: {r.reason}")
    if errors:
        # Not only sidecar faults: a source outside the provenance root can hold no
        # ledger at all (#112), and it is reported here rather than skipped.
        print("\nCould not backfill:")
        for r in errors:
            print(f"  ✗ {r.entry_id}: {r.reason}")

    print("\nSummary:")
    print(f"  {'Would backfill:' if dry_run else 'Backfilled:':<18}{len(written)}")
    print(f"  {'Refused:':<18}{len(refused)}")
    print(f"  {'Errors:':<18}{len(errors)}")
    if written and not dry_run:
        print(
            "\nNext step: a withdrawn paper among these can now be acknowledged with "
            "'factlog arxiv-acknowledge-withdrawal --id <id>'."
        )
    return 1 if errors else 0


def cmd_pubmed_backfill_provenance(args: argparse.Namespace) -> int:
    """Materialize the provenance ledger a front-matter-only PubMed paper already implies (#172, #105).

    A PubMed paper imported before provenance ledgers existed has front matter and no
    sidecar, so a re-import short-circuits on the front-matter identity match before the
    sidecar writer, its ledger is never created, and its retraction signal can never be
    acknowledged (``pubmed-acknowledge-retraction`` refuses a paper with no ledger, and
    points here). This builds that ledger from what the ``.md`` already asserts —
    ``add_source`` into a fresh sidecar, no new claim — so acknowledge can then silence the
    repeat ``pubmed-refresh`` surfaces on every run.

    **No network.** This makes no new claim: the record is derived deterministically from
    front matter, which a human reviewed at import. It changes where a belief is stored, not
    what is believed — so, unlike acknowledge, there is **no confirmation prompt, no
    ``--yes`` and no TTY gate**, and it needs no NCBI contact email (nothing is sent). It
    never constructs a PubMed client. It only *reads* front matter (P4: every ``.md`` stays
    byte- and ``mtime_ns``-identical).

    A backfilled record reproduces ``PubMedSourceWriter._provenance_record`` for the values
    the front matter carries — ``doi``, ``journal``, and PubMed's ``retracted`` /
    ``retraction_notice_pmid`` signal — each read verbatim. The one field it cannot
    reproduce is ``retraction_verified_at`` (the import clock, not a front-matter key): a
    backfill consulted PubMed at no time, so it invents none. That field is not identifying,
    so its absence causes no divergence — the same asymmetry OpenAlex documents for a field
    its writer does not emit.

    A PMID shared by two ``.md`` gets a sidecar for **each**, from its own front matter, so
    coverage never turns on which filename sorts first (#117); a nested paper is covered too
    (the shared #112 walker). ``--dry-run`` writes nothing and names both the ids that would
    get a ledger and the ids refused (a missing ``imported_at``, or a ``pubmed_retracted``
    outside the ledger's value space — anything but the YAML booleans ``true``/``false``).
    The latter is promoted verbatim and refused by the shared writer, never coerced.

    Re-running is a byte- and ``mtime_ns``-identical no-op (a record already present is not
    rewritten). A per-id read/write fault is reported for that paper, never a batch crash.
    """
    from pathlib import Path

    from factlog.integrations.common.backfill import (
        BACKFILL_ERROR,
        BACKFILL_REFUSED,
        BACKFILL_WRITTEN,
        backfill,
    )
    from factlog.integrations.pubmed.backfill import backfill_schema

    command = "pubmed-backfill-provenance"
    porcelain = getattr(args, "porcelain", False)
    dry_run = getattr(args, "dry_run", False)

    target_str, source = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if source in ("config", "cwd") and not porcelain:
        print(f"factlog {command}: target KB {target} (from {source})")
    if not _require_kb(target, command):
        return 1

    # Reads front matter and the ledgers only; constructs no PubMed client (a test asserts it).
    results = backfill(target, backfill_schema(), dry_run=dry_run)

    # BACKFILL_UNCHANGED is intentionally not handled: it can only be produced by a direct
    # _backfill_source call, never by backfill(). A paper whose sidecar already holds an
    # identical PubMed record is classified "ledger" by provenance_of and skipped before any
    # write is attempted, so backfill() never yields it.
    written = [r for r in results if r.status == BACKFILL_WRITTEN]
    refused = [r for r in results if r.status == BACKFILL_REFUSED]
    errors = [r for r in results if r.status == BACKFILL_ERROR]

    if porcelain:
        # The same machine contract arxiv-/openalex-backfill-provenance emit, shape for
        # shape: tab-separated, LF-terminated, order-independent (parse by the first field).
        #   result\t<status>\t<pmid>\t<ledger path or empty>\t<reason or empty>
        # then the summary counts, then dry_run and the sources dir.
        #
        # EVERY field a caller can influence is neutralized at emission, not just `reason`:
        # `entry_id` is a pmid read verbatim from front matter, `ledger` is a filename, and
        # `target` is `--target` — any of them can carry a tab or a line break that would otherwise
        # shift or split the columns after it (the #111 column-shift). Neutralizing only the
        # last column leaves the earlier ones exploitable.
        # Delegates to the shared rule rather than restating it: a local copy is how the
        # gate silently narrows. These three backfill emitters each carried their own
        # tab/CR/LF copy and were left behind when #396 widened the shared set to every
        # line break, so the eight characters porcelain_field had started neutralizing
        # still split these rows in two (measured).
        from factlog.integrations.common.porcelain import porcelain_field

        def _f(value: object) -> str:
            return porcelain_field(str(value))

        for r in results:
            print(
                f"result\t{r.status}\t{_f(r.entry_id)}\t{_f(r.ledger)}\t{_f(r.reason)}"
            )
        print(f"backfilled\t{len(written)}")
        print(f"refused\t{len(refused)}")
        print(f"errors\t{len(errors)}")
        print(f"dry_run\t{'1' if dry_run else '0'}")
        print(f"target\t{_f(target / 'sources')}")
        return 1 if errors else 0

    verb = "Would backfill" if dry_run else "Backfilling"
    print(
        f"\n{verb} provenance ledgers for front-matter-only PubMed papers in KB: {target}\n"
    )
    if not results:
        print(
            "No front-matter-only PubMed papers found (every PubMed paper the check "
            "commands can read already has a provenance ledger)."
        )
        return 0

    if written:
        label = "Would get a provenance ledger:" if dry_run else "Provenance ledger written:"
        print(label)
        for r in written:
            arrow = "would write" if dry_run else "wrote"
            print(f"  ✎ {r.entry_id}  ({arrow} {r.ledger})")
    if refused:
        print("\nRefused (front matter cannot supply a truthful ledger):")
        for r in refused:
            print(f"  ✗ {r.entry_id}: {r.reason}")
    if errors:
        # Not only sidecar faults: a source outside the provenance root can hold no ledger at
        # all (#112), and it is reported here rather than skipped.
        print("\nCould not backfill:")
        for r in errors:
            print(f"  ✗ {r.entry_id}: {r.reason}")

    print("\nSummary:")
    print(f"  {'Would backfill:' if dry_run else 'Backfilled:':<18}{len(written)}")
    print(f"  {'Refused:':<18}{len(refused)}")
    print(f"  {'Errors:':<18}{len(errors)}")
    if written and not dry_run:
        print(
            "\nNext step: a paper PubMed flags as retracted among these can now be "
            "acknowledged with 'factlog pubmed-acknowledge-retraction --id <id>'."
        )
    return 1 if errors else 0


def cmd_openalex_refresh(args: argparse.Namespace) -> int:
    """Has any OpenAlex record in the KB drifted from what OpenAlex now serves? (#83).
    Reads the provenance ledgers and a KB-level check-log, re-fetches each work by id
    (``GET /works/{id}``, 0 credits), and reports divergences in the fields the ledger
    stores — doi, work_type, journal — plus a newly-set retraction and a superseded id.

    Without ``--auto-update`` this is report-only: it never writes to sources/ or a
    ledger; only the check-log's last-checked timestamps advance.

    With ``--auto-update`` it records the three venue/identifier fields — doi,
    work_type, journal — into each changed work's provenance ledger via the second
    legitimate caller of ``provenance.update_source`` (alongside arxiv-check-versions).
    It never opens the original ``.md`` (P4 holds byte- and mtime_ns-identical), never
    rewrites any other ledger field, never writes ``is_retracted`` (H1), and never
    rewrites the ledger key when an id is superseded (H3): a retraction and an identity
    change are surfaced for human review under both modes.
    """
    from datetime import datetime, timezone

    from factlog.integrations.openalex import refresh as rf
    from factlog.integrations.openalex.api_client import (
        OpenAlexConnectionError,
        OpenAlexError,
        OpenAlexRateLimitError,
    )
    from factlog.integrations.openalex.check_log import (
        CheckLogError,
        check_log_path,
        read_check_log,
        record_check,
        write_check_log,
    )

    prepared = _openalex_prepare(args, "openalex-refresh")
    if prepared is None:
        return 1
    target, config = prepared
    porcelain = getattr(args, "porcelain", False)
    older_than_days = args.older_than
    auto_update = getattr(args, "auto_update", False)

    # A corrupt check-log is one KB-level file; surface it as a clear failure rather
    # than a traceback, and never as an empty log (which the next write would persist).
    log_path = check_log_path(target)
    try:
        check_log = read_check_log(log_path)
    except CheckLogError as exc:
        print(f"factlog openalex-refresh: {exc}", file=sys.stderr)
        return 1

    # A corrupt *ledger* is one source's problem (a per-id error), never a crash.
    entries, ledger_errors = rf.collect_ledger_entries(target)
    # A work named by a source outside the provenance root can never be refreshed (#112);
    # it is reported as a per-file error rather than left out of the denominator.
    excluded = rf.excluded_checks(target)
    if not entries and not ledger_errors and not excluded:
        if not porcelain:
            print(f"factlog openalex-refresh: no OpenAlex records in {target}")
        else:
            for line in rf.porcelain_lines([], [], rf.summarize([], []), target=target):
                print(line)
        return 0

    now = datetime.now(timezone.utc).replace(microsecond=0)
    to_check, skipped = rf.partition_by_freshness(entries, check_log, older_than_days, now)

    results: list = []
    if to_check:
        client = _make_openalex_client(config)
        print(
            f"Refreshing {len(to_check)} OpenAlex record(s) against the API "
            "(0 credits each)...",
            file=sys.stderr,
        )

        def _progress(done: int, total: int) -> None:
            print(f"  checked {done}/{total}", file=sys.stderr)

        try:
            results = rf.check_entries(to_check, client, progress=_progress)
        except (OpenAlexConnectionError, OpenAlexRateLimitError) as exc:
            print(f"factlog openalex-refresh: {exc}", file=sys.stderr)
            return 2
        except OpenAlexError as exc:
            # A service/transport failure cannot be trusted partial; the check-log is
            # left untouched so a re-run starts clean.
            print(f"factlog openalex-refresh: {exc}", file=sys.stderr)
            return 1

    # Record what was actually observed this run: every work the API answered gets a
    # fresh timestamp. A NotFound (per-id error) is left as it was so it is retried.
    # Nothing under sources/ is touched.
    now_iso = now.isoformat()
    recorded_any = False
    for result in results:
        if result.status != rf.STATUS_ERROR:
            record_check(check_log, result.openalex_id, now_iso)
            recorded_any = True
    if recorded_any:
        write_check_log(log_path, check_log)

    # --auto-update writes only doi/work_type/journal into each changed work's ledger.
    # It never opens a source .md; a work whose fields already match is a byte-identical
    # no-op; a front-matter-only work has no ledger and is reported, not fabricated; a
    # superseded id is reported, never followed; a corrupt ledger is a per-id error. A
    # retraction is surfaced under both modes and is never written. Only `results`
    # (works actually checked this run) are eligible.
    updates = rf.apply_auto_update(results, target) if auto_update else []

    all_results = results + ledger_errors + excluded
    summary = rf.summarize(all_results, skipped)
    if porcelain:
        for line in rf.porcelain_lines(
            all_results, skipped, summary, target=target, updates=updates
        ):
            print(line)
    else:
        for line in rf.report_lines(
            all_results, skipped, summary, target=target,
            older_than_days=older_than_days, updates=updates,
        ):
            print(line)
    update_errors = any(u.status == rf.UPDATE_ERROR for u in updates)
    return 1 if summary.errors or update_errors else 0


def cmd_openalex_acknowledge_retraction(args: argparse.Namespace) -> int:
    """Record a human's decision about one OpenAlex record's retraction signal (#101).

    The mirror of ``arxiv-acknowledge-withdrawal`` on the shared acknowledge primitive,
    in OpenAlex's vocabulary only. ``openalex-refresh`` surfaces a retraction on every run
    until a human records it; this is the verb that records it — the same human gate (P1)
    as ``accept`` / ``reject``: one explicit ``--id``, never a sweep. Running it *is* the
    decision. ``is_retracted`` is **OpenAlex's opinion**, not a fact — OpenAlex flags the
    Lancet Commission dementia report as retracted while PubMed records no retraction (#51)
    — so the front-matter key is ``openalex_is_retracted`` and the word "withdrawn" never
    appears here.

    A **live** ``GET /works/{id}`` (0 credits) is mandatory: a retraction recorded from a
    stale cache is a lie, since OpenAlex may already have reversed it. On a
    connection/rate-limit failure or a missing/merged-away record: non-zero exit, nothing
    written. ``get_work`` follows redirects, so a request for ``W_a`` can answer under a
    merged ``W_b``; acknowledging under the old key would be wrong, so an identity change
    is refused and reported (re-import to follow it).

    It writes OpenAlex's **live** flag into the ledger's ``is_retracted`` via the shared
    primitive: ``True`` to record a retraction, or ``None`` — which *removes* the key — to
    clear one OpenAlex has reversed (a literal ``False`` would change the JSON bytes and
    diverge from what an import writes, where retraction absent *means* not retracted).
    ``is_retracted`` is **not** an identifying field, so no re-import ever errors over it in
    either direction; the clear path exists so a reversed retraction can stop surfacing and
    the ledger can record that it was reversed. But ``--yes`` may only *record* a
    retraction, never *clear* one (#106, #414): clearing silences a recorded signal, and the
    un-retraction note is printed for a human to weigh, which ``--yes`` skips. A clear needs
    an interactive re-run. It never opens the ``.md`` (P4): after acknowledgement the ledger
    is the sole audit record.
    """
    from factlog.integrations.common.acknowledge import (
        ACK_ERROR,
        ACK_UNCHANGED,
        ACK_WRITTEN,
        AcknowledgeSchema,
        acknowledge,
    )
    from factlog.integrations.common.provenance import (
        backfill_remedy,
        excluded_reason,
        excluded_sources_by_id,
    )
    from factlog.integrations.openalex import refresh as rf
    from factlog.integrations.openalex.api_client import (
        OpenAlexConnectionError,
        OpenAlexError,
        OpenAlexNotFoundError,
        OpenAlexRateLimitError,
        normalize_work_id,
    )
    from factlog.integrations.openalex.work_parser import parse_work

    command = "openalex-acknowledge-retraction"

    # Identity is the bare OpenAlex work id. `normalize_work_id` accepts an
    # `openalex.org/W...` URL and a lowercase `w...`, and rejects zero-padding — the same
    # validation the import/refresh paths use, so the id keyed here matches the ledger's.
    try:
        openalex_id = normalize_work_id(args.id)
    except OpenAlexError as exc:
        print(f"factlog {command}: {exc}", file=sys.stderr)
        return 1

    prepared = _openalex_prepare(args, command)
    if prepared is None:
        return 1
    target, config = prepared

    # A command that cannot ask must not guess. Without a terminal — a pipe, a script —
    # there is no one to confirm to, and silencing a signal is exactly the write that must
    # never be guessed. `--yes` (only ever paired with the required, explicit `--id`) is
    # the deliberate act that stands in for the prompt; without it, a non-interactive run
    # refuses and writes nothing. There is no `--all` and no wildcard: the blast radius is
    # one id. This refuses BEFORE the query, so no request is spent on a run that cannot
    # confirm.
    assume_yes = getattr(args, "yes", False)
    if not assume_yes and not sys.stdin.isatty():
        print(
            f"factlog {command}: refusing to acknowledge without a terminal to confirm "
            "at. This silences OpenAlex's retraction signal for one id. Re-run in a "
            "terminal, or pass --yes with --id to confirm non-interactively. Nothing "
            "written.",
            file=sys.stderr,
        )
        return 1

    # Resolve ledger presence BEFORE the live query, so a work this command cannot write is
    # refused for **zero** API requests and with no prompt — the same shape as the TTY gate
    # above (#107 items 1, 3). `collect_ledger_entries` opens only source-provenance/ and
    # sources/ front matter; nothing under sources/ is written or opened for writing (P4).
    entries, ledger_errors = rf.collect_ledger_entries(target)

    # An unreadable ledger might be the one that carries this id — its `is_retracted`
    # cannot be read, so the recorded value is unknown. Refuse rather than assert "the
    # ledger did not record" on an incomplete view, and do it before spending a request or
    # prompting (#107 item 3).
    if ledger_errors:
        bad = ", ".join(sorted(e.openalex_id for e in ledger_errors))
        print(
            f"factlog {command}: cannot read every provenance ledger ({bad}); one of them "
            "may carry this id, so its recorded value is unknown. Repair or remove the "
            "unreadable ledger(s) and retry. No request was made; nothing written.",
            file=sys.stderr,
        )
        return 1

    entry = next((e for e in entries if e.openalex_id == openalex_id), None)

    # `acknowledge()` writes only provenance sidecars. A work known only from front matter
    # (imported before #84), or absent from the KB, has no sidecar to write, so
    # acknowledging it can only ever fail — and querying OpenAlex first would spend a
    # request and the operator's attention on a warning no human can turn off here (#107
    # item 1). Refuse before the fetch, and name the command that builds one (#115).
    # Ask the module that builds `sources`; a second copy of the predicate is how #64 and
    # #98 happened, and the two copies already disagreed on an empty tuple.
    front_matter_only = entry is not None and rf.provenance_of(entry.sources) == "front-matter"
    if entry is None or front_matter_only:
        if entry is None:
            # "Not in this KB" is a lie for a work that IS in the KB, in a place no ledger
            # can describe (#112). Name the files rather than deny the work.
            excluded = excluded_sources_by_id(target, "openalex_id").get(openalex_id, ())
            if excluded:
                reason = (
                    f"{openalex_id!r} is named by {', '.join(excluded)}, which "
                    f"{'is' if len(excluded) == 1 else 'are'} outside the provenance root, "
                    "so no ledger can record a decision about it. "
                    + excluded_reason(
                        ", ".join(excluded),
                        backfill_remedy("openalex-backfill-provenance"),
                    )
                )
            else:
                reason = (
                    f"no OpenAlex record for id {openalex_id!r} is in this KB, so there is "
                    "nothing to acknowledge."
                )
        else:
            reason = (
                f"{openalex_id!r} is known only from front matter (imported before #84), "
                "so it has no provenance ledger to record a decision in — and re-import "
                "will not create one. Run `factlog openalex-backfill-provenance` to give "
                "it one, then acknowledge."
            )
        print(f"factlog {command}: {reason} No request was made; nothing written.",
              file=sys.stderr)
        return 1

    # The recorded value comes from the ledger we just confirmed (never front matter).
    recorded_is_retracted = entry.recorded_is_retracted

    # The live query is mandatory: the value to write lives only upstream.
    client = _make_openalex_client(config)
    try:
        parsed = parse_work(client.get_work(openalex_id))
    except (OpenAlexConnectionError, OpenAlexRateLimitError) as exc:
        # Cannot reach OpenAlex (or out of budget): an acknowledgement from a stale cache
        # would be a lie.
        print(f"factlog {command}: {exc} Nothing written.", file=sys.stderr)
        return 2
    except OpenAlexNotFoundError:
        print(
            f"factlog {command}: OpenAlex has no record for {openalex_id!r} (deleted or "
            "merged away). Nothing written.",
            file=sys.stderr,
        )
        return 1
    except OpenAlexError as exc:
        print(f"factlog {command}: {exc} Nothing written.", file=sys.stderr)
        return 1

    # H3: `get_work` follows redirects and OpenAlex merges works, so a request for `W_a`
    # can answer under a merged `W_b`. Acknowledging under the old key would record a
    # decision about the wrong identity; refuse and report the change (re-import to follow
    # it). `refresh.py` reports the same case; here it must block the write.
    if parsed.openalex_id != openalex_id:
        print(
            f"factlog {command}: OpenAlex answered under a different id "
            f"{parsed.openalex_id!r} (the work was merged upstream). Refusing to "
            f"acknowledge under {openalex_id!r}; re-import to follow the new id. Nothing "
            "written.",
            file=sys.stderr,
        )
        return 1

    # OpenAlex's live opinion — `False` when the work is not (or no longer) retracted.
    current_is_retracted = bool(parsed.openalex_is_retracted)

    # Nothing to acknowledge when the ledger already matches OpenAlex. Say so and exit 0
    # without a note or a prompt — the divergence-phrased note would be a lie, and there is
    # no signal to silence (#107 item 7).
    if current_is_retracted == recorded_is_retracted:
        if current_is_retracted:
            print(
                f"The ledger already records OpenAlex's retraction flag for {openalex_id}; "
                "nothing to acknowledge."
            )
        else:
            print(
                f"OpenAlex does not flag {openalex_id} as retracted and the ledger records "
                "no retraction; nothing to acknowledge."
            )
        return 0

    # #106: a CLEAR (recorded retracted, OpenAlex no longer flags it) may not be confirmed
    # by `--yes`. Recording a retraction is the loud direction `--yes` may do; clearing one
    # is the silencing direction, and this project gates the silencing direction on a human
    # (#93). Under `--yes` nobody reads the un-retraction note printed below, so the clear
    # is refused. This is knowable only after the fetch.
    #
    # Note the argument this does NOT rest on. arXiv gates its clear because
    # `detect_withdrawal` cannot tell a reversal from a withdrawal sentence it failed to
    # parse; OpenAlex has no such weakness — `is_retracted` is a structured boolean, so a
    # `False` really is OpenAlex's current answer. OpenAlex is in fact a known
    # false-positive source (#51: it flags the Lancet Commission report PubMed does not),
    # so `True -> False` may well be OpenAlex correcting itself. That is a reason to keep
    # the *interactive* clear easy — one prompt, no extra ceremony — not a reason to let
    # `--yes` skip the human: recording wrongly is a nuisance, clearing wrongly means
    # citing a retracted paper. (#414: the rule was established 2h38m after this command
    # was written and was never applied back to it, which is why this gate arrived late.)
    if assume_yes and recorded_is_retracted and not current_is_retracted:
        print(
            f"factlog {command}: refusing to clear the retraction recorded for "
            f"{openalex_id} with --yes. OpenAlex no longer flags it as retracted, and that "
            "may well be OpenAlex correcting its own false positive — but a recorded "
            "retraction flipping back is exactly what a human should read the note for, "
            "and --yes means nobody sees it. Clearing silences a recorded signal, so it "
            "needs a human: re-run in a terminal without --yes and confirm at the prompt. "
            "Nothing written.",
            file=sys.stderr,
        )
        return 1

    note_source = rf.RefreshCheck(
        openalex_id=openalex_id,
        status=rf.STATUS_UNCHANGED,
        returned_id=parsed.openalex_id,
        recorded_is_retracted=recorded_is_retracted,
        current_is_retracted=current_is_retracted,
        newly_retracted=current_is_retracted and not recorded_is_retracted,
        un_retracted=(not current_is_retracted) and recorded_is_retracted,
        recorded_from="ledger",
    )

    # Show the operator exactly what they are about to record (or clear).
    if current_is_retracted:
        print(rf.retraction_note(note_source))
    else:
        print(rf.un_retraction_note(note_source, prescribe=False))

    if not assume_yes:
        try:
            answer = input(
                f"\nRecord OpenAlex's live retraction flag for {openalex_id} in the "
                "ledger? [y/N] "
            ).strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted; nothing written.")
            return 0

    # Write the live flag: `True` records a retraction, `None` clears one (removing the
    # key — never a literal `False`, which would change the bytes and diverge from an
    # import's absent-means-not-retracted convention).
    value = True if current_is_retracted else None
    schema = AcknowledgeSchema(type="openalex", field="is_retracted")
    result = acknowledge(target, openalex_id, value, schema)

    if result.status == ACK_WRITTEN:
        if current_is_retracted:
            print(
                f"Recorded OpenAlex's retraction flag for {openalex_id} in "
                f"{', '.join(result.ledgers)}."
            )
        else:
            print(
                f"Cleared the retraction recorded for {openalex_id} in "
                f"{', '.join(result.ledgers)}."
            )
        print(
            "openalex-refresh will no longer repeat this signal. The ledger is the audit "
            "record; the source .md is not rewritten (P4)."
        )
        return 0
    if result.status == ACK_UNCHANGED:
        print(
            f"The ledger for {openalex_id} already holds OpenAlex's live retraction flag; "
            "nothing changed. openalex-refresh will not repeat this signal."
        )
        return 0
    if result.status == ACK_ERROR:
        print(f"factlog {command}: {result.reason}", file=sys.stderr)
        return 1
    # ACK_NO_LEDGER cannot occur — ledger presence was confirmed before the fetch — but is
    # handled defensively rather than mis-reported as success.
    print(f"factlog {command}: {result.reason or result.status}", file=sys.stderr)
    return 1


def cmd_openalex_backfill_provenance(args: argparse.Namespace) -> int:
    """Materialize the provenance ledger a front-matter-only OpenAlex paper already implies (#115, #105).

    A paper imported before #84 has front matter and no sidecar, so a re-import
    short-circuits on the front-matter identity match before the sidecar writer, its ledger
    is never created, and its retraction signal can never be acknowledged
    (``openalex-acknowledge-retraction`` refuses a paper with no ledger, and points here).
    This builds that ledger from what the ``.md`` already asserts — ``add_source`` into a
    fresh sidecar, no new claim — so acknowledge can then silence the repeat.

    **No network.** This makes no new claim: the record is derived deterministically from
    front matter, which a human reviewed at import. It changes where a belief is stored, not
    what is believed — so, unlike acknowledge, there is **no confirmation prompt, no
    ``--yes`` and no TTY gate**. It never constructs an API client. It only *reads* front
    matter (P4: every ``.md`` stays byte- and ``mtime_ns``-identical).

    Unlike arXiv (#114), **nothing is lost**: every field the OpenAlex ledger record holds —
    ``doi``, ``work_type``, ``journal``, ``is_retracted`` — has a front-matter key the
    writer emits, and ``imported_at`` is in front matter too. A backfilled OpenAlex record
    is field-for-field what the import would have written. That asymmetry with arXiv's
    unrecoverable ``submitted`` is a fact about the two writers, not about this command.

    ``--dry-run`` writes nothing and names both the ids that would get a ledger and the ids
    that are refused: a missing ``imported_at``, or a ``openalex_is_retracted`` outside the
    ledger's value space (anything but the YAML booleans ``true``/``false``). The latter is
    promoted verbatim and refused by the shared writer, never coerced — dropping it would
    assert OpenAlex does not flag the paper, and reading ``1`` as true would assert a
    retraction no source made.

    Re-running is a byte- and ``mtime_ns``-identical no-op (a record already present is not
    rewritten). A per-id read/write fault is reported for that paper, never a batch crash.
    """
    from pathlib import Path

    from factlog.integrations.common.backfill import (
        BACKFILL_ERROR,
        BACKFILL_REFUSED,
        BACKFILL_WRITTEN,
        backfill,
    )
    from factlog.integrations.openalex.backfill import backfill_schema

    command = "openalex-backfill-provenance"
    porcelain = getattr(args, "porcelain", False)
    dry_run = getattr(args, "dry_run", False)

    target_str, source = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if source in ("config", "cwd") and not porcelain:
        print(f"factlog {command}: target KB {target} (from {source})")
    if not _require_kb(target, command):
        return 1

    # Reads front matter and the ledgers only; constructs no API client (a test asserts it).
    results = backfill(target, backfill_schema(), dry_run=dry_run)

    # BACKFILL_UNCHANGED is intentionally not handled: it can only be produced by a direct
    # _backfill_source call, never by backfill(). A paper whose sidecar already holds an
    # identical OpenAlex record is classified "ledger" by provenance_of and skipped before
    # any write is attempted, so backfill() never yields it — a status the command cannot
    # receive gets no dead section here.
    written = [r for r in results if r.status == BACKFILL_WRITTEN]
    refused = [r for r in results if r.status == BACKFILL_REFUSED]
    errors = [r for r in results if r.status == BACKFILL_ERROR]

    if porcelain:
        # The same machine contract arxiv-backfill-provenance emits, shape for shape:
        # tab-separated, LF-terminated, order-independent (parse by the first field). A
        # per-id row for every paper acted on or refused:
        #   result\t<status>\t<openalex id>\t<ledger path or empty>\t<reason or empty>
        # then the summary counts, then dry_run and the sources dir.
        #
        # EVERY field a caller can influence is neutralized at emission, not just `reason`:
        # `entry_id` is an openalex_id read verbatim from front matter, `ledger` is a
        # filename, and `target` is `--target` — any of them can carry a tab or a line break that
        # would otherwise shift or split the columns after it (the #111 column-shift, whose
        # danger is that a *document* — an `openalex_id: "x<TAB>y"` — not just an exotic
        # filename can drive it). Neutralizing only the last column leaves the earlier ones
        # exploitable.
        # Delegates to the shared rule rather than restating it: a local copy is how the
        # gate silently narrows. These three backfill emitters each carried their own
        # tab/CR/LF copy and were left behind when #396 widened the shared set to every
        # line break, so the eight characters porcelain_field had started neutralizing
        # still split these rows in two (measured).
        from factlog.integrations.common.porcelain import porcelain_field

        def _f(value: object) -> str:
            return porcelain_field(str(value))

        for r in results:
            print(
                f"result\t{r.status}\t{_f(r.entry_id)}\t{_f(r.ledger)}\t{_f(r.reason)}"
            )
        print(f"backfilled\t{len(written)}")
        print(f"refused\t{len(refused)}")
        print(f"errors\t{len(errors)}")
        print(f"dry_run\t{'1' if dry_run else '0'}")
        print(f"target\t{_f(target / 'sources')}")
        return 1 if errors else 0

    verb = "Would backfill" if dry_run else "Backfilling"
    print(
        f"\n{verb} provenance ledgers for front-matter-only OpenAlex papers in KB: {target}\n"
    )
    if not results:
        print(
            "No front-matter-only OpenAlex papers found (every OpenAlex paper the check "
            "commands can read already has a provenance ledger)."
        )
        return 0

    if written:
        label = "Would get a provenance ledger:" if dry_run else "Provenance ledger written:"
        print(label)
        for r in written:
            arrow = "would write" if dry_run else "wrote"
            print(f"  ✎ {r.entry_id}  ({arrow} {r.ledger})")
    if refused:
        print("\nRefused (front matter cannot supply a truthful ledger):")
        for r in refused:
            print(f"  ✗ {r.entry_id}: {r.reason}")
    if errors:
        # Not only sidecar faults: a source outside the provenance root can hold no
        # ledger at all (#112), and it is reported here rather than skipped.
        print("\nCould not backfill:")
        for r in errors:
            print(f"  ✗ {r.entry_id}: {r.reason}")

    print("\nSummary:")
    print(f"  {'Would backfill:' if dry_run else 'Backfilled:':<18}{len(written)}")
    print(f"  {'Refused:':<18}{len(refused)}")
    print(f"  {'Errors:':<18}{len(errors)}")
    if written and not dry_run:
        print(
            "\nNext step: a paper OpenAlex flags as retracted among these can now be "
            "acknowledged with 'factlog openalex-acknowledge-retraction --id <id>'."
        )
    return 1 if errors else 0


def cmd_openalex_cite(args: argparse.Namespace) -> int:
    """Show the citation neighbourhood of a source already in the KB (spec §5.2).

    Traversal uses OpenAlex's `cites:`/`cited_by:` filters (1 credit each), not a
    search (10) — the `cited_by_api_url` field the plan assumed no longer exists
    (#51). Nothing is written unless --auto-import is given.
    """
    from datetime import datetime, timezone

    from factlog.integrations.openalex.api_client import (
        OpenAlexConnectionError,
        OpenAlexError,
    )
    from factlog.integrations.openalex.importer import (
        import_works,
        parse_works,
        resolve_work_id,
    )

    prepared = _openalex_prepare(args, "openalex-cite")
    if prepared is None:
        return 1
    target, config = prepared

    porcelain = getattr(args, "porcelain", False)
    dry_run = getattr(args, "dry_run", False)
    if not _openalex_check_limit(args, config, "openalex-cite"):
        return 1

    client = _make_openalex_client(config)
    try:
        work_id = resolve_work_id(target, args.for_slug)
        directions = ("citing", "cited") if args.direction == "both" else (args.direction,)
        found = {}
        for direction in directions:
            fetch = client.citing_works if direction == "citing" else client.cited_works
            page = fetch(work_id, limit=args.limit)
            works = parse_works(page.results)
            label = "cite it" if direction == "citing" else "it cites"
            _openalex_show_results(
                works, page.count, porcelain=porcelain, scope=direction,
                heading=f"\nWorks that {label} ({page.count} total, showing {len(works)}):\n",
            )
            for work in works:
                found.setdefault(work.openalex_id, work)
    except OpenAlexConnectionError as exc:
        print(f"factlog openalex-cite: {exc}", file=sys.stderr)
        return 2
    except OpenAlexError as exc:
        print(f"factlog openalex-cite: {exc}", file=sys.stderr)
        return 1

    warning = _openalex_budget_warning(client)
    if not args.auto_import:
        if not porcelain:
            print("\nNothing written. Re-run with --auto-import to import these works.")
        if warning:
            print(warning, file=sys.stderr)
        return 0

    imported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    report = import_works(
        found.values(), target=target, config=config, imported_at=imported_at, dry_run=dry_run
    )
    return _openalex_finish(report, target, dry_run=dry_run, porcelain=porcelain, warning=warning)


def cmd_export(args: argparse.Namespace) -> int:
    """Export source provenance as BibTeX or CSL-JSON for citing in LaTeX/Word.

    Reads the YAML front matter each source records and emits one entry per
    bibliographic source (annotation companion files and sources without
    provenance are skipped), in deterministic filename order. Read-only.
    """
    import json
    from pathlib import Path

    from factlog.bibtex import is_annotation_source, read_front_matter, safe_cite_key, to_bibtex
    from factlog.csl import to_csl

    if getattr(args, "bibtex", False) == getattr(args, "csl", False):
        # neither or both
        print("factlog export: specify exactly one format (--bibtex or --csl)", file=sys.stderr)
        return 2

    target_str, _ = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if not _require_kb(target, "export"):
        return 1

    from factlog import common as _c  # noqa: PLC0415

    sources = []
    # walk_source_dir, not glob("*.md"): the glob saw only the TOP level, so a source in
    # a subdirectory -- which `factlog sources` lists and `sync` extracts from -- was
    # dropped from the citation list with exit 0 and no warning (#223).
    skipped: list[str] = []
    seen_keys: dict[str, str] = {}
    # source_files walks BOTH source roots (sources/ and runs/sources/), the same set
    # `factlog sources` lists -- globbing sources/ alone dropped a source `factlog
    # sources` shows from the citation list with exit 0 and no warning (#223). An ingest
    # conversion under runs/sources/ carries an HTML provenance comment, not YAML front
    # matter, so read_front_matter returns {} and it is reported as skipped rather than
    # dropped silently; a hand-placed .md there with real front matter IS cited.
    for path in _c.source_files(target):
        if path.suffix.lower() != ".md":
            continue
        rel = path.relative_to(target).as_posix()
        fm = read_front_matter(path)
        if not fm:
            skipped.append(f"{rel} (no YAML front matter)")
            continue
        if is_annotation_source(fm):
            continue
        if not (fm.get("zotero_key") or fm.get("title")):
            skipped.append(f"{rel} (front matter has neither title nor zotero_key)")
            continue
        # Dedup on the key that is actually EMITTED, not on the stem. BibTeX sanitizes
        # the key (safe_cite_key collapses non-ASCII to "ref"), so 한글.md and 다른이름.md
        # -- different stems -- both emit `@misc{ref,` and one silently wins in every
        # BibTeX processor. `a.b` and `a-b` collide the same way. CSL keeps the stem as
        # the id, so there the stem IS the emitted key.
        emitted = safe_cite_key(path.stem) if not args.csl else path.stem
        if emitted in seen_keys:
            n = 2
            while f"{emitted}-{n}" in seen_keys:
                n += 1
            print(
                f"factlog export: citation key {emitted!r} is used by {seen_keys[emitted]} "
                f"and {rel}; {rel} is exported as {emitted}-{n}",
                file=sys.stderr,
            )
            emitted = f"{emitted}-{n}"
        seen_keys[emitted] = rel
        sources.append((emitted, fm))

    for note in skipped:
        print(f"factlog export: skipped {note}", file=sys.stderr)

    if args.csl:
        text = json.dumps([to_csl(fm, stem) for stem, fm in sources], ensure_ascii=False, indent=2)
        text += "\n" if text else ""
    else:
        text = "\n".join(to_bibtex(fm, stem) for stem, fm in sources)

    if args.output:
        out = Path(args.output).expanduser()
        _atomic_write_text(out, text)
        print(f"factlog export: wrote {len(sources)} entr(y/ies) to {out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
        print(f"factlog export: {len(sources)} entr(y/ies)", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factlog", description="factlog environment and KB helpers")
    parser.add_argument("--version", action="version", version=f"factlog {__version__}")
    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser("doctor", help="verify Python and pyrewire requirements")
    doctor.set_defaults(func=cmd_doctor)

    init = sub.add_parser("init", help="scaffold an empty knowledge base layout")
    init.add_argument("--target", default=None, help="KB root to create (default: $FACTLOG_ROOT, else ~/wiki)")
    init.add_argument(
        "--activate",
        action="store_true",
        help="also make the new KB active, replacing the current one "
        "(without this, init never changes an existing active KB)",
    )
    init.set_defaults(func=cmd_init)

    setup = sub.add_parser(
        "setup",
        help="one-shot bootstrap: doctor, ensure deps, init KB, re-check",
    )
    setup.add_argument("--target", default=None, help="KB root to create (default: $FACTLOG_ROOT, else ~/wiki)")
    setup.add_argument(
        "--lang",
        default=None,
        metavar="CODE",
        help="narration language for the assistant's prose (e.g. ko, en); "
        "does not translate engine reports, CLI output, or fact data",
    )
    setup.set_defaults(func=cmd_setup)

    ingest = sub.add_parser(
        "ingest",
        help="convert binary/office file(s) (docx, pdf, ...) into text under runs/sources/",
    )
    ingest.add_argument(
        "paths",
        nargs="*",
        help="file(s) to convert; omit and pass --scan to auto-discover binaries in sources/",
    )
    ingest.add_argument(
        "--scan",
        action="store_true",
        help="auto-discover every binary file under sources/ and convert it (used by /factlog sync)",
    )
    ingest.add_argument(
        "--target",
        default=None,
        help="KB root whose runs/sources/ receives the conversions "
        "(default: the active KB set by `factlog init`/`use`, else cwd)",
    )
    ingest.add_argument(
        "--force",
        action="store_true",
        help="re-convert even when an up-to-date conversion already exists",
    )
    ingest.set_defaults(func=cmd_ingest)

    eject = sub.add_parser(
        "eject",
        help="inverse of ingest: remove a source (conversion + its facts), or just a fact",
    )
    eject.add_argument(
        "sources",
        nargs="*",
        help="source(s) to remove, named by filename, stem, or KB-relative path",
    )
    eject.add_argument(
        "--fact",
        action="append",
        nargs=3,
        metavar=("SUBJECT", "RELATION", "OBJECT"),
        help="retire one fact by its triple, leaving the source in place (repeatable)",
    )
    eject.add_argument(
        "--orphans",
        action="store_true",
        help="auto-detect and eject every orphaned source (a conversion whose "
        "original under sources/ is gone, or a cited source with no file)",
    )
    eject.add_argument(
        "--target",
        default=None,
        help="KB root (default: the active KB; see `factlog where`)",
    )
    eject.add_argument(
        "--purge",
        action="store_true",
        help="delete the matched candidate rows instead of marking them superseded",
    )
    eject.add_argument(
        "--delete-original",
        action="store_true",
        help="also delete the user's original file under sources/ (off by default)",
    )
    eject.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned changes without modifying anything",
    )
    eject.set_defaults(func=cmd_eject)

    use = sub.add_parser("use", help="set the active KB targeted by ingest/ask/sync from any directory")
    use.add_argument("target", help="knowledge base root to make active")
    use.add_argument(
        "--lang",
        default=None,
        metavar="CODE",
        help="also set the narration language for the assistant's prose (e.g. ko, en); "
        "omit to keep the current language",
    )
    use.set_defaults(func=cmd_use)

    lang = sub.add_parser(
        "lang",
        help="get or set the assistant's narration language (prose only; not engine output)",
    )
    lang.add_argument(
        "code",
        nargs="?",
        default=None,
        metavar="CODE",
        help="language code to set (e.g. ko, en); omit to print the current setting",
    )
    lang.set_defaults(func=cmd_lang)

    where = sub.add_parser("where", help="print the active KB and where it was resolved from")
    where.add_argument(
        "--porcelain",
        action="store_true",
        help="print only the active KB root (absolute path, one line, no label) for scripts",
    )
    where.set_defaults(func=cmd_where)

    sources = sub.add_parser("sources", help="list registered sources (original, conversion, fact count)")
    sources.add_argument("--target", default=None, help="KB root (default: the active KB; see `factlog where`)")
    sources.set_defaults(func=cmd_sources)

    provenance = sub.add_parser(
        "provenance",
        aliases=["trace"],
        help="trace a fact to its source(s): paths, status, confidence, note, staleness",
    )
    provenance.add_argument(
        "terms",
        nargs="+",
        metavar="TERM",
        help="SUBJECT [RELATION [OBJECT]] prefix; use '-' to wildcard a position",
    )
    provenance.add_argument("--target", default=None, help="KB root (default: the active KB; see `factlog where`)")
    provenance.set_defaults(func=cmd_provenance)

    vocab = sub.add_parser(
        "vocab",
        help="list the KB vocabulary: entity and relation names with counts",
    )
    vocab.add_argument("--entities", action="store_true", help="show only entities")
    vocab.add_argument("--relations", action="store_true", help="show only relations")
    vocab.add_argument("--all", action="store_true", help="include non-engine names (candidate/needs_review/superseded); default: engine facts")
    vocab.add_argument("--target", default=None, help="KB root (default: the active KB; see `factlog where`)")
    vocab.set_defaults(func=cmd_vocab)

    search = sub.add_parser(
        "search",
        help="find facts by a case-insensitive substring across subject/relation/object",
    )
    search.add_argument("term", help="substring to match (quote if it contains spaces)")
    search.add_argument("--target", default=None, help="KB root (default: the active KB; see `factlog where`)")
    search.set_defaults(func=cmd_search)

    review = sub.add_parser(
        "review",
        help="list facts awaiting a human decision (candidate/needs_review)",
    )
    review.add_argument(
        "--status",
        choices=["candidate", "needs_review"],
        default=None,
        help="show only this pending status (default: both)",
    )
    review.add_argument("--target", default=None, help="KB root (default: the active KB; see `factlog where`)")
    review.set_defaults(func=cmd_review)

    for _name, _func, _verb in (("accept", cmd_accept, "accepted"), ("reject", cmd_reject, "superseded")):
        _p = sub.add_parser(
            _name,
            help=f"set matching pending fact(s) to {_verb} (use `factlog review` to see the queue)",
        )
        _p.add_argument(
            "terms",
            nargs="+",
            metavar="TERM",
            help="SUBJECT [RELATION [OBJECT]] prefix; use '-' to wildcard a position",
        )
        _p.add_argument("--dry-run", action="store_true", help="print the planned changes without modifying anything")
        _p.add_argument("--target", default=None, help="KB root (default: the active KB; see `factlog where`)")
        _p.set_defaults(func=_func)

    amend = sub.add_parser(
        "amend",
        help="correct a fact's subject/relation/object/note (durable: updates runs/*.json too)",
    )
    amend.add_argument("subject", help="the fact's current subject")
    amend.add_argument("relation", help="the fact's current relation")
    amend.add_argument("object", help="the fact's current object")
    amend.add_argument("--set-subject", default=None, metavar="X", help="new subject")
    amend.add_argument("--set-relation", default=None, metavar="Y", help="new relation")
    amend.add_argument("--set-object", default=None, metavar="Z", help="new object")
    amend.add_argument("--set-note", default=None, metavar="TEXT", help="new note (may be empty to clear)")
    amend.add_argument("--accept", action="store_true", help="also promote the amended fact to accepted")
    amend.add_argument("--dry-run", action="store_true", help="print the planned changes without modifying anything")
    amend.add_argument("--target", default=None, help="KB root (default: the active KB; see `factlog where`)")
    amend.set_defaults(func=cmd_amend)

    ignore = sub.add_parser(
        "ignore",
        help="manage policy/sync-ignore.md: glob patterns of sources excluded from sync",
    )
    ignore.add_argument("patterns", nargs="*", help="glob/path pattern(s) to add (omit to list)")
    ignore.add_argument("--remove", action="store_true", help="remove the given pattern(s) instead of adding")
    ignore.add_argument("--target", default=None, help="KB root (default: the active KB; see `factlog where`)")
    ignore.set_defaults(func=cmd_ignore)

    status = sub.add_parser("status", help="summarise KB state (sources, facts, vocabulary, conflicts, engine)")
    status.add_argument("--target", default=None, help="KB root (default: the active KB; see `factlog where`)")
    status.set_defaults(func=cmd_status)

    export = sub.add_parser("export", help="export source provenance as BibTeX/CSL-JSON")
    _fmt = export.add_mutually_exclusive_group()
    _fmt.add_argument("--bibtex", action="store_true", help="emit BibTeX")
    _fmt.add_argument("--csl", action="store_true", help="emit CSL-JSON (Pandoc/Zotero/Word)")
    export.add_argument(
        "--target", default=None, help="KB root (default: the active KB; see `factlog where`)"
    )
    export.add_argument(
        "--output", "-o", default=None, help="write to FILE instead of stdout"
    )
    export.set_defaults(func=cmd_export)

    zimport = sub.add_parser(
        "zotero-import",
        help="import Zotero bibliographic metadata into sources/ (phase 1, Local API)",
    )
    _sel = zimport.add_mutually_exclusive_group(required=True)
    _sel.add_argument("--collection", help="Zotero collection name to import")
    _sel.add_argument("--tag", help="Zotero tag to import")
    _sel.add_argument("--items", help="comma-separated Zotero item keys to import")
    zimport.add_argument(
        "--target", default=None, help="KB root (default: the active KB; see `factlog where`)"
    )
    zimport.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be imported without creating any files",
    )
    zimport.add_argument(
        "--pdf",
        action="store_true",
        help="also fetch each item's PDF attachments into sources/ and convert them to text",
    )
    zimport.add_argument(
        "--annotations",
        action="store_true",
        help="also import each item's highlights and notes into sources/<stem>-notes.md",
    )
    zimport.add_argument(
        "--porcelain",
        action="store_true",
        help="machine-readable output (tab-separated field/value counts) for scripts",
    )
    zimport.set_defaults(func=cmd_zotero_import)

    def _openalex_common(p):
        p.add_argument(
            "--target", default=None, help="KB root (default: the active KB; see `factlog where`)"
        )
        p.add_argument(
            "--dry-run", action="store_true",
            help="show what would be imported without creating any files",
        )
        p.add_argument(
            "--porcelain", action="store_true",
            help="machine-readable output (tab-separated field/value counts) for scripts",
        )
        return p

    oa_search = _openalex_common(sub.add_parser(
        "openalex-search",
        help="search OpenAlex and import chosen works into sources/ (costs 10 credits/search)",
    ))
    oa_search.add_argument("--query", required=True, help="search text (required)")
    oa_search.add_argument("--year", help="publication year or range, e.g. 2023 or 2020-2025")
    oa_search.add_argument("--type", help="work type filter, e.g. article, book, dataset")
    oa_search.add_argument(
        "--limit", type=int, default=None,
        help="number of results (default: 25, max: 200; cost is the same either way)",
    )
    oa_search.add_argument(
        "--all", action="store_true",
        help="import every result without prompting (needed when stdin is not a terminal)",
    )
    oa_search.set_defaults(func=cmd_openalex_search)

    oa_import = _openalex_common(sub.add_parser(
        "openalex-import", help="import one OpenAlex work by id or DOI into sources/ (free)",
    ))
    _oa_sel = oa_import.add_mutually_exclusive_group(required=True)
    _oa_sel.add_argument("--work-id", help="OpenAlex Work ID, e.g. W2741809807")
    _oa_sel.add_argument("--doi", help="DOI, e.g. 10.1007/s10462-023-10448-w")
    oa_import.set_defaults(func=cmd_openalex_import)

    oa_cite = _openalex_common(sub.add_parser(
        "openalex-cite", help="show the citation neighbourhood of a source already in the KB",
    ))
    oa_cite.add_argument(
        "--for", dest="for_slug", required=True, help="factlog source slug to traverse from"
    )
    oa_cite.add_argument(
        "--direction", choices=("citing", "cited", "both"), default="citing",
        help="citing: works that cite it; cited: works it cites; both (default: citing)",
    )
    oa_cite.add_argument("--limit", type=int, default=None, help="number of results per direction")
    oa_cite.add_argument(
        "--auto-import", action="store_true", help="import the listed works (use with care)"
    )
    oa_cite.set_defaults(func=cmd_openalex_cite)

    oa_refresh = sub.add_parser(
        "openalex-refresh",
        help="report OpenAlex records whose doi/work_type/journal or retraction has "
             "drifted from OpenAlex's current metadata; with --auto-update record the "
             "new venue/identifier fields in the ledger (never touches sources/*.md, "
             "never writes retraction). Free: GET /works/{id} costs 0 credits.",
    )
    oa_refresh.add_argument(
        "--target", default=None, help="KB root (default: the active KB; see `factlog where`)"
    )
    oa_refresh.add_argument(
        "--older-than", type=float, default=30.0, metavar="DAYS",
        help="skip records checked within DAYS (read from the check-log, not the "
             "source files); default 30. Use 0 to force a re-check of every record.",
    )
    oa_refresh.add_argument(
        "--auto-update", action="store_true",
        help="record the new doi, work_type and journal in each changed work's "
             "provenance ledger. Never touches the original .md, never rewrites any "
             "other ledger field, never writes retraction (surfaced for human review "
             "under both modes), and never rewrites the ledger key for a superseded id. "
             "Without this flag, nothing is written but the check-log timestamp.",
    )
    oa_refresh.add_argument(
        "--porcelain", action="store_true",
        help="machine-readable output (tab-separated rows) for scripts; progress stays "
             "on stderr",
    )
    oa_refresh.set_defaults(func=cmd_openalex_refresh)

    oa_ack = sub.add_parser(
        "openalex-acknowledge-retraction",
        help="record a human decision about one OpenAlex record's retraction, so "
             "openalex-refresh stops repeating it. Live-queries OpenAlex for the one "
             "--id (0 credits) and writes its current flag (clearing it when OpenAlex no "
             "longer reports a retraction); never touches sources/*.md",
    )
    oa_ack.add_argument(
        "--target", default=None,
        help="KB root (default: the active KB; see `factlog where`)",
    )
    oa_ack.add_argument(
        "--id", required=True,
        help="the single OpenAlex work id to acknowledge (e.g. W2741809807 or an "
             "openalex.org/W... URL). No --all, no wildcard: the blast radius is one id, "
             "chosen by a human.",
    )
    oa_ack.add_argument(
        "--yes", action="store_true",
        help="skip the confirmation prompt (only ever paired with --id). Required to run "
             "without a terminal; without it a non-interactive run refuses and writes "
             "nothing. It may record a retraction, never clear one: clearing silences a "
             "recorded signal and is refused unless a human confirms it interactively.",
    )
    oa_ack.set_defaults(func=cmd_openalex_acknowledge_retraction)

    oa_backfill = sub.add_parser(
        "openalex-backfill-provenance",
        help="give every front-matter-only OpenAlex paper (imported before #84) the "
             "provenance ledger its front matter implies, so a retraction OpenAlex flags "
             "can be acknowledged. No network, never touches sources/*.md; --dry-run previews",
    )
    oa_backfill.add_argument(
        "--target", default=None,
        help="KB root (default: the active KB; see `factlog where`)",
    )
    oa_backfill.add_argument(
        "--dry-run", action="store_true",
        help="name the ids that would get a ledger and the ids refused (missing "
             "imported_at, or an openalex_is_retracted that is not a YAML boolean) without "
             "writing anything. A preview cannot report a write that would fail — an "
             "unwritable source-provenance/ shows up only on the real run",
    )
    oa_backfill.add_argument(
        "--porcelain", action="store_true",
        help="machine-readable output (tab-separated rows) for scripts",
    )
    oa_backfill.set_defaults(func=cmd_openalex_backfill_provenance)

    def _arxiv_common(p):
        p.add_argument(
            "--target", default=None, help="KB root (default: the active KB; see `factlog where`)"
        )
        p.add_argument(
            "--dry-run", action="store_true",
            help="show what would be imported without creating any files",
        )
        p.add_argument(
            "--porcelain", action="store_true",
            help="machine-readable output (tab-separated field/value counts) for scripts",
        )
        return p

    ax_import = _arxiv_common(sub.add_parser(
        "arxiv-import",
        help="import arXiv papers by id into sources/ (free; up to 100 ids per run)",
    ))
    ax_import.add_argument(
        "--id", action="append", required=True, dest="id",
        help="arXiv id to import, repeatable; pin a version inline, e.g. 2311.09277v2 "
             "(no separate --version flag). Up to 100 ids per run.",
    )
    ax_import.set_defaults(func=cmd_arxiv_import)

    def _pubmed_common(p):
        p.add_argument(
            "--target", default=None, help="KB root (default: the active KB; see `factlog where`)"
        )
        p.add_argument(
            "--dry-run", action="store_true",
            help="show what would be imported without creating any files",
        )
        p.add_argument(
            "--porcelain", action="store_true",
            help="machine-readable output (tab-separated field/value counts) for scripts",
        )
        return p

    pm_import = _pubmed_common(sub.add_parser(
        "pubmed-import",
        help="import PubMed records by PMID into sources/ (free; up to 200 ids per run)",
    ))
    pm_import.add_argument(
        "--pmid", action="append", required=True, dest="pmid",
        help="PubMed PMID to import, repeatable (batch), e.g. 32738937. A 'pmid:' "
             "prefix is accepted. Up to 200 ids per run; the client paces requests.",
    )
    pm_import.set_defaults(func=cmd_pubmed_import)

    ax_search = _arxiv_common(sub.add_parser(
        "arxiv-search",
        help="search arXiv and import chosen works into sources/ (free; --all or a TTY prompt)",
    ))
    ax_search.add_argument(
        "--show-query", action="store_true",
        help="print the exact search_query that would be sent and exit, without "
             "spending a request. `--dry-run` searches and reports what it would "
             "import, like openalex-search.",
    )
    ax_search.add_argument(
        "--query", required=True,
        help='search text (required). A bare multi-word query is searched as a '
             'phrase — factlog sends all:"your words" — because arXiv otherwise '
             "matches the words loosely and returns many times more results. "
             "Override with a field prefix (ti:, au:, abs:), your own quotes, or a "
             "boolean (AND/OR/ANDNOT). See --dry-run for the exact query sent.",
    )
    ax_search.add_argument(
        "--category", action="append", dest="category",
        help="restrict to an arXiv category, repeatable, e.g. cs.CL (AND-combined)",
    )
    ax_search.add_argument(
        "--year", help="submission year or range, e.g. 2023 or 2020-2025",
    )
    ax_search.add_argument(
        "--limit", type=int, default=None,
        help="number of results (default: 25, max: 200)",
    )
    ax_search.add_argument(
        "--sort", choices=("submitted", "updated", "relevance"), default=None,
        help="sort order: submitted (newest first), updated, or relevance",
    )
    ax_search.add_argument(
        "--all", action="store_true",
        help="import every result without prompting (needed when stdin is not a terminal)",
    )
    ax_search.set_defaults(func=cmd_arxiv_search)
    ax_check = sub.add_parser(
        "arxiv-check-versions",
        help="report arXiv records whose version is behind arXiv's latest; with "
             "--auto-update record the new version-tracking fields in the ledger "
             "(never touches sources/*.md)",
    )
    ax_check.add_argument(
        "--target", default=None, help="KB root (default: the active KB; see `factlog where`)"
    )
    ax_check.add_argument(
        "--older-than", type=float, default=30.0, metavar="DAYS",
        help="skip records checked within DAYS (read from the check-log, not the "
             "source files); default 30. Use 0 to force a re-check of every record.",
    )
    ax_check.add_argument(
        "--auto-update", action="store_true",
        help="record the new version, last_updated and comment in each changed "
             "paper's provenance ledger. Never touches the original .md, never "
             "rewrites any other ledger field, and never absorbs a withdrawal "
             "(surfaced for human review under both modes). Without this flag, "
             "nothing is written but the check-log timestamp.",
    )
    ax_check.add_argument(
        "--porcelain", action="store_true",
        help="machine-readable output (tab-separated rows) for scripts; progress "
             "and ETA stay on stderr",
    )
    ax_check.set_defaults(func=cmd_arxiv_check_versions)

    ax_ack = sub.add_parser(
        "arxiv-acknowledge-withdrawal",
        help="record a human decision about one arXiv record's withdrawal, so "
             "arxiv-check-versions stops repeating it. Live-queries arXiv for the "
             "one --id and writes its current value (clearing the field when arXiv "
             "no longer reports a withdrawal); never touches sources/*.md",
    )
    ax_ack.add_argument(
        "--target", default=None,
        help="KB root (default: the active KB; see `factlog where`)",
    )
    ax_ack.add_argument(
        "--id", required=True,
        help="the single arXiv id to acknowledge (base id; a version pin like "
             "2311.09277v2 is rejected — identity is the base id). No --all, no "
             "wildcard: the blast radius is one id, chosen by a human.",
    )
    ax_ack.add_argument(
        "--yes", action="store_true",
        help="skip the confirmation prompt (only ever paired with --id). Required to "
             "run without a terminal; without it a non-interactive run refuses and "
             "writes nothing. It may record a withdrawal, never clear one: clearing "
             "silences a recorded signal and is refused unless a human confirms it "
             "interactively.",
    )
    ax_ack.set_defaults(func=cmd_arxiv_acknowledge_withdrawal)

    ax_backfill = sub.add_parser(
        "arxiv-backfill-provenance",
        help="give every front-matter-only arXiv paper (imported before #82) the "
             "provenance ledger its front matter implies, so its withdrawal can be "
             "acknowledged. No network, never touches sources/*.md; --dry-run previews",
    )
    ax_backfill.add_argument(
        "--target", default=None,
        help="KB root (default: the active KB; see `factlog where`)",
    )
    ax_backfill.add_argument(
        "--dry-run", action="store_true",
        help="name the ids that would get a ledger and the ids refused (missing "
             "imported_at, or an unreadable arxiv_version) without writing anything. "
             "A preview cannot report a write that would fail — an unwritable "
             "source-provenance/ shows up only on the real run",
    )
    ax_backfill.add_argument(
        "--porcelain", action="store_true",
        help="machine-readable output (tab-separated rows) for scripts",
    )
    ax_backfill.set_defaults(func=cmd_arxiv_backfill_provenance)

    pm_search = _pubmed_common(sub.add_parser(
        "pubmed-search",
        help="search PubMed and list the results, surfacing a suspicious zero "
             "(free; a nonexistent MeSH term or bad field tag is caught, not read "
             "as an empty result)",
    ))
    pm_search.add_argument(
        "--show-query", action="store_true",
        help="print the exact esearch term that would be sent and exit, without "
             "spending a request. Different from --dry-run, which searches and "
             "declines to write.",
    )
    pm_search.add_argument(
        "--query", required=True,
        help="PubMed query in PubMed syntax (required). A bare multi-word query is "
             "sent verbatim and PubMed applies Automatic Term Mapping; the listing "
             "shows how PubMed read it (QueryTranslation), and --show-query shows the "
             "composed term. Quote a phrase yourself to search it literally. A "
             "[field tag] PubMed does not know is rejected before a request is sent.",
    )
    pm_search.add_argument(
        "--year", help="publication year or range, e.g. 2023 or 2020-2025",
    )
    pm_search.add_argument(
        "--mesh", action="append", dest="mesh",
        help="restrict to a MeSH term, repeatable (AND-combined). The term is sent "
             "as [MeSH Terms]; a term PubMed cannot map is surfaced, not swallowed.",
    )
    pm_search.add_argument(
        "--limit", type=int, default=None,
        help="number of results (default: 25, max: 200)",
    )
    pm_search.add_argument(
        "--all", action="store_true",
        help="import every result without prompting (needed when stdin is not a "
             "terminal); imported records still pass the sync -> review -> accept gate",
    )
    pm_search.set_defaults(func=cmd_pubmed_search)

    # pubmed-mesh proposes candidate aliases from a paper's MeSH terms; it never
    # writes, so it takes --target and --porcelain but NOT --dry-run (there is
    # nothing to preview) — hence the args are added directly rather than via
    # _pubmed_common, which carries --dry-run for the import path.
    pm_mesh = sub.add_parser(
        "pubmed-mesh",
        help="propose canonical-alias candidates from a KB paper's PubMed MeSH "
             "terms, split major/minor (proposals only; nothing is written)",
    )
    pm_mesh.add_argument(
        "--for", dest="for_slug", required=True,
        help="the factlog source slug whose PubMed MeSH terms to propose (the PMID "
             "is read from the source's provenance ledger)",
    )
    pm_mesh.add_argument(
        "--target", default=None, help="KB root (default: the active KB; see `factlog where`)"
    )
    pm_mesh.add_argument(
        "--porcelain", action="store_true",
        help="machine-readable output (tab-separated rows) for scripts",
    )
    pm_mesh.set_defaults(func=cmd_pubmed_mesh)

    pm_refresh = sub.add_parser(
        "pubmed-refresh",
        help="report PubMed records whose doi/journal or retraction status has drifted from "
             "what PubMed now serves; with --auto-update record the new identifier/journal "
             "fields in the ledger (never touches sources/*.md, never writes retraction). "
             "Estimates the run time first; --dry-run previews.",
    )
    pm_refresh.add_argument(
        "--target", default=None, help="KB root (default: the active KB; see `factlog where`)"
    )
    pm_refresh.add_argument(
        "--older-than", type=float, default=30.0, metavar="DAYS",
        help="skip records checked within DAYS (read from the check-log, not the source "
             "files); default 30. Use 0 to force a re-check of every record.",
    )
    pm_refresh.add_argument(
        "--only-flagged", action="store_true",
        help="re-check only records the KB already records as retracted — the cheap way to "
             "catch a retraction PubMed has since reversed without re-fetching the library.",
    )
    pm_refresh.add_argument(
        "--auto-update", action="store_true",
        help="record the new doi and journal in each changed record's provenance ledger. "
             "Never touches the original .md, never rewrites any other ledger field, and "
             "never writes retraction (surfaced for human review under both modes; "
             "acknowledge it with pubmed-acknowledge-retraction). Without this flag, "
             "nothing is written but the check-log timestamp.",
    )
    pm_refresh.add_argument(
        "--dry-run", action="store_true",
        help="show which records would be refreshed and the estimated run time without "
             "hitting the network or writing anything (not even the check-log).",
    )
    pm_refresh.add_argument(
        "--porcelain", action="store_true",
        help="machine-readable output (tab-separated rows) for scripts; the estimate and "
             "progress stay on stderr",
    )
    pm_refresh.set_defaults(func=cmd_pubmed_refresh)

    pm_ack = sub.add_parser(
        "pubmed-acknowledge-retraction",
        help="record a human decision about one PubMed record's retraction, so "
             "pubmed-refresh stops repeating it. Live-queries PubMed (efetch) for the one "
             "--id and writes its current status (clearing it when PubMed no longer reads "
             "as retracted); never touches sources/*.md",
    )
    pm_ack.add_argument(
        "--target", default=None,
        help="KB root (default: the active KB; see `factlog where`)",
    )
    pm_ack.add_argument(
        "--id", required=True,
        help="the single PMID to acknowledge (e.g. 32738937; a 'pmid:' prefix is "
             "accepted). No --all, no wildcard: the blast radius is one id, chosen by a "
             "human.",
    )
    pm_ack.add_argument(
        "--yes", action="store_true",
        help="skip the confirmation prompt (only ever paired with --id). Required to run "
             "without a terminal; without it a non-interactive run refuses and writes "
             "nothing. It may record a retraction, never clear one: clearing silences a "
             "recorded signal and is refused unless a human confirms it interactively.",
    )
    pm_ack.set_defaults(func=cmd_pubmed_acknowledge_retraction)

    pm_backfill = sub.add_parser(
        "pubmed-backfill-provenance",
        help="give every front-matter-only PubMed paper (imported before provenance "
             "ledgers existed) the provenance ledger its front matter implies, so a "
             "retraction PubMed flags can be acknowledged. No network, needs no NCBI email, "
             "never touches sources/*.md; --dry-run previews",
    )
    pm_backfill.add_argument(
        "--target", default=None,
        help="KB root (default: the active KB; see `factlog where`)",
    )
    pm_backfill.add_argument(
        "--dry-run", action="store_true",
        help="name the ids that would get a ledger and the ids refused (missing "
             "imported_at, or a pubmed_retracted that is not a YAML boolean) without "
             "writing anything. A preview cannot report a write that would fail — an "
             "unwritable source-provenance/ shows up only on the real run",
    )
    pm_backfill.add_argument(
        "--porcelain", action="store_true",
        help="machine-readable output (tab-separated rows) for scripts",
    )
    pm_backfill.set_defaults(func=cmd_pubmed_backfill_provenance)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows console defaults to the legacy code page (cp949); force UTF-8 so
    # Korean output (e.g. ingest filenames) isn't mangled. No-op elsewhere.
    if sys.platform == "win32":
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(encoding="utf-8")
            except (AttributeError, ValueError, OSError):
                pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except Exception as exc:
        # A library-level FactlogError (raised by common's loaders) becomes the
        # legacy "message to stderr, exit 1". Resolve the class lazily so it still
        # matches after a command reloads the common module. Anything else
        # propagates unchanged.
        from factlog.common import FactlogError

        if isinstance(exc, FactlogError):
            print(str(exc), file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    raise SystemExit(main())
