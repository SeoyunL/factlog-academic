# SPDX-License-Identifier: Apache-2.0
"""Regression checks for the review-reference title and its inbound links (#283)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def github_heading_slug(title: str) -> str:
    """Model GitHub's anchor rule: removed punctuation leaves its spaces intact."""
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", title.strip().lower())).strip("-")


@pytest.mark.parametrize(
    ("review_path", "title", "legacy_anchor", "inbound_paths"),
    [
        (
            "docs/reference/review.md",
            "사실 검토 (`factlog review` / `accept` / `reject`)",
            "사실-검토-factlog-review--accept--reject",
            ("docs/guide/concepts.md", "docs/guide/use-cases.md"),
        ),
        (
            "docs/reference/review.en.md",
            "Reviewing facts (`factlog review` / `accept` / `reject`)",
            "reviewing-facts-factlog-review--accept--reject",
            ("docs/guide/concepts.en.md", "docs/guide/use-cases.en.md"),
        ),
    ],
)
def test_review_title_has_no_duplicate_heading_and_preserves_old_anchor(
    review_path: str, title: str, legacy_anchor: str, inbound_paths: tuple[str, str]
):
    review = ROOT / review_path
    text = review.read_text(encoding="utf-8")

    assert text.startswith(f"# {title}\n")
    assert text.count(title) == 1
    assert github_heading_slug(title) == legacy_anchor

    relative_review = "../reference/" + review.name
    for inbound_path in inbound_paths:
        inbound = (ROOT / inbound_path).read_text(encoding="utf-8")
        assert f"]({relative_review})" in inbound
