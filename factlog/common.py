# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import csv
import decimal
import functools
import json
import math
import os
import re
import sys
import unicodedata
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from factlog import literal_types
from factlog.ingest import TEXT_CONTAINER_EXTS

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
ENGINE_STATUSES = frozenset({"confirmed", "accepted"})
REVIEW_STATUSES = frozenset({"needs_review", "candidate"})
# A row a human (or a resolution step) has marked as replaced by a newer fact.
# Superseded rows are retained in candidates.csv for audit but are NOT engine
# input (they never reach accepted.dl) and are ignored by conflict detection.
SUPERSEDED_STATUSES = frozenset({"superseded"})
# The whole status vocabulary. Anything outside it is unrecognised (a typo).
# Consumers MUST derive from this rather than restate the members: a tool that
# spelled the set out by hand omitted `superseded` and warned once per retired
# row (#208). Frozen so this union can never drift from the sets it snapshots.
# A new `*_STATUSES` set must be added to the union — a unit test enforces it.
KNOWN_STATUSES = frozenset(ENGINE_STATUSES | REVIEW_STATUSES | SUPERSEDED_STATUSES)
QUERY_PREDICATES = {"relation", "path", "count", "review_required"}
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
        return _relation_names_from(self.policy_dir / "attribute-relations.md")

    def relation_aliases(self) -> dict[str, str]:
        # Resolving THIS KB's attribute relations needs THIS KB's alias map; reading
        # it from the ambient root made one KB answer differently under --target than
        # under FACTLOG_ROOT (#226).
        return relation_aliases(self.root)

    def typed_relations(self) -> dict[str, TypedRelSpec]:
        path = self.policy_dir / "typed-relations.md"
        if not path.is_file():
            return {}
        reserved = _typed_reserved_names(
            relations=_try(lambda: allowed_relations(self.load_facts())),
            predicates=_try(lambda: policy_predicates(self.load_logic_policy())),
        )
        specs = _parse_typed_relations(path.read_text(encoding="utf-8"), reserved)
        _warn_typed_not_attribute(specs, self.attribute_relations(), self.relation_aliases())
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
    so every basename-keyed consumer (paired_conversion, eject) is unaffected by
    the header format — the subdir that #214 encodes lives in the conversion's
    own mirrored path, not in this pairing signal.
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


def is_hidden_source(path: Path, source_root: Path) -> bool:
    """True when *path* has a dot-prefixed component below *source_root* (#67).

    "Hidden" under sources/ means ANY path component beneath the source root
    starts with '.', so a top-level `.DS_Store` is hidden and so is every file
    inside a hidden directory — `sources/.provenance/x.json`, a nested
    `sources/.git/…` checkout, `sources/.obsidian/…` editor state. This is the
    single definition shared by every sources/ enumerator; before #67 four call
    sites checked only `path.name`, so a file inside a hidden *directory* was
    treated as a real source by `factlog sources`/`ingest --scan`/eject while
    sync and coverage (which check every component) skipped it — the counts
    disagreed and the `[no facts]` hint pointed at a file sync would never touch.

    A source whose own name begins with '.' (`sources/.hidden.md`) is likewise
    hidden: factlog has never enumerated dot-prefixed source filenames, and this
    keeps the one rule uniform across depths.
    """
    return any(part.startswith(".") for part in path.relative_to(source_root).parts)


def walk_source_dir(
    base: Path, *, include_hidden: bool, suffix: str | None = None
) -> list[Path]:
    """Every file under one source directory *base*, sorted; the KB's single directory walk.

    This is the one ``rglob`` over a ``sources/`` tree, shared so the two questions a source
    walk can ask cannot silently diverge (#142). *include_hidden* is the explicit answer to
    which question the caller is asking — the one axis on which the two legitimately differ:

    * ``include_hidden=False`` answers *"is this a source?"* (``source_files``, and through it
      ``provenance_sources`` / coverage / status). A dot-prefixed component below the source
      root is **not** a source (#67, :func:`is_hidden_source`): nothing syncs it, nothing
      gives it a ledger.
    * ``include_hidden=True`` answers *"does a file on disk already claim this name/identity?"*
      (the importer index, ``BaseSourceWriter._index``). A hidden ``.md`` can be duplicated
      whether or not ``sync`` counts it, so skipping it makes a re-import write a *second*
      ``.md`` and breaks P3 idempotence in silence (#112).

    Making the difference a *parameter* rather than a second hand-rolled ``rglob`` is the
    point: a future change to either question moves this one function or fails, so the walks
    can never quietly diverge again. *suffix* (e.g. ``".md"``) restricts to one extension;
    ``None`` yields every file. Directories are always excluded (``is_file``), so a directory
    whose name happens to end in *suffix* is never mistaken for a source.
    """
    if not base.is_dir():
        return []
    pattern = f"*{suffix}" if suffix else "*"
    return sorted(
        path for path in base.rglob(pattern)
        if path.is_file() and (include_hidden or not is_hidden_source(path, base))
    )


def source_files(root: Path) -> list[Path]:
    """Every real source file under the KB's SOURCE_ROOTS, hidden paths excluded.

    Hidden files (any dot-prefixed component below the source root — see
    is_hidden_source) are filtered here, at the single enumeration point, so no
    caller can forget the rule and disagree about what counts as a source (#67).
    """
    files: list[Path] = []
    for rel in SOURCE_ROOTS:
        files.extend(walk_source_dir(root / rel, include_hidden=False))
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
    """Is *path* ingestible AS TEXT, exactly as extraction reads it?

    Content decides — except for the text-based CONTAINERS (`.html`, `.htm`,
    `.rtf`). Their bytes are text, so a content sniff called them ingestible, and
    every consumer of this function then agreed: `--scan` skipped converting them,
    the "unconverted binary" warning ignored them, and coverage/status filed the
    raw original as a text source to extract from — so RTF control words and HTML
    tags went into extraction as if they were prose (#222).
    
    They are NOT ingestible as text. Declaring that here, once, is what keeps the
    four consumers in step; the first fix hand-rolled the exception at two call
    sites and the other two silently kept the old answer.

    The in-session fact extraction reads each sources/ file as text, so a file is
    only ingestible if it decodes as text. A file is treated as non-text when its
    first *sniff* bytes contain a NUL byte or do not decode as UTF-8. A multi-byte
    UTF-8 sequence truncated at the sniff boundary is tolerated *only* when the
    file actually extends past the boundary; for a fully-read short file an
    invalid trailing byte means binary. Detection is content-based, so binary
    formats (.docx, .pdf, images, ...) are flagged regardless of their extension.
    """
    if path.suffix.lower() in TEXT_CONTAINER_EXTS:
        return False
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
    return dedup_engine_atoms(rows)


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
    # Guard: canonical is a reserved EDB predicate; a head occurrence in policy
    # text silently corrupts the engine program (pyrewire treats canonical as IDB
    # and drops all compile-emitted EDB atoms). Fail loud here, after the full
    # policy text (base + extra.dl) is assembled, so the check covers both files.
    _assert_no_canonical_head(text)
    return text


def load_logic_policy() -> str:
    return _load_logic_policy_from(LOGIC_POLICY_DL)


def policy_predicates(policy_program: str | None = None) -> set[str]:
    text = policy_program if policy_program is not None else load_logic_policy()
    built_in = {"relation", "edge", "path", "attr_rel"}
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
            names.add(unicodedata.normalize("NFC", name))
    return names


def identity_relations(root: Path | None = None) -> set[str]:
    """Relations whose OBJECT identifies its SUBJECT (policy/identity-relations.md).

    A title or a DOI names exactly one paper; a publication year or a study type
    does not. The distinction changes what a value collision MEANS: in an identity
    relation two subjects sharing a value are probably two records of one thing; in
    any other relation they are one value split across two spellings — a query leak.

    This is DECLARED, not inferred. Deriving it from the data (a relation is an
    identity iff every value has one subject) is self-defeating: a single genuine
    duplicate record makes the relation non-injective, which flips it to
    categorical, which makes duplicate records fail the gate — the very thing the
    classification exists to avoid. A small KB is also injective by accident.

    Same one-name-per-line format as attribute-relations.md. Absent file → empty
    set → every relation is categorical, so a collision is reported as a leak (the
    conservative direction: a false leak is noisy, a missed leak is silent).
    """
    path = (root / "policy" / "identity-relations.md") if root else (POLICY_DIR / "identity-relations.md")
    return _relation_names_from(path)
# --- value hierarchy (policy/value-hierarchy.md) -----------------------------
# Declares that one OBJECT value of a relation is a kind of another — e.g. a
# cohort study is an observational study. Without it, `연구유형 = 관찰연구` and
# `연구유형 = 코호트연구` are unrelated strings, so asking for the broader value
# silently misses every row filed under a narrower one (#211). The declaration
# is applied when a query's object is MATCHED; it never adds facts to
# accepted.dl, which stays a 1:1 projection of the accepted candidate rows.

# `<:` and `<` are ASCII spellings of `⊂`. Backtick-quoted names are lifted out
# BEFORE the operator is looked for, so a value may itself contain a '<' or ':'.
_SUBSUMES_RE = re.compile(r"^(?P<child>.+?)\s*(?:⊂|<:|<)\s*(?P<parent>.+)$")
_BACKTICKED_RE = re.compile(r"`([^`]+)`")


def _hierarchy_line(stripped: str) -> tuple[str, str, str] | None:
    """Parse one declaration into (relation, child, parent), or None.

    Backticked names are extracted first and replaced by placeholders, so the
    ':' and '⊂' splits can never cut through a quoted value. Returns None for a
    line that is not a declaration; the caller decides whether that is a comment
    or a mistake worth warning about.
    """
    quoted: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        quoted.append(match.group(1).strip())
        return f"\x00{len(quoted) - 1}\x00"

    masked = _BACKTICKED_RE.sub(_stash, stripped)

    def _restore(text: str) -> str:
        return re.sub(r"\x00(\d+)\x00", lambda m: quoted[int(m.group(1))], text).strip()

    if ":" not in masked:
        return None
    relation_part, _, rest = masked.partition(":")
    match = _SUBSUMES_RE.match(rest.strip())
    if not match:
        return None
    relation = _restore(relation_part)
    child = _restore(match.group("child"))
    parent = _restore(match.group("parent"))
    if not relation or not child or not parent:
        return None
    return relation, child, parent


