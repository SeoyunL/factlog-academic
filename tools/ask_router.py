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
import importlib
import json
import math
import os
import re
import sys
from pathlib import Path

# Ensure tools/ is importable when run directly, and resolve the KB root BEFORE
# importing common (whose module-level ROOT captures FACTLOG_ROOT at import).
_TOOLS_DIR = Path(__file__).parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


# Resolve the KB root and export it before importing common, which binds
# its module-level paths from FACTLOG_ROOT at import time.
import factlog_config  # noqa: E402

os.environ["FACTLOG_ROOT"] = factlog_config.resolve_root_from_argv("--target")

from common import (  # noqa: E402
    ACCEPTED_DL,
    CANDIDATES_CSV,
    LOGIC_POLICY_DL,
    QUERY_FACT_ABSENT,
    QUERY_OK,
    FactlogError,
    arg_value,
    canonical_value,
    canonical_variants_of,
    is_quoted_string,
    is_variable,
    query_args,
    classify_query,
    dependency_graph,
    dependency_path,
    entity_set,
    fact_signals,
    load_accepted_facts,
    load_facts,
    load_logic_policy,
    logic_policy_md_has_rules,
    object_matches,
    policy_predicates,
    relation_aliases,
    value_hierarchy,
    run_wirelog,
)
from factlog import literal_types  # noqa: E402


def _policy_program_optional() -> str:
    """Return the fully assembled policy text — the generated `logic-policy.dl`
    PLUS the optional hand-authored `logic-policy.extra.dl` — or '' if no usable
    policy can be assembled yet.

    `/factlog ask` is interactive and must work before `/factlog check` compiles
    `policy/logic-policy.dl`. Reading the *assembled* program via the SAME loader
    `/factlog check` uses — `load_logic_policy()` → `common._load_logic_policy_from`,
    which merges `logic-policy.extra.dl` onto the compiled base — is the single
    source of truth, so ask and check never drift on what the policy program IS.
    That loader already merges `logic-policy.extra.dl` even when the compiled
    `logic-policy.dl` is ABSENT (#190), so a hand-authored comparison predicate
    that lives ONLY in extra.dl (no compiled .dl, no rules in logic-policy.md) is
    now seen and evaluated here, matching check (#198 — closes the ask≠check gap
    where extra.dl was silently ignored when the .dl was absent, #152/#120).
    Both the classify/route path and the evaluate/render path read this, so one
    source of truth fixes both.

    NON-RAISING by contract (#193): `_load_logic_policy_from` fails loud in a few
    cases (`logic-policy.dl` absent WHILE `logic-policy.md` defines uncompiled
    rules; a `canonical/3` head in the policy text) — the right behavior for the
    `check` verification gate, but ask is exploratory and must never hard-fail.
    We reuse the whole loader and catch `FactlogError` here rather than forking
    just its extra.dl-merge tail (which would duplicate logic and invite drift):
    on a LOAD-STAGE failure this returns '' (no policy applied). The uncompiled-
    but-authored `logic-policy.md` case is still surfaced separately as a warning
    by `_policy_uncompiled` (not silently dropped), so #193's behavior is intact —
    an empty return here + that warning, exactly as before.

    Scope note: this guards only the LOAD stage. The ENGINE-EVALUATION stage
    (`evaluate` -> `common.run_wirelog`) re-loads the policy AND runs pyrewire, so
    a present-but-broken `logic-policy.extra.dl` (an unscaled `number` threshold,
    or a syntax error the loader does not parse) can still fail there — including
    with a NON-`FactlogError` pyrewire exception. That stage is guarded separately
    at the `run_wirelog()` call in `evaluate` (degrading to a `policy_unevaluable`
    signal the render/evaluate commands surface as POLICY_UNEVALUABLE_WARNING),
    because it is a distinct failure surface this loader helper never reaches.
    """
    try:
        return load_logic_policy()
    except FactlogError:
        return ""


