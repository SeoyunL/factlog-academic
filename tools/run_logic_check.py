#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run deterministic logic checks over facts and query drafts."""

from __future__ import annotations

from collections.abc import Callable

from common import (
    attribute_relation_forms,
    FACTS_DIR,
    KNOWN_STATUSES,
    QueryVocabulary,
    is_valid_arg,
    is_variable,
    relation_aliases,
    relation_row_matches,
    policy_row_matches,
    dependency_path,
    is_quoted_string,
    path_query_rows,
    typed_policy_warnings,
    typed_projection_warnings,
    QUERY_PREDICATES,
    dedup_engine_atoms,
    engine_facts,
    value_hierarchy,
    value_hierarchy_warnings,
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


def vocabulary_checks(
    kind: str,
    args: list[str],
    vocab: QueryVocabulary,
) -> list[tuple[str, Callable[[str], bool]]]:
    """The (argument, accepts) pairs the GATE vocabulary-checks for this query kind,
    in the gate's own order.

    The report used to judge every position against ONE pooled set of constants,
    which admitted more than the gate does at any single position: an attribute
    literal in a policy pin, or a hierarchy ancestor declared under a DIFFERENT
    relation, passed here while `classify_query` answered `entity_not_accepted`.
    The report then rendered the empty extent as a verified negative ("0 rows") for
    a question the gate refuses to answer — the false negative of #362, the #284
    class. Positions and predicates are named together here, once, so the report and
    the gate cannot pick different sets for the same argument.
    """
    if kind == "relation":
        # The object licence is scoped to the queried relation, and a VARIABLE
        # relation widens to every relation — the gate's own rule, argument for
        # argument (common.classify_query's relation branch).
        relation = None if is_variable(args[1]) else arg_value(args[1])
        return [
            (args[0], vocab.accepts_subject),
            (args[1], vocab.accepts_relation),
            (args[2], lambda value: vocab.accepts_object(value, relation)),
        ]
    if kind == "count":
        return [(args[0], vocab.accepts_subject), (args[1], vocab.accepts_relation)]
    if kind == "path":
        # Both nodes take the SUBJECT predicate: a path argument is a true entity,
        # which is the predicate the gate's path branch applies to each of them
        # (common.classify_query, #299). Judged against the old pooled set instead,
        # a relation NAME in a node position (`path("founded_by", X)?`) passed
        # silently -- no warning at all -- and the report rendered "0 rows" for a
        # query the gate refuses entity_not_accepted (#366).
        return [(args[0], vocab.accepts_subject), (args[1], vocab.accepts_subject)]
    if kind == "policy":
        # Only the pinned entity: a variable there ranges over the extent.
        return [(args[0], vocab.accepts_policy_entity)]
    raise ValueError(f"unknown query kind: {kind}")


def unaccepted_constants(checks: list[tuple[str, Callable[[str], bool]]]) -> list[str]:
    """The quoted constants among `checks` that their own position rejects."""
    return [
        arg_value(arg)
        for arg, accepts in checks
        if is_quoted_string(arg) and not accepts(arg_value(arg))
    ]


def validate_query(
    line: str, vocab: QueryVocabulary, policy_query_predicates: set[str]
) -> tuple[list[str], list[str]]:
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
        # The shape guard the gate (classify_query's policy branch) applies. Arity
        # alone let a bare/single-quoted token like `'Alice'` through -- a wildcard to
        # the matcher -- so the report passed a line the gate calls malformed, and the
        # two verdicts on one line diverged (#321; #319 was the same omission in the
        # count branch). Run before the entity warning below so a bare token never
        # reaches the quoted-constant check.
        if not all(is_valid_arg(a) for a in args):
            errors.append(f"policy query arguments must be variables or quoted strings: {line}")
            return errors, warnings
        # `is_quoted_string`, not an inline `startswith('"') and endswith('"')`: the
        # inline form called `"\q"` (and a bare `"`) a quoted constant and handed it
        # to `arg_value`, which `json.loads`ed it and died with a JSONDecodeError --
        # a hard crash of the whole report over one draft line (#342). The gate uses
        # this same predicate (common.policy_row_matches, #320), so the report's
        # last inline copy of the quote test now agrees with it and calls arg_value
        # only after the guard passes. Membership is compared through canonical_value
        # inside the vocabulary, the same fold the gate applies, so an NFD-typed query
        # of an NFC-stored engine entity is no longer falsely warned as a "non-engine
        # entity" -- the two axes of one function agree (#341).
        for constant in unaccepted_constants(vocabulary_checks("policy", args, vocab)):
            warnings.append(f"query references non-engine entity: {constant}")
        return errors, warnings
    if predicate == "count":
        # count(subject, relation)? — engine-verified aggregate (see evaluate_queries).
        args = query_args(line)
        if len(args) != 2:
            errors.append(f"count query must have subject and relation arguments: {line}")
            return errors, warnings
        # The guard `relation` and `path` already have. A bare token is a wildcard to the
        # matcher, so `count("A", 'rel')?` used to pass this validator and render
        # "count results: 0 (distinct objects)" -- and zero is documented as a VERIFIED
        # answer -- for a line the ask gate rejects as malformed (#319).
        if not all(is_valid_arg(a) for a in args):
            errors.append(f"count arguments must be variables or quoted strings: {line}")
            return errors, warnings
        # A well-formed count IS vocabulary-checked, as `relation` and `path` are.
        # Warning nothing here left `count("Nobody", "born_in")?` silent -- no error,
        # no warning -- and the report then rendered "count results: 0 (distinct
        # objects)", a zero this file documents as a verified answer (#319). It is
        # checked per POSITION rather than by the generic loop below, and returns, so
        # subject and relation name each meet the set the gate uses for them and no
        # constant is warned about twice (#362).
        for constant in unaccepted_constants(vocabulary_checks("count", args, vocab)):
            warnings.append(f"query references non-engine entity or relation: {constant}")
        return errors, warnings
    if predicate == "relation":
        args = query_args(line)
        if len(args) != 3:
            errors.append(f"relation query must have subject, relation, and object arguments: {line}")
            return errors, warnings
        # A bare token is neither a variable nor a quoted constant. The matcher used
        # to treat it as a wildcard, so the report printed "0 rows" — a verified
        # negative — for a query the gate calls malformed (#213). Say it is broken.
        if not all(is_valid_arg(a) for a in args):
            errors.append(f"relation arguments must be variables or quoted strings: {line}")
            return errors, warnings
        # Position by position (subject / relation name / relation-scoped object),
        # then return: the generic loop below would judge the same three constants
        # against one pooled vocabulary -- weaker than the gate at every one of them
        # (#362) -- and warn a second time about a constant already reported here.
        for constant in unaccepted_constants(vocabulary_checks("relation", args, vocab)):
            warnings.append(f"query references non-engine entity or relation: {constant}")
        return errors, warnings
    if predicate == "path":
        # The same guard `relation` has, and for the same reason: without it the report
        # answered a malformed path query with "0 rows" -- a VERIFIED NEGATIVE -- while
        # the ask gate rejected it as bad_arity/malformed (#213, #220).
        args = query_args(line)
        if len(args) != 2:
            errors.append(f"path query must have start and target arguments: {line}")
            return errors, warnings
        if not all(is_valid_arg(a) for a in args):
            errors.append(f"path arguments must be variables or quoted strings: {line}")
            return errors, warnings
        # Per position (both nodes against entity_set), then return -- the last
        # predicate to leave the position-agnostic union behind. That union pooled
        # the relation names and the declared hierarchy ancestors in with the
        # entities, so it admitted at a path node what no path node accepts: it
        # warned about nothing for `path("founded_by", X)?` while the report printed
        # "0 rows", a VERIFIED NEGATIVE for a query the gate rejects (#366).
        for constant in unaccepted_constants(vocabulary_checks("path", args, vocab)):
            warnings.append(f"query references non-engine entity or relation: {constant}")
        return errors, warnings
    # Not reached: every QUERY_PREDICATES member (relation / path / count /
    # review_required) and every policy predicate returns above, and anything else
    # returned at the unknown-predicate guard. What used to be here was a fallback
    # vocabulary check over the POOLED constants, and that fallback is exactly how a
    # path node came to be judged by a set no path node accepts (#366) -- so the tail
    # of this function warns about nothing rather than warning by the wrong set.
    return errors, warnings


def policy_result_line(
    predicate: str,
    line: str,
    inferred: dict[str, set[tuple[str, ...]]],
    vocab: QueryVocabulary,
) -> str:
    args = query_args(line)
    # Filter BEFORE counting: `len(rows)` must count what the query asked for, not
    # the whole extent. The same predicate the router filters with (#320) -- a pinned
    # entity constrains the report's rows exactly as it constrains ask's.
    rows = [row for row in sorted(inferred[predicate]) if policy_row_matches(args, row)]
    if not rows:
        # An empty policy extent over an UNACCEPTED pinned entity is not a verified
        # negative -- the gate rejects the same query entity_not_accepted, the policy
        # analogue of #347 (#351). validate_query's policy branch WARNS on this entity
        # through the SAME predicate (vocabulary_checks("policy", ...)), so mark the
        # result unverified and point at that warning instead of rendering a verified
        # "0 rows" -- the pointer is exact because both read one predicate. Only the
        # pinned entity (args[0]) is vocabulary-checked: that POSITION is the one
        # validate_query warns on and the one the gate's policy branch judges, and a
        # variable pin -- which ranges over the extent -- is never flagged. Warning
        # severity and exit 0 are unchanged: a needs_review entity is a normal KB
        # state; this only stops the line asserting a checked negative. A real finding
        # (non-empty extent) never reaches here.
        #
        # The SET is now the gate's too: `accepts_policy_entity` is entity_set, which
        # deliberately excludes the object literals of attribute relations. Judged
        # against the old pooled constants (which carried value_set), a query pinning
        # an attribute literal -- needs_review("2020", R)? where 2020 is a
        # published_year object -- passed silently while the gate answered
        # entity_not_accepted (#362, closed).
        unaccepted = unverified_vocabulary(vocabulary_checks("policy", args, vocab))
        if unaccepted is not None:
            return (
                f"{predicate} results: unverified — '{unaccepted}' is not "
                "accepted vocabulary (see Warnings above)"
            )
    values: list[str] = []
    for row in rows:
        bindings = []
        for arg, value in zip(args, row, strict=False):
            # A pinned arg is rendered as its value alone -- `"Alice"=Alice` reads
            # badly -- but it IS rendered: skipping it dropped the entity column and
            # left the reader a list of reasons with nothing to attribute them to.
            bindings.append(value if is_quoted_string(arg) else f"{arg}={value}")
        values.append(", ".join(bindings) if bindings else ", ".join(row))
    suffix = "; " + "; ".join(values) if values else ""
    return f"{predicate} results: {len(rows)} rows{suffix}"


def unverified_vocabulary(checks: list[tuple[str, Callable[[str], bool]]]) -> str | None:
    """The first quoted query constant its own POSITION does not accept, or None if
    every one is accepted (or is a variable).

    The gate (``classify_query``) rejects a relation/count query whose SUBJECT or
    RELATION-NAME is outside the accepted vocabulary with
    ``entity_not_accepted``/``relation_not_accepted``, so it never renders a result.
    The report used to render such a query's empty extent as ``0 rows`` -- a VERIFIED
    NEGATIVE -- while warning, on the same page, that the term is "not an engine
    entity or relation". Zero rows over a vocabulary the KB never accepted is not a
    verified "no such fact"; it is an UNVERIFIED question. This is the vocabulary
    axis of the report/gate divergence #213 set out to close (the shape axis is the
    malformed guards above); the report keeps the WARNING severity (a needs_review
    vocabulary reference is a normal KB state, exit 0) but stops calling the empty
    result a verified negative (#347).

    Callers pass ``vocabulary_checks(...)``: the same positions, in the same order,
    with the same accepting predicate the gate applies to each -- relation passes
    subject, relation-name and relation-scoped object (#350), count passes subject and
    relation-name, policy passes the pinned entity. validate_query warns off that one
    list too, so a constant that draws the warning is the constant that marks the
    result unverified and the "(see Warnings above)" pointer cannot dangle.

    The object position admits values declared as ancestors UNDER THE QUERIED
    RELATION, so a broad-value object matching a narrower row (코호트연구 ⊂ 관찰연구)
    is accepted vocabulary and is not flagged -- while an ancestor declared only under
    a different relation is flagged, as the gate flags it (#362; the pooled set used
    to let it through and render "0 rows"). A query whose constants are all accepted
    but whose triple is simply absent (sample-kb q4) has no unaccepted constant here,
    so it keeps rendering the honest ``0 rows`` -- that is the discriminator between
    the two.
    """
    unaccepted = unaccepted_constants(checks)
    return unaccepted[0] if unaccepted else None


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
    # The same per-position vocabulary validate_query warns against, and the same one
    # the gate judges with: a query naming a term its position does not accept is
    # rendered "unverified", not a verified "0 rows" (#347, #362). Built once for
    # every query line.
    vocab = QueryVocabulary.from_facts(facts, hierarchy, aliases)
    for line in query_lines():
        predicate = line.split("(", 1)[0]
        if predicate in policy_query_predicates:
            results.append(policy_result_line(predicate, line, inferred, vocab))
        elif predicate == "path":
            # Constants AND variables. The old branch only handled two quoted
            # constants, so `path("A", X)?` appended nothing, the result list came
            # back empty, and main's fallback claimed `no facts/query.dl found` about
            # a file that was right there -- while `ask` answered the same question
            # (#220). Shared with the ask router so the two cannot diverge (#213).
            args = query_args(line)
            # A malformed query is not a verified negative. Without this guard a
            # 1-arg or bare-token path query flowed into path_query_rows and the
            # report answered it with "path results: 0 rows" -- a VERIFIED NEGATIVE --
            # for a query validate_query (and the ask gate) reject as malformed
            # (#284). Same criterion as validate_query's path branch (L165, L168);
            # run before path_query_rows so malformed args never reach it.
            if len(args) != 2 or not all(is_valid_arg(a) for a in args):
                results.append("path query malformed — see Errors above")
                continue
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
                    # Inside `if not rows:`, never before path_query_rows: a pair the
                    # ENGINE proved reachable stays reachable whatever the vocabulary
                    # says, and denying it on vocabulary grounds would put python back
                    # in charge of reachability (#303). An empty extent over an
                    # unaccepted node, though, is not a verified negative -- the gate
                    # answers the same query entity_not_accepted -- so it is
                    # unverified, not "(not found)" (#366, the path axis of #347).
                    # Two accepted entities with no path keep "(not found)": that is
                    # the negative this branch exists to render.
                    unaccepted = unverified_vocabulary(vocabulary_checks("path", args, vocab))
                    if unaccepted is not None:
                        value = (
                            f"unverified — '{unaccepted}' is not accepted "
                            "vocabulary (see Warnings above)"
                        )
                    else:
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
                if not rows:
                    # The variable form of the same judgement: no pair, and a pinned
                    # node its position rejects, is an unverified question rather
                    # than a verified "0 rows" (#366).
                    unaccepted = unverified_vocabulary(vocabulary_checks("path", args, vocab))
                    if unaccepted is not None:
                        results.append(
                            f"path results: unverified — '{unaccepted}' is not "
                            "accepted vocabulary (see Warnings above)"
                        )
                        continue
                routes = "; ".join(f"{start} -> {target}" for start, target in rows)
                suffix = f"; {routes}" if routes else ""
                results.append(f"path results: {len(rows)} rows{suffix}")

        elif predicate == "relation":
            # Guard before relation_results, matching validate_query's relation branch
            # (L151, L157). The matcher treats a bare token as a wildcard, so a 2-arg or
            # bare-token query used to print "relation results: 0 rows" -- a VERIFIED
            # NEGATIVE -- for a query the gate rejects as malformed (#284).
            args = query_args(line)
            if len(args) != 3 or not all(is_valid_arg(a) for a in args):
                results.append("relation query malformed — see Errors above")
                continue
            rows = relation_results(line, facts, hierarchy)
            if not rows:
                # An empty result over UNACCEPTED vocabulary is not a verified
                # negative -- the gate rejects the same query outright (#347). Say
                # unverified, not "0 rows". A fully-accepted vocabulary with an absent
                # triple (q4) has no unaccepted constant and falls through to "0 rows".
                # All THREE positions, object included: the gate rejects a relation
                # query whose object is outside the accepted vocabulary with
                # entity_not_accepted too, so an empty result there is unverified, not a
                # verified zero (#350, the object axis #347 deferred). The object
                # position admits declared hierarchy ancestors, so a broad-value object
                # that matches a narrower row (코호트연구 ⊂ 관찰연구) is accepted
                # vocabulary and is not flagged — no false positive.
                #
                # Each position is judged by the gate's own set (#362). The pooled set
                # this used to read admitted ancestors from EVERY relation, so an object
                # declared under a DIFFERENT relation — relation("x", "y", "anyone")?
                # where `anyone` is declared only under `founded_by` — drew no flag and
                # rendered "0 rows" while the gate answered entity_not_accepted. The
                # licence is now scoped to the queried relation here as it is there.
                unaccepted = unverified_vocabulary(vocabulary_checks("relation", args, vocab))
                if unaccepted is not None:
                    results.append(
                        f"relation results: unverified — '{unaccepted}' is not "
                        "accepted vocabulary (see Warnings above)"
                    )
                    continue
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
            # Guard before relation_row_matches, matching the path (L218) and relation
            # (L255) branches. The old `if len(args) == 2:` appended nothing on bad
            # arity, and let a bare token through to the matcher -- which reads it as a
            # wildcard -- so a malformed count rendered a VERIFIED zero (#319).
            if len(args) != 2 or not all(is_valid_arg(a) for a in args):
                results.append("count query malformed — see Errors above")
                continue
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
            if not objects:
                # Same vocabulary axis as the relation branch (#347): a count over an
                # unaccepted subject/relation is unverified, not a verified zero -- the
                # gate rejects it (relation_not_accepted/entity_not_accepted). An
                # accepted subject/relation with genuinely no objects keeps "0".
                unaccepted = unverified_vocabulary(vocabulary_checks("count", args, vocab))
                if unaccepted is not None:
                    results.append(
                        f"count results: unverified — '{unaccepted}' is not "
                        "accepted vocabulary (see Warnings above)"
                    )
                    continue
            results.append(f"count results: {len(objects)} (distinct objects)")
        elif predicate == "review_required":
            constants = quoted_constants(line)
            question = constants[0] if constants else "(missing question)"
            results.append(f"review_required: {question}")
        elif predicate not in QUERY_PREDICATES and predicate not in policy_query_predicates:
            # A predicate the gate does not recognise. The dispatch used to select a
            # branch by line.startswith(...), so `relationship(...)?` entered the
            # `relation` branch (and, post-#284, drew "relation query malformed") for a
            # line validate_query calls `query unknown predicate` — a section pointing
            # at the wrong diagnosis (#294). Match validate_query's predicate test
            # (L126) exactly and defer to the Errors section it already writes.
            # Left as a conditional elif, not a bare else: `conflict` is a QUERY
            # predicate with no evaluation branch (silent by design), and an else would
            # tag it "see Errors above" for an error that is not there.
            results.append("unknown query predicate — see Errors above")
    return results


def engine_relation_gap(
    facts: list[dict[str, str]],
    inferred: dict[str, set[tuple[str, ...]]],
) -> str | None:
    """An error string when disk has facts but the ENGINE holds no relation atoms.

    The report's ``engine facts`` line counts the facts on DISK (load_accepted_facts),
    so it cannot see the engine's own input silently emptying underneath it -- the exact
    blind spot behind the vacuous-pass in #305 (report said ``engine facts: 7`` while the
    engine evaluated over nothing). ``inferred["relation_alive"]`` is the engine's
    POST-FIXPOINT relation extent: a witness IDB (``relation_alive(S) :- relation(S,R,O)``
    in WIRELOG_PROGRAM) that surfaces as a step() delta, so its emptiness means the engine
    genuinely holds no relation atoms -- whether they were dropped at parse time or at the
    fixpoint. When disk holds N>0 rows yet the witness is empty, some cause quietly
    swallowed the engine input. This is the LAST NET: #305's guard rejects the known
    causes (relation rule-head / .decl re-declaration) loudly at policy load, and this
    catches whatever unknown cause slips past by comparing the two independent readers.

    Deliberately conservative -- only the TOTAL-emptying (0) case fires, so a healthy KB
    never trips it (no count-mismatch false alarms). NB: ``relation_alive`` is keyed on
    the subject alone, so its cardinality is the count of DISTINCT subjects, not of facts
    -- used ONLY for the ``== 0`` test, never compared for equality with ``len(facts)``.
    A pure function: no I/O, never raises.
    """
    if not facts:
        return None
    if len(inferred.get("relation_alive", set())) == 0:
        return (
            f"engine input gap: {len(facts)} accepted fact(s) on disk but the engine "
            "holds 0 relation atoms — something silently emptied the engine input. "
            "Recompile with `factlog check`; if it persists, the accepted.dl the engine "
            "reads disagrees with the facts on disk."
        )
    return None


def engine_input_drift(
    candidates: list[dict[str, str]],
    facts: list[dict[str, str]],
) -> str | None:
    """An error string when facts/accepted.dl disagrees with the confirmed rows in
    candidates.csv on the engine atom count — the candidates.csv → accepted.dl edge that
    engine_relation_gap cannot see (#328).

    engine_relation_gap (#308) compares two readers of the SAME file (accepted.dl):
    load_accepted_facts and the engine's relation_alive witness. By construction they are
    built to agree, so that axis only catches the engine emptying underneath a consistent
    disk — never the edge that actually drifts. That edge is candidates.csv → accepted.dl:
    a truncated write (#329) or a hand-edited candidates.csv not recompiled leaves
    accepted.dl holding FEWER (or different) atoms than the confirmed rows it is compiled
    from, and nobody reads it. This is that reader. The expected count is
    ``dedup_engine_atoms(engine_facts(candidates))`` — the exact collapse compile_facts
    applies — and the actual is ``len(facts)`` (load_accepted_facts already dedups), so the
    comparison is dedup-aware on both sides and a freshly compiled KB never trips it.

    A hard ERROR, not a warning: warnings never fail run_logic_check (#283 exit gate keys
    on errors), and a truncated accepted.dl silently signs verified negatives — a confirmed
    fact answering ``0 rows`` as if it were a real, checked answer (#328). Pure: no I/O.
    """
    expected = len(dedup_engine_atoms(engine_facts(candidates)))
    actual = len(facts)
    if expected == actual:
        return None
    return (
        f"engine input drift: candidates.csv has {expected} engine-input fact(s) "
        f"(confirmed/accepted, deduped) but facts/accepted.dl holds {actual} — "
        "accepted.dl disagrees with the confirmed rows it is compiled from (a truncated "
        "write or a hand-edited candidates.csv not recompiled). Recompile with "
        "`factlog check`; until then a confirmed fact can be answered as a verified "
        "'0 rows' negative."
    )


def main() -> int | None:
    ensure_dirs()
    facts = load_accepted_facts()
    candidates = load_facts()
    inferred = run_wirelog()
    policy_program = load_logic_policy()
    policy_query_predicates = policy_predicates(policy_program)
    # One vocabulary, judged per position exactly as the ask gate judges it, so a
    # literal object of an attribute relation is not falsely warned as a non-engine
    # entity and a constant the gate rejects is not silently accepted here (#362).
    hierarchy = value_hierarchy()
    aliases = relation_aliases()
    vocab = QueryVocabulary.from_facts(facts, hierarchy, aliases)
    errors: list[str] = []
    warnings: list[str] = []
    policy_findings: list[str] = []

    for row in candidates:
        if not row["subject"] or not row["relation"] or not row["object"]:
            errors.append(f"incomplete fact row: {row}")
    # Last net for a silently-emptied engine input (#308): disk has facts but the
    # engine parsed 0 relation atoms. A hard error so the #283 exit gate stops the
    # pipeline instead of reporting a vacuous "no contradictions" over nothing.
    gap = engine_relation_gap(facts, inferred)
    if gap:
        errors.append(gap)
    # The candidates.csv → accepted.dl edge engine_relation_gap cannot see (#328): a
    # truncated (#329) or hand-edited accepted.dl holding fewer atoms than the confirmed
    # rows it is compiled from. A hard error so the #283 exit gate stops the pipeline
    # instead of signing a verified '0 rows' over a silently shrunk engine input.
    drift = engine_input_drift(candidates, facts)
    if drift:
        errors.append(drift)
    warnings.extend(status_warnings(candidates))
    # A mistyped or cyclic declaration is a SILENT no-op: the author believes the
    # broader query now catches the narrower rows, and it does not. That is the
    # quiet omission this KB exists to surface, so say it (#211).
    warnings.extend(value_hierarchy_warnings(facts=facts))
    # A typed literal that does not parse is dropped from its comparison predicate.
    # That used to be announced on stderr only, so the report — the artifact the
    # gate makes you show verbatim — said warnings: 0 while a fact was quietly
    # missing from every typed query (#227).
    warnings.extend(typed_projection_warnings(facts, aliases=aliases))
    # Policy-parse warnings: a malformed/unknown-type line, or a typed relation not
    # declared attribute, drops facts from a comparison predicate but only hit stderr.
    warnings.extend(typed_policy_warnings())

    for predicate in sorted(policy_query_predicates):
        for target, reason in sorted(inferred[predicate]):
            policy_findings.append(f"{predicate}: {target} ({reason})")

    for line in query_lines():
        query_errors, query_warnings = validate_query(line, vocab, policy_query_predicates)
        errors.extend(query_errors)
        # No post-filter: the vocabulary already admits relation names (and their
        # aliases, and hierarchy ancestors declared for that position), so a warning
        # that survives validate_query is a genuinely unaccepted constant.
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
        # (#220). Say which — counting the same lines the validator treats as queries,
        # so a comment-only file reads as empty rather than as two failed queries.
        pending = query_lines()
        if not pending:
            report.append("- facts/query.dl is empty (no queries to evaluate)")
        else:
            # Point at Errors only when there IS one. The Errors section is rendered
            # solely when `errors` is non-empty, so "see Errors above" on an errors: 0
            # report is a dangling pointer -- exactly the self-contradiction #284/#220
            # removed elsewhere, and the one a lone `conflict(...)?` used to trigger
            # here before #306 gave it an unknown-predicate error (#306).
            pointer = " — see Errors above" if errors else ""
            report.append(
                f"- facts/query.dl has {len(pending)} line(s) but none produced a "
                f"result{pointer}"
            )

    text = "\n".join(report) + "\n"
    out = FACTS_DIR / "logic_report.txt"
    out.write_text(text, encoding="utf-8")
    print(text)
    # A logic-check error (arity/predicate/incomplete-row) is a hard failure the
    # freshness gate must see: the report is written and printed first so the
    # verbatim artifact still exists, then a non-zero exit stops the pipeline.
    # Warnings and policy findings are informational and never fail the check.
    if errors:
        return 1
    return None


if __name__ == "__main__":
    from common import run_cli

    raise SystemExit(run_cli(main))
