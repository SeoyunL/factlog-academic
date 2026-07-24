# SPDX-License-Identifier: Apache-2.0
"""What a markdown document's lines *are*, before anyone asks what they mean.

Which lines a renderer will show as code, where each heading begins and ends, and
which lines are list items. Four readers used to answer those questions with scans
of their own, none of them aware of fences, and each disagreement between the
copies let the file and the report about the file drift apart:

* *where does a section start* — ``insert_bullet``'s ``lines.index``, which matched
  a heading inside a fence and filed the bullet under no section at all;
* *where does it end* — its own ``startswith("## ")`` walk, which stopped at a
  fenced example and inserted into the code block (:func:`section_body_end`);
* *have I filed this bullet, and is anything filed here* — the producer's whole-file
  line comparison and the validator's ``- `` count, which between them made a
  needs_review row vanish from the file a human reads while the KB reported
  completely valid (:func:`bullets`);
* *is this stale-source record already written* — a substring test over the whole
  document, answered just as well by an example in a fence (:func:`bullets`).

A second notion of where a section is, disagreeing with this one, is what #495 was
about to begin with; #515 is the same lesson one level down — those four answers
had been living inside the module that owns the *review-category* contract, where
"is this line code" had to be read past to find "which categories does the file
keep".

**Anything added here has to be an answer a reader who does not know what the
document is for could still ask for** — which of its lines render as code, where
each heading starts and ends, which lines are list items. If a review category, a
file name, or a factlog concept has to be named to state the question, it is not
this module's: :mod:`factlog.review_sections` owns those.

Three things this deliberately does not claim:

* **Only top-level blocks are read.** A renderer will happily report a heading
  inside a block quote — ``> 출처`` over ``> ----`` renders as an ``<h2>`` — and
  :func:`headings` will not. That is on purpose and it is not a narrowness to be
  fixed later: quoted text is something the document *cites*, not something it is
  organised into, and a section heading that only exists inside a quotation is not a
  section anyone can file a bullet under. The same goes for a heading indented into
  a list item. **A block quote is invisible to every reader here** — measured: no
  headings, no bullets, and a fence opened after ``> `` neither opens nor is ever
  reported unclosed. (Indented *bullets* are the one nesting that does count, and
  deliberately: ``  - nested`` under a filed bullet is part of the same queue entry.
  See :func:`bullets`.)
* **Front matter is not read here.** A YAML block's closing ``---`` has exactly the
  shape of a Setext underline, and a naive scan calls every one of them a level-2
  heading — measured, 35 of them across one real KB's ``sources/*.md``. Nothing
  today feeds those files to this module, but a general module invites it, so:
  delimiting front matter is :mod:`factlog.front_matter_scan`'s, and a caller that
  wants both has to strip the block before asking here.
* **This is not a rendering library.** It answers the structural questions a
  producer and a validator have to agree on, and the fence and Setext rules exist
  for that reason rather than for their own sake.

Where the rules *are* claimed to match a renderer, the claim is exactly this and no
wider: **for the headings at the top level of a document built from the alphabet in
``tests/unit/test_md_lines_renderer_parity.py``, this module gives the answer
markdown-it-py gives.** Both qualifications are load-bearing and neither is
rhetorical.

*Top level*, because of the scoping above: nested headings are left out of the
comparison deliberately, not by accident.

*That alphabet*, because a claim of the form "agrees on N documents" is unreproducible
without the tokens they were built from — so the tokens live in the test, the test is
the comparison, and the numbers below are whatever it prints today rather than
something remembered. Over every two- and three-line document from it, 11,132 of them,
the two agree on all. **The alphabet contains no raw HTML**, and that is a gap and not
a choice: ``출처`` / ``<div>`` / ``----`` is read here as a heading and by a renderer
as an unterminated HTML block, because the ``<`` guard in
:func:`_first_paragraph_line` looks only at a paragraph's first line. It is pinned as
a known-wrong answer in the parity test rather than left to be rediscovered.

What is knowingly not modelled is a block **nested inside a list item**: over random
four-to-six-line documents from the same alphabet the two disagree on roughly one in
a thousand, and every one of those has an indented block under a list marker — which
the parity test asserts, using this module's own container predicate rather than a
second copy of the rule. Widening the rules to catch them costs far more real
headings than it saves (measured: false positives 130 → 47, false negatives 207 →
5,901 over 211,132 documents), and a missed heading is the expensive direction — the
file is then told it lacks a section it has, and a second one gets appended beside it.
"""
from __future__ import annotations