# Greppable one-line hint shown when the author wrote policy rules but never
# compiled them. Mirrors the remediation `/factlog check` prints on the same
# condition (run the generator, or /factlog add), but as a warning — ask is
# exploratory, not a verification gate.
POLICY_UNCOMPILED_WARNING = (
    "WARNING: policy is uncompiled — policy/logic-policy.md defines rules but "
    "policy/logic-policy.dl is absent, so policy is being IGNORED in this answer. "
    "Run tools/generate_logic_policy.py (or /factlog add) to compile it."
)

# Greppable one-line hint shown when a hand-authored logic-policy.extra.dl is
# PRESENT but the engine cannot evaluate it (a type-violating threshold, broken
# .dl syntax, etc.). Distinct from POLICY_UNCOMPILED_WARNING (uncompiled
# logic-policy.md rules) — the file and the failure mode differ. ask is graceful
# (#193): rather than crash or fake a verified negative, it answers WITHOUT the
# broken policy and says so; `{reason}` carries the engine/loader message so the
# author can fix the file.
POLICY_UNEVALUABLE_WARNING = (
    "WARNING: policy is unevaluable — policy/logic-policy.extra.dl could not be "
    "evaluated by the engine, so this answer was produced WITHOUT policy. Fix "
    "policy/logic-policy.extra.dl. Reason: {reason}"
)


def _policy_uncompiled() -> bool:
    """True iff the author wrote policy rules but never compiled them:
    ``logic-policy.dl`` is absent while ``logic-policy.md`` defines >=1 compilable
    rule.

    Mirrors ``/factlog check``'s detection (``common._load_logic_policy_from``)
    using the SAME shared helper (``logic_policy_md_has_rules``, #190), so ask and
    check never disagree about what "has rules" means — a single source of truth.
    Unlike check, ask stays graceful: it surfaces a warning, not a hard failure,
    because ask must work before check compiles the policy. This closes the
    asymmetry (#193) where ask silently ignored an uncompiled policy that check
    caught. The benign no-policy case (empty/prose ``logic-policy.md``) yields
    False here exactly as it does for check, so ask's legitimate no-policy
    tolerance is unchanged — only "rules written but not compiled" warns.
    """
    if LOGIC_POLICY_DL.is_file():
        return False
    return logic_policy_md_has_rules(LOGIC_POLICY_DL.with_name("logic-policy.md"))


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
        # An uncompiled-but-authored policy is silently ignored by the engine
        # path (policy program is ''); flag it so callers surface a warning
        # instead of presenting a policy-free answer as fully policy-checked (#193).
        "policy_uncompiled": _policy_uncompiled(),
    }


def evaluate_relation(draft: str, facts: list[dict[str, str]]) -> list[list[str]]:
    """Evaluate a single ``relation(...)`` query against accepted facts.

    Quoted constants must match the corresponding field; variables bind freely.
    When the relation argument is a quoted canonical name (one whose
    surface_variants set is non-empty), a fact row matches if its relation
    field equals the canonical name OR is in that variant set — so a canonical
    query returns all surface-variant rows. Subject matching is unchanged.

    The object honours policy/value-hierarchy.md: a query for a broad value also
    returns rows filed under a declared narrower one (#211), so `/factlog ask`
    and the logic report cannot disagree about what a question means.
    Returns the matching [subject, relation, object] rows. Does not touch
    facts/query.dl.
    """
    args = query_args(draft)
    if len(args) != 3:
        return []
    s_arg, r_arg, o_arg = args
    hierarchy = value_hierarchy()
    _relation_alias_map = relation_aliases() if hierarchy else {}
    # Pre-compute surface variants when the relation arg is a quoted canonical.
    rel_variants: set[str] = set()
    if is_quoted_string(r_arg):
        rel_variants = canonical_variants_of(arg_value(r_arg), relation_aliases())
    rows: list[list[str]] = []
    for row in facts:
        s_val, r_val = row["subject"], row["relation"]
        if not (is_variable(s_arg) or canonical_value(arg_value(s_arg)) == canonical_value(s_val)):
            continue
        if not (is_variable(r_arg) or
                canonical_value(arg_value(r_arg)) == canonical_value(r_val) or
                r_val in rel_variants):
            continue
        # Declarations are written on CANONICAL relation names. When the query
        # pins one, use it. When the relation is a variable, canonicalise the
        # row's own (possibly surface-variant) name, so an aliased KB does not
        # silently lose subsumption for variable-relation queries.
        query_relation = (
            arg_value(r_arg)
            if is_quoted_string(r_arg)
            else _relation_alias_map.get(r_val, r_val)
        )
        if not (
            is_variable(o_arg)
            or object_matches(arg_value(o_arg), row, hierarchy, canonical_value, relation=query_relation)
        ):
            continue
        rows.append([row["subject"], row["relation"], row["object"]])
    return rows


