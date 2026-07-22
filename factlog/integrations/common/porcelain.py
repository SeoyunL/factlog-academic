# SPDX-License-Identifier: Apache-2.0
"""The one neutralization rule every ``--porcelain`` row shares (issue #141).

A porcelain row is a tab-separated positional contract (#78): a fixed number of
fields, read by column offset. Any caller-influenced value in a row — a source or
corrupt-ledger *path* in an id column, an ``OSError`` message that carries a path in a
``reason``, a list of sidecar paths — can hold a tab and add a column, or a line break
and split the row; either way a positional consumer reads the wrong field, silently.

The arXiv and OpenAlex integrations both emit such rows, so the rule lives here rather
than hand-mirrored in each (#111 added it to OpenAlex's ``reason`` alone, and arXiv had
no such helper at all until #141 wrote one). One definition keeps "both integrations'
porcelain emits the documented field count" from quietly splitting in two.

Porcelain named the rule; it is not the only place that needs it. A stderr warning whose
*block shape* carries meaning breaks the same way — see :func:`porcelain_field` on the
second contract (#396) — and such a caller reuses this rule rather than growing a near-copy
under a second name, which is how the two integrations drifted apart in the first place.

**Not every porcelain emitter is gated.** That was true when #396 wrote it, it stayed true
after #406 closed three ``result`` rows, and it is the right thing to assume now. #416
closed fourteen more. The last AST sweep found **105 tab-carrying f-strings under
``factlog/``, with no ungated caller-influenced field among them** — read that as a
measurement with a date on it, never as "the set is closed".

An earlier revision of this paragraph did write it as a closed set, in those words, and
was wrong when it said so: three emitters were open at the time, and the review that
caught them reproduced all three end to end. *Why* it was wrong outlasts the claim. It
rested on grepping ``print(f"``, and **that search cannot see a row built any other way**
— ``rows.append(f"…")``, a list comprehension, a row assembled and handed to someone else
to print. All three misses were ``rows.append``; the ``candidate`` row #406 missed was a
comprehension. A shape-based search finds the shape it already knows, and reports silence
as absence.

So the instruction is: **walk the AST for f-strings whose literal parts contain a tab, and
check each interpolated field for a gate call.** ``print(f"`` is not the search, it is one
spelling of one shape. Do not trust a count in this note — the 105 included — without
re-running that sweep.

What #416 closed, with the evidence for each, because the strengths are not equal:

* Both ``query`` rows (``arxiv-search``, ``pubmed-search``) — the user's own ``--query``,
  the most caller-influenced value on any row here. **Measured:** ``--query $'a\\tb'
  --show-query --porcelain`` emitted ``query\\ta\\tb``, three columns against a contract
  of two.
* **Eight ``target`` rows**, all **measured**, by putting the KB in a directory whose name
  carries the character: five in ``cli.py``, and three more built by ``rows.append`` in
  ``pubmed/refresh.py``, ``openalex/refresh.py`` and ``arxiv/check_versions.py``. A path
  from the user's ``--target``, and a POSIX directory name may hold a tab outright. The
  last three are the ones the ``print(f"`` search could not see; note that
  ``pubmed-refresh`` emits **two** of these rows, and #416's first pass gated one and
  missed the other *in the same command*.
* Three of the four dry-run ``item``/``work`` rows — the fourth, ``_pubmed_finish``'s, was
  already gated by #141, which is why this shape existed four times with one checked.
  **Measured for two:** a tab-carrying Zotero key (no validator stands between the Zotero
  API and this row), and — through that pubmed sibling — a tab-carrying PMID from a real
  efetch body, which arrives and is neutralized to a space, so that gate is doing visible
  work rather than guarding an unreachable path.

  **Gated but with no route found: the arXiv and OpenAlex ``work`` rows.** For arXiv,
  ``versioned_id`` is only ever built from an ``arxiv_id`` that came through
  ``normalize_arxiv_id``/``parse_entry_id``, and an exhaustive run — every character this
  module neutralizes, at every insertion point — carries none of them through. For
  OpenAlex, ``normalize_work_id`` is the same story, and a hostile title is slugified
  before it can reach the filename column. Both are gated anyway: ``outcome.key`` *is*
  ``work.openalex_id``/``work.versioned_id``, the values ``_openalex_show_results`` and
  ``_arxiv_show_results`` gate one row over, and a caller gates its value rather than
  reasoning about what its own parser admits. An earlier revision of this note called the
  arXiv one "measured" on the strength of a test that handed the row a fabricated id
  through a fake client — which fixes the emitter and shows nothing about reachability.
* The ``candidate`` row (``_candidate_porcelain_lines``, #75) — **not in the list #406
  wrote**, and found only by re-running the sweep rather than trusting that list. Its two
  columns differ and the note keeps them apart: ``existing_path.name`` is **measured** (the
  path comes from a scan of ``sources/``, so renaming a real source file to hold a tab puts
  one there — five columns without the gate, four with it), while ``key`` has **no route
  found**, for the ``normalize_work_id`` reason above.

Line numbers are deliberately absent. Every earlier revision carried them, every
revision's numbers went stale within a merge or two, and the last set was stale on
arrival — so the sweep, not the list, is the durable part. It has now earned itself three
times: the ``target`` group turned up after this paragraph claimed five emitters, the
``candidate`` row after it claimed ten, and three ``rows.append`` rows after it claimed
the set was closed. Each time the list was wrong and re-running the search was right.

Two habits this note asks of the next person, both learned the hard way:

**Say which strength of evidence you have.** "Measured" and "gated, no route found" are
different claims, and #416 got this wrong before review caught it — twice labelling as
measured a row whose value had been injected through a fake client or a test helper that
bypassed the real normalizer. Fixing an emitter and reaching an emitter are different
experiments; a test that hands a row a fabricated value proves the first and is silent on
the second. **Gating a value you cannot show is reachable is right** — that is what these
gates are for. Calling it measured is what is wrong.

**Do not upgrade a hedge without an experiment that earns it.** Before #416 this note
read "at least ten" and "the count above is a floor". Those were correct. #416 replaced
them with an absolute claim that was false on the day it was written. A hedge is not
clutter to be tidied away; it is the part of the sentence carrying what is not known.

Note what kind of gap those were, because this module has seen both kinds. The ones above
were **ungated** — no neutralization at all. The three ``*-backfill-provenance`` commands
were something worse to review: **stale, not ungated**. Each kept its own local copy of
the tab/CR/LF rule, which *looked* checked while silently falling behind when #396 widened
the shared set, so eight characters it now neutralizes still split those rows in two. They
were converted to call this function (#396). A near-copy does not stay correct by being
correct once, which is the whole argument for one definition; an obvious gap at least
announces itself — provided a note like this one keeps announcing it.

Qualify that last clause with what #416 found, though: a gap announces itself *to a search
that can see it*. The three ``rows.append`` rows were as ungated as any bullet above and
stayed invisible through two issues, because both searched for a shape they did not have.
"Obvious" is a property of the search, not of the gap.
"""
from __future__ import annotations


