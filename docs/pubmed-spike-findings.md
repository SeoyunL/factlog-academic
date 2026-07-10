# Spike: PubMed E-utilities live verification (#160)

*Measurement only. No code path added — the `factlog/integrations/pubmed/` package is #159/#162/#163's to build. This document records what the live NCBI E-utilities API actually returned on 2026-07-11, so the parser (#163) is written against observed behaviour, not against the spec's assumptions.*

Endpoint: `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/` (`esearch`, `efetch`, `esummary`).
No API key used (this is the regime #159's default runs in). All fetches were `retmode=xml`.
Every claim below is a live observation; the throwaway probe scripts lived in scratchpad and are not committed.

---

## Summary of decisions this spike settles

1. **stdlib `xml.etree.ElementTree` is enough. Do not add a biopython dependency.** Every one of the seven checks below was parsed with a handful of ElementTree lines. The record schema is nested but boringly so; nothing here needed biopython `Entrez`'s DTD handling. Rationale expanded in the last section, following the #51 addendum v2 §2.4 precedent (prefer direct control unless a library's value is clear; pyalex was rejected on the same ground).
2. **The spec's retraction example (PMID 16354850) is *correct*, contrary to the preliminary concern that flagged it.** See check 1 — the preliminary check was wrong, and the record carries both retraction markers. No spec change needed there.
3. **A real spec-adjacent hazard did surface** (retraction-notice records look retraction-shaped but are the opposite; and the two retraction markers, while co-occurring today, are not guaranteed to). Draft issue notes are in the last section. **No issues were filed — that decision is the human's.**

---

## 1. Retraction detection

