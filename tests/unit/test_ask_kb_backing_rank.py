# SPDX-License-Identifier: Apache-2.0
"""KB-vocabulary backing credited to a row the lexical scan already cited (#594).

#576 built the bridge but scored it on one side only: a file the scan never cited
became a promoted row, and a file it DID cite got no credit for the accepted facts
that reach it. On a corpus whose sources borrow the English term as their title —
the reference KB's `neurosymbolic` papers — that rule subtracts exactly the right
files, because a topically-matching source is the one the lexical scan finds. The
issue's own evidence file ranked 13th of 28 with a flat (1,1,1) key — the key every
one of the 22 primary rows had, the other 6 being supplementary and already separated
by grade — and the only rows the bridge added were the two off-topic ones at 21 and 22.

So the credit has to reach the cited rows, and these assert the shape of it: what
it counts, what it must not count twice, what it may not overrule, and what it
leaves alone. The shell baseline (tests/test_ask_wiki_search.sh PIN9) pins the
resulting ANSWER; a rendered answer can only ever show one combination came out
right.
"""
from __future__ import annotations

import unicodedata

import ask_router

SOURCE_MD = """\
# {title}

{body}
"""


def build(tmp_path, sources, *, accepted=None, candidates=None, supplementary=None, synonyms=None):
    """A KB root with the given {name: body} sources and optional fact files."""
    root = tmp_path / "kb"
    (root / "facts").mkdir(parents=True)
    (root / "sources").mkdir()
    (root / "policy").mkdir()
    if synonyms is not None:
        (root / "policy" / "vocabulary-synonyms.md").write_text(synonyms, encoding="utf-8")
    for name, body in sources.items():
        (root / "sources" / f"{name}.md").write_text(
            SOURCE_MD.format(title=name, body=body), encoding="utf-8"
        )
    for name, body in (supplementary or {}).items():
        (root / "decisions").mkdir(exist_ok=True)
        (root / "decisions" / f"{name}.md").write_text(
            SOURCE_MD.format(title=name, body=body), encoding="utf-8"
        )
    if accepted is not None:
        (root / "facts" / "accepted.dl").write_text(accepted, encoding="utf-8")
    if candidates is not None:
        (root / "facts" / "candidates.csv").write_text(candidates, encoding="utf-8")
    return root


def csv_rows(*rows):
    header = "subject,relation,object,source,status,confidence,note\n"
    return header + "".join(f"{row}\n" for row in rows)


def refs(results):
    return [f"{r['file']}:{r['line']}" for r in results]