def coverage_hint(
    draft: str,
    facts: list[dict[str, str]],
    max_relations: int = 6,
) -> str | None:
    """Informational coverage hint for a verified-negative relation query (#189).

    When ``relation("S", "R", O)?`` is a VERIFIED NEGATIVE (0 rows) yet the subject
    ``S`` is an accepted entity that carries fact(s) under OTHER relations, return a
    single informational line naming those relations — so a user can tell a
    *predicate mismatch* ("I asked the wrong relation") apart from an *honest
    absence* ("there really is no such fact"). Deterministic and in-memory: reads
    only the accepted facts already loaded by the caller; writes nothing; never
    changes the verdict, routing, storage, or provenance — it is an ADDED line.

    Returns None (no hint) in every case that could produce a false positive:
      - the query is NOT a VERIFIED NEGATIVE (``classify`` route != engine OR
        negative == False) — e.g. an accepted subject with an UNACCEPTED object
        routes to wiki, and a wiki/positive answer must never carry this hint.
        This is the SAME gate ``cmd_render`` applies (``decision["negative"]``),
        reused via ``classify`` so render and evaluate never drift on scope;
      - predicate is not ``relation`` (path/count/policy carry no predicate
        mismatch), or the query is not a 3-arg relation;
      - the subject or relation argument is a variable (no concrete predicate to
        point at — a predicate-mismatch hint would be meaningless);
      - the subject is NOT an accepted entity (an unknown subject is already
        wiki-routed; never fabricate a hint for it);
      - the subject DOES have fact(s) under the queried relation R (then the empty
        result is an OBJECT mismatch, not a predicate mismatch — no hint);
      - the subject has NO fact under any other relation either (a genuine
        verified negative — the honest-absence value we must preserve).

    Relation names are compared canonically (matching ``evaluate_relation``), and
    the queried relation's declared surface variants are treated as the same
    predicate, so an alias never masquerades as an "other" relation. The listed
    relations are sorted deterministically and capped at *max_relations*.
    """
    if _predicate_of(draft) != "relation":
        return None
    args = query_args(draft)
    if len(args) != 3:
        return None
    s_arg, r_arg, _o_arg = args
    # A predicate-mismatch hint only makes sense for a concrete subject AND a
    # concrete queried predicate; a variable in either position has nothing to
    # compare against.
    if not (is_quoted_string(s_arg) and is_quoted_string(r_arg)):
        return None
    # SCOPE GATE (single source of truth with cmd_render): the hint is defined only
    # for a VERIFIED NEGATIVE engine answer. Reuse classify — exactly what render
    # branches on via decision["negative"] — so an accepted subject with an
    # unaccepted object (route=wiki, negative=False) or a positive answer never
    # emits the hint, keeping the machine (evaluate) and human (render) outputs on
    # the same contract with no drift.
    decision = classify(draft, facts)
    if decision["route"] != "engine" or not decision["negative"]:
        return None
    subject = arg_value(s_arg)
    # A verified negative already guarantees the subject is accepted; this canonical
    # membership check is a defensive guard (never fabricate a hint for an unknown
    # subject) aligned with the canonical counting below — so an amount/date
    # compound subject is matched consistently, not by raw string.
    accepted_entities_c = {canonical_value(e) for e in entity_set(facts)}
    if canonical_value(subject) not in accepted_entities_c:
        return None
    subject_c = canonical_value(subject)
    queried_rels = {canonical_value(arg_value(r_arg))} | {
        canonical_value(v) for v in canonical_variants_of(arg_value(r_arg), relation_aliases())
    }
    other_relations: set[str] = set()
    other_facts = 0
    queried_facts = 0
    for row in facts:
        if canonical_value(row["subject"]) != subject_c:
            continue
        if canonical_value(row["relation"]) in queried_rels:
            queried_facts += 1
        else:
            other_facts += 1
            other_relations.add(row["relation"])
    # The subject HAS the queried relation (just not this object): an object
    # mismatch, not a predicate mismatch — no hint.
    if queried_facts:
        return None
    # Honest verified negative: the subject has no fact under any other relation
    # either. Preserve the "verified absence" value — emit nothing.
    if not other_relations:
        return None
    shown = sorted(other_relations)[:max_relations]
    listing = ", ".join(shown)
    if len(other_relations) > len(shown):
        listing += ", ..."
    return (
        f"note: no verified '{arg_value(r_arg)}' for '{subject}', but '{subject}' has "
        f"{other_facts} fact(s) under other relations (possible predicate mismatch): "
        f"{listing}"
    )


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
    args = query_args(draft)
    if predicate == "relation":
        rows = evaluate_relation(draft, facts)
        result: dict[str, object] = {"rows": rows, "count": len(rows)}
        # Optional, additive coverage hint (#189) for a verified-negative relation
        # query — never changes rows/count, only appended when informative.
        if not rows:
            hint = coverage_hint(draft, facts)
            if hint:
                result["coverage_hint"] = hint
        return result
    if predicate == "count":
        # count(subject, relation)? -> number of distinct objects (a verified
        # aggregate; 0 is a real answer). Rendered as a single value row.
        # When the relation arg is a quoted canonical name (surface_variants
        # non-empty), count DISTINCT objects across the canonical AND all its
        # surface variants — symmetry with the relation branch (#227).
        # Guard arity BEFORE unpacking: a count with != 2 args is malformed. Match
        # classify_query (BAD_ARITY) and raise the same NotImplementedError the
        # unknown-predicate fallthrough uses, so cmd_evaluate turns it into a clean
        # error JSON instead of an uncaught IndexError (< 2 args) or a silently
        # accepted, bogus count (> 2 args) (#257).
        if len(args) != 2:
            raise NotImplementedError("count query must have subject and relation arguments")
        subject, relation = arg_value(args[0]), arg_value(args[1])
        rel_variants: set[str] = set()
        if is_quoted_string(args[1]):
            rel_variants = canonical_variants_of(relation, relation_aliases())
        objects = {
            row["object"]
            for row in facts
            if (is_variable(args[0]) or row["subject"] == subject)
            and (is_variable(args[1]) or row["relation"] == relation
                 or row["relation"] in rel_variants)
        }
        return {"rows": [[str(len(objects))]], "count": len(objects)}
    if predicate == "path":
        if len(args) == 2 and all(is_quoted_string(a) for a in args):
            path = dependency_path(facts, arg_value(args[0]), arg_value(args[1]))
            rows = [path] if path else []
        else:
            rows = [
                [start, target]
                for (start, target) in sorted(_reachable_pairs(facts))
                if (len(args) == 2
                    and (is_variable(args[0]) or arg_value(args[0]) == start)
                    and (is_variable(args[1]) or arg_value(args[1]) == target))
            ]
        return {"rows": rows, "count": len(rows)}
    if predicate in policy_predicates(_policy_program_optional()):
        # Engine evaluation of a policy predicate re-loads the policy program AND
        # runs pyrewire (common.run_wirelog): a hand-authored logic-policy.extra.dl
        # can make this fail loud in TWO ways the routing-time loader guard does
        # NOT cover — a FactlogError (e.g. an unscaled `number` threshold,
        # _assert_no_unscaled_number_threshold) or a pyrewire ParseError from
        # broken .dl syntax (NOT a FactlogError, so run_cli would not catch it and
        # ask would crash with a traceback). ask is exploratory and must never
        # hard-fail (#193). Degrade to a signalled empty result the callers surface
        # as a warning instead of a verified answer (rendering [] here would fake a
        # verified negative). Catch broad Exception — never BaseException, so
        # KeyboardInterrupt/SystemExit still propagate — because the engine may
        # raise non-FactlogError types.
        try:
            inferred = run_wirelog()
        except Exception as exc:  # noqa: BLE001 — engine/loader raise non-FactlogError too
            return {"rows": [], "count": 0, "policy_unevaluable": str(exc)}
        rows = []
        for row in sorted(inferred.get(predicate, set())):
            if args and is_quoted_string(args[0]) and (not row or arg_value(args[0]) != row[0]):
                continue
            rows.append(list(row))
        return {"rows": rows, "count": len(rows)}
    raise NotImplementedError(f"engine evaluation of predicate '{predicate}' is not supported")


