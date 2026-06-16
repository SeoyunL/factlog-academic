# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

try:
    import pyrewire
    from pyrewire import EasySession
except ImportError:  # pragma: no cover - exercised only on machines without pyrewire.
    pyrewire = None
    EasySession = None


ROOT = Path(os.environ.get("FACTLOG_ROOT", ".")).expanduser().resolve()
FACTS_DIR = ROOT / "facts"
DECISIONS_DIR = ROOT / "decisions"
RUNS_DIR = ROOT / "runs"
POLICY_DIR = ROOT / "policy"
PROMPTS_DIR = POLICY_DIR / "prompts"
CANDIDATES_CSV = FACTS_DIR / "candidates.csv"
ACCEPTED_DL = FACTS_DIR / "accepted.dl"
LOGIC_POLICY_DL = POLICY_DIR / "logic-policy.dl"
TEXT_TO_DATALOG_PROMPT = PROMPTS_DIR / "text_to_datalog.md"
QUESTIONS_MD = POLICY_DIR / "questions.md"

FACT_HEADER = ["subject", "relation", "object", "source", "status", "confidence", "note"]
ENGINE_STATUSES = {"confirmed", "accepted"}
REVIEW_STATUSES = {"needs_review", "candidate"}
QUERY_PREDICATES = {"relation", "path", "conflict", "review_required"}
RELATION_FACT_RE = re.compile(r"^relation\((.*)\)\.$")
MIN_PYREWIRE_VERSION = (1, 0, 1)


def version_tuple(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", value)
    return tuple(int(part) for part in parts[:3])


def require_pyrewire_version() -> None:
    if EasySession is None or pyrewire is None:
        sys.exit("pyrewire가 필요합니다. 예: pip install 'pyrewire>=1.0.1'")
    current = version_tuple(str(getattr(pyrewire, "__version__", "0")))
    if current < MIN_PYREWIRE_VERSION:
        sys.exit(
            "pyrewire 1.0.1 이상이 필요합니다. "
            f"현재 버전: {getattr(pyrewire, '__version__', 'unknown')}"
        )


def ensure_wiki_root() -> None:
    missing = [name for name in ["sources", "pages", "facts", "decisions", "policy"] if not (ROOT / name).exists()]
    if missing:
        sys.exit(f"not a factlog KB root: missing {', '.join(missing)}")


def ensure_dirs() -> None:
    ensure_wiki_root()
    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
    POLICY_DIR.mkdir(parents=True, exist_ok=True)
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        sys.exit(f"missing {path.relative_to(ROOT)}")
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_facts() -> list[dict[str, str]]:
    rows = read_csv(CANDIDATES_CSV)
    normalized: list[dict[str, str]] = []
    for row in rows:
        clean = {field: str(row.get(field, "")).strip() for field in FACT_HEADER}
        clean["confidence"] = normalize_confidence(clean["confidence"])
        normalized.append(clean)
    return normalized


def load_accepted_facts() -> list[dict[str, str]]:
    if not ACCEPTED_DL.is_file():
        sys.exit("missing facts/accepted.dl; run tools/compile_facts.py first")
    rows: list[dict[str, str]] = []
    for line in ACCEPTED_DL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            subject, relation, object_ = parse_relation_fact(line)
        except ValueError:
            sys.exit(f"accepted.dl contains unsupported fact syntax: {line}")
        rows.append({"subject": subject, "relation": relation, "object": object_})
    return rows


def load_logic_policy() -> str:
    if not LOGIC_POLICY_DL.is_file():
        sys.exit("missing policy/logic-policy.dl; run factlog init --target <kb> --force")
    return LOGIC_POLICY_DL.read_text(encoding="utf-8").strip()


def policy_predicates(policy_program: str | None = None) -> set[str]:
    text = policy_program if policy_program is not None else load_logic_policy()
    built_in = {"relation", "edge", "path"}
    return {
        name
        for name in re.findall(r"^\.decl\s+([A-Za-z_][A-Za-z0-9_]*)\(", text, flags=re.MULTILINE)
        if name not in built_in
    }


def load_questions() -> list[dict[str, str]]:
    if not QUESTIONS_MD.is_file():
        sys.exit("missing policy/questions.md; run factlog init --target <kb> --force")
    rows: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for lineno, line in enumerate(QUESTIONS_MD.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or not re.match(r"^(?:[-*]|\d+\.)", stripped):
            continue
        text = re.sub(r"^[-*]\s+", "", stripped)
        text = re.sub(r"^\d+\.\s+", "", text)
        if re.match(r"^\[[ xX]\]\s+", text):
            sys.exit(f"policy/questions.md line {lineno}: task-list checkboxes are not supported; use '- [q1] 질문' instead")
        match = re.match(r"^\[([A-Za-z0-9_-]+)\]\s*(.+)$", text)
        if match:
            question_id, question = match.groups()
        else:
            match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)\s*[:.)]\s*(.+)$", text)
            if match:
                question_id, question = match.groups()
            else:
                question_id, question = f"q{len(rows) + 1}", text
        question = question.strip()
        if question:
            question_id = question_id.strip()
            if question_id in seen_ids:
                sys.exit(f"policy/questions.md line {lineno}: duplicate question id {question_id!r}")
            seen_ids.add(question_id)
            rows.append({"id": question_id, "question": question})
    if not rows:
        sys.exit("policy/questions.md has no questions. Add lines such as '- [q1] Claude Code가 사용하는 것은 무엇인가?'")
    return rows