class TestCoverageCredit:
    """The backing joins the coverage component — the same reading a lexical hit
    gets: one more of the question's words that this row answers."""

    def test_backing_lifts_a_cited_row_over_an_equally_covered_one(self, tmp_path):
        # The issue's case, minimized. Both files match the ASCII keyword and nothing
        # else, so before this change the Korean half of the question decided nothing
        # and the order was frequency then corpus order — a-plain first, on both
        # counts. One of them is ALSO where an accepted fact about the question's
        # Korean word came from, and that is invisible to the lexical scan by
        # construction: the word occurs in neither file.
        #
        # a-plain repeats the keyword so it WINS the frequency component. That is what
        # makes this test about coverage: a credit that only reached frequency leaves
        # b-backed at (1, 2) against (1, 3) and the order unchanged.
        root = build(
            tmp_path,
            {
                "a-plain": "A neurosymbolic method. A neurosymbolic pipeline, neurosymbolic throughout.",
                "b-backed": "A neurosymbolic method.",
            },
            accepted='relation("s", "이점", "해석가능성_향상").\n',
            candidates=csv_rows("s,이점,해석가능성_향상,sources/b-backed.md,confirmed,0.9,"),
        )
        question = "neurosymbolic 해석가능성에서 어떤 이점을 갖는가"
        assert refs(ask_router.search(question, root, limit=None)) == [
            "sources/b-backed.md:3",
            "sources/a-plain.md:3",
        ]
        # No promoted row exists to carry that fact: b-backed was cited, so #576's
        # rule skips it. Without the credit the KB's evidence reaches nothing at all
        # for this question — which is the defect, not a side effect of it.
        assert all("via" not in r for r in ask_router.search(question, root, limit=None))

    def test_a_term_reached_both_ways_counts_once(self, tmp_path):
        # A Korean source can BOTH spell the question's word and be the source of an
        # accepted fact carrying it. Counting the term on each channel would rank
        # that row as answering more of the question than it does — here it would
        # beat a file that really does cover three of the question's words.
        #
        # a-backed sorts first, so a double count would also WIN the tie it creates:
        # coverage 3 either way, and its frequency (2 hits + 1 backing fact) equals
        # c-plain's 3 hits.
        root = build(
            tmp_path,
            {
                "a-backed": "설명가능성 benchmark 결과.",
                "c-plain": "설명가능성 benchmark alpha 결과.",
            },
            accepted='relation("s", "이점", "설명가능성_향상").\n',
            candidates=csv_rows("s,이점,설명가능성_향상,sources/a-backed.md,confirmed,0.9,"),
        )
        assert refs(ask_router.search("설명가능성 benchmark alpha", root, limit=None)) == [
            "sources/c-plain.md:3",
            "sources/a-backed.md:3",
        ]

    def test_one_word_in_two_compositions_is_still_one_word(self, tmp_path):
        # The union is by STRING, and only ONE of its two sides is folded:
        # _keyword_hits carries what the question typed, _bridge_terms folds its copy
        # to NFC to join candidates.csv. A term that is not already NFC therefore
        # enters the union twice — one over-credit, no error, and a ranking that
        # merely looks like a different judgement call.
        #
        # A term gets that far because _is_cjk is `any()` over the token: '漢' with
        # decomposed Hangul after it passes on the ideograph alone, Hangul still in
        # NFD. This is the whole reason the fold in _keyword_hits is load-bearing
        # rather than defensive, so it is asserted end-to-end (unfolded, the two files
        # below swap) instead of on the helper.
        mixed = "漢" + unicodedata.normalize("NFD", "글자")
        assert ask_router._is_cjk(mixed)
        assert mixed != unicodedata.normalize("NFC", mixed)
        root = build(
            tmp_path,
            {"a-backed": f"{mixed} benchmark 결과.", "c-plain": f"{mixed} benchmark alpha 결과."},
            accepted=f'relation("s", "이점", "{unicodedata.normalize("NFC", mixed)}_향상").\n',
            candidates=csv_rows(
                f"s,이점,{unicodedata.normalize('NFC', mixed)}_향상,sources/a-backed.md,confirmed,0.9,"
            ),
        )
        # Premise: this term really does bridge (in its folded spelling), otherwise
        # there is no second channel and the test proves nothing.
        assert ask_router._bridge_terms(f"{mixed} benchmark") == [
            unicodedata.normalize("NFC", mixed)
        ]
        assert refs(ask_router.search(f"{mixed} benchmark alpha", root, limit=None)) == [
            "sources/c-plain.md:3",
            "sources/a-backed.md:3",
        ]

    def test_an_all_hangul_nfd_term_does_not_reach_the_bridge_at_all(self, tmp_path):
        # The other half of the same filter, and the narrower case: with no ideograph
        # to carry it, an NFD term is conjoining jamo and _is_cjk rejects it outright,
        # so it never reaches the bridge in either composition. Pinned because it is
        # what makes the mixed case above the ONLY way the two channels diverge — and
        # because reading this case alone is what produced the wrong conclusion that
        # the fold changes nothing.
        nfd = unicodedata.normalize("NFD", "설명가능성")
        assert not ask_router._is_cjk(nfd)
        assert ask_router._bridge_terms(f"{nfd} benchmark") == []
        assert ask_router._bridge_terms("설명가능성 benchmark") == ["설명가능성"]

    def test_a_converted_source_under_runs_is_credited_too(self, tmp_path):
        # runs/sources/ is the other half of the corpus (converted originals), and it
        # is where an extractor's provenance points for anything that was not markdown
        # to begin with. Crediting only sources/ would make the rank of a paper depend
        # on which format it arrived in.
        root = build(
            tmp_path,
            {"a-plain": "A neurosymbolic method. neurosymbolic, neurosymbolic."},
            accepted='relation("s", "이점", "해석가능성_향상").\n',
            candidates=csv_rows("s,이점,해석가능성_향상,runs/sources/b-backed.txt,confirmed,0.9,"),
        )
        (root / "runs" / "sources").mkdir(parents=True)
        (root / "runs" / "sources" / "b-backed.txt").write_text(
            "A neurosymbolic method.\n", encoding="utf-8"
        )
        results = ask_router.search("neurosymbolic 해석가능성에서", root, limit=None)
        assert refs(results) == ["runs/sources/b-backed.txt:1", "sources/a-plain.md:3"]

    def test_backing_facts_join_the_frequency_component(self, tmp_path):
        # Second component, same reading as #576 gave a promoted row: how much
        # evidence says this row answers those words. Two files cover the question
        # equally (one ASCII hit + the same bridged word); the one more accepted
        # facts were extracted from ranks first.
        root = build(
            tmp_path,
            {"a-thin": "A neurosymbolic method.", "b-thick": "A neurosymbolic method."},
            accepted=(
                'relation("s1", "이점", "해석가능성_향상").\n'
                'relation("s2", "핵심_기법", "해석가능성_분석").\n'
            ),
            candidates=csv_rows(
                "s1,이점,해석가능성_향상,sources/a-thin.md,confirmed,0.9,",
                "s1,이점,해석가능성_향상,sources/b-thick.md,confirmed,0.9,",
                "s2,핵심_기법,해석가능성_분석,sources/b-thick.md,confirmed,0.9,",
            ),
        )
        assert refs(ask_router.search("neurosymbolic 해석가능성에서", root, limit=None)) == [
            "sources/b-thick.md:3",
            "sources/a-thin.md:3",
        ]

    def test_a_korean_filename_spelled_two_ways_still_joins(self, tmp_path):
        # The credit is looked up by PATH, and the two sides of that lookup come from
        # different places: the ref from the filesystem, the key from candidates.csv.
        # A Korean filename has two equal spellings (NFC/NFD) that compare unequal
        # character by character, so an unfolded lookup silently credits nothing —
        # no error, no empty result, just the old ranking. Every other reader of
        # candidates' `source` already folds (common.py, merge_candidates, cli), and
        # search() folds the same ref into its `cited` set for the same reason.
        nfd = unicodedata.normalize("NFD", "해석연구")
        root = build(
            tmp_path,
            {"a-plain": "A neurosymbolic method. neurosymbolic, neurosymbolic."},
            accepted='relation("s", "이점", "해석가능성_향상").\n',
            candidates=csv_rows(
                f"s,이점,해석가능성_향상,sources/{unicodedata.normalize('NFC', '해석연구')}.md,confirmed,0.9,"
            ),
        )
        (root / "sources" / f"{nfd}.md").write_text(
            SOURCE_MD.format(title="x", body="A neurosymbolic method."), encoding="utf-8"
        )
        results = ask_router.search("neurosymbolic 해석가능성에서", root, limit=None)
        # Premise: the scan really did read the name back in the other composition.
        # If the filesystem normalized it away, this test proves nothing and says so.
        assert [r["file"] for r in results if nfd in r["file"]], [r["file"] for r in results]
        assert results[0]["file"] == f"sources/{nfd}.md"
        # ...and not by being promoted: it was cited lexically, like the other file.
        assert all("via" not in r for r in results)


