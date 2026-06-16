#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Deterministic search router for `/factlog ask`.

Given an LLM-drafted candidate Datalog query, decide — by deterministic code,
never by LLM judgment — whether the question is answered by the facts/rule
ENGINE or routed to WIKI exploration, and (for the engine path) evaluate it.

Routing is keyed on the *reason class* returned by
``common.validate_candidate_query`` (NOT a raw boolean):

    ok=True,  predicate != review_required  -> route=engine (positive/negative)
    ok=True,  predicate == review_required  -> route=wiki
    ok=False, reason is fact-absence        -> route=engine, negative=True
                                               (vocabulary accepted, fact absent)
    ok=False, reason is shape/vocabulary    -> route=wiki

A *verified negative* (engine ran, no matching fact/path) is an engine result —
it is NEVER demoted to unverified wiki prose. Conflating "engine says no" with
"cannot express" is the most damaging routing error this module guards against.

The validator is always called with ``load_accepted_facts()`` (engine input
only), never ``load_facts()`` (candidates), so candidate vocabulary cannot leak
into the engine path.

This module is READ-ONLY with respect to engine inputs: it never writes
``facts/query.dl`` or ``facts/accepted.dl``.

Usage:
    python3 ask_router.py validate "<draft>" [--target <kb>]
    python3 ask_router.py evaluate "<draft>" [--target <kb>]
    python3 ask_router.py render   "<draft>" [--target <kb>]

Each subcommand prints JSON (validate/evaluate) or the rendered answer (render)
to stdout. --target overrides FACTLOG_ROOT (authoritative).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Ensure tools/ is importable when run directly, and resolve the KB root BEFORE
# importing common (whose module-level ROOT captures FACTLOG_ROOT at import).
_TOOLS_DIR = Path(__file__).parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


def _resolve_target_prepass() -> str:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--target", default=None)
    known, _ = pre.parse_known_args()
    if known.target:
        return str(Path(known.target).expanduser().resolve())
    return str(Path(os.environ.get("FACTLOG_ROOT", ".")).expanduser().resolve())


os.environ["FACTLOG_ROOT"] = _resolve_target_prepass()

from common import (  # noqa: E402
    LOGIC_POLICY_DL,
    _arg_value,
    _is_variable,
    _query_args,
    load_accepted_facts,
    validate_candidate_query,
)


def _policy_program_optional() -> str:
    """Return the compiled policy text, or '' if it has not been generated yet.

    `/factlog ask` is interactive and must work before `/factlog check` compiles
    `policy/logic-policy.dl`; a missing policy simply means no policy predicates.
    """
    return LOGIC_POLICY_DL.read_text(encoding="utf-8") if LOGIC_POLICY_DL.is_file() else ""

# The EXACT validator reasons that denote a fact-absence (a verified negative):
# accepted vocabulary, but the specific fact/path is simply not present. Matched
# exactly — never as a substring — so an unaccepted entity/relation name that
# happens to contain this phrase cannot masquerade as a verified negative.
_FACT_ABSENCE_REASONS = frozenset(
    {
        "relation query does not match accepted facts",
        "path query does not match accepted facts",
    }
)


def _predicate_of(draft: str) -> str:
    """Parse the predicate name the way the validator does (regex), so the router
    and the validator never disagree about what predicate a draft calls."""
    match = re.match(r"^([A-Za-z_]\w*)\(", draft.strip())
    return match.group(1) if match else ""


def classify(draft: str, facts: list[dict[str, str]]) -> dict[str, object]:
    """Route a draft to engine vs wiki by the validator's reason class.

    Returns {ok, reason, route, negative, predicate}. Pure: no I/O beyond the
    validator, which only reads the accepted facts already loaded by the caller.
    """
    ok, reason = validate_candidate_query(draft, facts, policy_program=_policy_program_optional())
    predicate = _predicate_of(draft)

    if ok:
        if predicate == "review_required":
            route, negative = "wiki", False
        else:
            route, negative = "engine", False
    elif reason in _FACT_ABSENCE_REASONS:
        # Vocabulary is accepted; the specific fact/path is simply absent.
        # This is a verified negative — an engine answer, not a wiki fallback.
        route, negative = "engine", True
    else:
        # Shape/vocabulary failure: the question cannot be expressed over
        # accepted facts.
        route, negative = "wiki", False

    return {
        "ok": ok,
        "reason": reason,
        "route": route,
        "negative": negative,
        "predicate": predicate,
    }


