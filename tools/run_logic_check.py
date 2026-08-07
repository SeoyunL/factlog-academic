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
import tempfile
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
    attribute_relations,
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
    query_arity_error,
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


def path_endpoints(line: str) -> list[str]:
    """The quoted endpoints of a path query, INCLUDING an empty literal.

    `quoted_constants` matches `"([^"]+)"`, so it drops `""` entirely: with it,
    `path("갑봇", "")?` yielded one constant, failed the `len(constants) >= 2`
    gate, and vanished from the report — no result line, no warning — while
    `ask`'s router answers the same query with a reason. #329 is what made `""`
    a graph node that is NOT an accepted entity, so the report has to say so."""
    return [arg_value(arg) for arg in query_args(line) if is_quoted_string(arg)]


def display_value(value: str) -> str:
    """A value as it should read in the report. The empty string is rendered
    `""` so `path 갑봇 -> "" ` does not print as a dangling arrow.

    ``one_line`` because this is the rendering helper for values DECODED out of
    a query: ``arg_value`` is ``json.loads``, so ``"a\\nstatus: ..."`` in
    facts/query.dl — one physical line, the escape written as two ordinary
    characters — decodes to a real newline. Every caller that renders a decoded
    query value goes through here or is wrapped at its own call site."""
    return one_line(value) if value else '""'


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


