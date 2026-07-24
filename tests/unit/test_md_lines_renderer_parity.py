# SPDX-License-Identifier: Apache-2.0
"""factlog.md_lines against a real CommonMark renderer (#500).

`factlog/md_lines.py` argues for its own rules by saying they give **the answer a
renderer gives**, because the only answer that matters is what a human sees when the
file is rendered. That sentence is a claim about a program nobody in this repo
wrote, so it is measured here rather than asserted: every two- and three-line
document over the alphabet below is rendered by markdown-it-py and its headings are
compared with :func:`factlog.md_lines.headings`.

**The claim is scoped, and the oracle here is where the scope is written down.**
md_lines reads the top level of a document and nothing else — a heading inside a
block quote or indented into a list item is a heading to a renderer and not to
md_lines, on purpose (see the module docstring: a section that exists only inside a
quotation is not a place a bullet can be filed). So `_top_level_headings` walks the
token stream with a container depth counter and takes only the headings at depth 0.
That counter *is* the scope. Deleting it and the comparison fails, which is the
point: if someone widens md_lines to read inside containers, or narrows it further,
this test is where the two definitions are forced to meet.

Why this is opt-in. `markdown-it-py` is in the `test` extra, so CI (which installs
`.[test]`) runs it on every push and the claim has to stay earned. A developer
without it gets a skip rather than a failure — the same shape the engine-dependent
tests in this suite already use for `pyrewire`. Pinned by floor and not by exact
version deliberately: CommonMark is a frozen spec and a renderer changing its mind
about one of these documents is something factlog wants to be told about, not
something to be insulated from. Measured on markdown-it-py 3.0.0 and 4.2.0, which
agree.
"""
from __future__ import annotations

import itertools
import random

import pytest

from factlog.md_lines import _container_content, headings

markdown_it = pytest.importorskip(
    "markdown_it", reason="renderer parity needs markdown-it-py (the `test` extra)"
)


# Fragments chosen because each one is a decision md_lines has to make: a Setext
# underline, something that only looks like one, the containers that make an
# underline mean something else, both fence markers, and the indentation steps on
# either side of the four-space threshold.
ATOMS = (
    "출처", "abc", "- a", "> q", "# T", "## S", "", "----", "====", "***",
    "```", "~~~", "    x", "-", "-=-", "   ----", "    ----", "1. n", "---",
    "+ b", "*  c", "  ind",
)


@pytest.fixture(scope="module")
def render():
    return markdown_it.MarkdownIt("commonmark")


def _top_level_headings(md, text: str) -> list[tuple[int, str]]:
    """``(level, inline text)`` of the headings *outside* any container.

    The scope of the parity claim, spelled out. A renderer reports headings at every
    depth; md_lines reports the ones a bullet could be filed under, which are the
    ones at the top level.
    """
    tokens = md.parse(text)
    found: list[tuple[int, str]] = []
    depth = 0
    for index, token in enumerate(tokens):
        if token.type in ("blockquote_open", "bullet_list_open", "ordered_list_open"):
            depth += 1
        elif token.type in (
            "blockquote_close", "bullet_list_close", "ordered_list_close",
        ):
            depth -= 1
        elif token.type == "heading_open" and depth == 0:
            found.append((int(token.tag[1:]), tokens[index + 1].content))
    return found


def _mine(text: str) -> list[tuple[int, str]]:
    """md_lines' headings in the same shape — level and title.

    `Heading.title` and not a rule written out here. This side of the comparison used
    to strip the marker and the underline itself, which is a second answer to "what
    does this heading say" living in the oracle: a test that normalises its own side
    can be made to agree with anything, and the one place that must not happen is the
    thing standing in for the renderer.
    """
    return [(heading.level, heading.title) for heading in headings(text)]


def _documents(length: int):
    return ("\n".join(combo) + "\n" for combo in itertools.product(ATOMS, repeat=length))


@pytest.mark.parametrize("length", [2, 3])
def test_every_short_document_gets_the_renderers_answer(render, length):
    """Exhaustive over the alphabet: 484 two-line and 10,648 three-line documents.

    Exhaustive rather than a handful of examples because the interesting cases here
    are the ones nobody thinks to write down. Three of md_lines' rules exist only
    because this comparison found them: a paragraph may begin on the line after an
    ATX heading, an indented code block above a paragraph is a boundary and not the
    paragraph's first line, and an empty list marker takes no lazy continuation.
    """
    disagreed = [
        (text, expected, actual)
        for text in _documents(length)
        for expected, actual in [(_top_level_headings(render, text), _mine(text))]
        if expected != actual
    ]
    assert disagreed == []


def test_a_quoted_heading_is_the_one_documented_disagreement(render):
    """The scope, from the other side: inside a container the two do differ.

    Asserted rather than left implicit so that the exhaustive test above cannot be
    read as "md_lines is a renderer". It is a renderer for the top level of a
    document, and this is what that costs.
    """
    quoted = "> 출처\n> ----\n"
    assert render.render(quoted).count("<h2>") == 1  # the renderer sees a heading
    assert _top_level_headings(render, quoted) == []  # not at the top level
    assert _mine(quoted) == []  # and md_lines agrees with the top-level reading


# Four-to-six-line documents are sampled rather than enumerated (22**6 is 113
# million) and the budget is a bound rather than an expectation of zero: the module
# docstring records ~1 disagreement per 1000, all of them a block nested inside a
# list item, and explains why widening the rules to catch them costs more real
# headings than it saves. The bound is here to catch a *change* in that rate.
_SAMPLE = 4000
_MAX_DISAGREEMENT_RATE = 0.01


def test_longer_documents_stay_within_the_recorded_disagreement_rate(render):
    rng = random.Random(500)  # fixed: a flaky parity test would teach people to ignore it
    disagreed = []
    for _ in range(_SAMPLE):
        text = "\n".join(rng.choice(ATOMS) for _ in range(rng.choice((4, 5, 6)))) + "\n"
        if _top_level_headings(render, text) != _mine(text):
            disagreed.append(text)
    rate = len(disagreed) / _SAMPLE
    assert rate <= _MAX_DISAGREEMENT_RATE, (rate, disagreed[:5])
    # and every one of them is the nesting the docstring names. Asked of md_lines'
    # own predicate rather than a second copy of the rule written out here: two
    # notions of what opens a list, disagreeing, is the disease this module pair
    # exists because of.
    for text in disagreed:
        assert any(
            _container_content(line) is not None for line in text.splitlines()
        ), text


@pytest.mark.xfail(
    strict=True,
    reason="known gap: the `<` guard reads only a paragraph's first line (#500 follow-up)",
)
def test_raw_html_above_an_underline_is_not_a_heading(render):
    """Why ATOMS carries no raw HTML, said out loud instead of by omission.

    An HTML block runs to the next blank line, so a renderer swallows the underline
    into it and reports no heading; this module reads the paragraph as ordinary text
    and calls it one. The guard that would catch it looks at the first line of the
    paragraph only, and widening it to every line would refuse a real heading whose
    body merely contains an inline tag.

    ``strict`` on purpose: leaving the document out of the alphabet would hide the
    gap, and asserting today's wrong answer would freeze it. This fails the day
    someone fixes it, which is the only signal worth having.
    """
    document = "출처\n<div>\n----\n"
    assert _top_level_headings(render, document) == _mine(document)
