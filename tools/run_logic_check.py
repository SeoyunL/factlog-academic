#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run deterministic logic checks over facts and query drafts."""

from __future__ import annotations

from common import (
    attribute_relation_forms,
    FACTS_DIR,
    KNOWN_STATUSES,
    canonical_value,
    declared_ancestors,
    is_variable,
    relation_aliases,
    relation_row_matches,
    dependency_path,
    is_quoted_string,
    path_query_rows,
    typed_projection_warnings,
    QUERY_PREDICATES,
    allowed_relations,
    value_hierarchy,
    value_hierarchy_warnings,
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


def relation_results(
    line: str,
    facts: list[dict[str, str]],
    hierarchy: dict[str, dict[str, set[str]]] | None = None,
) -> list[tuple[str, str, str]]:
    """Rows of `accepted.dl` satisfying a `relation(...)` query.

    Delegates to `common.relation_row_matches` — the ONE matching predicate the
    report, the router and the gate all share. Three near-copies used to drift, and
    the report's copy compared raw strings: declaring a relation alias made facts
    vanish from the verification report while `/factlog ask` still found them
    (#213).
    """
    args = query_args(line)
    if len(args) != 3:
        return []
    if hierarchy is None:
        hierarchy = value_hierarchy()
    aliases = relation_aliases()
    return [
        (row["subject"], row["relation"], row["object"])
        for row in facts
        if relation_row_matches(args, row, aliases, hierarchy)
    ]


def known_constants(
    facts: list[dict[str, str]],
    hierarchy: dict[str, dict[str, set[str]]] | None = None,
    aliases: dict[str, str] | None = None,
) -> set[str]:
    """Every constant a query may legitimately name, canonicalised.

    Judging a query against the RAW accepted values made the report contradict
    itself: it returned rows for `relation(P, "연구유형", ...)` in an aliased KB
    (the rows store the surface variant) while warning, on the same page, that
    `연구유형` is "not an engine relation" (#213). The vocabulary a query may use
    is the vocabulary the matcher accepts — canonical names, their declared surface
    variants, declared hierarchy ancestors, and amount literals in either quoting.
    """
    aliases = aliases if aliases is not None else relation_aliases()
    known = {canonical_value(v) for v in value_set(facts)}
    known |= {canonical_value(r) for r in allowed_relations(facts)}
    known |= {canonical_value(raw) for raw in aliases}
    known |= {canonical_value(canonical) for canonical in aliases.values()}
    known |= declared_ancestors(hierarchy, None, canonical_value)
    return known


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
    if predicate == "relation":
        args = query_args(line)
        if len(args) != 3:
            errors.append(f"relation query must have subject, relation, and object arguments: {line}")
            return errors, warnings
        # A bare token is neither a variable nor a quoted constant. The matcher used
        # to treat it as a wildcard, so the report printed "0 rows" — a verified
        # negative — for a query the gate calls malformed (#213). Say it is broken.
        if not all(is_variable(a) or is_quoted_string(a) for a in args):
            errors.append(f"relation arguments must be variables or quoted strings: {line}")
            return errors, warnings
    if predicate == "path":
        # The same guard `relation` has, and for the same reason: without it the report
        # answered a malformed path query with "0 rows" -- a VERIFIED NEGATIVE -- while
        # the ask gate rejected it as bad_arity/malformed (#213, #220).
        args = query_args(line)
        if len(args) != 2:
            errors.append(f"path query must have start and target arguments: {line}")
            return errors, warnings
        if not all(is_variable(a) or is_quoted_string(a) for a in args):
            errors.append(f"path arguments must be variables or quoted strings: {line}")
            return errors, warnings
    for constant in quoted_constants(line):
        if constant and canonical_value(constant) not in entities and constant not in {"S", "R", "O", "X", "Q"}:
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


def evaluate_queries(
    facts: list[dict[str, str]],
    inferred: dict[str, set[tuple[str, ...]]],
    policy_query_predicates: set[str],
    hierarchy: dict[str, dict[str, set[str]]] | None = None,
) -> list[str]:
    results: list[str] = []
    if hierarchy is None:
        hierarchy = value_hierarchy()
    aliases = relation_aliases()
    for line in query_lines():
        predicate = line.split("(", 1)[0]
        if predicate in policy_query_predicates:
            results.append(policy_result_line(predicate, line, inferred))
        elif line.startswith("path"):
            # Constants AND variables. The old branch only handled two quoted
            # constants, so `path("A", X)?` appended nothing, the result list came
            # back empty, and main's fallback claimed `no facts/query.dl found` about
            # a file that was right there -- while `ask` answered the same question
            # (#220). Shared with the ask router so the two cannot diverge (#213).
            args = query_args(line)
            # The ENGINE decides what is reachable; python only renders the route. The
            # first cut let the python closure decide, so on a KB with an edge rule in
            # logic-policy.extra.dl the report said "(not found)" about a pair the
            # engine had proved -- a verification artifact contradicting the engine
            # while signed with its name.
            rows = path_query_rows(args, facts, inferred["path"])
            if all(is_quoted_string(a) for a in args) and len(args) == 2:
                start, target = arg_value(args[0]), arg_value(args[1])
                # Explicit attr_forms, matching first_dependency_path: the route must be
                # drawn over the same graph the engine used, not an ambient default.
                route = dependency_path(facts, start, target, attribute_relation_forms())
                if not rows:
                    value = "(not found)"
                elif route:
                    value = " -> ".join(route)
                else:
                    # Reachable per the engine, but no route through the accepted facts
                    # -- a rule in logic-policy.extra.dl put the edge there. Printing
                    # `start -> target` would draw a one-hop route that does not exist;
                    # printing "(not found)" would deny what the engine proved. Say what
                    # is true.
                    value = "reachable (engine); no route through the accepted facts"
                results.append(f"path {start} -> {target}: {value}")
            else:
                routes = "; ".join(f"{start} -> {target}" for start, target in rows)
                suffix = f"; {routes}" if routes else ""
                results.append(f"path results: {len(rows)} rows{suffix}")

        elif line.startswith("relation"):
            rows = relation_results(line, facts, hierarchy)
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
                # Same canonicalisation as the relation branch and as ask's count
                # (#213). Comparing raw strings here made the report answer "0" to
                # a question ask answered "2" — in an aliased KB, on the very same
                # facts. A count query is a relation query with a free object, so
                # it is matched by the shared predicate with a variable object.
                subj_q, rel_q = args
                objects = {
                    row["object"]
                    for row in facts
                    if relation_row_matches([subj_q, rel_q, "O"], row, aliases, hierarchy)
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
    hierarchy = value_hierarchy()
    aliases = relation_aliases()
    entities = known_constants(facts, hierarchy, aliases)
    errors: list[str] = []
    warnings: list[str] = []
    policy_findings: list[str] = []

    for row in candidates:
        if not row["subject"] or not row["relation"] or not row["object"]:
            errors.append(f"incomplete fact row: {row}")
    warnings.extend(status_warnings(candidates))
    # A mistyped or cyclic declaration is a SILENT no-op: the author believes the
    # broader query now catches the narrower rows, and it does not. That is the
    # quiet omission this KB exists to surface, so say it (#211).
    warnings.extend(value_hierarchy_warnings(facts=facts))
    # A typed literal that does not parse is dropped from its comparison predicate.
    # That used to be announced on stderr only, so the report — the artifact the
    # gate makes you show verbatim — said warnings: 0 while a fact was quietly
    # missing from every typed query (#227).
    warnings.extend(typed_projection_warnings(facts))

    for predicate in sorted(policy_query_predicates):
        for target, reason in sorted(inferred[predicate]):
            policy_findings.append(f"{predicate}: {target} ({reason})")

    for line in query_lines():
        query_errors, query_warnings = validate_query(line, entities, policy_query_predicates)
        errors.extend(query_errors)
        # No post-filter: known_constants() already admits relation names (and
        # their aliases, and declared hierarchy ancestors), so a warning that
        # survives validate_query is a genuinely unknown constant.
        warnings.extend(query_warnings)

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
    query_items = [
        f"- {item}"
        for item in evaluate_queries(facts, inferred, policy_query_predicates, value_hierarchy())
    ]
    if query_items:
        report.extend(query_items)
    elif not (FACTS_DIR / "query.dl").is_file():
        report.append("- no facts/query.dl found")
    else:
        # The file IS there. Saying "not found" sent users looking for a file they had
        # already written, when the truth was that no line in it produced a result
        # (#220). Say which.
        raw = (FACTS_DIR / "query.dl").read_text(encoding="utf-8")
        lines = [ln.strip() for ln in raw.splitlines()]
        pending = [ln for ln in lines if ln and not ln.startswith("//")]
        if not pending:
            report.append("- facts/query.dl is empty (no queries to evaluate)")
        else:
            report.append(
                f"- facts/query.dl has {len(pending)} line(s) but none produced a "
                f"result — see Errors above"
            )

    text = "\n".join(report) + "\n"
    out = FACTS_DIR / "logic_report.txt"
    out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    from common import run_cli

    raise SystemExit(run_cli(main))
