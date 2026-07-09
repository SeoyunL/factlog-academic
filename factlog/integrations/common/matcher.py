# SPDX-License-Identifier: Apache-2.0
r"""Title+author+year similarity — the fallback duplicate *matcher* (#75, part 2 of #66).

## What this is, and what it is emphatically not

Priority 4 of duplicate detection (spec §7.1 addendum v2 §5) is the only rule that
is a judgement rather than an exact-identifier match. When DOI, PMID and the
normalized arXiv id all miss, two papers can still be the same work — or two
genuinely different works that happen to share a title, a first author and a year.

This module scores that resemblance. It **never decides a merge.** The spike that
measured it (``docs/spike-fallback-precision.md``, #74, corrected in #88) found the
harmful failures are unreachable by these three fields: at title Jaccard 1.00 the
two works have a byte-identical title, the same first-author surname and adjacent
years, so *no* threshold separates a real duplicate from a distinct source record.
Precision is flat across the whole sweep. The matcher therefore only ever
*surfaces a candidate for a human*; the caller (``BaseSourceWriter``) still imports
the paper as a new file (see :data:`TITLE_SIMILARITY_THRESHOLD`).

## Ported, not reinvented

``normalize_title``, ``title_similarity``, ``surname``, ``surnames_agree`` and
``years_agree`` are lifted from ``tools/spike_fallback_precision.py`` so production
scores exactly what the spike measured: ``$...$`` math and ``\command`` control
words stripped, ``{}`` groups opened, NFKD accent fold, lowercase, every
non-alphanumeric dropped (which also erases the ``:`` before a subtitle), then
token-set Jaccard.

The one deliberate divergence from the spike is :func:`surname`. The spike's own
docstring flags that ``van der Berg, Jan`` folds to ``vanderberg`` while
``Jan van der Berg`` folds to ``berg`` — the two serializations disagree. That is
the exact #45 bug class, and the spike left it *exposed on purpose* to measure it.
Production must not reintroduce it, so :func:`surname` folds a trailing run of
nobiliary particles into the family name for the display form, making both
serializations agree. It still fails *closed*: a name it cannot parse yields ``""``
and the gate refuses to fire, so it never manufactures a false match.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: Minimum normalized-title token Jaccard for a candidate to surface. **0.80.**
#:
#: This is a *recall* knob, not a precision one, and the distinction is the whole
#: point (#74/#88). On the spike's DOI-carrying sample the harmful-merge rate is
#: **2 of 86 fired matches (2.3%, 95% Wilson CI 0.6-8.1%)** and precision is *flat
#: from 0.50 to 1.00* — the two harmful pairs have a byte-identical title, the same
#: first-author surname and adjacent years, so no threshold separates them from a
#: benign duplicate. Raising or lowering this number changes how many candidates a
#: human is shown, never whether a merge is safe (the matcher never merges). The
#: retracted precision figure the first report gave must not be read as the design
#: number: it counted a paper's own arXiv preprint mirror as a false merge (12 of
#: 14), which is arguably a correct merge, not a harm.
TITLE_SIMILARITY_THRESHOLD = 0.80

#: An arXiv preprint's submission year and its published year routinely differ by
#: one (a 2023 preprint printed in 2024 proceedings), so year agreement is within
#: +/-1. Wider would let unrelated same-title works collide; exact would drop real
#: duplicates. Same tolerance the spike reported its headline numbers at.
YEAR_TOLERANCE = 1

_MATH_SPAN = re.compile(r"\$[^$]*\$")
_LATEX_CMD = re.compile(r"\\[a-zA-Z]+\*?")
_NON_TITLE = re.compile(r"[^0-9a-z\s]")
_NON_ALPHA = re.compile(r"[^a-z]")

# arXiv marks a truncated author list with one of these sentinel "names"; treating
# one as a surname would match on the word "al", so they blank to "".
_ET_AL = {"et al", "and others", "others", "..."}

# Lowercase nobiliary particles that belong to the family name, not the given name.
# Used only for the display form ``Given ... Family`` so its surname agrees with the
# comma form ``Family, Given`` (the #45 fix). Conservative on purpose: ``mac``/``mc``
# and ``st`` are omitted because they are normally fused into the surname token
# itself (``MacLeod``, ``St John`` as one unit is rare in author metadata), and
# wrongly treating a given-name token as a particle would corrupt a real surname.
_SURNAME_PARTICLES = frozenset({
    "van", "von", "der", "den", "de", "del", "della", "di", "da", "dos", "das",
    "du", "la", "le", "lo", "ter", "ten", "bin", "ibn", "abu", "av", "zu",
})


def strip_accents(text: str) -> str:
    """Fold accents so ``Francois`` and ``François`` compare equal (NFKD + drop marks)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_title(title: str | None) -> str:
    r"""Case/punctuation/LaTeX/subtitle-insensitive title, ready for tokenizing.

    Strips ``$...$`` math and ``\command`` control words, opens ``{}`` groups, folds
    accents, lowercases, and drops every non-alphanumeric character — which also
    erases the ``:`` separating a subtitle, so ``BERT: Pre-training ...`` and
    ``BERT`` share their leading tokens.
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

    Handles the two serializations factlog sees, and makes them *agree* (the #45
    fix the spike deliberately left broken to measure):

    * ``Family, Given`` -> the text before the comma.
    * ``Given Family`` / ``Given van der Family`` -> the last token, plus any run of
      nobiliary particles immediately before it (``van``, ``der``, ...), so
      ``Jan van der Berg`` folds to ``vanderberg`` exactly as ``van der Berg, Jan``
      does.

    A truncation sentinel (``et al.``) or an unparseable name yields ``""``; the
    gate then refuses to fire, failing closed rather than matching on noise.
    """
    if not name or is_et_al(name):
        return ""
    raw = name.strip()
    if "," in raw:
        family = raw.split(",", 1)[0]
    else:
        tokens = raw.split()
        if not tokens:
            return ""
        # The last token is always part of the family name. Walk backwards over any
        # nobiliary particles preceding it, but never past index 0 — the first token
        # is the given name and must not be swallowed even if it looks like a particle.
        start = len(tokens) - 1
        cursor = start - 1
        while cursor >= 1 and tokens[cursor].lower().rstrip(".") in _SURNAME_PARTICLES:
            start = cursor
            cursor -= 1
        family = " ".join(tokens[start:])
    return _NON_ALPHA.sub("", strip_accents(family).lower())


