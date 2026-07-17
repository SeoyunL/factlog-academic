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
# _load_logic_policy_from and finalize.py) share one parser and never drift (#190).


def fixture_policy_json(policy_text: str) -> dict[str, Any]:
    rules: list[dict[str, Any]] = []
    rejected: list[str] = []
    for lineno, reason, sentence in markdown_policy_items(policy_text):
        is_canonical, body_sentence = _strip_canonical_prefix(sentence, lineno)
        relations = logic_policy_md_relations(body_sentence)
        if not relations:
            rejected.append(f"line {lineno}: expected at least one backtick relation name")
            continue
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

    prompt = render_prompt(policy_text)
    PROMPT_OUT.write_text(prompt + "\n", encoding="utf-8")

    # LLM draft step is Claude-native (see references/natural-language-to-policy.md).
    # Deterministic compile uses fixture_policy_json for local/non-LLM runs.
    draft = fixture_policy_json(policy_text)
    RESPONSE_OUT.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    rules = normalized_rules(draft)
    program = compile_policy(rules)
    smoke_compile(program)

    write_trace(rules, OUTPUT_DL.relative_to(OUTPUT_DL.parents[1]).as_posix())

    if not args.dry_run:
        tmp = OUTPUT_DL.with_suffix(".dl.tmp")
        tmp.write_text(program, encoding="utf-8")
        tmp.replace(OUTPUT_DL)
    print(f"policy rules: {len(rules)}")
    print(f"written: {OUTPUT_DL}" if not args.dry_run else f"dry-run: {OUTPUT_DL} not changed")
    print(f"prompt: {PROMPT_OUT}")
    print(f"trace: {TRACE_OUT}")
    return 0


if __name__ == "__main__":
    from common import run_cli

    sys.exit(run_cli(main))
