#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Generate policy/logic-policy.dl from controlled natural-language policy text."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from common import (
    POLICY_DIR,
    PROMPTS_DIR,
    RUNS_DIR,
    WIRELOG_PROGRAM,
    FactlogError,
    _engine_decl_predicates,
    dl_string,
    ensure_dirs,
    logic_policy_md_relations,
    markdown_policy_items,
    require_pyrewire_version,
    wirelog_undecodable_chars,
)

try:
    from pyrewire import EasySession
except ImportError:  # pragma: no cover - exercised only on machines without pyrewire.
    EasySession = None


SOURCE_MD = POLICY_DIR / "logic-policy.md"
OUTPUT_DL = POLICY_DIR / "logic-policy.dl"
PROMPT_MD = PROMPTS_DIR / "natural_language_to_policy.md"
PROMPT_OUT = RUNS_DIR / "natural-language-to-policy-prompt.md"
RESPONSE_OUT = RUNS_DIR / "natural-language-to-policy-response.json"
TRACE_OUT = RUNS_DIR / "natural-language-to-policy-trace.md"
FAILED_OUT = RUNS_DIR / "natural-language-to-policy-failed.md"
# The three artifacts a generating run accounts for in FAILED_OUT. Ordered as the run
# writes them, so the marker reads in pipeline order.
RUN_ARTIFACTS = (PROMPT_OUT, RESPONSE_OUT, TRACE_OUT)
REASON_RE = re.compile(r"^[a-z0-9_]+$")
PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
RELATION_RE = re.compile(r"^[^\s\"`(),.]+$")
# A generated bullet may never HEAD a predicate the engine already declares
# (common.WIRELOG_PROGRAM); doing so makes pyrewire treat that EDB/IDB as an IDB the
# policy owns and silently mishandle it with rc=0. DERIVED from the engine's own .decl
# set (#334) so it cannot drift the way it did in #332 (relation_alive missing) — the
# hand-managed literal that lost review_required (declared by no .decl) and never gained
# relation_alive is exactly what this replaces. This is one of four consumers of
# common._engine_decl_predicates; test_reserved_predicate_parity pins them together.
RESERVED_PREDICATES = _engine_decl_predicates()
CANONICAL_MARKER = "{canonical}"
# The canonical marker is an ANCHORED, lowercase `{canonical}` followed by an ASCII
# space. The separator was `\s+`, which also matches NBSP, so a `{canonical}\xa0` typo
# still compiled as a canonical rule; it is narrowed to ASCII space/tab here.
CANONICAL_PREFIX_RE = re.compile(r"^\{canonical\}[ \t]+")
# Any `{...}` at the very START of a sentence is a marker ATTEMPT. If it is not exactly
# the canonical marker above, we refuse rather than silently falling back to a relation
# body (#335): a canonical head is blocked by RESERVED_PREDICATES anyway, so an
# unrecognized marker has nowhere safe to go — a load failure beats a silent meaning flip.
_LEADING_MARKER_RE = re.compile(r"^\{[^}]*\}")


def _strip_canonical_prefix(sentence: str, lineno: int) -> tuple[bool, str]:
    """Return (is_canonical, sentence_without_marker).

    The canonical marker is an ANCHORED lowercase ``{canonical}`` followed by an ASCII
    space; a mid-sentence or prose {canonical} does NOT count, so a documentation bullet
    mentioning it never becomes a live rule. A sentence that STARTS with a ``{...}`` marker
    shape that is NOT exactly that — a case variant like ``{Canonical}``, a space-less
    ``{canonical}`x``, or an NBSP separator — is rejected LOUDLY (#335) instead of
    compiling to a relation(...) body under a meaning the author did not intend.
    """
    stripped = sentence.strip()
    m = CANONICAL_PREFIX_RE.match(stripped)
    if m:
        return True, stripped[m.end():].strip()
    marker = _LEADING_MARKER_RE.match(stripped)
    if marker:
        raise FactlogError(
            f"policy/logic-policy.md line {lineno}: unrecognized leading marker "
            f"{marker.group(0)!r}. The only supported marker is '{CANONICAL_MARKER}' "
            f"followed by an ASCII space; a case variant, a missing space, or a "
            f"non-ASCII separator would silently compile to a relation(...) rule body "
            f"instead of canonical(...). Fix the marker, or if the '{{...}}' is prose, "
            f"do not place it at the start of the sentence."
        )
    return False, sentence


