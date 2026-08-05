#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run deterministic logic checks over facts and query drafts."""

from __future__ import annotations

from common import (
    FACTS_DIR,
    KNOWN_STATUSES,
    QUERY_PREDICATES,
    allowed_relations,
    dependency_path,
    entity_set,
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
            if arg.startswith('"') and arg.endswith('"') and arg_value(arg) != row[field]:
                matched = False
                break
        if matched:
            rows.append((row["subject"], row["relation"], row["object"]))
    return rows


def validate_query(
    line: str,
    entities: set[str],
    policy_query_predicates: set[str],
    path_nodes: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Validate one query line against the KB vocabulary.

    *entities* is value_set — entities AND literal values — because a relation
    query's object may legitimately be a literal. *path_nodes* is the narrower
    entity_set: a path node must be an entity, which is what classify_query
    enforces for `ask`. ``None`` means "do not distinguish the two" and keeps the
    pre-#329 behaviour for the callers that pass three arguments.
    """
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
        args = query_args(line)
        if len(args) != 2:
            errors.append(f"policy query must have entity and reason arguments: {line}")
            return errors, warnings
        if args[0].startswith('"') and args[0].endswith('"') and arg_value(args[0]) not in entities:
            warnings.append(f"query references non-engine entity: {arg_value(args[0])}")
        return errors, warnings
    if predicate == "count":
        # count(subject, relation)? — engine-verified aggregate (see evaluate_queries).
        if len(query_args(line)) != 2:
            errors.append(f"count query must have subject and relation arguments: {line}")
        return errors, warnings
    if predicate == "path" and path_nodes is not None:
        # A path node must be an ENTITY. The object of a declared attribute
        # relation is a literal value: it is in the KB (so the generic check
        # below stays silent) but cannot sit on a path. classify_query refuses
        # the same query outright — say why here too, rather than letting the
        # result line answer "(not found)", which reads as "the facts do not
        # connect them" (#329).
        for constant in quoted_constants(line):
            if constant in entities and constant not in path_nodes:
                warnings.append(f"query path argument is not an accepted entity: {constant}")
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

    The arity test is the SAME one validate_query applies to a policy query
    (entity + reason, i.e. exactly 2 args), so a line the report is rejecting in
    its Errors section never also receives an answer here. Three shapes reach
    this function malformed, and each used to be answered:

    - unparseable (no trailing '?'): `query_args` returns [], no constant is
      pinned, so the filter passes everything -> the whole extent;
    - `pred()?` -> one empty arg, likewise unfiltered -> the whole extent;
    - `pred("Alice")?` / `pred("Alice", R, "zzz")?` -> wrong arity, filtered by
      whatever constants happen to line up -> a plausible but meaningless count.

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
    args = query_args(line)
    if len(args) != 2:
        return None
    rows = [row for row in sorted(inferred[predicate]) if policy_row_matches(args, row)]
    values: list[str] = []
    for row in rows:
        bindings = []
        for arg, value in zip(args, row, strict=False):
            if not (arg.startswith('"') and arg.endswith('"')):
                bindings.append(f"{arg}={value}")
        values.append(", ".join(bindings) if bindings else ", ".join(row))
    suffix = "; " + "; ".join(values) if values else ""
    echo = f" (query: {line})" if any(is_quoted_string(arg) for arg in args) else ""
    return f"{predicate} results{echo}: {len(rows)} rows{suffix}"


def evaluate_queries(
    facts: list[dict[str, str]],
    inferred: dict[str, set[tuple[str, ...]]],
    policy_query_predicates: set[str],
    path_nodes: set[str] | None = None,
) -> list[str]:
    """Render one result line per query in facts/query.dl.

    *path_nodes* is entity_set — the values that may be path endpoints. ``None``
    means "do not distinguish", the pre-#329 behaviour kept for three-argument
    callers; ``main`` always passes it.
    """
    results: list[str] = []
    for line in query_lines():
        predicate = line.split("(", 1)[0]
        if predicate in policy_query_predicates:
            result_line = policy_result_line(predicate, line, inferred)
            if result_line is not None:
                results.append(result_line)
        elif predicate == "path":
            constants = quoted_constants(line)
            if len(constants) >= 2:
                # An endpoint that is a literal (object of a declared attribute
                # relation) is not a path node at all. Name the reason instead of
                # reporting "(not found)", which claims the facts were searched
                # and do not connect the two — and which `ask` does not claim,
                # because classify_query rejects the query as entity_not_accepted
                # (#329).
                not_nodes = (
                    [value for value in constants[:2] if value not in path_nodes]
                    if path_nodes is not None else []
                )
                if not_nodes:
                    results.append(
                        f"path {constants[0]} -> {constants[1]}: "
                        f"(not evaluated — not an accepted entity: {', '.join(not_nodes)})"
                    )
                    continue
                is_reachable = (constants[0], constants[1]) in inferred["path"]
                trace = dependency_path(facts, constants[0], constants[1]) if is_reachable else []
                value = " -> ".join(trace) if trace else "(not found)"
                results.append(f"path {constants[0]} -> {constants[1]}: {value}")
        elif predicate == "relation":
            rows = relation_results(line, facts)
            args = query_args(line)
            result_values: list[str] = []
            for subject, relation, object_ in rows:
                bindings = []
                for arg, value in zip(args, [subject, relation, object_], strict=True):
                    if not (arg.startswith('"') and arg.endswith('"')):
                        bindings.append(f"{arg}={value}")
                result_values.append(", ".join(bindings) if bindings else f"{subject}, {relation}, {object_}")
            suffix = "; " + "; ".join(result_values) if result_values else ""
            results.append(f"relation results: {len(rows)} rows{suffix}")
        elif predicate == "count":
            # count(subject, relation)? -> number of DISTINCT objects for that
            # (subject, relation) over engine facts (0 is a verified answer).
            # Same semantics as ask_router.evaluate's count branch.
            args = query_args(line)
            if len(args) == 2:
                subj_q, rel_q = args
                subj, rel = arg_value(subj_q), arg_value(rel_q)
                subj_const = subj_q.startswith('"') and subj_q.endswith('"')
                rel_const = rel_q.startswith('"') and rel_q.endswith('"')
                objects = {
                    f["object"]
                    for f in facts
                    if (not subj_const or f["subject"] == subj)
                    and (not rel_const or f["relation"] == rel)
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
    # entity_set is the narrower set a path endpoint must belong to — the same
    # test classify_query applies for `ask`, so the report and the router give
    # the same answer to the same path query (#329).
    path_nodes = entity_set(facts)
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
        query_errors, query_warnings = validate_query(line, entities, policy_query_predicates, path_nodes)
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
    report.extend([f"- {item}" for item in evaluate_queries(facts, inferred, policy_query_predicates, path_nodes)] or ["- no facts/query.dl found"])

    text = "\n".join(report) + "\n"
    out = FACTS_DIR / "logic_report.txt"
    out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    from common import run_cli

    raise SystemExit(run_cli(main))