def engine_facts(facts: list[dict[str, str]]) -> list[dict[str, str]]:
    return [row for row in facts if row["status"] in ENGINE_STATUSES]


def review_facts(facts: list[dict[str, str]]) -> list[dict[str, str]]:
    return [row for row in facts if row["status"] in REVIEW_STATUSES]


def engine_input_rows(facts: list[dict[str, str]]) -> list[dict[str, str]]:
    if facts and "status" in facts[0]:
        return engine_facts(facts)
    return facts


def entity_set(facts: list[dict[str, str]] | None = None) -> set[str]:
    selected = engine_input_rows(facts if facts is not None else load_accepted_facts())
    return {value for row in selected for value in [row["subject"], row["object"]] if value}


def allowed_relations(facts: list[dict[str, str]] | None = None) -> set[str]:
    selected = facts if facts is not None else load_facts()
    return {row["relation"] for row in selected if row["relation"]}


def slugify(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z가-힣]+", "-", value.strip().lower())
    return text.strip("-") or "item"


def normalize_confidence(value: str) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "0.50"
    score = max(0.0, min(1.0, score))
    return f"{score:.2f}"


def dl_atom(row: dict[str, str]) -> str:
    return f"relation({dl_string(row['subject'])}, {dl_string(row['relation'])}, {dl_string(row['object'])})."


def dl_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def parse_relation_fact(line: str) -> tuple[str, str, str]:
    match = RELATION_FACT_RE.match(line)
    if not match:
        raise ValueError(line)
    try:
        value = json.loads(f"[{match.group(1)}]")
    except json.JSONDecodeError as exc:
        raise ValueError(line) from exc
    if not isinstance(value, list) or len(value) != 3 or not all(isinstance(item, str) for item in value):
        raise ValueError(line)
    return value[0], value[1], value[2]


def schema_context() -> str:
    accepted = load_accepted_facts()
    candidates = load_facts()
    entities = sorted(entity_set(accepted))
    relations = sorted(allowed_relations(accepted))
    return "\n".join(
        [
            "Allowed query predicates:",
            "- relation(subject, relation, object)?",
            "- path(start, target)?",
            *[f"- {predicate}(entity, reason)?" for predicate in sorted(policy_predicates())],
            '- review_required("원문 질문")?',
            "",
            "Generated policy schema:",
            load_logic_policy(),
            "",
            "Allowed relation names from facts/accepted.dl:",
            ", ".join(relations) or "(none)",
            "",
            "Review facts still outside engine input:",
            str(len(review_facts(candidates))),
            "",
            "Accepted entity names from this wiki:",
            ", ".join(entities) or "(none)",
            "",
            "Confirmed relation facts from this wiki:",
            *[
                f'- relation("{row["subject"]}", "{row["relation"]}", "{row["object"]}")'
                for row in accepted
            ],
        ]
    )


def build_text_to_datalog_prompt(question: str) -> str:
    if not TEXT_TO_DATALOG_PROMPT.is_file():
        sys.exit("missing policy/prompts/text_to_datalog.md; run factlog init --target <kb> --force")
    template = TEXT_TO_DATALOG_PROMPT.read_text(encoding="utf-8")
    bad = [name for name in ["{{SCHEMA_CONTEXT}}", "{{QUESTION}}"] if template.count(name) != 1]
    if bad:
        sys.exit(f"policy/prompts/text_to_datalog.md must contain placeholder(s) exactly once: {', '.join(bad)}")
    rendered = (
        template.replace("{{SCHEMA_CONTEXT}}", schema_context())
        .replace("{{QUESTION}}", question)
        .strip()
    )
    unresolved = sorted(set(re.findall(r"{{[^}]+}}", rendered)))
    if unresolved:
        sys.exit(f"policy/prompts/text_to_datalog.md contains unknown placeholder(s): {', '.join(unresolved)}")
    return rendered


