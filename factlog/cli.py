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

MIN_PYTHON = (3, 11)
MIN_PYREWIRE = (1, 0, 3)  # bundles wirelog v0.52.0 with \" escape support (wirelog#924)


def _atomic_write_text(path: _Path, text: str) -> None:
    """Write *text* to *path* atomically (temp file + os.replace).

    Used for run-file JSON so an interrupted/`amend`/`eject` run can never leave a
    truncated runs/*.json behind — a corrupt run file still holds retired rows and
    would resurrect them (or be skipped, losing the run) on the next merge. Mirrors
    the temp+replace pattern already used for candidates.csv.
    """
    import os

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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


def cmd_init(args: argparse.Namespace) -> int:
    from pathlib import Path

    target = Path(args.target).expanduser().resolve()
    _init_kb(target)
    factlog_config.write_root(target)
    print(f"factlog init: active KB set to {target} (ingest/ask/sync default here from any directory)")
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
        if o and rel not in attr:  # objects of attribute relations are literals, not entities
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
            tags = [t for t, on in (("attribute", name in attr), ("single-valued", name in sv)) if on]
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
    if facts:
        order = ["confirmed", "accepted", "needs_review", "candidate", "superseded"]
        seen = [f"{s}={by_status[s]}" for s in order if by_status.get(s)]
        extra = [f"{s}={n}" for s, n in by_status.items() if s not in order]
        print(f"  facts:      {len(facts)} candidate(s) [{', '.join(seen + extra)}]; {len(engine_rows)} engine fact(s)")
    else:
        print("  facts:      none (no facts/candidates.csv — run /factlog sync)")

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
        by_key: dict[tuple, set] = {}
        for r in engine_rows:
            if r["relation"] in sv:
                by_key.setdefault((r["subject"], r["relation"]), set()).add(r["object"])
        conflicts = {k: v for k, v in by_key.items() if len(v) > 1}
        msg = f"  conflicts:  {len(conflicts)} (over {len(sv)} single-valued relation(s))"
        if conflicts:
            msg += "  ⚠ resolve via superseded / see tools/check_conflicts.py"
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
    from pathlib import Path

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
    target = Path(args.target).expanduser().resolve()
    kb_created = _init_kb(target)
    factlog_config.write_root(target)
    if kb_created:
        actions.append(f"created KB layout at {target}")
    else:
        actions.append(f"KB already present at {target}")
    actions.append(f"set active KB to {target} (ingest/ask/sync default here from any directory)")
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
    conv_origin: dict[str, str] = {}
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
                conv_origin[ref] = PurePosixPath(origin).name

    def matches(ref: str, name: str) -> bool:
        name = nfc(name)
        rp, np_ = Path(ref), Path(name)
        if ref == name:  # exact KB-relative ref
            return True
        is_conv = ref.startswith("runs/sources/")
        if "/" in name:
            # A path was given: the exact original is handled above; for a
            # binary original also match the conversion it produced (by
            # recorded origin). Same-basename files elsewhere are NOT matched.
            return is_conv and conv_origin.get(ref) == np_.name
        if np_.suffix:  # a bare filename with an extension
            if not is_conv:
                return rp.name == np_.name  # an original with that filename
            origin = conv_origin.get(ref)  # the conversion made from this original
            # Provenance is the reliable signal. A headerless conversion falls
            # back to its own name minus the ingest out-suffix: since ingest now
            # keeps the original's extension (report.pptx -> report.pptx.md), the
            # conversion's rp.stem ("report.pptx") is the original's full name.
            return origin == np_.name if origin else rp.stem == np_.name
        # bare stem: every original with that stem, and a conversion made from
        # one (matched via its recorded origin so the source's own extension in
        # the new naming — report.pptx.md — does not defeat the stem compare).
        if is_conv:
            origin = conv_origin.get(ref)
            return Path(origin if origin else rp.name).stem == np_.stem
        return rp.stem == np_.stem

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
        convert_rc = _run_conversion(quiet=True)
        if dry_run:
            for outcome in report.outcomes:
                name = outcome.path.name if outcome.path is not None else ""
                print(f"item\t{outcome.status}\t{outcome.key}\t{name}")
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
        print(f"target\t{target / 'sources'}")
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
    """
    lines = [
        f"candidate\t{o.key}\t{o.candidate.existing_path.name}\t{o.candidate.score:.4f}"
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
    """Emit the shared summary for an openalex import and return the exit code."""
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
                print(f"work\t{outcome.status}\t{outcome.key}\t{name}")
        print(f"imported\t{report.imported}")
        print(f"skipped\t{report.skipped}")
        print(f"merged\t{report.merged}")
        print(f"errors\t{report.errors}")
        print(f"dry_run\t{'1' if dry_run else '0'}")
        print(f"target\t{target / 'sources'}")
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
    if porcelain:
        # `scope` distinguishes the two directions of openalex-cite, which emit
        # two result blocks into one stream; openalex-search leaves it empty.
        prefix = f"\t{scope}" if scope else ""
        for index, work in enumerate(works, 1):
            flag = "retracted" if work.openalex_is_retracted else "-"
            print(f"result{prefix}\t{index}\t{work.openalex_id}\t{flag}\t{work.title or ''}")
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

    from factlog.integrations.arxiv.config import ArxivConfigError, load_config

    porcelain = getattr(args, "porcelain", False)
    target_str, source = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if source in ("config", "cwd") and not porcelain:
        print(f"factlog {command}: target KB {target} (from {source})")
    if not _require_kb(target, command):
        return None
    try:
        return target, load_config(kb_root=target)
    except ArxivConfigError as exc:
        print(f"factlog {command}: {exc}", file=sys.stderr)
        return None


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
    a credit budget — arXiv is free).
    """
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
                print(f"work\t{outcome.status}\t{outcome.key}\t{name}")
        print(f"imported\t{report.imported}")
        print(f"skipped\t{report.skipped}")
        print(f"merged\t{report.merged}")
        print(f"errors\t{report.errors}")
        print(f"dry_run\t{'1' if dry_run else '0'}")
        print(f"target\t{target / 'sources'}")
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

    if porcelain:
        # The agent that withdrew a paper is a prose warning, so it goes to
        # stderr, keeping the machine contract on stdout clean (see below).
        for index, work in enumerate(works, 1):
            flag = "withdrawn" if work.withdrawn else "-"
            print(f"result\t{index}\t{work.versioned_id}\t{flag}\t{work.title or ''}")
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
            print(f"query\t{composed}")
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