def read_required(path: Path) -> str:
    if not path.is_file() or not path.read_text(encoding="utf-8").strip():
        raise SystemExit(f"missing or empty {path.relative_to(path.parents[1])}")
    return path.read_text(encoding="utf-8")


def render_prompt(policy_text: str) -> str:
    template = read_required(PROMPT_MD)
    if template.count("{{POLICY_TEXT}}") != 1:
        raise SystemExit("policy/prompts/natural_language_to_policy.md must contain {{POLICY_TEXT}} exactly once")
    rendered = template.replace("{{POLICY_TEXT}}", policy_text).strip()
    unresolved = sorted(set(re.findall(r"{{[^}]+}}", rendered)))
    if unresolved:
        raise SystemExit(f"policy prompt contains unknown placeholder(s): {', '.join(unresolved)}")
    return rendered


# markdown_policy_items lives in factlog/common.py so this compiler and the
# "does this .md define rules?" check (common.logic_policy_md_has_rules, used by
# _load_logic_policy_from and finalize.py) share one parser and never drift (upstream#190).


def _reject_undecodable_policy_name(kind: str, name: str, lineno: int) -> None:
    """Refuse to compile a policy name carrying a control char dl_string would emit as a
    wirelog-undecodable escape (#359, the policy-text sibling of #331/#357).

    Placement and keep/delete rules: see wirelog_undecodable_chars (factlog/common.py).

    Both names below reach the .dl through dl_string (json.dumps), and the engine decodes
    only \\" and \\\\ — so a C0 control (U+0000–U+001F) is stored as a literal backslash
    plus letter. The rule body then names a relation no fact can ever hold: the policy is
    silently dead rather than wrong, which is the worst failure mode for a gate. We check
    HERE because this is the only point where the source lineno survives (normalized_rules
    knows the rule index only), so the error can name the bullet to fix.

    This gate covers the DETERMINISTIC path only: it runs from fixture_policy_json, which
    an LLM draft never calls (that path is parse_json_object -> normalized_rules). The
    draft path is gated separately inside normalized_rules (#365); claims about what can
    reach where hold per path, not globally.

    Reachability is asymmetric. RELATION_RE excludes whitespace but nothing else, so 23 C0
    characters (\\x00-\\x08, \\x0e-\\x1b) pass it and reach us. Nothing reaches us on the
    reason axis, but NOT because of REASON_RE — that runs in normalized_rules, i.e. AFTER
    this gate, so it cannot decide what arrives here. The real boundary HERE is the bullet
    tag regex in markdown_policy_items (common.py), which admits no C0 character into a
    reason tag, so such a bullet is not a policy item at all. On the draft path there is no
    bullet, and there REASON_RE is the reason axis's only defence.

    We gate reason anyway, because that boundary is a PARSING rule, not an integrity rule:
    markdown_policy_items exists to define bullet syntax (upstream#190), not to protect the
    engine's wire format. Whoever later widens the tag grammar is making a parsing decision and has
    no reason to suspect they are opening an engine-integrity hole — which is exactly when
    a cheap local check at the emission site earns its keep.
    """
    bad = wirelog_undecodable_chars(name)
    if not bad:
        return
    shown = ", ".join(repr(c) for c in bad)
    raise FactlogError(
        f"policy/logic-policy.md line {lineno}: control character(s) {shown} in {kind} "
        f"{name!r} cannot be compiled: policy/logic-policy.dl would encode them as JSON "
        "escapes the wirelog engine does not decode (\\t \\n \\r \\b \\f and other "
        "U+0000–U+001F controls), so the generated rule would reference a name no fact can "
        "ever match and the policy would be silently dead (#359). Correct the bullet on that "
        f"line — retype the {kind} as clean text; do NOT write the control character back. "
        "(U+0085/U+2028/U+2029 are fine and never rejected.)"
    )