class TestOneCreditPerFile:
    """The credit is a fact about the FILE, so it lifts ONE excerpt of it (#602).

    #594 added it to every excerpt, which reads one piece of evidence as many: a
    file's excerpts then rose as a block and one paper took most of the render cap.
    Measured on the reference KB, '오메가-3 보충이 COPD 환자에게 효과있음을 보인
    연구는?' answered with 3 distinct papers over the default 10 rows (5 + 4 + 1
    excerpts) where the uncredited ranking answered with 9.

    Each fixture below puts TWO excerpts in one backed file, far enough apart that
    search()'s window collapse does not merge them, plus a plain file that sits
    between them once only one of the two is credited. Asserting the full order is
    what makes them discriminating: the plain file's position is the whole signal.
    """

    def test_a_files_other_excerpts_keep_their_lexical_key(self, tmp_path):
        # The invariant, stated as an order: the credit lifts f-backed's stronger
        # excerpt above the plain file, and its weaker one stays BELOW that plain
        # file — exactly where the lexical scan alone put it, i.e. where #576 left
        # it. Under #594's per-excerpt credit both excerpts of f-backed came out
        # above z-plain (the weaker one at (2,2), tied with z-plain and ahead of it
        # on corpus order), which is the block movement this issue is about.
        root = build(
            tmp_path,
            {
                "f-backed": "해석가능성 해석가능성 해석가능성 결과.\n" + "\n" * 8 + "해석가능성 결과.",
                "z-plain": "해석가능성 그리고 설명가능성 결과.",
            },
            accepted='relation("s", "이점", "설명가능성_향상").\n',
            candidates=csv_rows("s,이점,설명가능성_향상,sources/f-backed.md,confirmed,0.9,"),
        )
        question = "해석가능성에서 설명가능성을"
        # Premise: the file really is backed, and by a term its text never spells —
        # otherwise there is no credit to place and the order below proves nothing.
        bridged = ask_router.kb_vocabulary_bridge(question, root)
        assert list(bridged) == ["sources/f-backed.md"]
        assert bridged["sources/f-backed.md"]["terms"] == ["설명가능성을"]
        assert "설명가능성" not in (root / "sources" / "f-backed.md").read_text(encoding="utf-8")
        assert refs(ask_router.search(question, root, limit=None)) == [
            "sources/f-backed.md:3",  # credited: (2, 3 + 1)
            "sources/z-plain.md:3",  # lexical only: (2, 2)
            "sources/f-backed.md:12",  # uncredited: (1, 1)
        ]

    def test_the_credit_goes_to_the_excerpt_it_lifts_furthest(self, tmp_path):
        # Which excerpt gets it is decided AFTER the credit, not before. Coverage is
        # a union, so the credit is not a constant: f-backed:3 already spells one of
        # the bridged terms and gains only the other, while f-backed:12 spells
        # neither and gains both. Ranked before the credit, :3 wins (its lexical key
        # (1,3) beats :12's (1,1)) — and that is the wrong excerpt, because the
        # credit takes :12 to coverage 3 and :3 only to 2. z-plain sits between the
        # two outcomes, so a pre-credit pick shows up here as an order change.
        root = build(
            tmp_path,
            {
                "f-backed": "해석가능성 해석가능성 해석가능성 결과.\n" + "\n" * 8 + "alpha 결과.",
                "z-plain": "해석가능성 그리고 설명가능성 결과.",
            },
            accepted=(
                'relation("s1", "이점", "해석가능성_향상").\n'
                'relation("s2", "이점", "설명가능성_향상").\n'
            ),
            candidates=csv_rows(
                "s1,이점,해석가능성_향상,sources/f-backed.md,confirmed,0.9,",
                "s2,이점,설명가능성_향상,sources/f-backed.md,confirmed,0.9,",
            ),
        )
        question = "해석가능성에서 alpha 설명가능성을"
        assert refs(ask_router.search(question, root, limit=None)) == [
            "sources/f-backed.md:12",  # credited: ({alpha} | 2 bridged terms, 1 + 2)
            "sources/z-plain.md:3",  # lexical only: (2, 2)
            "sources/f-backed.md:3",  # uncredited: (1, 3)
        ]

    def test_a_tie_goes_to_the_excerpt_the_scan_emitted_first(self, tmp_path):
        # Two excerpts of one file with identical keys before AND after the credit.
        # Nothing about the rows can break that tie, so the rule has to name an
        # order, and it names the scan's: earliest line of the earliest-sorted file.
        # z-plain ties with the credited row at (2,2) and is emitted after both, so
        # crediting the LATER excerpt instead would show up as z-plain moving up.
        root = build(
            tmp_path,
            {
                "f-backed": "해석가능성 결과.\n" + "\n" * 8 + "해석가능성 결과.",
                "z-plain": "해석가능성 그리고 설명가능성 결과.",
            },
            accepted='relation("s", "이점", "설명가능성_향상").\n',
            candidates=csv_rows("s,이점,설명가능성_향상,sources/f-backed.md,confirmed,0.9,"),
        )
        assert refs(ask_router.search("해석가능성에서 설명가능성을", root, limit=None)) == [
            "sources/f-backed.md:3",  # credited: (2, 2)
            "sources/z-plain.md:3",  # (2, 2), same key, emitted later
            "sources/f-backed.md:12",  # uncredited: (1, 1)
        ]

    def test_a_backed_file_no_longer_takes_the_whole_render_cap(self, tmp_path):
        # The issue's own shape, minimized: one file with many matching excerpts and
        # the richest backing, against two thinner files. Under a per-excerpt credit
        # every row of a-fat outranked b-thin and c-thin together, so a cap of 3
        # answered a "which studies …?" question with ONE paper three times — the
        # reader's evidence that the KB holds one such paper. The cap is what makes
        # this visible, and it is the default render budget, not a test artifact.
        root = build(
            tmp_path,
            {
                "a-fat": "\n".join(["alpha 결과."] + [""] * 8 + ["alpha 결과."] + [""] * 8 + ["alpha 결과."]),
                "b-thin": "alpha 결과.",
                "c-thin": "alpha 결과.",
            },
            accepted=(
                'relation("s1", "이점", "설명가능성_향상").\n'
                'relation("s2", "핵심_기법", "설명가능성_분석").\n'
                'relation("s3", "다룬_주제", "설명가능성_평가").\n'
            ),
            candidates=csv_rows(
                "s1,이점,설명가능성_향상,sources/a-fat.md,confirmed,0.9,",
                "s2,핵심_기법,설명가능성_분석,sources/a-fat.md,confirmed,0.9,",
                "s3,다룬_주제,설명가능성_평가,sources/a-fat.md,confirmed,0.9,",
                "s1,이점,설명가능성_향상,sources/b-thin.md,confirmed,0.9,",
                "s1,이점,설명가능성_향상,sources/c-thin.md,confirmed,0.9,",
            ),
        )
        question = "alpha 설명가능성을"
        # Premise: a-fat really does contribute three excerpts, so a cap of 3 is a
        # cap it could fill on its own.
        everything = ask_router.search(question, root, limit=None)
        assert len([r for r in everything if r["file"] == "sources/a-fat.md"]) == 3
        top = ask_router.search(question, root, limit=3)
        assert refs(top) == [
            "sources/a-fat.md:3",  # credited: (2, 1 + 3)
            "sources/b-thin.md:3",  # credited: (2, 1 + 1)
            "sources/c-thin.md:3",  # credited: (2, 1 + 1)
        ]
        assert len({r["file"] for r in top}) == 3


    def test_a_credit_reached_through_a_declared_synonym_is_damped_the_same_way(self, tmp_path):
        # #606 lets a question word reach the KB's vocabulary through
        # policy/vocabulary-synonyms.md, and that term joins #594's credit like any
        # other. So it inherits this defect too: it is still a per-FILE constant, and
        # without the damping a single declaration would lift every excerpt of the
        # declared file as a block. The rule does not distinguish how a term was
        # reached — asserted here rather than left to read off _credit_backing,
        # because the two channels meet in kb_vocabulary_bridge and a change on
        # either side could quietly exempt one.
        #
        # Same geometry as the first test in this class: the credit lifts f-backed's
        # first excerpt above z-plain and leaves its second below.
        root = build(
            tmp_path,
            {
                "f-backed": "해석가능성 해석가능성 해석가능성 결과.\n" + "\n" * 8 + "해석가능성 결과.",
                "z-plain": "해석가능성 그리고 재현가능성 결과.",
            },
            accepted='relation("s", "이점", "설명가능성_향상").\n',
            candidates=csv_rows("s,이점,설명가능성_향상,sources/f-backed.md,confirmed,0.9,"),
            synonyms="- `재현가능성` = `설명가능성`\n",
        )
        question = "해석가능성에서 재현가능성을"
        # Premise: the reach really is the table's. '재현가능성' shares no prefix with
        # '설명가능성' — without the declaration this question backs nothing at all,
        # and the assertion below would be about the previous test's axis.
        bridged = ask_router.kb_vocabulary_bridge(question, root)
        assert list(bridged) == ["sources/f-backed.md"]
        assert bridged["sources/f-backed.md"]["synonyms"] == [["재현가능성을", "설명가능성"]]
        assert ask_router.kb_vocabulary_bridge(question, build(
            tmp_path / "nosyn",
            {"f-backed": "해석가능성 결과."},
            accepted='relation("s", "이점", "설명가능성_향상").\n',
            candidates=csv_rows("s,이점,설명가능성_향상,sources/f-backed.md,confirmed,0.9,"),
        )) == {}
        assert refs(ask_router.search(question, root, limit=None)) == [
            "sources/f-backed.md:3",  # credited: (2, 3 + 1)
            "sources/z-plain.md:3",  # lexical only: (2, 2)
            "sources/f-backed.md:12",  # uncredited: (1, 1)
        ]


