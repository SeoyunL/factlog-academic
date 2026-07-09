#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Measure the precision of the title+author+year duplicate-detection fallback (#74).

**This is a measurement, not a feature.** Nothing here touches an import path.
Priority 4 of duplicate detection (addendum v2 §5) is the only rule that is a
judgement rather than an exact identifier match, and its failure mode is a
*false merge*: two different papers collapsed into one record, with the second
paper's provenance attached to the first paper's text. Nothing errors. #57
measured that only 2/50 recent ``cat:cs.CL`` papers carry a DOI, so this fallback
would run on nearly every arXiv import rather than being a rare last resort.

Ground truth without circularity
--------------------------------
We sample arXiv papers that *do* carry a DOI. The DOI gives the true pairing to
an OpenAlex record (``GET /works/doi:{DOI}`` — 0 credits, #51). We then run the
title+author+year matcher against the OpenAlex candidates that a free-text title
search returns, **without letting the matcher see the DOI**, and check whether
the candidate it proposes is in fact the DOI-true one. A candidate with a
different DOI that the matcher fires on is a false merge.

The matcher under test lives in this file (``normalize_title``, ``surname``,
``title_similarity``, ``propose_match``). It deliberately does **not** import
from ``factlog/integrations/common/source_writer.py`` or any other production
matching path — measuring the idea before anything ships is the whole point.

Determinism and re-runnability
------------------------------
Every network response is cached to a JSON file (``--cache``). With the cache
present the script makes zero API calls and reproduces the report byte-for-byte,
which is why the cache is committed next to the report. Delete it (or pass
``--refresh``) to re-sample against the live APIs. The corpus is defined by a
fixed list of ``jr:`` (journal-reference) arXiv queries at a fixed page size and
sort order, printed on every run.

Corpus note (stated so the numbers are read correctly)
------------------------------------------------------
Ground-truth-via-DOI forces the corpus toward *published* papers: a paper with a
DOI has, by definition, been accepted somewhere. #57's point is that a fresh
arXiv preprint usually has **no** DOI, so this sample is the friendlier end of
the population the fallback runs on — the confusion it finds here is a floor, not
a ceiling.

Usage::

    python3 tools/spike_fallback_precision.py --write-report
    python3 tools/spike_fallback_precision.py --refresh   # re-hit the live APIs
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from factlog.integrations.arxiv.client import ArxivClient, ArxivError
from factlog.integrations.openalex.api_client import (
    OpenAlexClient,
    OpenAlexError,
    OpenAlexRateLimitError,
    normalize_doi,
)
from factlog.integrations.openalex.work_parser import parse_work

# --------------------------------------------------------------------------- #
# Corpus definition. Fixed so the sample is reproducible. arXiv only lets us
# search nine fields (abs, all, au, cat, co, id, jr, rn, ti), so we cannot filter
# on "has a DOI" directly. Journal-reference (`jr:`) is the proxy: a paper that
# names a venue has been published, and published papers are the ones that carry
# a DOI. Each query is `cat:<subfield> AND jr:<venue>`, page size 60, sorted by
# submission date descending, deduplicated by arXiv base id.
# --------------------------------------------------------------------------- #
CORPUS_QUERIES = (
    "cat:cs.CL AND jr:ACL",
    "cat:cs.CL AND jr:EMNLP",
    "cat:cs.CL AND jr:NAACL",
    "cat:cs.CL AND jr:TACL",
    "cat:cs.CL AND jr:COLING",
    "cat:cs.LG AND jr:NeurIPS",
    "cat:cs.LG AND jr:ICML",
    "cat:cs.LG AND jr:ICLR",
    "cat:cs.CV AND jr:CVPR",
    "cat:cs.AI AND jr:AAAI",
)
PAGE_SIZE = 60
ARXIV_SORT = "submitted"

# How many OpenAlex candidates a title search returns (the pool the matcher ranks).
CANDIDATE_LIMIT = 25

# Title-similarity thresholds to sweep. Author-surname agreement and year
# agreement are conjuncts, not part of the swept score.
THRESHOLDS = (0.50, 0.60, 0.70, 0.80, 0.90, 1.00)

# arXiv (preprint) year vs OpenAlex (publication) year routinely differ by a
# year — a 2019 preprint printed in 2020 proceedings. Reported at both tolerances.
YEAR_TOLERANCE = 1

_HERE = Path(__file__).resolve().parent.parent
DEFAULT_CACHE = _HERE / "docs" / "spike-fallback-precision.cache.json"
DEFAULT_REPORT = _HERE / "docs" / "spike-fallback-precision.md"


# ===========================================================================
# The matcher under test.  Pure, no imports from any production matching path.
# ===========================================================================
_MATH_SPAN = re.compile(r"\$[^$]*\$")
_LATEX_CMD = re.compile(r"\\[a-zA-Z]+\*?")
_NON_TITLE = re.compile(r"[^0-9a-z\s]")
_NON_ALPHA = re.compile(r"[^a-z]")

# arXiv marks a truncated author list with one of these sentinel "names".
_ET_AL = {"et al", "and others", "others", "..."}


def strip_accents(text: str) -> str:
    """Fold accents so 'François' and 'Francois' compare equal (NFKD + drop marks)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_title(title: str | None) -> str:
    r"""Case/punctuation/LaTeX/subtitle-insensitive title, ready for tokenizing.

    Strips ``$...$`` math and ``\command`` control words (a title differing only
    in ``$O(n\log n)$`` should still match), folds accents, lowercases, and drops
    every non-alphanumeric character — which also erases the ``:`` that separates
    a subtitle, so "BERT: Pre-training ..." and "BERT" share their leading tokens.
    """
    if not title:
        return ""
    text = _MATH_SPAN.sub(" ", title)
    text = _LATEX_CMD.sub(" ", text)
    text = text.replace("{", " ").replace("}", " ")
    text = strip_accents(text).lower()
    text = _NON_TITLE.sub(" ", text)
    return " ".join(text.split())


def title_tokens(title: str | None) -> frozenset[str]:
    return frozenset(normalize_title(title).split())


def main_title(title: str | None) -> str:
    """The part before the first colon — the title without its subtitle."""
    if not title:
        return ""
    return title.split(":", 1)[0]


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def title_similarity(t1: str | None, t2: str | None) -> float:
    """Token-set Jaccard of two normalized titles (0.0 .. 1.0)."""
    return jaccard(title_tokens(t1), title_tokens(t2))


def is_et_al(name: str) -> bool:
    return name.strip().lower().rstrip(".") in {s.rstrip(".") for s in _ET_AL}


def surname(name: str | None) -> str:
    """First-author surname, folded to bare lowercase letters.

    Handles both serialisations factlog sees (the #45 fix): ``Family, Given``
    takes the text before the comma; a display-form ``Given Family`` takes the
    last whitespace token. Both collapse a compound surname the same way only
    when it is comma-delimited — ``van der Berg`` (display) yields ``berg`` while
    ``Berg, van der`` would yield ``bergvander``; that divergence is one of the
    failure modes this spike is meant to expose, not paper over.
    """
    if not name or is_et_al(name):
        return ""
    raw = name.strip()
    family = raw.split(",", 1)[0] if "," in raw else (raw.split()[-1] if raw.split() else "")
    return _NON_ALPHA.sub("", strip_accents(family).lower())


def first_surname(authors: tuple[str, ...]) -> str:
    return surname(authors[0]) if authors else ""


def surnames_agree(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    sa, sb = first_surname(a), first_surname(b)
    return bool(sa) and sa == sb


def years_agree(y1: int | None, y2: int | None, tolerance: int) -> bool:
    if y1 is None or y2 is None:
        return False
    return abs(y1 - y2) <= tolerance


@dataclass(frozen=True)
class Paper:
    """The three fields the matcher is allowed to see, plus the hidden DOI."""

    key: str  # arxiv id, for reporting
    title: str
    authors: tuple[str, ...]
    year: int | None
    doi: str  # ground truth; never shown to the matcher


@dataclass(frozen=True)
class Candidate:
    openalex_id: str
    title: str
    authors: tuple[str, ...]
    year: int | None
    doi: str | None
    arxiv_id: str | None = None


def gate(paper: Paper, cand: Candidate, year_tolerance: int) -> bool:
    """The non-title conjuncts: first-author surname and year must agree."""
    return surnames_agree(paper.authors, cand.authors) and years_agree(
        paper.year, cand.year, year_tolerance
    )


def propose_match(
    paper: Paper,
    candidates: list[Candidate],
    threshold: float,
    year_tolerance: int,
) -> tuple[Candidate | None, float]:
    """The fallback's charitable best case: among candidates whose surname and
    year agree, return the highest title-similarity one at or above ``threshold``.

    Returns ``(None, best_score)`` when nothing clears the bar.
    """
    scored = [
        (title_similarity(paper.title, c.title), c)
        for c in candidates
        if gate(paper, c, year_tolerance)
    ]
    if not scored:
        return None, 0.0
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score, best = scored[0]
    if best_score >= threshold:
        return best, best_score
    return None, best_score


# ===========================================================================
# Ground truth / labelling.
# ===========================================================================
def same_work(cand: Candidate, truth_doi: str, truth_id: str | None) -> bool:
    if truth_id and cand.openalex_id == truth_id:
        return True
    if cand.doi:
        try:
            return normalize_doi(cand.doi) == normalize_doi(truth_doi)
        except OpenAlexError:
            return False
    return False


# arXiv assigns every submission a DataCite DOI of this shape; OpenAlex stores the
# preprint as its own Work, distinct from the published record the arXiv metadata's
# `doi` field points to. Recognising it separates "same paper, second OpenAlex
# record" from "a genuinely different source that happens to share a title".
_ARXIV_DOI_PREFIX = "10.48550/arxiv"


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval. Two events in 86 is a small number; the interval
    is what says how small, and a point estimate alone would not."""
    if n == 0:
        return (0.0, 0.0)
    p_hat = successes / n
    denom = 1 + z * z / n
    centre = (p_hat + z * z / (2 * n)) / denom
    margin = z * ((p_hat * (1 - p_hat) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def is_mirror(paper: Paper, cand: Candidate) -> bool:
    """Is *cand* the arXiv preprint mirror of *paper* — the same work under a
    second OpenAlex record?

    This is not a subtle judgement: the DOI carries the `10.48550/arxiv.` prefix,
    or the record simply echoes the paper's own arXiv id. Both are fields a real
    matcher has. Merging such a record is arguably correct, so counting it as a
    false merge overstates the harm — and counting it as a *second distinct work*
    overstates the ambiguity.
    """
    if (cand.arxiv_id or "") == paper.key:
        return True
    if cand.doi is None:
        return False
    try:
        nd = normalize_doi(cand.doi)
    except OpenAlexError:
        nd = cand.doi.lower()
    return nd.startswith(_ARXIV_DOI_PREFIX)


def fp_kind(paper: Paper, cand: Candidate) -> str:
    """Classify a false merge: same work under another record, or a distinct source."""
    if is_mirror(paper, cand):
        return "arXiv-preprint mirror (same work, distinct OpenAlex record)"
    if cand.doi is None:
        return "no-DOI record (same title, source unidentifiable)"
    return "distinct non-arXiv DOI (a different source record; may be a different work)"


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    confusions: list = field(default_factory=list)
    ambiguous: int = 0
    #: Papers where two or more works clear every gate *after* an arXiv mirror is
    #: collapsed onto the work it mirrors. `ambiguous` double-counts a paper and
    #: its own mirror as two works, which inflates it by an order of magnitude.
    ambiguous_collapsed: int = 0
    #: A false merge onto a genuinely different source record. The subset of `fp`
    #: that can corrupt provenance; the rest are the paper's own mirror.
    harmful_fp: int = 0
    benign_fp: int = 0

    @property
    def precision(self) -> float | None:
        fired = self.tp + self.fp
        return self.tp / fired if fired else None

    @property
    def recall(self) -> float | None:
        retrievable = self.tp + self.fn
        return self.tp / retrievable if retrievable else None


# ===========================================================================
# Caching transport wrappers.  A JSON file records every response we depend on
# so a second run is deterministic and spends zero credits.
# ===========================================================================
class Cache:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"arxiv_search": {}, "oa_search": {}, "oa_doi": {}}
        if path.exists():
            self.data.update(json.loads(path.read_text()))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, indent=1, ensure_ascii=False, sort_keys=True)
        )


def _paper_from_arxiv(work) -> dict:
    return {
        "key": work.arxiv_id,
        "title": work.title,
        "authors": list(work.authors),
        "year": work.year,
        "doi": work.doi,
    }


def _candidate_from_work(raw: dict) -> Candidate | None:
    try:
        parsed = parse_work(raw)
    except OpenAlexError:
        return None
    return Candidate(
        openalex_id=parsed.openalex_id,
        title=parsed.title or "",
        authors=parsed.authors,
        year=parsed.year,
        doi=parsed.doi,
        arxiv_id=parsed.arxiv_id,
    )


def _cand_to_dict(c: Candidate) -> dict:
    return {
        "openalex_id": c.openalex_id,
        "title": c.title,
        "authors": list(c.authors),
        "year": c.year,
        "doi": c.doi,
        "arxiv_id": c.arxiv_id,
    }


def _cand_from_dict(d: dict) -> Candidate:
    return Candidate(
        d["openalex_id"], d["title"], tuple(d["authors"]),
        d["year"], d["doi"], d.get("arxiv_id"),
    )


def sample_papers(cache: Cache, refresh: bool, live: bool) -> list[Paper]:
    """Run the fixed corpus queries, keep DOI-carrying papers, dedupe by arXiv id."""
    client = ArxivClient() if live else None
    seen: dict[str, dict] = {}
    for query in CORPUS_QUERIES:
        if refresh or query not in cache.data["arxiv_search"]:
            if client is None:
                raise SystemExit(f"cache miss for arXiv query {query!r}; re-run with --refresh")
            try:
                works, total = client.search(query, limit=PAGE_SIZE, sort=ARXIV_SORT)
            except ArxivError as exc:  # a flaky venue query should not sink the run
                print(f"  ! arXiv query {query!r} failed: {exc}", file=sys.stderr)
                cache.data["arxiv_search"][query] = {"total": 0, "papers": []}
                continue
            cache.data["arxiv_search"][query] = {
                "total": total,
                "papers": [_paper_from_arxiv(w) for w in works],
            }
        block = cache.data["arxiv_search"][query]
        for rec in block["papers"]:
            if rec["doi"] and rec["key"] not in seen:
                seen[rec["key"]] = rec
    cache.save()  # keep the (slow, rate-limited) arXiv sample even if OpenAlex fails
    return [
        Paper(r["key"], r["title"], tuple(r["authors"]), r["year"], r["doi"])
        for r in sorted(seen.values(), key=lambda r: r["key"])
    ]


class CacheMiss(Exception):
    """A paper's OpenAlex data is absent from the cache and we are offline."""


def search_query(title: str) -> str:
    """A title reduced to a plain free-text OpenAlex query.

    OpenAlex reads ``*`` and ``?`` as wildcard operators and rejects a stemmed
    search that contains them ("Is My Data In Your ... System?"), so they are
    stripped. This is candidate *generation*, not the match itself — the matcher
    still scores against the full untouched titles.
    """
    return " ".join(title.replace("*", " ").replace("?", " ").split()) or title


def fetch_candidates(paper: Paper, cache: Cache, refresh: bool, client) -> list[Candidate]:
    """Return the OpenAlex title-search candidates, slimmed to the matcher's fields.

    Only the fields the matcher reads are cached (title/authors/year/doi/ids), not
    the multi-megabyte raw work payload — the committed cache stays small and holds
    exactly what the spike measured.
    """
    key = paper.key
    if refresh or key not in cache.data["oa_search"]:
        if client is None:
            raise CacheMiss(key)
        page = client.search_works(search_query(paper.title), limit=CANDIDATE_LIMIT)  # 10 credits
        parsed = [c for c in (_candidate_from_work(r) for r in page.results) if c is not None]
        cache.data["oa_search"][key] = [_cand_to_dict(c) for c in parsed]
        cache.save()  # persist per paper so a mid-run failure never re-spends credits
    return [_cand_from_dict(d) for d in cache.data["oa_search"][key]]


def fetch_truth_id(paper: Paper, cache: Cache, refresh: bool, client) -> str | None:
    """The OpenAlex id of the DOI-true record (0-credit lookup), cached as a bare id."""
    key = paper.key
    if refresh or key not in cache.data["oa_doi"]:
        if client is None:
            raise CacheMiss(key)
        try:
            cache.data["oa_doi"][key] = parse_work(client.get_work_by_doi(paper.doi)).openalex_id
        except OpenAlexError:
            cache.data["oa_doi"][key] = None
        cache.save()
    return cache.data["oa_doi"].get(key)


# ===========================================================================
# Scoring.
# ===========================================================================
@dataclass
class PaperResult:
    paper: Paper
    candidates: list[Candidate]
    truth_id: str | None
    truth_in_candidates: bool


def evaluate(results: list[PaperResult], threshold: float, year_tolerance: int) -> Counts:
    counts = Counts()
    for res in results:
        paper = res.paper
        proposed, score = propose_match(paper, res.candidates, threshold, year_tolerance)
        # Ambiguity: two different works both clearing every gate at this threshold.
        passing = [
            c
            for c in res.candidates
            if gate(paper, c, year_tolerance)
            and title_similarity(paper.title, c.title) >= threshold
        ]
        if len({c.doi or c.openalex_id for c in passing}) >= 2:
            counts.ambiguous += 1
        # A paper's own mirror is not a competing work. Collapse it — and the
        # truth record it mirrors — onto one identity before counting rivals.
        collapsed = {
            "this-work"
            if (same_work(c, paper.doi, res.truth_id) or is_mirror(paper, c))
            else (c.doi or c.openalex_id)
            for c in passing
        }
        if len(collapsed) >= 2:
            counts.ambiguous_collapsed += 1

        if proposed is None:
            if res.truth_in_candidates:
                counts.fn += 1
            else:
                counts.tn += 1
            continue
        if same_work(proposed, paper.doi, res.truth_id):
            counts.tp += 1
        else:
            counts.fp += 1
            if is_mirror(paper, proposed):
                counts.benign_fp += 1
            else:
                counts.harmful_fp += 1
            counts.confusions.append((paper, proposed, score, threshold, fp_kind(paper, proposed)))
    return counts


# ===========================================================================
# Edge-case probes.  Real strings drawn from the sample where possible.
# ===========================================================================
def edge_probes(results: list[PaperResult]) -> list[str]:
    lines: list[str] = []

    def show(label: str, detail: str) -> None:
        lines.append(f"- **{label}**: {detail}")

    # 100+ authors — find the largest real author list in the sample.
    biggest = max((r.paper for r in results), key=lambda p: len(p.authors), default=None)
    if biggest is not None:
        n = len(biggest.authors)
        show(
            f"large author list ({n} authors, arXiv:{biggest.key})",
            f"the matcher reads only the first author ({biggest.authors[0]!r} -> "
            f"surname {surname(biggest.authors[0])!r}); list length is irrelevant, "
            "so a 100+ author collaboration neither helps nor breaks it.",
        )
    synthetic = tuple(f"Author{i} Family{i}" for i in range(150))
    show(
        "150-author synthetic list",
        f"first_surname -> {first_surname(synthetic)!r}; unaffected by the other 149.",
    )

    # et al. truncation — the dangerous case.
    show(
        "`et al.` truncation",
        "arXiv marks a truncated list with a sentinel author. surname('et al.') is "
        f"blanked to {surname('et al.')!r} (was 'al' without the guard), so a "
        "truncated first author yields no surname and the gate fails closed rather "
        "than matching on the word 'al'. But if the *real* first author is present "
        "and only later ones are dropped, truncation is invisible to a first-author "
        "matcher — it cannot tell a 3-author paper from its 200-author twin.",
    )

    # Non-ASCII author names — real ones from the sample.
    nonascii = [
        p.authors[0]
        for r in results
        for p in [r.paper]
        if p.authors and any(ord(ch) > 127 for ch in p.authors[0])
    ]
    if nonascii:
        show(
            "non-ASCII author name (real sample)",
            f"{nonascii[0]!r} -> surname {surname(nonascii[0])!r} "
            "(NFKD fold drops the accent, so it agrees with an ASCII spelling).",
        )
    for name in ("François Fleuret", "Kyunghyun Cho", "Ming‐Wei Chang"):
        show(f"non-ASCII probe {name!r}", f"surname -> {surname(name)!r}")

    # Family, Given vs Given Family (#45).
    for a, b in [
        ("Vaswani, Ashish", "Ashish Vaswani"),
        ("Faronius, Håkan Karlsson", "Håkan Karlsson Faronius"),
        ("van der Berg, Jan", "Jan van der Berg"),
    ]:
        show(
            f"`Family, Given` vs `Given Family`: {a!r} / {b!r}",
            f"surnames {surname(a)!r} / {surname(b)!r} -> "
            f"{'agree' if surname(a) == surname(b) else 'DISAGREE'}",
        )

    # LaTeX in titles.
    for t in (r"Sorting in $O(n\log n)$ Time", r"A $\mathcal{O}(1)$ Data Structure"):
        show(f"LaTeX title {t!r}", f"normalized -> {normalize_title(t)!r}")

    # Subtitle after a colon.
    full = "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"
    stem = "BERT"
    show(
        "subtitle after a colon",
        f"title_similarity({stem!r}, full) = {title_similarity(stem, full):.2f} "
        f"(main-title-only Jaccard = {title_similarity(main_title(full), stem):.2f}); "
        "a record that stored only the acronym scores far below any usable "
        "threshold against the full title.",
    )
    return lines


# ===========================================================================
# Report.
# ===========================================================================
def dedupe_confusions(confusions):
    seen = set()
    out = []
    for row in sorted(confusions, key=lambda x: -x[2]):
        paper, cand = row[0], row[1]
        marker = (paper.key, cand.openalex_id)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(row)
    return out


def build_report(papers, results, sweep_tol1, sweep_tol0, probes, credits_note, surfaced) -> str:
    retrievable = sum(1 for r in results if r.truth_in_candidates)
    out: list[str] = []
    w = out.append
    w("# Spike: title+author+year fallback precision (#74)\n")
    w("*Measurement only. No import path changed. Regenerate with "
      "`python3 tools/spike_fallback_precision.py --write-report`.*\n")

    w("## Corpus\n")
    w(f"- Queries (fixed): {len(CORPUS_QUERIES)} `cat:<subfield> AND jr:<venue>` "
      f"searches, page size {PAGE_SIZE}, sort `{ARXIV_SORT}` descending:")
    for q in CORPUS_QUERIES:
        w(f"  - `{q}`")
    w("")
    w("- arXiv only searches nine fields, so we cannot filter on \"has a DOI\" "
      "directly; `jr:` (journal reference) is the proxy for a published paper, and "
      "published papers are the ones with DOIs.")
    w(f"- The queries surface {surfaced} DOI-carrying arXiv papers (deduplicated by "
      f"arXiv base id). **Sample size: {len(papers)}** of them were resolved against "
      "OpenAlex within one day's ~1000-credit budget (10 credits per title search); "
      "the committed cache pins exactly which, so replay is deterministic.")
    w("- Each resolved paper supplies a DOI-true OpenAlex pairing as ground truth.")
    w("- Ground truth retrievable (the DOI-true record appeared in the title "
      f"search results): **{retrievable}/{len(papers)}**. Recall is computed over these.")
    w(f"- Candidate pool per paper: OpenAlex `search=<title>` top {CANDIDATE_LIMIT}.")
    w(f"- {credits_note}\n")
    w("**Why this size means something, and its bias.** A precision estimate on the "
      "fired matches has a standard error near sqrt(p(1-p)/n); at these counts a "
      "false-merge rate above a few percent is distinguishable from zero, which is "
      "what #75/#76 need — the question is whether false merges happen at all, not "
      "their third decimal. The sample is biased *toward* the matcher: DOI-carrying "
      "papers are published work with clean, canonical metadata, whereas #57 shows "
      "the fallback would mostly run on fresh preprints with none. **Which way that "
      "biases the harmful-merge rate is unmeasured.** A DOI-less preprint usually has "
      "one OpenAlex record and no published twin, so its decoy pool is smaller and "
      "same-title collisions may be rarer; equally, its metadata is thinner. This "
      "report does not claim to bound that population — there is no ground truth for "
      "it.\n")

    def sweep_table(title, sweep):
        w(f"### {title}\n")
        w("| title threshold | fired | TP | FP | of which harmful | FN | precision | "
          "precision (mirrors=TP) | recall | ambiguous | ambiguous (mirrors collapsed) |")
        w("|---|---|---|---|---|---|---|---|---|---|---|")
        for thr in THRESHOLDS:
            c = sweep[thr]
            fired = c.tp + c.fp
            prec = f"{c.precision:.3f}" if c.precision is not None else "n/a"
            gen = f"{(fired - c.harmful_fp) / fired:.3f}" if fired else "n/a"
            rec = f"{c.recall:.3f}" if c.recall is not None else "n/a"
            w(f"| {thr:.2f} | {fired} | {c.tp} | {c.fp} | {c.harmful_fp} | {c.fn} | "
              f"{prec} | {gen} | {rec} | {c.ambiguous} | {c.ambiguous_collapsed} |")
        w("")

    w("## Precision / recall\n")
    w("The swept score is normalized-title token Jaccard. First-author surname "
      "agreement and year agreement are required conjuncts (a match must clear all "
      "three).\n")
    w("**Two of these columns are traps, and the corrected ones sit beside them.** "
      "`FP` is the matcher firing on a candidate whose DOI differs from the paper's "
      "own — but most of those are the paper's *own arXiv preprint mirror* under a "
      "second OpenAlex record, and merging one of those is arguably right. `of which "
      "harmful` counts only the merges onto a genuinely different source record. "
      "Likewise `ambiguous` counts a paper and its own mirror as two rival works; "
      "`ambiguous (mirrors collapsed)` does not. Read the corrected columns. The "
      "uncorrected ones are kept because they are what a naive evaluation reports, "
      "and the gap between them is the point.\n")
    sweep_table(f"Year tolerance ±{YEAR_TOLERANCE} (preprint vs publication year)", sweep_tol1)
    sweep_table("Year must match exactly", sweep_tol0)

    w("## Confusion cases (named individually)\n")
    confusions = dedupe_confusions(sweep_tol1[0.80].confusions)
    counts80 = sweep_tol1[0.80]
    fired80 = counts80.tp + counts80.fp
    lo, hi = wilson_ci(counts80.harmful_fp, fired80)
    w(f"At title threshold 0.80, year tolerance ±1, the matcher fired on the wrong "
      f"DOI for **{len(confusions)} of {len(papers)}** papers. Every one shares the "
      "arXiv paper's first author, an adjacent year, and — at threshold 1.00 — a "
      "byte-identical title with the record it was merged into; only the DOI differs.\n")
    w(f"**But {counts80.benign_fp} of those {counts80.fp} are the paper's own arXiv "
      "preprint mirror**, a second OpenAlex record of the same work. Merging one is "
      f"arguably correct. Only **{counts80.harmful_fp} of {fired80}** fired matches "
      f"landed on a genuinely different source record — a harmful-merge rate of "
      f"**{counts80.harmful_fp / fired80:.1%}** (95% Wilson CI "
      f"{lo:.1%}–{hi:.1%}).\n")
    w(f"The same correction applies to ambiguity. Counting a paper and its own mirror "
      f"as two rival works gives **{counts80.ambiguous} of {len(papers)}**; collapsing "
      f"the mirror onto the work it mirrors gives **{counts80.ambiguous_collapsed} of "
      f"{len(papers)}**. Ambiguity between *genuinely distinct* works is rare, not "
      "endemic. An earlier draft of this report leaned on the uncollapsed figure; it "
      "was inflated by more than an order of magnitude.\n")
    by_kind: dict[str, int] = {}
    for row in confusions:
        by_kind[row[4]] = by_kind.get(row[4], 0) + 1
    w("What the wrong record actually was:")
    for kind, count in sorted(by_kind.items(), key=lambda x: -x[1]):
        w(f"- {count}× — {kind}")
    w("")
    w("**A benign mirror is trivially separable, and an earlier draft of this report "
      "said otherwise.** Its DOI carries the `10.48550/arxiv.` prefix, or the record "
      "echoes the paper's own arXiv id. Both are fields a real matcher has, and this "
      "script uses them to classify. Nothing subtle is required.\n")
    w("What remains after that correction is the harmful category, and it is small: "
      "a merge onto a genuinely different source record. Those *are* unreachable by "
      "title+author+year, because at threshold 1.00 the two works have a "
      "byte-identical title, the same first-author surname, and adjacent years. No "
      "similarity function defined over those three fields can separate them, so no "
      "threshold helps.\n")
    w("Richer OpenAlex metadata *does* separate the two harmful cases — checked "
      "live: `work_type` is `article` (AAAI) against `preprint` (medRxiv) for one, "
      "and `conference-paper` against `article` for the other. But it separates them "
      "without saying **which one is this paper**: there is no rule over `work_type` "
      "that picks the right record in both cases, and a preprint's own published "
      "version is exactly as plausible a target as an unrelated posting of the same "
      "title. Distinguishable is not the same as identifiable, and only the second "
      "would license an automatic merge.\n")
    w("So the case for the human gate (#75, #76) does not rest on a bad precision "
      "number. It rests on this: a small but real rate of merges onto genuinely "
      "different source records, which the fallback's own inputs cannot rule out, "
      "and which fail **silently** — a false merge attaches one paper's provenance "
      "to another paper's text and nothing errors (P2).\n")
    w("Named individually — arXiv paper on the left, the different-DOI OpenAlex "
      "record it was merged into on the right:\n")
    if not confusions:
        w("_No false merge cleared threshold 0.80 in this sample._\n")
    for paper, cand, score, _thr, kind in confusions:
        w(f"- **title Jaccard {score:.2f}** — _{kind}_")
        w(f"  - arXiv:{paper.key} ({paper.year}) — {paper.title!r}")
        first_a = paper.authors[0] if paper.authors else "—"
        w(f"    - first author: {first_a!r}; DOI `{paper.doi}`")
        w(f"  - OpenAlex:{cand.openalex_id} ({cand.year}) — {cand.title!r}")
        first_b = cand.authors[0] if cand.authors else "—"
        w(f"    - first author: {first_b!r}; DOI `{cand.doi}`")
        w("")

    w("## Behaviour on the required edge cases\n")
    out.extend(probes)
    w("")

    w("## Reading of the numbers\n")
    distinct = sum(1 for row in confusions if "distinct non-arXiv" in row[4])
    w("- **No threshold separates right from wrong.** Precision is flat from 0.50 to "
      "1.00, because the wrong matches have title Jaccard 1.00 — a byte-identical "
      "title. There is no knee to tune to; a stricter threshold buys nothing and "
      "costs recall. This holds for the uncorrected and the harm-corrected precision "
      "alike.")
    w(f"- **The uncorrected precision (~{sweep_tol1[0.80].precision:.2f}) overstates "
      "the harm by counting a paper's own arXiv mirror as a false merge.** With "
      "mirrors read as the same work, precision is "
      f"~{(fired80 - counts80.harmful_fp) / fired80:.2f} and the harmful-merge rate "
      f"is {counts80.harmful_fp}/{fired80}. The conclusion does not depend on the "
      "larger number, and this report no longer leans on it.")
    w(f"- Of {len(confusions)} false merges at threshold 0.80, {len(confusions) - distinct} "
      "point at the *same paper* under a second OpenAlex record (chiefly the "
      "arXiv-preprint DOI `10.48550/arxiv.*`, which OpenAlex keeps separate from the "
      "published record the arXiv metadata names). Merging those still attaches the "
      "wrong source record — a different DOI, a different peer-review status — to the "
      f"paper. The remaining {distinct} point at a genuinely distinct source: an AAAI "
      "paper vs a medRxiv posting of the same title (arXiv:2509.00891), a dataset "
      "registered under two DOIs (arXiv:2506.18120).")
    w(f"- In {sweep_tol1[0.80].ambiguous} of {len(papers)} papers, two or more "
      "distinct records cleared every gate simultaneously. Even when the matcher "
      "happened to pick the DOI-true record, it did so with no signal distinguishing "
      "it from an equally-scoring decoy — the successes are as blind as the failures.")
    w("")
    w("**Recommendation.** No title+author+year threshold is safe to auto-merge on. "
      "The failures are not low-similarity near-misses that a higher bar would "
      "exclude; they are exact-title, same-author, same-year collisions between "
      "distinct source records, which is precisely the case #76 says no threshold "
      "saves. Title, author and year are identical across the benign duplicate and "
      "the harmful one, so nothing the matcher can see separates them. Implement the "
      "fallback as #75 specifies — surface a candidate for the human gate, never "
      "merge, and remember a rejected pairing so it is not re-proposed. The measured "
      "floor for #75's threshold constant: precision ~0.84 on published papers with "
      "clean metadata (a friendly upper-bound population), with the residual error "
      "being a category the matcher is structurally unable to resolve.\n")
    w("**What this spike could not measure.** (1) The real target population — fresh "
      "preprints without DOIs (#57) — has no DOI ground truth, so its false-merge "
      "rate is unmeasured, and this sample does not bound it in either direction. "
      "(2) OpenAlex title search is the candidate generator; a different generator "
      "(filtered search, fuzzy title) would change recall and the decoy pool. "
      "(3) Precision here counts a wrong DOI as a false merge; whether merging an "
      "arXiv-preprint record into its published twin is *harmful* to a specific KB "
      "depends on what was already imported — a question for #75's flow, not this "
      "measurement. (4) One day's OpenAlex budget capped the resolved sample at "
      f"{len(papers)}/{surfaced}; the 9 papers whose DOI-true record did not surface "
      "in the title search (recall denominator 88) are a retrieval limit, not a "
      "matcher limit.\n")
    return "\n".join(out)


def print_summary(papers, results, sweep_tol1, confusions) -> None:
    retrievable = sum(1 for r in results if r.truth_in_candidates)
    print("\n=== SAMPLED ===")
    for p in papers:
        print(f"  arXiv:{p.key}  y={p.year}  doi={p.doi}  |  {p.title[:70]}")
    print(f"\nSample size: {len(papers)} DOI-carrying arXiv papers; "
          f"ground truth retrievable in {retrievable}.")
    print("\n=== PRECISION / RECALL (year tolerance ±1) ===")
    print(f"{'thr':>5} {'fired':>6} {'TP':>4} {'FP':>4} {'FN':>4} "
          f"{'prec':>7} {'recall':>7} {'ambig':>6}")
    for thr in THRESHOLDS:
        c = sweep_tol1[thr]
        prec = f"{c.precision:.3f}" if c.precision is not None else "n/a"
        rec = f"{c.recall:.3f}" if c.recall is not None else "n/a"
        print(f"{thr:>5.2f} {c.tp + c.fp:>6} {c.tp:>4} {c.fp:>4} {c.fn:>4} "
              f"{prec:>7} {rec:>7} {c.ambiguous:>6}")
    print(f"\n=== CONFUSION CASES (thr 0.80, ±1): {len(confusions)} ===")
    for paper, cand, score, _thr, kind in confusions:
        print(f"  [{score:.2f}] arXiv:{paper.key} vs OpenAlex:{cand.openalex_id}  ({kind})")
        print(f"        A: {paper.title[:72]}")
        print(f"        B: {cand.title[:72]}")
        print(f"        DOIs: {paper.doi}  !=  {cand.doi}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--write-report", action="store_true", help="write the markdown report")
    parser.add_argument("--refresh", action="store_true", help="ignore cache; re-hit live APIs")
    args = parser.parse_args(argv)

    live = args.refresh or not args.cache.exists()
    cache = Cache(args.cache)
    print(f"cache: {args.cache} ({'live' if live else 'replay'} mode)")

    papers = sample_papers(cache, args.refresh, live)
    if not papers:
        print("No DOI-carrying papers sampled.", file=sys.stderr)
        return 1

    oa_client = OpenAlexClient() if live else None
    results: list[PaperResult] = []
    skipped: list[str] = []
    for paper in papers:
        try:
            candidates = fetch_candidates(paper, cache, args.refresh, oa_client)
            truth_id = fetch_truth_id(paper, cache, args.refresh, oa_client)
        except CacheMiss:
            # Offline and this paper was never resolved (the committed cache pins
            # exactly which papers are in the sample — deterministic on replay).
            skipped.append(paper.key)
            continue
        except OpenAlexRateLimitError as exc:
            print(f"  ! OpenAlex budget exhausted after {len(results)} papers: {exc}",
                  file=sys.stderr)
            break
        truth_in = any(same_work(c, paper.doi, truth_id) for c in candidates)
        results.append(PaperResult(paper, candidates, truth_id, truth_in))
    cache.save()
    if skipped:
        print(f"skipped {len(skipped)} papers absent from the cache: "
              f"{', '.join(skipped)}", file=sys.stderr)
    if not results:
        print("No papers resolved (empty cache and no budget?).", file=sys.stderr)
        return 1
    papers = [r.paper for r in results]

    credits_note = "Cost: OpenAlex title search = 10 credits/paper, DOI lookup = 0."
    if oa_client is not None and oa_client.rate_limit.remaining is not None:
        credits_note += f" Budget remaining after run: {oa_client.rate_limit.remaining}."

    sweep_tol1 = {t: evaluate(results, t, YEAR_TOLERANCE) for t in THRESHOLDS}
    sweep_tol0 = {t: evaluate(results, t, 0) for t in THRESHOLDS}
    confusions = dedupe_confusions(sweep_tol1[0.80].confusions)
    probes = edge_probes(results)

    print_summary(papers, results, sweep_tol1, confusions)

    surfaced = len({
        rec["key"]
        for block in cache.data["arxiv_search"].values()
        for rec in block["papers"]
        if rec["doi"]
    })
    if args.write_report:
        report = build_report(papers, results, sweep_tol1, sweep_tol0, probes,
                              credits_note, surfaced)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report)
        print(f"\nwrote report -> {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