def first_surname(authors) -> str:
    """The surname of the first author, or ``""`` for an empty author list."""
    authors = tuple(authors)
    return surname(authors[0]) if authors else ""


def surnames_agree(a, b) -> bool:
    """True when both first-author surnames are non-empty and equal."""
    sa, sb = first_surname(a), first_surname(b)
    return bool(sa) and sa == sb


def years_agree(y1: int | None, y2: int | None, tolerance: int = YEAR_TOLERANCE) -> bool:
    """True when both years are present and within ``tolerance`` of each other."""
    if y1 is None or y2 is None:
        return False
    return abs(y1 - y2) <= tolerance


@dataclass(frozen=True)
class MatchInput:
    """The three fields the matcher is allowed to see for one paper.

    ``first_author`` is the raw first-author string (``surname`` folds it); an empty
    string disables the surname gate for that paper. ``year`` is an int or ``None``;
    ``title`` is the raw (un-normalized) title.
    """

    first_author: str
    year: int | None
    title: str


def score_pair(incoming: MatchInput, existing: MatchInput,
               tolerance: int = YEAR_TOLERANCE) -> float | None:
    """Title Jaccard of *incoming* vs *existing*, or ``None`` if the gate blocks it.

    The gate is the two required conjuncts the spike never swept: the first-author
    surnames must agree and the years must agree within ``tolerance``. Only when
    both hold is a title score meaningful — a shared title alone is not a duplicate.
    Returns the Jaccard score (which the caller compares to
    :data:`TITLE_SIMILARITY_THRESHOLD`) or ``None`` when the gate refuses.
    """
    if not surnames_agree((incoming.first_author,), (existing.first_author,)):
        return None
    if not years_agree(incoming.year, existing.year, tolerance):
        return None
    return title_similarity(incoming.title, existing.title)