def dependency_graph(facts: list[dict[str, str]]) -> dict[str, list[str]]:
    graph: dict[str, list[str]] = defaultdict(list)
    for row in engine_input_rows(facts):
        graph[row["subject"]].append(row["object"])
    return graph


def dependency_path(facts: list[dict[str, str]], start: str, target: str) -> list[str]:
    graph = dependency_graph(facts)
    queue: deque[tuple[str, list[str]]] = deque([(start, [start])])
    seen = {start}
    while queue:
        node, path = queue.popleft()
        if node == target:
            return path
        for nxt in graph.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, path + [nxt]))
    return []


def first_dependency_path(facts: list[dict[str, str]]) -> list[str]:
    entities = sorted({row["subject"] for row in facts})
    targets = sorted({row["object"] for row in facts})
    for start in entities:
        for target in targets:
            path = dependency_path(facts, start, target)
            if len(path) > 1:
                return path
    return []


WIRELOG_PROGRAM = """
.decl relation(subject: symbol, rel: symbol, object: symbol)
.decl edge(start: symbol, target: symbol)
.decl path(start: symbol, target: symbol)

edge(S, O) :- relation(S, R, O).
path(S, O) :- edge(S, O).
path(S, O) :- edge(S, M), path(M, O).
"""


def decode_wirelog_value(session: EasySession, value: object) -> object:
    """Resolve a wirelog integer ID back to its interned symbol string.

    Uses the private ``session._intern`` table exposed by pyrewire's EasySession.
    This is a private API (underscore-prefixed), intentionally pinned to
    ``pyrewire>=1.0.1,<2.0`` in pyproject.toml to guard against breakage if the
    internals change in a future major release.  The <2.0 upper bound in
    requirements.txt mirrors this constraint.

    Python 3.11+ is required (the engine dependency ``pyrewire`` needs 3.11+;
    see ``requires-python`` in pyproject.toml).  The ``X | Y`` unions and
    ``tuple[...]`` annotations used here need 3.10+, which the 3.11 floor
    satisfies.
    """
    if isinstance(value, int) and session._intern.contains_id(value):
        return session._intern.lookup(value)
    return value


def run_wirelog() -> dict[str, set[tuple[str, ...]]]:
    require_pyrewire_version()

    if not ACCEPTED_DL.is_file():
        sys.exit("missing facts/accepted.dl; run tools/compile_facts.py first")

    accepted_program = ACCEPTED_DL.read_text(encoding="utf-8")
    policy_program = load_logic_policy()
    session = EasySession(WIRELOG_PROGRAM + "\n" + policy_program + "\n" + accepted_program)
    for value in re.findall(r'"([^"]+)"', policy_program):
        session.intern(value)
    for row in load_accepted_facts():
        session.intern(row["subject"])
        session.intern(row["relation"])
        session.intern(row["object"])

    inferred: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    for relation_name, row, diff in session.step():
        if diff > 0:
            inferred[relation_name].add(tuple(str(decode_wirelog_value(session, value)) for value in row))
    session.close()
    return inferred


# ---------------------------------------------------------------------------
# validate_candidate_query — deterministic self-correction re-validation anchor
# Promoted from 04_self_correct.py so downstream LLM steps can call it
# without depending on the self-correct script directly (AC4).
# ---------------------------------------------------------------------------

def _query_args(line: str) -> list[str]:
    """Parse positional args from a Datalog query atom like pred(a, b, c)?."""
    match = re.match(r"^\w+\((.*)\)\?$", line.strip())
    if not match:
        return []
    args: list[str] = []
    current: list[str] = []
    in_string = False
    escaped = False
    for char in match.group(1):
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and in_string:
            current.append(char)
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
        if char == "," and not in_string:
            args.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    args.append("".join(current).strip())
    return args


def _arg_value(arg: str) -> str:
    if len(arg) >= 2 and arg[0] == '"' and arg[-1] == '"':
        return json.loads(arg)
    return arg


