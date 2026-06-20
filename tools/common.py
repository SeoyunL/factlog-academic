# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import csv
import decimal
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import literal_types

try:
    import pyrewire
    from pyrewire import EasySession
except ImportError:  # pragma: no cover - exercised only on machines without pyrewire.
    pyrewire = None
    EasySession = None


def enable_utf8_stdio() -> None:
    """Force stdout/stderr to UTF-8 on Windows so non-ASCII console output
    (e.g. Korean entity/relation names) is not mangled by the legacy code page
    (cp949). Files are always written with explicit ``encoding="utf-8"``; this
    only fixes what gets printed to the terminal.

    No-op on non-Windows platforms, where stdio is already UTF-8. Idempotent and
    safe to call repeatedly; tolerates streams that do not support reconfigure
    (e.g. pytest capture, redirected pipes).
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):  # pragma: no cover - stream already closed/detached
            pass


# Applied at import so every tool that imports common gets correct Windows
# console output without an explicit call.
enable_utf8_stdio()


class FactlogError(Exception):
    """A recoverable factlog error (missing input, malformed policy, ...).

    Library functions in this module raise it instead of calling ``sys.exit`` so
    an in-process caller (e.g. the CLI or ask_router) can catch and handle the
    condition rather than having the interpreter killed underneath it. Tool entry
    points wrap their ``main`` in :func:`run_cli`, which restores the legacy
    behaviour of printing the message to stderr and exiting with status 1.
    """


def run_cli(main_func) -> int:
    """Invoke a tool ``main()`` translating a :class:`FactlogError` into the
    legacy "print message to stderr, exit 1" behaviour that ``raise FactlogError(str)``
    used to provide. Returns the main's exit code (None -> 0)."""
    try:
        return main_func() or 0
    except FactlogError as exc:
        print(str(exc), file=sys.stderr)
        return 1


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
# A row a human (or a resolution step) has marked as replaced by a newer fact.
# Superseded rows are retained in candidates.csv for audit but are NOT engine
# input (they never reach accepted.dl) and are ignored by conflict detection.
SUPERSEDED_STATUSES = {"superseded"}
QUERY_PREDICATES = {"relation", "path", "count", "conflict", "review_required"}
RELATION_FACT_RE = re.compile(r"^relation\((.*)\)\.$")
MIN_PYREWIRE_VERSION = (1, 0, 1)


@dataclass(frozen=True)
class KbContext:
    """Resolved KB paths for one explicit root, with loaders bound to them.

    The module-level path globals (ROOT/FACTS_DIR/CANDIDATES_CSV/...) stay the
    default surface for the ambient ``FACTLOG_ROOT`` and every existing caller.
    KbContext lets an in-process caller (notably ``factlog.cli``) read a *different*
    KB without mutating ``FACTLOG_ROOT`` and ``importlib.reload``-ing this module.
    Its loader methods share the exact parsing of the module-level functions via
    the ``_*_from(path)`` helpers, so the two can never drift.
    """

    root: Path
    facts_dir: Path
    decisions_dir: Path
    runs_dir: Path
    policy_dir: Path
    prompts_dir: Path
    candidates_csv: Path
    accepted_dl: Path
    logic_policy_dl: Path
    questions_md: Path

    @classmethod
    def for_root(cls, root) -> KbContext:
        root = Path(root).expanduser().resolve()
        facts = root / "facts"
        policy = root / "policy"
        return cls(
            root=root,
            facts_dir=facts,
            decisions_dir=root / "decisions",
            runs_dir=root / "runs",
            policy_dir=policy,
            prompts_dir=policy / "prompts",
            candidates_csv=facts / "candidates.csv",
            accepted_dl=facts / "accepted.dl",
            logic_policy_dl=policy / "logic-policy.dl",
            questions_md=policy / "questions.md",
        )

    def load_facts(self) -> list[dict[str, str]]:
        return _load_facts_from(self.candidates_csv)

    def load_accepted_facts(self) -> list[dict[str, str]]:
        return _load_accepted_facts_from(self.accepted_dl)

    def load_logic_policy(self) -> str:
        return _load_logic_policy_from(self.logic_policy_dl)

    def single_valued_relations(self) -> set[str]:
        return _relation_names_from(self.policy_dir / "single-valued.md")

    def attribute_relations(self) -> set[str]:
        return _relation_names_from(self.policy_dir / "attribute-relations.md")

    def typed_relations(self) -> dict[str, TypedRelSpec]:
        path = self.policy_dir / "typed-relations.md"
        if not path.is_file():
            return {}
        reserved = _typed_reserved_names(
            relations=_try(lambda: allowed_relations(self.load_facts())),
            predicates=_try(lambda: policy_predicates(self.load_logic_policy())),
        )
        specs = _parse_typed_relations(path.read_text(encoding="utf-8"), reserved)
        _warn_typed_not_attribute(specs, self.attribute_relations())
        return specs


