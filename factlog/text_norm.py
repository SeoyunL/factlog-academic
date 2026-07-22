# SPDX-License-Identifier: Apache-2.0
"""Unicode digit vocabulary: what the ``Nd`` category is, and how to fold it.

This module is the **single source of truth for "which characters are decimal
digits"**. It is pure: no engine, no I/O, no regex compilation, no policy. It
answers two questions and takes no position on what a caller does with the
answer:

- :func:`fold_decimal_digits` — respell every Unicode decimal digit as ASCII.
- :func:`non_ascii_digits` — name the non-ASCII ones, for warning text.

The **policy** — fold here, refuse there — lives at the call sites, and they
deliberately disagree. :mod:`factlog.literal_types` **refuses** these characters
in hand-written literals so the author sees the accident while the front matter
is still editable (#388). The import boundary
(:mod:`factlog.integrations.zotero.item_parser`, #398) and the export boundary
(:mod:`factlog.csl`, #399) **fold** them, because there the odd digit comes from
an external library or is already in the store, and refusing would only drop a
value that reads perfectly well. Vocabulary here, policy there.

Both functions key on ``str.isdecimal``, which is EXACTLY the Unicode ``Nd``
category — the set Python's ``\\d`` matches. That equality is the whole reason
this can be one module: the fold and the diagnostic must name the same set, or a
warning would blame a character the parser never rejected.

**The scope is narrower than the name: this is the ``Nd`` module, and the other
normalizations stay where they are.** NFC canonicalisation (``common``, ``cli``)
and the NFKD + combining-mark strip used for fuzzy matching
(``integrations.common.matcher``) do NOT belong here. They answer different
questions with different failure modes, and gathering them under one "text norm"
roof would invite a caller to reach for the wrong one — the duplication this
module exists to end, re-introduced one level up. Anything added here has to be a
claim about decimal digits.
"""
from __future__ import annotations

import unicodedata

_ASCII_DIGITS = frozenset("0123456789")


def fold_decimal_digits(value: str) -> str:
    """*value* with every Unicode decimal digit rewritten as its ASCII equivalent.

    Total and defensive: a non-digit character passes through untouched, so this
    is safe to call on a whole string, a regex group, or a substring. Passing one
    through is the intended contract, not a missing check — the predecessor at the
    Zotero boundary (#398) raised ``ValueError`` on a non-``Nd`` character, which
    made this function a second, accidental guard on whether a capture was right.
    That judgement belongs to the caller's regex: **what got captured is the call
    site's responsibility, and folding it is this function's.** The cost is that a
    mis-captured value now fails later, where it is parsed, rather than here.
    It is also
    length- and position-preserving (one character in, one out), which is what
    makes "fold, then match ``[0-9]``" accept exactly the strings ``\\d`` accepted
    before, with the same numeric result — mixed scripts (``２0２0``) and non-Latin
    ones (``٢٠٢٠``) included. This restates the old ``\\d`` behaviour; it does not
    widen or narrow it.

    Deliberately NOT ``unicodedata.normalize("NFKC", …)``, which is wrong in both
    directions (measured):

    - too broad — it folds ``²`` and ``①`` (category ``No``) into digits, which
      ``\\d`` never matched, so running it over a raw date could invent a year the
      source never stated;
    - too narrow — it leaves Arabic-Indic ``٢٠٢٠``, Devanagari ``२०२०`` and
      Extended Arabic-Indic ``۲۰۲۰`` untouched (they are already NFKC-stable),
      all of which ``\\d`` did match.

    The codebase normalizes to NFC precisely to avoid that class of folding.
    Mapping digit-by-digit is exactly as wide as ``Nd`` and no wider.
    """
    return "".join(
        str(unicodedata.decimal(ch)) if ch.isdecimal() else ch for ch in value
    )


def non_ascii_digits(value: str) -> str:
    """The distinct non-ASCII Unicode decimal digits in *value*, first-appearance
    order, as one string (``""`` when there are none). Pure; never raises.

    EXACTLY the set :mod:`factlog.literal_types`' parsers used to accept and no
    longer do: ``str.isdecimal`` is the ``Nd`` category, which is what ``\\d``
    matched, minus ``0-9``. It is NOT ``str.isdigit`` — that also admits
    superscripts (``²``, category ``No``), which ``\\d`` never matched, so
    reporting one would name a character that was never the cause.

    This exists for the WARNING TEXT, not for parsing. ``date(２０２０,１)`` and
    ``date(2020,1)`` render nearly identically in a terminal, so "does not parse as
    date" alone sends a human hunting for a typo that is invisible. Callers append
    the offending characters; nothing here decides whether a value parses.
    """
    seen: list[str] = []
    for ch in value:
        if ch.isdecimal() and ch not in _ASCII_DIGITS and ch not in seen:
            seen.append(ch)
    return "".join(seen)