**PMID 16354850 (spec §6.4's example) — both markers present. The preliminary "not visible" concern does not reproduce.**

Live `efetch`:
- `PublicationTypeList` contains `Retracted Publication` (UI `D016441`), alongside Journal Article / RCT / Comparative Study / Research Support.
- `CommentsCorrectionsList` contains `<CommentsCorrections RefType="RetractionIn">` → PMID `18842931` (RefSource `Chest. 2008 Oct;134(4):893.`).

So both markers the spec relies on are present on this record. **The preliminary observation that "Retracted Publication" was absent from the PublicationTypeList did not reproduce on the live 2026-07-11 fetch.** Either the record was recurated since that observation, or the preliminary check inspected the wrong list. The spec's §6.4 example is safe to keep.

**Cross-section over real retractions (the marker-disagreement question).** I pulled recent retractions two ways: `esearch db=pubmed term="Retracted Publication"[Publication Type] sort=date`, and the `RetractionOf` targets of the newest `"Retraction Notice"[Publication Type]` records. Across ~30 retracted articles spanning indexing statuses `MEDLINE`, `In-Process`, `PubMed-not-MEDLINE`, and `Publisher`:

- **The two markers co-occurred in every single case.** `Retracted Publication` pub-type and a `RetractionIn` comment appeared together even in `Publisher`- and `PubMed-not-MEDLINE`-status records that have *no* MeSH indexing. NCBI evidently applies both when the retraction link is created, not at MEDLINE curation time. I did **not** find a live case of one marker without the other.
- **A weaker disagreement mode does exist:** in 2 of the first 10 retracted records (PMIDs 42235148, 42129929) the `RetractionIn` element was present but its child `<PMID>` was empty — the retraction is asserted via a `RefSource` citation string only, with no machine-linkable notice PMID. A parser must not assume `RetractionIn` always yields a PMID.

**The false-positive trap the spec does not spell out.** The retraction *notice* is a separate PubMed record and looks retraction-shaped in the opposite direction:
- notice PublicationType is `Retraction Notice` (not `Retracted Publication`);
- its `CommentsCorrections RefType` is `RetractionOf` (not `RetractionIn`), pointing *back* to the retracted article.

Examples observed: notice `42328254` `RetractionOf` → `42245901`; notice `42390976` `RetractionOf` → `42090157`. A parser that keys on the substring "retract" anywhere, or that treats any retraction-related `CommentsCorrections` as "this paper is retracted", will **wrongly flag every retraction notice as itself retracted.** The correct rule: a record is retracted iff PublicationType contains `Retracted Publication` **OR** a `CommentsCorrections` has `RefType="RetractionIn"`; `Retraction Notice` / `RetractionOf` must be excluded.

**Verdict on the spec's "either marker alone is sufficient" rule:** safe. Today the markers are redundant (they co-occur), so OR-ing them costs nothing; and if NCBI ever adds one before the other during a curation lag, OR-ing catches the retraction earlier. Keep the OR.

## 2. Non-retracted control

PMID `33301246` (a phase II/III RCT): PublicationTypeList has no `Retracted Publication`; CommentsCorrectionsList has 11 `CommentIn` refs and **zero** `RetractionIn`. Both retraction markers absent → a correct parser leaves it unflagged. This is the guard against a parser that over-triggers on the presence of *any* `CommentsCorrections` (this record has plenty, none of them retractions).

## 3. Rate limiting (no API key, no delay)

**NCBI enforces hard, and it tells you.** Serial requests never tripped it because each round-trip is ~0.45s, holding the effective rate under the 3/s ceiling — 6 back-to-back serial `efetch`es all returned HTTP 200. Every response carried `X-RateLimit-Limit: 3` and a decrementing `X-RateLimit-Remaining`.

Forcing the violation with 12 concurrent requests:
- 8 of 12 returned **HTTP 429 Too Many Requests**;
- header **`Retry-After: 2`**;
- JSON body: `{"error":"API rate limit exceeded","api-key":"<caller-IP>","count":"4","limit":"3"}` (the `api-key` field echoes the caller IP when no key is used).

So the failure is **loud, not silent** — a real 429 with a machine-readable `Retry-After`, not a quiet truncation or a corrupt body. Recommended client behaviour for #159/#162: serialize requests with ≥0.34s spacing (stay ≤3/s without a key; ≤10/s with one), and on a 429 honour `Retry-After` (observed value 2s) before retrying. Do **not** parallelize unkeyed requests — the concurrent burst above is exactly what earns an IP-level block, so #162's client should default to a single-flight queue.

## 4. Merged PMIDs

I could **not** confirm a live merged-PMID example in the spike window (candidate historically-suspect PMIDs 20493475, 52, 11250746 all resolved to normal single records). What I *can* state from the batch-omission behaviour:

- `efetch` returns records **by omission**, not by substitution. Requesting `16354850,999999999,33301246` returned exactly `[16354850, 33301246]` — the absent id is silently dropped; `efetch` never returns a record whose `<PMID>` differs from a requested id.
- Therefore a merged/discontinued PMID would surface to the caller the same way a deleted one does (check 5): **as an absence in the returned set**, discoverable only by diffing requested ids against returned `MedlineCitation/PMID`s. The E-utilities `efetch`/`esummary` responses do **not** carry a forwarding pointer to the surviving PMID.

**Consequence for the caller:** the API alone cannot tell you "PMID X was merged into Y." It can only tell you "X returned nothing." Recovering the survivor requires NLM's out-of-band deleted-PMID mapping, which is out of scope for a live-fetch client. **Remaining uncertainty — flagged for #163.**

## 5. Deleted / nonexistent PMIDs

- A **well-formed but nonexistent** PMID (`999999999`, `42328254999`) → **HTTP 200** with an empty `<PubmedArticleSet></PubmedArticleSet>` (205 bytes). Not an error.
- A **malformed** id (`0`) → **HTTP 400** with `<eFetchResult><ERROR>ID list is empty! Possibly it has no correct IDs.</ERROR></eFetchResult>`.
- `esummary` **distinguishes** the two states per-id: nonexistent/deleted → `<DocumentSummary uid="999999999"><error>cannot get document summary</error></DocumentSummary>` (HTTP 200, `DocumentSummarySet status="OK"`); a real record → a full `DocumentSummary`.

So the three "empty-ish" states are all distinguishable:
| situation | efetch signal |
|---|---|
| deleted / nonexistent valid PMID | HTTP 200, empty `<PubmedArticleSet/>` |
| malformed id | HTTP 400, `<ERROR>` element |
| network failure | connection error / timeout / non-200 with no PubMed XML |
| empty *search* result | different endpoint — `esearch` returns `<Count>0</Count>` |

**Guidance:** a client must treat "HTTP 200 + empty PubmedArticleSet" as "this PMID is gone", **not** as a network error and **not** as a parse failure. Distinguishing deleted from never-existed is possible only via `esummary`'s per-id `<error>`, and neither the API nor the empty efetch tells you *why* it is gone.

## 6. Missing fields (author / abstract / DOI)

All three are normally absent; the parser must treat absence as data, not as an error.

- **Abstract:** absent on 4/12 of a mixed batch — every absence was an old record (PMID 1 (1975), 13718526, 5432063, 4586824). Abstract absence correlates with age.
- **DOI:** absent on 4/8 old news items (1980–85), which carried only an `ArticleId IdType="pubmed"` and no `doi`. Note DOI can live in **two** places — `ArticleIdList/ArticleId[@IdType="doi"]` and `ELocationID[@EIdType="doi"]`; the parser must check both before concluding "no DOI".
- **Author:** `AuthorList` is sometimes **entirely absent** — 4/15 recent `Published Erratum` records had no `AuthorList` element at all (not an empty one). Also watch `CollectiveName` (group authorship with no personal-name `Author`).
- **Abstract shape:** structured abstracts split into multiple `<AbstractText>` segments with `@Label` (e.g. BACKGROUND/METHODS/RESULTS/CONCLUSIONS on PMID 33301246); the full abstract is the concatenation. In the records sampled, `AbstractText` had no inline child markup, but the DTD permits it, so use `''.join(node.itertext())` rather than `.text`.

## 7. MeSH major/minor — the #53 landmine, reproduced live

This is the check that matters most, because #53 traced the 2001–2009 major-topic Jaccard collapse (to 0.10) to OpenAlex discarding `QualifierName`-level majorness. **Both attributes exist and are populated in PubMed's own feed — but *where* the majorness lives moved between eras.**

**Pre-2010 record — PMID 16354850 (2005).** Majorness is carried largely on the **QualifierName**, not the DescriptorName:
- `DescriptorName MajorTopicYN="Y"`: only **1** descriptor (`Dietary Supplements`).
- `QualifierName MajorTopicYN="Y"`: **3** qualifiers, each under a descriptor whose own `MajorTopicYN="N"`:
  - `Fatty Acids, Omega-3` (Desc=N) / `therapeutic use` (Qual=**Y**)
  - `Inflammation Mediators` (Desc=N) / `analysis` (Qual=**Y**)
  - `Pulmonary Disease, Chronic Obstructive` (Desc=N) / `drug therapy` (Qual=**Y**)

A descriptor-only reading of this record finds **1** major topic; the true count including qualifier majorness is **4**. That is exactly the information OpenAlex drops, and exactly the mechanism behind #53's 0.10 collapse — reproduced here on the raw source.

**Post-2022 record — PMID 42277084 (2026).** Majorness is carried on the **DescriptorName**:
- `DescriptorName MajorTopicYN="Y"`: **3** (`Bioelectric Energy Sources`, `Oxidoreductases`, `Enzymes, Immobilized`).
- `QualifierName MajorTopicYN="Y"`: **0** (all qualifiers `N`).

**Conclusion for #163:** the PubMed parser must read `MajorTopicYN` on **both** `DescriptorName` **and** every `QualifierName`, and treat a heading as major if the descriptor **or any** of its qualifiers is `Y`. Reading only the descriptor silently undercounts major topics in pre-2010 records — the precise failure factlog exists to avoid, and the reason to ingest PubMed's own MeSH rather than OpenAlex's flattened concepts for that era.

---

## biopython `Entrez` vs stdlib `xml.etree.ElementTree` — decision, on evidence

**Decision: stdlib `ElementTree` (over the httpx client #159 already carries). Do not add biopython.**

Measured basis, not spec preference:
- Every check above — retraction markers across two `CommentsCorrections` RefTypes, PublicationType lists, MeSH major/minor across two attribute levels, ArticleId/ELocationID DOI in two places, batch omission diffing, structured abstracts — was parsed in a few ElementTree lines each. No check hit a wall that biopython would have cleared.
- The genuinely awkward parts (majorness on two levels, DOI in two places, absence-as-signal, 429 handling) are **semantic** decisions that a client must make explicitly regardless of the XML library; biopython's `Entrez.read` would hand back nested dicts but would not make the majorness-OR or the notice-vs-retracted distinction for us. It changes the shape of the drudgery, not its amount.
- biopython adds a heavy transitive dependency to `factlog[pubmed]` for a parse that stdlib already does. #51 addendum v2 §2.4 set the precedent: reject a wrapper library when direct control is tractable (pyalex was rejected). PubMed's schema is more nested than OpenAlex JSON, but not enough to overturn that.

**Caveat carried forward:** this judgement is over the seven record shapes sampled here. If #163 hits `PubmedBookArticle`, MEDLINE (non-XML) retmode, or the DTD's rarer branches, the cost balance should be re-weighed there — but on the evidence in hand, stdlib wins.

---

## Draft issue notes (NOT filed — for the human to decide, in the #51/#53/#54 style)

**Draft A — Retraction notices must be excluded from the retracted set.**
> The retracted-article detector keys on `Retracted Publication` pub-type OR `RetractionIn` comment. The retraction *notice* record carries `Retraction Notice` pub-type and a `RetractionOf` comment — shaped like a retraction but pointing the other way. A naive "any retraction-related marker ⇒ retracted" rule flags every notice as itself retracted (observed notices 42328254, 42390976). Add the exclusion + a counter-example test on a `Retraction Notice` record.

**Draft B — `RetractionIn` may lack a linkable notice PMID.**
> 2/10 sampled retracted records (42235148, 42129929) had a `RetractionIn` element whose child `<PMID>` was empty (RefSource citation string only). Any code that assumes `RetractionIn` yields a notice PMID will NPE/skip. The retraction is still true; only the link target is missing.

**Draft C — the spec §6.4 preliminary concern about PMID 16354850 does not reproduce; keep the example.**
> The live 2026-07-11 fetch shows 16354850 carries both `Retracted Publication` pub-type and a `RetractionIn` comment. The preliminary "Retracted Publication not visible" observation could not be reproduced. Recommend leaving §6.4's example as-is (this note exists so the discrepancy is on record, not lost).

## Remaining uncertainty

- **Merged PMIDs:** no confirmed live example found; behaviour inferred from the omission model (a merged PMID reads as an absence, with no survivor pointer in the API). Not directly observed — #163 should keep a TODO to source a known merged PMID for a regression fixture.
- Marker co-occurrence (check 1) is **empirical for the ~30 records sampled**, not guaranteed by NCBI contract; the OR rule is what makes co-occurrence non-load-bearing.
- The stdlib-vs-biopython call is scoped to `PubmedArticle` XML; `PubmedBookArticle` and non-XML retmodes were not exercised.