def render_engine_answer(
    draft: str,
    rows: list[list[str]],
    signals: dict[tuple[str, str, str], dict[str, object]] | None = None,
    annotate_objects: bool = False,
) -> str:
    """Render the VERIFIED — engine answer block (positive or negative).

    The literal marker 'VERIFIED — engine' is the greppable verification token.
    The engine verdict is BINARY — a row is verified or it is not; it carries no
    probability. The annotations below describe the *evidentiary basis* of a
    verified row, never the certainty of the verdict:

    - A relation row backed by an extracted candidate is annotated with
      '(sources: N, extraction conf: C)' — the distinct-source count and the
      LLM's source->fact *extraction* confidence (a candidate-stage trust signal,
      NOT a confidence in the engine verification) — plus '[stale: source
      missing]' when a backing source has vanished, with backing source path(s)
      listed beneath ('    ← <source>').
    - A relation row with NO backing extraction (no signal entry) carries no
      extraction confidence, so it is marked '[no extraction backing]' rather
      than left ambiguous. Today accepted.dl is a 1:1 projection of the
      candidates table and no rule derives relation atoms, so this only arises
      when the two are out of sync (recompile via /factlog check); it would also
      cover a future rule-derived relation. Either way the verdict stays binary.

    Non-relation predicates (path/count/policy) pass signals=None and
    annotate_objects=False: their rows are computed by the engine, carry no
    extraction confidence by construction, and are rendered without annotation.
    Both the signals annotation and the humanize annotation are gated to relation
    rows via these flags; a coincidental 3-element shape on a path/policy row
    never triggers either annotation.
    """
    lines = ["VERIFIED — engine", f"query: {draft}", f"rows: {len(rows)}"]
    if rows:
        for row in rows:
            line = f"  - {', '.join(row)}"
            # Display-only: annotate a compound-term object (amount/date/number)
            # with its human-friendly form. Gated to relation rows via
            # annotate_objects so a coincidental 3-element shape on a path/policy
            # row is never annotated. The stored/canonical string stays in the row
            # verbatim (still copy-paste queryable); the pretty form is appended,
            # never substituted. No-op for plain objects (#188 follow-up).
            if annotate_objects and len(row) == 3:
                pretty = literal_types.humanize(row[2])
                if pretty != row[2]:
                    line += f"  (= {pretty})"
            sig = signals.get((row[0], row[1], row[2])) if signals is not None and len(row) == 3 else None
            if sig:
                line += f" (sources: {sig['sources']}, extraction conf: {sig['confidence']})"
                if sig.get("stale"):
                    line += " [stale: source missing]"
            elif signals is not None and len(row) == 3:
                # A relation answer is expected to have an extraction-backed signal
                # per row. A row without one carries no extraction confidence:
                # today that means candidates.csv/accepted.dl are out of sync
                # (accepted.dl is a 1:1 projection of the candidates table — no
                # rule derives relation atoms yet); it would also cover a future
                # rule-derived relation. Mark the absence; the verdict stays
                # binary (the row IS verified).
                line += " [no extraction backing]"
            lines.append(line)
            if sig:
                for path in sig.get("source_paths", []):
                    lines.append(f"    ← {path}")
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
    # Tokenizer captures programming-term punctuation: internal '.'/'-' (node.js,
    # 도구가) and trailing '+'/'#' (c++, c#, f#), while excluding trailing
    # sentence punctuation. Plain \w runs (incl. CJK) still tokenize as before.
    for word in re.findall(r"\w+(?:[.+#-]+\w+)*[+#]*", question.lower(), flags=re.UNICODE):
        if word in seen:
            continue
        if _is_cjk(word):
            if len(word) >= 2:
                seen.add(word)
                patterns.append(re.compile(re.escape(word)))
        elif len(word) > 2:
            seen.add(word)
            # Lookaround boundaries (not \b) so punctuation-edged tokens like
            # 'c++' / 'c#' match while 'api' still does not match inside
            # 'therapist'.
            patterns.append(re.compile(rf"(?<!\w){re.escape(word)}(?!\w)"))
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
    (graceful degrade). The backend reorders only the already-capped top lexical
    candidates; it cannot widen recall beyond lexical matches. The module runs
    with full process privileges (it is opt-in by the KB operator)."""
    module_name = os.environ.get("FACTLOG_EMBED_MODULE")
    if not module_name or not results:
        return results
    try:
        backend = importlib.import_module(module_name)
        scores = backend.rank(question, [str(r["excerpt"]) for r in results])
        if not isinstance(scores, list) or len(scores) != len(results):
            return results
        floats = [float(score) for score in scores]
        if not all(math.isfinite(value) for value in floats):
            return results  # reject NaN/inf → keep lexical order
        order = sorted(range(len(results)), key=lambda i: floats[i], reverse=True)
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


def _entity_mentioned(entity: str, question_low: str) -> bool:
    """Whether an accepted entity name appears in the question (bilingual,
    matching the keyword matcher's contract): CJK substring (length >= 2);
    ASCII lookaround boundaries so punctuation-edged names like 'C++'/'.NET'
    match while short names don't match inside unrelated words."""
    name = entity.lower()
    if _is_cjk(entity):
        return len(entity) >= 2 and name in question_low
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", question_low) is not None


def grounding_facts(question: str, accepted: list[dict[str, str]]) -> list[dict[str, str]]:
    """Engine-verified accepted facts about the accepted entities the question
    mentions — verified anchors to show alongside an unverified wiki answer.
    Pure: only reads the accepted facts passed in."""
    question_low = question.lower()
    mentioned = {ent for ent in entity_set(accepted) if _entity_mentioned(ent, question_low)}
    if not mentioned:
        return []
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, str]] = []
    for row in accepted:
        if row["subject"] in mentioned or row["object"] in mentioned:
            key = (row["subject"], row["relation"], row["object"])
            if key not in seen:
                seen.add(key)
                out.append(row)
    return out


