#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Turn an OpenAlex ``Work`` payload into factlog's normalized shape (spec §5.4).

The API's own shape is not what a source file wants. Three translations happen
here, each forced by measured behavior (#51):

* **Identifiers are URLs.** ``id``, ``doi``, and ``ids.pmid`` arrive as
  ``https://openalex.org/W…``, ``https://doi.org/10.…``, and
  ``https://pubmed.ncbi.nlm.nih.gov/…``. §7.1 duplicate detection matches on
  bare DOIs and PMIDs, so they are reduced here.
* **Most fields are optional.** On a live query of 100 works: 37 had no
  abstract, 21 no journal name, 17 no DOI, 8 no authors. Parsing is therefore
  total — every field but the id degrades to ``None``/empty rather than raising.
* **``is_retracted`` is not authoritative.** OpenAlex flagged the Lancet
  Commission dementia report (``W3046275966``, PMID 32738937) as retracted;
  PubMed records no retraction for it. The flag is carried as
  :attr:`ParsedWork.openalex_is_retracted` — deliberately source-scoped, so a
  writer cannot mistake it for the merged ``retracted:`` claim that §7.2 says
  PubMed owns.

Parsing is pure: no network, no filesystem. A malformed payload that carries no
usable work id is the one hard error, since nothing downstream can address it.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from factlog.integrations.arxiv.id_normalizer import ArxivIdError, normalize_arxiv_id
from factlog.integrations.openalex.abstract_util import index_is_complete, restore_abstract
from factlog.integrations.openalex.api_client import (
    OpenAlexError,
    normalize_doi,
    normalize_pmid,
    normalize_work_id,
)

OPENALEX_WORK_URL = "https://openalex.org/"

# OpenAlex orders authorships by this key; the array order is usually already
# correct, but a stable sort makes the author list independent of that.
_AUTHOR_POSITION_ORDER = {"first": 0, "middle": 1, "last": 2}


@dataclass(frozen=True)
class Concept:
    """One OpenAlex concept, with the score that decides whether it becomes a tag."""

    name: str
    score: float | None = None
    level: int | None = None


@dataclass(frozen=True)
class PrimaryTopic:
    """The work's top-scored topic and its four-level hierarchy.

    ``score`` is carried because ``primary_topic`` is simply the highest-scoring
    entry of ``topics[]`` — and that score can be 0.06. Recording the name alone
    would state a confident classification for a work that has none.
    """

    display_name: str
    score: float | None = None
    subfield: str | None = None
    field: str | None = None
    domain: str | None = None


@dataclass(frozen=True)
class ParsedWork:
    """An OpenAlex work reduced to the fields a factlog source file records.

    ``openalex_is_retracted`` is named for its source rather than as a bare
    ``retracted`` on purpose: it is one source's opinion, and a contradicted one
    (see module docstring). §7.3's per-source provenance is where it belongs.

    ``concepts`` is the full scored list. The subset that becomes the source's
    ``tags`` is :attr:`tags` — see its docstring for why the cut is at zero.
    """

    openalex_id: str
    title: str | None = None
    authors: tuple[str, ...] = ()
    year: int | None = None
    journal: str | None = None
    doi: str | None = None
    pmid: str | None = None
    arxiv_id: str | None = None
    concepts: tuple[Concept, ...] = ()
    primary_topic: PrimaryTopic | None = None
    cited_by_count: int | None = None
    work_type: str | None = None
    openalex_is_retracted: bool = False
    abstract: str = ""
    abstract_complete: bool | None = None
    mesh_terms: tuple[str, ...] = ()

    @property
    def openalex_url(self) -> str:
        """The human-facing landing page recorded under "Original source"."""
        return f"{OPENALEX_WORK_URL}{self.openalex_id}"

    @property
    def has_abstract(self) -> bool:
        return bool(self.abstract)

    @property
    def tags(self) -> tuple[str, ...]:
        """Concept names with a positive score, most confident first (#54).

        Across 8 sampled papers, 12 of the 13 concepts that were clearly unrelated
        to the paper's subject scored exactly ``0.00`` (``Paleontology`` on a
        PDE-solver paper, ``Visual arts`` on an object detector). Cutting at zero
        drops 92% of that noise while keeping ~11 concepts per paper; every higher
        threshold removed true parents without removing more noise.

        A concept whose score is *missing* is excluded rather than assumed good:
        tags seed canonical aliases, so an unknown-confidence term is not worth a
        wrong alias. If OpenAlex ever stopped emitting scores this yields empty
        tags — a visible failure, not a silent flood of noise.

        The filter cannot remove wrong-sense entities (``Object (grammar)`` on an
        object-detection paper) — they score like good tags. The P1 human gate is
        what catches those.
        """
        scored = [c for c in self.concepts if isinstance(c.score, float) and c.score > 0]
        return tuple(c.name for c in sorted(scored, key=lambda c: -c.score))


def _text(value: object) -> str | None:
    """A non-empty, stripped string, or None."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _year(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    # A four-digit year; anything else is data we should not silently record.
    return value if 1000 <= value <= 9999 else None


def _optional(normalizer, value: object) -> str | None:
    """Apply a strict normalizer to API data, degrading to None on junk.

    The normalizers raise for *user* input (a mistyped ``--doi`` is worth an
    error); a malformed identifier inside an API payload should not abort the
    import of an otherwise usable record.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return normalizer(value)
    except OpenAlexError:
        return None


def _authors(work: dict) -> tuple[str, ...]:
    authorships = work.get("authorships")
    if not isinstance(authorships, list):
        return ()

    ordered = sorted(
        (a for a in authorships if isinstance(a, dict)),
        # Unknown/missing positions sort after the three known ones, keeping
        # their relative order (sorted() is stable).
        key=lambda a: _AUTHOR_POSITION_ORDER.get(a.get("author_position"), len(_AUTHOR_POSITION_ORDER)),
    )

    names: list[str] = []
    for authorship in ordered:
        author = authorship.get("author")
        name = _text(author.get("display_name")) if isinstance(author, dict) else None
        # `raw_author_name` survives when disambiguation produced no author
        # record; §5.6 Risk 3 asks that we record what the source said.
        name = name or _text(authorship.get("raw_author_name"))
        if name:
            names.append(name)
    return tuple(names)


def _journal(work: dict) -> str | None:
    location = work.get("primary_location")
    if not isinstance(location, dict):
        return None
    source = location.get("source")
    if not isinstance(source, dict):
        return None
    return _text(source.get("display_name"))


def _pmid(work: dict) -> str | None:
    ids = work.get("ids")
    if not isinstance(ids, dict):
        return None
    return _optional(normalize_pmid, ids.get("pmid"))


def _is_arxiv_host(url: str) -> bool:
    """True when ``url``'s host is arxiv.org or a subdomain (e.g. export.arxiv.org).

    The host filter is what lets us feed a URL to :func:`normalize_arxiv_id`
    safely: it admits ``export.arxiv.org`` while excluding non-arXiv preprint
    servers (bioRxiv, SSRN) and — critically — the ``https://doi.org/10.48550/
    arxiv...`` shape that appears in real ``locations[]`` and that the normalizer
    would raise on. ``endswith(".arxiv.org")`` (not ``endswith("arxiv.org")``)
    avoids admitting a look-alike host such as ``notarxiv.org``.
    """
    host = (urlparse(url).hostname or "").lower()
    return host == "arxiv.org" or host.endswith(".arxiv.org")


def _arxiv_id(work: dict) -> str | None:
    """The canonical arXiv base id for this work, from ``locations[]``, or None.

    The id lives in ``locations[]``, never in ``ids`` (measured: 0/5 in #57).
    ``best_oa_location`` is not read — for a published paper it points at the
    journal, not arXiv. Iterate locations in order; per location try
    ``landing_page_url`` then ``pdf_url``; keep only arXiv-hosted URLs; run each
    through the shared :func:`normalize_arxiv_id` (skipping anything it rejects);
    take the first that parses. Every version of a paper collapses to the same
    base, so "first" is deterministic and version-independent. The canonical base
    is emitted so the file matches the read-side normalization.
    """
    locations = work.get("locations")
    if not isinstance(locations, list):
        return None
    for location in locations:
        if not isinstance(location, dict):
            continue
        for field_name in ("landing_page_url", "pdf_url"):
            url = location.get(field_name)
            if not isinstance(url, str) or not url.strip() or not _is_arxiv_host(url):
                continue
            try:
                return normalize_arxiv_id(url).base
            except ArxivIdError:
                continue
    return None


def _score(value: object) -> float | None:
    """A concept/topic score as a float, or None when absent or non-numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _level(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _concepts(work: dict) -> tuple[Concept, ...]:
    items = work.get("concepts")
    if not isinstance(items, list):
        return ()
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("display_name"))
        if name:
            out.append(Concept(name, _score(item.get("score")), _level(item.get("level"))))
    return tuple(out)


def _primary_topic(work: dict) -> PrimaryTopic | None:
    topic = work.get("primary_topic")
    if not isinstance(topic, dict):
        return None
    name = _text(topic.get("display_name"))
    if not name:
        return None

    def nested(key: str) -> str | None:
        node = topic.get(key)
        return _text(node.get("display_name")) if isinstance(node, dict) else None

    return PrimaryTopic(
        display_name=name,
        score=_score(topic.get("score")),
        subfield=nested("subfield"),
        field=nested("field"),
        domain=nested("domain"),
    )


def _named(items: object, key: str) -> tuple[str, ...]:
    if not isinstance(items, list):
        return ()
    names = (_text(item.get(key)) for item in items if isinstance(item, dict))
    return tuple(name for name in names if name)


def _mesh_terms(work: dict) -> tuple[str, ...]:
    """Distinct MeSH descriptor names, in the order OpenAlex first lists them.

    Deduplicated because OpenAlex repeats rows: one work returned 108 ``mesh``
    entries naming 26 distinct descriptors — ``Aging`` four times over (#53). The
    rows differ only by qualifier, and factlog records descriptors, not
    qualifiers, so the repeats carry no information.

    Descriptors only, never ``is_major_topic``: OpenAlex mirrors PubMed's
    descriptor-level flag and drops qualifier-level majorness, which NLM used for
    most of PubMed's history. Major-topic Jaccard against PubMed is 0.10 on
    2001-2009 records (#53).
    """
    seen: dict[str, None] = {}
    for name in _named(work.get("mesh"), "descriptor_name"):
        seen.setdefault(name, None)
    return tuple(seen)


def is_placeholder_title(title: object) -> bool:
    """True when a title is the literal string ``"null"`` (not JSON ``null``).

    W2567289819 records exactly that, which would slug as ``…-2016-null.md``.
    The record is *not* rejected — a paper legitimately titled "Null" is possible
    — so callers warn instead of dropping it.
    """
    return isinstance(title, str) and title.strip().lower() == "null"


def parse_work(work: object) -> ParsedWork:
    """Reduce one ``/works`` payload to :class:`ParsedWork`.

    Raises :class:`OpenAlexError` only when the payload carries no usable work
    id — every other field degrades rather than failing the record.
    """
    if not isinstance(work, dict):
        raise OpenAlexError(f"expected an OpenAlex work object, got {type(work).__name__}")

    raw_id = work.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise OpenAlexError("OpenAlex work payload has no 'id'.")
    openalex_id = normalize_work_id(raw_id)

    index = work.get("abstract_inverted_index")
    abstract = restore_abstract(index)

    return ParsedWork(
        openalex_id=openalex_id,
        # `title` is the spec's field; `display_name` carries the same string and
        # covers payloads selected without `title`.
        title=_text(work.get("title")) or _text(work.get("display_name")),
        authors=_authors(work),
        year=_year(work.get("publication_year")),
        journal=_journal(work),
        doi=_optional(normalize_doi, work.get("doi")),
        pmid=_pmid(work),
        arxiv_id=_arxiv_id(work),
        concepts=_concepts(work),
        primary_topic=_primary_topic(work),
        cited_by_count=_count(work.get("cited_by_count")),
        work_type=_text(work.get("type")),
        openalex_is_retracted=work.get("is_retracted") is True,
        abstract=abstract,
        # Only meaningful when there *is* an abstract; None says "nothing to judge".
        abstract_complete=index_is_complete(index) if abstract else None,
        mesh_terms=_mesh_terms(work),
    )
