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

from factlog import __version__, ingest, literal_types
from factlog import config as factlog_config
from factlog.common import FACT_HEADER, _atomic_write_text

MIN_PYTHON = (3, 11)
MIN_PYREWIRE = (1, 0, 3)  # bundles wirelog v0.52.0 with \" escape support (wirelog#924)


# _atomic_write_text (temp + os.replace) now lives in factlog.common so
# compile_facts.py can write accepted.dl atomically too. Re-exported here
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

    return checks


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
structure better than prose strings: date(2030,1), date(2030,1,15),
number(2.5), ordinal(3), amount(100,"억"). Keep entity objects as plain names.
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
    "policy/attribute-relations.md": """\
# Attribute (literal-valued) relations
#
# List relation names whose OBJECT is a literal value (a date, number, ordinal,
# ...) rather than a first-class entity. One relation NAME per line; '#' comment
# lines and '-' bullets are allowed; quote a name containing spaces in backticks.
#
# Objects of these relations are kept OUT of the entity set (so they do not show
# up as entities, path nodes, or count subjects) but remain valid, verifiable
# relation-query objects. Leave this file with no declarations if every object
# is a first-class entity.
#
# Example (remove the leading '# ' to activate):
# operates_since
# ranked
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
#   number   1,000 / 3.5          -> fixed-point int64, scaled ×1000 (3 decimals,
#                                    positive only); thresholds in scaled units
#                                    (e.g. `V >= 2.0` -> `V >= 2000`)
#   ordinal  rank 3 / 3rd         -> int rank
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
# (re-extraction), `factlog ingest --scan`, coverage gap reporting, and
# `/factlog ask` wiki exploration — even when modified. Their already-merged
# facts are KEPT (use `factlog eject` to remove those). Manage with
# `factlog ignore [--remove] <pattern>`.
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
    # Empty fact ledger: the header alone IS the schema contract tools/validate.py
    # checks, so it is built from FACT_HEADER rather than retyped (#327). Without
    # it a fresh KB failed validate with "missing facts/candidates.csv"; with it the
    # file loads as zero rows, which is exactly how the absent file was treated.
    "facts/candidates.csv": ",".join(FACT_HEADER) + "\n",
    # Human-review ledger. The four section headings are a standing contract that
    # tools/validate.py requires unconditionally, so `init` must lay them down —
    # otherwise a KB where no fact of a given class has come up yet can never pass
    # validate (#327). Headings are byte-identical to what
    # merge_candidates.decision_section() emits, so `sync` fills these sections
    # instead of appending duplicates; tests/unit/test_init_validate_clean.py pins
    # the two together. validate looks for each section by plain substring, so the
    # prose below must not repeat its own section's keyword (중복/모호/출처/충돌) —
    # otherwise the prose answers the check and a deleted heading goes unnoticed.
    "decisions/open-questions.md": """\
# Open Questions

`/factlog sync` 가 사람의 판단이 필요하다고 표시한 후보 사실(needs_review)을 분류별로
모아 두는 파일입니다. 아래 네 섹션은 검토 계약이므로 해당 분류의 항목이 아직 하나도
없더라도 비운 채로 유지합니다(tools/validate.py 가 확인합니다).

## 중복 개념 후보

같은 대상을 다른 이름이나 다른 방향으로 가리키는 것으로 보이는 항목.

## 모호한 관계명

관계명이나 대상이 여러 가지로 읽혀 확정하기 어려운 항목.

## 출처 부족

근거 문서가 없거나, 사라진 문서를 가리키는 항목.

## 기존 내용과 충돌할 수 있는 항목

이미 확정된 사실과 어긋날 수 있어 둘 중 하나를 골라야 하는 항목.
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


_DEFAULT_KB = "~/wiki"

# Human-readable name for each rank ``resolve_root`` can report, minus ``flag``:
# a target named on the command line is never announced, so it never needs one.
# Indexed, not ``.get``-with-fallthrough. This pair of commands delegates the
# chain precisely so a rank added to ``resolve_root`` cannot be silently ignored
# here — and a fallthrough reinstates that in a quieter form, printing the raw
# token ("from localrc") inside a sentence written for these names. A KeyError
# lands on whoever adds the rank, which is the only person who can label it.
_TARGET_SOURCE_LABELS = {
    "env": "$FACTLOG_ROOT",
    "config": "the active-KB config",
    "default": f"the default {_DEFAULT_KB}",
}


def _resolve_kb_target(cli_value: str | None, command: str):
    """Resolve the KB root `init`/`setup` will scaffold, announcing an implicit one.

    ``--target`` defaulted to the literal string ``"~/wiki"`` in the parser, which
    made these the only two commands that ignored factlog's documented root
    precedence: with ``$FACTLOG_ROOT`` exported, or a KB already active, a bare
    ``factlog init`` still scaffolded a stray ``~/wiki`` — and then, before the
    activation rule above, made that invented directory the global default (#356).

    Same order as every other command, minus the cwd fallback: a bare ``init``
    scattering a KB layout across whatever directory the user happens to stand in
    would be a worse default than the one it replaces, so ``~/wiki`` stays the
    last resort. A target that was not spelled out is printed with its source —
    an implicit target is only safe if the user can see which one it was.

    "Same order" is enforced rather than asserted: the chain itself is
    ``factlog_config.resolve_root``, which the config module's docstring claims
    to own for every caller, and ``~/wiki`` reaches it as the ``fallback``
    argument. A second copy here would drift without any test noticing — the
    shape of #356 itself, where these two commands were the ones ignoring the
    documented precedence. What stays local is only what is local: rejecting an
    empty ``--target``, the human labels for each source, and the cwd guard.
    """
    from pathlib import Path
    from factlog.common import FactlogError

    if cli_value is not None and not cli_value.strip():
        # Folding "" into "no --target given" made the next line print a claim
        # that was simply false, and then scaffolded somewhere the user had not
        # named. An empty value is a mistake worth reporting, not a synonym.
        raise FactlogError(f"{command}: --target was given but empty. Pass a path, or omit the flag.")

    resolved, origin = factlog_config.resolve_root(cli_value, fallback=_DEFAULT_KB)
    target = Path(resolved)

    # `_init_kb` mkdirs `<target>/sources`, so a regular file at `<target>` raised
    # a bare `NotADirectoryError` — a stack trace where a sentence belongs. Here
    # rather than after the `flag` return below, because all four sources can
    # name a file and the implicit ones are the worse crash: the traceback
    # printed *before* the "no --target given" line, leaving no clue which source
    # had chosen the path.
    #
    # `exists()` follows symlinks, so a link to a file is caught. A *broken* link
    # is deliberately not: `.resolve()` has already flattened it to a
    # nonexistent path, which `mkdir` creates happily.
    if target.exists() and not target.is_dir():
        advice = (
            "Pass --target with a directory path."
            if origin == "flag"
            else (
                f"It was chosen implicitly (from {_TARGET_SOURCE_LABELS[origin]}), not named "
                "on the command line — point that at a directory, or pass --target with a "
                "directory path."
            )
        )
        raise FactlogError(
            f"{command}: refusing to scaffold a KB at {target}, which is an existing "
            f"file, not a directory. {advice}"
        )

    if origin == "flag":
        return target

    source = _TARGET_SOURCE_LABELS[origin]

    # Keeping cwd out of the *chain* was not enough to keep it out of the
    # outcome. `factlog where --porcelain` prints cwd when nothing is configured
    # (config.py `resolve_root`), and SKILL.md tells every flow to export that
    # into $FACTLOG_ROOT — so a bare `init` in a directory of unrelated files
    # scattered a KB layout across it, which is the exact default the chain
    # excludes as worse. On main the hard-coded `~/wiki` made it impossible.
    #
    # Narrow on purpose: only when the target was *not* named on the command
    # line, resolves to the current directory, that directory already holds
    # something, and it is not already a KB. An empty directory loses nothing,
    # and re-scaffolding a real KB you happen to be standing in is idempotent.
    if target == Path.cwd().resolve() and not (target / "sources").is_dir() and any(target.iterdir()):
        raise FactlogError(
            f"{command}: refusing to scaffold a KB into the current directory ({target}), "
            "which already holds other files and is not a factlog KB. It was chosen "
            f"implicitly (from {source}), not named on the command line — pass "
            f"--target {target} if that is really what you want."
        )
    print(f"{command}: no --target given; using {target} (from {source})")
    return target


class Activation(NamedTuple):
    """What ``init``/``setup`` decided about the active-KB config, and what to say.

    Deliberately *only* the config question. An earlier version also carried a
    ``recorded`` flag that ``setup``'s closing line consulted to decide whether a
    flagless flow would reach the new KB — but that is a different question, and
    the config cannot answer it: ``$FACTLOG_ROOT`` outranks the config, so a KB
    the config does not record is still what every flagless command reaches when
    the environment names it. The closing line asks ``resolve_root`` instead (see
    ``_reach_note``), and this type no longer offers a flag that invites the
    wrong question.
    """

    write: bool
    summary: str
    hint: str | None


def _activated_line(target) -> str:
    """Confirm the write, promising the default-here behaviour only when it holds.

    The parenthetical is a claim about what flagless commands will do, and it is
    false while ``$FACTLOG_ROOT`` names something else — the config was written,
    but rank 2 still wins. In ``init`` the notes underneath corrected it; in
    ``setup`` the same string also goes out alone as a summary ``done:`` line,
    with nothing beside it. So the promise is dropped when it is not true, and
    the notes say where flagless commands actually go.

    The condition is computed, not read back. This line is rendered by
    ``_plan_activation``, *before* the write it confirms, so asking
    ``resolve_root`` there answers about the config as it stands now. On a true
    first run — no config, no ``$FACTLOG_ROOT`` — that resolver falls through to
    cwd, the comparison missed, and the promise was dropped in the one case where
    it was about to hold: the exact inverse of the rule above. What the resolver
    *will* say once the config records *target* is decided by rank 2 alone —
    ``$FACTLOG_ROOT`` when it is set, otherwise the value being written.
    """
    import os
    from pathlib import Path

    recorded = str(Path(target).expanduser().resolve())
    env = os.environ.get("FACTLOG_ROOT")
    effective = str(Path(env).expanduser().resolve()) if env else recorded
    if effective == recorded:
        return f"active-KB config set to {target} (ingest/ask/sync default here from any directory)"
    return f"active-KB config set to {target}"