def fixture_policy_json(policy_text: str) -> dict[str, Any]:
    rules: list[dict[str, Any]] = []
    rejected: list[str] = []
    for lineno, reason, sentence in markdown_policy_items(policy_text):
        is_canonical, body_sentence = _strip_canonical_prefix(sentence, lineno)
        relations = logic_policy_md_relations(body_sentence)
        if not relations:
            rejected.append(f"line {lineno}: expected at least one backtick relation name")
            continue
        _reject_undecodable_policy_name("reason tag", reason, lineno)
        for relation in relations:
            _reject_undecodable_policy_name("backtick relation name", relation, lineno)
        predicate = infer_fixture_predicate(body_sentence)
        rule: dict[str, Any] = {
            "predicate": predicate,
            "reason": reason,
            "conditions": [{"relation": relation} for relation in relations],
        }
        if is_canonical:
            rule["canonical"] = True
        rules.append(rule)
    if not rules:
        detail = "; ".join(rejected) if rejected else "no supported policy bullets"
        raise SystemExit(f"policy/logic-policy.md has no compilable policies: {detail}")
    return {"rules": rules}


def infer_fixture_predicate(sentence: str) -> str:
    lowered = sentence.lower()
    if "충돌" in sentence or "conflict" in lowered:
        return "conflict"
    if "검토" in sentence or "review" in lowered:
        return "requires_review"
    if "경고" in sentence or "주의" in sentence or "warning" in lowered:
        return "warning"
    if "차단" in sentence or "금지" in sentence or "block" in lowered or "deny" in lowered:
        return "blocked"
    return "policy_match"


def parse_json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("policy draft must be a JSON object")
    return value