from dataclasses import dataclass

# Four leading spaces is an indented code block in CommonMark, so at four spaces a
# line stops being any kind of marker and becomes ordinary content. **One constant,
# not one per rule**: fence markers, Setext underlines and the first line of a
# paragraph all sit under the same threshold, and giving each its own copy would
# mean a mutant that raises only one of them still passes — the other rule's tests
# would cover for it. Counting indented markers as fences made a well-formed
# document read as permanently unclosed.
_MAX_BLOCK_INDENT = 3


def _indent(line: str) -> int:
    """Leading spaces of *line*."""
    return len(line) - len(line.lstrip(" "))


def _fence_marker(line: str) -> tuple[str, int, str] | None:
    """*line* as a fence marker — ``(char, run length, rest)`` — or None.

    A run of three or more backticks or tildes, indented no more than
    :data:`_MAX_BLOCK_INDENT`. ``rest`` is what follows the run, which decides
    whether the marker is allowed to close: an info string makes it an opener only.
    """
    body = line.lstrip(" ")
    if _indent(line) > _MAX_BLOCK_INDENT:
        return None
    for char in ("`", "~"):
        if body.startswith(char * 3):
            run = len(body) - len(body.lstrip(char))
            return char, run, body[run:]
    return None


def fence_flags(text: str) -> tuple[list[bool], int | None]:
    """Per-line "is this inside a code fence", and where an unclosed fence opened.

    One scan, one answer, used by everything below. It used to be written out twice
    — once to find headings and once to ask whether the file ended mid-fence — and
    two copies of the rule for what a section is are what #495 was about in the
    first place.

    A fence closes only on **CommonMark's terms**: the same character, a run at
    least as long as the opener's, and nothing after it but whitespace. Treating
    every marker as a plain toggle broke the one document this module's own
    reference recommends — a ``~~~`` block quoting a ```` ``` ```` line, which is
    how anyone writes a bullet-format example without nesting backticks. The
    backtick line "closed" the tilde fence and the real ``~~~`` re-opened it, so a
    correct file read as permanently unclosed and both writers refused it forever,
    pointing at the line that closes it and asking for it to be closed.

    Matching is not uniformly more or less strict than the toggle was: a
    non-matching marker no longer ends a fence (more of the file is code), and the
    marker that does match now ends it (less). What it is instead is **the same
    answer a renderer gives**, which is the only answer that matters — the whole
    point of the flag is whether a human will see the line or a code block.

    "The same answer a renderer gives" is scoped, and the module docstring says how:
    **at the top level of the document.** A marker written after ``> `` is inside a
    quotation, and this neither opens a fence for it nor ever reports it unclosed.
    """
    flags: list[bool] = []
    opened_at: int | None = None
    open_marker: tuple[str, int] | None = None
    for index, line in enumerate(text.splitlines()):
        marker = _fence_marker(line)
        if marker is None:
            flags.append(opened_at is not None)
            continue
        char, run, rest = marker
        if open_marker is None:
            opened_at, open_marker = index, (char, run)
        elif char == open_marker[0] and run >= open_marker[1] and not rest.strip():
            opened_at, open_marker = None, None
        flags.append(True)
    return flags, opened_at