def version_tuple(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", value)
    return tuple(int(part) for part in parts[:3])


def require_pyrewire_version() -> None:
    if EasySession is None or pyrewire is None:
        raise FactlogError("pyrewire가 필요합니다. 예: pip install 'pyrewire>=1.0.1'")
    current = version_tuple(str(getattr(pyrewire, "__version__", "0")))
    if current < MIN_PYREWIRE_VERSION:
        raise FactlogError(
            "pyrewire 1.0.1 이상이 필요합니다. "
            f"현재 버전: {getattr(pyrewire, '__version__', 'unknown')}"
        )


def ensure_wiki_root() -> None:
    missing = [name for name in ["sources", "pages", "facts", "decisions", "policy"] if not (ROOT / name).exists()]
    if missing:
        raise FactlogError(f"not a factlog KB root: missing {', '.join(missing)}")


def ensure_dirs() -> None:
    ensure_wiki_root()
    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
    POLICY_DIR.mkdir(parents=True, exist_ok=True)
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        # Show the path relative to the ambient ROOT when it lives under it;
        # a KbContext may point read_csv at a different root, so fall back to the
        # full path rather than letting relative_to raise.
        try:
            shown: Path = path.relative_to(ROOT)
        except ValueError:
            shown = path
        raise FactlogError(f"missing {shown}")
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# --- Source file discovery (shared by merge_candidates / coverage) -----------
SOURCE_ROOTS = ("sources", "runs/sources")


def source_rel_key(ref: str) -> str:
    """A source ref minus its source-root prefix and file suffix.

    This is the key that pairs a binary original with its runs/sources/<rel>
    conversion now that `factlog ingest` mirrors the original's subdirectory:
        'sources/a/report.pdf'      -> 'a/report'
        'runs/sources/a/report.md'  -> 'a/report'   (pairs with the line above)
        'sources/report.pdf'        -> 'report'      (top-level: unchanged)
    Subdirectory-aware, so same-stem files in different subtrees no longer
    collide. NFC-normalised. (Uses PurePosixPath since refs are posix-style.)
    """
    ref = unicodedata.normalize("NFC", ref)
    for rootname in SOURCE_ROOTS:
        prefix = rootname + "/"
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
            break
    return PurePosixPath(ref).with_suffix("").as_posix()


def source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for rel in SOURCE_ROOTS:
        base = root / rel
        if base.is_dir():
            files.extend(path for path in base.rglob("*") if path.is_file())
    return sorted(files)


def source_file_refs(root: Path) -> set[str]:
    """Source paths relative to the KB root (sources/- or runs/sources/-prefixed).

    Example: <root>/sources/my-doc.md -> 'sources/my-doc.md';
             <root>/runs/sources/report.md -> 'runs/sources/report.md'.
    These match the canonical source value that candidate rows must use.

    Paths are NFC-normalised: macOS stores filenames as NFD (decomposed), but
    extracted candidate sources are typically NFC, so an un-normalised compare
    would silently drop facts for Korean (or any decomposable) filenames.
    """
    return {
        unicodedata.normalize("NFC", path.relative_to(root).as_posix())
        for path in source_files(root)
    }


def is_text_source(path: Path, *, sniff: int = 8192) -> bool:
    """Return True iff *path*'s leading bytes look like readable UTF-8 text.

    The in-session fact extraction reads each sources/ file as text, so a file is
    only ingestible if it decodes as text. A file is treated as non-text when its
    first *sniff* bytes contain a NUL byte or do not decode as UTF-8. A multi-byte
    UTF-8 sequence truncated at the sniff boundary is tolerated *only* when the
    file actually extends past the boundary; for a fully-read short file an
    invalid trailing byte means binary. Detection is content-based, so binary
    formats (.docx, .pdf, images, ...) are flagged regardless of their extension.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    chunk = raw[:sniff]
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError as exc:
        return len(raw) > sniff and exc.start >= len(chunk) - 3
    return True


# load_facts / load_accepted_facts / load_logic_policy delegate to path-taking
# _*_from helpers so the module-level (ambient-root) functions and KbContext's
# methods parse identically. The module functions are unchanged for callers.
def _load_facts_from(candidates_csv: Path) -> list[dict[str, str]]:
    rows = read_csv(candidates_csv)
    normalized: list[dict[str, str]] = []
    for row in rows:
        clean = {field: str(row.get(field, "")).strip() for field in FACT_HEADER}
        clean["confidence"] = normalize_confidence(clean["confidence"])
        normalized.append(clean)
    return normalized


def load_facts() -> list[dict[str, str]]:
    return _load_facts_from(CANDIDATES_CSV)


def _load_accepted_facts_from(accepted_dl: Path) -> list[dict[str, str]]:
    if not accepted_dl.is_file():
        raise FactlogError("missing facts/accepted.dl; run tools/compile_facts.py first")
    rows: list[dict[str, str]] = []
    for line in accepted_dl.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            subject, relation, object_ = parse_relation_fact(line)
        except ValueError:
            raise FactlogError(f"accepted.dl contains unsupported fact syntax: {line}")
        rows.append({"subject": subject, "relation": relation, "object": object_})
    return rows


def load_accepted_facts() -> list[dict[str, str]]:
    return _load_accepted_facts_from(ACCEPTED_DL)


def _load_logic_policy_from(logic_policy_dl: Path) -> str:
    if not logic_policy_dl.is_file():
        raise FactlogError("missing policy/logic-policy.dl; run factlog init --target <kb> --force")
    text = logic_policy_dl.read_text(encoding="utf-8").strip()
    # Optional sibling for hand-authored rules (e.g. typed comparison predicates,
    # #120). Unlike logic-policy.dl this file is never regenerated or byte-compared
    # by generate_logic_policy.py --check, so authors may edit it directly. Absent
    # or all-comment/empty → text is byte-identical to today (#116 invariant 1).
    extra = logic_policy_dl.with_name("logic-policy.extra.dl")
    if extra.is_file():
        extra_text = extra.read_text(encoding="utf-8").strip()
        # Skip an empty or comment-only sibling so the program text stays
        # byte-identical to today. Both `//` (Datalog) and `#` (used in every
        # other policy file) are treated as comments; a `#`-only stub must NOT
        # leak bytes into the engine program — wirelog rejects `#` with a
        # ParseError.
        if extra_text and any(
            line.strip()
            and not line.strip().startswith("//")
            and not line.strip().startswith("#")
            for line in extra_text.splitlines()
        ):
            text = text + "\n" + extra_text
    return text


def load_logic_policy() -> str:
    return _load_logic_policy_from(LOGIC_POLICY_DL)


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
        raise FactlogError("missing policy/questions.md; run factlog init --target <kb> --force")
    rows: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for lineno, line in enumerate(QUESTIONS_MD.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or not re.match(r"^(?:[-*]|\d+\.)", stripped):
            continue
        text = re.sub(r"^[-*]\s+", "", stripped)
        text = re.sub(r"^\d+\.\s+", "", text)
        if re.match(r"^\[[ xX]\]\s+", text):
            raise FactlogError(f"policy/questions.md line {lineno}: task-list checkboxes are not supported; use '- [q1] 질문' instead")
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
                raise FactlogError(f"policy/questions.md line {lineno}: duplicate question id {question_id!r}")
            seen_ids.add(question_id)
            rows.append({"id": question_id, "question": question})
    if not rows:
        raise FactlogError("policy/questions.md has no questions. Add lines such as '- [q1] Claude Code가 사용하는 것은 무엇인가?'")
    return rows


def _relation_names_from(path: Path) -> set[str]:
    """Parse a policy file that lists relation names, one per line.

    Bullets and '#' comments are allowed; the relation name is the first
    `backtick`-quoted token if present, else the first whitespace token (quote a
    name that contains spaces). Absent file → empty set."""
    if not path.is_file():
        return set()
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = re.sub(r"^\s*[-*]\s+", "", line.strip()).strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.search(r"`([^`]+)`", stripped)
        name = match.group(1).strip() if match else stripped.split()[0]
        if name:
            names.add(name)
    return names


def sync_ignore_patterns(root: Path | None = None) -> list[str]:
    """Glob patterns from policy/sync-ignore.md naming sources to skip on sync.

    One pattern per line; '#' comments and '-' bullets are allowed; wrap a
    pattern that contains spaces in `backticks`. (A '*' is NOT treated as a
    bullet, so a bare `*.md` glob survives.) Order-preserving and de-duplicated.
    *root* selects the KB (its policy/ dir); None uses the module ROOT. Absent
    file -> no patterns (every source is synced).
    """
    base = (root / "policy") if root is not None else POLICY_DIR
    path = base / "sync-ignore.md"
    if not path.is_file():
        return []
    patterns: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = re.sub(r"^\s*-\s+", "", line.strip()).strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.fullmatch(r"`([^`]+)`", stripped)
        pat = unicodedata.normalize("NFC", (m.group(1) if m else stripped).strip())
        if pat and pat not in seen:
            seen.add(pat)
            patterns.append(pat)
    return patterns


def _glob_to_regex(pattern: str) -> str:
    """Translate a path glob to a regex where `*`/`?` stay within a path segment.

    Unlike fnmatch (whose `*` crosses `/`), here:
      - `*`  matches any run of non-`/` characters (one path segment),
      - `?`  matches a single non-`/` character,
      - `**` matches across segments (`**/` = zero-or-more directories),
      - a trailing `/` is shorthand for `/**` (the whole subtree).
    So `drafts/*.md` matches `drafts/x.md` but NOT `drafts/sub/x.md`, while
    `drafts/**` (or `drafts/`) matches everything under `drafts/`.
    """
    if pattern.endswith("/"):
        pattern += "**"
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 2] == "**":
                i += 2
                if pattern[i:i + 1] == "/":
                    out.append("(?:.*/)?")  # '**/' — zero or more directories
                    i += 1
                else:
                    out.append(".*")        # '**' — anything, crossing '/'
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "(?s:" + "".join(out) + r")\Z"


def is_sync_ignored(ref: str, patterns: list[str]) -> bool:
    """True if a source ref matches any sync-ignore glob.

    *ref* is a source path relative to the KB root (sources/- or
    runs/sources/-prefixed). A pattern matches the full ref OR the ref's path
    within its source root, so `drafts/*.md` matches `sources/drafts/x.md` and
    `sources/wip.md` matches itself. Matching is case-sensitive; both sides are
    NFC-normalised. Glob semantics: see _glob_to_regex (`*` does not cross `/`).
    """
    if not patterns:
        return False
    ref = unicodedata.normalize("NFC", ref)
    candidates = [ref]
    for rootname in SOURCE_ROOTS:
        prefix = rootname + "/"
        if ref.startswith(prefix):
            candidates.append(ref[len(prefix):])
            break
    return any(
        re.match(_glob_to_regex(pat), c) is not None
        for pat in patterns
        for c in candidates
    )


def single_valued_relations() -> set[str]:
    """Relation names declared single-valued (functional) in policy/single-valued.md.

    Such a relation may hold at most one object per subject; two distinct objects
    are a contradiction (see tools/check_conflicts.py). Absent file → no
    single-valued relations → no conflicts.
    """
    return _relation_names_from(POLICY_DIR / "single-valued.md")


def attribute_relations() -> set[str]:
    """Relation names whose object is a LITERAL value, not a first-class entity
    (policy/attribute-relations.md).

    Objects of these relations (dates, numbers, ordinals, ...) are excluded from
    entity_set so they do not pollute the entity vocabulary (entity listings,
    path nodes, count subjects). They remain valid relation-query objects — see
    value_set and classify_query — so a fact about a literal is still verifiable.
    Same file format as single-valued.md; absent file → no attribute relations
    → entity_set == value_set (fully backward compatible).
    """
    return _relation_names_from(POLICY_DIR / "attribute-relations.md")


# --- typed relations (policy/typed-relations.md) -----------------------------
# Declares which relations carry a typed literal object (date/number/ordinal),
# and the ASCII alias of the engine side-relation that holds the comparable
# value. The alias is author-chosen (not derived from the relation name) so it is
# guaranteed to be a legal, stable engine identifier even when the relation name
# is non-ASCII. The flat triple stays canonical; this only declares typing.

@dataclass(frozen=True)
class TypedRelSpec:
    type: str   # one of literal_types.TYPES
    alias: str  # ASCII identifier naming the engine side-relation
    # Inline unit table for an `amount` relation, e.g. {"억": 10**8, "원": 1}.
    # None for non-amount types, and for an amount line with no inline clause
    # (the projection then resolves to literal_types.DEFAULT_AMOUNT_UNITS).
    units: dict[str, int] | None = None


_ASCII_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# `name` : type  as  alias  (units)?  — name optionally backtick-quoted (may
# contain spaces); an optional trailing `(...)` unit clause is valid ONLY on an
# `amount` line (enforced in _parse_typed_relations). Lines with no clause parse
# byte-identically to before and yield units=None.
_TYPED_REL_RE = re.compile(
    r"^(?:`(?P<qname>[^`]+)`|(?P<name>\S+))\s*:\s*(?P<type>\w+)\s+as\s+(?P<alias>\S+)"
    r"(?:\s*\((?P<units>[^)]*)\))?\s*$"
)
_TYPED_RESERVED = {"relation", "edge", "path"}  # built-in engine predicates


def _try(fn):
    """Best-effort: return fn()'s result, or an empty set if it raises a
    FactlogError (e.g. a fresh KB with no candidates.csv / logic-policy.dl)."""
    try:
        return fn()
    except FactlogError:
        return set()


def _typed_reserved_names(relations: set[str], predicates: set[str]) -> set[str]:
    return _TYPED_RESERVED | set(relations) | set(predicates)


def _parse_amount_units(body: str) -> dict[str, int]:
    """Parse an inline `amount` unit clause body, e.g. ``억=1e8, 만=1e4, 원=1``.

    Comma-separated ``unit=number`` pairs; the value may be written ``1e8`` or
    ``100000000`` but MUST resolve to a **positive integer** (the engine projects
    amounts into an int64 column). A non-positive / non-integer / non-numeric
    value, or a malformed pair, → FactlogError (fail loudly)."""
    units: dict[str, int] = {}
    for pair in body.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise FactlogError(f"typed-relations: malformed unit pair {pair!r} (expected unit=number)")
        unit, _, value = pair.partition("=")
        unit = unit.strip()
        value = value.strip()
        if not unit:
            raise FactlogError(f"typed-relations: empty unit name in {pair!r}")
        try:
            num = decimal.Decimal(value)
        except decimal.InvalidOperation as exc:
            raise FactlogError(f"typed-relations: non-numeric unit value {value!r} for {unit!r}") from exc
        if num != num.to_integral_value() or num <= 0:
            raise FactlogError(f"typed-relations: unit value for {unit!r} must be a positive integer, got {value!r}")
        units[unit] = int(num)
    return units


def _parse_typed_relations(text: str, reserved: frozenset[str] | set[str] = frozenset()) -> dict[str, TypedRelSpec]:
    """Pure parser for typed-relations.md. *reserved* is the set of names the
    alias must not collide with (built-ins + existing relations/predicates).

    - relation names are NFC-normalised;
    - an unknown type tag → warning + the line is skipped (loaded untyped);
    - a malformed line → warning + skipped;
    - a non-ASCII-identifier alias, an alias colliding with a reserved/existing
      name, or a duplicate alias within the file → FactlogError (fail loudly).
    """
    specs: dict[str, TypedRelSpec] = {}
    seen_alias: dict[str, str] = {}
    for line in text.splitlines():
        stripped = re.sub(r"^\s*[-*]\s+", "", line.strip()).strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = re.sub(r"\s*#.*$", "", stripped).strip()  # drop a trailing inline comment
        if not stripped:
            continue
        m = _TYPED_REL_RE.match(stripped)
        if not m:
            print(f"typed-relations: skipping malformed line: {stripped!r}", file=sys.stderr)
            continue
        name = unicodedata.normalize("NFC", (m.group("qname") or m.group("name")).strip())
        type_tag = m.group("type")
        alias = m.group("alias")
        units_body = m.group("units")  # None if no clause, "" if empty `()`
        if type_tag not in literal_types.TYPES:
            print(f"typed-relations: unknown type {type_tag!r} for {name!r}; skipping", file=sys.stderr)
            continue
        # A units clause is valid ONLY on an amount line (fail loudly otherwise).
        if units_body is not None and type_tag != "amount":
            raise FactlogError(f"typed-relations: a units clause is only valid on an amount line, not {type_tag!r} ({name!r})")
        units = _parse_amount_units(units_body) if (type_tag == "amount" and units_body is not None) else None
        if not _ASCII_IDENT_RE.match(alias):
            raise FactlogError(f"typed-relations: alias must be an ASCII identifier: {alias!r}")
        if alias in _TYPED_RESERVED or alias in reserved:
            raise FactlogError(f"typed-relations: alias {alias!r} collides with a reserved or existing name")
        if alias in seen_alias:
            raise FactlogError(f"typed-relations: duplicate alias {alias!r} ({seen_alias[alias]} and {name})")
        seen_alias[alias] = name
        specs[name] = TypedRelSpec(type=type_tag, alias=alias, units=units)
    return specs


def _warn_typed_not_attribute(specs: dict[str, TypedRelSpec], attrs: set[str]) -> None:
    attrs_nfc = {unicodedata.normalize("NFC", a) for a in attrs}
    for name in specs:
        if name not in attrs_nfc:
            print(
                f"typed-relations: {name!r} is typed but not declared in attribute-relations.md "
                "(its object should be a literal, not an entity)",
                file=sys.stderr,
            )


def typed_relations() -> dict[str, TypedRelSpec]:
    """Relations declared typed in policy/typed-relations.md → {name: TypedRelSpec}.

    Absent (or all-comment) file → empty mapping (no typed relations; behaviour
    is byte-identical to a KB without the feature). See KbContext.typed_relations
    for the per-KB variant.
    """
    path = POLICY_DIR / "typed-relations.md"
    if not path.is_file():
        return {}
    reserved = _typed_reserved_names(
        relations=_try(allowed_relations),
        predicates=_try(policy_predicates),
    )
    specs = _parse_typed_relations(path.read_text(encoding="utf-8"), reserved)
    _warn_typed_not_attribute(specs, attribute_relations())
    return specs


# Per-type engine column for a projectable typed side-relation. `number` is
# intentionally absent: this pyrewire build's .dl TEXT parser accepts only
# int32|int64|string|symbol scalar columns — there is NO float text column, and
# one unparseable .decl rejects the whole program. So `number` cannot be
# projected yet (it is warned declared-but-not-supported and skipped); follow-up
# #125 handles it. `date`/`ordinal` normalize to sortable ints -> int64.
_TYPED_COL = {"date": "int64", "ordinal": "int64"}


def _typed_decls(specs: dict[str, TypedRelSpec]) -> str:
    """`.decl <alias>(subject: symbol, v: <col>)` lines for every projectable
    typed relation (type in _TYPED_COL), sorted by alias for determinism.

    Returns "" when none, so appending to the program text is byte-identical to
    today whenever there are no projectable typed relations (#116 invariant 1)."""
    lines = sorted(
        f".decl {spec.alias}(subject: symbol, v: {_TYPED_COL[spec.type]})"
        for spec in specs.values()
        if spec.type in _TYPED_COL
    )
    return ("\n" + "\n".join(lines) + "\n") if lines else ""


def _assert_no_alias_collision(specs: dict[str, TypedRelSpec], program_text: str) -> None:
    """Raise FactlogError if a projectable alias duplicates a `.decl <name>(`
    already present in the assembled program.

    The engine silently accepts a duplicate .decl, and #118's parse-time check
    uses a best-effort reserved set, so re-check here against the real, fully
    assembled program (WIRELOG_PROGRAM + policy + accepted)."""
    declared = set(re.findall(r"^\.decl\s+([A-Za-z_][A-Za-z0-9_]*)\(", program_text, flags=re.MULTILINE))
    for spec in specs.values():
        if spec.type in _TYPED_COL and spec.alias in declared:
            raise FactlogError(
                f"typed-relations: alias {spec.alias!r} collides with a .decl already in the program"
            )


def corroboration_counts(facts: list[dict[str, str]]) -> dict[tuple[str, str, str], int]:
    """Map each engine-input fact (subject, relation, object) to the number of
    DISTINCT sources backing it. A fact corroborated by several independent
    sources is more trustworthy — a signal a plain notes wiki cannot give."""
    sources: dict[tuple[str, str, str], set[str]] = {}
    for row in engine_facts(facts):
        key = (row["subject"], row["relation"], row["object"])
        sources.setdefault(key, set()).add(row["source"])
    return {key: len(srcs) for key, srcs in sources.items()}


def fact_signals(
    facts: list[dict[str, str]],
    root: Path | None = None,
) -> dict[tuple[str, str, str], dict[str, object]]:
    """Per engine fact (subject, relation, object), the answer-quality signals:
    distinct ``sources`` count, max ``confidence``, and ``stale`` (True if any
    backing source file no longer exists under the KB — the fact rests on a
    vanished/changed source and should be re-verified)."""
    base = ROOT if root is None else Path(root)
    acc: dict[tuple[str, str, str], dict[str, object]] = {}
    for row in engine_facts(facts):
        key = (row["subject"], row["relation"], row["object"])
        entry = acc.setdefault(key, {"sources": set(), "confidence": 0.0, "stale": False})
        entry["sources"].add(row["source"])
        try:
            entry["confidence"] = max(float(entry["confidence"]), float(row["confidence"]))
        except (TypeError, ValueError):
            pass
        source_file = row["source"].partition("#")[0]
        if source_file and not (base / source_file).is_file():
            entry["stale"] = True
    return {
        key: {
            "sources": len(entry["sources"]),
            "source_paths": sorted(entry["sources"]),
            "confidence": f"{float(entry['confidence']):.2f}",
            "stale": entry["stale"],
        }
        for key, entry in acc.items()
    }


def engine_facts(facts: list[dict[str, str]]) -> list[dict[str, str]]:
    return [row for row in facts if row["status"] in ENGINE_STATUSES]


def review_facts(facts: list[dict[str, str]]) -> list[dict[str, str]]:
    return [row for row in facts if row["status"] in REVIEW_STATUSES]


def engine_input_rows(facts: list[dict[str, str]]) -> list[dict[str, str]]:
    if facts and "status" in facts[0]:
        return engine_facts(facts)
    return facts


def value_set(facts: list[dict[str, str]] | None = None) -> set[str]:
    """Every accepted subject/object — the full validatable vocabulary, INCLUDING
    literal values (dates, numbers, ...). Use this to validate a relation query's
    object so a fact about a literal stays verifiable."""
    selected = engine_input_rows(facts if facts is not None else load_accepted_facts())
    return {value for row in selected for value in [row["subject"], row["object"]] if value}


def entity_set(
    facts: list[dict[str, str]] | None = None,
    attribute_rels: set[str] | None = None,
) -> set[str]:
    """First-class entities only: every subject, plus objects whose relation is
    NOT declared an attribute relation. Objects of attribute relations are
    literal values (see attribute_relations) and are excluded so they don't show
    up as entities (entity listings, path nodes, count subjects). With no
    policy/attribute-relations.md this equals value_set (backward compatible).

    *attribute_rels* overrides which relations count as attribute (literal-valued)
    relations; pass a KbContext's attribute_relations() to read a non-default KB.
    None falls back to the module-level (ambient-root) attribute_relations()."""
    selected = engine_input_rows(facts if facts is not None else load_accepted_facts())
    literal_rels = attribute_relations() if attribute_rels is None else attribute_rels
    entities: set[str] = set()
    for row in selected:
        if row["subject"]:
            entities.add(row["subject"])
        if row["object"] and row["relation"] not in literal_rels:
            entities.add(row["object"])
    return entities


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
        raise FactlogError("missing policy/prompts/text_to_datalog.md; run factlog init --target <kb> --force")
    template = TEXT_TO_DATALOG_PROMPT.read_text(encoding="utf-8")
    bad = [name for name in ["{{SCHEMA_CONTEXT}}", "{{QUESTION}}"] if template.count(name) != 1]
    if bad:
        raise FactlogError(f"policy/prompts/text_to_datalog.md must contain placeholder(s) exactly once: {', '.join(bad)}")
    rendered = (
        template.replace("{{SCHEMA_CONTEXT}}", schema_context())
        .replace("{{QUESTION}}", question)
        .strip()
    )
    unresolved = sorted(set(re.findall(r"{{[^}]+}}", rendered)))
    if unresolved:
        raise FactlogError(f"policy/prompts/text_to_datalog.md contains unknown placeholder(s): {', '.join(unresolved)}")
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
        raise FactlogError("missing facts/accepted.dl; run tools/compile_facts.py first")

    accepted_program = ACCEPTED_DL.read_text(encoding="utf-8")
    policy_program = load_logic_policy()
    specs = typed_relations()
    base_program = WIRELOG_PROGRAM + "\n" + policy_program + "\n" + accepted_program
    if specs:
        _assert_no_alias_collision(specs, base_program)
        for name, spec in specs.items():
            if spec.type not in _TYPED_COL:
                print(
                    f"typed-relations: {name!r} declared {spec.type!r}, which is not yet "
                    "projectable into the engine; skipping (see #125)",
                    file=sys.stderr,
                )
    # _typed_decls(specs) is "" when there is nothing projectable, so the program
    # text is byte-identical to today for a KB with no typed-relations (#116 inv.1).
    session = EasySession(base_program + _typed_decls(specs))
    for value in re.findall(r'"([^"]+)"', policy_program):
        session.intern(value)
    accepted = load_accepted_facts()
    for row in accepted:
        session.intern(row["subject"])
        session.intern(row["relation"])
        session.intern(row["object"])

    # Project typed rows into their side-relations, deterministically ordered so
    # the run is reproducible (#116 invariant 3). A non-parsing object warns and
    # skips ONLY that row — the fact still loads untyped via relation/3 (#116
    # invariant 4). Scalars are bare ints and must NEVER be interned.
    #
    # NB: hand-authored comparison-predicate rules (#120) use arity-2
    # (subject, reason) heads with a quoted reason string; the scalar stays in
    # the body. A bare scalar in a head would be mis-decoded as an interned
    # symbol by decode_wirelog_value (it round-trips ints through the intern
    # table), so it must never appear there. Those rules live in the optional
    # policy/logic-policy.extra.dl, not here.
    if specs:
        for row in sorted(accepted, key=lambda r: (r["relation"], r["subject"], r["object"])):
            spec = specs.get(row["relation"])
            if spec is None or spec.type not in _TYPED_COL:
                continue
            scalar = literal_types.normalize(spec.type, row["object"])
            if scalar is None:
                print(
                    f"typed-relations: {row['object']!r} for {row['relation']!r} "
                    f"({row['subject']!r}) does not parse as {spec.type}; loading untyped",
                    file=sys.stderr,
                )
                continue
            session.insert(spec.alias, (session.intern(row["subject"]), scalar))

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


# Public query-parsing API -----------------------------------------------------
# These are the stable, documented names external callers should use to parse a
# Datalog query atom (ask_router and run_logic_check both depend on them, so they
# are de-facto public). The underscore-prefixed originals above remain as internal
# aliases used within this module; prefer the public names from other modules.
#   query_args(line)       -> positional args, string-aware (commas inside quotes)
#   arg_value(arg)         -> a quoted literal's value (JSON-decoded) or the bare arg
#   is_quoted_string(arg)  -> True if arg is a quoted string literal
#   is_variable(arg)       -> True if arg is a Datalog variable (capitalised)
#   quoted_constants(line) -> every "..." literal in a line
query_args = _query_args
arg_value = _arg_value
is_quoted_string = _is_quoted_string
is_variable = _is_variable
quoted_constants = _quoted_constants


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


# Stable structured outcome codes for query classification. Callers (e.g. the
# ask router) route on these codes, NOT on the human-readable reason text, so a
# reworded message — or an entity/relation constant that happens to contain a
# reason phrase — can never change routing.
QUERY_OK = "ok"
QUERY_REVIEW_REQUIRED = "review_required"
QUERY_FACT_ABSENT = "fact_absent"  # accepted vocabulary, but fact/path absent
QUERY_MALFORMED = "malformed"
QUERY_UNKNOWN_PREDICATE = "unknown_predicate"
QUERY_BAD_ARITY = "bad_arity"
QUERY_ENTITY_NOT_ACCEPTED = "entity_not_accepted"
QUERY_RELATION_NOT_ACCEPTED = "relation_not_accepted"
QUERY_UNSUPPORTED = "unsupported"


def classify_query(
    line: str,
    facts: list[dict[str, str]],
    policy_program: str | None = None,
) -> tuple[bool, str, str]:
    """Classify a candidate Datalog query line, returning (ok, code, reason).

    ``code`` is one of the stable ``QUERY_*`` constants — the machine-readable
    classification callers should branch on. ``reason`` is the human-readable
    explanation (display only). ``ok`` is True only for a query that resolves
    against accepted facts (or a well-formed ``review_required``).

    ``policy_program`` — see ``validate_candidate_query``.
    """
    query = line.strip()
    if "\n" in query or not query:
        return False, QUERY_MALFORMED, "candidate query must be a single non-empty line"
    if not query.endswith("?"):
        return False, QUERY_MALFORMED, "candidate query must end with ?"
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\(", query)
    if not match:
        return False, QUERY_MALFORMED, "candidate query must call a predicate"
    predicate = match.group(1)
    policy_query_predicates = policy_predicates(
        load_logic_policy() if policy_program is None else policy_program
    )
    allowed_predicates = {"relation", "path", "count", "review_required"} | policy_query_predicates
    if predicate not in allowed_predicates:
        return False, QUERY_UNKNOWN_PREDICATE, f"unknown predicate: {predicate}"

    args = _query_args(query)
    entities = entity_set(facts)
    # Relation OBJECTS may be literal values (attribute relations), which are not
    # in entity_set; validate them against the broader value_set so a fact about
    # a literal stays queryable. Subjects/path nodes/count subjects must be true
    # entities, so those keep using entity_set.
    values = value_set(facts)
    relations = allowed_relations(facts)
    if predicate == "review_required":
        if len(args) != 1 or len(_quoted_constants(query)) != 1:
            return False, QUERY_MALFORMED, "review_required must include the original question string"
        return True, QUERY_REVIEW_REQUIRED, "passed"
    if predicate == "relation":
        if len(args) != 3:
            return False, QUERY_BAD_ARITY, "relation query must have subject, relation, and object arguments"
        if not all(_is_valid_arg(arg) for arg in args):
            return False, QUERY_MALFORMED, "relation arguments must be variables or quoted strings"
        subject, relation, object_ = args
        if not _is_variable(subject) and _arg_value(subject) not in entities:
            return False, QUERY_ENTITY_NOT_ACCEPTED, f"relation subject is not an accepted entity: {_arg_value(subject)}"
        if not _is_variable(relation) and _arg_value(relation) not in relations:
            return False, QUERY_RELATION_NOT_ACCEPTED, f"relation name is not accepted: {_arg_value(relation)}"
        if not _is_variable(object_) and _arg_value(object_) not in values:
            return False, QUERY_ENTITY_NOT_ACCEPTED, f"relation object is not an accepted entity: {_arg_value(object_)}"
        if _relation_match_count(query, facts) == 0:
            return False, QUERY_FACT_ABSENT, "relation query does not match accepted facts"
        return True, QUERY_OK, "passed"
    if predicate == "path":
        if len(args) != 2:
            return False, QUERY_BAD_ARITY, "path query must have start and target arguments"
        if not all(_is_valid_arg(arg) for arg in args):
            return False, QUERY_MALFORMED, "path arguments must be variables or quoted strings"
        for arg in args:
            if not _is_variable(arg) and _arg_value(arg) not in entities:
                return False, QUERY_ENTITY_NOT_ACCEPTED, f"path argument is not an accepted entity: {_arg_value(arg)}"
        if all(_is_quoted_string(arg) for arg in args) and not dependency_path(facts, _arg_value(args[0]), _arg_value(args[1])):
            return False, QUERY_FACT_ABSENT, "path query does not match accepted facts"
        return True, QUERY_OK, "passed"
    if predicate == "count":
        # count(subject, relation)? — how many objects (subject, relation) has.
        # A valid count always has an answer (0 is a verified zero, never a
        # FACT_ABSENT), so it is QUERY_OK whenever the vocabulary is accepted.
        if len(args) != 2:
            return False, QUERY_BAD_ARITY, "count query must have subject and relation arguments"
        if not all(_is_valid_arg(arg) for arg in args):
            return False, QUERY_MALFORMED, "count arguments must be variables or quoted strings"
        subject, relation = args
        if not _is_variable(subject) and _arg_value(subject) not in entities:
            return False, QUERY_ENTITY_NOT_ACCEPTED, f"count subject is not an accepted entity: {_arg_value(subject)}"
        if not _is_variable(relation) and _arg_value(relation) not in relations:
            return False, QUERY_RELATION_NOT_ACCEPTED, f"count relation is not accepted: {_arg_value(relation)}"
        return True, QUERY_OK, "passed"
    if predicate in policy_query_predicates:
        if len(args) != 2:
            return False, QUERY_BAD_ARITY, "policy query must have entity and reason arguments"
        if not all(_is_valid_arg(arg) for arg in args):
            return False, QUERY_MALFORMED, "policy query arguments must be variables or quoted strings"
        if not _is_variable(args[0]) and _arg_value(args[0]) not in entities:
            return False, QUERY_ENTITY_NOT_ACCEPTED, f"policy query entity is not accepted: {_arg_value(args[0])}"
        return True, QUERY_OK, "passed"
    return False, QUERY_UNSUPPORTED, "unsupported query"


def validate_candidate_query(
    line: str,
    facts: list[dict[str, str]],
    policy_program: str | None = None,
) -> tuple[bool, str]:
    """Validate a single candidate Datalog query line against the current KB state.

    Returns (True, "passed") on success or (False, reason) on failure — a thin
    back-compatible wrapper over ``classify_query`` (which also returns a stable
    ``code``). This is the deterministic re-validation anchor used by the
    self-correction loop (AC4): after each LLM repair attempt the corrected query
    is run through this function before being accepted.

    ``policy_program`` lets callers supply the policy program text directly. When
    None (default) the compiled ``policy/logic-policy.dl`` is loaded, which
    requires that file to exist. Callers that must tolerate a KB without a
    compiled policy (e.g. interactive ask before ``/factlog check``) can pass the
    file's text if present or ``""`` if absent, so a missing policy yields an
    empty policy-predicate set instead of a hard exit.
    """
    ok, _code, reason = classify_query(line, facts, policy_program)
    return ok, reason