def normalized_rules(value: dict[str, Any]) -> list[dict[str, Any]]:
    if set(value) != {"rules"} or not isinstance(value["rules"], list):
        raise ValueError("policy JSON must contain only a rules list")
    rules: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...], bool]] = set()
    for idx, rule in enumerate(value["rules"], start=1):
        if not isinstance(rule, dict):
            raise ValueError(f"rule {idx} must be an object")
        required = {"predicate", "reason", "conditions"}
        optional = {"canonical"}
        unsupported = sorted(set(rule) - (required | optional))
        missing = sorted(required - set(rule))
        if unsupported or missing:
            details = []
            if unsupported:
                details.append(f"unsupported key(s): {', '.join(unsupported)}")
            if missing:
                details.append(f"missing key(s): {', '.join(missing)}")
            raise ValueError(f"rule {idx} must contain only predicate, reason, and conditions ({'; '.join(details)})")
        predicate = str(rule.get("predicate", "")).strip()
        if not PREDICATE_RE.match(predicate) or predicate in RESERVED_PREDICATES:
            raise ValueError(f"rule {idx} has invalid policy predicate name: {predicate!r}")
        reason = str(rule.get("reason", "")).strip()
        if not REASON_RE.match(reason):
            raise ValueError(f"rule {idx} reason must match [a-z0-9_]+: {reason!r}")
        conditions = rule.get("conditions")
        if not isinstance(conditions, list) or not conditions:
            raise ValueError(f"rule {idx} must have at least one condition")
        relations: list[str] = []
        for condition in conditions:
            if not isinstance(condition, dict) or set(condition) != {"relation"}:
                raise ValueError(f"rule {idx} condition must contain only relation")
            relation = str(condition["relation"]).strip()
            if not relation or not RELATION_RE.match(relation):
                raise ValueError(f"rule {idx} has invalid relation name: {relation!r}")
            # Last line of defence before emission (#365). _reject_undecodable_policy_name
            # guards the DETERMINISTIC path, but it lives in fixture_policy_json, which the
            # LLM draft path never calls: a draft goes parse_json_object -> here. RELATION_RE
            # excludes whitespace and nothing else, so all 23 C0 characters that clear it
            # (\x00-\x08, \x0e-\x1b) are wirelog-undecodable and would reach compile_policy,
            # where dl_string writes them as escapes the engine does not decode — a rule body
            # naming a relation no fact can hold, i.e. a silently dead policy. Every path to
            # compile_policy passes through this function, so this is where both meet.
            # Judged by wirelog_undecodable_chars, never by a local character set, so the
            # verdict cannot drift from the engine's actual wire format. Placement and
            # keep/delete rules: see that predicate's docstring (factlog/common.py) — this
            # gate duplicates the deterministic-path one on deterministic input yet stays,
            # because on draft input it is the only defence.
            #
            # Do NOT widen this to U+0085/U+2028/U+2029: they round-trip through the engine
            # (#255) and are not an integrity problem. RELATION_RE already refuses them a
            # few lines up as whitespace, which is why the message below does not mention
            # them — no input reaching here can involve those three, so naming them in the
            # error would describe a situation the reader is not in.
            undecodable = wirelog_undecodable_chars(relation)
            if undecodable:
                shown = ", ".join(repr(c) for c in undecodable)
                raise ValueError(
                    f"rule {idx} relation name {relation!r} carries control character(s) "
                    f"{shown} that policy/logic-policy.dl would encode as JSON escapes the "
                    "wirelog engine does not decode, so the rule would reference a name no "
                    "fact can ever match and the policy would be silently dead (#365). "
                    "Retype the relation as clean text."
                )
            relations.append(relation)
        if len(set(relations)) != len(relations):
            raise ValueError(f"rule {idx} must not repeat relation names")
        canonical = rule.get("canonical", False)
        if not isinstance(canonical, bool):
            raise ValueError(f"rule {idx} canonical flag must be a boolean")
        key = (predicate, reason, tuple(relations), canonical)
        if key in seen:
            continue
        seen.add(key)
        rules.append({"predicate": predicate, "reason": reason, "relations": relations, "canonical": canonical})
    if not rules:
        raise ValueError("policy JSON has no rules")
    # A (predicate, reason, relations) tuple appearing both canonical and
    # non-canonical is an authoring error, not two distinct rules — reject it
    # rather than silently emitting both a relation- and a canonical-bodied rule.
    flag_by_tuple: dict[tuple[str, str, tuple[str, ...]], bool] = {}
    for row in rules:
        tuple_key = (row["predicate"], row["reason"], tuple(row["relations"]))
        if tuple_key in flag_by_tuple and flag_by_tuple[tuple_key] != row["canonical"]:
            raise ValueError(
                f"rule ({row['predicate']}, {row['reason']}) appears both canonical and non-canonical"
            )
        flag_by_tuple[tuple_key] = row["canonical"]
    return sorted(rules, key=lambda row: (row["predicate"], row["reason"], *row["relations"]))


def compile_policy(rules: list[dict[str, Any]]) -> str:
    lines = [
        "// generated from policy/logic-policy.md",
        "// run tools/generate_logic_policy.py to regenerate",
        "",
    ]
    for predicate in sorted({rule["predicate"] for rule in rules}):
        lines.append(f".decl {predicate}(entity: symbol, reason: symbol)")
    lines.append("")
    for rule in rules:
        body_pred = "canonical" if rule["canonical"] else "relation"
        conditions = []
        for index, relation in enumerate(rule["relations"]):
            suffix = "." if index == len(rule["relations"]) - 1 else ","
            conditions.append(f"  {body_pred}(X, {dl_string(relation)}, _){suffix}")
        lines.extend([f"// {rule['reason']}", f"{rule['predicate']}(X, {dl_string(rule['reason'])}) :-", *conditions, ""])
    return "\n".join(lines)


def smoke_compile(policy_program: str) -> None:
    if EasySession is None:
        return
    require_pyrewire_version()
    session = EasySession(WIRELOG_PROGRAM + "\n" + policy_program)
    session.close()


