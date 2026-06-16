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

# The distinctive phrase both fact-absence reasons share (relation + path).
# See common.validate_candidate_query: "relation query does not match accepted
# facts" and "path query does not match accepted facts".
_FACT_ABSENCE_MARKER = "does not match accepted facts"


def classify(draft: str, facts: list[dict[str, str]]) -> dict[str, object]:
    """Route a draft to engine vs wiki by the validator's reason class.

    Returns {ok, reason, route, negative, predicate}. Pure: no I/O beyond the
    validator, which only reads the accepted facts already loaded by the caller.
    """
    ok, reason = validate_candidate_query(draft, facts, policy_program=_policy_program_optional())
    predicate = draft.split("(", 1)[0].strip()

    if ok:
        if predicate == "review_required":
            route, negative = "wiki", False
        else:
            route, negative = "engine", False
    elif _FACT_ABSENCE_MARKER in reason:
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
    predicate = draft.split("(", 1)[0].strip()
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
        rows = evaluate_relation(args.draft, facts) if decision["predicate"] == "relation" else []
        if decision["predicate"] != "relation":
            # Defer non-relation engine evaluation; do not fabricate a result.
            print(
                json.dumps(
                    {"route": "engine", "deferred": decision["predicate"], "reason": decision["reason"]},
                    ensure_ascii=False,
                )
            )
            return 0
        print(render_engine_answer(args.draft, rows))
        return 0
    # route == wiki
    print(json.dumps({"route": "wiki", "reason": decision["reason"]}, ensure_ascii=False))
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
