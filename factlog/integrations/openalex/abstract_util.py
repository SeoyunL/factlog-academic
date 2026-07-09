#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Restore OpenAlex's inverted-index abstracts to plain text (spec §5.4).

OpenAlex does not ship abstracts as prose. It ships ``abstract_inverted_index``,
a ``{word: [positions]}`` mapping, and the reader reassembles the text. Two
properties of real payloads shape this module (measured in #51, across 100
works from a live query):

* **Abstracts are often absent** — 37 of 100 works had no index at all. A
  bibliographic-only import is the normal case, not an edge case, so a missing
  index yields ``""`` rather than an error.
* **Position sets can be sparse** — 2 of 100 had small gaps (e.g. ``W2913668833``
  is missing positions 479, 482, 491). Reassembly walks the positions that exist
  instead of ``range(max)``, so a gap silently drops a token rather than raising.
  The restored abstract is therefore faithful but not guaranteed complete.

No duplicate positions were observed. Should one appear, the first word wins so
that the same payload always restores to the same text (P3).
"""
from __future__ import annotations


def restore_abstract(inverted_index: object) -> str:
    """Reassemble ``abstract_inverted_index`` into plain text.

    Returns ``""`` for a missing, empty, or malformed index. Entries whose word
    is not a string, or whose positions are not non-negative integers, are
    skipped rather than poisoning the whole abstract.
    """
    if not isinstance(inverted_index, dict) or not inverted_index:
        return ""

    positions: dict[int, str] = {}
    for word, position_list in inverted_index.items():
        if not isinstance(word, str) or not isinstance(position_list, (list, tuple)):
            continue
        for position in position_list:
            # bool is an int subclass; a stray `true` must not land at index 1.
            if not isinstance(position, int) or isinstance(position, bool) or position < 0:
                continue
            positions.setdefault(position, word)

    return " ".join(positions[index] for index in sorted(positions))


def has_abstract(work: object) -> bool:
    """True when the work carries a non-empty inverted index."""
    if not isinstance(work, dict):
        return False
    index = work.get("abstract_inverted_index")
    return isinstance(index, dict) and bool(index)