def query_error(label: str, line: str) -> str | None:
    """The report's single verdict on *line*'s SIGNATURE, or None when answerable.

    Neither rule is restated here: ``common.query_arity_error`` and
    ``common.query_shape_error`` are the same two functions ``classify_query``
    applies, in the same order, so the gate and the report cannot disagree about
    which lines are answerable nor word the verdict differently. They used to, on
    every predicate:

    - ``count`` was checked on ARITY ONLY, so ``count("S", 'r')?`` reached
      ``evaluate_queries``, where a non-double-quoted argument is treated as a
      WILDCARD rather than a filter. The count then ranged over every relation of
      that subject and was printed as an engine-verified aggregate (#328). An
      aggregate is the output a reader is least able to check by eye, which is
      why answering it wrongly is worse than not answering it.
    - a policy query was checked on arity only, so ``stale_entity(Alice, stale)?``
      had both bare tokens taken for variables and rendered the predicate's WHOLE
      extent with invented bindings (``Alice=Bob``).
    - ``relation`` and ``path`` were checked on NEITHER rule — they fell through
      to the generic warning loop. Same mechanism, wider blast radius:
      ``relation("Marie Curie", 'born_in', O)?`` reported rows spanning every
      relation of the subject, each carrying a nonsense binding
      (``'born_in'=worked_at``), and an arity violation was answered as a
      confident NEGATIVE about a fact that is in the KB:

          relation("Marie Curie", "born_in", "Warsaw", X)?
            gate   -> bad_arity
            report -> relation results: 0 rows
          path("Marie Curie", "Warsaw", "Poland")?
            gate   -> bad_arity
            report -> path Marie Curie -> Warsaw: Marie Curie -> Warsaw

      A dropped extra argument is a plausible typo, and both answers read as
      engine-verified.

    ARITY IS CHECKED FIRST, which is not interchangeable with the other order:
    it decides the DIAGNOSIS a line that breaks both rules receives. Shape-first
    told the author of ``relation()?`` that "arguments must be variables or
    quoted strings" — advice about the quoting of arguments that are not there.

    One verdict serves both the Errors section (``validate_query``) and the
    answer renderers (``evaluate_queries``, ``policy_result_line``), so the
    report cannot call a line an error and answer it in the same run.

    Message wording is the gate's, with the offending line appended — the
    convention every other error in this module follows.
    """
    args = query_args(line)
    message = query_arity_error(label, args) or query_shape_error(label, args)
    return f"{message}: {line}" if message else None


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

    Signature errors come first, from ``query_error``: a line the gate refuses on
    arity or shape is reported as an error and never also warned about or
    answered (#328).
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
        policy_error = query_error("policy query", line)
        if policy_error:
            errors.append(policy_error)
            return errors, warnings
        args = query_args(line)
        if is_quoted_string(args[0]) and arg_value(args[0]) not in entities:
            warnings.append(
                f"query references non-engine entity: {one_line(arg_value(args[0]))}"
            )
        return errors, warnings
    if predicate == "count":
        # count(subject, relation)? — engine-verified aggregate (see evaluate_queries).
        count_error = query_error("count", line)
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
        signature_error = query_error(predicate, line)
        if signature_error:
            errors.append(signature_error)
            return errors, warnings
        # SIGNATURE first, then vocabulary: a path query the gate refuses on arity
        # or shape has already returned, so the endpoint check below never sees a
        # half-parsed argument and the same line is never both an error and a
        # warning (#328 + #329).
        if predicate == "path" and path_nodes is not None:
            # A path node must be an ENTITY. The object of a declared attribute
            # relation is a literal value: it is in the KB (so the generic check
            # below stays silent) but cannot sit on a path. classify_query refuses
            # the same query outright — say why here too, rather than letting the
            # result line answer "(not found)", which reads as "the facts do not
            # connect them" (#329).
            for constant in path_endpoints(line):
                # `not constant` covers the empty string, which value_set drops, so
                # the generic unknown-constant check below is silent on it as well —
                # without it that endpoint drew no diagnostic anywhere.
                if constant not in path_nodes and (constant in entities or not constant):
                    warnings.append(
                        f"query path argument is not an accepted entity: {display_value(constant)}"
                    )
    for constant in quoted_constants(line):
        if constant and constant not in entities and constant not in {"S", "R", "O", "X", "Q"}:
            warnings.append(
                f"query references non-engine entity or relation: {one_line(constant)}"
            )
    return errors, warnings


# The two validate_query warnings that fire on a constant absent from the KB
# vocabulary. A relation NAME is legitimate vocabulary, so a warning about one is
# dropped by the caller.
_UNKNOWN_CONSTANT_PREFIXES = (
    "query references non-engine entity or relation: ",
    "query references non-engine entity: ",
)


def names_a_relation(warning: str, relations: set[str]) -> bool:
    """True when *warning* is an unknown-constant warning about a relation NAME.

    Matching the prefix matters: filtering on `rsplit(": ", 1)[-1]` tested the
    tail of EVERY warning, so an unrelated warning whose value happened to equal
    a relation name — `query path argument is not an accepted entity: <name>` —
    was silently dropped. Stripping the known prefix instead also keeps a value
    that itself contains ": " intact."""
    for prefix in _UNKNOWN_CONSTANT_PREFIXES:
        if warning.startswith(prefix):
            return warning[len(prefix):] in relations
    return False


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

    The test is `query_error`, the SAME verdict validate_query puts in the
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
    if query_error("policy query", line) is not None:
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
                bindings.append(f"{arg}={one_line(value)}")
        values.append(", ".join(bindings) if bindings else ", ".join(one_line(v) for v in row))
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
    # Read policy/attribute-relations.md at most once for the whole run, not once
    # per path query — dependency_path falls back to reading it when the argument
    # is None. classify_query hoists it the same way. Stays lazy so a KB with no
    # path query does not touch the file at all.
    attribute_rels: set[str] | None = None
    for line in query_lines():
        predicate = line.split("(", 1)[0]
        if predicate in policy_query_predicates:
            result_line = policy_result_line(predicate, line, inferred)
            if result_line is not None:
                results.append(result_line)
        elif predicate == "path":
            # Same verdict validate_query put in the Errors section, so a line
            # reported as malformed is never also answered here (#328). It runs
            # before the endpoints are read, so a refused line never reaches
            # path_endpoints at all.
            if query_error("path", line) is not None:
                continue
            constants = path_endpoints(line)
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
                head = (
                    f"path {display_value(constants[0])} -> {display_value(constants[1])}"
                )
                if not_nodes:
                    reason = ", ".join(display_value(node) for node in not_nodes)
                    results.append(
                        f"{head}: (not evaluated — not an accepted entity: {reason})"
                    )
                    continue
                is_reachable = (constants[0], constants[1]) in inferred["path"]
                if is_reachable:
                    if attribute_rels is None:
                        attribute_rels = attribute_relations()
                    trace = dependency_path(facts, constants[0], constants[1], attribute_rels)
                else:
                    trace = []
                # one_line here because the trace nodes come from
                # dependency_path over the facts and are rendered directly. `head`
                # needs no wrapping at THIS site because it is built from
                # display_value, which applies one_line itself — not because a
                # query-derived value is safe. It is not: path_endpoints goes
                # through arg_value, which is json.loads, so an escape in
                # facts/query.dl decodes to a real newline inside a single
                # physical line. That is what put the escaping into display_value
                # in the first place.
                value = " -> ".join(one_line(node) for node in trace) if trace else "(not found)"
                results.append(f"{head}: {value}")
        elif predicate == "relation":
            if query_error("relation", line) is not None:
                continue
            rows = relation_results(line, facts)
            args = query_args(line)
            result_values: list[str] = []
            for subject, relation, object_ in rows:
                bindings = []
                for arg, value in zip(args, [subject, relation, object_], strict=True):
                    if not is_quoted_string(arg):
                        bindings.append(f"{arg}={one_line(value)}")
                result_values.append(
                    ", ".join(bindings) if bindings
                    else f"{one_line(subject)}, {one_line(relation)}, {one_line(object_)}"
                )
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
            if query_error("count", line) is not None:
                continue
            subj_q, rel_q = query_args(line)
            subj, rel = arg_value(subj_q), arg_value(rel_q)
            objects = {
                f["object"]
                for f in facts
                if (not is_quoted_string(subj_q) or f["subject"] == subj)
                and (not is_quoted_string(rel_q) or f["relation"] == rel)
            }
            # Echo the query, as policy_result_line does (17cf7d3): a malformed
            # line now drops out of this list, so 3 query lines can leave 2 result
            # lines and positional correspondence silently breaks. Unlike the
            # policy echo this is unconditional — count has no "Policy evaluation:"
            # extent line for a variable-only query to be read against, and a
            # dropped line misaligns every shape equally.
            results.append(f"count results (query: {line}): {len(objects)} (distinct objects)")
        elif predicate == "review_required":
            constants = quoted_constants(line)
            question = one_line(constants[0]) if constants else "(missing question)"
            results.append(f"review_required: {question}")
    return results


# The line that tells a reader — and hooks/gate_check.sh — that this report
# describes a run in which THE ENGINE NEVER RAN. It is the whole discriminator
# between "engine ran and found nothing" and "there is nothing to find out from
# here", so it is matched as a whole line, byte for byte, on both sides. Change
# it in one place only together with the other (`grep -qxF` in the gate).
#
# The marker is NEGATIVE — a successful report carries no status line at all —
# for one reason: the success report's text is a published contract
# (tests/golden/logic_report.txt, examples/sample-kb/facts/logic_report.txt, and
# every report already sitting in a user's KB), and a positive `status: ok`
# marker would make every one of those read as unrecognised.
#
# A negative marker only works while "carries the marker" and "written by the
# failure path" are the same set, and that is NOT free. This report interpolates
# KB-derived text, and KB text reaches it through TWO decoders, each of which can
# deliver a newline the file itself does not show:
#
#   - facts/candidates.csv — a quoted CSV field may span physical lines, so a
#     hand-edited status of "odd\nstatus: engine-did-not-run" arrives as a value
#     containing a real newline;
#   - facts/query.dl — `arg_value` is `json.loads`, so `"a\nstatus: ..."` written
#     as ONE physical line, with the escape as two ordinary characters, decodes
#     to the same thing. Splitting the file into lines cannot see it. This is the
#     worst carrier of the three: query.dl is an engine input this very gate
#     guards, and `/factlog query` writes it;
#   - facts/accepted.dl — `common.parse_relation_fact` is `json.loads` too, so a
#     compiled fact carries escapes exactly like a query does, and reaches here
#     through the ordinary pipeline: compile_facts renders a candidates value into
#     dl_string form and this report decodes it back. Covered by display_value and
#     the result renderers, which one_line every value they print.
#
# Whichever the carrier, the run SUCCEEDS, the report carries real counts, and
# both readers then call it an engine failure with `reason: (not recorded)` — the
# deadlock #338 removes, rebuilt out of KB content, and since the status fix
# repeated by two consumers rather than one.
#
# `one_line` is what keeps the two sets equal, and it has to be at every site
# where a DECODED value reaches a report line — the csv-side sites alone left
# query.dl wide open. The claim to be careful with is the one this comment used
# to make: not "no interpolated value can open a line" as a property of the
# design, but "these call sites are wrapped", which is a property of a list and
# stays true only while the list is complete. tests/unit pins one carrier per
# decoder for that reason; a new interpolation site needs its own.
#
# The cost of the negative marker is also that a report truncated inside its
# first three lines would lose it and read as a success; _write_report closes
# that by replacing the file atomically.
ENGINE_FAILED_STATUS_LINE = "status: engine-did-not-run"

# Characters that may not appear inside a report line. Two families, and the
# second was learned the hard way:
#
#   - LINE BREAKS: every character str.splitlines() breaks on, which is wider
#     than "\n" because it is what a READER may split on (cli.py's splitlines()
#     did). A value carrying one opens a line for one reader and not for another.
#     U+2028 is routine in text pasted from PDFs — see factlog/common.py's
#     line-break handling.
#   - OTHER C0/C1 CONTROLS, above all NUL. A report line is judged by tools that
#     are not all Python, and a NUL made BSD sed abort mid-pipeline; because the
#     gate read that pipeline's status as "no marker", ONE NUL byte in the report
#     turned a DENY into an ALLOW. That is this whole change in reverse — not
#     forging the marker but erasing the reader's ability to see it — so no value
#     reaching a report line may carry one. ESC belongs here too: this report is
#     printed to a terminal.
#
# Pinned per family in tests/unit (newline, NUL, U+2028). Narrowing the set to
# "\n" alone used to leave every suite green.
_LINE_BREAKS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
# TAB is deliberately NOT here, and it is the only C0 character excluded. It
# cannot break a line, cannot end a reader, and cannot drive a terminal — it is
# the one control character people actually type into a KB — so escaping it would
# change the report's text for benign input while defending nothing. Measured:
# with TAB in the set, a tab-bearing status value renders differently from
# origin/main; without it, the report stays byte-identical.
_FORBIDDEN_IN_LINE = frozenset(
    _LINE_BREAKS
    + "".join(chr(c) for c in range(0x00, 0x20) if c != 0x09)
    + "\x7f"
    + "".join(chr(c) for c in range(0x80, 0xA0))
)


def one_line(value: object) -> str:
    """*value* as report text that cannot become more than one line, nor blind a
    reader of it.

    Values carrying none of _FORBIDDEN_IN_LINE — every ordinary status, entity
    and literal — are returned UNCHANGED, so the report stays byte-identical to
    what it has always written (tests/golden/logic_report.txt pins that). Only a
    value that would break or corrupt the line is escaped, via ``repr``, which
    keeps it readable and visible rather than silently dropping the offending
    part: a hand-edited status of ``odd\\nstatus: engine-did-not-run`` reports as
    ``'odd\\nstatus: engine-did-not-run'``, on one line, still naming what is
    wrong with the row.
    """
    text = str(value)
    return repr(text) if any(ch in _FORBIDDEN_IN_LINE for ch in text) else text


def _write_report(text: str) -> None:
    """Put *text* in facts/logic_report.txt, atomically and with LF endings.

    temp + os.replace, not write_text: the gate reads this file to decide
    whether editing engine inputs is allowed, and a write interrupted after the
    header but before ENGINE_FAILED_STATUS_LINE would leave a file that is
    neither report yet passes the gate as one.

    ``newline="\\n"`` is load-bearing, not tidiness. Text mode translates "\\n" to
    os.linesep, so on Windows every line of this report would be written CRLF —
    and the gate's whole-line match then stops matching, which fails OPEN: it
    hands out edit rights on engine inputs at the moment the engine is broken.
    Measured here by writing a CRLF report by hand (gate exit 0 where LF gives
    2); that Windows *produces* one is read off the io.TextIOWrapper contract,
    not measured, since neither this lane nor the review could run Windows. The
    gate strips CR as well, so a report from either side reads correctly.

    NOT common._atomic_write_text: same temp+replace shape, but that one writes
    in default text mode. Reusing it would reintroduce exactly the translation
    this pins against; the duplication is the difference.
    """
    out = FACTS_DIR / "logic_report.txt"
    # A per-run temp name, not a fixed "<name>.tmp": two checks running against
    # one KB would otherwise write the SAME temp file and each replace it, so a
    # reader could be handed a file mixing both runs. Atomicity against
    # truncation held with the fixed name; atomicity against a concurrent run did
    # not. The finally-unlink keeps a failed replace from leaving the temp behind.
    fd, tmp_name = tempfile.mkstemp(dir=str(out.parent), prefix=out.name + ".", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink()


def engine_failure_report(exc: BaseException) -> str:
    """The report for a run that could not run the engine (#338).

    Until this existed, an engine that could not start — pyrewire missing or too
    old, facts/accepted.dl absent, a policy program the engine refuses — left
    facts/logic_report.txt *untouched*. Two things followed, and the second is
    the reason this function reports failure rather than staying silent:

    - the previous run's report survived on disk and read as this run's result.
      A reader (and `/factlog check`'s output) had nothing to tell the two
      apart, so a report describing facts that are no longer compiled looked
      current;
    - the gate's freshness predicate had nothing to compare against, so it
      denied every edit to an engine input and pointed at `/factlog check`,
      which is the command that had just failed.

    What this report may and may not say:

    - It states the CAUSE and nothing about the KB. Every count a successful
      report carries (engine facts, policy findings, errors, warnings) is
      OMITTED rather than written as 0 — `engine facts: 0` is a claim that the
      engine ran over an empty KB, which is exactly the sentence this report
      must not produce.
    - It does NOT satisfy the gate. A report of a run that did not happen is not
      evidence that editing facts/accepted.dl or facts/query.dl is safe, so
      ENGINE_FAILED_STATUS_LINE keeps the deny in place — the gate's message
      just changes from "run /factlog check" to the actual cause. Recovery is
      the Bash route in docs/guide/determinism.md, which the hook's Write|Edit
      matcher deliberately leaves open.
    - It does not change the exit code. main re-raises, so `/factlog check`
      still fails and still prints the error on stderr; this file is a side
      effect of failing, never a sign of success.

    *reason* is collapsed to a single line and then escaped like any other
    KB-derived value: the format is line-oriented and judged whole-line on the
    other side, and an engine ParseError's message spans several lines.

    THE REASON LINE IS A CARRIER, and the one the carrier list above did not
    name — it is very nearly the only place KB text enters a FAILURE report. A
    line of facts/accepted.dl the engine refuses goes into the exception message
    verbatim, so whatever that line holds lands here. ``" ".join(split())``
    collapses Python whitespace only, so a NUL rode it into the report intact and
    blinded the gate's reader; ``one_line`` is what closes that, and it is the
    same grade of escaping every other interpolation gets.
    """
    reason = one_line(" ".join(str(exc).split())) or exc.__class__.__name__
    accepted = FACTS_DIR / "accepted.dl"
    query = FACTS_DIR / "query.dl"
    return "\n".join(
        [
            "Logic Check Report",
            "==================",
            ENGINE_FAILED_STATUS_LINE,
            "engine: wirelog / pyrewire",
            "input: facts/accepted.dl",
            f"reason: {reason}",
            f"reason type: {type(exc).__name__}",
            f"facts/accepted.dl: {'present' if accepted.is_file() else 'MISSING'}",
            f"facts/query.dl: {'present' if query.is_file() else 'absent'}",
            "",
            "The engine did not run, so this report says NOTHING about the KB.",
            "The counts a successful report carries — engine facts, policy",
            "findings, errors, warnings — are missing above rather than 0,",
            "because 0 would mean the engine ran and found nothing.",
            "",
            "Any earlier report has been replaced, so nothing here can be read as",
            "an older run's result. While this status line is present, EDITS to",
            "facts/accepted.dl and facts/query.dl stay denied — creating either",
            "for the first time in this KB is still allowed, as it is when no",
            "report exists at all.",
            "",
            "Recovery: fix the cause above and re-run the logic check. When the",
            "check cannot run at all, see the Bash recovery in",
            "docs/guide/determinism.md — the gate's Write|Edit matcher leaves it",
            "open on purpose.",
            "",
        ]
    )


def main() -> None:
    ensure_dirs()
    try:
        text = build_report_text()
    except Exception as exc:
        # Report the failure, then let it propagate untouched: run_cli prints a
        # FactlogError on stderr and exits 1, anything else keeps its traceback.
        # ensure_dirs is deliberately OUTSIDE this — "not a factlog KB root" has
        # no facts/ to write into, and is not a statement about the engine.
        #
        # THE GUARANTEE IS "an exception out of build_report_text", NOT "any run
        # in which the engine could not start". A run that dies before reaching
        # this try still leaves the previous report standing, and one such path
        # is a real engine failure rather than a hypothetical: `common` guards
        # its `import pyrewire` with `except ImportError` ONLY, so an engine
        # whose import fails some other way — a broken native extension raising
        # OSError from dlopen — propagates out of the `from common import ...`
        # above, at module import time, where no handler here can run. Measured:
        # no report is written, and the traceback is the only output. Widening
        # that guard belongs next to it in factlog/common.py, not here; catching
        # it at this module's import would mean rebuilding FACTS_DIR without the
        # module that defines it.
        #
        # BEST EFFORT, because reporting the failure must not REPLACE it. With
        # facts/ read-only, writing raised PermissionError from inside this
        # handler and that became the program's output: the operator got a
        # traceback about the report instead of the one clean line naming the
        # actual cause, which origin/main gave them. A report we could not write
        # is not worth the diagnosis we already have.
        try:
            _write_report(engine_failure_report(exc))
        except Exception as write_exc:  # noqa: BLE001 - never mask *exc*
            print(
                f"warning: could not write facts/logic_report.txt: {write_exc}",
                file=sys.stderr,
            )
        raise
    _write_report(text)
    print(text)


def build_report_text() -> str:
    """The report for a run that reached the engine, byte-identical to what this
    module has always written (tests/golden/logic_report.txt pins it).

    It is a separate function only so main can tell a report it could build from
    one it could not: everything here presumes ``run_wirelog`` returned, and the
    one caller turns any exception raised below into ``engine_failure_report``.
    """
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
            # one_line: this is the report's most direct path from a hand-edited
            # CSV cell to a report line, and a quoted CSV field may contain a
            # newline. Unescaped, a status of "odd\nstatus: engine-did-not-run"
            # put ENGINE_FAILED_STATUS_LINE into a SUCCESSFUL report and both
            # readers then called a completed run an engine failure.
            warnings.append(f"unknown status treated as non-engine input: {one_line(row['status'])}")

    for predicate in sorted(policy_query_predicates):
        for target, reason in sorted(inferred[predicate]):
            policy_findings.append(f"{one_line(predicate)}: {one_line(target)} ({one_line(reason)})")

    for line in query_lines():
        query_errors, query_warnings = validate_query(line, entities, policy_query_predicates, path_nodes)
        errors.extend(query_errors)
        warnings.extend(
            [item for item in query_warnings if not names_a_relation(item, relations)]
        )

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
    query_results = evaluate_queries(facts, inferred, policy_query_predicates, path_nodes)
    if query_results:
        report.extend([f"- {item}" for item in query_results])
    elif not (FACTS_DIR / "query.dl").is_file() and not query_lines():
        report.append("- no facts/query.dl found")
    else:
        # A file whose every line was refused is not a missing file. SKILL.md
        # reads "no facts/query.dl found" as "the /factlog query step was
        # skipped", so printing it here would send the reader to re-run a step
        # they already ran instead of to the Errors section above.
        #
        # "Missing" needs BOTH signals absent. Keying on `query_lines()` alone
        # called a comment-only query.dl missing, which is the same false claim;
        # keying on the file alone would call a line set supplied by a caller
        # missing. A query.dl holding only variable-form path queries
        # (`path(X, Y)?`) lands here too — those are answerable, they just render
        # no result line, because a path result names its two endpoints.
        report.append("- no answerable queries in facts/query.dl (see Errors)")

    return "\n".join(report) + "\n"


if __name__ == "__main__":
    from common import run_cli

    # main() takes no argv; parse here only so `--wiki` is a documented option
    # with --help, and a mistyped flag is rejected instead of silently ignored.
    _parser = argparse.ArgumentParser(description="Run deterministic logic checks over facts and query drafts.")
    _parser.add_argument("--wiki", default=os.environ["FACTLOG_ROOT"], help="KB root")
    _parser.parse_args()
    raise SystemExit(run_cli(main))
