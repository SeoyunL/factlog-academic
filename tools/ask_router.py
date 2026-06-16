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
    QUERY_FACT_ABSENT,
    QUERY_OK,
    _arg_value,
    _is_quoted_string,
    _is_variable,
    _query_args,
    classify_query,
    dependency_graph,
    dependency_path,
    load_accepted_facts,
    policy_predicates,
    run_wirelog,
)


def _policy_program_optional() -> str:
    """Return the compiled policy text, or '' if it has not been generated yet.

    `/factlog ask` is interactive and must work before `/factlog check` compiles
    `policy/logic-policy.dl`; a missing policy simply means no policy predicates.
    """
    return LOGIC_POLICY_DL.read_text(encoding="utf-8") if LOGIC_POLICY_DL.is_file() else ""

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
    ok, code, reason = classify_query(draft, facts, policy_program=_policy_program_optional())
    predicate = _predicate_of(draft)

    # Route on the stable classification CODE, never on the reason text — so an
    # entity/relation constant can never masquerade as a routing signal.
    if code == QUERY_OK:
        route, negative = "engine", False
    elif code == QUERY_FACT_ABSENT:
        # Accepted vocabulary, fact/path absent: a verified negative — an engine
        # answer, never demoted to wiki.
        route, negative = "engine", True
    else:
        # review_required or any shape/vocabulary failure: cannot be expressed
        # over accepted facts.
        route, negative = "wiki", False

    return {
        "ok": ok,
        "code": code,
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


def _reachable_pairs(facts: list[dict[str, str]]) -> set[tuple[str, str]]:
    """Transitive closure of edge(S,O) :- relation(S, _, O), pure-python.

    Mirrors the wirelog `path` semantics (WIRELOG_PROGRAM) without needing the
    engine, so variable `path` queries resolve even before `/factlog check`.
    """
    graph = dependency_graph(facts)
    pairs: set[tuple[str, str]] = set()
    for start in list(graph):
        seen: set[str] = set()
        stack = list(graph.get(start, []))
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            pairs.add((start, node))
            stack.extend(graph.get(node, []))
    return pairs


def evaluate(draft: str, facts: list[dict[str, str]]) -> dict[str, object]:
    """Evaluate a validated engine query: relation, path, or a policy predicate.

    - relation: match against accepted facts.
    - path: a fully-quoted query returns the dependency path (or none); a query
      with a variable returns the reachable (start, target) pairs.
    - policy predicate: the inferred (entity, reason) rows from the engine,
      optionally filtered by a quoted entity argument.

    A truly unknown predicate raises NotImplementedError rather than returning 0
    rows, so a caller never mistakes an unsupported predicate for a verified
    negative.
    """
    predicate = _predicate_of(draft)
    args = _query_args(draft)
    if predicate == "relation":
        rows = evaluate_relation(draft, facts)
        return {"rows": rows, "count": len(rows)}
    if predicate == "path":
        if len(args) == 2 and all(_is_quoted_string(a) for a in args):
            path = dependency_path(facts, _arg_value(args[0]), _arg_value(args[1]))
            rows = [path] if path else []
        else:
            rows = [
                [start, target]
                for (start, target) in sorted(_reachable_pairs(facts))
                if (len(args) == 2
                    and (_is_variable(args[0]) or _arg_value(args[0]) == start)
                    and (_is_variable(args[1]) or _arg_value(args[1]) == target))
            ]
        return {"rows": rows, "count": len(rows)}
    if predicate in policy_predicates(_policy_program_optional()):
        inferred = run_wirelog()
        rows = []
        for row in sorted(inferred.get(predicate, set())):
            if args and _is_quoted_string(args[0]) and (not row or _arg_value(args[0]) != row[0]):
                continue
            rows.append(list(row))
        return {"rows": rows, "count": len(rows)}
    raise NotImplementedError(f"engine evaluation of predicate '{predicate}' is not supported")


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
# decisions/ (human review notes / open questions) is searched as clearly-labeled
# SUPPLEMENTARY context — useful for an unanswered question, but tagged so it is
# never conflated with source ground truth. pages/ stays excluded entirely.
WIKI_SUPPLEMENTARY_DIRS = ("decisions",)
_EXCERPT_WINDOW = 3


def _wiki_corpus() -> list[tuple[str, str]]:
    """(relative dir, display label) pairs for the wiki search, primary first."""
    corpus = [(rel, rel) for rel in WIKI_SOURCE_DIRS]
    corpus += [(rel, f"{rel} (supplementary)") for rel in WIKI_SUPPLEMENTARY_DIRS]
    return corpus


def _is_cjk(word: str) -> bool:
    """True if *word* contains a Hangul / CJK / kana character."""
    return any(
        "가" <= ch <= "힣"  # Hangul syllables
        or "一" <= ch <= "鿿"  # CJK unified ideographs
        or "぀" <= ch <= "ヿ"  # Hiragana + Katakana
        for ch in word
    )


def _keyword_patterns(question: str) -> list[re.Pattern[str]]:
    """Keyword matchers for the question, bilingual:

    - ASCII words (len>2): word-boundary match — avoids substring false positives
      (e.g. 'api' in 'therapist').
    - CJK words (len>=2): substring match — CJK content words are commonly two
      characters, and substring tolerates attached particles/조사 (e.g. '근거'
      matches '근거는'). CJK compounding has no word delimiters, so a 2-char
      query can substring-match inside an unrelated compound; this recall-over-
      precision trade-off is acceptable for the UNVERIFIED exploration surface,
      but do NOT reuse this matcher on a precision-sensitive path.
    """
    seen: set[str] = set()
    patterns: list[re.Pattern[str]] = []
    for word in re.findall(r"\w+", question.lower(), flags=re.UNICODE):
        if word in seen:
            continue
        if _is_cjk(word):
            if len(word) >= 2:
                seen.add(word)
                patterns.append(re.compile(re.escape(word)))
        elif len(word) > 2:
            seen.add(word)
            patterns.append(re.compile(rf"\b{re.escape(word)}\b"))
    return patterns


def _sanitize(line: str) -> str:
    """Drop non-printable control characters (keep tabs) so a malformed source
    cannot smuggle NUL/ANSI/control bytes into a rendered answer."""
    return "".join(ch for ch in line if ch == "\t" or ch.isprintable())


def _excerpt_score(excerpt: str, patterns: list[re.Pattern[str]]) -> tuple[int, int]:
    """Relevance of an excerpt to the query: (distinct keyword coverage, total
    match frequency). An excerpt covering more of the query's keywords ranks
    above one that merely repeats a single keyword — so the most relevant excerpt
    surfaces even under a small result cap."""
    low = excerpt.lower()
    coverage = sum(1 for pat in patterns if pat.search(low))
    frequency = sum(len(pat.findall(low)) for pat in patterns)
    return (coverage, frequency)


def _semantic_rerank(question: str, results: list[dict[str, object]]) -> list[dict[str, object]]:
    """Optional neural re-rank. Bundled retrieval is lexical (relevance-ranked);
    a neural backend is NOT bundled (it would need a model + network, breaking
    deterministic/offline CI). If the env var FACTLOG_EMBED_MODULE names an
    importable module exposing ``rank(question, texts) -> list[float]`` (higher =
    more similar), results are reordered by it. Any absence/failure → unchanged
    (graceful degrade)."""
    module_name = os.environ.get("FACTLOG_EMBED_MODULE")
    if not module_name or not results:
        return results
    try:
        import importlib

        backend = importlib.import_module(module_name)
        scores = backend.rank(question, [str(r["excerpt"]) for r in results])
        if not isinstance(scores, list) or len(scores) != len(results):
            return results
        order = sorted(range(len(results)), key=lambda i: float(scores[i]), reverse=True)
        return [results[i] for i in order]
    except Exception:
        return results  # graceful degrade to lexical ranking


def search(question: str, root: Path, *, limit: int = 10) -> list[dict[str, object]]:
    """Relevance-ranked search over the wiki corpus (sources/ + runs/sources/).

    Collects keyword-matched excerpts, ranks them by relevance (keyword coverage,
    then frequency), optionally re-ranks via a neural backend (graceful degrade
    when absent), and returns the top *limit* cited excerpts: {file, line,
    excerpt, dir}. Binary files (e.g. an un-converted .docx) are skipped.
    """
    patterns = _keyword_patterns(question)
    if not patterns:
        return []
    scored: list[tuple[tuple[int, int], dict[str, object]]] = []
    for rel, label in _wiki_corpus():
        base = root / rel
        if not base.is_dir():
            continue
        base_resolved = base.resolve()
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            # Stay within the corpus root: never follow a symlink out of the KB.
            if not path.resolve().is_relative_to(base_resolved):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # unreadable — skip
            if "\x00" in text:
                continue  # binary (valid-UTF-8-with-NUL) — skip
            lines = text.splitlines()
            last_end = -1  # collapse overlapping windows within this file
            for i, line in enumerate(lines):
                low = line.lower()
                if not any(pat.search(low) for pat in patterns):
                    continue
                start = max(0, i - _EXCERPT_WINDOW)
                if start <= last_end:
                    continue  # window overlaps the previously emitted excerpt
                end = min(len(lines), i + _EXCERPT_WINDOW + 1)
                last_end = end - 1
                excerpt = "\n".join(_sanitize(line_text) for line_text in lines[start:end])
                result = {
                    "file": path.relative_to(root).as_posix(),
                    "line": i + 1,
                    "excerpt": excerpt,
                    "dir": label,
                }
                scored.append((_excerpt_score(excerpt, patterns), result))
    # Rank by relevance (desc); ties keep corpus/line order (stable sort over the
    # already-ordered collection). Then take the cap, then optional neural rerank.
    scored.sort(key=lambda item: item[0], reverse=True)
    ranked = [result for _score, result in scored][:limit]
    return _semantic_rerank(question, ranked)


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
        f"sources searched: {', '.join(label for _rel, label in _wiki_corpus())}",
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
    question = " ".join(question.split())  # collapse newlines/runs so one bullet
    sink = root / "decisions" / "ask-open-questions.md"
    if not question:
        return sink  # nothing to record
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
        # Positive engine answer: relation, path, and policy predicates are all
        # evaluated by the engine and rendered (0 rows -> a verified-empty result,
        # never a wiki fallback).
        print(render_engine_answer(args.draft, evaluate(args.draft, facts)["rows"]))
        return 0
    # route == wiki
    print(json.dumps({"route": "wiki", "reason": decision["reason"]}, ensure_ascii=False))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    root = Path(os.environ["FACTLOG_ROOT"])
    print(json.dumps({"results": search(args.text, root)}, ensure_ascii=False))
    return 0


def cmd_wiki(args: argparse.Namespace) -> int:
    root = Path(os.environ["FACTLOG_ROOT"])
    results = search(args.text, root)
    print(render_wiki_answer(args.text, args.reason, results))
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    root = Path(os.environ["FACTLOG_ROOT"])
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
