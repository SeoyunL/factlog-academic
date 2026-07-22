#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""DOI prefix grammar, and the one fold that may be applied to it.

This module is the **single source of truth for what a DOI prefix is** and for
the asymmetry every DOI-handling site in factlog obeys: **the prefix is folded to
ASCII digits, the suffix is preserved byte for byte.** Under ISO 26324 the
registrant code in ``10.<registrant>`` is a decimal number, so ``10.１２３４`` is a
*spelling* of ``10.1234`` and both name one registrant. The suffix is an opaque
string, where respelling a character would invent a different identifier.

Two call sites need that fold and they are not the same kind of site, which is
why the grammar lives here rather than at either of them:

- :func:`~factlog.integrations.common.source_writer.normalize_cross_id` folds a
  **derived comparison key** (#405), additionally lowercasing it because DOIs are
  case-insensitive.
- :mod:`factlog.integrations.zotero.item_parser` folds the **stored value** at
  the import boundary (#420), on both of its independent DOI paths, and does not
  lowercase: a stored value keeps the case the library spelled.

Before #420 the grammar sat privately in ``source_writer``; a second copy at the
import boundary would have been exactly the duplication #410 removed elsewhere,
and the two copies could drift apart on the one detail that already cost a bug
(subdivided registrant codes — see :data:`DOI_PREFIX_RE`).

Deliberately NOT in :mod:`factlog.text_norm`: that module is the ``Nd`` category
and nothing else, and takes no position on what a caller does with the answer. A
DOI prefix is identifier syntax, not a Unicode fact.
"""
from __future__ import annotations

import re

from factlog.text_norm import fold_decimal_digits

# A DOI prefix, ASCII-spelled: ``10.`` then the registrant code. The trailing
# ``(?:\.[0-9]+)*`` is not decoration — the DOI Handbook (2.2.2) lets a registrant
# subdivide its code (``10.1000.10``), and each part is still a decimal number, so
# a grammar narrower than the spec would leave exactly the DOIs this fix is about
# splitting into two files.
DOI_PREFIX_RE = re.compile(r"10\.[0-9]+(?:\.[0-9]+)*")


def fold_doi_prefix(value: str) -> str:
    """*value* with the digits of its DOI prefix respelled in ASCII.

    The split is the **first** ``/`` — a DOI suffix may itself contain slashes
    (``10.1002/x/y``), and all of them belong to the opaque half, which passes
    through untouched even when it holds non-ASCII digits.

    Folded only when the folded head is exactly a DOI prefix; anything else (a
    ``doi.org`` URL, a ``doi:`` label left in a hand-edited file, plain junk) is
    returned **unchanged**, so this cannot quietly rewrite a value it does not
    understand into one that looks canonical.

    A value with **no** slash is likewise returned unchanged, deliberately: with
    no suffix to delimit it, there is nothing to distinguish a bare prefix from
    junk that happens to start with digits.

    Case is preserved. Callers that want a case-insensitive comparison form
    lowercase around this call (:func:`normalize_cross_id`); the import boundary
    does not, because it is storing a value rather than deriving a key.
    """
    head, slash, suffix = value.partition("/")
    if not slash:
        return value
    folded = fold_decimal_digits(head)
    if not DOI_PREFIX_RE.fullmatch(folded):
        return value
    return f"{folded}{slash}{suffix}"
