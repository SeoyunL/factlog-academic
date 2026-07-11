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
    query_args,
    quoted_constants,
)


def status_warnings(candidates: list[dict]) -> list[str]:
    """Warn only for statuses outside the vocabulary.

    `superseded` is a known status: `factlog reject`/`amend` sets it, and such
    rows are kept in candidates.csv for audit while staying out of engine input.
    Warning on them made the report noisier the more review work had been done
    (#208). A genuinely unrecognised status — a typo — must still warn.
    """
    return [
        f"unknown status treated as non-engine input: {row['status']}"
        for row in candidates
        if row["status"] not in KNOWN_STATUSES
    ]


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
    for constant in quoted_constants(line):
        if constant and constant not in entities and constant not in {"S", "R", "O", "X", "Q"}:
            warnings.append(f"query references non-engine entity or relation: {constant}")
    return errors, warnings


def policy_result_line(predicate: str, line: str, inferred: dict[str, set[tuple[str, ...]]]) -> str:
    rows = sorted(inferred[predicate])
    args = query_args(line)
    values: list[str] = []
    for row in rows:
        bindings = []
        for arg, value in zip(args, row, strict=False):
            if not (arg.startswith('"') and arg.endswith('"')):
                bindings.append(f"{arg}={value}")
        values.append(", ".join(bindings) if bindings else ", ".join(row))
    suffix = "; " + "; ".join(values) if values else ""
    return f"{predicate} results: {len(rows)} rows{suffix}"


def evaluate_queries(facts: list[dict[str, str]], inferred: dict[str, set[tuple[str, ...]]], policy_query_predicates: set[str]) -> list[str]:
    results: list[str] = []
    for line in query_lines():
        predicate = line.split("(", 1)[0]
        if predicate in policy_query_predicates:
            results.append(policy_result_line(predicate, line, inferred))
        elif line.startswith("path"):
            constants = quoted_constants(line)
            if len(constants) >= 2:
                is_reachable = (constants[0], constants[1]) in inferred["path"]
                trace = dependency_path(facts, constants[0], constants[1]) if is_reachable else []
                value = " -> ".join(trace) if trace else "(not found)"
                results.append(f"path {constants[0]} -> {constants[1]}: {value}")
        elif line.startswith("relation"):
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
        elif line.startswith("count"):
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
        elif line.startswith("review_required"):
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
    warnings.extend(status_warnings(candidates))

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

    raise SystemExit(run_cli(main))