def write_trace(rules: list[dict[str, Any]], output: str) -> None:
    trace = [
        "# Natural Language To Policy Trace",
        "",
        "- provider: fixture",
        f"- rules generated: {len(rules)}",
        f"- output: {output}",
        "",
    ]
    for rule in rules:
        trace.extend(
            [
                f"## {rule['reason']}",
                "",
                f"- predicate: {rule['predicate']}",
                f"- relations: {', '.join(rule['relations'])}",
                "",
            ]
        )
    TRACE_OUT.write_text("\n".join(trace), encoding="utf-8")


def failure_marker(exc: BaseException, written: list[Path]) -> str:
    """Render the accounting note a failed run leaves in runs/ (#372).

    A run that raises after PROMPT_OUT leaves runs/ describing no single run: the files
    it managed to write sit beside whatever the previous run left, and nothing in their
    bytes says which is which. Reproduce on base 4b40fac, over a copy of
    examples/sample-kb — one clean run, then replace policy/logic-policy.md with the
    CONTROL_CHAR_MD constant of tests/unit/test_failed_policy_run_marker.py (a U+0001
    inside a backtick relation name) and run again: rc=1, prompt.md c3dd879a ->
    cc0b8770, and response.json / trace.md still at 56ca7221 / 3b618ecb, the first run's
    bytes.
    That directory is an audit record of a run that never happened, which is the defect
    — not the leftovers themselves, since deleting them only moves the mismatch onto the
    earlier run's half-record.

    Files are sorted into what this run wrote and what merely sits there, because only
    the first group is evidence about the failing input. The verdict is what main()
    RECORDED writing, not a re-read of the bytes: a run whose output happens to equal
    its predecessor's is still this run's output, and a hash comparison would call it
    stale.

    The headline states only what the two groups support, because a marker that
    overstates recreates the defect it exists to report. A run that wrote every artifact
    and then failed on the .dl leaves runs/ internally consistent — the mixed-vintage
    sentence would be false there, so it appears only when something in runs/ is in fact
    older than this run. main() does not call this at all when the run wrote nothing.

    Contains no wall-clock time and no absolute paths: the same input must produce the
    same marker, the way every other generated artifact in this repo does.
    """
    written_names = {path.name for path in written}
    mine = [p for p in RUN_ARTIFACTS if p.name in written_names]
    stale = [p for p in RUN_ARTIFACTS if p.name not in written_names and p.is_file()]
    absent = [p for p in RUN_ARTIFACTS if p.name not in written_names and not p.is_file()]
    lines = [
        "# natural-language-to-policy: last run failed",
        "",
        "tools/generate_logic_policy.py raised part-way through, so policy/logic-policy.dl",
        "was not regenerated from the files below (#372).",
        "",
    ]
    if stale:
        lines += [
            "Those files do not all belong to the same run: the ones this run wrote sit",
            "beside files an earlier run left, and their bytes cannot tell you which is",
            "which. Do not read this directory as one audit record while this marker",
            "exists.",
            "",
        ]
    message = str(exc)
    lines += [
        "## Failure",
        "",
        f"{type(exc).__name__}: {message}" if message else type(exc).__name__,
        "",
    ]
    groups = [
        ("Written by this run", mine),
        ("Present, not written by this run", stale),
        ("Not present", absent),
    ]
    for heading, paths in groups:
        lines.append(f"## {heading}")
        lines.append("")
        lines.extend(f"- {path.name}" for path in paths)
        if not paths:
            lines.append("- (none)")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate policy/logic-policy.dl from controlled natural-language policy text.")
    parser.add_argument("--dry-run", action="store_true", help="render and validate, but do not write policy/logic-policy.dl")
    parser.add_argument("--check", action="store_true", help="verify policy/logic-policy.dl matches the generated output")
    args = parser.parse_args()
    if args.dry_run and args.check:
        raise SystemExit("--dry-run and --check cannot be used together")

    ensure_dirs()
    policy_text = read_required(SOURCE_MD)

    if args.check:
        draft = fixture_policy_json(policy_text)
        rules = normalized_rules(draft)
        program = compile_policy(rules)
        smoke_compile(program)
        if not OUTPUT_DL.is_file():
            raise SystemExit("missing policy/logic-policy.dl; run tools/generate_logic_policy.py")
        if OUTPUT_DL.read_text(encoding="utf-8") != program:
            raise SystemExit("policy/logic-policy.dl is stale; run tools/generate_logic_policy.py")
        print(f"checked: {OUTPUT_DL}")
        return 0

    # The marker is cleared on SUCCESS, at the end, not here. Clearing it up front would
    # drop the previous run's accounting for files this run may never touch: a run that
    # dies before PROMPT_OUT leaves runs/ byte-for-byte as it found it, so whatever the
    # marker said about those files is still true and deleting it would hide a mixture an
    # earlier run created. Absence therefore means "the last run that wrote anything into
    # runs/ finished". A --check run writes nothing under runs/ and touches neither.
    #
    # BaseException, not Exception: render_prompt (via read_required on the prompt
    # template) and fixture_policy_json both raise SystemExit from inside this try, and a
    # run cut short by one leaves the same half-written directory as any other failure.
    written: list[Path] = []
    try:
        prompt = render_prompt(policy_text)
        PROMPT_OUT.write_text(prompt + "\n", encoding="utf-8")
        written.append(PROMPT_OUT)

        # LLM draft step is Claude-native (see references/natural-language-to-policy.md).
        # Deterministic compile uses fixture_policy_json for local/non-LLM runs.
        draft = fixture_policy_json(policy_text)
        RESPONSE_OUT.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(RESPONSE_OUT)

        rules = normalized_rules(draft)
        program = compile_policy(rules)
        smoke_compile(program)

        write_trace(rules, OUTPUT_DL.relative_to(OUTPUT_DL.parents[1]).as_posix())
        written.append(TRACE_OUT)

        if not args.dry_run:
            tmp = OUTPUT_DL.with_suffix(".dl.tmp")
            tmp.write_text(program, encoding="utf-8")
            tmp.replace(OUTPUT_DL)
    except BaseException as exc:
        # Only a run that wrote something can have left a mixture; one that wrote nothing
        # leaves runs/ exactly as it found it, and a marker there would announce a
        # mismatch that does not exist — the very kind of audit surface #372 removes.
        if written:
            try:
                FAILED_OUT.write_text(failure_marker(exc, written), encoding="utf-8")
            except OSError:
                # A full or read-only disk must not rewrite the verdict: the caller needs
                # to see the gate that fired, not the failure to record it. The marker is
                # an extra record, so losing it is strictly better than replacing `exc`.
                pass
        # Re-raised unchanged, so the exit code and stderr a caller sees stay exactly
        # what they were.
        raise
    # Reported BEFORE the marker is cleared, because the files named here exist by now and
    # clearing can fail. Printing afterwards left the unlink-failure path with empty stdout
    # — rc=1 and not one line about the .dl it had just written, so an operator had only
    # the error sentence to go on.
    print(f"policy rules: {len(rules)}")
    print(f"written: {OUTPUT_DL}" if not args.dry_run else f"dry-run: {OUTPUT_DL} not changed")
    print(f"prompt: {PROMPT_OUT}")
    print(f"trace: {TRACE_OUT}")

    # Every artifact under runs/ now comes from this run, so the accounting is settled.
    # This one is NOT swallowed, unlike the write above, and the asymmetry is the point:
    # an unwritable marker only costs a record, while an undeletable one leaves a stale
    # marker calling current files leftovers — a false audit surface, which is the thing
    # #372 removes. So the run says it could not honour that invariant instead of exiting
    # 0 over a lie. Reproduced by placing a directory at the marker path.
    try:
        FAILED_OUT.unlink(missing_ok=True)
    except OSError as exc:
        raise FactlogError(
            f"generated {OUTPUT_DL.name} but could not clear {FAILED_OUT.name}: {exc}. "
            f"That file marks the previous run as failed, and leaving it would describe "
            f"this run's fresh runs/ files as another run's leftovers. Remove it by hand "
            f"and re-run."
        ) from exc
    return 0


if __name__ == "__main__":
    from common import run_cli

    sys.exit(run_cli(main))