def evaluate_relation(draft: str, facts: list[dict[str, str]]) -> list[list[str]]:
    """Evaluate a single ``relation(...)`` query against accepted facts.

    Quoted constants must match the corresponding field; variables bind freely.
    Returns the matching [subject, relation, object] rows. Does not touch
    facts/query.dl.
    """
    args = _query_args(draft)
    if len(args) != 3:
        return []
    rows: list[list[str]] = []
    for row in facts:
        values = [row["subject"], row["relation"], row["object"]]
        if all(_is_variable(arg) or _arg_value(arg) == value for arg, value in zip(args, values)):
            rows.append([row["subject"], row["relation"], row["object"]])
    return rows


def evaluate(draft: str, facts: list[dict[str, str]]) -> dict[str, object]:
    """Evaluate a validated engine query. Phase 1 supports ``relation`` only.

    ``path`` and policy predicates are deferred to a later phase; this raises a
    NotImplementedError rather than returning 0 rows, so the caller never
    mistakes a deferred predicate for a verified negative.
    """
    predicate = _predicate_of(draft)
    if predicate == "relation":
        rows = evaluate_relation(draft, facts)
        return {"rows": rows, "count": len(rows)}
    raise NotImplementedError(
        f"engine evaluation of predicate '{predicate}' is not implemented in this "
        "version (path/policy evaluation is tracked separately)"
    )