def _switch_hint(target) -> str:
    """How to make the config record *target*.

    Says "record" rather than "work in": with ``$FACTLOG_ROOT`` naming the target,
    the user can already work in it, and "to work in it: factlog use <target>"
    then told them to go where they already were. What ``use`` adds in that state
    is durability, not access.
    """
    return f"to record it in the config: factlog use {target}   (or re-run with --activate)"


def _not_active_lines(current: str | None, target) -> tuple[str, str]:
    """The summary + hint printed when the KB is created but not recorded.

    The old value is always named, because the failure mode this replaces was
    losing it with nothing on screen to restore it from. Every line here is about
    the config file only — which KB is actually in force also depends on
    ``$FACTLOG_ROOT`` (see ``_env_override_note`` and ``_reach_note``). Claiming
    the target "is NOT active" while ``factlog where`` reports it as active would
    make this line contradict the one output the skill machine-reads.
    """
    if current:
        summary = f"active-KB root unchanged: {current} — {target} is not recorded in the config"
    else:
        summary = f"the config records no active KB — {target} is not recorded in it either"
    return summary, _switch_hint(target)


class _Unreadable(NamedTuple):
    """The fragments every "I would not write this config" sentence is built from.

    ``UNREADABLE`` covers two unrelated failures, and the prose was written for
    only one of them. A truncated write has bytes worth preserving and is fixed
    by repairing the file; a symlink whose target is not mounted has no bytes at
    all, holds a *pointer* rather than a root, and is fixed by mounting the
    volume or re-pointing the link. Three sentences in three places say this —
    the activation refusal, the ``--lang`` deferral note, and ``setup``'s rc-1
    closing line — so the branch lives here once instead of three times, which is
    how the first two drifted out of agreement in the first place.

    ``lost`` serves the two sites that go through with the write (``--activate``
    and ``factlog use``) rather than refusing, and it is **on a different axis
    from the other four**. Those answer *what is wrong*, for which reachability
    is the right question: a reachable-but-malformed target really does have
    bytes worth preserving and really is fixed by repairing that file. ``lost``
    answers *what will this write destroy*, and the answer is decided by
    ``is_symlink()`` alone — ``os.replace`` swaps the link itself, so a symlinked
    config loses its indirection whether the far end is unmounted, truncated, a
    directory, or unreadable by mode. Keying ``lost`` off reachability let those
    four reachable-but-unreadable classes announce "any narration language in it
    is gone" and say nothing at all about the link they had just replaced with a
    regular file.

    ``lost_root`` is that same answer for the write that goes the other way.
    ``lost`` is worded for the sites that set the **root** — what else the file
    held is the language — and ``factlog lang --force`` (#366) sets the
    **language**, where the thing at risk is the root. Reusing ``lost`` there
    would have told a user who just destroyed a truncated ``{"root": "…"}`` that
    a narration language was gone, naming the one field the command was setting
    anyway and staying silent about the path it had actually dropped. The
    symlink branch needs no mirror: ``os.replace`` swaps the link and leaves the
    far end intact, so losing the indirection is the whole loss either way.

    Scope, because the sentence above is about the fragments and not about every
    symlinked config: every caller of this reaches it only inside the
    ``config_status() == UNREADABLE`` branch, so "loses its indirection" is
    disclosed for the configs *this* module classifies as damaged. A symlink
    pointing at a **valid** config — the ordinary dotfiles arrangement, and the
    most valuable symlinked case — is ``READABLE``, never reaches here, and is
    still replaced by a regular file with nothing said about it. That is a known
    gap rather than a promise kept: it predates this branch, sits outside the
    damaged-config contract these fragments serve, and closing it means changing
    what the writing paths do, not what they say.
    """

    reason: str
    preserved: str
    cost: str
    remedy: str
    lost: str
    lost_root: str


def _unreadable() -> _Unreadable:
    path = factlog_config.config_path()
    # Two questions, two predicates — see the class docstring. What a write
    # destroys is decided by the link alone, because `os.replace` swaps the link
    # and not its target, so the indirection goes whatever the far end holds.
    if path.is_symlink():
        lost = lost_root = "the symlink is gone — it is a regular file now"
    else:
        lost = "any narration language in it is gone"
        lost_root = "any KB root it may still have held is gone"
    # What is *wrong* is decided by reachability. `config_status` classifies this
    # pair together because both mean "do not write"; only the words differ, so
    # the split is here rather than there.
    if path.is_symlink() and not path.exists():
        return _Unreadable(
            "is a symlink whose target is not reachable right now",
            "leaving the link in place",
            "replace the link with a file",
            # "mount it, re-point the link" turned two *alternatives* into a
            # procedure at the two sites that append ", then set the language …".
            # Mounting the volume and re-pointing the link exclude each other, so
            # the `or` has to live in the fragment, not in one caller's glue.
            "mount it or re-point the link",
            lost,
            lost_root,
        )
    return _Unreadable(
        "could not be read",
        "leaving its bytes untouched",
        "destroy the KB root it may still hold",
        "repair that file",
        lost,
        lost_root,
    )


def _unreadable_lines(target) -> tuple[str, str]:
    """Said when the config exists but must not be written, so it is left alone.

    Refusing here is the point: the unreadable bytes may be a truncated write of
    the user's real root — and ``write_root`` rebuilds the file from a failed
    read, so writing would replace both that root and any ``lang`` with nothing.
    A broken symlink is refused for the neighbouring reason and described in its
    own words; see ``_unreadable``.
    """
    said = _unreadable()
    return (
        f"active-KB config at {factlog_config.config_path()} {said.reason} — "
        f"{said.preserved}; {target} is not recorded in it",
        f"{said.remedy}, or overwrite it deliberately: factlog use {target}",
    )


def _env_override_note() -> str | None:
    """Warn when ``$FACTLOG_ROOT`` outranks whatever the config now says.

    The config is what these commands write, but it is only rank 3 of the
    precedence, and ``SKILL.md`` tells every flow to export ``$FACTLOG_ROOT``
    first — so "exported" is the recommended state, not an edge case. Without
    this line, a message about the config reads as a claim about the active KB
    and contradicts ``factlog where --porcelain`` in the same session.

    The comparison is env against the **config**, which is the only pair whose
    disagreement this line is about. Comparing env against the *target* instead —
    as the first version did — was wrong in both directions: it fired when env
    and config agreed and only the target differed (nothing is being overridden,
    so the note was a false alarm), and stayed silent when env genuinely
    outranked a different config root that happened to equal the target (the case
    where the user most needs to know the config is not what is in force). Call
    this *after* any write, so "what the config now says" is current.
    """
    import os
    from pathlib import Path

    env = os.environ.get("FACTLOG_ROOT")
    if not env:
        return None
    resolved = str(Path(env).expanduser().resolve())
    if resolved == factlog_config.read_root():
        return None
    return f"note: $FACTLOG_ROOT={resolved} outranks the config in this session (factlog where)"


# How `resolve_root`'s source codes are named in prose. Shared, because two
# sentences now report where a flagless command would land and they must not
# call the same rank by two different names.
_ROOT_SOURCE_LABEL = {
    "env": "$FACTLOG_ROOT",
    "config": "the active-KB config",
    "cwd": "the current directory",
}


def _reach_note(target, *, quiet_when: str | None = None) -> str | None:
    """Say where a flagless command would actually go, when it is not *target*.

    ``setup``'s closing line used to ask the ``Activation`` whether the config
    recorded the target, and phrase the next step accordingly. That is the wrong
    question: what the user is about to do is run a flagless ``/factlog sync``,
    and where that lands is ``resolve_root``'s answer — ``$FACTLOG_ROOT`` first,
    then the config, then cwd. With the environment naming the new KB, the
    config-based answer produced a closing line that was simply false.

    Returns None when a flagless command already reaches *target*, so the common
    path keeps the plain "next step" wording. Otherwise it names the KB that
    would be reached, where that came from, and both ways to redirect — including
    the fact that ``factlog use`` alone does not help while the environment wins.

    *quiet_when* suppresses the line for one resolution source. Callers that have
    already printed the same fact pass ``"config"``: beside a summary reading
    "root unchanged: /wiki — /scratch is not recorded in the config" and a hint reading
    "factlog use /scratch", a third line saying a flagless command would reach
    /wiki from the config is pure restatement — and that is #356's own
    reproduction path, so it is the case most readers meet. The closing line
    passes nothing, because there the fact has not been said yet.
    """
    effective, source = factlog_config.resolve_root()
    if effective == str(target) or source == quiet_when:
        return None
    origin = _ROOT_SOURCE_LABEL.get(source, source)
    fix = f"pass --target {target}"
    if source == "env":
        fix += f", or point $FACTLOG_ROOT at {target}"
    else:
        fix += f", or record it: factlog use {target}"
    return f"a flagless command would target {effective} (from {origin}), not {target} — {fix}"