class TestWhatTheCreditMayNotOverrule:
    """Two standing contracts the new component must not become a way around."""

    def test_a_supplementary_excerpt_still_ranks_below_every_primary_one(self, tmp_path):
        # #572: the directory grade is the top key. A decisions/ note named as a
        # provenance source would otherwise buy its way above source text — and it
        # is the ONE row type SKILL.md promises can never displace a source.
        root = build(
            tmp_path,
            {"a-plain": "A neurosymbolic method."},
            supplementary={"notes": "A neurosymbolic method."},
            accepted='relation("s", "이점", "해석가능성_향상").\n',
            candidates=csv_rows("s,이점,해석가능성_향상,decisions/notes.md,confirmed,0.9,"),
        )
        results = ask_router.search("neurosymbolic 해석가능성에서", root, limit=None)
        assert refs(results) == ["sources/a-plain.md:3", "decisions/notes.md:3"]
        assert results[0]["dir"] == "sources"

    def test_a_credited_row_is_never_labeled_a_bridged_one(self, tmp_path):
        # 수용 기준 2 of #576 defines the tag as "reached via KB vocabulary and NOT
        # by a lexical match". This row WAS matched lexically; tagging it would say
        # something false, and the reader would look for a keyword-free excerpt.
        root = build(
            tmp_path,
            {"b-backed": "A neurosymbolic method."},
            accepted='relation("s", "이점", "해석가능성_향상").\n',
            candidates=csv_rows("s,이점,해석가능성_향상,sources/b-backed.md,confirmed,0.9,"),
        )
        results = ask_router.search("neurosymbolic 해석가능성에서", root, limit=None)
        assert results and all("via" not in r for r in results)
        out = ask_router.render_wiki_answer("q", "unknown entity", results)
        assert ask_router.VIA_KB_VOCABULARY_TAG not in out
        assert "← accepted" not in out
        # And the block it renders in is still the unverified one: a higher rank is
        # not a verdict. No line STARTS a VERIFIED block ('UNVERIFIED' on line 1
        # contains the substring, so an unanchored check passes vacuously).
        assert out.startswith("UNVERIFIED — wiki exploration")
        assert not [line for line in out.splitlines() if line.startswith("VERIFIED")]

    def test_a_promoted_row_no_longer_outranks_the_source_that_holds_the_keyword(self, tmp_path):
        # Measured on the reference KB before this change, question '오메가-3 보충이
        # COPD 환자에게 효과있음을 보인 연구는?': the top FOUR rows of 151 were promoted
        # rows, above every excerpt that actually contains the reader's 'COPD' —
        # a promoted row's frequency is its backing fact count, which is unbounded,
        # while a lexical row's was its keyword count alone. Crediting the cited rows
        # puts the row that answers BOTH channels first; the promoted row keeps its
        # own key and stays reachable (it is still returned, just not first).
        root = build(
            tmp_path,
            {"a-cited": "A neurosymbolic method.", "z-uncited": "An unrelated method."},
            accepted=(
                'relation("s1", "이점", "해석가능성_향상").\n'
                'relation("s2", "핵심_기법", "해석가능성_분석").\n'
                'relation("s3", "다룬_주제", "해석가능성_평가").\n'
            ),
            candidates=csv_rows(
                "s1,이점,해석가능성_향상,sources/a-cited.md,confirmed,0.9,",
                "s1,이점,해석가능성_향상,sources/z-uncited.md,confirmed,0.9,",
                "s2,핵심_기법,해석가능성_분석,sources/z-uncited.md,confirmed,0.9,",
                "s3,다룬_주제,해석가능성_평가,sources/z-uncited.md,confirmed,0.9,",
            ),
        )
        results = ask_router.search("neurosymbolic 해석가능성에서", root, limit=None)
        assert refs(results) == ["sources/a-cited.md:3", "sources/z-uncited.md:1"]
        assert "via" not in results[0] and results[1]["via"]["terms"] == ["해석가능성에서"]