@dataclass(frozen=True)
class Heading:
    """A heading of the document, as a *place* rather than as a string.

    ``start`` is the index of its first line and ``end`` the index just past its
    last, so ``lines[start:end]`` is the heading and nothing else. ``text`` is those
    lines raw, joined by newlines, trailing whitespace and all: callers put it in
    warnings a human reads, and re-spelling it there would make the message disagree
    with the file.

    Why a place. Every caller that had a heading string went looking for it again
    with line equality — ``lines.index(heading)`` — and line equality answers "which
    line says this" rather than "where is the heading". The two differ whenever the
    body of a document repeats its own title line, and the caller then filed content
    against a line that is not a heading at all. Answering with the position once
    removes the second lookup, and with it the chance of a different answer.
    """

    start: int
    end: int
    level: int
    text: str

    @property
    def title(self) -> str:
        """What the heading *says* — its text without the syntax that marks it.

        ``## 출처 부족`` and ``출처 부족`` over ``----`` have the same title, which is
        the point: the two spellings are the same heading to a reader, so anything
        showing a heading to a human should show the same thing for both.

        Here because both consumers of :attr:`text` were computing it, differently.
        A validator warning printed the raw span, so an operator reading a one-line
        message got a literal newline in the middle of it — ``has 2 '출처' sections
        ('출처\n----', …)``. And the renderer-parity oracle had its own copy, which
        is the worst place for one: a test that normalises its own side of a
        comparison can be made to agree with anything.

        A heading is ATX when it occupies a single line and Setext otherwise, which
        is exact rather than a guess about the first character — a paragraph may
        begin with ``#`` without being ATX (``#foo`` has no space after the marker),
        and it would be underlined into a Setext heading whose text starts with a
        ``#``. An ATX closing sequence (``## foo ##``) is dropped, the way a renderer
        drops it.
        """
        lines = self.text.split("\n")
        if len(lines) > 1:
            return "\n".join(lines[:-1]).strip()
        content = lines[0].lstrip("#").strip()
        closed = content.rstrip("#")
        if closed != content and (not closed or closed.endswith(" ")):
            return closed.strip()
        return content


# A heading level of 7 does not exist; ``####### x`` is a paragraph.
_MAX_HEADING_LEVEL = 6


def _atx_level(line: str) -> int | None:
    """*line* as an unindented ATX heading — its level — or None.

    Unindented on purpose, which is narrower than CommonMark (it allows up to three
    leading spaces). This is the predicate the review-section scan has always used
    and widening it would move section boundaries in existing KBs, which is not what
    #500 is about; it is recorded here as a known narrowness rather than left to be
    rediscovered.
    """
    if not line.startswith("#"):
        return None
    run = len(line) - len(line.lstrip("#"))
    if run > _MAX_HEADING_LEVEL:
        return None
    # `#` and `## ` are headings; `###x` is not — the marker needs a space after it
    # or nothing at all.
    if len(line) > run and line[run] != " ":
        return None
    return run


def _setext_underline(line: str) -> str | None:
    """``"="`` or ``"-"`` if *line* has the shape of a Setext underline, else None.

    Shape only — whether it *is* one depends on what sits above it, which is
    :func:`_paragraph_start`'s question. A run of one or more of a single character,
    indented no more than :data:`_MAX_BLOCK_INDENT`, with nothing but whitespace
    after it. One dash is enough; ``-=-`` is not a run.
    """
    if _indent(line) > _MAX_BLOCK_INDENT:
        return None
    body = line.strip()
    if not body or body[0] not in "=-" or body != body[0] * len(body):
        return None
    return body[0]


def _is_thematic_break(line: str) -> bool:
    """Is *line* a horizontal rule — ``---``, ``***``, ``___``, spaces allowed?

    It matters here only as a *boundary*: a thematic break is a block of its own, so
    a paragraph may begin on the line after it and cannot extend across it.
    """
    if _indent(line) > _MAX_BLOCK_INDENT:
        return False
    body = "".join(line.split())
    return len(body) >= 3 and body[0] in "-_*" and body == body[0] * len(body)