def _config_write_or_explain(command: str, write: Callable[[], object]) -> None:
    """Run *write*, but a filesystem refusal becomes an actionable message.

    The contract this branch added is "a damaged config always has a way out",
    and every advertised exit — ``init --activate``, ``factlog use`` and
    ``factlog lang --force`` — goes through a config write. With a *directory*
    sitting at the config path every write raises ``IsADirectoryError``, so those
    exits died on the same traceback.

    *write* is a thunk rather than a fixed call because the exits do not all
    write the same field. This started as ``write_root``'s wrapper, and
    ``write_lang`` was left outside it — so the refusal that ``factlog lang``
    prints ("overwrite it deliberately: factlog lang ko --force") named a command
    that then crashed with a traceback, and a *readable* config under a
    mode-0500 config directory crashed the same way with no ``--force`` in sight
    (#366 review). The boundary belongs to the act of writing this file, not to
    one of its two fields.

    Nothing here removes what is in the way: deleting a path the user may have
    put there deliberately is not this command's call. It reports which path
    blocks the write and what to do, and exits 1 through the normal
    ``FactlogError`` boundary instead of a stack trace.

    The diagnosis is branched because a single one was worse than none. Every
    ``OSError`` used to become "something other than a regular file is in the way
    — move or remove that path": with an unwritable config *directory* (left root
    owned by an old ``sudo factlog setup``, a read-only mount) and a perfectly
    good ``config.json`` inside it, that named the user's real config file, and
    following it deleted the recorded root and ``lang`` without making the
    directory writable — the loss #356 exists to prevent, this time invited by
    the message. So the non-regular-file claim is now made only when the path
    really is one, ``EACCES``/``EROFS`` name the directory that cannot be written
    and never suggest touching the config, and out-of-space or a losing race
    stops at reporting the error rather than inventing a cause for it.

    Each branch also checks the state it names rather than inferring it from the
    errno alone — an ``EPERM`` from something other than directory permissions
    would otherwise inherit the same unverified certainty the old single message
    had. When nothing checks out, the error is reported and no cause is offered.

    "Nothing was changed" is gone for the same reason: on the ``init --activate``
    path ``_init_kb`` has already scaffolded the whole KB layout by the time this
    runs. What holds in every branch is narrower — the write is staged in a
    sibling temp file and swapped in, so a failure leaves the config path itself
    exactly as it was — and that is now all the message claims.
    """
    import errno
    import os

    from factlog.common import FactlogError

    try:
        write()
    except OSError as exc:
        path = factlog_config.config_path()
        parent = path.parent
        permission = exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS)
        if path.exists() and not path.is_file():
            cause = (
                f"Something other than a regular file is at {path} — move or remove "
                "that path, then re-run."
            )
        elif permission and parent.is_dir() and not os.access(parent, os.W_OK):
            st = parent.stat()
            detail = f"owner uid {st.st_uid}, mode {oct(st.st_mode & 0o777)}"
            if exc.errno == errno.EROFS:
                detail += ", read-only filesystem"
            cause = (
                f"The config directory {parent} is not writable ({detail}). Make it "
                f"writable — {path.name} is not the obstacle, and deleting it would "
                "drop the recorded root and lang without unblocking the write."
            )
        elif permission and not parent.is_dir():
            cause = (
                f"The directory {parent} does not exist and could not be created — "
                "check write access on the nearest existing parent, then re-run."
            )
        elif exc.errno == errno.ENOSPC:
            cause = f"The filesystem holding {parent} is out of space. Free some, then re-run."
        else:
            cause = "Re-run once that is resolved."
        raise FactlogError(
            f"{command}: cannot write the active-KB config at {path} "
            f"({exc.strerror or exc}). {cause} Nothing at that path was changed."
        ) from exc


def _write_root_or_explain(command: str, target) -> None:
    """Record *target* as the active KB through the shared write boundary.

    ``factlog_config.write_root`` is resolved when the thunk runs, not when it is
    built, so the tests that monkeypatch it still reach this handler.
    """
    _config_write_or_explain(command, lambda: factlog_config.write_root(target))


def _plan_activation(target, activate: bool | None) -> Activation:
    """Decide whether creating *target* also moves the global active-KB pointer.

    ``init``/``setup`` used to call ``write_root`` unconditionally, so creating
    one throwaway KB silently replaced whichever KB the user had been working in
    — with no confirmation and no surviving record of the old value (#356).
    Creating a KB and choosing which KB is active are separate intents, so the
    config is only written when nothing holds it yet (the first-run experience
    ``setup`` exists for), when the target is already what it names, or when the
    user asks with ``--activate``.

    *activate* is the tri-state ``--activate``/``--no-activate`` flag: True to
    always claim the config, False to never claim it (scratch KBs, scripts),
    None for the default above.

    Two states count as held even though ``read_root`` reports neither of them:

    * a recorded root that does not currently exist — an unmounted volume must
      not hand the setting back to ``init``;
    * a config file that cannot be parsed — ``read_root`` folds that into None
      like a fresh install, but the bytes may *be* the user's root, and unlike
      the unmounted case a write destroys them. Only a missing file means
      "nothing is recorded yet".

    A config that parses but records no usable root (``{"lang": "ko"}``,
    ``{"root": ""}``) is deliberately *not* held: it is understood, it holds no
    path to lose, ``resolve_root`` already treats it as empty, and ``write_root``
    preserves the sibling ``lang``. Treating it as held would deny the first-run
    experience to anyone who ran ``factlog lang`` before their first ``init``.
    """
    status = factlog_config.config_status()
    if status == factlog_config.UNREADABLE:
        if activate is True:
            # Explicitly asked for, and the only way back to a usable setting
            # from a corrupt file — but say what was destroyed rather than
            # letting it vanish quietly. The clause is worded exactly as `factlog
            # use` words it: this is the same write through the other door, and
            # the two must not disclose different amounts. What "destroyed" means
            # depends on the class, so it comes from `_unreadable`, and it is
            # read here — before the write — while the link still exists.
            return Activation(
                True,
                f"active-KB root: replaced an unreadable {factlog_config.config_path()} with {target}"
                f" ({_unreadable().lost})",
                None,
            )
        return Activation(False, *_unreadable_lines(target))

    current = factlog_config.read_root()
    already = current is not None and current == str(target)

    if already and activate is not True:
        return Activation(False, f"active-KB root unchanged: {target} (already recorded)", None)
    if activate is False:
        return Activation(False, *_not_active_lines(current, target))
    if activate is True:
        if current and not already:
            return Activation(True, f"active-KB root: {current} → {target}", None)
        return Activation(True, _activated_line(target), None)
    if current is None:
        return Activation(True, _activated_line(target), None)
    return Activation(False, *_not_active_lines(current, target))


def _apply_activation(
    command: str, target, activate: bool | None, *, defer_reach: bool = False
) -> tuple[Activation, list[str]]:
    """Run ``_plan_activation``'s decision, print it, and hand back what was said.

    Returns the plan plus the extra notes printed under it, so ``setup`` can
    repeat the *same* strings in its end-of-run summary rather than recomputing
    them — the summary used to carry the decision without the environment note
    beside it, which is how a false closing line ended up with no counter-evidence
    on screen.
    """
    plan = _plan_activation(target, activate)
    if plan.write:
        _write_root_or_explain(command, target)
    print(f"{command}: {plan.summary}")
    if plan.hint:
        print(f"  {plan.hint}")
    # After the write: the note is about what the config says *now*.
    # *defer_reach* is for callers that render the reach note themselves later.
    # `setup` does, in its closing line, and printing it here as well put the same
    # sentence on screen three times: under the decision, again in the summary,
    # and again at the end.
    candidates = (_env_override_note(),) if defer_reach else (
        _env_override_note(),
        _reach_note(target, quiet_when="config"),
    )
    notes = [note for note in candidates if note]
    for note in notes:
        print(f"  {note}")
    return plan, notes


def cmd_init(args: argparse.Namespace) -> int:
    target = _resolve_kb_target(getattr(args, "target", None), "factlog init")
    _init_kb(target)
    _apply_activation("factlog init", target, getattr(args, "activate", None))
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


