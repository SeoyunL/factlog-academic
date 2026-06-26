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
MIN_PYREWIRE = (1, 0, 1)


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


def _run_doctor_checks() -> bool:
    """Run and print the doctor checks. Returns True iff all checks pass.

    Shared by `cmd_doctor` and `cmd_setup` so setup reports the exact same
    Python/pyrewire status the standalone doctor would.
    """
    ok = True

    if sys.version_info[:2] >= MIN_PYTHON:
        print(f"OK  Python {sys.version_info.major}.{sys.version_info.minor}")
    else:
        ok = False
        print(f"FAIL Python {sys.version_info.major}.{sys.version_info.minor} < 3.11", file=sys.stderr)

    try:
        import pyrewire  # type: ignore

        current = _version_tuple(str(getattr(pyrewire, "__version__", "0")))
        if current >= MIN_PYREWIRE:
            print(f"OK  pyrewire {getattr(pyrewire, '__version__', '?')}")
        else:
            ok = False
            print(
                f"FAIL pyrewire {getattr(pyrewire, '__version__', '?')} < 1.0.1 "
                "(pip install -r requirements.txt)",
                file=sys.stderr,
            )
    except ImportError:
        ok = False
        print("FAIL pyrewire not installed (pip install -r requirements.txt)", file=sys.stderr)

    return ok


def cmd_doctor(_args: argparse.Namespace) -> int:
    return 0 if _run_doctor_checks() else 1


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
#   number   1,000 / 3.5          -> numeric magnitude (declared only; not yet
#                                    engine-projectable — see issue #125)
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


def cmd_use(args: argparse.Namespace) -> int:
    from pathlib import Path

    target = Path(args.target).expanduser().resolve()
    if not target.is_dir():
        print(f"factlog use: {target} does not exist. Run 'factlog init --target {args.target}' first.", file=sys.stderr)
        return 1
    factlog_config.write_root(target)
    note = "" if (target / "sources").is_dir() else "  (warning: no sources/ — not a factlog KB yet; run 'factlog init')"
    print(f"factlog use: active KB set to {target}{note}")
    print(f"  config: {factlog_config.config_path()}")
    return 0