def _container_content(line: str) -> str | None:
    """What *line* holds after its list-item or block-quote marker, or None.

    None means it opens neither. The distinction between an empty container and one
    with content matters: a paragraph on the next line lazily continues a list item
    that has content, and does **not** continue an empty marker — CommonMark gives a
    ``-`` on its own no lazy continuation at all.

    Indentation is not looked at, which errs towards *not* calling something a
    heading: the direction that costs a heading rather than invents one.
    """
    body = line.lstrip(" ")
    if body.startswith(">"):
        return body[1:].strip()
    for marker in ("-", "+", "*"):
        if body.startswith(f"{marker} "):
            return body[len(marker):].strip()
        if body.rstrip() == marker:
            return ""
    digits = len(body) - len(body.lstrip("0123456789"))
    if 1 <= digits <= 9 and len(body) > digits and body[digits] in ".)":
        rest = body[digits + 1:]
        if rest.startswith(" ") or not rest.strip():
            return rest.strip()
    return None


def _paragraph_start(
    lines: list[str], flags: list[bool], underline: int, floor: int
) -> int | None:
    """Where the top-level paragraph ending just above *underline* begins, or None.

    The half of Setext that a two-line pattern match gets wrong. ``----`` under a
    line makes that line a heading **only when the line is a top-level paragraph**,
    and whether it is depends on what is above it, sometimes several lines up. The
    walk goes up from the underline and reads what it finds:

    * a blank line, code, an ATX heading or a thematic break — a clean boundary.
      Those end a block, so a paragraph may start on the line below one. Collecting
      nothing before reaching one means there was no paragraph at all: ``- a`` over
      ``----`` is a list and then a horizontal rule, and so is ``# Title`` over
      ``----``.
    * a list item or a block quote with no blank line between — then what looked like
      a paragraph is a **lazy continuation inside that container**, and the ``----``
      below it is a thematic break outside it. Measured against a renderer,
      ``- a`` / ``출처`` / ``----`` is a one-item list reading "a 출처" followed by a
      rule. Calling that a heading is the worst thing this function could do: the
      file would be told it has a section no reader ever sees.

    *floor* is the end of the last heading already found, so a walk cannot reach back
    into one — in ``abc`` / ``===`` / ``출처`` / ``====`` the second underline sees
    only ``출처``.

    What survives the walk is a run of ordinary lines, and the paragraph begins at
    the **first of them indented three spaces or less**. Lines above that inside the
    same run are an indented code block: four spaces after a boundary opens one, but
    four spaces *after a paragraph has started* is only a continuation line, so
    ``abc`` / ``    출처`` / ``----`` is one heading of two lines while ``    x`` /
    ``출처`` / ``----`` is a code block and then a heading of one.

    One shape is rejected outright: a leading ``<`` makes the line an HTML block,
    which runs to the next blank line and takes the underline in with it.
    """
    run: list[int] = []
    index = underline - 1
    while index >= floor:
        line = lines[index]
        if flags[index] or not line.strip():
            break
        if _atx_level(line) is not None or _is_thematic_break(line):
            break
        content = _container_content(line)
        if content is not None:
            # A marker with content swallows the lines below it as its own; an empty
            # one takes no lazy continuation, so it is a boundary — but only for a
            # line starting at column 0, because anything indented under an empty
            # marker becomes that item's content instead.
            if content:
                return None
            start = _first_paragraph_line(lines, run)
            return start if start is not None and _indent(lines[start]) == 0 else None
        run.append(index)
        index -= 1
    return _first_paragraph_line(lines, run)


def _first_paragraph_line(lines: list[str], run: list[int]) -> int | None:
    """The index in *run* (bottom-up) where its paragraph starts, or None."""
    for start in reversed(run):
        if _indent(lines[start]) <= _MAX_BLOCK_INDENT:
            return None if lines[start].lstrip(" ").startswith("<") else start
    return None