def render_wiki_answer(
    question: str,
    reason: str,
    results: list[dict[str, object]],
    grounding: list[dict[str, str]] | None = None,
) -> str:
    """Render the UNVERIFIED — wiki exploration answer block.

    The literal marker 'UNVERIFIED — wiki exploration' is the greppable token.
    Excerpt citations point only at source text (sources/ , runs/sources/). When
    *grounding* is given, the answer additionally shows a clearly-separated
    'VERIFIED — engine' block of accepted facts about the entities the question
    mentions, so verified anchors sit beside the unverified prose.
    """
    lines = [
        "UNVERIFIED — wiki exploration",
        f"question: {question}",
        f"reason: {reason}",
    ]
    if grounding:
        lines.append("")
        lines.append("VERIFIED — engine (grounding: accepted facts about mentioned entities):")
        lines.extend(f"  - {row['subject']}, {row['relation']}, {row['object']}" for row in grounding)
        lines.append("")
    lines.append(f"sources searched: {', '.join(label for _rel, label in _wiki_corpus())}")
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
            # Additive coverage hint (#189): if this verified-negative relation
            # query has an accepted subject that carries fact(s) under OTHER
            # relations, surface a predicate-mismatch note. The verdict block above
            # is untouched — this is an extra line, not a change to the answer.
            hint = coverage_hint(args.draft, facts)
            if hint:
                print(hint)
        else:
            # Positive engine answer: relation, path, and policy predicates are all
            # evaluated by the engine and rendered (0 rows -> a verified-empty
            # result, never a wiki fallback).
            result = evaluate(args.draft, facts)
            if result.get("policy_unevaluable"):
                # A policy predicate needs the engine, but the hand-authored policy
                # could not be evaluated (broken logic-policy.extra.dl). Do NOT
                # render an empty engine answer — that would fake a verified
                # negative. Degrade to a wiki directive + a warning, rc 0: ask never
                # crashes or hard-fails on a human extra.dl mistake (#193).
                print(json.dumps(
                    {
                        "route": "wiki",
                        "reason": "policy unevaluable — logic-policy.extra.dl could not be evaluated",
                        "policy_uncompiled": decision["policy_uncompiled"],
                    },
                    ensure_ascii=False,
                ))
                print(POLICY_UNEVALUABLE_WARNING.format(reason=result["policy_unevaluable"]))
                return 0
            # Answer-quality signals (sources/extraction-conf/staleness) annotate
            # relation rows only (the (s,r,o) key is a relation triple); gate on the
            # predicate so path/policy rows are never annotated by a coincidental
            # 3-element shape.
            is_relation = decision["predicate"] == "relation"
            signals = (
                fact_signals(load_facts(), Path(os.environ["FACTLOG_ROOT"]))
                if is_relation and CANDIDATES_CSV.is_file()
                else None
            )
            print(render_engine_answer(
                args.draft,
                result["rows"],
                signals,
                annotate_objects=is_relation,
            ))
        # The engine answer is real, but if the author wrote policy rules and
        # never compiled them, the engine had no policy to apply — say so, so a
        # policy-free answer is not mistaken for a policy-checked one (#193).
        if decision["policy_uncompiled"]:
            print(POLICY_UNCOMPILED_WARNING)
        return 0
    # route == wiki: emit a machine-readable directive so the caller runs wiki
    # exploration. Always carry policy_uncompiled (same schema as `validate`), so
    # the caller can surface the same warning the wiki answer appends.
    print(json.dumps(
        {
            "route": "wiki",
            "reason": decision["reason"],
            "policy_uncompiled": decision["policy_uncompiled"],
        },
        ensure_ascii=False,
    ))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    root = Path(os.environ["FACTLOG_ROOT"])
    print(json.dumps({"results": search(args.text, root)}, ensure_ascii=False))
    return 0


def cmd_wiki(args: argparse.Namespace) -> int:
    root = Path(os.environ["FACTLOG_ROOT"])
    results = search(args.text, root)
    # Grounding: accepted facts about mentioned entities (empty if not compiled yet).
    accepted = load_accepted_facts() if ACCEPTED_DL.is_file() else []
    grounding = grounding_facts(args.text, accepted)
    print(render_wiki_answer(args.text, args.reason, results, grounding))
    # A wiki answer is already UNVERIFIED, but an uncompiled-but-authored policy
    # is a separate, actionable defect the author should fix — surface it (#193).
    if _policy_uncompiled():
        print(POLICY_UNCOMPILED_WARNING)
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
    from common import run_cli

    raise SystemExit(run_cli(main))