class TestWhatIsLeftAlone:
    """The credit is additive: a KB that cannot produce it must be unaffected."""

    def test_a_kb_without_fact_files_ranks_exactly_as_before(self, tmp_path):
        # The degrade path (#576 수용 기준 5), now also for the cited rows. Corpus
        # order, no reordering, no new keys.
        root = build(tmp_path, {"a-plain": "A neurosymbolic method.", "b-plain": "A neurosymbolic method."})
        results = ask_router.search("neurosymbolic 해석가능성에서", root, limit=None)
        assert refs(results) == ["sources/a-plain.md:3", "sources/b-plain.md:3"]
        assert all("via" not in r for r in results)

    def test_a_non_engine_status_backs_nothing(self, tmp_path):
        # Same filter the promoted path applies (ENGINE_STATUSES). A superseded row
        # must not raise a source's rank either — quietly re-ordering an answer
        # around retired evidence is the same defect as quoting it.
        root = build(
            tmp_path,
            {"a-plain": "A neurosymbolic method.", "b-super": "A neurosymbolic method."},
            accepted='relation("s", "이점", "해석가능성_향상").\n',
            candidates=csv_rows("s,이점,해석가능성_향상,sources/b-super.md,superseded,0.9,"),
        )
        assert refs(ask_router.search("neurosymbolic 해석가능성에서", root, limit=None)) == [
            "sources/a-plain.md:3",
            "sources/b-super.md:3",
        ]

    def test_the_recall_tally_still_reports_the_bridged_term_as_unmatched(self, tmp_path):
        # #575 reports which of the question's keywords the CORPUS contains. The
        # credit is a ranking signal, not a claim about the text: '해석가능성에서'
        # occurs in no file here, and a tally that said otherwise would promise
        # wording the sources do not use.
        root = build(
            tmp_path,
            {"b-backed": "A neurosymbolic method."},
            accepted='relation("s", "이점", "해석가능성_향상").\n',
            candidates=csv_rows("s,이점,해석가능성_향상,sources/b-backed.md,confirmed,0.9,"),
        )
        recall: dict[str, list[str]] = {}
        ask_router.search("neurosymbolic 해석가능성에서", root, limit=None, recall=recall)
        assert recall == {"matched": ["neurosymbolic"], "unmatched": ["해석가능성에서"]}
