#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run deterministic logic checks over facts and query drafts.

Usage:
    python3 run_logic_check.py [--wiki <kb>]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


# Resolve the KB root and export it before importing common, which binds
# its module-level paths from FACTLOG_ROOT at import time.
import factlog_config  # noqa: E402

os.environ["FACTLOG_ROOT"] = factlog_config.resolve_root_from_argv("--wiki")

from common import (  # noqa: E402
    FACTS_DIR,
    KNOWN_STATUSES,
    QUERY_PREDICATES,
    allowed_relations,
    dependency_path,
    value_set,
    ensure_dirs,
    load_accepted_facts,
    load_facts,
    load_logic_policy,
    policy_predicates,
    review_facts,
    LOGIC_POLICY_DL,
    run_wirelog,
    arg_value,
    is_quoted_string,
    query_args,
    query_shape_error,
    quoted_constants,
)


def query_lines() -> list[str]:
    query_file = FACTS_DIR / "query.dl"
    if not query_file.exists():
        return []
    return [
        line.strip()
        for line in query_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("//")
    ]


# Query parsing is delegated to common's string-aware parsers
# (_query_args / _arg_value / _quoted_constants, imported above) so this engine
# and the ask router agree on every query — notably commas inside quoted literals
# like relation("A", "born_in", "Paris, France")?, which a naive split(",") would
# mis-count as 4 args and report as "0 rows".


def relation_results(line: str, facts: list[dict[str, str]]) -> list[tuple[str, str, str]]:
    args = query_args(line)
    if len(args) != 3:
        return []
    fields = ["subject", "relation", "object"]
    rows: list[tuple[str, str, str]] = []
    for row in facts:
        matched = True
        for arg, field in zip(args, fields, strict=True):
            if is_quoted_string(arg) and arg_value(arg) != row[field]:
                matched = False
                break
        if matched:
            rows.append((row["subject"], row["relation"], row["object"]))
    return rows


def shape_error(label: str, line: str) -> str | None:
    """The report's verdict on *line*'s ARGUMENT SHAPE, or None when valid.

    The rule is not restated here: ``common.query_shape_error`` is the same
    function ``classify_query`` applies, so the gate and the report cannot
    disagree about which lines are malformed, nor word the verdict differently.
    They used to, on every predicate:

    - ``count`` was checked on ARITY ONLY, so ``count("S", 'r')?`` reached
      ``evaluate_queries``, where a non-double-quoted argument is treated as a
      WILDCARD rather than a filter. The count then ranged over every relation of
      that subject and was printed as an engine-verified aggregate (#328). An
      aggregate is the output a reader is least able to check by eye, which is
      why answering it wrongly is worse than not answering it.
    - ``relation`` and ``path`` were not shape-checked AT ALL — they fell through
      to the generic warning loop. Same mechanism, wider blast radius:
      ``relation("Marie Curie", 'born_in', O)?`` reported rows spanning every
      relation of the subject, each carrying a nonsense binding
      (``'born_in'=worked_at``).
    - a policy query was checked on arity only, so ``stale_entity(Alice, stale)?``
      had both bare tokens taken for variables and rendered the predicate's WHOLE
      extent with invented bindings (``Alice=Bob``).

    Callers pair this with the arity rule their predicate has (see
    ``count_query_error`` / ``policy_query_error``) and use ONE verdict for both
    the Errors section and the answer renderer, so the report cannot call a line
    an error and answer it in the same run.

    Message wording is the gate's, with the offending line appended — the
    convention every other error in this module follows.
    """
    message = query_shape_error(label, query_args(line))
    return f"{message}: {line}" if message else None


def count_query_error(line: str) -> str | None:
    """The report's single verdict on a count query, or None when it is answerable.

    Arity before shape, the order ``classify_query``'s count branch uses, so the
    two paths give the same reason for a line that violates both.
    """
    if len(query_args(line)) != 2:
        return f"count query must have subject and relation arguments: {line}"
    return shape_error("count", line)


def policy_query_error(line: str) -> str | None:
    """The report's single verdict on a policy query, or None when answerable.

    Same arity-then-shape order as ``count_query_error`` and as the gate's policy
    branch. ``validate_query`` and ``policy_result_line`` both route through it,
    which is what keeps the Errors section and the rendered answer from
    disagreeing — the arity half of that pairing is what ``1bc172a`` established.
    """
    if len(query_args(line)) != 2:
        return f"policy query must have entity and reason arguments: {line}"
    return shape_error("policy query", line)