def _is_quoted_string(arg: str) -> bool:
    if len(arg) < 2 or arg[0] != '"' or arg[-1] != '"':
        return False
    try:
        return isinstance(json.loads(arg), str)
    except json.JSONDecodeError:
        return False


def _is_variable(arg: str) -> bool:
    return bool(re.fullmatch(r"[A-Z_][A-Za-z0-9_]*", arg))


def _is_valid_arg(arg: str) -> bool:
    return _is_variable(arg) or _is_quoted_string(arg)


def _quoted_constants(line: str) -> list[str]:
    return re.findall(r'"([^"]+)"', line)


def _relation_match_count(query: str, facts: list[dict[str, str]]) -> int:
    if query.startswith("relation"):
        args = _query_args(query)
        if len(args) != 3:
            return 0
        count = 0
        for row in facts:
            values = [row["subject"], row["relation"], row["object"]]
            if all(_is_variable(arg) or _arg_value(arg) == value for arg, value in zip(args, values, strict=True)):
                count += 1
        return count
    return 0


def validate_candidate_query(
    line: str,
    facts: list[dict[str, str]],
    policy_program: str | None = None,
) -> tuple[bool, str]:
    """Validate a single candidate Datalog query line against the current KB state.

    Returns (True, "passed") on success or (False, reason) on failure.
    This is the deterministic re-validation anchor used by the self-correction
    loop (AC4): after each LLM repair attempt the corrected query is run
    through this function before being accepted.

    ``policy_program`` lets callers supply the policy program text directly. When
    None (default) the compiled ``policy/logic-policy.dl`` is loaded, which
    requires that file to exist. Callers that must tolerate a KB without a
    compiled policy (e.g. interactive ask before ``/factlog check``) can pass the
    file's text if present or ``""`` if absent, so a missing policy yields an
    empty policy-predicate set instead of a hard exit.
    """
    query = line.strip()
    if "\n" in query or not query:
        return False, "candidate query must be a single non-empty line"
    if not query.endswith("?"):
        return False, "candidate query must end with ?"
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\(", query)
    if not match:
        return False, "candidate query must call a predicate"
    predicate = match.group(1)
    policy_query_predicates = policy_predicates(
        load_logic_policy() if policy_program is None else policy_program
    )
    allowed_predicates = {"relation", "path", "review_required"} | policy_query_predicates
    if predicate not in allowed_predicates:
        return False, f"unknown predicate: {predicate}"

    args = _query_args(query)
    entities = entity_set(facts)
    relations = allowed_relations(facts)
    if predicate == "review_required":
        if len(args) != 1 or len(_quoted_constants(query)) != 1:
            return False, "review_required must include the original question string"
        return True, "passed"
    if predicate == "relation":
        if len(args) != 3:
            return False, "relation query must have subject, relation, and object arguments"
        if not all(_is_valid_arg(arg) for arg in args):
            return False, "relation arguments must be variables or quoted strings"
        subject, relation, object_ = args
        if not _is_variable(subject) and _arg_value(subject) not in entities:
            return False, f"relation subject is not an accepted entity: {_arg_value(subject)}"
        if not _is_variable(relation) and _arg_value(relation) not in relations:
            return False, f"relation name is not accepted: {_arg_value(relation)}"
        if not _is_variable(object_) and _arg_value(object_) not in entities:
            return False, f"relation object is not an accepted entity: {_arg_value(object_)}"
        if _relation_match_count(query, facts) == 0:
            return False, "relation query does not match accepted facts"
        return True, "passed"
    if predicate == "path":
        if len(args) != 2:
            return False, "path query must have start and target arguments"
        if not all(_is_valid_arg(arg) for arg in args):
            return False, "path arguments must be variables or quoted strings"
        for arg in args:
            if not _is_variable(arg) and _arg_value(arg) not in entities:
                return False, f"path argument is not an accepted entity: {_arg_value(arg)}"
        if all(_is_quoted_string(arg) for arg in args) and not dependency_path(facts, _arg_value(args[0]), _arg_value(args[1])):
            return False, "path query does not match accepted facts"
        return True, "passed"
    if predicate in policy_query_predicates:
        if len(args) != 2:
            return False, "policy query must have entity and reason arguments"
        if not all(_is_valid_arg(arg) for arg in args):
            return False, "policy query arguments must be variables or quoted strings"
        if not _is_variable(args[0]) and _arg_value(args[0]) not in entities:
            return False, f"policy query entity is not accepted: {_arg_value(args[0])}"
        return True, "passed"
    return False, "unsupported query"