# Tab (adds a column) plus every character `str.splitlines()` treats as a line break.
# That list, not "the C0 range", is the right generator: what breaks both contracts is
# line-splitting, and three of these — U+0085 NEL, U+2028, U+2029 — are NOT C0 and are
# perfectly legal XML 1.0 (measured: `&#133;`/`&#8232;`/`&#8233;` parse fine where
# `&#27;` is a parse error). Gating on "control character" would have missed exactly
# those three, which is the hole #396's first cut shipped with.
_LINE_BREAKS = ("\n", "\v", "\f", "\r", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
_NEUTRALIZE = {ord(char): " " for char in ("\t", *_LINE_BREAKS)}


def porcelain_field(text: str) -> str:
    """Replace every tab and line break in ``text`` with a single space each.

    Each such character maps to one space, so ``"\\r\\n"`` becomes two spaces. That is
    deliberate: the guarantee is that no tab and no line break survives — never that the
    field's length is preserved — so a row keeps its field count and stays a single line.

    Two contracts rest on that one guarantee, and neither is "all human-readable output".
    The first is the positional one above: a porcelain row read by column offset. The
    second is a **human** line whose shape is itself load-bearing — ``pubmed-search``'s
    year-range warning is one line of claim plus one indented continuation, so a line
    break inside a quoted ``MedlineDate`` splits the block and lets record data appear as
    a ``⚠`` line of factlog's own (#396). Prose that merely *contains* a caller value is
    still left untouched; what earns the gate is output where the character changes how
    the reader parses the line, not merely how it looks.

    **What is deliberately NOT neutralized**, and why that is a decision about the two
    contracts rather than about any one caller's input: a control character that adds
    neither a column nor a row is left alone. It can only look odd, and stripping it would
    put this function in the business of deciding what renders nicely — the "all
    human-readable output" scope both contracts above exclude.

    Two such characters are known to reach here, by different routes, and neither is
    universal — this function has several consumers and they do not share a parser:

    * **U+007F DEL** reaches the #396 warning gate through a real PubMed efetch
      (measured); XML 1.0 admits it and ``work_parser._text`` does not collapse it,
      since it is not Python whitespace.
    * **ESC** is *not* reachable through that XML path — XML 1.0 rejects it outright —
      but it very much is elsewhere: JSON admits it (``json.loads('"a\\u001bb"')`` →
      ``'a\\x1bb'``), so an OpenAlex ``reason`` can carry one, and a POSIX filename may
      contain it outright, so a ``ledger`` path can too. A row carrying ``\\x1b[2K``
      (ANSI erase-line) was emitted through this gate and still measured three fields on
      one line. It is left alone for the same reason DEL is: a terminal may erase what is
      already drawn, but the row's field count and line count — all either contract
      claims — are untouched.

    Do not read either bullet as "this gate is narrow enough". Both are notes about what
    happens to reach it today, from parsers this function does not control and callers it
    has not met; a caller gates its value rather than reasoning about what its own parser
    admits (the error #396's first cut shipped, in the opposite direction).
    """
    return text.translate(_NEUTRALIZE)
