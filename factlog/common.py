# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import csv
import decimal
import json
import math
import os
import re
import sys
import unicodedata
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from factlog import literal_types

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


def _atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path* atomically (temp file + os.replace).

    An interrupted plain ``write_text`` leaves a file truncated at a byte
    boundary — and a truncated ``accepted.dl`` still parses cleanly (it is broken
    only at a line boundary), so the engine evaluates over the surviving facts and
    the report passes with ``errors: 0`` while a confirmed fact silently answers
    ``0 rows``. temp+replace guarantees a reader sees either the prior
    snapshot or the complete new file, never a partial one. Mirrors the pattern
    already used for run-file JSON and candidates.csv in ``factlog.cli``.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
# Every lifecycle status that may legitimately appear in candidates.csv.  Keep
# this derived from the policy-specific groups so validation and diagnostics do
# not mistake a retired (non-engine) fact for an unknown status.
KNOWN_STATUSES = ENGINE_STATUSES | REVIEW_STATUSES | SUPERSEDED_STATUSES
QUERY_PREDICATES = {"relation", "path", "count", "conflict", "review_required"}
RELATION_FACT_RE = re.compile(r"^relation\((.*)\)\.$")
# 1.0.3 is the floor: it bundles/validates wirelog v0.52.0, the first release
# whose .dl parser supports \" escapes (wirelog#924) — required so an always-quoted
# amount unit (amount(N,"unit")) loads instead of aborting the whole program.
MIN_PYREWIRE_VERSION = (1, 0, 3)


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
        return _attribute_relations_from(self.policy_dir, lambda: relation_aliases(self.root))

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
        raise FactlogError("pyrewire가 필요합니다. 예: pip install 'pyrewire>=1.0.3'")
    current = version_tuple(str(getattr(pyrewire, "__version__", "0")))
    if current < MIN_PYREWIRE_VERSION:
        raise FactlogError(
            "pyrewire 1.0.3 이상이 필요합니다. "
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
    """The key that pairs a binary original with its runs/sources/ conversion.

    `factlog ingest` names a conversion by appending the converter's out-suffix
    to the original's *full* filename (extension included) and mirrors the
    original's subdirectory, so same-stem/different-extension originals no longer
    collide on one output file (#213). The pairing key therefore keeps the
    original's extension and drops only the conversion's final (out-)suffix:
        'sources/a/report.hwpx'         -> 'a/report.hwpx'
        'runs/sources/a/report.hwpx.md' -> 'a/report.hwpx'  (pairs with above)
        'sources/report.pptx'           -> 'report.pptx'
        'runs/sources/report.pptx.md'   -> 'report.pptx'    (pairs with above)
    An original under sources/ keeps its full name; a conversion under
    runs/sources/ drops one suffix. Subdirectory-aware, so same-name files in
    different subtrees never collide. NFC-normalised. (PurePosixPath: refs are
    posix-style.)

    Backward compatibility: a legacy conversion made before #213 is named by the
    bare stem (`runs/sources/report.md` from `report.pdf`), so its key is the
    stem (`report`) and no longer equals the new full-name original key
    (`report.pdf`). Such conversions pair through their provenance header where
    that signal exists (eject/orphan); otherwise re-run `factlog ingest --force`
    to migrate them to the new layout. See the migration note in the #213 PR.
    """
    ref = unicodedata.normalize("NFC", ref)
    is_conversion = False
    for rootname in SOURCE_ROOTS:
        prefix = rootname + "/"
        if ref.startswith(prefix):
            is_conversion = rootname == "runs/sources"
            ref = ref[len(prefix):]
            break
    p = PurePosixPath(ref)
    # Conversion: drop the out-suffix (.md/.txt) added by ingest, keeping the
    # original's own extension. Original: keep the full name so its extension is
    # part of the key and can't be confused with a same-stem sibling.
    return (p.with_suffix("") if is_conversion else p).as_posix()


def source_stem_key(ref: str) -> str:
    """The pre-#213 pairing key: source-root prefix stripped, one suffix dropped.

        'sources/a/report.pdf'     -> 'a/report'
        'runs/sources/a/report.md' -> 'a/report'   (legacy naming)

    Used only as a *fallback* to keep a legacy conversion (named by the bare
    stem, before #213 kept the original's extension) pairing with its original.
    A fresh/re-ingested KB matches on source_rel_key() and never needs this.
    Subdirectory-aware; NFC-normalised. See the #213 migration note.
    """
    ref = unicodedata.normalize("NFC", ref)
    for rootname in SOURCE_ROOTS:
        prefix = rootname + "/"
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
            break
    return PurePosixPath(ref).with_suffix("").as_posix()


def conversion_origin(path: Path) -> str | None:
    """The original filename recorded in an ingest conversion's provenance header.

    ingest writes a first-line header `... | source: <original-name> | ...` (or
    `[ingested-by-factlog] source: <name> | ...` for non-markdown output). Return
    the NFC-normalised original basename, or None when there is no header / no
    reliable `source:` value (a hand-placed conversion). Used to *verify* a
    legacy stem-key pairing so a pre-#213 conversion is tied to the exact
    original it was made from, never a same-stem sibling of a different extension.

    The recorded `source:` may be a bare basename (legacy, pre-#214) OR a
    sources/-relative path (#214: `sub_a/data.hwpx` disambiguates same-name
    originals in different subdirs). Either way this returns just the basename,
    so paired_conversion — the one caller, which is basename-keyed — is
    unaffected by the header format: the subdir that #214 encodes lives in the
    conversion's own mirrored path, not in this pairing signal.

    `eject` reads the header itself rather than calling this, and since #324 it
    is *not* basename-keyed: it keys on the conversion's own mirrored subdir
    joined with this basename, so a path argument cannot reach a same-name
    original in another directory. Widening what this returns would not reach
    that code, but narrowing the idea it encodes would mislead it.
    """
    try:
        head = path.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
    except OSError:
        return None
    m = re.search(r"source:\s*([^|>]+?)\s*(?:\||-->|$)", head)
    if not m:
        return None
    origin = unicodedata.normalize("NFC", m.group(1).strip())
    if not origin:
        return None
    # Normalise a sources/-relative header (#214) down to the basename so the
    # contract ("the original basename") holds for both header formats.
    return PurePosixPath(origin).name or None


def conversion_body_is_empty(path: Path) -> bool:
    """True iff an ingest conversion's body (excluding its provenance header) is blank.

    A scanned/image-only PDF (or any input with no extractable text) converts to
    a file that carries only the ingest provenance header and no content — a
    silent 0-facts source (#229). Return True for such a conversion so callers can
    flag it as `converted-but-empty (likely scanned/needs OCR)` instead of
    conflating it with a not-yet-synced source.

    Returns False for a file with no factlog provenance header (a plain text
    source or a hand-placed conversion — not something ingest produced) and for an
    unreadable file (err toward "has content" so a read glitch never hides text).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    parts = text.split("\n", 1)
    if "ingested-by-factlog" not in parts[0]:
        return False  # not an ingest conversion — do not judge its emptiness here
    body = parts[1] if len(parts) > 1 else ""
    return body.strip() == ""


def paired_conversion(
    orig_ref: str,
    conv_by_key: dict[str, str],
    path_of: Callable[[str], Path],
) -> str | None:
    """The runs/sources/ conversion ref that backs the original *orig_ref*, or None.

    *conv_by_key* maps source_rel_key(conv_ref) -> conv_ref for every candidate
    conversion; *path_of* resolves a conv_ref to its on-disk Path (to read the
    provenance header for the legacy fallback).

    Matching, shared by sources/coverage/status/merge so they agree:
      1. New scheme (#213): the conversion keeps the original's full name, so
         source_rel_key(orig) == source_rel_key(conv) — an exact, extension-aware
         1:1 match.
      2. Legacy fallback: a pre-#213 conversion is named by the bare stem, so it
         keys under source_stem_key(orig). Accept it ONLY when its provenance
         header names this exact original (or has no header — a hand-placed
         conversion, kept for backward compatibility). This prevents a new,
         still-unconverted original (report.pptx) from being mispaired to a
         legacy stem conversion made from a same-stem sibling (report.pdf).
    """
    conv = conv_by_key.get(source_rel_key(orig_ref))
    if conv is not None:
        return conv
    conv = conv_by_key.get(source_stem_key(orig_ref))
    if conv is not None:
        origin = conversion_origin(path_of(conv))
        if origin is None or origin == PurePosixPath(orig_ref).name:
            return conv
    return None


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
    # Split on '\n' only, NOT str.splitlines(): a fact's object can legitimately
    # contain U+2028/U+2029/U+0085 (routine in text copied from PDFs/web), which
    # dl_string keeps as raw chars on one physical line and the wirelog engine
    # parses fine — but .splitlines() would break the line on them and corrupt the
    # whole file's parse (#255). '\r' from CRLF is handled by the .strip() below.
    for line in accepted_dl.read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("canonical("):
            continue
        try:
            subject, relation, object_ = parse_relation_fact(line)
        except ValueError:
            raise FactlogError(f"accepted.dl contains unsupported fact syntax: {line}")
        rows.append({"subject": subject, "relation": relation, "object": object_})
    # Defensive: a stale or hand-edited accepted.dl may still carry duplicate
    # triples; collapse them so evaluate/check stay set-consistent. These rows
    # are bare triples, so no source/provenance is lost here.
    #
    # EXACT triples, deliberately NOT dedup_engine_atoms' canonical fold. This
    # list is what `run_wirelog` interns, while the engine parses the FILE TEXT
    # (`accepted_program`) — so every spelling present in the file must survive
    # to here. Folding identity here dropped the losing spelling from the intern
    # table while leaving its atom in the program, and `decode_wirelog_value`
    # then fell through to the bare intern id: `requires_review: 3` where the
    # name belongs, in facts/logic_report.txt. Any accepted.dl compiled by an
    # earlier release carries both spellings, so this fires on upgrade with no
    # hand-editing at all. Identity folding belongs at COMPILE time, where the
    # file is written (compile_facts); this loader's job is byte-duplicate
    # hygiene, which cannot desynchronize the two because equal bytes intern to
    # one symbol.
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, str]] = []
    for row in rows:
        triple = (row["subject"], row["relation"], row["object"])
        if triple in seen:
            continue
        seen.add(triple)
        unique.append(row)
    return unique


def load_accepted_facts() -> list[dict[str, str]]:
    return _load_accepted_facts_from(ACCEPTED_DL)


def markdown_policy_items(text: str) -> list[tuple[int, str, str]]:
    """Parse policy bullets out of a logic-policy.md body.

    Single source of truth for the policy-bullet grammar (#190): dash/star OR
    numbered (``1.``) list markers, a ``[id]`` tag, multi-line continuation of a
    wrapped bullet, and — critically — lines inside a ```` ``` ```` fenced code
    block are skipped (they are documentation examples, not live rules).
    ``tools/generate_logic_policy.py`` imports this so the compiler and the
    "does this .md define rules?" check can never disagree.
    """
    rows: list[tuple[int, str, str]] = []
    in_fence = False
    current_lineno: int | None = None
    current_item: str | None = None

    def flush_current() -> None:
        nonlocal current_lineno, current_item
        if current_lineno is None or current_item is None:
            return
        match = re.match(r"^\[([a-z0-9_]+)\]\s+(.+)$", current_item)
        if match:
            rows.append((current_lineno, match.group(1), match.group(2).strip()))
        current_lineno = None
        current_item = None

    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_current()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not stripped or stripped.startswith("#"):
            flush_current()
            continue
        if re.match(r"^(?:[-*]|\d+\.)\s+", stripped):
            flush_current()
            item = re.sub(r"^[-*]\s+", "", stripped)
            item = re.sub(r"^\d+\.\s+", "", item)
            current_lineno = lineno
            current_item = item
            continue
        if current_item is not None and line[:1].isspace():
            current_item = f"{current_item} {stripped}"
            continue
        flush_current()
    flush_current()
    return rows


def logic_policy_md_relations(sentence: str) -> list[str]:
    """Backtick-quoted relation names in a policy bullet. A bullet becomes a
    compilable rule iff this is non-empty — the exact condition
    ``generate_logic_policy.fixture_policy_json`` uses to accept/reject an item.
    """
    return re.findall(r"`([^`]+)`", sentence)


def logic_policy_md_has_rules(md_path: Path) -> bool:
    """Deterministic 'does this policy .md define compilable rules?' check.

    Delegates to the real compiler parser (``markdown_policy_items`` +
    ``logic_policy_md_relations``) rather than a look-alike regex, so it agrees
    byte-for-byte with what ``generate_logic_policy`` would compile: numbered
    lists, multi-line bullets, and fenced-code examples are all handled the same
    way (#190). Result is True iff at least one bullet yields a rule (an ``[id]``
    tag plus ≥1 backtick relation) — matching ``fixture_policy_json``. Used by
    ``_load_logic_policy_from`` and ``tools/finalize.py`` to tell a benign empty
    policy (→ graceful) from an uncompiled real one (→ fail loud).
    """
    if not md_path.is_file():
        return False
    md_text = md_path.read_text(encoding="utf-8")
    return any(
        logic_policy_md_relations(sentence)
        for _lineno, _reason, sentence in markdown_policy_items(md_text)
    )


def _load_logic_policy_from(logic_policy_dl: Path) -> str:
    if not logic_policy_dl.is_file():
        # A fresh `init`ed KB has no compiled logic-policy.dl yet. Distinguish
        # the benign no-policy case (empty/prose logic-policy.md → treat as an
        # empty policy so `check` can complete with 0 findings, matching how
        # `/factlog ask` is already graceful, #190) from a real error where the
        # author DID write rules but never compiled them (do not silently drop
        # the policy). The asymmetry is intentional: `ask` is exploratory and
        # short-circuits on a missing file (ask_router._policy_program_optional),
        # while `check` is a verification gate that must still complete.
        md_path = logic_policy_dl.with_name("logic-policy.md")
        if logic_policy_md_has_rules(md_path):
            raise FactlogError(
                "policy/logic-policy.dl is missing but policy/logic-policy.md defines "
                "rules; run tools/generate_logic_policy.py (or /factlog add) to compile it"
            )
        # No compiled logic-policy.dl, but a hand-authored logic-policy.extra.dl
        # may still exist (#120). Fall through to the extra.dl merge tail with an
        # empty base rather than short-circuiting here — otherwise those rules
        # would be silently dropped (justinjoy review), violating #190's own
        # invariant that user policy is never discarded without a loud error.
        text = ""
    else:
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
            # Avoid a leading newline when the base is empty (no compiled
            # logic-policy.dl) so the engine program text stays clean.
            text = (text + "\n" + extra_text) if text else extra_text
    # Guard: canonical/attr_rel/entity_node are engine-owned predicates; a head or
    # a re-.decl in policy text silently corrupts the engine program (pyrewire
    # treats the EDB as IDB and drops the injected atoms) or kills it outright.
    # Fail loud here, after the full policy text (base + extra.dl) is assembled,
    # so the check covers both files.
    _assert_no_reserved_head(text)
    return text


def load_logic_policy() -> str:
    return _load_logic_policy_from(LOGIC_POLICY_DL)


def policy_predicates(policy_program: str | None = None) -> set[str]:
    text = policy_program if policy_program is not None else load_logic_policy()
    # The predicates classify_query gives a dedicated branch, plus the engine's
    # own relations. A hand-authored `.decl` in logic-policy.extra.dl must not
    # re-route one of them through the policy branch: the two paths test
    # membership in a different order — classify_query checks count BEFORE policy,
    # validate_query checks policy first — so a `.decl count(...)` used to make the
    # report treat a count query as a policy query while the gate still treated it
    # as a count, which reproduces #328's report/gate divergence exactly.
    #
    # The rest are engine-owned relations with no branch at all: `edge`, and the
    # three names WIRELOG_PROGRAM declares — `canonical`, `attr_rel`,
    # `entity_node`, the last two being what keep a declared literal out of the
    # entity graph (#329). None is a queryable predicate (all are absent from
    # classify_query's `allowed_predicates`, so a query naming one is rejected as
    # an unknown predicate); `edge` has been excluded since before that set
    # existed. `_assert_no_reserved_head` already refuses a policy that re-`.decl`s
    # any of the three, so a loadable policy cannot reach this filter carrying one
    # — the exclusion is what makes that true of a program assembled by hand too,
    # and `canonical` was the one the set had been missing while this comment
    # already described it.
    # `conflict` is deliberately NOT here: it likewise has no branch, but it IS
    # meant to be policy-declared, and QUERY_PREDICATES lists it.
    built_in = {
        "relation",
        "edge",
        "path",
        "count",
        "review_required",
        "canonical",
        "attr_rel",
        "entity_node",
    }
    return {
        name
        for name in re.findall(r"^\.decl\s+([A-Za-z_][A-Za-z0-9_]*)\(", text, flags=re.MULTILINE)
        if name not in built_in
    }


def load_questions() -> list[dict[str, str]]:
    if not QUESTIONS_MD.is_file():
        raise FactlogError("missing policy/questions.md; run factlog init --target <kb>")
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


def fold_relation_name(name: str) -> str:
    """Return *name* under Unicode canonical composition, for membership tests.

    NFC only — never NFKC, never casefold. Fullwidth ``ＡＢＣ`` and ``ABC`` are
    different relations, not two spellings of one, and must stay distinct.
    """
    return unicodedata.normalize("NFC", name)


def folded_relation_names(names: Iterable[str]) -> set[str]:
    """NFC-fold a set of policy relation names once, for O(1) membership tests.

    ``_relation_names_from`` returns policy names verbatim, so comparing a row's
    relation against them raw is a byte comparison between two hand-written
    files: policy/single-valued.md and the extracted facts. A KB written
    uniformly in NFD — the macOS default for Hangul — then matches nothing, and
    every consumer that tests membership silently reports it as clean.

    ``check_conflicts`` folds both sides for exactly this reason. Readers that
    display conflict counts must use the same predicate, or ``factlog status``
    reports 0 on a KB ``finalize`` then refuses to compile — and unlike the
    remaining checker/status divergences, this one needs no mixed spellings at
    all, just one consistently decomposed KB.
    """
    return {fold_relation_name(name) for name in names}


def normalization_form(value: str) -> str:
    """Name the Unicode normalization form *value* is written in.

    Only NFC and NFD are named: they are the two forms this codebase folds
    between, and telling them apart is what a reader needs in order to know which
    row to edit. A pure-ASCII string is identical under both and is reported
    ``"NFC"``.

    ``"mixed"`` is the honest answer for everything else, and "everything else"
    is wider than the obvious case. It covers composed and decomposed syllables
    mixed inside one string, but also every string that is neither wholly
    composed nor wholly decomposed: a canonical-order violation (``'q̧́'`` — the
    combining marks in the wrong order, so it equals neither form), a composition
    exclusion (``'ä́'`` — NFC cannot recompose it), and a canonical singleton
    (``'Ω'`` U+2126, which NFC replaces with U+03A9 and NFD leaves alone). The
    label does not claim to explain which of those it is; it says only "not one
    of the two forms named above", which is what the reader needs.

    Shared so that ``check_conflicts`` and ``factlog vocab`` label a form the
    same way — two reports that disagree about which form a string is in are
    worse than one that says nothing.
    """
    if value == unicodedata.normalize("NFC", value):
        return "NFC"
    if value == unicodedata.normalize("NFD", value):
        return "NFD"
    return "mixed"


def composed_spelling(spellings: Iterable[str]) -> str:
    """Return the spelling to display on behalf of a Unicode-folded group.

    Deterministic, and always one of the strings as written (provenance). Where
    the group holds several normalization forms the **composed (NFC)** spelling
    wins; ties break lexicographically.

    Plain ``min`` would be deterministic too, but it picks the wrong member in
    practice: Hangul conjoining jamo (U+1100…) sort below precomposed syllables
    (U+AC00…), so ``min`` on a mixed group *always* returns the decomposed form —
    the one that will not match if the reader types or pastes the name into a
    search from an NFC editor. Preferring NFC makes the reported string the one
    most likely to grep.

    The grep argument only holds where the group *has* a composed member. On a
    uniformly decomposed KB every candidate is NFD and this returns NFD — still
    deterministic and still a spelling actually written, which is the guarantee
    that matters.

    The fold is the same NFC as ``fold_relation_name``; the separate name is
    because this one is applied to subjects and values, not to policy relation
    names, and only the latter is a membership test.

    Every caller standing a representative in front of a folded group goes
    through THIS function, never through a re-derived ordering of its own:
    ``check_conflicts._representative`` for the report, ``corroboration`` for its
    competing-values clause, and ``dedup_engine_atoms`` for the spelling written
    into ``accepted.dl``. Two of them naming one value differently is the bug
    this centralization exists to prevent.

    Same function, but only the same ANSWER where the callers are looking at the
    same group. For a **canonically equivalent** group they are, so the spelling
    the checker reports is the spelling the compiled file carries. For a typed
    relation they are not: ``_group_key`` partitions on the parsed scalar, so it
    holds ``{NFD('제3호'), '3위'}`` as one value, while ``dedup_engine_atoms``
    keeps two atoms because those strings are not canonically equivalent. Same
    representative rule applied to different partitions — the user-facing message
    is right about that, and this docstring should not be read past canonical
    equivalence.
    """
    return min(spellings, key=lambda s: (s != unicodedata.normalize("NFC", s), s))


def _relation_names_from(path: Path) -> set[str]:
    """Parse a policy file that lists relation names, one per line.

    Bullets and '#' comments are allowed; the relation name is the first
    `backtick`-quoted token if present, else the first whitespace token (quote a
    name that contains spaces). Absent file → empty set.

    Names are returned VERBATIM — no NFC coercion. single-valued.md's consumer
    (check_conflicts._canonicalize) deliberately preserves an NFD-authored name
    so a non-participating relation is reported exactly as written (#210/#227),
    and normalizing here breaks its NFD membership test. attribute-relations.md
    handles the same problem without touching the fact side; see
    _attribute_relations_from."""
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


def relation_aliases(root: Path | None = None) -> dict[str, str]:
    """Parse ``policy/relation-aliases.md`` into a ``{raw: canonical}`` map.

    File format — one bullet per mapping, two backtick groups separated by
    ``->``:

    .. code-block:: markdown

        # Relation aliases
        - `게재연도` -> `published_year`
        - `publication_year` -> `published_year`

    Rules: skip blank lines and ``#`` comments; each mapping line has exactly
    two backtick groups with ``->`` between; a leading ``-``/``*`` bullet is
    ignored.  Absent file → ``{}`` (behaviour is byte-identical for KBs without
    the file).  *root* selects the KB (mirrors how ``sync_ignore_patterns(root)``
    picks ``root/policy``); ``None`` → module ``POLICY_DIR``.

    Validation (raises :class:`FactlogError` on first violation — fail loud):

    * a ``raw`` mapped to two DIFFERENT canonicals → error;
    * a name that is both a ``raw`` key and a ``canonical`` value → chain →
      error;
    * ``raw == canonical`` self-map → error.
    """
    base = (root / "policy") if root is not None else POLICY_DIR
    path = base / "relation-aliases.md"
    if not path.is_file():
        return {}
    aliases: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = re.sub(r"^\s*[-*]\s+", "", line.strip()).strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Expect exactly `raw` -> `canonical` — arrow is required.
        m = re.fullmatch(r"`([^`]+)`\s*->\s*`([^`]+)`", stripped)
        if not m:
            continue
        raw = unicodedata.normalize("NFC", m.group(1).strip())
        canonical = unicodedata.normalize("NFC", m.group(2).strip())
        if not raw or not canonical:
            continue
        # self-map
        if raw == canonical:
            raise FactlogError(
                f"relation-aliases.md: self-map {raw!r} -> {canonical!r} is not allowed"
            )
        # duplicate raw with conflicting canonical
        if raw in aliases and aliases[raw] != canonical:
            raise FactlogError(
                f"relation-aliases.md: {raw!r} mapped to both "
                f"{aliases[raw]!r} and {canonical!r}"
            )
        aliases[raw] = canonical
    # chain: a raw that also appears as a canonical value
    canonical_values = set(aliases.values())
    for raw in aliases:
        if raw in canonical_values:
            raise FactlogError(
                f"relation-aliases.md: {raw!r} is both a raw predicate and a "
                "canonical target — alias chains are not allowed"
            )
    return aliases


def surface_variants(canonical: str, aliases: dict[str, str]) -> set[str]:
    """Reverse lookup — all raw predicates that map to *canonical*.

    Returns an empty set when *canonical* has no surface aliases.
    """
    return {raw for raw, canon in aliases.items() if canon == canonical}


def canonical_variants_of(relation: str, aliases: dict[str, str]) -> set[str]:
    """Surface variants of *relation* when it is a declared canonical, else empty.

    NFC-normalizes *relation* before the reverse lookup so a query-supplied name
    matches the NFC-normalized alias keys (relation_aliases() normalizes on load).
    Callers pass *aliases* (from relation_aliases()) so a hot path fetches it once;
    an empty result doubles as "not a declared canonical" (the boolean use in
    classify_query).
    """
    return surface_variants(unicodedata.normalize("NFC", relation), aliases)


def _attribute_relations_from(policy_dir: Path, read_aliases) -> set[str]:
    """Declared attribute relations, plus every surface spelling of the same
    relation (policy/relation-aliases.md).

    ``relation/3`` — engine input and every python consumer — stores the RAW
    relation name a fact was written with, so a KB that declares `정식_운영` an
    attribute relation and aliases `출시일` -> `정식_운영` still let its literals
    through the exclusion under the surface spelling. Expanding the set here
    closes the engine and the renderer at once: the engine's attr_rel/1 EDB is
    emitted from this same set and is likewise matched against a raw R.
    check_conflicts already canonicalizes single-valued relations this way.

    Alias expansion is bidirectional (declared name → its canonical → all surface
    variants) because the policy author may write either spelling.

    BOTH unicode normal forms of every name are emitted, rather than folding
    either side to NFC. macOS writes NFD routinely, and an NFD-authored
    attribute-relations.md against NFC facts matched nothing at all — the declared
    exclusion was simply off, with no diagnostic. Folding the POLICY side alone
    would only move that miss to the NFD-facts KBs it currently serves, and the
    FACT side cannot be folded here: the engine matches attr_rel/1 against
    relation/3's raw R, so a renderer that folded and an engine that could not
    would disagree — the divergence #329 exists to remove. Carrying both spellings
    costs at most 2n atoms and keeps the two sides identical.

    *read_aliases* is a CALLABLE, not a dict: a KB that declares no attribute
    relation must not pay an extra relation-aliases.md read per query, which is
    the #242 gate invariant tests/unit/test_query_literal_nfc.py pins."""
    names = _relation_names_from(policy_dir / "attribute-relations.md")
    if not names:
        return names
    aliases = read_aliases()
    if aliases:
        expanded = set(names)
        for name in names:
            # relation_aliases() keys are NFC-normalized, so look up the NFC form.
            nfc = unicodedata.normalize("NFC", name)
            canonical = aliases.get(nfc, nfc)
            expanded.add(canonical)
            expanded |= surface_variants(canonical, aliases)
        names = expanded
    return names | {
        form
        for name in names
        for form in (unicodedata.normalize("NFC", name), unicodedata.normalize("NFD", name))
    }


def attribute_relations() -> set[str]:
    """Relation names whose object is a LITERAL value, not a first-class entity
    (policy/attribute-relations.md).

    Objects of these relations (dates, numbers, ordinals, ...) are excluded from
    entity_set so they do not pollute the entity vocabulary (entity listings,
    path nodes, count subjects). All three axes apply ONE exclusion rule, spelled
    once in _entity_nodes: entity listings and count subjects read entity_set,
    and the entity GRAPH — both dependency_graph and the engine's entity_node/1 —
    gates its edges on the same predicate, so a literal is not a path node either
    (#329). They remain valid relation-query objects — see value_set and
    classify_query — so a fact about a literal is still verifiable.
    Same file format as single-valued.md; absent file → no attribute relations
    → entity_set == value_set (fully backward compatible). Surface aliases of a
    declared relation count as declared — see _attribute_relations_from.
    """
    return _attribute_relations_from(POLICY_DIR, relation_aliases)


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
# Built-in engine predicates declared by WIRELOG_PROGRAM; a typed-relation alias
# may not take one of these names. attr_rel/entity_node join the list with #329 —
# a colliding alias would re-.decl them and the engine silently accepts a
# duplicate .decl, quietly restoring the literal-as-path-node bug.
_TYPED_RESERVED = {"relation", "edge", "path", "attr_rel", "entity_node"}


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
    value, or a malformed pair, → FactlogError (fail loudly).

    Unit names are stored **NFC-folded** (``fold_relation_name``), and the
    duplicate check runs on the folded key. The lookup side folds too
    (``literal_types.parse_amount``), so a units clause written in NFD — the
    macOS default for Hangul — resolves the same objects as an NFC one. Without
    the fold the two sides are a raw byte comparison between a policy file and
    an extracted object: on an NFD KB every amount would fail to parse, and two
    spellings of one value (``5400억`` / ``0.54조``) would split into a CONFLICT
    that no normalization message explains. NFC only, never NFKC — same rule as
    ``fold_relation_name``. An all-ASCII / all-NFC clause folds to itself, so
    this is byte-identical for KBs that were already composed."""
    units: dict[str, int] = {}
    for pair in body.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise FactlogError(f"typed-relations: malformed unit pair {pair!r} (expected unit=number)")
        unit, _, value = pair.partition("=")
        unit = fold_relation_name(unit.strip())
        value = value.strip()
        if not unit:
            raise FactlogError(f"typed-relations: empty unit name in {pair!r}")
        try:
            num = decimal.Decimal(value)
        except decimal.InvalidOperation as exc:
            raise FactlogError(f"typed-relations: non-numeric unit value {value!r} for {unit!r}") from exc
        if not num.is_finite() or num != num.to_integral_value() or num <= 0:
            raise FactlogError(f"typed-relations: unit value for {unit!r} must be a positive integer, got {value!r}")
        if unit in units:
            raise FactlogError(f"typed-relations: duplicate unit {unit!r} in units clause")
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


# Per-type engine column for a projectable typed side-relation. This pyrewire
# build's .dl TEXT parser accepts only int32|int64|string|symbol scalar columns
# — there is NO float text column. `date`/`ordinal` normalize to sortable ints
# -> int64. `amount` normalizes to an exact integer base unit -> int64. `number`
# (#125) has no native float column, so it projects as a fixed-point int64
# scaled ×1000 (3 decimal places, see literal_types.parse_number_scaled);
# comparison thresholds in hand-authored predicates MUST be written in the same
# SCALED units (`version >= 2.0` -> `version_num(S, V), V >= 2000`).
_TYPED_COL = {"date": "int64", "ordinal": "int64", "number": "int64", "amount": "int64"}


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


# Engine predicate names a hand-authored policy may not HEAD or re-``.decl``.
# Each is owned by the engine program: canonical and attr_rel are EDB filled from
# outside the policy, entity_node is derived from them. Value = the "why" the
# error message shows. See _assert_no_reserved_head for what each failure looks
# like when it is NOT caught.
_RESERVED_POLICY_HEADS = {
    "canonical": "a reserved engine EDB predicate (populated from relation-aliases.md)",
    "attr_rel": "a reserved engine EDB predicate (populated from policy/attribute-relations.md)",
    "entity_node": (
        "a reserved engine predicate (derived from relation/3 and attr_rel/1; it is "
        "what keeps a literal value out of the entity graph)"
    ),
}


def _reserved_head_error(name: str) -> FactlogError:
    return FactlogError(
        f"{name} is {_RESERVED_POLICY_HEADS[name]}; it may appear only in rule "
        "bodies, not as a rule head, a bare fact, or a .decl in "
        f"logic-policy(.extra).dl. Rename your predicate (e.g. my_{name})."
    )


def _assert_no_reserved_head(policy_text: str, reserved: set[str] | None = None) -> None:
    """Raise FactlogError if the policy text HEADS or re-``.decl``s a reserved
    engine predicate (*reserved*, default :data:`_RESERVED_POLICY_HEADS`).

    Every name in that set is declared by WIRELOG_PROGRAM and owned by the engine.
    Each fails differently, and none of them fails usefully on its own:

    * ``canonical`` is EDB emitted by compile_facts into accepted.dl.  A head makes
      pyrewire treat it as IDB and silently drop every compile-emitted EDB atom —
      wrong answers with rc=0 (#227).
    * ``attr_rel`` is EDB emitted from policy/attribute-relations.md.  Same failure
      shape, and its consequence is that declared literals silently return to the
      entity graph: measured, one ``attr_rel(R) :- relation(S, R, O), R = "…".``
      line put engine path/2 back to ``[('갑봇','2030.1'), …]`` while the python
      renderer still answered ``[('갑봇','을서비스')]``, rc=0 — exactly the
      engine/renderer divergence #329 removed (#329 round 2).
    * ``entity_node`` is derived (``edge`` is gated on it).  A policy head ADDS
      rows to it and puts literals back in the graph; a ``.decl`` at another arity
      — ``pred(entity, reason)``, this repo's standard policy-predicate shape — made
      pyrewire raise a bare ``ExecError: execution error`` traceback, and with a
      matching fact present it died with SIGSEGV.

    They may appear freely in rule *bodies* (right of ``:-``) — that is the whole
    point of #227.

    Detection strategy: split the policy into logical STATEMENTS (a clause up to its
    terminating ``.``), stripping quoted strings first so ``"canonical("`` inside a
    reason literal is not mistaken for a predicate call, and cutting comments to the
    end of their line — an END-of-line comment left standing disabled this check
    outright (see the ordering note below).  Then tokenize each
    statement's HEAD — the predicate name left of ``:-``, or the whole clause for a
    bare fact — and reject it only when that name is exactly a reserved one.  Text
    that is not a clause at all (an unterminated statement, which absorbs the next
    one and would hide ITS head) is caught by a second pass that rejects a reserved
    name standing in call position anywhere a head can stand.  A
    substring search was wrong in BOTH directions and this replaces it: ``canonical
    (X, ...)`` with a space before the paren slipped past ``find("canonical(")`` (a
    head evaded the guard, rc=0), while ``not_canonical(X, ...)`` — a user predicate
    that merely CONTAINS the reserved name — was rejected as a head.

    The ``.decl``s in WIRELOG_PROGRAM are never passed as *policy_text*, but a
    hand-authored ``.decl canonical(...)`` in the policy re-declares the engine EDB
    and is still rejected (checked, then stripped, before statement splitting because
    a ``.decl`` directive carries no clause-terminating dot and would otherwise merge
    into — and mask — the head of the statement that follows it).

    Raises :class:`FactlogError` on first offending line with an actionable message.
    """
    names = set(_RESERVED_POLICY_HEADS) if reserved is None else reserved
    # Strip quoted literals and comments, then split into logical STATEMENTS on
    # clause-terminating '.' rather than per physical line. A period terminates a
    # clause unless it opens a '.decl'-style directive (dot followed by a letter at
    # a token start) or sits inside a float (dot between digits). Per-line tracking
    # mis-classified a reserved head/fact that shares a physical line with a
    # preceding rule's terminator as an in-body reference (#261); a statement is a
    # full clause, so reserved-name-left-of-neck (or no neck at all) is
    # unambiguously a head/fact.
    #
    # ONE pass, left to right, alternating literal | comment, so each construct
    # ends the other. Dropping whole comment LINES instead was wrong in both
    # directions (#329 round 3): an END-of-line comment survived, `_split_policy_
    # statements` pushed it onto the front of the NEXT statement, and the head
    # tokenizer below failed on the '/' — m is None, so a reserved head one line
    # after `foo(X). // note` passed UNCHECKED with rc=0. Conversely the `.decl`
    # scan ran on text that still held comments, so `// .decl entity_node(...) 은
    # 금지` — an author documenting why a name was avoided — was rejected as a real
    # re-declaration. Ordering is load-bearing on both sides: a literal is consumed
    # whole so `"http://x"` is not read as a comment, and a comment is consumed to
    # end of line so a lone `"` in prose cannot pair with a quote further down and
    # delete the policy in between.
    # `[^"\\]|\\.` consumes an ESCAPED quote as part of the literal. `"[^"]*"`
    # stopped at the backslash's quote, so the leftover `"` paired with the next
    # literal's opening quote and everything between them was deleted — measured,
    # `a(X,"q\"") :- …` two lines above `attr_rel(R) :- …` erased the reserved head
    # outright and the guard passed. pyrewire compiles that literal, so it is
    # policy a person can legitimately write, not garbage input.
    bare = re.sub(r'"(?:[^"\\]|\\.)*"|(?://|#)[^\n]*', "", policy_text)
    # A `.decl <name>(...)` directive has no clause-terminating '.', so it merges into
    # the statement that follows it and would hide that statement's real head from the
    # tokenizer below. Reject a reserved re-declaration first, then strip every
    # `.decl ...(...)` so the head tokenizer sees only rule heads and bare facts. The
    # .decl check is not redundant with the head check: `.decl entity_node(a, b)` alone
    # — no rule at all — already changes the arity the engine program compiles against.
    for name in re.findall(r"\.decl\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", bare):
        if name in names:
            raise _reserved_head_error(name)
    # Strip EVERY directive, not just `.decl`. A paren-less one — `.output p2` —
    # carries no terminator either, so it merges with the fact after it exactly as
    # `.decl` does; the merged statement then begins with `.`, the head tokenizer
    # finds nothing, and the fact rode in unexamined (#329 round 4). See
    # `_strip_directives` for how much a directive is allowed to own, which is
    # where the bypasses have been rather than in the stripping itself.
    bare = _strip_directives(bare)

    for statement in _split_policy_statements(bare):
        # Tokenize the HEAD (the predicate name left of ':-', or the whole clause for
        # a bare fact); do NOT substring-search the statement. `find("canonical(")`
        # was wrong in both directions: `canonical (X, ...)` (one space) slipped past
        # it while `not_canonical(X, ...)` — a user predicate that merely CONTAINS the
        # reserved name — was rejected. `\s*\(` tolerates the space; matching the whole
        # name rules out `not_canonical`.
        head = statement.split(":-", 1)[0]
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", head)
        if m and m.group(1) in names:
            # A reserved head or bare fact → reject.
            raise _reserved_head_error(m.group(1))
        # `re.match` sees only the FIRST atom, and a statement whose clause never
        # terminated absorbs the one after it — whose own head is then never
        # examined. `foo(X, a` followed by `attr_rel(R) :- …` passed here and on
        # main, and comment cutting widened the ways to get there: `foo(X, a#b).`
        # is not valid Datalog, but stripping `#b).` leaves exactly that shape.
        #
        # The rule is positional, not "a reserved name appears somewhere left of a
        # neck": THE ATOM IMMEDIATELY LEFT OF A NECK IS THAT CLAUSE'S HEAD. So for
        # every segment that has a neck after it, check its LAST atom and nothing
        # else. Scanning the whole segment instead rejected legal policy — the
        # body reference in `p(X) :- canonical(X,_,_).` sits left of a neck too,
        # once a mis-split has glued the next clause on, and #227 exists precisely
        # to allow that position. Reading only the last atom is what separates the
        # two: a body reference reaches that position only when another atom does
        # not stand between it and the neck.
        #
        # That distinction narrowed once directives started being stripped above.
        # A program the engine COMPILES gives every clause one neck and a
        # terminator, so it no longer reaches a two-neck segment at all, and on
        # such input this rule and the older whole-segment scan agree. What it
        # still buys is on text the engine rejects: a legal #227 body reference is
        # not reported as a head merely because the clause above it lost its dot.
        #
        # A statement with NO neck is a bare fact, and the same positional reading
        # applies to it whole: the clause is its own head. `re.match` above covers
        # it only when the fact starts the statement, which a merged directive
        # remnant breaks — `.plan 0` leaves ` 0` in front, the match fails on the
        # digit, and the fact behind it was never looked at. Scanning the neckless
        # statement here is what closes that, so the two shapes #358 and round 4
        # found are handled by one rule rather than by where the text happened to
        # begin.
        segments = statement.split(":-")
        head_bearing = segments[:-1] if len(segments) > 1 else segments
        for segment in head_bearing:
            atoms = re.findall(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*\(", segment)
            if atoms and atoms[-1] in names:
                raise _reserved_head_error(atoms[-1])
        # A reserved name anywhere else in a body → reference → allowed.


# Datalog directives, whose leading '.' does NOT terminate a clause. Spelled out
# rather than inferred: "dot at a token start followed by a letter" also describes
# an ordinary terminator with a space before it, which pyrewire accepts —
# `p(X) :- canonical(X,_,_) .q(Y) :- relation(Y,_,_).` compiles, and reading its
# ` .q` as a directive fused two legal clauses into one statement.
_DL_DIRECTIVES = frozenset(
    {
        "decl",
        "type",
        "symbol_type",
        "number_type",
        "input",
        "output",
        "printsize",
        "limitsize",
        "pragma",
        "functor",
        "comp",
        "init",
        "override",
        "plan",
    }
)
_DIRECTIVE_RE = re.compile(
    r"\.(?:" + "|".join(sorted(_DL_DIRECTIVES)) + r")(?![A-Za-z0-9_])"
)
# What a directive may own: the relation it names and that relation's
# parenthesised parameters, on the directive's own line.
#
# `[^\S\n]` — same-line whitespace only, because a directive's operand is on the
# directive's own line.
#
# Round 4 HAD the defect this narrowness was introduced for: `.pragma "x" "y"`
# loses its quoted operands to the literal strip that runs first, leaving a bare
# `.pragma`, and `\s+\w+` then reached the `canonical(` on the following line and
# deleted it. That is history, not a description of this code — since the tail
# rule below matches with `\s*`, an atom taken from the next line is now given
# straight back, and widening this pattern to `\s+` changes no verdict: measured,
# the round-4 shapes still refuse and the suite is unchanged at 1612.
#
# So this constraint is deliberate redundancy, not the thing currently holding
# the line, and it is UNPINNED — there is no observable difference to assert on,
# which is exactly why the claim it used to carry could rot unnoticed. It is kept
# because the two sides answer different questions and should not drift into
# sharing an answer by accident.
_DIRECTIVE_OPERAND_RE = re.compile(
    r"[^\S\n]+[A-Za-z_][A-Za-z0-9_]*(?:[^\S\n]*\([^)]*\))?"
)
# What follows an atom that proves the atom was a CLAUSE, not an operand: a
# clause-terminating '.', or a neck.
#
# `\s*`, NOT the `[^\S\n]*` the operand pattern uses — the two sides answer
# different questions and must not share a whitespace policy. Where the operand
# CAN be is line-local, because a directive's operand sits on the directive's
# line. Where the proof-of-clause can be is not: a clause may put its terminator
# or its neck on the next line, and pyrewire compiles that. Inheriting the
# line-local constraint here meant give-back never fired for
# `.plan attr_rel("v0")\n.` and the reserved clause was eaten as an operand —
# the same silent wrong answer at a third location (#329 round 6).
_CLAUSE_TAIL_RE = re.compile(r"\s*(?:\.|:-)")


def _strip_directives(text: str) -> str:
    """Remove Datalog directives so the head tokenizer sees only clauses.

    A directive carries no clause-terminating '.', so it merges with the statement
    after it and would hide that statement's head. Stripping it is what stops
    `.output p2` above a bare `attr_rel("참조").` from riding in unexamined.

    OVER-consuming is its own bypass, and this has bitten twice. The strip stops
    at the parameters rather than at end of line, because
    `.output p2 attr_rel("참조").` on ONE line is a program pyrewire compiles and
    eating the remainder would delete a real reserved fact. It stops at the
    newline for the same reason.

    The third case is this function rather than the pattern: an operand that is
    ITSELF a terminated clause is not an operand. `.plan attr_rel("참조").` read
    `attr_rel` as the operand and `("참조")` as its parameters and deleted the
    whole fact — five characters, `.plan `, and the report's path answer flipped
    silently. pyrewire does the opposite: it takes the fact. So whenever the text
    that would be consumed is followed by `.` or `:-`, it is given back and only
    the keyword is removed. The neck counts because a rule head after a directive
    is a clause too, and `.plan attr_rel(R) :- …` is engine-accepted. That covers
    every directive, including any this engine does not implement today, without
    depending on which ones it does.

    A competing rule was available — never treat an atom followed by `(` as the
    operand — and the two disagree on exactly one shape::

        .limitsize attr_rel(n=10)
        p(X) :- relation(X,_,_).      here: PASS      paren rule: REJECT

    This one PASSES it: `attr_rel(n=10)` is followed by neither `.` nor `:-`, so
    it reads as the directive's own operand. pyrewire refuses that program either
    way (measured), so PASS cannot carry a wrong answer to a user and REJECT
    could not have saved one. The clause-terminator rule was chosen because it
    generalises to any directive taking a bare name, where the paren rule assumes
    operands are unparenthesised — an assumption `.limitsize p2(n=10)` already
    breaks, and refusing that shape is the false-rejection failure mode this
    guard has been sent back for twice.
    """
    out: list[str] = []
    pos = 0
    for keyword in _DIRECTIVE_RE.finditer(text):
        if keyword.start() < pos:
            continue  # already inside text consumed by an earlier directive
        out.append(text[pos : keyword.start()])
        end = keyword.end()
        operand = _DIRECTIVE_OPERAND_RE.match(text, end)
        if operand and not _CLAUSE_TAIL_RE.match(text, operand.end()):
            end = operand.end()
        out.append(" ")  # never glue the neighbours into one name
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _split_policy_statements(text: str) -> list[str]:
    """Split Datalog policy text into logical statements on clause-terminating '.'.

    A '.' ends a clause EXCEPT when it opens one of the directives in
    :data:`_DL_DIRECTIVES` or sits inside a float (a dot between two digits). This
    lets `_assert_no_reserved_head` see each head/fact/rule as one unit even when
    several share a physical line.

    Matching the directive KEYWORD is the whole point. "A dot at a token start
    followed by a letter" is also what a perfectly ordinary terminator written with
    a space before it looks like, so that heuristic silently glued the next clause
    onto the current one. That leaked in BOTH directions, and the keyword match is
    what closes them together (#329 round 3, #358):

    * the absorption scan read the first clause's BODY reference as a head,
      rejecting policy the engine compiles;
    * a bare reserved FACT absorbed into the previous statement was never
      examined, and the engine HONOURS it. ``p(X) :- relation(X,_,_)
      .attr_rel("참조").`` loaded clean and moved the report's
      ``- path 갑봇 -> 병문서`` from a derived path to ``(not found)`` at rc=0 with
      no error; the identical fact on its own line has always been refused.
      Measured on `main` for ``canonical`` too, where the same one space turned
      ``policy findings: 0`` into a finding about an entity in no fact (#358).

    The adjacency is not always as authored: stripping a quoted literal makes it,
    ``O = "v1.0".canonical(`` becoming ``O = .canonical(``, so this cannot be
    handled by looking at the source text alone.

    This covers only dots MISREAD as directives. A GENUINE directive still merges
    with the statement after it, correctly — it has no terminator — and a
    paren-less one (`.output p2`) leaves that statement starting with `.` where
    the head tokenizer finds nothing. Nothing here catches that; the directive
    strip in `_assert_no_reserved_head` does (#329 round 4)."""
    statements: list[str] = []
    buf: list[str] = []
    for i, ch in enumerate(text):
        buf.append(ch)
        if ch == ".":
            prev = text[i - 1] if i > 0 else ""
            nxt = text[i + 1] if i + 1 < len(text) else ""
            is_directive = (prev == "" or prev.isspace()) and _DIRECTIVE_RE.match(
                text, i
            )
            is_float = prev.isdigit() and nxt.isdigit()
            if not is_directive and not is_float:
                statements.append("".join(buf))
                buf = []
    if buf:
        statements.append("".join(buf))
    return statements


_FLOAT_LITERAL_RE = re.compile(r"\d+\.\d+")


def _assert_no_unscaled_number_threshold(
    specs: dict[str, TypedRelSpec], extra_dl_text: str
) -> None:
    """Fail loud if a hand-authored logic-policy.extra.dl rule compares a `number`
    alias against an UNSCALED float literal (e.g. ``version_num(S, V), V >= 2.0``).

    `number` projects as a fixed-point int64 scaled ×1000 (#125), so a float
    threshold like ``2.0`` is both wrong (it means 0.002 in scaled units) AND a
    hard ParseError — the engine .dl text parser rejects a float literal, which
    rejects the WHOLE program (killing relation/3 + every fact: a dead KB) with
    only a bare ParseError. Catch it here with a clear, actionable message.

    Scan is NARROW to avoid false positives: only lines that reference a declared
    `number` alias as a whole word, only the hand-authored extra.dl text (never
    accepted.dl or date/amount data — their thresholds are legitimately ints).
    Quoted `"..."` spans (e.g. a reason string like ``"v2.0_plus"``) are stripped
    before the float scan — a float-looking token there is a string the engine
    accepts, not a threshold."""
    number_aliases = [
        spec.alias for spec in specs.values() if spec.type == "number"
    ]
    if not number_aliases:
        return
    alias_re = re.compile(
        r"\b(?:" + "|".join(re.escape(a) for a in number_aliases) + r")\b"
    )
    for line in extra_dl_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("#"):
            continue
        # Strip quoted strings so a float inside a reason symbol (which the engine
        # accepts) is not mistaken for an unscaled threshold in the rule body.
        line_wo_strings = re.sub(r'"[^"]*"', "", line)
        m = _FLOAT_LITERAL_RE.search(line_wo_strings)
        if m and alias_re.search(line_wo_strings):
            alias = alias_re.search(line_wo_strings).group(0)
            raise FactlogError(
                f"logic-policy.extra.dl: {alias!r} threshold uses an unscaled "
                f"float {m.group(0)!r}; number is scaled ×1000 — write it in "
                f"scaled units (e.g. 'V >= 2.0' -> 'V >= 2000')"
            )


def corroboration_counts(facts: list[dict[str, str]]) -> dict[tuple[str, str, str], int]:
    """Map each engine atom (``engine_atom_key``) to the number of DISTINCT
    sources backing it. A fact corroborated by several independent sources is
    more trustworthy — a signal a plain notes wiki cannot give.

    Keyed on the atom's identity, not on the raw triple, because its consumer
    (``factlog/compile_facts.py``) annotates the atoms ``dedup_engine_atoms``
    wrote and those are folded: keyed raw, a fact backed by two sources under two
    spellings collapsed to one atom reported ``sources=1``, counting only the
    spelling that happened to win. Sources are counted per folded atom, so a
    source backing both spellings counts once (summing two raw counts would
    double it)."""
    sources: dict[tuple[str, str, str], set[str]] = {}
    for row in engine_facts(facts):
        sources.setdefault(engine_atom_key(row), set()).add(row["source"])
    return {key: len(srcs) for key, srcs in sources.items()}


def fact_signals(
    facts: list[dict[str, str]],
    root: Path | None = None,
) -> dict[tuple[str, str, str], dict[str, object]]:
    """Per engine atom (``engine_atom_key``), the answer-quality signals:
    distinct ``sources`` count, max ``confidence``, and ``stale`` (True if any
    backing source file no longer exists under the KB — the fact rests on a
    vanished/changed source and should be re-verified).

    Keyed on the atom's identity, not the raw triple, and ``ask``'s renderer
    folds the engine row through ``fold_atom_triple`` before looking in here.
    Both sides move together or neither does. With the atom folded at compile
    time and this map still raw, the failure took two shapes and the ordinary
    one is the quieter:

    * a group holding two spellings on ONE axis (same subject, object written
      both ways — the common shape) writes a representative that IS one of the
      rows, so the raw lookup **finds** an entry: measured
      ``(sources: 1, conf 0.90)`` with one backing path, for a fact backed by
      two sources. Right-looking, silently short.
    * a CROSS group (subject and object swapping forms) has no row composed on
      both axes, so the representative is synthesized and the raw lookup
      **misses**: ``[no extraction backing]``, dropping the source count, every
      backing path and the ``[stale: source missing]`` marker at once.

    A group written one way is unaffected either way — every row shares the raw
    triple, so the raw key was already the atom's key.

    Before the atom folded, both key spaces were raw and every atom matched: the
    duplicate rows were wrong but nothing was missing. For a provenance tool
    losing a source is the worse failure, which is why this moved rather than
    the atom moving back."""
    base = ROOT if root is None else Path(root)
    acc: dict[tuple[str, str, str], dict[str, object]] = {}
    for row in engine_facts(facts):
        key = engine_atom_key(row)
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


def engine_atom_key(row: dict[str, str]) -> tuple[str, str, str]:
    """Identity of the engine atom *row* compiles to: ``(NFC(subject), relation,
    NFC(object))``.

    The single definition of "these rows are the same engine atom", shared by
    ``dedup_engine_atoms`` (which collapses on it) and ``corroboration_counts``
    (which aggregates sources under it), so the atom written to ``accepted.dl``
    and the source count reported for it cannot disagree.

    **Which axes fold, and why those.** Subject and object fold under NFC;
    the relation does not.

    * *subject / object* — ``check_conflicts`` already folds both (``_fold``,
      applied to subjects and to objects through ``_group_key``), and
      ``common._canonical_value`` (#213) fixed NFC as value equality for every
      query comparison. Keying the engine atom raw made the engine the one
      component that still saw two entities where the rest of the pipeline saw
      one: measured, a fact written once in NFC and once in NFD produced two
      byte-different, visually identical ``relation(...)`` lines in
      ``accepted.dl`` — the inflated duplicate count ``dedup_engine_atoms``
      exists to prevent, arriving through the normal ``finalize`` path (#342).

      This fold is per-triple, which is NOT the checker's subject fold:
      ``_group_key`` folds the subject across rows into a bucket. Two rows with
      the same folded subject and different objects stay two atoms here and are
      one group there, so the checker remains stricter on that axis. See
      ``check_conflicts.collect_conflicts``.
    * *relation* — left verbatim, matching the checker's grouping and
      ``corroboration``. Folding it is the deferred #210 call: it changes which
      rows collide on an axis where "no silent NFC coercion for a
      non-participating relation" was a deliberate promise, and deciding that is
      a maintainer's, not this fix's. So two spellings of one relation still
      make two ``relation/3`` atoms.

      Read that as "this function does not fold it", never as "the axis is
      unfolded". ``canonical_atoms`` NFC-folds the relation before its alias
      lookup, so the SAME ``accepted.dl`` can carry two ``relation/3`` atoms and
      one ``canonical/3`` atom for one aliased pair; and ``_canonical_value``
      folds a query's relation argument, so one spelling typed by the user
      already matches both. The #210/#345 question is therefore not whether to
      start folding this axis but whether to make the rest agree with the two
      places that already do.

    NFC only — never NFKC, never casefold. Fullwidth ``ＡＢＣ`` and ``ABC``, and
    ``a`` and ``A``, are different values and must stay different atoms.

    Every map keyed by this must be *looked up* through it too. ``fact_signals``
    and ``corroboration_counts`` build their keys here; ``ask``'s renderer folds
    the engine row it holds through :func:`fold_atom_triple` before asking them.
    Half a move — one side folded, the other raw — is wrong in two different
    ways depending on the group's shape: it under-counts where the raw lookup
    still happens to hit, and drops the annotation outright where the
    representative is synthesized. Neither merely duplicates, which is what the
    all-raw arrangement did. See ``fact_signals`` for both measured."""
    return fold_atom_triple(row["subject"], row["relation"], row["object"])


def fold_atom_triple(subject: str, relation: str, object_: str) -> tuple[str, str, str]:
    """:func:`engine_atom_key` for a caller holding the three values loose —
    an engine answer row, say, which is a list and not a candidates dict."""
    return (
        unicodedata.normalize("NFC", subject),
        relation,
        unicodedata.normalize("NFC", object_),
    )


def dedup_engine_atoms(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Collapse rows that are the same engine atom (``engine_atom_key``) to one
    row, emitted in first-occurrence order.

    The engine atom carries only the triple (see ``dl_atom``); the same fact
    accepted from several sources must appear once in ``accepted.dl`` so ``ask``
    and ``run_logic_check`` report set semantics (one row / true count) rather
    than an inflated, duplicated count. Source aggregation (``sources: N``,
    provenance) lives on the separate candidates path (``corroboration_counts``,
    ``fact_signals``) and is untouched by this collapse.

    **Which spelling is written.** Each axis independently: the subject is
    ``composed_spelling`` of every subject in the group, the object likewise.
    That is the *same* choice ``check_conflicts._representative`` makes — it is
    ``composed_spelling`` — so for a canonically equivalent group the checker's
    report and the compiled atom name a value identically, which is the invariant
    that docstring states and this must not break. The composed spelling is also
    the one a reader greps for from an NFC editor, and the one the engine's typed
    projection can parse (``_project_typed_relations`` hands
    ``literal_types.normalize`` the object as written, so a decomposed
    ``NFD('7위')`` normalizes to ``None`` and the fact silently leaves the typed
    table).

    Preferring it therefore only *rescues a group that has one*. Where every
    member is decomposed there is nothing to prefer, the atom stays decomposed,
    and the typed literal is dropped exactly as before — see the byte-invariance
    note below, which is the same fact stated as a guarantee.

    Ranking whole rows instead — picking the group member that sorts first — is
    what an earlier revision did, and it is wrong on a *cross* group: one row
    NFD-subject/NFC-object and another NFC-subject/NFD-object have no member
    composed on both axes, so whichever row wins writes a decomposed axis while
    the group demonstrably holds a composed spelling for it. The two axes are
    independent and are chosen independently.

    So the emitted row can be a triple no single input row carried. That is safe
    only because every map keyed on an atom is keyed on ``engine_atom_key`` and
    looked up through it (``fact_signals``, ``corroboration_counts``); a
    raw-triple map would miss the synthesized atom entirely and drop its
    provenance. The non-triple fields (source, confidence, status) come from the
    group's FIRST row, so first-occurrence still decides everything the fold does
    not.

    **Byte-invariance.** ``composed_spelling`` of a one-element set is that
    element, so a group written one way yields its first row untouched — the
    same object, not a copy. The bytes of an atom change only for a group that
    actually holds more than one spelling on an axis, and then only to a
    spelling already written in that KB. A KB with no canonically equivalent
    duplicates — every NFC-only KB, and every uniformly-NFD one — compiles to a
    byte-identical ``accepted.dl``. Nothing is normalized on the way out; a
    uniformly decomposed KB keeps its decomposed spelling, because there is no
    composed member to prefer."""
    groups: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault(engine_atom_key(row), []).append(row)
    unique: list[dict[str, str]] = []
    for members in groups.values():
        base = members[0]
        subject = composed_spelling({row["subject"] for row in members})
        object_ = composed_spelling({row["object"] for row in members})
        if subject == base["subject"] and object_ == base["object"]:
            unique.append(base)
        else:
            unique.append({**base, "subject": subject, "object": object_})
    return unique


def canonical_atoms(
    rows: list[dict[str, str]],
    aliases: dict[str, str],
) -> list[tuple[str, str, str]]:
    """Return deduped ``(subject, canonical_rel, object)`` triples for rows that
    participate in the alias map (alias-participating only: strategy A).

    A row participates when its relation is either:
    - an alias **key** (raw predicate) → canonical = ``aliases[R]``
    - an alias **value** (canonical name itself stored literally) → canonical = R

    Rows whose relation is in neither set are skipped.  Deduplication mirrors
    ``dedup_engine_atoms``: first-occurrence stable, keeps the first triple seen.
    NFC-normalization is applied to the row's relation before lookup so NFD-
    authored CSV rows match the NFC-normalized alias keys produced by
    ``relation_aliases``."""
    if not aliases:
        return []
    canonical_values: set[str] = set(aliases.values())
    seen: set[tuple[str, str, str]] = set()
    unique: list[tuple[str, str, str]] = []
    for row in rows:
        R = unicodedata.normalize("NFC", row["relation"])
        if R in aliases:
            canon = aliases[R]
        elif R in canonical_values:
            canon = R
        else:
            continue
        triple = (row["subject"], canon, row["object"])
        if triple in seen:
            continue
        seen.add(triple)
        unique.append(triple)
    return unique


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


def _entity_nodes(rows: list[dict[str, str]], literal_rels: set[str]) -> set[str]:
    """The engine's ``entity_node/1``, computed literally over engine-input *rows*.

        entity_node(S) :- relation(S, R, O).
        entity_node(O) :- relation(S, R, O), !attr_rel(R).

    Deliberately WITHOUT a truthiness guard: the engine admits every subject
    unconditionally, empty string included, so an empty value is an engine path
    node. Graph membership must be this set — see dependency_graph. Vocabulary is
    a different question and entity_set drops the empty string on top of this."""
    nodes: set[str] = set()
    for row in rows:
        nodes.add(row["subject"])
        if row["relation"] not in literal_rels:
            nodes.add(row["object"])
    return nodes


def entity_set(
    facts: list[dict[str, str]] | None = None,
    attribute_rels: set[str] | None = None,
) -> set[str]:
    """First-class entities only: every subject, plus objects whose relation is
    NOT declared an attribute relation. Objects of attribute relations are
    literal values (see attribute_relations) and are excluded so they don't show
    up as entities (entity listings, path nodes, count subjects). With no
    policy/attribute-relations.md this equals value_set (backward compatible).

    This is ``_entity_nodes`` (the engine's entity_node/1) minus the empty string:
    an incomplete row must not offer "" as a name to list, suggest or accept as a
    query argument. Do NOT reuse this as the entity GRAPH's membership test — the
    engine has no such guard there and the two would diverge (#329).

    *attribute_rels* overrides which relations count as attribute (literal-valued)
    relations; pass a KbContext's attribute_relations() to read a non-default KB.
    None falls back to the module-level (ambient-root) attribute_relations()."""
    selected = engine_input_rows(facts if facts is not None else load_accepted_facts())
    literal_rels = attribute_relations() if attribute_rels is None else attribute_rels
    return {value for value in _entity_nodes(selected, literal_rels) if value}


def allowed_relations(facts: list[dict[str, str]] | None = None) -> set[str]:
    selected = facts if facts is not None else load_facts()
    return {row["relation"] for row in selected if row["relation"]}


def nearby_vocabulary(term: str, vocabulary: set[str], *, limit: int = 3) -> list[str]:
    """Return bounded, deterministic spelling suggestions without rewriting *term*.

    Comparison folds NFC and case; returned values retain the trusted vocabulary's
    spelling.  The small length-relative edit-distance threshold keeps unrelated
    names out of a display-only did-you-mean hint.
    """
    query = unicodedata.normalize("NFC", term).strip().casefold()
    if len(query) < 2 or limit <= 0:
        return []

    def distance(left: str, right: str) -> int:
        previous = list(range(len(right) + 1))
        for i, lch in enumerate(left, 1):
            current = [i]
            for j, rch in enumerate(right, 1):
                current.append(min(previous[j] + 1, current[j - 1] + 1,
                                   previous[j - 1] + (lch != rch)))
            previous = current
        return previous[-1]

    maximum = min(3, max(1, len(query) // 3))
    by_key: dict[str, str] = {}
    for value in vocabulary:
        shown = unicodedata.normalize("NFC", value).strip()
        key = shown.casefold()
        if not shown or key == query:
            continue  # exact/NFC/case-equivalent is never a suggestion
        if key not in by_key or shown < by_key[key]:
            by_key[key] = shown
    scored = [(distance(query, key), shown) for key, shown in by_key.items()]
    return [shown for dist, shown in sorted(
        (item for item in scored if item[0] <= maximum),
        key=lambda item: (item[0], item[1].casefold(), item[1]),
    )[:limit]]


def slugify(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z가-힣]+", "-", value.strip().lower())
    return text.strip("-") or "item"


def normalize_confidence(value: str) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "0.50"
    if not math.isfinite(score):
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
    # Build canonical section: one line per canonical name (sorted), listing its
    # surface variants. Absent alias file → aliases is {} → section is empty →
    # schema_context output is byte-identical to a KB without the file.
    aliases = relation_aliases()
    canonical_names: set[str] = set(aliases.values())
    canonical_lines: list[str] = []
    if canonical_names:
        canonical_lines.append("")
        canonical_lines.append("Canonical relation names (prefer these):")
        for canonical in sorted(canonical_names):
            variants = sorted(surface_variants(canonical, aliases))
            canonical_lines.append(f"- {canonical} <- {', '.join(variants)}")
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
            *canonical_lines,
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
        raise FactlogError("missing policy/prompts/text_to_datalog.md; run factlog init --target <kb>")
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


def dependency_graph(
    facts: list[dict[str, str]],
    attribute_rels: set[str] | None = None,
) -> dict[str, list[str]]:
    """The entity graph: an edge for every engine fact whose OBJECT is an entity.

    A literal value — the object of a declared attribute relation, and a subject
    of nothing (see entity_set) — is NOT an entity, so no edge points at it and it
    can never be a path node. That is exactly what policy/attribute-relations.md
    promises ("kept OUT of the entity set, so they do not show up as entities,
    path nodes, or count subjects"); before #329 the promise held on the entity
    axis only and a date could sit in the middle of a dependency path.

    The membership test is entity_node/1 itself (``_entity_nodes``), not "drop
    every attribute edge": a value that is a subject somewhere, or the object of
    some non-attribute relation, IS a node, and classify_query already admits it
    as a path endpoint. Filtering on anything narrower would put the endpoint
    guard and the path evaluation back into disagreement — the same class of
    divergence, moved.

    It is not entity_set either, close as the two are: entity_set additionally
    drops the empty string, which is right for a vocabulary listing and wrong
    here, because the engine's entity_node/1 has no such guard. Reusing it made
    `ask` render an engine-derived path as "no such fact (verified negative)" on
    a KB with an incomplete row — which run_logic_check reports yet exits 0 on.

    The emitted engine program (WIRELOG_PROGRAM) computes the identical predicate
    as entity_node/1 and gates `edge` on it; keep the two in step.

    *attribute_rels* overrides the declared attribute relations (a KbContext's, or
    a hoisted read); None falls back to the module-level attribute_relations().
    """
    selected = engine_input_rows(facts)
    literal_rels = attribute_relations() if attribute_rels is None else attribute_rels
    nodes = _entity_nodes(selected, literal_rels)
    graph: dict[str, list[str]] = defaultdict(list)
    for row in selected:
        if row["object"] in nodes:
            graph[row["subject"]].append(row["object"])
    return graph


def dependency_path(
    facts: list[dict[str, str]],
    start: str,
    target: str,
    attribute_rels: set[str] | None = None,
) -> list[str]:
    graph = dependency_graph(facts, attribute_rels)
    # The engine defines path/2 only over edges (path(S,O):-edge(S,O) / :-edge(S,M),
    # path(M,O)), so a path requires >= 1 edge: match `target` only AFTER at least
    # one hop. This makes a reflexive path("X","X") a verified negative unless a real
    # cycle leads back to X — never the zero-edge trivial [start] (#256). `seen`
    # guards EXPANSION (not enqueue) so a genuine cycle back to `start`/`target` is
    # still discovered before that node's edges are pruned.
    queue: deque[tuple[str, list[str]]] = deque([(start, [start])])
    seen: set[str] = set()
    while queue:
        node, path = queue.popleft()
        if len(path) > 1 and node == target:
            return path
        if node in seen:
            continue
        seen.add(node)
        for nxt in graph.get(node, []):
            queue.append((nxt, path + [nxt]))
    return []


# ``attr_rel`` is a reserved engine EDB predicate populated from
# policy/attribute-relations.md (see attribute_relation_program). ``entity_node``
# is the engine's copy of entity_set: every subject, plus every object reached by
# a relation that is NOT an attribute relation. ``edge`` is gated on it so a
# literal value can never be a path node — the guarantee the scaffolded
# policy/attribute-relations.md makes to the user (#329). Negation here is
# trivially stratified: attr_rel is pure EDB and is never a rule head.
#
# The python renderer computes the same predicate in dependency_graph. Change one
# and tests/unit/test_attribute_path_exclusion.py::TestEngineAndRendererAgree
# fails; that is deliberate — do not fix one path alone.
WIRELOG_PROGRAM = """
.decl relation(subject: symbol, rel: symbol, object: symbol)
.decl canonical(subject: symbol, rel: symbol, object: symbol)
.decl attr_rel(rel: symbol)
.decl entity_node(name: symbol)
.decl edge(start: symbol, target: symbol)
.decl path(start: symbol, target: symbol)

entity_node(S) :- relation(S, R, O).
entity_node(O) :- relation(S, R, O), !attr_rel(R).
edge(S, O) :- relation(S, R, O), entity_node(O).
path(S, O) :- edge(S, O).
path(S, O) :- edge(S, M), path(M, O).
"""


def attribute_relation_program(names: set[str] | None = None) -> str:
    """The ``attr_rel/1`` EDB block appended to the engine program.

    *names* defaults to the ambient policy/attribute-relations.md declarations.
    Emitted in sorted order so the assembled program text is reproducible, and
    through dl_string so a name carrying a quote or a backslash stays a legal
    atom. Empty (no declarations) → "" , so the program text for a KB with no
    attribute relations is byte-identical to WIRELOG_PROGRAM + policy + accepted
    and every existing path answer is unchanged.
    """
    declared = attribute_relations() if names is None else names
    if not declared:
        return ""
    lines = [f"attr_rel({dl_string(name)})." for name in sorted(declared)]
    return "\n" + "\n".join(lines) + "\n"


def decode_wirelog_value(session: EasySession, value: object) -> object:
    """Resolve a wirelog integer ID back to its interned symbol string.

    Uses the private ``session._intern`` table exposed by pyrewire's EasySession.
    This is a private API (underscore-prefixed), intentionally pinned to
    ``pyrewire>=1.0.3,<2.0`` in pyproject.toml to guard against breakage if the
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


def _project_typed_relations(session, specs, accepted) -> None:
    """Insert each parseable typed-relation object into its int64 side-relation,
    deterministically ordered so the run is reproducible (#116 invariant 3). A
    non-parsing object warns and skips ONLY that row — the fact still loads
    untyped via relation/3 (#116 invariant 4). Scalars are bare ints and must
    NEVER be interned.

    Touches *session* only via intern/insert — no step/close — so it is
    unit-testable with a fake session and no engine.

    NB: hand-authored comparison-predicate rules (#120) use arity-2
    (subject, reason) heads with a quoted reason string; the scalar stays in
    the body. A bare scalar in a head would be mis-decoded as an interned
    symbol by decode_wirelog_value (it round-trips ints through the intern
    table), so it must never appear there. Those rules live in the optional
    policy/logic-policy.extra.dl, not here.
    """
    if not specs:
        return
    for row in sorted(accepted, key=lambda r: (r["relation"], r["subject"], r["object"])):
        spec = specs.get(row["relation"])
        if spec is None or spec.type not in _TYPED_COL:
            continue
        scalar = literal_types.normalize(spec.type, row["object"], spec.units)
        if scalar is None:
            # This is the one surfacing path that runs unconditionally, and the
            # remedy it points at ("correct the source to ASCII and re-collect")
            # is unusable unless the reader can see WHICH character is wrong.
            # repr cannot: '1２3억' and '123억' are indistinguishable in most
            # fonts. Appended, not substituted, so every other not-parsing value
            # (a typo, an 'n/a') reads byte-identically to before.
            marked = (
                f" (non-ASCII digits: {literal_types.mark_non_ascii_digits(row['object'])})"
                if literal_types.has_non_ascii_digits(row["object"])
                else ""
            )
            print(
                f"typed-relations: {row['object']!r} for {row['relation']!r} "
                f"({row['subject']!r}) does not parse as {spec.type}{marked}; loading untyped",
                file=sys.stderr,
            )
            continue
        # Defensive: every _TYPED_COL is an int64 column. pyrewire silently
        # accepts a float into an int64 column (wrong comparison), so if a
        # future normalizer ever leaks a non-int, skip + warn loudly rather
        # than insert a silently-wrong value.
        if not isinstance(scalar, int):
            print(
                f"typed-relations: {row['object']!r} for {row['relation']!r} "
                f"({row['subject']!r}) normalized to non-int {scalar!r}; skipping",
                file=sys.stderr,
            )
            continue
        if not (-(2**63) <= scalar < 2**63):
            print(
                f"typed-relations: {row['object']!r} for {row['relation']!r} "
                f"({row['subject']!r}) = {scalar} out of int64 range; skipping",
                file=sys.stderr,
            )
            continue
        session.insert(spec.alias, (session.intern(row["subject"]), scalar))


def run_wirelog() -> dict[str, set[tuple[str, ...]]]:
    require_pyrewire_version()

    if not ACCEPTED_DL.is_file():
        raise FactlogError("missing facts/accepted.dl; run tools/compile_facts.py first")

    accepted_program = ACCEPTED_DL.read_text(encoding="utf-8")
    policy_program = load_logic_policy()
    specs = typed_relations()
    # attr_rel/1 is EDB, so it must be populated before the engine runs: it is what
    # keeps a declared literal out of entity_node — and therefore out of path (#329).
    # "" when nothing is declared, leaving the appended block empty.
    base_program = (
        WIRELOG_PROGRAM
        + attribute_relation_program()
        + "\n"
        + policy_program
        + "\n"
        + accepted_program
    )
    if specs:
        _assert_no_alias_collision(specs, base_program)
        # Fail loud BEFORE handing a float-bearing program to the engine: a
        # number alias compared against an unscaled float in extra.dl would
        # ParseError-reject the whole program (#125 scaled-×1000 contract).
        extra_dl = LOGIC_POLICY_DL.with_name("logic-policy.extra.dl")
        if extra_dl.is_file():
            _assert_no_unscaled_number_threshold(
                specs, extra_dl.read_text(encoding="utf-8")
            )
        # Every literal_types.TYPES member is now projectable (date/ordinal/number/
        # amount all map to int64 in _TYPED_COL), and _parse_typed_relations drops
        # any tag outside TYPES at parse time — so a spec is always projectable.
        assert all(spec.type in _TYPED_COL for spec in specs.values())
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

    # Intern canonical-atom symbols so decode_wirelog_value round-trips for any
    # canonical/3 tuple the engine emits or a rule references.  canonical/3 is
    # pure EDB — never a rule head — so we only intern, never insert.
    _c_aliases = relation_aliases()
    if _c_aliases:
        for s, canon, o in canonical_atoms(accepted, _c_aliases):
            session.intern(s)
            session.intern(canon)
            session.intern(o)

    _project_typed_relations(session, specs, accepted)

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


def _canonical_value(value: str) -> str:
    """Canonicalise a literal value for comparison so unit quoting does not change
    a match. An ``amount`` compound term is normalised to its always-quoted
    canonical form (``amount(7,억)`` / ``amount(7,"억")`` -> ``amount(7,"억")``),
    the same form merge stores — so a query literal matches the stored object
    whether or not the author quoted the unit.

    NFC folding at the single query-value comparison chokepoint (#213): every
    query-match path routes value comparison through here, so folding once here
    makes an NFD-stored relation or object meet an NFC-typed query constant (and
    the reverse) without touching any per-path code. macOS text is routinely NFD,
    so an NFD-stored fact would otherwise never meet an NFC-typed query constant.
    Idempotent no-op on NFC-only data, so a KB that was already NFC compares
    byte-identically. Non-amount strings are otherwise returned unchanged, so
    dates/numbers/ordinals/entities keep their form. Total: never raises."""
    nfc = unicodedata.normalize("NFC", value)
    return literal_types.canonical_amount(nfc) or nfc


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


# label -> (expected argument count, the message for a query that misses it).
# The label is also what _query_shape_error puts in front of its message, so one
# label selects both of a predicate's signature rules.
_QUERY_ARITY_RULES: dict[str, tuple[int, str]] = {
    "relation": (3, "relation query must have subject, relation, and object arguments"),
    "path": (2, "path query must have start and target arguments"),
    "count": (2, "count query must have subject and relation arguments"),
    "policy query": (2, "policy query must have entity and reason arguments"),
}


def _query_arity_error(label: str, args: Sequence[str]) -> str | None:
    """The bad-arity verdict on *args*, or None when the count is right.

    Paired with `_query_shape_error` and always applied FIRST: a line that
    violates both rules must get the same reason from the gate, the report and
    the router, and the gate reports arity first. Checking shape first is not
    merely a different opinion — it renames the defect. `relation()?` is a
    zero-argument query, and calling it "arguments must be variables or quoted
    strings" tells the author to fix the quoting of arguments that are not there.
    """
    arity, message = _QUERY_ARITY_RULES[label]
    return None if len(args) == arity else message


def _query_shape_error(label: str, args: Sequence[str]) -> str | None:
    """The malformed-shape verdict on *args*, or None when every one is valid.

    *label* names the query in the message ("relation", "path", "count",
    "policy query"), which is the ONLY thing that differs between the four
    branches — the rule itself is `_is_valid_arg` for every one of them.

    Returning the message rather than a bool is what keeps the three consumers
    honest. classify_query (the gate), run_logic_check (the report) and
    ask_router.evaluate (`ask`) must reject the SAME lines with the SAME wording;
    when each restated the rule, the report's count branch drifted into accepting
    lines the gate calls malformed and answering them with a wrong aggregate
    (#328). A caller that phrases its own message can drift again, so callers
    pass this string through.
    """
    if all(_is_valid_arg(arg) for arg in args):
        return None
    return f"{label} arguments must be variables or quoted strings"


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
#   is_valid_arg(arg)      -> True if arg is a well-formed query argument
#   query_arity_error(label, args) -> the bad-arity message, or None
#   query_shape_error(label, args) -> the malformed-shape message, or None
#   quoted_constants(line) -> every "..." literal in a line
#
# `is_valid_arg` is the SINGLE definition of "a query argument is a variable or a
# double-quoted string", the rule every classify_query branch applies, and
# `query_shape_error` is the single place that turns it into a verdict + message.
# Both are exported so the report (tools/run_logic_check.py) and the router
# (tools/ask_router.py) apply the same predicate instead of restating it: a
# second copy is exactly how the count branch came to accept lines the gate
# rejects as malformed (#328).
query_args = _query_args
arg_value = _arg_value
canonical_value = _canonical_value
is_quoted_string = _is_quoted_string
is_variable = _is_variable
is_valid_arg = _is_valid_arg
query_arity_error = _query_arity_error
query_shape_error = _query_shape_error
quoted_constants = _quoted_constants


def _relation_match_count(
    query: str,
    facts: list[dict[str, str]],
    aliases: dict[str, str] | None = None,
) -> int:
    if query.startswith("relation"):
        args = _query_args(query)
        if len(args) != 3:
            return 0
        # Pre-compute surface variants for the relation argument when it is a
        # quoted canonical name (i.e. its surface_variants set is non-empty).
        # This lets a canonical query count surface-variant rows so the validator
        # returns QUERY_OK (not QUERY_FACT_ABSENT) when matching rows exist.
        # *aliases* lets classify_query share the single relation_aliases() read
        # it already performed for the canonical-acceptance check (#242); None
        # keeps the original lazy per-call fetch, so the read (and its
        # raise-on-malformed-file) stays gated to a quoted relation arg exactly
        # as before — no new call path is exposed to that raise.
        rel_arg = args[1]
        canonical_variants: set[str] = set()
        if _is_quoted_string(rel_arg):
            _aliases = relation_aliases() if aliases is None else aliases
            # Folded so a stored surface variant in the other normal form still
            # counts: the alias keys are NFC, a stored row need not be (#213).
            canonical_variants = {
                _canonical_value(v)
                for v in canonical_variants_of(_arg_value(rel_arg), _aliases)
            }
        count = 0
        for row in facts:
            s_arg, r_arg, o_arg = args
            s_val, r_val, o_val = row["subject"], row["relation"], row["object"]
            if not (_is_variable(s_arg) or _canonical_value(_arg_value(s_arg)) == _canonical_value(s_val)):
                continue
            # Relation: match exact canonical name OR any surface variant.
            if not (_is_variable(r_arg) or
                    _canonical_value(_arg_value(r_arg)) == _canonical_value(r_val) or
                    _canonical_value(r_val) in canonical_variants):
                continue
            if not (_is_variable(o_arg) or _canonical_value(_arg_value(o_arg)) == _canonical_value(o_val)):
                continue
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
    # Read the attribute-relation policy ONCE and reuse it for both the entity
    # membership check and the path evaluation below, so the endpoint guard and
    # dependency_path can never disagree about which relations are attributes
    # (and a path query costs one policy read, not two).
    _attribute_rels = attribute_relations()
    entities = entity_set(facts, _attribute_rels)
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
        arity_error = _query_arity_error("relation", args)
        if arity_error:
            return False, QUERY_BAD_ARITY, arity_error
        shape_error = _query_shape_error("relation", args)
        if shape_error:
            return False, QUERY_MALFORMED, shape_error
        subject, relation, object_ = args
        # Entity membership must fold on both sides too: accepted facts can carry
        # an NFD-authored subject, while an NFC query must still reach the folded
        # relation/object match below.
        if not _is_variable(subject) and _canonical_value(_arg_value(subject)) not in {
            _canonical_value(entity) for entity in entities
        }:
            return False, QUERY_ENTITY_NOT_ACCEPTED, f"relation subject is not an accepted entity: {_arg_value(subject)}"
        # Read relation_aliases() at most once per relation query and hand it to
        # _relation_match_count below: the canonical-acceptance check here and the
        # match count were the two sites that each re-read it per relation query
        # (#242). The read stays gated to a quoted canonical not literally in
        # accepted.dl, so which queries can trigger its raise-on-malformed-file is
        # unchanged (a variable/known-variant relation never reads it here).
        _rel_aliases: dict[str, str] | None = None
        # Membership goes through _canonical_value on BOTH sides, like the object
        # check below: allowed_relations() returns raw stored names, so an
        # NFD-stored relation would otherwise miss an NFC-typed query and be
        # rejected here — before _relation_match_count (which does fold) is ever
        # reached (#213).
        if not _is_variable(relation) and _canonical_value(_arg_value(relation)) not in {
            _canonical_value(r) for r in relations
        }:
            # A declared canonical name (one whose surface_variants is non-empty)
            # counts as accepted even though the canonical itself may not appear
            # literally in accepted.dl — the stored facts use surface variants.
            # canonical_variants_of NFC-normalizes its argument and relation_aliases()
            # NFC-normalizes both the keys and the canonical targets on load, so this
            # lookup is already form-insensitive.
            _rel_aliases = relation_aliases()
            if not canonical_variants_of(_arg_value(relation), _rel_aliases):
                return False, QUERY_RELATION_NOT_ACCEPTED, f"relation name is not accepted: {_arg_value(relation)}"
        if not _is_variable(object_) and _canonical_value(_arg_value(object_)) not in {
            _canonical_value(v) for v in values
        }:
            return False, QUERY_ENTITY_NOT_ACCEPTED, f"relation object is not an accepted entity: {_arg_value(object_)}"
        if _relation_match_count(query, facts, _rel_aliases) == 0:
            return False, QUERY_FACT_ABSENT, "relation query does not match accepted facts"
        return True, QUERY_OK, "passed"
    if predicate == "path":
        arity_error = _query_arity_error("path", args)
        if arity_error:
            return False, QUERY_BAD_ARITY, arity_error
        shape_error = _query_shape_error("path", args)
        if shape_error:
            return False, QUERY_MALFORMED, shape_error
        for arg in args:
            if not _is_variable(arg) and _arg_value(arg) not in entities:
                return False, QUERY_ENTITY_NOT_ACCEPTED, f"path argument is not an accepted entity: {_arg_value(arg)}"
        if all(_is_quoted_string(arg) for arg in args) and not dependency_path(
            facts, _arg_value(args[0]), _arg_value(args[1]), _attribute_rels
        ):
            return False, QUERY_FACT_ABSENT, "path query does not match accepted facts"
        return True, QUERY_OK, "passed"
    if predicate == "count":
        # count(subject, relation)? — how many objects (subject, relation) has.
        # A valid count always has an answer (0 is a verified zero, never a
        # FACT_ABSENT), so it is QUERY_OK whenever the vocabulary is accepted.
        arity_error = _query_arity_error("count", args)
        if arity_error:
            return False, QUERY_BAD_ARITY, arity_error
        shape_error = _query_shape_error("count", args)
        if shape_error:
            return False, QUERY_MALFORMED, shape_error
        subject, relation = args
        # Keep count aligned with relation queries: a subject is accepted across
        # NFC/NFD forms before the relation-name check runs.
        if not _is_variable(subject) and _canonical_value(_arg_value(subject)) not in {
            _canonical_value(entity) for entity in entities
        }:
            return False, QUERY_ENTITY_NOT_ACCEPTED, f"count subject is not an accepted entity: {_arg_value(subject)}"
        # Same folded membership as the relation branch above — a count over an
        # NFD-stored relation must accept an NFC-typed query name (#213).
        if not _is_variable(relation) and _canonical_value(_arg_value(relation)) not in {
            _canonical_value(r) for r in relations
        }:
            # A declared canonical name (one whose surface_variants is non-empty)
            # counts as accepted even though the canonical itself may not appear
            # literally in accepted.dl — the stored facts use surface variants.
            if not canonical_variants_of(_arg_value(relation), relation_aliases()):
                return False, QUERY_RELATION_NOT_ACCEPTED, f"count relation is not accepted: {_arg_value(relation)}"
        return True, QUERY_OK, "passed"
    if predicate in policy_query_predicates:
        arity_error = _query_arity_error("policy query", args)
        if arity_error:
            return False, QUERY_BAD_ARITY, arity_error
        shape_error = _query_shape_error("policy query", args)
        if shape_error:
            return False, QUERY_MALFORMED, shape_error
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