def headings(text: str) -> list[Heading]:
    """Every heading of *text*, in document order, at **every** level.

    All levels, because "which levels count as a section" is a caller's contract and
    not a property of the document — :mod:`factlog.review_sections` keeps ``## ``
    and says so with a ``level == 2`` test, where it can be read and mutated. It used
    to be a ``startswith("## ")`` buried in the scan, where the contract was
    invisible from the module that owns it.

    Outside any code fence. A ``## 출처 부족`` written as an example inside a fence
    was read as the section itself: the file reported no missing sections while
    having no real 출처 section, and the bullet routed there was inserted after the
    closing fence, in no section at all.

    **At the top level, and only there.** A renderer reports ``> ## 출처 부족`` as a
    heading inside a block quote; this does not report it at all, because a heading
    that exists only inside a quotation is not a place a bullet can be filed. The
    same holds for one indented into a list item. This is the one place the module
    knowingly answers differently from a renderer, so it is named here, in the
    module docstring, and in a test.

    Both spellings, ATX and Setext. A Setext heading is two lines or more — the
    paragraph and its underline — and it is the reason :class:`Heading` carries a
    range instead of a line number: ``start`` is the paragraph's first line, which is
    what a reader sees as the title, and ``start + 1`` is not the end of anything.
    ``=`` underlines to level 1 and ``-`` to level 2. What makes an underline an
    underline is :func:`_paragraph_start`.
    """
    lines = text.splitlines()
    flags, _ = fence_flags(text)
    found: list[Heading] = []
    # The end of the last heading found: a Setext scan walks *up* from its underline,
    # and it must not walk into a heading already claimed.
    floor = 0
    for index, line in enumerate(lines):
        if flags[index]:
            continue
        level = _atx_level(line)
        if level is not None:
            found.append(Heading(start=index, end=index + 1, level=level, text=line))
            floor = index + 1
            continue
        underline = _setext_underline(line)
        if underline is None:
            continue
        start = _paragraph_start(lines, flags, index, floor)
        if start is None:
            continue
        found.append(
            Heading(
                start=start,
                end=index + 1,
                level=1 if underline == "=" else 2,
                text="\n".join(lines[start:index + 1]),
            )
        )
        floor = index + 1
    return found


def bullets(text: str) -> list[str]:
    """The bullet lines of *text* that are really bullets — raw, fences excluded.

    Who has already been filed, and whether anything has. Two places asked that and
    each answered it its own way, both counting lines inside code fences:
    ``insert_bullet`` deduplicated against them, so a bullet whose text was quoted as
    a *format example* in a fence was taken for already present and never written;
    and ``validate.py`` counted them, so that same example answered "this KB does have
    review bullets" for a file that had none. Together — measured — a needs_review row
    vanished from the file a human reads while the KB reported entirely valid.

    ``- `` after optional indentation, which is what a reader sees as a list item.
    """
    flags, _ = fence_flags(text)
    return [
        line
        for line, fenced in zip(text.splitlines(), flags)
        if not fenced and line.lstrip().startswith("- ")
    ]


def ends_inside_fence(text: str) -> bool:
    """Did *text* open a code fence and never close it?

    Everything after such a fence is content, including anything appended to the end
    of the file — so a writer that appends there is writing a heading it cannot read
    back, and a bullet no reader will ever see rendered. Ask before writing.
    """
    return fence_flags(text)[1] is not None


def unclosed_fence_line(text: str) -> int | None:
    """The 1-based line number where an unclosed fence opened, or None.

    For telling an operator *where*. "This file has a review section missing" sends
    someone looking for a heading that is right there in front of them; "the fence
    on line 5 is never closed" is the same failure said in a way they can act on.
    """
    opened_at = fence_flags(text)[1]
    return None if opened_at is None else opened_at + 1


def section_body_end(text: str, heading: Heading) -> int:
    """The line index just past the body of *heading* — where a new line goes.

    The body runs to the next heading at *heading*'s level or above, or to the end
    of the file. Level-aware because a section is ended by its peers and its
    parents, not by a subsection of its own: a ``### `` under a ``## `` is part of
    that section, and a ``# `` after it is not.

    Fence-aware for the same reason everything here is. The scan this replaced was
    ``startswith("## ")`` over the raw lines, which stopped at a ``## `` quoted as an
    example inside a fence and inserted the new bullet into the code block.
    """
    lines = text.splitlines()
    for other in headings(text):
        if other.start >= heading.end and other.level <= heading.level:
            return other.start
    return len(lines)