def _hierarchy_declarations(root: Path | None = None) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Read the policy file: (declarations, warnings-about-unparsable-lines).

    Names are NFC-normalised on load, exactly as relation_aliases() does: a
    policy file authored on macOS is NFD, accepted facts are NFC, and comparing
    the two raw would make every declaration silently do nothing — the same quiet
    no-op this feature exists to remove.
    """
    path = (root / "policy" / "value-hierarchy.md") if root else (POLICY_DIR / "value-hierarchy.md")
    if not path.is_file():
        return [], []

    declarations: list[tuple[str, str, str]] = []
    warnings: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = re.sub(r"^\s*[-*]\s+", "", line.strip()).strip()
        if not stripped or stripped.startswith("#"):
            continue
        parsed = _hierarchy_line(stripped)
        if parsed is None:
            warnings.append(f"value-hierarchy line {lineno} is not a declaration, ignored: {stripped}")
            continue
        relation, child, parent = (unicodedata.normalize("NFC", part) for part in parsed)
        if child == parent:
            warnings.append(f"value-hierarchy line {lineno} declares a value as its own parent, ignored: {stripped}")
            continue
        declarations.append((relation, child, parent))
    return declarations, warnings


def value_hierarchy(root: Path | None = None) -> dict[str, dict[str, set[str]]]:
    """Parse policy/value-hierarchy.md → {relation: {child_value: {ancestors}}}.

    Line format (bullets and '#' comments allowed; backtick-quote a name that
    contains spaces, a ':' or a '<'):

        - 연구유형: 코호트연구 ⊂ 관찰연구
        - 대상질환: `emphysema` <: COPD

    Ancestors are TRANSITIVE (a ⊂ b and b ⊂ c ⇒ a query for c matches an a row).

    A CYCLE IS DROPPED — every value on it, not just the self-edge. Keeping a
    cycle would make subsumption mutual (a query for the narrow value returning
    the broad one), silently breaking the one-way contract this feature is built
    on. The dropped values are reported by value_hierarchy_warnings, so the
    mistake surfaces instead of quietly changing what queries mean.

    Absent file → {} → behaviour byte-identical to a KB without the feature.
    """
    hierarchy, _ = _closed_hierarchy(root)
    return hierarchy


def _closed_hierarchy(root: Path | None = None) -> tuple[dict[str, dict[str, set[str]]], list[str]]:
    declarations, warnings = _hierarchy_declarations(root)

    direct: dict[str, dict[str, set[str]]] = {}
    for relation, child, parent in declarations:
        direct.setdefault(relation, {}).setdefault(child, set()).add(parent)

    closed: dict[str, dict[str, set[str]]] = {}
    for relation, edges in direct.items():
        # Ancestors of each value, by walking parents. A value that reaches
        # itself sits on a cycle.
        reach: dict[str, set[str]] = {}
        for child in edges:
            seen: set[str] = set()
            frontier = set(edges[child])
            while frontier:
                node = frontier.pop()
                if node in seen:
                    continue
                seen.add(node)
                frontier |= edges.get(node, set()) - seen
            reach[child] = seen

        cyclic = {child for child, ancestors in reach.items() if child in ancestors}
        if cyclic:
            warnings.append(
                f"value-hierarchy: '{relation}' declares a cycle through "
                f"{', '.join(sorted(cyclic))} — those declarations are IGNORED "
                f"(subsumption must stay one-way)"
            )
        table = {
            child: ancestors - cyclic
            for child, ancestors in reach.items()
            if child not in cyclic and ancestors - cyclic
        }
        if table:
            closed[relation] = table
    return closed, warnings


@functools.lru_cache(maxsize=8)
def _alias_canonicals_cached(items: tuple) -> frozenset:
    return frozenset(v for _, v in items)


def _alias_canonicals(aliases: dict[str, str]) -> frozenset:
    """The canonical names in an alias map, without rebuilding the set per row.

    _canonicalize runs once per fact; rebuilding set(aliases.values()) inside it made
    detect_conflicts O(rows x aliases), and status now calls it on every run.
    """
    return _alias_canonicals_cached(tuple(sorted(aliases.items())))


def _canonicalize(relation: str, aliases: dict[str, str]) -> str:
    """Return the canonical relation name when *relation* participates in the
    alias map; otherwise return *relation* verbatim (NFD-preserving).

    Participation mirrors ``common.canonical_atoms``:

    * relation is an alias **key** (raw predicate) → ``aliases[NFC(relation)]``
    * relation **is** a canonical value (stored literally) → its NFC form
    * relation is not in the alias map → verbatim (no normalization)

    When *aliases* is empty the function short-circuits and returns *relation*
    unchanged, preserving byte-identical behaviour for KBs without a
    relation-aliases.md file.
    """
    if not aliases:
        return relation
    rn = unicodedata.normalize("NFC", relation)
    if rn in aliases:
        return aliases[rn]
    if rn in _alias_canonicals(aliases):
        return rn
    return relation


def resolve_relation(name: str, aliases: dict[str, str]) -> str:
    """THE alias probe: the canonical name *name* maps to, or *name* itself when it
    is not a declared alias key.

    The alias map is keyed by NFC names (``relation_aliases`` normalizes on load),
    so the lookup MUST fold NFC first — an NFD-authored name would otherwise miss
    the map, fall through to its raw self, and re-create the per-axis "unfolded
    alias" split this converges away. #307/#310/#314/#324/#325 were each one more
    call site remembering (or forgetting) the fold on its own; routing every raw
    ``aliases.get(...)`` probe through here makes the fold un-forgettable (#343).

    Distinct from ``_canonicalize``: this is the bare probe — a drop-in for
    ``aliases.get(name, name)`` — used where a caller wants "the query name this
    row answers to". It does NOT special-case a name that is itself a canonical
    value; callers needing that membership keep using ``_canonicalize`` /
    ``_alias_canonicals``.
    """
    return aliases.get(unicodedata.normalize("NFC", name), name)


def _group_key(obj: str, spec: TypedRelSpec | None) -> tuple:
    """Return the equivalence key an *object* string is grouped under.

    For a relation declared typed (#116), the object's canonical scalar
    (``literal_types.normalize``) is the key, so equivalent notations of the same
    value (e.g. ``amount(5400,"억")`` and ``amount(0.54,"조")`` -> 5.4e11) collapse
    to one value instead of firing a false CONFLICT. ``amount`` needs its unit
    table, so ``spec.units`` is passed through.

    Falls back to the raw object string when the relation is untyped OR the value
    does not parse (normalize -> None): backward-compatible, lossless degrade. The
    two key spaces are tagged (``"scalar"`` vs ``"raw"``) so a scalar never
    collides with an unrelated raw string. Total: never raises (normalize is
    total).

    **ordinal unit loss (#218 / #224 A):** ``normalize("ordinal", …)`` keeps only
    the integer *rank* — the ordinal-class unit (호/위/번/차/등/째) is dropped at
    parse time (``literal_types.parse_ordinal``), so it never enters the key. A
    cross-unit pair therefore collapses onto one scalar: ``제3호`` and ``3위`` both
    key as ``("scalar", 3)``. This is **by design** and consistent with the engine,
    which likewise compares ordinals on rank alone (``_TYPED_COL["ordinal"]`` is a
    bare int64, no unit column). ordinal is a *rank-only* contract: same rank =
    same value. If two notations denote genuinely different domains (a rank vs a
    house number), that distinction belongs in the model — declare them as
    **separate relations**, not one single-valued ordinal relation. (Contrast
    ``amount``, where 억↔조 equivalence is the intended collapse.)

    **int64 divergence note (#224 C):** ``normalize`` can return a scalar wider
    than int64 (mainly ``number`` via ``parse_number_scaled``, and unbounded
    ``ordinal`` ranks — both lack a range guard; ``amount`` already degrades to raw
    when ``parse_amount`` overflows, #205). The engine, by contrast, **skips insertion** of an out-of-int64-range
    scalar (see ``insert_typed_facts`` in ``common.py`` ~ the ``-(2**63) <= scalar
    < 2**63`` guard). So this checker may group under a scalar the engine would
    drop. That affects **grouping only** (never insertion) and is harmless: the
    checker is strictly more willing to merge equivalents, never less. No behaviour
    change here — note only."""
    if spec is not None:
        scalar = literal_types.normalize(spec.type, obj, spec.units)
        if scalar is not None:
            return ("scalar", scalar)
    # Untyped (or unparseable) values group on their NFC form, so NFC- and NFD-
    # authored spellings of one value collapse into a single key instead of a false
    # conflict/competition (#307). Pure NFC only — NOT canonical_value, whose amount
    # normalization would fold amount-shaped strings of an UNTYPED relation and leak
    # scalar equivalence into predicates that never declared it (#224/#218). The
    # reported value stays verbatim: callers keep the raw strings per key and report
    # a deterministic representative (min).
    return ("raw", unicodedata.normalize("NFC", obj))


def detect_conflicts(
    facts: list[dict[str, str]],
    single_valued: set[str],
    typed: dict[str, TypedRelSpec] | None = None,
    aliases: dict[str, str] | None = None,
    hierarchy: dict[str, dict[str, set[str]]] | None = None,
) -> dict[tuple[str, str], list[str]]:
    """Map (subject, canonical_relation) -> sorted distinct *display* objects,
    for single-valued relations that hold more than one *distinct value* (a
    contradiction).

    Distinctness is judged on the canonical grouping key (typed scalar when
    available, else the raw string — see ``_group_key``), so equivalent typed
    notations do not false-positive. The reported values, however, preserve the
    original object strings (provenance): each distinct key contributes one
    deterministic representative (the lexicographically smallest raw object seen
    for it). Deterministic; never raises.

    Two grouping subtleties documented on ``_group_key``: ordinal collapses
    cross-unit notations onto the shared rank (rank-only contract, #218/#224 A),
    and a scalar wider than int64 groups here even though the engine skips its
    insertion (harmless grouping-only divergence, #224 C).

    **Alias canonicalization (#227):** when *aliases* is provided (non-empty),
    each row's relation is canonicalized via ``_canonicalize`` before the
    single-valued membership test and before grouping.  This causes surface
    variants that map to the same canonical name (e.g. ``게재연도`` and ``발행년도``
    both aliased to ``published_year``) to collide under one key, so a cross-
    variant contradiction is detected as a single conflict on the canonical
    name.  Relations that do **not** participate in the alias map are passed
    through verbatim (no normalization), preserving byte-identical behaviour for
    those predicates.

    When *aliases* is ``None`` or ``{}`` the bucket key folds BOTH the subject and
    the relation to their NFC form (and the object is grouped on its NFC form too,
    #307), so NFC- and NFD-authored spellings of one ``(subject, relation, value)``
    collapse into a single bucket instead of splitting a cross-spelling
    contradiction across two that each look conflict-free (#295/#310). Neither the
    reported subject nor the reported relation is silently coerced to NFC: each
    folded bucket reports the deterministic representative ``min(raw spellings
    seen)`` on each axis, so provenance stays a spelling that actually occurs in the
    data (#227). The single-valued MEMBERSHIP gate likewise folds both the declared
    ``single_valued`` set and each fact's ``canon`` to NFC, so a declaration and a
    fact that disagree on Unicode form still match — a policy-loaded (NFC)
    declaration catching an NFD-authored fact relation, and its mirror (#285/#210).

    **Typed-spec lookup (#210):** the ``typed`` dict is keyed by NFC-normalized
    names (``typed_relations`` normalizes at ``common._parse_typed_relations``).
    The lookup first tries the canonical relation name (already NFC when it came
    from the alias map), then falls back to the NFC form of the raw relation
    string.  This ensures that an NFD-authored relation that also participates in
    the alias map still reaches its typed spec, so equivalent notations (억↔조)
    collapse correctly."""
    typed = typed or {}
    aliases = aliases or {}
    hierarchy = value_hierarchy() if hierarchy is None else hierarchy
    # Precompute the set of canonical single-valued relation names so the
    # per-row membership test is O(1). Folded to NFC so membership is decided on
    # the canonical Unicode form regardless of how either side was authored: the
    # declaration may arrive NFC (loaded and normalized from policy) or NFD
    # (passed straight to this API), and the fact relation likewise. Only the
    # membership PROBE folds; the grouping key and reported name stay verbatim.
    sv = {unicodedata.normalize("NFC", _canonicalize(r, aliases)) for r in single_valued}
    # (NFC subject, NFC-folded canonical relation) -> group key -> set of raw objects.
    by_key: dict[tuple[str, str], dict[tuple, set[str]]] = {}
    # Same folded key -> the raw subject / canonical spellings actually seen, so the
    # report can name a deterministic representative (min) on each axis instead of
    # coercing to NFC (#295/#310 grouping fold, #227 provenance).
    raw_canons: dict[tuple[str, str], set[str]] = {}
    raw_subjects: dict[tuple[str, str], set[str]] = {}
    for row in engine_facts(facts):
        relation = row["relation"]
        canon = _canonicalize(relation, aliases)
        fcanon = unicodedata.normalize("NFC", canon)
        # Membership folds the fact side to NFC to match the NFC-folded ``sv``
        # above, so an NFD-authored fact relation still matches its declaration
        # (#285/#210).
        if fcanon not in sv:
            continue
        obj = row["object"]
        # THE shared lookup rule (NFC + alias fold), same as the projection and the
        # report use -- so a typed spec is found the same way in all three (#244).
        spec = _lookup_typed_spec(relation, typed, aliases)
        key = _group_key(obj, spec)
        # The subject folds to NFC too, so NFD- and NFC-authored spellings of one
        # subject share a bucket rather than hiding a contradiction across two (#310).
        gk = (unicodedata.normalize("NFC", row["subject"]), fcanon)
        by_key.setdefault(gk, {}).setdefault(key, set()).add(obj)
        raw_canons.setdefault(gk, set()).add(canon)
        raw_subjects.setdefault(gk, set()).add(row["subject"])
    conflicts: dict[tuple[str, str], list[str]] = {}
    for gk, groups in by_key.items():
        if len(groups) <= 1:
            continue
        _fsubject, fcanon = gk
        values = sorted(min(raws) for raws in groups.values())
        # typed and hierarchy dicts are NFC-keyed, so look them up on the folded
        # name; the reported subject and relation are the deterministic min raw
        # spellings seen for this bucket.
        if _is_specialisation_chain(values, fcanon, hierarchy, _lookup_typed_spec(fcanon, typed, aliases)):
            continue
        conflicts[(min(raw_subjects[gk]), min(raw_canons[gk]))] = values
    return conflicts


def _hier_key(value: str, spec: TypedRelSpec | None = None) -> object:
    """The form a value is compared in when matching it against the hierarchy.

    NFC as well as canonical_value: the policy file is NFC-normalized at parse time but
    a fact row is not, and macOS-authored text is routinely NFD -- so a declaration and
    the fact it describes, spelled identically, did not meet, and the report blamed a
    typo that was not there.
    """
    # The SAME key _group_key uses, so the scaler equivalence it advertises (억 ↔ 조)
    # reaches the hierarchy too. Comparing raw strings here meant a declaration written
    # in 조 never met a fact written in 억, even though the two are the same number and
    # _group_key already collapses them.
    if spec is not None:
        return _group_key(unicodedata.normalize("NFC", value), spec)
    return canonical_value(unicodedata.normalize("NFC", value))


def _is_specialisation_chain(
    values: list[str],
    relation: str,
    hierarchy: dict[str, dict[str, set[str]]] | None,
    spec: TypedRelSpec | None = None,
) -> bool:
    """True when every value sits on ONE declared ancestor chain for *relation*.

    `연구유형: 코호트연구 ⊂ 관찰연구` means a cohort study IS an observational study,
    so a paper carrying both is not contradicting itself -- it is being described at
    two levels of precision, and both rows are true. Reporting that as a conflict
    made finalize refuse to compile, and the resolution text then told the user to
    retire one of them: retire the subtype and you lose the more precise fact, retire
    the supertype and you delete something the source states outright. Either way a
    human-checked fact is discarded on no evidence (#219).

    A chain, not merely "some pair is related": with 관찰연구 / 코호트연구 / 실험연구 the
    first two are on a chain but 실험연구 is a genuine sibling, and that is still a
    contradiction. So require a single most-specific value that has every other value
    among its declared ancestors.

    *hierarchy* is what value_hierarchy() returns, i.e. already transitively closed,
    so a grandparent is reachable in one lookup.
    """
    if not hierarchy or len(values) < 2:
        return False
    for candidate in values:
        # canonical_value on BOTH sides. hierarchy_ancestors' contract is that both keys
        # go through `normalize`, and every other caller passes it; omitting it here
        # while folding `others` with it meant a typed relation (an amount written
        # `amount(7,"억")`) never matched its own declaration, so the false conflict the
        # issue is about survived for exactly the values a hierarchy is most useful for.
        # hierarchy_ancestors keys on the raw declaration text, so the LOOKUP stays on
        # canonical_value; only the COMPARISON folds through the typed key.
        raw_ancestors = hierarchy_ancestors(hierarchy, relation, candidate, _hier_key)
        if not raw_ancestors:
            continue
        ancestors = {_hier_key(a, spec) for a in raw_ancestors}
        others = {_hier_key(v, spec) for v in values if v != candidate}
        if others <= ancestors:
            return True
    return False


def value_hierarchy_warnings(
    root: Path | None = None,
    facts: list[dict[str, str]] | None = None,
) -> list[str]:
    """Problems with the declarations themselves — unparsable lines, cycles, and
    names that no accepted fact uses.

    A typo in a declaration is a SILENT no-op: the author believes the broader
    query now catches the narrower rows, and it does not. That is precisely the
    quiet omission #211 is about, so the logic report says so rather than
    leaving the author to trust a file nobody checked.
    """
    hierarchy, warnings = _closed_hierarchy(root)
    if facts is None:
        return warnings

    # Index the facts the way the LOOKUP does — by canonical relation name and
    # normalised value. Indexing by the raw stored relation made an aliased KB
    # (rows carrying a surface variant) report a perfectly good declaration as
    # having "no effect", which is worse than saying nothing: a user who believes
    # it and deletes the declaration gets the silent omission back.
    #
    # The alias lookup key is folded to NFC because the alias map is keyed by NFC
    # names: an NFD-typed relation would miss the map, fall through to its raw
    # self, and re-create exactly the false "no effect" report above. Only this
    # internal index key is folded — the reported strings keep their stored form.
    aliases = relation_aliases(root)

    def _canon_rel(name: str) -> str:
        return _canonical_value(resolve_relation(name, aliases))

    values_by_relation: dict[str, set[str]] = {}
    for row in facts:
        values_by_relation.setdefault(_canon_rel(row["relation"]), set()).add(
            _canonical_value(row["object"])
        )

    for relation, table in sorted(hierarchy.items()):
        # Resolve the DECLARED relation name through the same alias axis as the
        # facts (via _canon_rel -> resolve_relation), not just _canonical_value: a
        # hierarchy declared on an alias RAW name (`연구유형`, not the canonical
        # `study_type`) otherwise keys itself under its surface name, misses the
        # facts indexed by canonical name, and a live declaration is condemned as
        # "no effect" (#344 — #211 inverted, independent of NFC/NFD).
        key = _canon_rel(relation)
        if key not in values_by_relation:
            warnings.append(f"value-hierarchy: no accepted fact uses relation '{relation}' — declaration has no effect")
            continue
        values = values_by_relation[key]
        for child in sorted(table):
            if _canonical_value(child) not in values:
                warnings.append(
                    f"value-hierarchy: no accepted '{relation}' fact has the value '{child}' — "
                    f"declaration has no effect (typo?)"
                )
    return warnings


def hierarchy_ancestors(
    hierarchy: dict[str, dict[str, set[str]]] | None,
    relation: str,
    value: str,
    normalize: Callable[[str], str] | None = None,
) -> set[str]:
    """Declared ancestors of `value` under `relation` (empty when undeclared).

    Both keys are compared through `normalize`, so a policy file and a fact row
    that differ only in surface spelling still meet. The RELATION passed by
    callers is the query's relation (a canonical name) rather than the stored
    row's, so a KB using relation aliases — where rows carry a surface variant —
    still resolves against declarations written on the canonical name.
    """
    if not hierarchy:
        return set()
    norm = normalize or (lambda value: value)
    want_rel, want_val = norm(relation), norm(value)
    for declared_rel, table in hierarchy.items():
        if norm(declared_rel) != want_rel:
            continue
        for child, ancestors in table.items():
            if norm(child) == want_val:
                return ancestors
    return set()


def declared_ancestors(
    hierarchy: dict[str, dict[str, set[str]]] | None,
    relation: str | None,
    normalize: Callable[[str], str] | None = None,
) -> set[str]:
    """Every value declared as an ancestor under `relation` (normalised).

    These are queryable objects even when no fact carries them. Pass the query's
    relation so the licence stays scoped to it; `None` (a variable-relation query,
    which really can range over every relation) widens to the whole file.
    """
    if not hierarchy:
        return set()
    norm = normalize or (lambda value: value)
    want = norm(relation) if relation is not None else None
    found: set[str] = set()
    for declared_rel, table in hierarchy.items():
        if want is not None and norm(declared_rel) != want:
            continue
        for ancestors in table.values():
            found |= {norm(ancestor) for ancestor in ancestors}
    return found


def policy_row_matches(args: list[str], row: tuple[str, ...]) -> bool:
    """Does `row` satisfy a policy query's pinned entity?

    THE single filtering predicate for policy-predicate extents. The report
    (run_logic_check) and the router (ask_router) each carried their own copy and
    drifted: the router filtered on the pinned entity while the report printed the
    whole extent, so `needs_review("Alice", R)?` was answered with every subject's
    rows — and for an entity with no rows at all the report presented the full
    extent as that entity's (#213, #320). Two verification paths disagreeing is
    worse than either being wrong. One predicate, two callers, no room to drift.

    Only the first arg constrains, and only when it is a quoted string: a variable
    there ranges over the whole extent.

    BOTH sides go through `_canonical_value`, and they must stay that way. The router
    used to compare verbatim, so an NFD extent row `("한글", "low_conf")` never met an
    NFC query `needs_review("한글", R)?`: the pin found nothing and the answer was
    `0 rows` — not silence, but a positive claim that a subject with rows has none,
    the verified negative #284 forbids. Filtering an extent raw only looks safe
    because a fabricated negative is quiet.

    Parity is the means here, not the standard: two paths agreeing on `0 rows` is
    still wrong, and worse than one of them being wrong, because their disagreement
    was the only signal the NFD row existed at all. So the fold belongs HERE, in the
    one predicate both callers route through — fold in the router alone and the
    report/router divergence #320 removed comes straight back. Fold one side only and
    a new one appears. `relation_row_matches` routes its value comparison through the
    same function for the same reason.

    `_canonical_value` folds NFC and `amount(...)` unit quoting; entity strings,
    dates, numbers and ordinals keep their form, so this narrows nothing else.
    """
    if not args or not _is_quoted_string(args[0]):
        return True
    return bool(row) and _canonical_value(_arg_value(args[0])) == _canonical_value(row[0])


def relation_row_matches(
    args: list[str],
    row: dict[str, str],
    aliases: dict[str, str] | None = None,
    hierarchy: dict[str, dict[str, set[str]]] | None = None,
) -> bool:
    """Does `row` satisfy the three args of a `relation(...)` query?

    THE single matching predicate. The report (run_logic_check), the router
    (ask_router) and the gate (classify_query) each used to carry their own
    near-copy, and they drifted: the report compared raw strings while the router
    canonicalised, so declaring a relation alias made facts vanish from the
    verification report while `/factlog ask` still found them (#213). Two
    verification paths disagreeing is worse than either being wrong — you cannot
    tell which to believe. One predicate, three callers, no room to drift.

    * subject/object: compared through `_canonical_value` (an `amount` term matches
      whether or not the author quoted its unit).
    * relation: the canonical name OR any declared surface variant of it.
    * object: also honours policy/value-hierarchy.md, so a query for a broad value
      returns rows filed under a declared narrower one (#211).
    """
    if len(args) != 3:
        return False
    s_arg, r_arg, o_arg = args
    aliases = aliases or {}

    if not (_is_variable(s_arg) or _canonical_value(_arg_value(s_arg)) == _canonical_value(row["subject"])):
        return False

    if not _is_variable(r_arg):
        variants = canonical_variants_of(_arg_value(r_arg), aliases)
        if not (
            _canonical_value(_arg_value(r_arg)) == _canonical_value(row["relation"])
            or unicodedata.normalize("NFC", row["relation"]) in variants
        ):
            return False

    if _is_variable(o_arg):
        return True
    # Declarations are written on CANONICAL relation names; rows may store a
    # surface variant. Look up under the name the QUERY used, falling back to the
    # row's own canonicalised name for a variable-relation query.
    query_relation = _arg_value(r_arg) if not _is_variable(r_arg) else resolve_relation(row["relation"], aliases)
    return object_matches(_arg_value(o_arg), row, hierarchy, _canonical_value, relation=query_relation)


def object_matches(
    query_object: str,
    row: dict[str, str],
    hierarchy: dict[str, dict[str, set[str]]] | None,
    normalize: Callable[[str], str] | None = None,
    relation: str | None = None,
) -> bool:
    """Does a fact row satisfy a query asking for `query_object`?

    True on an exact match, or when the row's object is a declared descendant of
    it (`관찰연구` matches a row filed as `코호트연구`). Subsumption is one-way:
    asking for the narrow value never returns the broad one.

    `relation` is the relation the QUERY named; pass it when the query pins a
    relation constant, so an aliased KB (rows storing a surface variant) still
    matches declarations written on the canonical name. Falls back to the row's
    own relation for a variable-relation query.

    `normalize` folds surface spelling before comparing — ask_router matches
    canonicalised values, so a declaration must not be defeated by a stray space.
    """
    norm = normalize or (lambda value: value)
    want = norm(query_object)
    if norm(row["object"]) == want:
        return True
    ancestors = hierarchy_ancestors(hierarchy, relation or row["relation"], row["object"], norm)
    return any(norm(ancestor) == want for ancestor in ancestors)


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
        # Strip a trailing comment that starts OUTSIDE backticks, so a valid mapping with
        # an inline note (`` `x` -> `y`  # a note``) is not read as malformed. A `#`
        # inside backticks is part of the name; mask backtick spans, then cut at the first
        # `#` that survives. typed-relations.md strips inline comments the same way.
        masked = re.sub(r"`[^`]*`", lambda mm: "\x00" * len(mm.group()), stripped)
        hash_at = masked.find("#")
        if hash_at != -1:
            stripped = stripped[:hash_at].strip()
        if not stripped:
            continue
        # Expect exactly `raw` -> `canonical` — arrow AND backticks required. Accept the
        # common unicode arrow variants only to DETECT a mis-spelled mapping (below), not
        # to parse it: the format is ASCII `->`.
        m = re.fullmatch(r"`([^`]+)`\s*->\s*`([^`]+)`", stripped)
        if not m:
            # A line that looks like a mapping -- an arrow (ASCII -> or a unicode variant)
            # or two backtick groups -- but does not match is one the user meant to make
            # and mis-spelled. Silently skipping it left the alias unapplied with no sign,
            # so a query that relied on it just missed. typed-relations.md warns on the
            # same shape; match it, rather than swallow.
            looks_like_mapping = ("->" in stripped or "=>" in stripped
                                  or "\u2192" in stripped or "\u27f6" in stripped
                                  or "\u21a6" in stripped or "\u21d2" in stripped
                                  or len(re.findall(r"`[^`]+`", stripped)) >= 2)
            if looks_like_mapping:
                print(
                    f"relation-aliases.md: skipping malformed line {line.strip()!r} "
                    "(each mapping needs backticks AND an ASCII arrow: `raw` -> `canonical`)",
                    file=sys.stderr,
                )
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


def attribute_relations() -> set[str]:
    """Relation names whose object is a LITERAL value, not a first-class entity
    (policy/attribute-relations.md).

    Objects of these relations (dates, numbers, ordinals, ...) are excluded from
    entity_set so they do not pollute the entity vocabulary (entity listings,
    path nodes, count subjects) — provided the value appears nowhere else. No edge is
    drawn ALONG an attribute relation, which is the actual guarantee; a value that also
    appears as a subject, or as the object of a non-attribute relation, is an ordinary
    entity and paths may run through it. They remain valid relation-query objects — see
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
# Built-in engine predicates. `attr_rel` joined them with #226: a policy file
# declaring its own `attr_rel` used to work and now collides with the program.
_TYPED_RESERVED = {"relation", "edge", "path", "attr_rel", "relation_alive"}


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
        if not num.is_finite() or num != num.to_integral_value() or num <= 0:
            raise FactlogError(f"typed-relations: unit value for {unit!r} must be a positive integer, got {value!r}")
        if unit in units:
            raise FactlogError(f"typed-relations: duplicate unit {unit!r} in units clause")
        units[unit] = int(num)
    return units


def _typed_warn(sink: list[str] | None, message: str) -> None:
    """Append to *sink* (for the report) or print to stderr (streaming diagnostic).

    A malformed typed-relations line drops a whole relation from its comparison
    predicate -- broader than a single value's parse failure -- yet the report said
    warnings: 0. Routing these through a sink lets run_logic_check surface them, the
    same way typed_projection_warnings surfaces value drops (#244).
    """
    if sink is not None:
        sink.append(message)
    else:
        print(message, file=sys.stderr)


def _parse_typed_relations(
    text: str,
    reserved: frozenset[str] | set[str] = frozenset(),
    warnings: list[str] | None = None,
) -> dict[str, TypedRelSpec]:
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
            _typed_warn(warnings, f"typed-relations: skipping malformed line {stripped!r} "
                        "— its relation gets no type, so all its facts drop out of the "
                        "comparison predicate")
            continue
        name = unicodedata.normalize("NFC", (m.group("qname") or m.group("name")).strip())
        type_tag = m.group("type")
        alias = m.group("alias")
        units_body = m.group("units")  # None if no clause, "" if empty `()`
        if type_tag not in literal_types.TYPES:
            _typed_warn(warnings, f"typed-relations: unknown type {type_tag!r} for {name!r}; "
                        "skipping — its facts drop out of the comparison predicate")
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


def _warn_typed_not_attribute(
    specs: dict[str, TypedRelSpec],
    attrs: set[str],
    aliases: dict[str, str] | None = None,
    sink: list[str] | None = None,
) -> None:
    # Through the shared predicate: comparing raw declarations made this warn that a
    # relation was "not declared" when it WAS, just under its alias -- while the engine,
    # entity_set and vocab all treated it as an attribute. A diagnostic that cries wolf
    # is one users learn to ignore.
    forms = attribute_relation_forms(attrs, aliases)
    for name in specs:
        if not is_attribute_relation(name, forms):
            _typed_warn(
                sink,
                f"typed-relations: {name!r} is typed but not declared in attribute-relations.md "
                "(its object should be a literal, not an entity)",
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


def _scan_policy(text: str, *, strict: bool = True) -> tuple[str, list[str]]:
    """ONE left-to-right lex of a .dl program, in the engine's order.

    Returns (skeleton, literals):
      * skeleton -- the text with each string literal replaced by a single space and
        every //- or #-comment removed. This is the code STRUCTURE the reserved-head
        guard reads.
      * literals -- each string literal's VALUE, decoded the way pyrewire decodes it:
        `\\"` -> `"`, `\\\\` -> `\\`, and every other `\\X` kept as the two literal
        characters (verified against the engine). This is what run_wirelog pre-interns
        so decode_wirelog_value can turn an emitted symbol id back into its text.

    One scan with one bit of state, so the guard, the interning, and _quoted_constants
    tokenize a policy the same way the engine does. Three parsers drifting -- a regex
    that split a literal at `\\"`, another that missed a `//` comment, a third that
    over-decoded `\\n` -- is what kept #226/#250 reopening.

    *strict* raises FactlogError on an unterminated string (the engine rejects it too);
    a per-line query caller passes strict=False and takes what closed.
    """
    skeleton: list[str] = []
    literals: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            chars: list[str] = []
            j = i + 1
            closed = False
            while j < n:
                c = text[j]
                if c == "\\" and j + 1 < n:
                    nxt = text[j + 1]
                    chars.append(nxt if nxt in '"\\' else "\\" + nxt)
                    j += 2
                    continue
                if c == '"':
                    closed = True
                    break
                if c == "\n":
                    break  # a literal must close on the line it opens
                chars.append(c)
                j += 1
            if not closed:
                if strict:
                    raise FactlogError(
                        "unterminated string literal in logic-policy(.extra).dl; a "
                        "quoted value must close on the line it opens"
                    )
                break
            literals.append("".join(chars))
            skeleton.append(" ")  # the literal's content is not code
            i = j + 1
            continue
        if text.startswith("//", i) or ch == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        skeleton.append(ch)
        i += 1
    return "".join(skeleton), literals


def _assert_no_canonical_head(policy_text: str) -> None:
    """Raise FactlogError if the policy heads a reserved ENGINE EDB predicate.

    Covers `canonical` and `attr_rel`. Both are populated by us and declared in
    WIRELOG_PROGRAM; heading either makes pyrewire treat it as IDB and SILENTLY drop
    every EDB atom we emitted, with rc=0. For attr_rel that means `!attr_rel(R)`
    becomes vacuously true, every edge is drawn again, and #226 is back -- with the
    engine and the tracer now disagreeing, which the report is built on not happening.
    canonical was guarded twice over; attr_rel was guarded nowhere.

    ``canonical`` is a reserved engine EDB predicate emitted by compile_facts into
    accepted.dl and declared in WIRELOG_PROGRAM.  It may appear freely in rule
    *bodies* (right of ``:-``) — that is the whole point of #227.  But a rule
    that *heads* ``canonical`` (left of ``:-``, or a bare canonical fact line)
    makes pyrewire treat canonical as IDB and silently drops every compile-emitted
    EDB atom, producing wrong answers with rc=0.

    Detection strategy: track whether we are inside a rule body (i.e. a ``:-`` has
    been seen and the rule has not yet ended with ``.``).  On each non-comment line,
    strip quoted strings (so ``"canonical("`` inside a reason literal is not
    mistaken for a predicate call), then:

    - If we are NOT already in a body, a ``canonical(`` token on this line is a
      head occurrence (either a head in a single-line rule, or a bare fact).
    - If we ARE already in a body, ``canonical(`` is a body reference → allowed.

    The ``.decl canonical`` in WIRELOG_PROGRAM is never passed as *policy_text*, so
    it is safe from this check.

    Raises :class:`FactlogError` on first offending line with an actionable message.
    """
    # edge/path are the engine's own derivations, not EDB we emit -- but a policy that
    # heads `edge` re-draws every link the attribute filter removed, and the scaffold
    # promises, unconditionally, that no edge is drawn along an attribute relation. A
    # guarantee with an unguarded escape hatch is the false promise this issue is about.
    _SOURCES = {
        "canonical": "relation-aliases.md",
        "attr_rel": "attribute-relations.md",
        "edge": "the engine's edge/2 rule",
        "path": "the engine's path/2 rule",
        # The #308 witness IDB. A policy that heads it (rule or .decl) would UNION fake
        # tuples into the extent the engine-input-gap check reads, masking a real empty
        # engine (a false negative on the last net). It is engine-declared like edge/path,
        # so a policy has no legitimate reason to head it.
        "relation_alive": "the engine's relation_alive/1 witness rule",
    }
    # Drop comment lines, strip quoted literals, then split into logical
    # STATEMENTS on clause-terminating '.' rather than per physical line. A period
    # terminates a clause unless it opens a '.decl'-style directive (dot followed
    # by a letter at a token start) or sits inside a float (dot between digits).
    # Per-line tracking mis-classified a canonical head/fact that shares a physical
    # line with a preceding rule's terminator as an in-body reference (#261); a
    # statement is a full clause, so canonical-left-of-neck (or no neck at all)
    # is unambiguously a head/fact.
    # ONE shared lex, so the guard and the interning tokenize the policy identically
    # (three parsers drifting is what reopened #226/#250). The skeleton has each string
    # literal replaced by a space and comments removed.
    skeleton, _ = _scan_policy(policy_text)
    bare = "\n".join(line for line in skeleton.splitlines() if line.strip())

    for name in re.findall(r"\.decl\s+([A-Za-z_][A-Za-z0-9_]*)", bare):
        if name in _SOURCES:
            raise FactlogError(
                f"{name} is a reserved engine predicate (populated from "
                f"{_SOURCES[name]}) and is already declared by the engine; remove the "
                f".decl from logic-policy(.extra).dl"
            )
        # `relation` is the accepted-fact EDB and is ALREADY declared by the engine
        # (WIRELOG_PROGRAM). A policy `.decl relation(...)` re-declares it, and pyrewire
        # then silently mishandles the accepted atoms with compile rc=0: an arity that
        # differs from the engine's 3 partially LOSES facts (an arity mismatch dropped
        # a KB's path pairs 3->2 with no signal), and even an arity-MATCHING re-decl is
        # a meaningless duplicate that only invites that failure later. So every form is
        # rejected (#305). Unlike the fact/rule head check below, a `.decl relation`
        # carries no legitimate use -- a bare relation(...) FACT (allowed, #303) is not
        # a `.decl`, so this does not touch it.
        if name == "relation":
            raise FactlogError(
                "relation is the engine's accepted-fact EDB, already declared by the "
                "engine (WIRELOG_PROGRAM); a policy .decl relation(...) re-declares it "
                "and pyrewire then silently drops or corrupts accepted facts (an arity "
                "mismatch loses them, compile stays rc=0). Remove the .decl from "
                "logic-policy(.extra).dl."
            )

    # A policy predicate may only carry symbol/string columns. The report renders an
    # emitted row by printing its values, and only a symbol column is renderable text;
    # a scalar column reaches the report as a bare int with no way to say what it MEANS
    # (#323). The scalar-free-head rule used to exist only as prose next to
    # _project_typed_relations, and a `.decl low_rank(subject: symbol, r: int64)` in
    # extra.dl loaded fine and printed a number where a reason belongs. Fail at LOAD,
    # the one point that sees the whole policy, instead of at render time per row.
    #
    # `symbol` and `string` both map to ColumnType.STRING in the engine, so both are
    # renderable and both are allowed. Everything else (int32/int64/float/unsigned) is
    # a scalar and is rejected.
    #
    # This guard reads POLICY text only. The typed side-relations (#116) legitimately
    # declare int64 columns, but _typed_decls appends them to the program AFTER
    # load_logic_policy has returned (see the EasySession call in run_wirelog), so they
    # never pass through here and typed projection is untouched.
    _RENDERABLE_COL = {"symbol", "string"}
    for m in re.finditer(r"\.decl\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)", bare):
        name, columns = m.group(1), m.group(2)
        for column in columns.split(","):
            if ":" not in column:
                continue
            field, _, coltype = column.partition(":")
            coltype = coltype.strip()
            if coltype and coltype not in _RENDERABLE_COL:
                raise FactlogError(
                    f"policy predicate {name!r} declares column {field.strip()!r} as "
                    f"{coltype!r}, but a policy .decl may use only symbol/string "
                    f"columns. The report renders an emitted row by printing its "
                    f"values, so a scalar column would print a bare number where a "
                    f"reason belongs. Keep the scalar in the rule BODY and head a "
                    f"quoted reason instead, e.g. "
                    f'`{name}(S, "rank below 5") :- priority_rank(S, R), R < 5.` '
                    f"If you need a comparable scalar RELATION to compare against, "
                    f"declare it as a typed relation in policy/typed-relations.md "
                    f"(#116) — factlog projects those into int64 side-relations "
                    f"outside the policy text, which is why they are exempt from "
                    f"this rule. See docs/typed-relations.md. Fix the .decl in "
                    f"logic-policy(.extra).dl."
                )
    bare = re.sub(r"\.decl\s+[A-Za-z_][A-Za-z0-9_]*\s*\([^)]*\)", "", bare)

    for statement in _split_policy_statements(bare):
        # Tokenize the HEAD; do not substring-search the statement. A substring search
        # was wrong in both directions: `attr_rel (R) :- ...` (one space) slipped past
        # it and #226 came back with rc=0, while `not_canonical(X, ...) :- ...` -- a
        # user predicate that merely CONTAINS the reserved name -- was rejected, so a
        # KB that worked before could no longer run `factlog check`.
        head = statement.split(":-", 1)[0]
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", head)
        if m and m.group(1) in _SOURCES:
            name = m.group(1)
            raise FactlogError(
                f"{name} is a reserved engine EDB predicate (populated from "
                f"{_SOURCES[name]}); it may appear only in rule bodies, not as a "
                f"rule head/fact in logic-policy(.extra).dl"
            )
        # `relation` is the accepted-fact EDB (facts/accepted.dl). A bare fact line is
        # fine and stays allowed (#303), but a RULE that HEADS relation makes pyrewire
        # treat relation as IDB and SILENTLY drop every accepted-fact atom: compile
        # rc=0, then relation/path/every policy predicate evaluate over an empty
        # relation -- a vacuous pass that check/add/ask all report as success (#305).
        # `:-` is tested on the skeleton (strings and comments already stripped), so a
        # reason literal or comment containing `:-` never trips this; the body (right of
        # `:-`) is not the head, so a standard rule that READS relation is unaffected.
        if m and m.group(1) == "relation" and ":-" in statement:
            raise FactlogError(
                "relation is the engine's accepted-fact EDB (populated from "
                "facts/accepted.dl); a rule that HEADS relation makes pyrewire treat it "
                "as IDB and silently drops every accepted fact (compile stays rc=0, then "
                "relation/path/policy all evaluate over an empty relation -- a vacuous "
                "pass). Define a derived relation under a DIFFERENT predicate name in "
                "logic-policy(.extra).dl, not as a relation rule head. (A bare "
                "relation(...) fact is allowed.)"
            )


def _split_policy_statements(text: str) -> list[str]:
    """Split Datalog policy text into logical statements on clause-terminating '.'.

    A '.' ends a clause EXCEPT when it opens a directive (a '.decl'-style dot at a
    token start: preceded by whitespace/start and followed by a letter) or sits
    inside a float (a dot between two digits). This lets `_assert_no_canonical_head`
    see each head/fact/rule as one unit even when several share a physical line."""
    statements: list[str] = []
    buf: list[str] = []
    for i, ch in enumerate(text):
        buf.append(ch)
        if ch == ".":
            prev = text[i - 1] if i > 0 else ""
            nxt = text[i + 1] if i + 1 < len(text) else ""
            is_directive = (prev == "" or prev.isspace()) and nxt.isalpha()
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


def dedup_engine_atoms(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Collapse rows that share a ``(subject, relation, object)`` triple to a
    single engine atom, keeping the FIRST occurrence (stable, not sort-min).

    The engine atom carries only the triple (see ``dl_atom``); the same triple
    accepted from several sources must appear once in ``accepted.dl`` so ``ask``
    and ``run_logic_check`` report set semantics (one row / true count) rather
    than an inflated, duplicated count. Source aggregation (``sources: N``,
    provenance) lives on the separate candidates path (``corroboration_counts``,
    ``fact_signals``) and is untouched by this collapse. First-occurrence order
    keeps ``accepted.dl`` byte-identical when the KB has no duplicate triple."""
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, str]] = []
    for row in rows:
        key = (row["subject"], row["relation"], row["object"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
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


def entity_set(
    facts: list[dict[str, str]] | None = None,
    attribute_rels: set[str] | None = None,
    aliases: dict[str, str] | None = None,
) -> set[str]:
    """First-class entities only: every subject, plus objects whose relation is
    NOT declared an attribute relation. Objects of attribute relations are
    literal values (see attribute_relations) and are excluded so they don't show
    up as entities (entity listings, path nodes, count subjects) — provided they appear
    nowhere else. No edge is drawn ALONG an attribute relation; a value that also
    appears as a subject, or as the object of a non-attribute relation, is an ordinary
    entity and paths may run through it. With no
    policy/attribute-relations.md this equals value_set (backward compatible).

    *attribute_rels* overrides which relations count as attribute (literal-valued)
    relations; pass a KbContext's attribute_relations() to read a non-default KB.
    None falls back to the module-level (ambient-root) attribute_relations().

    *aliases* must come from the SAME KB. Resolving attribute relations needs the
    alias map, and reading it from the ambient root while taking attribute_rels from
    the target made one KB answer differently depending on whether it was named with
    --target or FACTLOG_ROOT -- the ambient KB's alias file leaked into the target's
    vocabulary."""
    selected = engine_input_rows(facts if facts is not None else load_accepted_facts())
    # Surface forms, not raw declarations: a KB that declares the canonical while
    # its facts carry an alias had every attribute row miss this filter, so the
    # literal was admitted as an entity anyway (#226).
    literal_rels = attribute_relation_forms(attribute_rels, aliases)
    entities: set[str] = set()
    for row in selected:
        if row["subject"]:
            entities.add(row["subject"])
        if row["object"] and not is_attribute_relation(row["relation"], literal_rels):
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
    attr_forms: set[str] | None = None,
) -> dict[str, list[str]]:
    """Edges between entities, mirroring the engine's `edge/2`.

    An ATTRIBUTE RELATION is skipped, exactly as the engine rule skips it: its object
    is a value (a date, a count), and asserting a value about a thing is not a
    dependency you can route through (#226).

    The filter keys on the RELATION, not on the node. Keying on the node — "a value
    that never appears as a subject" — was tried and is wrong in both directions:
    it deletes a genuinely asserted non-attribute edge into a value that happens to
    be attributed elsewhere (a real false negative), and one stray fact making a
    value a subject turns that value back into a hub every path can route through,
    which is #226 itself.

    A literal can therefore still be an entity (entity_set admits any subject) while
    no path leads INTO it. That is not a divergence: `path` is defined over
    dependency edges, so "no dependency path to 2030.1" is an honest verified
    negative even though 2030.1 heads facts of its own.

    Keeping this in step with the engine matters — the report asks the ENGINE
    whether a path exists and then asks THIS to render the trace, so a divergence
    would print a route the engine says does not exist.
    """
    attrs = attribute_relation_forms() if attr_forms is None else attr_forms
    graph: dict[str, list[str]] = defaultdict(list)
    for row in engine_input_rows(facts):
        if is_attribute_relation(row["relation"], attrs):
            continue
        graph[row["subject"]].append(row["object"])
    return graph


def path_query_rows(
    args: list[str],
    facts: list[dict[str, str]],
    pairs: set[tuple[str, str]],
) -> list[list[str]]:
    """Rows answering a `path` query, whether its arguments are constants or variables.

    THE shared answer, so the report and the ask router cannot disagree. They did: the
    report only handled two quoted constants, so `path("A", X)?` produced no result
    line at all, the result list came back empty, and main's fallback printed
    `no facts/query.dl found` about a file that was right there -- while ask answered
    the same question with two rows (#220). #213 unified relation and count this way;
    path was left behind.

    Two constants yield the TRACE (the route), a variable yields one row per matching
    reachable pair -- the same shapes the two callers already rendered.
    """
    if len(args) != 2:
        return []
    # `pairs` is required: it is the ENGINE's path/2. Defaulting to a python closure
    # over the accepted facts would leave a second source of truth in the tree, and the
    # two would drift -- which is the bug this function exists to end. Both callers (the
    # report and the ask router) run the engine and pass its answer.
    reachable = pairs
    # The engine interns accepted.dl VERBATIM (an NFD-authored entity stays NFD in its
    # path/2 pairs), but a query constant may be NFC -- so a raw compare missed, and
    # #296's value-chokepoint fold never reached here (path decides membership against
    # the engine pairs, not through _canonical_value). Fold BOTH the engine pair and the
    # query constant to their canonical form at the comparison, keeping the engine pair
    # as the truth of reachability and RENDERING the stored (verbatim) pair for
    # provenance (#299). NFC-only data folds to itself, so the answer is unchanged.
    if all(is_quoted_string(a) for a in args):
        want = (canonical_value(arg_value(args[0])), canonical_value(arg_value(args[1])))
        # canonical pair -> its stored spelling; setdefault over sorted() keeps the
        # lexicographically smallest stored pair when several fold to one canonical.
        stored_of: dict[tuple[str, str], tuple[str, str]] = {}
        for (a, b) in sorted(reachable):
            stored_of.setdefault((canonical_value(a), canonical_value(b)), (a, b))
        if want not in stored_of:
            return []
        start, target = stored_of[want]
        route = dependency_path(facts, start, target)
        # Reachable per the truth set but with no route through the accepted facts: a
        # policy rule in logic-policy.extra.dl put the edge there. Report the pair, not
        # a false "(not found)".
        return [route] if route else [[start, target]]
    # The SAME variable twice means a join, not two independent wildcards: `path(X, X)?`
    # asks which nodes lie on a cycle, and answering it with every reachable pair was
    # simply a wrong answer.
    same_var = is_variable(args[0]) and is_variable(args[1]) and args[0] == args[1]
    return [
        [start, target]
        for (start, target) in sorted(reachable)
        if (is_variable(args[0]) or canonical_value(arg_value(args[0])) == canonical_value(start))
        and (is_variable(args[1]) or canonical_value(arg_value(args[1])) == canonical_value(target))
        and (not same_var or start == target)
    ]


def dependency_path(
    facts: list[dict[str, str]],
    start: str,
    target: str,
    attr_forms: set[str] | None = None,
) -> list[str]:
    graph = dependency_graph(facts, attr_forms)
    # The graph nodes are stored (verbatim) entity strings; a query endpoint may be
    # NFC while the fact was authored NFD. Resolve each endpoint to its stored
    # spelling (min when several share a canonical form) so the BFS compares
    # stored-to-stored -- the graph is self-consistent -- and the rendered path
    # carries the stored (verbatim) nodes, matching the engine's interned pairs
    # (#299). NFC-only data resolves each node to itself, so nothing moves.
    stored_of: dict[str, str] = {}
    for node in sorted(set(graph) | {o for outs in graph.values() for o in outs}):
        stored_of.setdefault(canonical_value(node), node)
    start = stored_of.get(canonical_value(start), start)
    target = stored_of.get(canonical_value(target), target)
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


def first_dependency_path(facts: list[dict[str, str]]) -> list[str]:
    # Read the policy ONCE: this is an S x O loop, and dependency_graph used to
    # re-parse attribute-relations.md on every call inside it.
    attr_forms = attribute_relation_forms()
    entities = sorted({row["subject"] for row in facts})
    targets = sorted({row["object"] for row in facts})
    for start in entities:
        for target in targets:
            path = dependency_path(facts, start, target, attr_forms)
            if len(path) > 1:
                return path
    return []


# `attr_rel` carries the relations declared in policy/attribute-relations.md, and
# edges skip them. Their objects are LITERALS — a date, a count — not things a
# dependency path can meaningfully run through. The scaffolded policy file promises
# exactly this ("kept OUT of the entity set, so they do not show up as entities,
# path nodes, or count subjects"), but the rule had no such filter, so a path could
# hop through `2030.1` as if a date were an entity (#226).
#
# A KB that declares no attribute relations gets no `attr_rel` facts, so the
# derived edges are identical to before.
WIRELOG_PROGRAM = """
.decl relation(subject: symbol, rel: symbol, object: symbol)
.decl canonical(subject: symbol, rel: symbol, object: symbol)
.decl attr_rel(rel: symbol)
.decl edge(start: symbol, target: symbol)
.decl path(start: symbol, target: symbol)
.decl relation_alive(subject: symbol)

edge(S, O) :- relation(S, R, O), !attr_rel(R).
path(S, O) :- edge(S, O).
path(S, O) :- edge(S, M), path(M, O).
relation_alive(S) :- relation(S, R, O).
"""
# relation_alive is the #308 WITNESS: an IDB projection of relation, so it surfaces as a
# step() delta (relation itself is EDB and never does) and reflects the engine's
# POST-FIXPOINT relation extent. inferred["relation_alive"] empty <=> the engine holds no
# relation atoms, whatever emptied them (a parse-time drop OR a fixpoint drop). Compared
# against the disk fact count in run_logic_check.engine_relation_gap, it is the last net
# for a silently-emptied engine input beyond what #305's policy-load guard rejects.


def attribute_relation_forms(
    attribute_rels: set[str] | None = None,
    aliases: dict[str, str] | None = None,
) -> set[str]:
    """Every SURFACE form that names a declared attribute relation.

    The engine's `!attr_rel(R)` compares R against the name stored in accepted.dl,
    which is the surface form. A declaration names the CANONICAL, so a KB that
    declares `published_year` while its facts say `게재연도` had the filter miss
    every row and the literal stayed a path node — the same alias-blindness #213
    fixed for report/ask/gate. NFC too: a policy file saved as NFD silently
    matched nothing.

    One set, shared by the engine emitter, dependency_graph, and entity_set, so
    the three cannot drift — a divergence here means the report renders a route
    the engine says does not exist.
    """
    declared = attribute_relations() if attribute_rels is None else attribute_rels
    if not declared:
        return set()
    alias_map = relation_aliases() if aliases is None else aliases
    forms: set[str] = set()
    for name in declared:
        nfc_name = unicodedata.normalize("NFC", name)
        # Canonicalize the DECLARATION too, not just expand it. Expanding only
        # canonical -> surface covered a declared canonical whose facts carry an
        # alias, but not the reverse -- a KB declaring the alias `게재연도` while its
        # facts say `published_year` had every attribute row miss the filter, and the
        # scaffold never tells the user which form to declare. Canonicalizing both
        # sides covers both directions with one rule.
        canon = alias_map.get(nfc_name, nfc_name)
        forms.add(nfc_name)
        forms.add(canon)
        forms |= surface_variants(canon, alias_map)
    return forms


def is_attribute_relation(relation: str, attr_forms: set[str]) -> bool:
    """Does *relation* (a stored surface form) name a declared attribute relation?

    THE predicate. Every consumer that asks "is this object a literal?" -- the engine
    emitter, dependency_graph, entity_set, vocab, entity_audit, merge_candidates --
    calls this, so they cannot answer differently. They did: four of them compared the
    raw declaration set, so on an alias KB `vocab --entities` listed a value as an
    entity while `status` and the engine called it a literal, and entity_audit advised
    declaring a relation that was already declared (#226).
    """
    return unicodedata.normalize("NFC", relation) in attr_forms


def _attr_rel_facts(accepted: list[dict[str, str]] | None = None) -> str:
    """`attr_rel` facts for the declared attribute relations (empty when none).

    Emitted as the relation symbols that are ACTUALLY IN accepted.dl, not as the
    declaration's own spelling. The engine compares symbols byte-for-byte, and
    accepted.dl carries a row's relation verbatim (dl_atom does not normalize), so a
    fact written NFD while the policy file is NFC slipped past `!attr_rel(R)` -- the
    engine kept routing paths through the literal, exactly the #226 symptom, while
    the python tracer (which does normalize) said otherwise. Matching on the stored
    symbol keeps the two in step under any normalization, and leaves accepted.dl
    byte-identical.
    """
    forms = attribute_relation_forms()
    if not forms:
        return ""
    rows = load_accepted_facts() if accepted is None else accepted
    names = sorted(
        {row["relation"] for row in engine_input_rows(rows) if is_attribute_relation(row["relation"], forms)}
    )
    if not names:
        return ""
    # dl_string, not an f-string: a name carrying a quote emitted `attr_rel(""x"")`
    # and the engine failed to parse the whole program, killing `factlog check` on
    # a KB that worked before the declaration existed.
    return "\n" + "\n".join(f"attr_rel({dl_string(name)})." for name in names) + "\n"


def policy_string_literals(text: str) -> list[str]:
    """Every quoted string literal in a .dl program, escape-decoded to its real value.

    Thin wrapper over the shared _scan_policy lexer so interning tokenizes the policy the
    same way the reserved-head guard and the engine do -- a private regex here split a
    literal at `\\"`, then missed `//` comments, then over-decoded `\\n`, each reopening
    #250. run_wirelog pre-interns these so the ENGINE can decode an emitted symbol id
    back into its text: pyrewire's _decode_row resolves a STRING column through the
    session intern table and falls back silently to the raw int on a miss, so a literal
    missing from this list renders as a bare number, not as text (measured: without
    pre-interning a symbol column arrives as ('int', 0)).
    """
    return _scan_policy(text)[1]

def decode_wirelog_value(session: EasySession, value: object) -> object:
    """Pass an already-decoded wirelog value through unchanged.

    The row reaching us is decoded ALREADY, and it is a two-party result: the
    engine does the decoding, but only because WE filled the table it decodes
    against. ``EasySession.step()`` runs each row through ``_decode_row``, which
    resolves a STRING column via ``self._intern.lookup(raw)`` and -- this is the
    part that matters -- falls back SILENTLY to the raw ``int`` when that lookup
    misses. The engine never learns a symbol on its own; run_wirelog pre-interns
    every policy literal, accepted-fact value and canonical atom (#250) so the
    lookup can hit.

    Measured both ways on pyrewire 1.0.3, for
    ``flagged(S, "needs review") :- relation(S, "is", "thing").``::

        without pre-interning:  [('int', 0), ('int', 3)]              # raw ids
        with pre-interning:     [('str', 'alpha'), ('str', 'needs review')]

    So pass-through is sound because the pre-interning holds, NOT because the
    engine is a self-sufficient authority. Break the pre-interning and symbol
    columns arrive as raw ints and this function faithfully passes those ints on.
    That is why the pre-interning is load-bearing and must not be mistaken for
    dead code -- see the call sites in run_wirelog.

    Both preconditions are ENFORCED, not merely written down here, because a
    convention that only lives in prose is the exact defect #323 was filed to fix:
    run_wirelog refuses to start when the engine has no schema to decode against
    (the ``_schema_program is None`` guard), and the policy-load guard in
    _assert_no_canonical_head keeps every policy column symbol/string, so a
    pre-interned literal is all a policy row can carry.

    What this layer must NOT do is guess. It used to re-decode by looking only at
    the VALUE (``isinstance(value, int) and session._intern.contains_id(value)``),
    a type-blind second pass over a row the schema had already typed. It could not
    help a correctly pre-interned run -- a symbol column is already ``str`` there,
    so the ``isinstance`` never fired -- and it could only harm: a genuine
    ``int64`` scalar small enough to be a valid intern id was rewritten into
    whatever symbol held that id, so a report printed ``low_rank: alpha (beta)``
    where the truth was ``low_rank: alpha (3)`` (#323). ``bool`` being an ``int``
    subclass meant ``True`` was looked up as id 1 by the same mistake; that dies
    here too. Reading a value cannot recover a column's type -- only the schema
    knows it, and the schema is the engine's to apply.

    The ``pyrewire>=1.0.3,<2.0`` pin in pyproject.toml (mirrored in
    requirements.txt) is what keeps this decoding contract stable: the >=1.0.3
    floor is where ``step()`` decodes rows against the schema for us, and the <2.0
    ceiling keeps a major release from changing that -- or the silent raw-int
    fallback -- under us unnoticed.

    Kept as a function, not inlined, so the "the row is already decoded" rule has
    ONE place to live and to be re-checked against a new engine release.
    """
    return value


def _lookup_typed_spec(
    relation: str,
    specs: dict[str, TypedRelSpec],
    aliases: dict[str, str] | None = None,
) -> TypedRelSpec | None:
    """The typed spec for a stored relation name, folding alias and NFC.

    THE single lookup rule, so the projection and the report cannot find a spec
    differently. specs is keyed by the NFC canonical name, but accepted.dl stores a
    relation verbatim -- an alias surface form, or NFD -- so a raw `specs.get` missed it
    and the fact vanished from the typed comparison with no warning: warnings: 0 while a
    row silently dropped, the exact omission #227 set out to end (#244).
    """
    nfc = unicodedata.normalize("NFC", relation)
    canonical = (aliases or {}).get(nfc, nfc)
    spec = specs.get(canonical) or specs.get(nfc)
    if spec is None or spec.type not in _TYPED_COL:
        return None
    return spec


def typed_projection_outcome(
    row: dict[str, str],
    spec: TypedRelSpec,
) -> tuple[int | None, str | None]:
    """(scalar, drop_reason) for one row under one typed spec.

    THE single place that decides whether a fact reaches its comparison predicate.
    The projection inserts iff scalar is not None; the report warns iff drop_reason
    is not None. They cannot disagree, which they did: the report checked only
    "does not parse" while the projection ALSO dropped non-ints and int64 overflows,
    so a `number` past ~9.2e15 (this KB's own examples reach 억/조 magnitudes, and
    number is scaled x1000) vanished from every typed query while the report said
    `warnings: 0` (#227). Add a guard here and both sides learn about it at once.
    """
    scalar = literal_types.normalize(spec.type, row["object"], spec.units)
    if scalar is None:
        return None, f"does not parse as {spec.type}"
    # Every _TYPED_COL is an int64 column. pyrewire silently accepts a float into
    # one (wrong comparison), so a non-int from a future normalizer must be dropped
    # loudly, not inserted.
    if not isinstance(scalar, int):
        return None, f"normalized to non-int {scalar!r}, which an int64 column cannot hold"
    if not (-(2**63) <= scalar < 2**63):
        return None, f"= {scalar}, out of int64 range"
    return scalar, None


def typed_policy_warnings(root: Path | None = None) -> list[str]:
    """Warnings from PARSING typed-relations.md, for the report.

    A malformed or unknown-type line, or a typed relation not declared as an attribute,
    drops facts from a comparison predicate -- broader than one value failing to parse --
    but only ever reached stderr (#244). Pure: re-reads the policy file and collects
    without side effects, so run_logic_check can add them to warnings.
    """
    base = (root / "policy") if root is not None else POLICY_DIR
    path = base / "typed-relations.md"
    if not path.is_file():
        return []
    sink: list[str] = []
    reserved = _typed_reserved_names(
        relations=_try(allowed_relations),
        predicates=_try(policy_predicates),
    )
    specs = _parse_typed_relations(path.read_text(encoding="utf-8"), reserved, warnings=sink)
    attrs = _relation_names_from(base / "attribute-relations.md")
    aliases = relation_aliases(root)
    _warn_typed_not_attribute(specs, attrs, aliases, sink=sink)
    return sink


def typed_projection_warnings(
    accepted: list[dict[str, str]],
    specs: dict[str, TypedRelSpec] | None = None,
    aliases: dict[str, str] | None = None,
) -> list[str]:
    """Facts whose object does not parse as its relation's declared type.

    Such a fact is silently dropped from the typed side-relation, so a comparison
    predicate never sees it — and the logic report said `warnings: 0` while the
    projection wrote the reason to stderr, where a piped run loses it (#227).
    README calls that report "the verifiable report" and the deterministic gate
    says to show it verbatim before concluding anything. A fact vanishing from a
    typed query with the report claiming nothing is wrong is the exact silent
    omission this KB exists to prevent.

    Pure, so the report can compute it without running the engine.
    """
    specs = typed_relations() if specs is None else specs
    if not specs:
        return []
    aliases = relation_aliases() if aliases is None else aliases
    warnings: list[str] = []
    for row in sorted(accepted, key=lambda r: (r["relation"], r["subject"], r["object"])):
        spec = _lookup_typed_spec(row["relation"], specs, aliases)
        if spec is None:
            continue
        _, reason = typed_projection_outcome(row, spec)
        if reason is not None:
            warnings.append(
                f"typed-relations: {row['subject']} / {row['relation']} / {row['object']} "
                f"{reason} — the fact is EXCLUDED from {spec.alias} comparisons "
                f"(it stays a normal relation fact)"
            )
    return warnings


def _project_typed_relations(session, specs, accepted, aliases=None) -> None:
    """Insert each parseable typed-relation object into its int64 side-relation,
    deterministically ordered so the run is reproducible (#116 invariant 3). A
    non-parsing object warns and skips ONLY that row — the fact still loads
    untyped via relation/3 (#116 invariant 4). Scalars are bare ints and must
    NEVER be interned.

    Touches *session* only via intern/insert — no step/close — so it is
    unit-testable with a fake session and no engine.

    NB: hand-authored comparison-predicate rules (#120) use arity-2
    (subject, reason) heads with a quoted reason string; the scalar stays in
    the body. This is no longer prose that a policy author has to know: a policy
    .decl with a scalar column is now REJECTED at load by
    _assert_no_canonical_head (see the symbol/string column check there), because
    a scalar in a head reaches the report as a bare int with nothing to say what
    it means (#323). Those rules live in the optional
    policy/logic-policy.extra.dl, not here.
    """
    if not specs:
        return
    aliases = relation_aliases() if aliases is None else aliases
    for row in sorted(accepted, key=lambda r: (r["relation"], r["subject"], r["object"])):
        spec = _lookup_typed_spec(row["relation"], specs, aliases)
        if spec is None:
            continue
        scalar, reason = typed_projection_outcome(row, spec)
        if scalar is None:
            print(
                f"typed-relations: {row['object']!r} for {row['relation']!r} "
                f"({row['subject']!r}) {reason}; loading untyped",
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
    accepted_rows = load_accepted_facts()
    base_program = (
        WIRELOG_PROGRAM + _attr_rel_facts(accepted_rows) + "\n" + policy_program + "\n" + accepted_program
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
    # decode_wirelog_value passes values through because step() decodes each row
    # against this side-parsed schema. EasySession re-parses the program to build it
    # and, if that re-parse fails (wirelog's parser disagreeing with the easy facade
    # about what is well-formed), keeps None and runs ON — then _decode_row has no
    # schema, every column falls back to its raw id, and a report prints
    # `flagged: 0 (3)`: a subject that is not in the KB, asserted with rc=0. An
    # over-claim is worse than silence, so refuse to run instead.
    #
    # This is enforced, not documented, on purpose: the same silent-fallback-plus-
    # prose-convention shape is exactly what #323 was filed to remove. The pin is
    # >=1.0.3,<2.0, so a 1.x MINOR is free to introduce such a parser disagreement
    # and the fallback would stay quiet — a version pin cannot catch it, only this
    # check can. Private attribute by necessity: the facade exposes no public way to
    # ask whether decoding is live (see tools/README.md).
    if session._schema_program is None:
        session.close()
        raise FactlogError(
            "the engine could not build a schema for the logic program (pyrewire's "
            "side parse of the assembled program failed), so it would decode every "
            "column as a raw intern id: the report would print bare numbers where "
            "subjects and reasons belong, with a clean exit. Refusing to run. This "
            "is an engine/factlog parser disagreement, not a KB error — re-run "
            "`factlog doctor`, check the pyrewire version against the "
            "`pyrewire>=1.0.3,<2.0` pin in pyproject.toml, and report the policy "
            "text that triggered it."
        )
    for value in policy_string_literals(policy_program):
        session.intern(value)
    accepted = accepted_rows  # already loaded above for _attr_rel_facts
    for row in accepted:
        session.intern(row["subject"])
        session.intern(row["relation"])
        session.intern(row["object"])

    # Intern canonical-atom symbols so the ENGINE can decode any canonical/3 tuple it
    # emits or a rule references. NOT dead code: pyrewire's _decode_row resolves a
    # STRING column through this table and falls back silently to the raw int on a
    # miss, so dropping an intern here does not raise — it prints a bare id where a
    # name belongs. canonical/3 is pure EDB — never a rule head — so we only intern,
    # never insert.
    _c_aliases = relation_aliases()
    if _c_aliases:
        for s, canon, o in canonical_atoms(accepted, _c_aliases):
            session.intern(s)
            session.intern(canon)
            session.intern(o)

    _project_typed_relations(session, specs, accepted, _c_aliases)

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
    query-match path (``relation_row_matches``/``object_matches``/``classify_query``)
    routes value comparison through here, so folding once here makes an NFD-stored
    relation or object meet an NFC-typed query constant (and the reverse) without
    touching any per-path code. Idempotent no-op on NFC-only data, so a KB that was
    already NFC compares byte-identically. Non-amount strings are otherwise returned
    unchanged, so dates/numbers/ordinals/entities keep their form. Total: never
    raises."""
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


def _quoted_constants(line: str) -> list[str]:
    # Shared lexer, not a regex: an escaped quote in a review_required question
    # (`review_required("who said \\" hi")?`) split the old findall at `\\"`, so the
    # report showed a truncated question and the arity check could miscount (#250).
    # strict=False: a malformed query line is surfaced by validate_query, not a crash.
    return _scan_policy(line, strict=False)[1]


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
canonical_value = _canonical_value
is_quoted_string = _is_quoted_string
is_variable = _is_variable
quoted_constants = _quoted_constants


def _relation_match_count(
    query: str,
    facts: list[dict[str, str]],
    aliases: dict[str, str] | None = None,
    hierarchy: dict[str, dict[str, set[str]]] | None = None,
) -> int:
    """How many accepted facts satisfy a relation query — via the shared predicate."""
    if not query.startswith("relation"):
        return 0
    args = _query_args(query)
    if len(args) != 3:
        return 0
    if aliases is None and not _is_variable(args[1]):
        aliases = relation_aliases()
    return sum(1 for row in facts if relation_row_matches(args, row, aliases, hierarchy))


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
    # Derive from QUERY_PREDICATES, the one static query vocabulary the report
    # (validate_query / evaluate_queries) also keys off, so ask and the report can
    # never disagree on which predicates are known — the divergence #306 removed,
    # where `conflict` was in the report's set but not this literal. Policy
    # predicates are the dynamic half, declared per KB.
    allowed_predicates = QUERY_PREDICATES | policy_query_predicates
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
    # Gate membership compared through the SAME fold the matcher uses
    # (relation_row_matches -> _canonical_value), so a query constant and an
    # NFD-stored fact meet in the GATE exactly as they do in the MATCHER. Without
    # this the two disagreed: an NFD fact matched the row but the gate called the
    # constant "not accepted" (route=wiki), breaking the gate/matcher parity #213
    # guarantees for NFD-authored facts (#296). Folded in the gate ONLY — the set
    # builders (entity_set/value_set/allowed_relations) stay raw so the engine
    # emitter, dependency_graph, vocab and display provenance are byte-unchanged.
    # (path stays raw on BOTH sides — self-consistent there — and is out of scope,
    # tracked as #299.)
    entities_c = {canonical_value(e) for e in entities}
    relations_c = {canonical_value(r) for r in relations}
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
        if not _is_variable(subject) and canonical_value(_arg_value(subject)) not in entities_c:
            return False, QUERY_ENTITY_NOT_ACCEPTED, f"relation subject is not an accepted entity: {_arg_value(subject)}"
        # Read relation_aliases() at most once per relation query and hand it to
        # _relation_match_count below: the canonical-acceptance check here and the
        # match count were the two sites that each re-read it per relation query
        # (#242). The read stays gated to a quoted canonical not literally in
        # accepted.dl, so which queries can trigger its raise-on-malformed-file is
        # unchanged (a variable/known-variant relation never reads it here).
        _rel_aliases: dict[str, str] | None = None
        if not _is_variable(relation) and canonical_value(_arg_value(relation)) not in relations_c:
            # A declared canonical name (one whose surface_variants is non-empty)
            # counts as accepted even though the canonical itself may not appear
            # literally in accepted.dl — the stored facts use surface variants.
            _rel_aliases = relation_aliases()
            if not canonical_variants_of(_arg_value(relation), _rel_aliases):
                return False, QUERY_RELATION_NOT_ACCEPTED, f"relation name is not accepted: {_arg_value(relation)}"
        # The gate must know the value hierarchy too. A broader value may be a
        # legitimate query object while appearing in NO accepted fact — that is
        # the whole point of declaring `코호트연구 ⊂ 관찰연구`. Judged by the raw
        # value_set alone, the gate rejected such a query (route=wiki) or, worse,
        # called it a verified negative — asserting "no such fact" about facts the
        # logic report was happily returning (#211). An assertion that is wrong is
        # worse than the silent omission this feature set out to fix, so the gate,
        # the evaluator and the report all read the same declarations.
        _hierarchy = value_hierarchy()
        if not _is_variable(object_):
            _object_value = _arg_value(object_)
            _accepted_objects = {_canonical_value(v) for v in values}
            # A declared ancestor is a legitimate query object even when it appears
            # in no accepted fact — that is the point of `코호트연구 ⊂ 관찰연구`.
            # But the licence is SCOPED TO ITS RELATION. Pooling every relation's
            # ancestors into one vocabulary would let a declaration on one relation
            # make the value "known" everywhere, so a query naming it under an
            # UNRELATED relation would stop being "not our vocabulary" (route=wiki)
            # and become a *verified negative* — the engine asserting "no such fact"
            # about a term the KB never adopted there. A wrong assertion is worse
            # than an honest "cannot express".
            _declared = _canonical_value(_object_value) in declared_ancestors(
                _hierarchy,
                None if _is_variable(relation) else _arg_value(relation),
                _canonical_value,
            )
            if _canonical_value(_object_value) not in _accepted_objects and not _declared:
                return False, QUERY_ENTITY_NOT_ACCEPTED, f"relation object is not an accepted entity: {_object_value}"
        if _relation_match_count(query, facts, _rel_aliases, _hierarchy) == 0:
            return False, QUERY_FACT_ABSENT, "relation query does not match accepted facts"
        return True, QUERY_OK, "passed"
    if predicate == "path":
        if len(args) != 2:
            return False, QUERY_BAD_ARITY, "path query must have start and target arguments"
        if not all(_is_valid_arg(arg) for arg in args):
            return False, QUERY_MALFORMED, "path arguments must be variables or quoted strings"
        for arg in args:
            # Same fold the matcher (path_query_rows/dependency_path) now applies, so
            # the gate and the matcher agree on an NFD-stored entity vs an NFC query
            # constant -- the gate/matcher parity #296 restored for relation/count,
            # now for path too (#299). entities_c was folded once at the top.
            if not _is_variable(arg) and canonical_value(_arg_value(arg)) not in entities_c:
                return False, QUERY_ENTITY_NOT_ACCEPTED, f"path argument is not an accepted entity: {_arg_value(arg)}"
        # Reachability is decided by the ENGINE alone, never re-derived here (#303).
        # The gate has no engine pairs, so it once answered FACT_ABSENT from
        # dependency_path -- a python mirror of the STANDARD edge/path rules that
        # cannot see an edge a logic-policy.extra.dl rule proved. On a pair reachable
        # only through such a policy edge the gate called a *verified negative* while
        # the matcher (path_query_rows, over the engine's fixpoint pairs) answered
        # "reachable" -- the two disagreed on the same query, and because cmd_render
        # skips the engine when the classification is negative, that false negative
        # reached the USER's answer. So the gate stops asserting absence: vocabulary
        # is validated above (entities accepted), and whether a path EXISTS is left to
        # path_query_rows over inferred["path"]. A true negative is then the engine's
        # own empty result (#213's gate/matcher parity strengthened from an
        # approximation to the engine's proof; the #256 reflexive-no-cycle negative is
        # still pinned via ask_router.evaluate, which runs the engine).
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
        if not _is_variable(subject) and canonical_value(_arg_value(subject)) not in entities_c:
            return False, QUERY_ENTITY_NOT_ACCEPTED, f"count subject is not an accepted entity: {_arg_value(subject)}"
        if not _is_variable(relation) and canonical_value(_arg_value(relation)) not in relations_c:
            # A declared canonical name (one whose surface_variants is non-empty)
            # counts as accepted even though the canonical itself may not appear
            # literally in accepted.dl — the stored facts use surface variants.
            if not canonical_variants_of(_arg_value(relation), relation_aliases()):
                return False, QUERY_RELATION_NOT_ACCEPTED, f"count relation is not accepted: {_arg_value(relation)}"
        return True, QUERY_OK, "passed"
    if predicate in policy_query_predicates:
        if len(args) != 2:
            return False, QUERY_BAD_ARITY, "policy query must have entity and reason arguments"
        if not all(_is_valid_arg(arg) for arg in args):
            return False, QUERY_MALFORMED, "policy query arguments must be variables or quoted strings"
        if not _is_variable(args[0]) and canonical_value(_arg_value(args[0])) not in entities_c:
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