def validate_query(line: str, entities: set[str], policy_query_predicates: set[str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    predicate = line.split("(", 1)[0]
    if predicate not in QUERY_PREDICATES and predicate not in policy_query_predicates:
        errors.append(f"query unknown predicate: {line}")
        return errors, warnings
    if not line.endswith("?"):
        errors.append(f"query must end with ?: {line}")
    if predicate == "review_required":
        constants = quoted_constants(line)
        if len(constants) != 1:
            errors.append(f"review_required must include the original question string: {line}")
        return errors, warnings
    if predicate in policy_query_predicates:
        policy_error = policy_query_error(line)
        if policy_error:
            errors.append(policy_error)
            return errors, warnings
        args = query_args(line)
        if is_quoted_string(args[0]) and arg_value(args[0]) not in entities:
            warnings.append(f"query references non-engine entity: {arg_value(args[0])}")
        return errors, warnings
    if predicate == "count":
        # count(subject, relation)? — engine-verified aggregate (see evaluate_queries).
        count_error = count_query_error(line)
        if count_error:
            errors.append(count_error)
            return errors, warnings
        # A well-formed count falls through to the shared warning loop below, so
        # a subject or relation the engine does not carry gets the same
        # "non-engine entity or relation" warning relation/path queries get. It
        # used to return here, which left the report's most misreadable answer —
        # `0 (distinct objects)`, indistinguishable from a verified zero — as the
        # only signal that the query named something the KB has never heard of.
    elif predicate in {"relation", "path"}:
        relation_error = shape_error(predicate, line)
        if relation_error:
            errors.append(relation_error)
            return errors, warnings
    for constant in quoted_constants(line):
        if constant and constant not in entities and constant not in {"S", "R", "O", "X", "Q"}:
            warnings.append(f"query references non-engine entity or relation: {constant}")
    return errors, warnings


def policy_row_matches(args: list[str], row: tuple[str, ...] | list[str]) -> bool:
    """True when *row* satisfies every quoted constant *args* pins, by position.

    A quoted constant is a FILTER, at whatever position it appears — not merely a
    binding the display omits. Filtering only args[0] would still let
    ``pred(E, "stale")?`` report the other reasons' rows, and would do so while
    the first-argument form answers correctly, which is worse than filtering
    nothing: the reader loses the one signal that the second line is untrustworthy.

    A row shorter than the pinned position cannot satisfy the constant, so the
    0-arity row an engine may emit is dropped from a constant-pinned query (it
    still shows up for an all-variable query, as before).

    Comparison is RAW (``arg_value`` only), deliberately not
    ``common.canonical_value``: it mirrors ask_router's policy branch exactly so
    the report and ``ask`` cannot diverge, which is the property
    tests/unit/test_policy_query_filter.py pins. This means the policy path does
    NOT go through the "#213 single query-value comparison chokepoint"
    (common.py `_canonical_value`), contrary to what that docstring claims for
    every query-match path. Measured consequence: an NFD-stored entity queried
    with an NFC-typed constant now yields `0 rows`, which reads as a verified
    negative. Folding both sides belongs in one place for BOTH paths and is out
    of scope here; see #213.

    The matching rule is kept identical to ask_router's `policy_row_matches`
    (same body, module-specific docstring). The natural home is common.py
    alongside the other query-parsing helpers, but hoisting it there is a wider
    change than this fix needs; the report/router parity test fails if the two
    copies ever drift. Two of its cases carry that load — the 0-arity row and the
    NFD-stored/NFC-queried entity. Every other case is a 2-column ASCII row,
    which a copy that lost the short-row guard, or that folded to NFC on its own,
    still gets right; do not delete those two.
    """
    for index, arg in enumerate(args):
        if not is_quoted_string(arg):
            continue
        if index >= len(row) or arg_value(arg) != row[index]:
            return False
    return True


def policy_result_line(predicate: str, line: str, inferred: dict[str, set[tuple[str, ...]]]) -> str | None:
    """Render one policy query's result, or None when the query is malformed.

    The test is `policy_query_error`, the SAME verdict validate_query puts in the
    Errors section, so a line the report is rejecting never also receives an
    answer here. Four shapes reach this function malformed, and each used to be
    answered:

    - unparseable (no trailing '?'): `query_args` returns [], no constant is
      pinned, so the filter passes everything -> the whole extent;
    - `pred()?` -> one empty arg, likewise unfiltered -> the whole extent;
    - `pred("Alice")?` / `pred("Alice", R, "zzz")?` -> wrong arity, filtered by
      whatever constants happen to line up -> a plausible but meaningless count;
    - `pred(Alice, stale)?` -> right arity, but neither bare token is a variable
      or a quoted string, so `policy_row_matches` pins nothing and every row is
      rendered against them: the whole extent again, this time with `Alice=Bob`
      bindings that name an entity the row is not about (#328).

    "No usable args" is not "no constants to honour" — it means the query was
    never understood, so answering it invents an answer for a line the report is
    simultaneously calling an error. Emitting nothing leaves the Errors section
    to speak. ask_router.evaluate raises NotImplementedError on the same shapes,
    so neither path answers a malformed policy query.

    The query is echoed ONLY when a quoted constant is pinned. Such a line and
    the "Policy evaluation:" extent line ("<pred>: N rows", the count over ALL
    entities) sit a few lines apart and now legitimately disagree — 3 rows there,
    0 rows here — so naming the query that produced the 0 is what makes the pair
    readable as scope rather than contradiction, and it tells two queries on the
    same predicate apart. A variable-only query cannot produce that mismatch (it
    reports the extent, which is what the extent line says), so it keeps its
    original text byte for byte — the query-shape whose output this fix promised
    not to change. The extent line itself is left untouched: it is pinned by
    tests/golden/logic_report.txt, and its section header already says it is the
    policy evaluation rather than the answer to any one query.
    """
    if policy_query_error(line) is not None:
        return None
    args = query_args(line)
    rows = [row for row in sorted(inferred[predicate]) if policy_row_matches(args, row)]
    values: list[str] = []
    for row in rows:
        bindings = []
        for arg, value in zip(args, row, strict=False):
            # With the shape guard above, an arg is a variable or a quoted
            # string, so is_quoted_string is exactly "not a variable" — the
            # predicate policy_row_matches already uses, said the same way.
            if not is_quoted_string(arg):
                bindings.append(f"{arg}={value}")
        values.append(", ".join(bindings) if bindings else ", ".join(row))
    suffix = "; " + "; ".join(values) if values else ""
    echo = f" (query: {line})" if any(is_quoted_string(arg) for arg in args) else ""
    return f"{predicate} results{echo}: {len(rows)} rows{suffix}"


def evaluate_queries(facts: list[dict[str, str]], inferred: dict[str, set[tuple[str, ...]]], policy_query_predicates: set[str]) -> list[str]:
    results: list[str] = []
    for line in query_lines():
        predicate = line.split("(", 1)[0]
        if predicate in policy_query_predicates:
            result_line = policy_result_line(predicate, line, inferred)
            if result_line is not None:
                results.append(result_line)
        elif predicate == "path":
            # Same verdict validate_query put in the Errors section, so a line
            # reported as malformed is never also answered here (#328).
            if shape_error("path", line) is not None:
                continue
            constants = quoted_constants(line)
            if len(constants) >= 2:
                is_reachable = (constants[0], constants[1]) in inferred["path"]
                trace = dependency_path(facts, constants[0], constants[1]) if is_reachable else []
                value = " -> ".join(trace) if trace else "(not found)"
                results.append(f"path {constants[0]} -> {constants[1]}: {value}")
        elif predicate == "relation":
            if shape_error("relation", line) is not None:
                continue
            rows = relation_results(line, facts)
            args = query_args(line)
            result_values: list[str] = []
            for subject, relation, object_ in rows:
                bindings = []
                for arg, value in zip(args, [subject, relation, object_], strict=True):
                    if not is_quoted_string(arg):
                        bindings.append(f"{arg}={value}")
                result_values.append(", ".join(bindings) if bindings else f"{subject}, {relation}, {object_}")
            suffix = "; " + "; ".join(result_values) if result_values else ""
            results.append(f"relation results: {len(rows)} rows{suffix}")
        elif predicate == "count":
            # count(subject, relation)? -> number of DISTINCT objects for that
            # (subject, relation) over engine facts (0 is a verified answer).
            # NOT the same number as ask_router.evaluate's count branch: #227 gave
            # the router's count surface-variant expansion (a quoted canonical
            # relation also counts objects stored under its declared variants) and
            # this branch never got it, so on a KB with relation aliases the two
            # disagree — the gate passes the query, the router answers 2 and the
            # report answers 0. Which side is right is #227's question, not this
            # guard's; what is fixed here is that both refuse the SAME malformed
            # lines.
            if count_query_error(line) is not None:
                continue
            subj_q, rel_q = query_args(line)
            subj, rel = arg_value(subj_q), arg_value(rel_q)
            objects = {
                f["object"]
                for f in facts
                if (not is_quoted_string(subj_q) or f["subject"] == subj)
                and (not is_quoted_string(rel_q) or f["relation"] == rel)
            }
            results.append(f"count results: {len(objects)} (distinct objects)")
        elif predicate == "review_required":
            constants = quoted_constants(line)
            question = constants[0] if constants else "(missing question)"
            results.append(f"review_required: {question}")
    return results


def main() -> None:
    ensure_dirs()
    facts = load_accepted_facts()
    candidates = load_facts()
    inferred = run_wirelog()
    policy_program = load_logic_policy()
    policy_query_predicates = policy_predicates(policy_program)
    # value_set (entities + literal values) so a query naming a literal object of
    # an attribute relation is not falsely warned as a non-engine entity.
    entities = value_set(facts)
    relations = allowed_relations(facts)
    errors: list[str] = []
    warnings: list[str] = []
    policy_findings: list[str] = []

    for row in candidates:
        if not row["subject"] or not row["relation"] or not row["object"]:
            errors.append(f"incomplete fact row: {row}")
        if row["status"] not in KNOWN_STATUSES:
            warnings.append(f"unknown status treated as non-engine input: {row['status']}")

    for predicate in sorted(policy_query_predicates):
        for target, reason in sorted(inferred[predicate]):
            policy_findings.append(f"{predicate}: {target} ({reason})")

    for line in query_lines():
        query_errors, query_warnings = validate_query(line, entities, policy_query_predicates)
        errors.extend(query_errors)
        warnings.extend([item for item in query_warnings if item.rsplit(": ", 1)[-1] not in relations])

    report = [
        "Logic Check Report",
        "==================",
        "engine: wirelog / pyrewire",
        "input: facts/accepted.dl",
        f"policy: {LOGIC_POLICY_DL.relative_to(LOGIC_POLICY_DL.parents[1])}",
        f"engine facts: {len(facts)}",
        f"review facts outside engine input: {len(review_facts(candidates))}",
        f"policy findings: {len(policy_findings)}",
        f"errors: {len(errors)}",
        f"warnings: {len(warnings)}",
        "",
    ]
    if policy_findings:
        report.extend(["Policy Findings:", *[f"- {item}" for item in policy_findings], ""])
    if errors:
        report.extend(["Errors:", *[f"- {item}" for item in errors], ""])
    if warnings:
        report.extend(["Warnings:", *[f"- {item}" for item in warnings], ""])
    report.append("Policy evaluation:")
    policy_items = [
        f"{predicate}: {len(inferred[predicate])} rows"
        for predicate in sorted(policy_query_predicates)
    ]
    report.extend([f"- {item}" for item in policy_items] or ["- no generated policy predicates"])
    report.append("")
    report.append("Query evaluation:")
    report.extend([f"- {item}" for item in evaluate_queries(facts, inferred, policy_query_predicates)] or ["- no facts/query.dl found"])

    text = "\n".join(report) + "\n"
    out = FACTS_DIR / "logic_report.txt"
    out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    from common import run_cli

    # main() takes no argv; parse here only so `--wiki` is a documented option
    # with --help, and a mistyped flag is rejected instead of silently ignored.
    _parser = argparse.ArgumentParser(description="Run deterministic logic checks over facts and query drafts.")
    _parser.add_argument("--wiki", default=os.environ["FACTLOG_ROOT"], help="KB root")
    _parser.parse_args()
    raise SystemExit(run_cli(main))