def render_engine_answer(draft: str, rows: list[list[str]]) -> str:
    """Render the VERIFIED — engine answer block (positive or negative).

    The literal marker 'VERIFIED — engine' is the greppable verification token.
    """
    lines = ["VERIFIED — engine", f"query: {draft}", f"rows: {len(rows)}"]
    if rows:
        lines.extend(f"  - {', '.join(row)}" for row in rows)
    else:
        lines.append("no such fact (verified negative)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Path B — wiki exploration (UNVERIFIED)
# ---------------------------------------------------------------------------
# The wiki corpus is the user's source text ONLY: sources/ (originals) and
# runs/sources/ (text conversions of binary originals). pages/ is DELIBERATELY
# EXCLUDED — it is engine-derived from candidates.csv (including needs_review /
# candidate rows), so grepping it would re-surface facts the engine never
# accepted, leaking candidate vocabulary into an answer as if it were knowledge.
WIKI_SOURCE_DIRS = ("sources", "runs/sources")
_EXCERPT_WINDOW = 3


def _keywords(question: str) -> list[str]:
    return [w.lower() for w in re.findall(r"\w+", question, flags=re.UNICODE) if len(w) > 2]


def search(question: str, root: Path, *, limit: int = 10) -> list[dict[str, object]]:
    """Keyword search over the wiki corpus (sources/ + runs/sources/ only).

    Returns up to *limit* cited excerpts: {file, line, excerpt, dir}. Binary
    files (e.g. an un-converted .docx) are skipped (they do not decode as text);
    their conversions live in runs/sources/ and are searched there.
    """
    keywords = _keywords(question)
    if not keywords:
        return []
    results: list[dict[str, object]] = []
    seen: set[tuple[str, int]] = set()
    for rel in WIKI_SOURCE_DIRS:
        base = root / rel
        if not base.is_dir():
            continue
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue  # binary / unreadable — skip
            for i, line in enumerate(lines):
                low = line.lower()
                if not any(kw in low for kw in keywords):
                    continue
                # Collapse overlapping windows in the same file into one excerpt.
                key = (path.as_posix(), i // (_EXCERPT_WINDOW * 2 + 1))
                if key in seen:
                    continue
                seen.add(key)
                start = max(0, i - _EXCERPT_WINDOW)
                end = min(len(lines), i + _EXCERPT_WINDOW + 1)
                results.append(
                    {
                        "file": path.relative_to(root).as_posix(),
                        "line": i + 1,
                        "excerpt": "\n".join(lines[start:end]),
                        "dir": rel,
                    }
                )
    return results[:limit]


def render_wiki_answer(question: str, reason: str, results: list[dict[str, object]]) -> str:
    """Render the UNVERIFIED — wiki exploration answer block.

    The literal marker 'UNVERIFIED — wiki exploration' is the greppable token.
    Citations point only at source text (sources/ , runs/sources/); this answer
    never cites facts/accepted.dl, so its provenance alone marks it unverified.
    """
    lines = [
        "UNVERIFIED — wiki exploration",
        f"question: {question}",
        f"reason: {reason}",
        f"sources searched: {', '.join(WIKI_SOURCE_DIRS)}",
    ]
    if results:
        for r in results:
            lines.append(f"[{r['file']}:{r['line']}] ({r['dir']})")
            for excerpt_line in str(r["excerpt"]).splitlines():
                lines.append(f"    {excerpt_line}")
    else:
        lines.append("(no matching source excerpts found)")
    lines.append("WARNING: unverified candidates — do not treat as confirmed facts.")
    return "\n".join(lines)


def record_open_question(question: str, root: Path) -> Path:
    """Append an unanswered question to a NON-engine-input sink for later review.

    Writes to decisions/ask-open-questions.md (not guarded by the PreToolUse
    gate, never engine input), so interactive ask never touches facts/query.dl.
    Idempotent: a question already present is not duplicated.
    """
    sink = root / "decisions" / "ask-open-questions.md"
    sink.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Ask — open questions\n\n"
        "Unanswered `/factlog ask` questions, kept for later review. This file is\n"
        "NOT engine input; promote items into policy/questions.md deliberately.\n"
    )
    text = sink.read_text(encoding="utf-8") if sink.is_file() else header
    bullet = f"- {question}\n"
    if bullet not in text:
        if not text.endswith("\n"):
            text += "\n"
        text += bullet
    sink.write_text(text, encoding="utf-8")
    return sink


def cmd_validate(args: argparse.Namespace) -> int:
    facts = load_accepted_facts()
    print(json.dumps(classify(args.draft, facts), ensure_ascii=False))
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    facts = load_accepted_facts()
    try:
        result = evaluate(args.draft, facts)
    except NotImplementedError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    """Validate + (engine) evaluate + render. Wiki rendering is out of scope for
    this module; for route=wiki this prints a machine-readable directive so the
    caller (the skill) can run wiki exploration."""
    facts = load_accepted_facts()
    decision = classify(args.draft, facts)
    if decision["route"] == "engine":
        # A verified negative is proven by the validator regardless of predicate,
        # so it is always renderable as an engine answer — never demoted.
        if decision["negative"]:
            print(render_engine_answer(args.draft, []))
            return 0
        # Positive engine answer: relation is evaluated now; non-relation
        # positives (path/policy) are deferred — emit a directive, never a
        # fabricated result.
        if decision["predicate"] == "relation":
            print(render_engine_answer(args.draft, evaluate(args.draft, facts)["rows"]))
            return 0
        print(
            json.dumps(
                {"route": "engine", "deferred": decision["predicate"], "reason": decision["reason"]},
                ensure_ascii=False,
            )
        )
        return 0
    # route == wiki
    print(json.dumps({"route": "wiki", "reason": decision["reason"]}, ensure_ascii=False))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from pathlib import Path as _Path

    root = _Path(os.environ["FACTLOG_ROOT"])
    print(json.dumps({"results": search(args.text, root)}, ensure_ascii=False))
    return 0


def cmd_wiki(args: argparse.Namespace) -> int:
    from pathlib import Path as _Path

    root = _Path(os.environ["FACTLOG_ROOT"])
    results = search(args.text, root)
    print(render_wiki_answer(args.text, args.reason, results))
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    from pathlib import Path as _Path

    root = _Path(os.environ["FACTLOG_ROOT"])
    sink = record_open_question(args.text, root)
    print(json.dumps({"recorded": args.text, "sink": sink.relative_to(root).as_posix()}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ask_router", description="Deterministic /factlog ask router")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func, helptext in (
        ("validate", cmd_validate, "classify a draft query to engine vs wiki (JSON)"),
        ("evaluate", cmd_evaluate, "evaluate a relation query against accepted facts (JSON)"),
        ("render", cmd_render, "validate+evaluate+render the engine answer, or emit a wiki directive"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("draft", help="the candidate Datalog query line")
        p.add_argument("--target", default=None, help="KB root (overrides FACTLOG_ROOT)")
        p.set_defaults(func=func)

    # Path B (wiki) subcommands take the natural-language question, not a draft.
    search_p = sub.add_parser("search", help="search the wiki corpus (sources/ + runs/sources/) (JSON)")
    search_p.add_argument("text", help="the natural-language question")
    search_p.add_argument("--target", default=None, help="KB root (overrides FACTLOG_ROOT)")
    search_p.set_defaults(func=cmd_search)

    wiki_p = sub.add_parser("wiki", help="render the UNVERIFIED — wiki exploration answer")
    wiki_p.add_argument("text", help="the natural-language question")
    wiki_p.add_argument("--reason", default="not expressible over accepted facts", help="why the engine path did not apply")
    wiki_p.add_argument("--target", default=None, help="KB root (overrides FACTLOG_ROOT)")
    wiki_p.set_defaults(func=cmd_wiki)

    note_p = sub.add_parser("note", help="record an unanswered question to the non-engine-input sink")
    note_p.add_argument("text", help="the natural-language question")
    note_p.add_argument("--target", default=None, help="KB root (overrides FACTLOG_ROOT)")
    note_p.set_defaults(func=cmd_note)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
