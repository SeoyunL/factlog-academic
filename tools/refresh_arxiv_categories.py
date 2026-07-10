#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Regenerate the arXiv category whitelist from the published taxonomy.

``factlog.integrations.arxiv.config.CATEGORIES`` is a whitelist because arXiv
answers a bogus ``cat:`` value with HTTP 200 and zero results (#57) — the
operator reads "no such literature exists". Unlike the pre-2007 archive set,
which was frozen when the old id scheme was retired, the category taxonomy still
grows: ``econ.*`` and ``eess.*`` post-date the original list. So it needs
occasional refreshing, and this script is the source of that refresh.

Prints the categories it finds, and diffs them against what ``config.py``
currently declares. Read-only and network-dependent; run it by hand, paste the
result into ``config.py``. Exit code is 1 when the two disagree, so CI can
notice drift without this ever writing to the source tree.

    python3 tools/refresh_arxiv_categories.py
"""
from __future__ import annotations

import re
import sys
import urllib.request

TAXONOMY_URL = "https://arxiv.org/category_taxonomy"
USER_AGENT = (
    "factlog-academic (category taxonomy refresh; "
    "https://github.com/SeoyunL/factlog-academic)"
)

# Categories appear as `<h4>cs.CL <span>...`. Nine archives (`hep-th`, ...) are
# themselves categories and carry no subject class, so both shapes are matched.
_SUBCATEGORY_RE = re.compile(r"<h4>([a-zA-Z-]+\.[a-zA-Z-]+)\s")
_BARE_ARCHIVE_RE = re.compile(r"<h4>([a-z-]+)\s*<span")


def fetch_categories() -> set[str]:
    request = urllib.request.Request(TAXONOMY_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        html = response.read().decode("utf-8", "replace")
    found = set(_SUBCATEGORY_RE.findall(html)) | set(_BARE_ARCHIVE_RE.findall(html))
    if not found:
        raise SystemExit(
            f"no categories parsed from {TAXONOMY_URL}; the page layout probably changed."
        )
    return found


def main() -> int:
    from factlog.integrations.arxiv.config import CATEGORIES

    published = fetch_categories()
    added = sorted(published - CATEGORIES)
    removed = sorted(CATEGORIES - published)

    print(f"{TAXONOMY_URL}: {len(published)} categories")
    print(f"config.py:      {len(CATEGORIES)} categories")

    if not added and not removed:
        print("\nIn sync.")
        return 0

    if added:
        print(f"\nNew on arXiv, missing from config.py ({len(added)}):")
        for category in added:
            print(f"  + {category}")
    if removed:
        # Retired categories stay valid as historical `cat:` filters, so removing
        # them from the whitelist would false-reject a legitimate query. Report,
        # do not prescribe.
        print(f"\nIn config.py, absent from the taxonomy ({len(removed)}):")
        for category in removed:
            print(f"  - {category}")
        print("\n  Note: a category that disappears from the taxonomy page may still")
        print("  match historical papers. Prefer keeping it over dropping it.")

    print("\nPaste the full sorted set into config.py's CATEGORIES:")
    for category in sorted(published | CATEGORIES):
        print(f'    "{category}",')
    return 1


if __name__ == "__main__":
    sys.exit(main())