def cmd_arxiv_check_versions(args: argparse.Namespace) -> int:
    """Is any arXiv record in the KB behind arXiv's latest? (#78/#79, §11 Step 6).
    Reads the provenance ledgers and a KB-level check-log, queries arXiv, and
    reports version divergences and newly-withdrawn papers.

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
    if not entries and not ledger_errors:
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

    all_results = results + ledger_errors
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
    return 1 if summary.errors or update_errors else 0


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
    if not entries and not ledger_errors:
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

    all_results = results + ledger_errors
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

    from factlog.bibtex import is_annotation_source, read_front_matter, to_bibtex
    from factlog.csl import to_csl

    if getattr(args, "bibtex", False) == getattr(args, "csl", False):
        # neither or both
        print("factlog export: specify exactly one format (--bibtex or --csl)", file=sys.stderr)
        return 2

    target_str, _ = factlog_config.resolve_root(args.target)
    target = Path(target_str)
    if not _require_kb(target, "export"):
        return 1

    sources = []
    for path in sorted((target / "sources").glob("*.md")):
        fm = read_front_matter(path)
        if not fm or is_annotation_source(fm):
            continue
        if not (fm.get("zotero_key") or fm.get("title")):
            continue
        sources.append((path.stem, fm))

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
    init.add_argument("--target", default="~/wiki", help="knowledge base root to create")
    init.set_defaults(func=cmd_init)

    setup = sub.add_parser(
        "setup",
        help="one-shot bootstrap: doctor, ensure deps, init KB, re-check",
    )
    setup.add_argument("--target", default="~/wiki", help="knowledge base root to create")
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