def _apply_lang(normalized: str, command: str) -> str:
    """Persist an already-validated *normalized* language and return the one-line
    confirmation phrase. An empty string clears the setting. Centralised so all
    three entry points word the set/clear outcome identically.

    Goes through ``_config_write_or_explain`` for the same reason the root write
    does: this is a write of the same file, and it failed the same way. A
    directory at the config path, or a config directory the user cannot write,
    used to leave every one of the three entry points on a raw ``IsADirectoryError``
    / ``PermissionError`` — including a perfectly *readable* config, where the
    damaged-config guard above never fires. *command* names the caller so the
    message says which command could not write.
    """
    _config_write_or_explain(command, lambda: factlog_config.write_lang(normalized or None))
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
    # A config that cannot be read loses its `lang` to this write, because
    # write_root rebuilds the file from a read that returned {}. Unlike `init`,
    # `use` still goes ahead — re-pointing is the whole command, and it is the
    # advertised way out of a damaged config — but it says what it is about to
    # cost. `init --activate` reaches the same write through the other door and
    # now names the same loss in the same words (`_plan_activation`), so the two
    # entry points disclose the same thing.
    # Captured *before* the write, not just tested before it: the write replaces a
    # broken symlink with a regular file, so asking `_unreadable()` at print time
    # would describe the file this command just created instead of the link it
    # destroyed — and describe it in the words of the other class.
    replacing = _unreadable() if factlog_config.config_status() == factlog_config.UNREADABLE else None
    _write_root_or_explain("factlog use", target)
    # --lang, when given, is set (or cleared) alongside the root in the same config
    # file; when omitted the existing language is preserved by write_root, so `use`
    # never silently drops a configured narration language.
    phrase: str | None = None
    if normalized is not None:
        phrase = _apply_lang(normalized, "factlog use")
    note = "" if (target / "sources").is_dir() else "  (warning: no sources/ — not a factlog KB yet; run 'factlog init')"
    print(f"factlog use: active KB set to {target}{note}")
    if phrase is not None:
        print(f"  {phrase}")
    if replacing is not None:
        print(f"  replaced an unreadable config ({replacing.lost})")
    print(f"  config: {factlog_config.config_path()}")
    # `use` is where the hint from `init`/`setup` sends people, so it owes the same
    # disclosure they do: it writes rank 3, and rank 2 outranks it. Without this,
    # "active KB set to X" was followed by `where --porcelain` printing something
    # else — the exact contradiction fixed in `init` and in `setup`'s closing line.
    for extra in (_env_override_note(), _reach_note(target)):
        if extra:
            print(f"  {extra}")
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

    "Leaving the root untouched" holds only for a config this process could read.
    `write_lang` re-emits the whole file from `_read_config()`, which folds bad
    JSON, a non-object, and an `OSError` alike into `{}` — so on a damaged config
    the re-emitted file held the new language and nothing else, and a truncated
    `{"root": "/…/kb",` that still carried the user's root *as text* became
    unrecoverable (#366). #356 closed this for `init`/`setup` by asking
    `config_status()` first and refusing on UNREADABLE; the language write is the
    same hole through the sibling door, and is refused here in the same shape and
    the same words. MISSING still writes — a first run must be able to set a
    language before any `init` — and so does a config that parses but records no
    root, which has no path to lose.
    """
    code = getattr(args, "code", None)
    if code is None:
        # Query mode: one line, no label (empty line when unset). A read, so an
        # unreadable config is not this branch's problem: `read_lang` folds it to
        # None and prints the empty line, and adding a warning here would break
        # the porcelain contract the skill parses.
        print(factlog_config.read_lang() or "")
        return 0
    normalized, error = _normalize_lang(code)
    if error is not None:
        print(f"factlog lang: {error}", file=sys.stderr)
        return 2
    # Captured *before* the write, not merely tested before it: `--force` replaces
    # a broken symlink with a regular file, so asking `_unreadable()` at print
    # time would describe the file this command just created — the mistake
    # `factlog use` documents at its own call site.
    replacing = _unreadable() if factlog_config.config_status() == factlog_config.UNREADABLE else None
    if replacing is not None and not getattr(args, "force", False):
        # rc 1, not 0: setting the language is the *whole* of this command, so a
        # run that did not set it must not hand a script the same signal as one
        # that did — `setup --lang` exits 1 for this reason on the same refusal.
        # Nothing is written, so `preserved` is the one place these fragments are
        # literally true rather than a promise to keep.
        #
        # The retry line quotes the code the user actually typed rather than
        # `<code>`: the clear action is `factlog lang ''`, and a placeholder there
        # loses the one argument a reader would not guess.
        import shlex

        print(
            f"factlog lang: narration language NOT set: {factlog_config.config_path()} "
            f"{replacing.reason} — {replacing.preserved}, because writing it would "
            f"{replacing.cost}. {replacing.remedy[0].upper()}{replacing.remedy[1:]}, "
            f"or overwrite it deliberately: factlog lang {shlex.quote(code)} --force",
            file=sys.stderr,
        )
        return 1
    phrase = _apply_lang(normalized, "factlog lang")
    print(f"factlog lang: {phrase}")
    if replacing is not None:
        # The escape hatch says what it cost, as `factlog use` and `init
        # --activate` do for the write that goes the other way. `lost_root`, not
        # `lost`: the field this command sets is the language, so the one worth
        # naming is the root. See `_Unreadable`.
        print(f"  replaced an unreadable config ({replacing.lost_root})")
        # …and then says what that leaves behind. `factlog use` cannot reach this
        # state — it writes a replacement root in the same breath — but this exit
        # writes only the language, so the config comes out of it recording no KB
        # at all. SKILL.md opens every flow with
        # `export FACTLOG_ROOT="$(factlog where --porcelain)"`, so a `--force` run
        # from an arbitrary directory silently promotes *that directory* to the
        # active KB on the next sync. Naming the fallback is the difference
        # between a disclosed cost and a trap.
        if factlog_config.read_root() is None:
            effective, source = factlog_config.resolve_root()
            origin = _ROOT_SOURCE_LABEL.get(source, source)
            print(
                f"  the config now records no KB root — a flagless command would target "
                f"{effective} (from {origin}); record one with: factlog use <kb>"
            )
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
            if p.is_file() and not p.name.startswith("."):
                ref = nfc(p.relative_to(target).as_posix())
                conv.setdefault(source_rel_key(ref), ref)

    entries: list[tuple[int, str, str]] = []  # (facts, original-ref, conversion-ref or "")
    listed: set[str] = set()
    for p in sorted((target / "sources").rglob("*")):
        if not p.is_file() or p.name.startswith("."):
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


def _review_queue(rows: list[dict[str, str]]) -> tuple[list[tuple[str, str, str]], str]:
    """Return stable pending-fact numbers and a digest of their full snapshot.

    Numbers name unique NFC-normalized triples, sorted lexicographically.  The
    digest additionally covers every pending backing row (including source,
    status, confidence and note), so accepting a number cannot silently act on
    a queue that changed after it was reviewed.
    """
    import hashlib
    import json
    import unicodedata

    from factlog.common import REVIEW_STATUSES

    def fld(row: dict[str, str], key: str) -> str:
        return unicodedata.normalize("NFC", (row.get(key) or "").strip())

    pending_rows = [row for row in rows if (row.get("status") or "").strip() in REVIEW_STATUSES]
    triples = sorted({(fld(row, "subject"), fld(row, "relation"), fld(row, "object")) for row in pending_rows})
    snapshot_rows = sorted(
        (
            fld(row, "subject"), fld(row, "relation"), fld(row, "object"),
            fld(row, "source"), fld(row, "status"), fld(row, "confidence"), fld(row, "note"),
        )
        for row in pending_rows
    )
    payload = json.dumps(
        {"domain": "factlog-review-snapshot-v1", "rows": snapshot_rows},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return triples, "sha256:" + hashlib.sha256(payload).hexdigest()


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
    queue, digest = _review_queue(rows)
    pending = [r for r in rows if (r.get("status") or "").strip() in want]
    if not pending:
        print(f"factlog review (KB: {target}): no pending facts ({'/'.join(sorted(want))})")
        if args.status is None:
            print(f"  snapshot: {digest}")
        return 0

    def fld(r: dict, k: str) -> str:
        return nfc((r.get(k) or "").strip())

    groups: dict[tuple[str, str, str], list[dict]] = {}
    for r in pending:
        groups.setdefault((fld(r, "subject"), fld(r, "relation"), fld(r, "object")), []).append(r)

    number = {triple: index for index, triple in enumerate(queue, start=1)}
    print(f"factlog review (KB: {target}): {len(groups)} pending fact(s), {len(pending)} row(s)")
    for (s, rel, o) in sorted(groups):
        grp = groups[(s, rel, o)]
        prefix = f"[{number[(s, rel, o)]}] " if args.status is None else ""
        print(f"  {prefix}{s} / {rel} / {o}")
        for r in sorted(grp, key=lambda r: fld(r, "source")):
            src = fld(r, "source")
            status = (r.get("status") or "").strip()
            conf = normalize_confidence((r.get("confidence") or "").strip())
            note = (r.get("note") or "").strip()
            print(f"    ← {src or '(no source)'}  [{status}, conf {conf}]")
            if note:
                print(f"        note: {note}")
    print("  decide with: factlog accept <subject> <relation> <object>   (or: factlog reject ...)")
    if args.status is None:
        print(f"  snapshot: {digest}")
        print(f"  or by reviewed number: factlog accept --number 1 --from {digest}")
    return 0


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
    numbers = list(args.numbers or [])
    numbered = bool(numbers)
    if numbered:
        import re

        if args.terms:
            print(
                f"factlog {verb}: do not mix --number with a triple selector",
                file=sys.stderr,
            )
            return 2
        if len(set(numbers)) != len(numbers):
            print(f"factlog {verb}: duplicate --number value; give each review number once", file=sys.stderr)
            return 2
        if any(number < 1 for number in numbers):
            print(f"factlog {verb}: --number must be a positive review number", file=sys.stderr)
            return 2
        if args.from_digest is None or not re.fullmatch(r"sha256:[0-9a-f]{64}", args.from_digest):
            print(
                f"factlog {verb}: numeric selection needs the current review snapshot; no changes made. "
                "Run factlog review again.",
                file=sys.stderr,
            )
            return 1
    elif args.from_digest is not None:
        print(f"factlog {verb}: --from is only valid with one or more --number selectors", file=sys.stderr)
        return 2
    elif len(args.terms) > 3:
        print(
            f"factlog {verb}: too many terms — give at most SUBJECT RELATION OBJECT "
            "(quote a value that contains spaces)",
            file=sys.stderr,
        )
        return 2
    filt = None if numbered else _triple_filter(args.terms)
    if not numbered and filt is None:
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

    selected_numbers: set[int] = set()
    selected_triples: set[tuple[str, str, str]] = set()
    if numbered:
        queue, actual_digest = _review_queue(rows)
        selected_numbers = set(numbers)
        invalid = sorted(number for number in selected_numbers if number > len(queue))
        if invalid:
            print(
                f"factlog {verb}: review number(s) out of range: {', '.join(map(str, invalid))}; no changes made. "
                "Run factlog review again.",
                file=sys.stderr,
            )
            return 2
        if args.from_digest != actual_digest:
            print(
                f"factlog {verb}: review snapshot is stale; no changes made. Run factlog review again.",
                file=sys.stderr,
            )
            return 1
        selected_triples = {queue[number - 1] for number in selected_numbers}
        matched = [r for r in rows if (fld(r, "subject"), fld(r, "relation"), fld(r, "object")) in selected_triples]
    else:
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
        is_selected = (
            (fld(r, "subject"), fld(r, "relation"), fld(r, "object")) in selected_triples
            if numbered else all(fld(r, k) == v for k, v in filt.items())
        )
        if is_selected and (r.get("status") or "").strip() in REVIEW_STATUSES:
            r["status"] = new_status
            changed += 1
    _atomic_write_csv(csv_path, rows, out_fields)

    recompile_failed = not _recompile_accepted(target, verb)
    recompiled = "accepted.dl NOT recompiled" if recompile_failed else "accepted.dl recompiled"
    print(f"factlog {verb}: {changed} row(s) → {new_status}; {recompiled}")
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

    The positional triple identifies a live fact (exact NFC match); superseded
    tombstones are never matched. The --set-* flags give the new values (at
    least one required, or --accept). A
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
        KbContext,
        entity_set,
        nearby_vocabulary,
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
        # Keep no-match's stderr/rc=1 contract.  The optional notes are derived
        # only from accepted engine vocabulary (never candidate/source text).
        if (target / "facts" / "accepted.dl").is_file():
            accepted = KbContext.for_root(target).load_accepted_facts()
            entities = entity_set(accepted)
            relations = {row["relation"] for row in accepted if row["relation"]} | set(aliases) | set(aliases.values())
            for kind, term, vocabulary in (
                ("entity", filt.get("subject"), entities),
                ("relation", filt.get("relation"), relations),
                ("entity", filt.get("object"), entities),
            ):
                if term is None:
                    continue
                if any(nfc(value).casefold() == nfc(term).casefold() for value in vocabulary):
                    continue
                suggestions = nearby_vocabulary(term, vocabulary)
                if suggestions:
                    print(
                        f"note: no accepted {kind} '{term}'. did you mean: {', '.join(suggestions)}?",
                        file=sys.stderr,
                    )
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
    """Manage policy/sync-ignore.md — sources excluded from sync and wiki evidence.

    No patterns: list current entries and the on-disk sources each matches.
    With pattern(s): add them, or remove them with --remove. Excluding a source
    stops its re-extraction (ingest --scan / sync / coverage) and keeps it out
    of `/factlog ask` wiki evidence; its already-merged facts are untouched
    (use `factlog eject` to remove those).
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
    sv = ctx.single_valued_relations()
    sv_folded = common.folded_relation_names(sv)
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
        if o and rel not in attr:  # objects of attribute relations are literals, not entities
            ent_counts[o] += 1

    print(f"factlog vocab (KB: {target}) — {scope_label} facts")
    if show_e:
        print(f"  entities ({len(ent_counts)}):")
        # Counting is on the raw name, so two spellings of one entity are two
        # lines. Since #342 the ENGINE folds them into one atom, so this count
        # is candidate spellings and is deliberately NOT the engine's entity
        # count — a KB holding 한라산기지 in both forms lists 4 here where the
        # engine has 2. Folding this loop alone is trivial — it builds its own
        # counter and calls nothing shared — and that is exactly why it is not
        # done: the list would then no longer be the set ``common.entity_set``
        # returns, which is what ``ask`` validates a query's entity arguments
        # against and what the path-node set is built from, so this command
        # would advertise a vocabulary the gate does not honour. Making the two
        # agree means folding ``entity_set`` itself, which moves ask's query
        # validation with it — #213's chokepoint, wider than this change. What
        # is NOT acceptable is leaving the reader
        # unable to see why: label the normalization form on exactly the names
        # sharing a folded spelling with another, the same rule (and the same
        # three-valued-label caveat) the relation list below uses. A KB with no
        # such pair prints byte-identically to before.
        folded_ent = Counter(unicodedata.normalize("NFC", name) for name in ent_counts)
        for name, n in sorted(ent_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            form = (
                f"  ({common.normalization_form(name)})"
                if folded_ent[unicodedata.normalize("NFC", name)] > 1 else ""
            )
            print(f"    [{n:>3}] {name}{form}")
        if not ent_counts:
            print("    (none)")
    if show_r:
        print(f"  relations ({len(rel_counts)}):")
        # Counting is on the raw name (the fold is a membership predicate, not a
        # grouping key — see check_conflicts), so two spellings of one relation
        # are two lines that render identically. Tagging membership made the pair
        # indistinguishable: both lines now say [single-valued], where before one
        # of them was at least untagged. Name the normalization form on exactly
        # the names that share a folded spelling with another. A KB with no such
        # pair prints byte-identically to before.
        #
        # Scope, stated exactly: this separates **NFC from NFD**, which is the
        # pair that actually occurs (a composed name beside its macOS-decomposed
        # twin). It does NOT make every pair distinguishable, because the label
        # is three-valued: two names that are each neither wholly composed nor
        # wholly decomposed — NFC('소속')+NFD('기관') beside NFD('소속')+NFC('기관')
        # — both render as `소속기관  (mixed)` and stay identical on screen.
        # ``check_conflicts._spellings`` does not have that gap because it
        # escapes the string *and* labels it; this prints the name as written and
        # labels it, so the label is carrying the whole distinction. Escaping
        # here would close it, at the cost of making every such line unreadable
        # for the common case — deliberately not done; see the follow-up.
        folded_rel = Counter(common.fold_relation_name(name) for name in rel_counts)
        for name, n in sorted(rel_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            # Membership folded, matching the gate (check_conflicts) and the two
            # other consumers. Without it a uniformly-NFD KB gets `conflicts: 1
            # (over 1 single-valued relation(s))` out of `status` and no
            # [single-valued] tag here, so the reader is told a conflict exists
            # and not which relation is functional. The typed lookup two lines
            # down already folds; leaving this one raw made the same loop
            # asymmetric.
            tname = unicodedata.normalize("NFC", name)
            tags = [
                t
                for t, on in (("attribute", name in attr), ("single-valued", tname in sv_folded))
                if on
            ]
            # typed_relations() keys are NFC-normalized; the CSV-sourced name may be NFD.
            if tname in typed:
                tags.append(f"typed:{typed[tname].type}")
            tagstr = f"  [{', '.join(tags)}]" if tags else ""
            form = f"  ({common.normalization_form(name)})" if folded_rel[tname] > 1 else ""
            print(f"    [{n:>3}] {name}{form}{tagstr}")
        if not rel_counts:
            print("    (none)")
    return 0


# The whole line run_logic_check.py writes into facts/logic_report.txt when the
# engine never ran (#338). Matched byte for byte against a whole line, the same
# way hooks/gate_check.sh matches it — its `_records_engine_failure` splits the
# report on "\n", strips trailing CRs and compares for equality, which is what
# the reader below does; the constant is also spelled out in
# tools/run_logic_check.py as ENGINE_FAILED_STATUS_LINE. All three are one
# vocabulary and change together. Its natural home is factlog/common.py, next to
# the other shared report vocabulary — worth hoisting when something else needs
# it, and not worth a third reader having to rediscover the coupling meanwhile.
ENGINE_FAILED_STATUS_LINE = "status: engine-did-not-run"


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
    if facts:
        order = ["confirmed", "accepted", "needs_review", "candidate", "superseded"]
        seen = [f"{s}={by_status[s]}" for s in order if by_status.get(s)]
        extra = [f"{s}={n}" for s, n in by_status.items() if s not in order]
        print(f"  facts:      {len(facts)} candidate(s) [{', '.join(seen + extra)}]; {len(engine_rows)} engine fact(s)")
    else:
        # Empty row list has two causes and `init` now makes the first one the
        # normal first-run state, so they must read differently: a scaffolded
        # ledger holds zero rows, while an absent one is a breakage validate
        # reports as an error (#327).
        why = "0 rows" if ctx.candidates_csv.is_file() else "no facts/candidates.csv"
        print(f"  facts:      none ({why} — run /factlog sync)")

    # Vocabulary
    attr = ctx.attribute_relations()
    sv = ctx.single_valued_relations()
    # Pass attr so entity_set reads THIS KB's attribute relations, not the module
    # default (cmd_status may target a KB other than the ambient FACTLOG_ROOT).
    ent, val = common.entity_set(facts, attr), common.value_set(facts)
    # Literals are values appearing only as attribute-relation objects; with no
    # attribute-relations.md declared, entity_set == value_set so there are none.
    literals = f"{len(val) - len(ent)} literal(s)" if attr else "0 literal(s) — none declared"
    print(
        f"  vocabulary: {len(ent)} entit(y/ies), {literals}, "
        # Scoped to the engine ROWS (engine_facts), like entity_set/value_set
        # above, so this agrees with `factlog vocab`, which counts the same way.
        # It is NOT the engine's own entity count: both count raw candidate
        # spellings, and since #342 the engine folds canonically equivalent ones
        # into a single atom, so a KB with a name in two forms reads 2 here and 1
        # in accepted.dl. `vocab` labels the normalization form on such names;
        # this line is a total and cannot. Closing the gap means folding
        # `common.entity_set` itself: THIS call hands it explicit candidate rows
        # and is not ask's vocabulary, but the function is the one ask validates
        # a query's entity arguments through and the one the path-node set is
        # built from, so folding it moves those — #213's chokepoint, deliberately
        # not touched here.
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
        if any(part.startswith(".") for part in p.relative_to(target).parts):
            continue  # hidden (.DS_Store, .git, ...)
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
        by_key: dict[tuple, set] = {}
        # Membership folded, matching the gate (check_conflicts), and so are the
        # two axes the gate folds for grouping: the subject and the untyped
        # object. Leaving the grouping raw made this count disagree with the gate
        # in BOTH directions once the gate started folding — it printed 0 on a
        # mixed-subject KB finalize refuses to compile, and 1 with
        # "⚠ resolve via superseded" on a KB whose only defect is two spellings
        # of one value, where superseding is the wrong repair and would drop a
        # source's corroboration. The relation axis stays raw, matching the gate,
        # which defers that decision (#210).
        #
        # This is still a count, not the gate: it does not parse typed literals,
        # so a #116 cross-notation pair (5400억 / 0.54조) is two values here and
        # one to check_conflicts. Closing that needs the checker's grouping
        # shared rather than reimplemented — a follow-up, since `tools/` is not
        # importable from the installed package (pyproject packages = ["factlog"]).
        #
        # FOLLOW-UP, and note that #325 WIDENED this divergence rather than only
        # inheriting it. An NFD-authored typed literal (`매출` ordinal, Acme =
        # NFD('제3호') and '3위') agreed on main — status 1, gate 1 — and now
        # reads status 1, gate 0, because the gate folds the object before
        # parsing and this count does not. The two then give OPPOSITE repairs:
        # "resolve via superseded" here, "unify the spelling in sources/ and
        # re-collect" from the gate's disclosure. The gate is the authority
        # (`finalize` calls it) and this message points the reader at it, and the
        # divergence direction is over-reporting here, which is why it is not
        # treated as a release blocker — but the follow-up that shares the
        # grouping owns this input specifically.
        sv_folded = common.folded_relation_names(sv)
        for r in engine_rows:
            if common.fold_relation_name(r["relation"]) in sv_folded:
                key = (unicodedata.normalize("NFC", r["subject"]), r["relation"])
                by_key.setdefault(key, set()).add(unicodedata.normalize("NFC", r["object"]))
        conflicts = {k: v for k, v in by_key.items() if len(v) > 1}
        msg = f"  conflicts:  {len(conflicts)} (over {len(sv)} single-valued relation(s))"
        if conflicts:
            msg += "  ⚠ resolve via superseded / see tools/check_conflicts.py"
        print(msg)
        # Under a TYPED relation, a conflicting value carrying non-ASCII digits
        # does not parse as the declared type, so the generic "resolve via
        # superseded" advice above can clear the gate while leaving that value in
        # the KB (#331). Name it here — this path did not print the values at all,
        # and repr() would not distinguish '１００억' from '100억' on screen.
        #
        # Restricted to typed relations on purpose: under an untyped relation the
        # two spellings are just two strings, the value is a usable relation/3
        # fact, and superseding the outdated row IS the fix — warning there would
        # steer the user away from the one action that works.
        #
        # Digit test FIRST, typed lookup only if it can matter. ctx.typed_relations()
        # is not a pure read: it warns when a typed relation is missing from
        # attribute-relations.md, and re-reads facts + logic policy to compute
        # reserved names. Resolving it unconditionally put that warning on every
        # status run for such a KB — including one with zero conflicts.
        flagged: dict[str, set[str]] = {}
        for (_subject, relation), objs in conflicts.items():
            hits = {o for o in objs if literal_types.has_non_ascii_digits(o)}
            if hits:
                flagged.setdefault(relation, set()).update(hits)
        odd: set[str] = set()
        if flagged:
            # typed_relations() FAILS LOUDLY on a broken policy (non-ASCII alias,
            # alias collision/duplicate, a units clause on a non-amount line). That
            # is right for the commands that must not run on a bad policy, but
            # `status` is the command you run to find out WHAT is bad, so it has to
            # stay total: a policy error costs this supplementary warning only, and
            # the report (conflicts, logic freshness, engine) still prints in full.
            # The error itself is not swallowed — every other entry point still
            # raises it, and status has no output line to attach it to here.
            # Resolved lazily for the same reason `main` does it (see the comment
            # there): the class must match the one `common` currently exports.
            from factlog.common import FactlogError

            # OSError/ValueError too, and not only for tidiness: typed_relations()
            # reads logic-policy.dl to compute reserved names, so a policy file
            # that is not UTF-8 (cp949 is realistic here) raises
            # UnicodeDecodeError — a ValueError, which main()'s handler re-raises
            # as a raw traceback. This call site is the only thing that made
            # `status` decode that file at all.
            #
            # Widened HERE and not in common._try: every other caller of
            # typed_relations() (finalize, check_conflicts, vocab) must keep
            # failing loudly on an unreadable policy. Only `status` trades the
            # warning for finishing the report.
            try:
                typed = ctx.typed_relations()
            except (FactlogError, OSError, ValueError):
                typed = {}
            for relation, hits in flagged.items():
                # typed_relations() keys are NFC; a CSV-sourced name may be NFD.
                spec = typed.get(unicodedata.normalize("NFC", relation))
                if spec is None:
                    continue
                # Carrying non-ASCII digits is not the same as failing to parse: a
                # declared UNIT NAME may carry them (`amount(100,"억１")` under a
                # declared `억１` unit) and still normalize to a scalar, which the
                # engine reads fine. Ask the normalizer, so this line and
                # check_conflicts' note fire on the one predicate that decides
                # raw-vs-scalar in the first place.
                odd.update(
                    literal_types.mark_non_ascii_digits(o)
                    for o in hits
                    if literal_types.normalize(spec.type, o, spec.units) is None
                )
        if odd:
            # A set, so one offender shared by several conflict groups is named once.
            print(
                "              ⚠ non-ASCII digits in " + ", ".join(f"'{o}'" for o in sorted(odd))
                + " — superseding a row clears the gate but can keep a value that does"
                " not parse; correct the source to ASCII and re-collect, then supersede"
                " the outdated row if the values still differ"
            )
    else:
        print("  conflicts:  n/a (no single-valued relations declared in policy/single-valued.md)")

    # Logic report freshness
    report = ctx.facts_dir / "logic_report.txt"
    if report.is_file():
        # The VERDICT is computed on BYTES, and hooks/gate_check.sh performs the
        # identical operation on the identical bytes: split on b"\n", rstrip
        # b"\r", compare to the marker. Every part is load-bearing, and each was
        # a divergence before it was:
        #
        #   - splitlines() also breaks on U+2028/U+2029/U+0085; the gate does
        #     not. U+2028 is routine in text pasted from PDFs, so a value
        #     carrying one opened a line here that is not a line there.
        #   - read_text()'s universal-newline mode turns "\r\n" AND a lone "\r"
        #     into "\n" before this code sees anything, re-creating that
        #     divergence one layer down.
        #   - TRAILING CRs only, and all of them, matching the gate.
        #   - and DECODING cannot be part of the verdict at all: with
        #     errors="ignore" an undecodable byte is deleted, so a line that is
        #     not the marker becomes one — the same "manufacture a marker out of
        #     a non-marker line" defect that `tr -d '\r'` had, with the readers
        #     swapped. Bytes have no such failure mode; a byte that decodes to
        #     nothing is simply unequal to the marker.
        #
        # Decoding happens only for what is DISPLAYED, with errors="replace" so
        # an unreadable byte is visible as U+FFFD instead of silently vanishing.
        raw = report.read_bytes()
        report_byte_lines = [ln.rstrip(b"\r") for ln in raw.split(b"\n")]
        report_lines = [
            ln.decode("utf-8", errors="replace") for ln in report_byte_lines
        ]
        if ENGINE_FAILED_STATUS_LINE.encode("utf-8") in report_byte_lines:
            # A report of a run in which THE ENGINE NEVER RAN is not a result, and
            # freshness is the wrong question to ask of it: /factlog check has just
            # written it, so it IS fresh by mtime, and reporting that would say a
            # run that never started the engine is up to date. It also carries no
            # `errors:`/`warnings:` lines at all — deliberately, since 0 would mean
            # the engine ran and found nothing — so the `?` fallbacks below would
            # print two count fields for counts that were never obtained (#338).
            reason = next(
                (ln.split(":", 1)[1].strip() for ln in report_lines if ln.startswith("reason: ")),
                "(not recorded)",
            )
            print(f"  logic:      report records a run that never started the engine; reason: {reason}")
            print(
                "              ⚠ the counts a completed check reports are absent, not 0"
                " — fix the cause above, then run /factlog check"
            )
        else:
            # Lower-case `errors:`/`warnings:` are the summary lines in
            # run_logic_check's report (the `Errors:`/`Warnings:` headers are capitalised).
            errors = next((ln.split(":", 1)[1].strip() for ln in report_lines if ln.startswith("errors:")), "?")
            warnings = next((ln.split(":", 1)[1].strip() for ln in report_lines if ln.startswith("warnings:")), "?")
            rep_mtime = report.stat().st_mtime
            # The report is a function of all three run_logic_check inputs.
            inputs = [p for p in (ctx.accepted_dl, ctx.facts_dir / "query.dl", ctx.logic_policy_dl) if p.is_file()]
            stale = any(p.stat().st_mtime > rep_mtime for p in inputs)
            fresh = "STALE (inputs changed since last check — run /factlog check)" if stale else "fresh"
            print(f"  logic:      report {fresh}; errors={errors}, warnings={warnings}")
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
    target = _resolve_kb_target(getattr(args, "target", None), "factlog setup")
    kb_created = _init_kb(target)
    if kb_created:
        actions.append(f"created KB layout at {target}")
    else:
        actions.append(f"KB already present at {target}")
    # Shared with `init`: a first-time setup activates the KB it just made, but a
    # setup run beside an existing active KB creates without re-pointing it (#356).
    activation, activation_notes = _apply_activation(
        "factlog setup", target, getattr(args, "activate", None), defer_reach=True
    )
    # The summary is a "done:" action only when there *was* a write. The rule the
    # next comment states — `actions` are prefixed "done:", so nothing setup
    # declined to do belongs there — was applied to the hint and the notes but not
    # to the line they hang off, and `done: active-KB root unchanged: …` announced
    # leaving the config alone as an accomplishment.
    if activation.write:
        actions.append(activation.summary)
    # The hint and the notes travel with the summary into the block the user
    # actually reads at the end. Printed only at the moment of the decision, they
    # sat twenty-odd lines above the closing "next step" line with nothing to
    # recall them. They are kept out of `actions` because those are prefixed
    # "done:" and none of this is something setup did.
    notes: list[str] = ([] if activation.write else [activation.summary])
    notes += ([activation.hint] if activation.hint else []) + activation_notes
    # Optional narration language: applied only when --lang is given, so an existing
    # language survives a re-run of setup that omits the flag (the root write above,
    # when it happens, preserves it). Uses the shared validate/apply path, so an
    # empty value clears the setting with the same wording as `factlog lang`.
    #
    # Not applied at all to a config that could not be read: `write_lang` rebuilds
    # the file from `_read_config()`, which is `{}` for a damaged file, so it would
    # destroy the KB root the activation step above just refused to touch — while
    # the summary says it left it alone. Guarding only the root write left this
    # sibling path open. Re-read the status here rather than reusing the plan's,
    # because `--activate` may have just replaced the damaged file with a valid one.
    lang_deferred = False
    if lang_normalized is not None:
        if factlog_config.config_status() == factlog_config.UNREADABLE:
            lang_deferred = True
            said = _unreadable()
            notes.append(
                f"narration language NOT set: {factlog_config.config_path()} {said.reason}, "
                f"and writing it would {said.cost} — {said.remedy}, "
                "then set the language with `factlog lang`"
            )
        else:
            phrase = _apply_lang(lang_normalized, "factlog setup")
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
    for note in notes:
        print(f"  → {note}")

    if final_ok and lang_deferred:
        # A valid --lang was asked for and not applied. Exiting 0 handed a script
        # three agreeing signals — rc 0, a "complete" line, and `factlog lang`
        # printing empty — with only prose to say the request had been declined.
        said = _unreadable()
        print(
            f"\nfactlog setup: the KB at {target} is ready, but --lang was not applied "
            f"because {factlog_config.config_path()} {said.reason} (see above). "
            f"{said.remedy[0].upper()}{said.remedy[1:]}, then set the language "
            "with `factlog lang`.",
            file=sys.stderr,
        )
        return 1

    if final_ok:
        # The closing line is the one a user (or an LLM) acts on, so it asks the
        # question they are about to ask: where does a flagless /factlog sync go?
        # That is `resolve_root`, not the config — with $FACTLOG_ROOT naming the
        # new KB, a config-based answer claimed it was unreachable when it was
        # already the KB in force.
        reach = _reach_note(target)
        if reach is None:
            print(
                "\nfactlog setup complete. Next: run /factlog sync (and then query, "
                "check, repair) inside your knowledge base."
            )
        else:
            print(
                f"\nfactlog setup complete, but {reach}. "
                "Then: /factlog sync (and then query, check, repair)."
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
    """Strict boolean inverse of merge_candidates.is_text_source for --scan
    discovery: ``_looks_binary(p) == (not is_text_source(p))`` for every file.

    Treats a file as binary if its first *sniff* bytes contain a NUL or do not
    decode as UTF-8. A multi-byte char truncated at the sniff boundary is
    tolerated (not binary) ONLY when the file actually extends past the boundary;
    a fully-read short file with an invalid trailing byte is binary. Previously
    this read just ``[:sniff]`` and so could not tell a short truncated file from
    a boundary-truncated long one, disagreeing with is_text_source on the former —
    which left such a source classified as NEITHER text nor binary (#259). Read
    one byte past *sniff* to recover the "extends past sniff" signal cheaply.
    """
    try:
        with path.open("rb") as fh:
            raw = fh.read(sniff + 1)
    except OSError:
        return True
    chunk = raw[:sniff]
    if b"\x00" in chunk:
        return True
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError as exc:
        return not (len(raw) > sniff and exc.start >= len(chunk) - 3)
    return False


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

    from factlog.common import is_sync_ignored, sync_ignore_patterns

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
        for path in sorted(p for p in (target / "sources").rglob("*") if p.is_file()):
            if path.name.startswith("."):
                continue
            ref = unicodedata.normalize("NFC", path.relative_to(target).as_posix())
            if not _looks_binary(path):
                # Only a recognized *conversion target* (a binary-format
                # extension) is worth flagging: a plain .txt/.md source is read
                # directly by sync as text and is correctly not a conversion job.
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
        else:
            converted += 1
            print(f"factlog ingest: {source_label} -> {dst_rel} (via {tool})")

    summary = f"{converted} converted, {skipped} skipped, {failures} failed"
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
    #
    # The stored value is the original's path *relative to sources/* — the
    # conversion's own mirrored subdir joined with the basename read from the
    # header (see the loop below). Consumers that only want the basename go
    # through origin_name().
    conv_origin: dict[str, str] = {}
    for ref, p in disk_refs.items():
        if not ref.startswith("runs/sources/"):
            continue
        try:
            head = p.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
        except OSError:
            head = ""
        # Same header grammar as common.conversion_origin(); kept inline because
        # that helper returns a basename and this needs the full recorded value,
        # and because every conversion's header is read here in one pass.
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
                # (sub_a/data.hwpx) rather than a bare basename. Rebuild the
                # original's sources-relative path from the conversion's *own*
                # mirrored subdir plus the header's basename, so both header
                # formats yield the same value and no legacy basename header
                # regresses: ingest derives the mirrored subdir and the header
                # label from one src_rel, so the header's directory component is
                # never new information (and is the wrong signal to trust when a
                # hand-edit makes the two disagree — the file's own location is
                # what the conversion actually mirrors).
                # Only PurePosixPath joining is used: it folds "//" and "./" but
                # keeps ".." verbatim, so a stored origin still spells the header
                # the same way a consumer would.
                # Dropping the header's directory also narrows one shape that no
                # released factlog has ever written — a *flat* conversion whose
                # header carries a path — since mirroring (rel_parent) and the
                # #214 path header (source_label) were added by the same commit,
                # 73cc76a. That is defence against a hand-edited KB, not a
                # migration path.
                subdir = PurePosixPath(ref[len("runs/sources/"):]).parent
                base = PurePosixPath(origin).name
                # A header with no filename component ("source: /") is as
                # unusable as an empty one; keep the empty-origin sentinel rather
                # than inventing an original named after the subdir.
                conv_origin[ref] = (subdir / base).as_posix() if base else ""

    def origin_name(ref: str) -> str | None:
        """Basename of the original a conversion was made from — None when the
        conversion carries no provenance header at all."""
        origin = conv_origin.get(ref)
        return PurePosixPath(origin).name if origin is not None else None

    # Every basename claimed by an original under sources/, at any depth. A flat
    # conversion's header records only a basename, so this set answers "does the
    # KB hold an original this conversion could have been made from?" — the
    # question both the --orphans pairing below and the basename fallback in
    # selector() have to agree on.
    src_basenames = {Path(r).name for r in disk_refs if not r.startswith("runs/sources/")}

    # What resolve() does with a symlink loop depends on the interpreter: on
    # 3.11/3.12 pathlib re-raises ELOOP as RuntimeError (not OSError), while on
    # 3.13+ it raises nothing and hands back the path unresolved. Both are in
    # range of requires-python >=3.11, and CI pins 3.11, so RuntimeError is live
    # rather than defensive. ValueError covers a path with an embedded NUL,
    # which argv cannot actually carry — that one is pure defence.
    #
    # None of them may reach the user as a traceback. Note that swallowing the
    # error is not by itself enough to make the argument harmless: an
    # unresolved path is still absolute and still lands outside the KB, so what
    # keeps it from deleting anything is the sources/<basename> guard below.
    _RESOLVE_ERRORS = (OSError, RuntimeError, ValueError)

    try:
        troot = target.resolve()
    except _RESOLVE_ERRORS:
        troot = target

    def ident(p: Path) -> tuple[int, int] | None:
        """The filesystem's own identity for a path, or None when it cannot be
        stat'd — the file is missing, or it sits behind a symlink loop."""
        try:
            st = p.stat()
        except (OSError, ValueError):
            return None
        return (st.st_dev, st.st_ino)

    troot_id, sroot_id = ident(troot), ident(target / "sources")

    def kb_rel_by_identity(p: Path) -> str | None:
        """Reduce an absolute path to a KB-relative ref by asking the filesystem
        whether one of its ancestors *is* sources/ or the KB root.

        Comparing resolved strings does not answer this: it misses whenever
        sources/ is a symlink or --target is spelled in a different case on a
        case-insensitive filesystem, and both misses fall through to the
        basename fallback — the deletion #324 is about.

        This is deliberately *stronger* than what ingest uses. ingest reduces
        with relative_to() on resolved strings (cli.py:2050), so the two
        commands do not agree: given a case-different --target, ingest treats an
        in-sources/ file as outside and writes a flat conversion with a
        bare-basename header, while this resolves the same argument into
        sources/. A conversion produced that way cannot be paired from here —
        its header names a basename, which says nothing about which directory it
        came from — so deleting the original reports the orphan it leaves rather
        than guessing (see the --delete-original notes below).

        Walks strictly upward, so a symlink loop cannot trap it: the loop makes
        stat() fail, which only means "not a root" and the walk keeps rising.
        Returns None at the filesystem root, leaving the string reduction below
        to handle a KB whose own root cannot be stat'd.
        """
        tail: list[str] = []
        cur = p
        while True:
            cur_id = ident(cur)
            if cur_id is not None:
                if sroot_id is not None and cur_id == sroot_id:
                    return "/".join(["sources", *reversed(tail)])
                if troot_id is not None and cur_id == troot_id:
                    # "." is what relative_to() spells for the root itself.
                    return "/".join(reversed(tail)) if tail else "."
            if cur.parent == cur:
                return None
            tail.append(cur.name)
            cur = cur.parent

    def selector(name: str) -> tuple[set[str], str | None, str]:
        """Canonicalise one `eject <name>` argument into what the matcher needs:

          refs    — the KB-relative ref(s) the argument names exactly, in both
                    the raw and the normalised spelling. ingest never writes a
                    ref containing "./" or "//", but candidates.csv is
                    hand-editable and a row's source column can carry one, so
                    the raw form keeps such a row ejectable by the path typed;
          src_rel — the original's path *relative to sources/*, when a path was
                    given; compared against conv_origin to reach the conversion
                    that path produced. None when the argument names no original
                    under sources/ (a conversion ref, or a bare name);
          raw     — the argument as written, for the bare filename / stem rules.
        """
        raw = nfc(name)
        p = Path(raw)
        if p.is_absolute():
            # Resolve an *absolute* argument, and the root with it: this machine
            # can reach the KB through a symlink (/tmp -> /private/tmp), and an
            # unresolved argument would then look like it lies outside the KB.
            # A relative argument is never resolved — resolving it against the
            # cwd would turn `sub/report.html`, typed inside the KB, into an
            # outside-the-KB path and drop it back to basename matching, which
            # is the deletion #324 is about.
            try:
                p = p.resolve()
            except _RESOLVE_ERRORS:
                pass
            # Identity first; the string reduction still covers a KB root that
            # cannot be stat'd at all.
            kb_rel = kb_rel_by_identity(p)
            if kb_rel is None:
                try:
                    kb_rel = p.relative_to(troot).as_posix()
                except ValueError:
                    kb_rel = None
            kb_rel = nfc(kb_rel) if kb_rel is not None else None
            if kb_rel is not None and (kb_rel == "sources" or kb_rel.startswith("sources/")):
                return {kb_rel}, kb_rel[len("sources/"):] or None, raw
            # An original outside sources/ — anywhere else in the KB, or outside
            # it entirely — has no subtree for ingest to mirror, so it always
            # converts to a *flat* runs/sources/<name> whose rebuilt origin is a
            # bare basename. Comparing against that basename keeps the legitimate
            # case working while leaving every mirrored conversion out of reach.
            #
            # Only when nothing under sources/ claims that basename, though.
            # ingest stores just src.name for an original outside sources/
            # (:2186), so a flat conversion's header cannot say *which* file of
            # that name it came from; when the KB holds one itself, that file is
            # the answer and this argument is not. Ejecting is unprompted and
            # exits 0, so an ambiguous argument must select nothing at all.
            # The competing original can sit at any depth — src_basenames is the
            # same set --orphans pairs against, so one command cannot call a
            # conversion paired there and unpaired here.
            base = nfc(p.name)
            fallback = base if base and base not in src_basenames else None
            return ({kb_rel} if kb_rel is not None else set()), fallback, raw
        # A relative argument is read KB-relative (`sources/...`, `runs/...`) or
        # sources-relative (`sub/report.html`). PurePosixPath folds "./" and "//"
        # but keeps ".." verbatim: no normpath/realpath here, so the comparison
        # never rewrites a path into something the recorded origin spells
        # differently.
        norm = PurePosixPath(raw).as_posix() if "/" in raw else raw
        if norm.startswith("sources/"):
            return {raw, norm}, norm[len("sources/"):] or None, raw
        if norm.startswith("runs/sources/"):
            return {raw, norm}, None, raw  # names a conversion, not an original
        return {raw, norm}, norm, raw

    def matches(ref: str, sel: tuple[set[str], str | None, str]) -> bool:
        refs, src_rel, name = sel
        if ref in refs:  # exact KB-relative ref
            return True
        is_conv = ref.startswith("runs/sources/")
        if "/" in name:
            # A path was given: the exact original is handled above; for a
            # binary original also match the conversion it produced — the one
            # whose recorded origin *is* that sources-relative path. #324: a
            # same-name original in another directory is NOT matched, because
            # both sides keep their directory instead of collapsing to a
            # basename. An *empty* src_rel (a root path like "/" names no file)
            # must not compare equal either: the empty string is also the
            # no-usable-origin sentinel stored for a header like "source: /".
            return bool(is_conv and src_rel and conv_origin.get(ref) == src_rel)
        rp, np_ = Path(ref), Path(name)
        if np_.suffix:  # a bare filename with an extension
            if not is_conv:
                return rp.name == np_.name  # an original with that filename
            origin = origin_name(ref)  # the conversion made from this original
            # Provenance is the reliable signal. A headerless conversion falls
            # back to its own name minus the ingest out-suffix: since ingest now
            # keeps the original's extension (report.pptx -> report.pptx.md), the
            # conversion's rp.stem ("report.pptx") is the original's full name.
            return origin == np_.name if origin else rp.stem == np_.name
        # bare stem: every original with that stem, and a conversion made from
        # one (matched via its recorded origin so the source's own extension in
        # the new naming — report.pptx.md — does not defeat the stem compare).
        if is_conv:
            origin = origin_name(ref)
            return Path(origin if origin else rp.name).stem == np_.stem
        return rp.stem == np_.stem

    matched: set[str] = set()
    # Originals that an argument pointed *at* without naming: a sources-relative
    # path selects the conversion made from a file, not the file. Collected so
    # --delete-original can say so instead of just reporting 0.
    pointed_at: list[str] = []
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
        for ref in all_refs:
            if ref.startswith("runs/sources/"):
                if ref in disk_refs:
                    origin = conv_origin.get(ref)
                    # origin is not None == has a factlog provenance header
                    # (hand-placed conversions are kept). It already carries the
                    # conversion's mirrored subdir, so a "/" in it *is* the
                    # mirrored case and sources/<origin> is the exact original.
                    if origin is not None:
                        if "/" in origin:
                            paired = f"sources/{origin}" in disk_refs
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
            sel = selector(name)
            hits = {ref for ref in all_refs if matches(ref, sel)}
            if hits:
                matched |= hits
                src_rel = sel[1]
                if src_rel and f"sources/{src_rel}" in disk_refs:
                    pointed_at.append(f"sources/{src_rel}")
            else:
                print(f"factlog eject: no source matches '{name}'", file=sys.stderr)
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

    action = "purge" if args.purge else "supersede"
    print(f"  candidates.csv: {len(affected)} row(s) to {action}")
    print(f"  runs/sources conversion(s) to delete: {len(conv_to_delete)}")
    if args.delete_original:
        print(f"  original(s) to delete (--delete-original): {len(orig_on_disk)}")
        # A sources-relative path selects what a file *produced*, not the file,
        # so --delete-original can legitimately have nothing to do. Say which
        # spelling would have included it, rather than reporting a bare 0.
        for ref in dict.fromkeys(r for r in pointed_at if r not in matched):
            print(f"    note: {ref} is on disk but was not named; pass '{ref}' to delete it too")
        # Deleting an original can strand a *flat* conversion that no argument
        # could have selected: a bare-basename header does not say which
        # directory its original was in, so it is only pairable by name. ingest
        # writes exactly that shape whenever it could not place the original
        # under sources/ — including for a --target spelled in a different case,
        # which this command does resolve into sources/. Name the conversion
        # instead of leaving it behind with nothing to point at.
        doomed = {PurePosixPath(r).name for r in orig_on_disk}
        surviving = {
            PurePosixPath(r).name
            for r in disk_refs
            if not r.startswith("runs/sources/") and r not in matched
        }
        for ref in sorted(disk_refs):
            if not ref.startswith("runs/sources/") or ref in matched:
                continue
            origin = conv_origin.get(ref)
            if origin and "/" not in origin and origin in doomed and origin not in surviving:
                print(
                    f"    note: {ref} records 'source: {origin}' and will have no original "
                    f"left; run 'factlog eject --orphans' to retire it"
                )
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
    A source is named by its filename, stem, or path. Naming the binary original
    (e.g. report.pptx) also matches its runs/sources/<stem> conversion; a bare
    stem matches every source with that stem. A filename is deliberately wide —
    it matches that name in every directory — while a *path* is narrow: it
    selects the original at that path and the conversion made from it, never a
    same-name original elsewhere (#324). Paths are read relative to sources/ (or
    as a KB-relative ref / absolute path) and compared as written: no ".."
    folding, no case folding, and only an absolute path is resolved. eject also
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

    from factlog.common import FACT_HEADER

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
                if p.is_file() and not p.name.startswith("."):
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


_TARGET_HELP = (
    "knowledge base root to create "
    f"(default: $FACTLOG_ROOT, else the active KB, else {_DEFAULT_KB})"
)


def _add_activation_flags(parser: argparse.ArgumentParser) -> None:
    """Attach the tri-state active-KB flags shared by `init` and `setup`.

    Default (neither flag) is None, which ``_plan_activation`` reads as "activate
    only if no KB is active yet". Mutually exclusive, so asking for both is a
    usage error (rc 2) rather than a silent last-flag-wins.
    """
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--activate",
        dest="activate",
        action="store_true",
        help="also make this KB the active one, replacing the current active KB",
    )
    group.add_argument(
        "--no-activate",
        dest="activate",
        action="store_false",
        help="never touch the active-KB setting, not even when none is set yet",
    )
    parser.set_defaults(activate=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factlog", description="factlog environment and KB helpers")
    parser.add_argument("--version", action="version", version=f"factlog {__version__}")
    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser("doctor", help="verify Python and pyrewire requirements")
    doctor.set_defaults(func=cmd_doctor)

    init = sub.add_parser("init", help="scaffold an empty knowledge base layout")
    init.add_argument("--target", default=None, help=_TARGET_HELP)
    _add_activation_flags(init)
    init.set_defaults(func=cmd_init)

    setup = sub.add_parser(
        "setup",
        help="one-shot bootstrap: doctor, ensure deps, init KB, re-check",
    )
    setup.add_argument("--target", default=None, help=_TARGET_HELP)
    _add_activation_flags(setup)
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
        "(default: the active KB set by `factlog use`, else cwd)",
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
    lang.add_argument(
        "--force",
        action="store_true",
        help="set the language even when the active-KB config cannot be read, "
        "replacing it (this discards any KB root it may still hold)",
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
            nargs="*",
            metavar="TERM",
            help="SUBJECT [RELATION [OBJECT]] prefix; use '-' to wildcard a position",
        )
        _p.add_argument(
            "--number",
            dest="numbers",
            action="append",
            type=int,
            metavar="N",
            help="select reviewed pending fact number N (repeatable; requires --from)",
        )
        _p.add_argument(
            "--from",
            dest="from_digest",
            default=None,
            metavar="SNAPSHOT",
            help="select reviewed numeric item(s) only if this review snapshot digest still matches",
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
        help="manage policy/sync-ignore.md: glob patterns excluded from sync and wiki evidence",
    )
    ignore.add_argument("patterns", nargs="*", help="glob/path pattern(s) to add (omit to list)")
    ignore.add_argument("--remove", action="store_true", help="remove the given pattern(s) instead of adding")
    ignore.add_argument("--target", default=None, help="KB root (default: the active KB; see `factlog where`)")
    ignore.set_defaults(func=cmd_ignore)

    status = sub.add_parser("status", help="summarise KB state (sources, facts, vocabulary, conflicts, engine)")
    status.add_argument("--target", default=None, help="KB root (default: the active KB; see `factlog where`)")
    status.set_defaults(func=cmd_status)

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