def cmd_where(args: argparse.Namespace) -> int:
    root, source = factlog_config.resolve_root()
    label = {"env": "env ($FACTLOG_ROOT)", "config": "config file", "cwd": "current directory"}.get(source, source)
    print(f"active KB: {root}")
    print(f"resolved from: {label} (precedence: --flag > $FACTLOG_ROOT > config > cwd)")
    print(f"config file: {factlog_config.config_path()}")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    """List registered sources: original file, its conversion, and fact count."""
    import csv
    import unicodedata
    from pathlib import Path

    from factlog.common import is_sync_ignored, source_rel_key, sync_ignore_patterns

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
        conv_ref = conv.get(source_rel_key(orig_ref), "")
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

    def is_old(d: dict) -> bool:
        return (fld(d, "subject"), fld(d, "relation"), fld(d, "object")) == old

    matched = [r for r in rows if is_old(r)]
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
    changed = 0
    for r in rows:
        if is_old(r):
            for k, v in sets.items():
                r[k] = v
            if args.accept:
                r["status"] = "accepted"
            changed += 1
    _atomic_write_csv(csv_path, rows, out_fields)

    # 2. runs/*.json (durability) — a value lives here; merge rebuilds from it
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
            for item in data:
                if isinstance(item, dict) and (
                    nfc(str(item.get("subject", "")).strip()),
                    nfc(str(item.get("relation", "")).strip()),
                    nfc(str(item.get("object", "")).strip()),
                ) == old:
                    for k, v in sets.items():
                        item[k] = v
                    dirty = True
                    runs_changed += 1
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
    """
    import csv
    import unicodedata
    from pathlib import Path

    from factlog.common import normalize_confidence, source_file_refs

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

    matched = [r for r in rows if all(field(r, k) == v for k, v in filt.items())]
    if not matched:
        shown = ", ".join(f"{k}={v}" for k, v in filt.items())
        print(f"factlog provenance: no fact matches ({shown})", file=sys.stderr)
        return 1

    on_disk = source_file_refs(target)  # NFC-normalised refs of files that exist

    groups: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for r in matched:
        groups.setdefault((field(r, "subject"), field(r, "relation"), field(r, "object")), []).append(r)

    distinct_sources: set[str] = set()
    stale_rows = 0
    print(f"factlog provenance (KB: {target}): {len(groups)} fact(s), {len(matched)} source row(s)")
    for (s, rel, o), grp in groups.items():
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
        if any(part.startswith(".") for part in p.relative_to(target).parts):
            continue  # hidden (.DS_Store, .git, ...)
        ref = unicodedata.normalize('NFC', p.relative_to(target).as_posix())
        if common.is_sync_ignored(ref, patterns):
            n_ignored += 1  # excluded from sync on purpose — not a gap
            continue
        refs[p] = ref
    # only a *text* conversion under runs/sources/ backs an original (a stray
    # binary there is an anomaly, not a usable conversion — matches coverage).
    covered_keys = {
        common.source_rel_key(ref)
        for p, ref in refs.items()
        if ref.startswith("runs/sources/") and ref in cited and common.is_text_source(p)
    }
    direct = sum(1 for ref in refs.values() if ref in cited)
    via = sum(
        1
        for p, ref in refs.items()
        if ref not in cited
        and ref.startswith("sources/")
        and not common.is_text_source(p)
        and common.source_rel_key(ref) in covered_keys
    )
    covered = direct + via
    total = len(refs)
    via_note = f" ({via} via conversion)" if via else ""
    excl_note = f", {n_ignored} sync-ignored" if n_ignored else ""
    print(f"  sources:    {total} file(s), {covered} with facts{via_note}, {total - covered} with none{excl_note}")

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
    """Attempt ``python3 -m pip install -r <requirements>``.

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
            "    python3 -m venv ~/.factlog-venv\n"
            "    source ~/.factlog-venv/bin/activate\n"
            "    python3 -m factlog setup --target <kb>\n",
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

    print("\n=== factlog setup: final environment check ===")
    final_ok = _run_doctor_checks()

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
    """Heuristic inverse of merge_candidates.is_text_source for --scan discovery.

    Treats a file as binary if its first *sniff* bytes contain a NUL or do not
    decode as UTF-8 (tolerating a multi-byte char truncated at the boundary).
    """
    try:
        chunk = path.read_bytes()[:sniff]
    except OSError:
        return True
    if b"\x00" in chunk:
        return True
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError as exc:
        return exc.start < len(chunk) - 3
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
    if args.scan:
        patterns = sync_ignore_patterns(target)
        ignored = 0
        for path in sorted(p for p in (target / "sources").rglob("*") if p.is_file()):
            if path.name.startswith(".") or not _looks_binary(path):
                continue
            ref = unicodedata.normalize("NFC", path.relative_to(target).as_posix())
            if is_sync_ignored(ref, patterns):
                ignored += 1
                continue
            work.append(path)
        if ignored:
            print(f"factlog ingest --scan: skipped {ignored} sync-ignored source(s)")
    if not work:
        if args.scan:
            print("factlog ingest --scan: no binary source files to convert")
            return 0
        print("factlog ingest: no input files (give file paths or --scan)", file=sys.stderr)
        return 2

    converted = 0
    skipped = 0
    failures = 0
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
        # source (sources/sub/x.pdf) converts to runs/sources/sub/x.md — never a
        # flat name that would collide with a same-stem file in another subdir.
        # An explicitly-named path outside sources/ has no subtree to mirror, so
        # it falls back to a flat output name.
        try:
            rel_parent = src.resolve().relative_to(sources_dir).parent
        except (ValueError, OSError):
            rel_parent = Path()
        dst = derived / rel_parent / (src.stem + out_suffix)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and not args.force and dst.stat().st_mtime >= src.stat().st_mtime:
            print(f"factlog ingest: {dst.relative_to(target).as_posix()} up to date; skipping {src.name}")
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
            header = f"<!-- ingested-by-factlog | source: {src.name} | converter: {tool} | date: {when} -->\n\n"
        else:
            header = f"[ingested-by-factlog] source: {src.name} | converter: {tool} | date: {when}\n\n"
        dst.write_text(header + body, encoding="utf-8")

        converted += 1
        print(f"factlog ingest: {src.name} -> {dst.relative_to(target).as_posix()} (via {tool})")

    print(f"factlog ingest: {converted} converted, {skipped} skipped, {failures} failed")
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
    from pathlib import Path

    from factlog.common import source_rel_key

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
                conv_origin[ref] = origin

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
            return origin == np_.name if origin else rp.stem == np_.stem
        return rp.stem == np_.stem  # bare stem: every source with that stem

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
        #  - a *mirrored* conversion (runs/sources/<sub>/x.md) carries the
        #    original's subdir, so we pair by subdir-aware rel key. A flat
        #    basename set used to mask an orphan when same-name originals lived
        #    in different subtrees (sources/a/report.pdf vs sources/b/report.pdf):
        #    deleting only a/report.pdf left report.pdf in the set (from b), so
        #    a/report's conversion was never flagged.
        #  - a *flat* conversion (runs/sources/x.md — legacy layout, or an
        #    original ingested without a subtree to mirror) has no subdir, so
        #    the provenance basename is the only origin signal; fall back to it
        #    and keep erring toward retention.
        src_basenames = {Path(r).name for r in disk_refs if not r.startswith("runs/sources/")}
        src_relkeys = {source_rel_key(r) for r in disk_refs if not r.startswith("runs/sources/")}
        for ref in all_refs:
            if ref.startswith("runs/sources/"):
                if ref in disk_refs:
                    origin = conv_origin.get(ref)
                    # origin is not None == has a factlog provenance header
                    # (hand-placed conversions are kept).
                    if origin is not None:
                        ck = source_rel_key(ref)
                        paired = ck in src_relkeys if "/" in ck else origin in src_basenames
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
    use.set_defaults(func=cmd_use)

    where = sub.add_parser("where", help="print the active KB and where it was resolved from")
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
