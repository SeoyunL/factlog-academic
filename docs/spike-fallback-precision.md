# Spike: title+author+year fallback precision (#74)

*Measurement only. No import path changed. Regenerate with `python3 tools/spike_fallback_precision.py --write-report`.*

## Corpus

- Queries (fixed): 10 `cat:<subfield> AND jr:<venue>` searches, page size 60, sort `submitted` descending:
  - `cat:cs.CL AND jr:ACL`
  - `cat:cs.CL AND jr:EMNLP`
  - `cat:cs.CL AND jr:NAACL`
  - `cat:cs.CL AND jr:TACL`
  - `cat:cs.CL AND jr:COLING`
  - `cat:cs.LG AND jr:NeurIPS`
  - `cat:cs.LG AND jr:ICML`
  - `cat:cs.LG AND jr:ICLR`
  - `cat:cs.CV AND jr:CVPR`
  - `cat:cs.AI AND jr:AAAI`

- arXiv only searches nine fields, so we cannot filter on "has a DOI" directly; `jr:` (journal reference) is the proxy for a published paper, and published papers are the ones with DOIs.
- The queries surface 113 DOI-carrying arXiv papers (deduplicated by arXiv base id). **Sample size: 97** of them were resolved against OpenAlex within one day's ~1000-credit budget (10 credits per title search); the committed cache pins exactly which, so replay is deterministic.
- Each resolved paper supplies a DOI-true OpenAlex pairing as ground truth.
- Ground truth retrievable (the DOI-true record appeared in the title search results): **88/97**. Recall is computed over these.
- Candidate pool per paper: OpenAlex `search=<title>` top 25.
- Cost: OpenAlex title search = 10 credits/paper, DOI lookup = 0.

**Why this size means something, and its bias.** A precision estimate on the fired matches has a standard error near sqrt(p(1-p)/n); at these counts a false-merge rate above a few percent is distinguishable from zero, which is what #75/#76 need — the question is whether false merges happen at all, not their third decimal. The sample is biased *toward* the matcher: DOI-carrying papers are published work with clean, canonical metadata, whereas #57 shows the fallback would mostly run on fresh preprints with none. **Which way that biases the harmful-merge rate is unmeasured.** A DOI-less preprint usually has one OpenAlex record and no published twin, so its decoy pool is smaller and same-title collisions may be rarer; equally, its metadata is thinner. This report does not claim to bound that population — there is no ground truth for it.

## Precision / recall

The swept score is normalized-title token Jaccard. First-author surname agreement and year agreement are required conjuncts (a match must clear all three).

**Two of these columns are traps, and the corrected ones sit beside them.** `FP` is the matcher firing on a candidate whose DOI differs from the paper's own — but most of those are the paper's *own arXiv preprint mirror* under a second OpenAlex record, and merging one of those is arguably right. `of which harmful` counts only the merges onto a genuinely different source record. Likewise `ambiguous` counts a paper and its own mirror as two rival works; `ambiguous (mirrors collapsed)` does not. Read the corrected columns. The uncorrected ones are kept because they are what a naive evaluation reports, and the gap between them is the point.

### Year tolerance ±1 (preprint vs publication year)

| title threshold | fired | TP | FP | of which harmful | FN | precision | precision (mirrors=TP) | recall | ambiguous | ambiguous (mirrors collapsed) |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.50 | 87 | 73 | 14 | 2 | 3 | 0.839 | 0.977 | 0.961 | 35 | 1 |
| 0.60 | 87 | 73 | 14 | 2 | 3 | 0.839 | 0.977 | 0.961 | 35 | 1 |
| 0.70 | 86 | 72 | 14 | 2 | 4 | 0.837 | 0.977 | 0.947 | 35 | 1 |
| 0.80 | 86 | 72 | 14 | 2 | 4 | 0.837 | 0.977 | 0.947 | 35 | 1 |
| 0.90 | 86 | 72 | 14 | 2 | 4 | 0.837 | 0.977 | 0.947 | 35 | 1 |
| 1.00 | 86 | 72 | 14 | 2 | 4 | 0.837 | 0.977 | 0.947 | 35 | 1 |

### Year must match exactly

| title threshold | fired | TP | FP | of which harmful | FN | precision | precision (mirrors=TP) | recall | ambiguous | ambiguous (mirrors collapsed) |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.50 | 74 | 60 | 14 | 1 | 15 | 0.811 | 0.986 | 0.800 | 30 | 0 |
| 0.60 | 74 | 60 | 14 | 1 | 15 | 0.811 | 0.986 | 0.800 | 30 | 0 |
| 0.70 | 73 | 59 | 14 | 1 | 16 | 0.808 | 0.986 | 0.787 | 30 | 0 |
| 0.80 | 73 | 59 | 14 | 1 | 16 | 0.808 | 0.986 | 0.787 | 30 | 0 |
| 0.90 | 73 | 59 | 14 | 1 | 16 | 0.808 | 0.986 | 0.787 | 30 | 0 |
| 1.00 | 73 | 59 | 14 | 1 | 16 | 0.808 | 0.986 | 0.787 | 30 | 0 |

## Confusion cases (named individually)

At title threshold 0.80, year tolerance ±1, the matcher fired on the wrong DOI for **14 of 97** papers. Every one shares the arXiv paper's first author, an adjacent year, and — at threshold 1.00 — a byte-identical title with the record it was merged into; only the DOI differs.

**But 12 of those 14 are the paper's own arXiv preprint mirror**, a second OpenAlex record of the same work. Merging one is arguably correct. Only **2 of 86** fired matches landed on a genuinely different source record — a harmful-merge rate of **2.3%** (95% Wilson CI 0.6%–8.1%).

The same correction applies to ambiguity. Counting a paper and its own mirror as two rival works gives **35 of 97**; collapsing the mirror onto the work it mirrors gives **1 of 97**. Ambiguity between *genuinely distinct* works is rare, not endemic. An earlier draft of this report leaned on the uncollapsed figure; it was inflated by more than an order of magnitude.

What the wrong record actually was:
- 12× — arXiv-preprint mirror (same work, distinct OpenAlex record)
- 2× — distinct non-arXiv DOI (a different source record; may be a different work)

**A benign mirror is trivially separable, and an earlier draft of this report said otherwise.** Its DOI carries the `10.48550/arxiv.` prefix, or the record echoes the paper's own arXiv id. Both are fields a real matcher has, and this script uses them to classify. Nothing subtle is required.

What remains after that correction is the harmful category, and it is small: a merge onto a genuinely different source record. Those *are* unreachable by title+author+year, because at threshold 1.00 the two works have a byte-identical title, the same first-author surname, and adjacent years. No similarity function defined over those three fields can separate them, so no threshold helps.

Richer OpenAlex metadata *does* separate the two harmful cases — checked live: `work_type` is `article` (AAAI) against `preprint` (medRxiv) for one, and `conference-paper` against `article` for the other. But it separates them without saying **which one is this paper**: there is no rule over `work_type` that picks the right record in both cases, and a preprint's own published version is exactly as plausible a target as an unrelated posting of the same title. Distinguishable is not the same as identifiable, and only the second would license an automatic merge.

So the case for the human gate (#75, #76) does not rest on a bad precision number. It rests on this: a small but real rate of merges onto genuinely different source records, which the fallback's own inputs cannot rule out, and which fail **silently** — a false merge attaches one paper's provenance to another paper's text and nothing errors (P2).

Named individually — arXiv paper on the left, the different-DOI OpenAlex record it was merged into on the right:

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2404.01822 (2024) — 'A (More) Realistic Evaluation Setup for Generalisation of Community Models on Malicious Content Detection'
    - first author: 'Ivo Verhoeven'; DOI `10.18653/v1/2024.findings-naacl.30`
  - OpenAlex:W4393931081 (2024) — 'A (More) Realistic Evaluation Setup for Generalisation of Community Models on Malicious Content Detection'
    - first author: 'Ivo Verhoeven'; DOI `10.48550/arxiv.2404.01822`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2404.08189 (2024) — 'Reducing hallucination in structured outputs via Retrieval-Augmented Generation'
    - first author: 'Patrice Béchard'; DOI `10.18653/v1/2024.naacl-industry.19`
  - OpenAlex:W4394838812 (2024) — 'Reducing hallucination in structured outputs via Retrieval-Augmented Generation'
    - first author: 'Patrice Béchard'; DOI `10.48550/arxiv.2404.08189`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2406.09043 (2024) — 'Language Models are Crossword Solvers'
    - first author: 'Soumadeep Saha'; DOI `10.18653/v1/2025.naacl-long.104`
  - OpenAlex:W4399695551 (2024) — 'Language Models are Crossword Solvers'
    - first author: 'Soumadeep Saha'; DOI `10.48550/arxiv.2406.09043`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2410.14235 (2024) — 'Towards Robust Knowledge Representations in Multilingual LLMs for Equivalence and Inheritance based Consistent Reasoning'
    - first author: 'Gaurav Arora'; DOI `10.18653/v1/2025.naacl-long.394`
  - OpenAlex:W4403995631 (2024) — 'Towards Robust Knowledge Representations in Multilingual LLMs for Equivalence and Inheritance based Consistent Reasoning'
    - first author: 'Gaurav Arora'; DOI `10.48550/arxiv.2410.14235`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2411.15927 (2024) — 'Generative Prompt Internalization'
    - first author: 'Haebin Shin'; DOI `10.18653/v1/2025.naacl-long.376`
  - OpenAlex:W4404987031 (2024) — 'Generative Prompt Internalization'
    - first author: 'Haebin Shin'; DOI `10.48550/arxiv.2411.15927`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2502.06394 (2025) — 'SynthDetoxM: Modern LLMs are Few-Shot Parallel Detoxification Data Annotators'
    - first author: 'Daniil Moskovskiy'; DOI `10.18653/v1/2025.naacl-long.294`
  - OpenAlex:W4407359015 (2025) — 'SynthDetoxM: Modern LLMs are Few-Shot Parallel Detoxification Data Annotators'
    - first author: 'Daniil Moskovskiy'; DOI `10.48550/arxiv.2502.06394`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2503.16457 (2025) — 'Integrating Personality into Digital Humans: A Review of LLM-Driven Approaches for Virtual Reality'
    - first author: 'Iago Alves Brito'; DOI `10.18653/v1/2025.findings-emnlp.506`
  - OpenAlex:W4417149532 (2025) — 'Integrating Personality into Digital Humans: A Review of LLM-Driven Approaches for Virtual Reality'
    - first author: 'Iago Alves Brito'; DOI `10.48550/arxiv.2503.16457`

- **title Jaccard 1.00** — _distinct non-arXiv DOI (a different source record; may be a different work)_
  - arXiv:2506.18120 (2025) — 'The Syntactic Acceptability Dataset (Preview): A Resource for Machine Learning and Linguistic Analysis of English'
    - first author: 'Tom S Juzek'; DOI `10.17605/OSF.IO/5E8KZ`
  - OpenAlex:W7131853804 (2024) — 'The Syntactic Acceptability Dataset (Preview): A Resource for Machine Learning and Linguistic Analysis of English'
    - first author: 'Thomas Stephan Juzek'; DOI `10.63317/3f2yamzokm9i`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2508.21589 (2025) — 'Middo: Model-Informed Dynamic Data Optimization for Enhanced LLM Fine-Tuning via Closed-Loop Learning'
    - first author: 'Zinan Tang'; DOI `10.18653/v1/2025.emnlp-main.350`
  - OpenAlex:W4414691774 (2025) — 'Middo: Model-Informed Dynamic Data Optimization for Enhanced LLM Fine-Tuning via Closed-Loop Learning'
    - first author: 'Tang, Zinan'; DOI `10.48550/arxiv.2508.21589`

- **title Jaccard 1.00** — _distinct non-arXiv DOI (a different source record; may be a different work)_
  - arXiv:2509.00891 (2025) — 'ChatCLIDS: Simulating Persuasive AI Dialogues to Promote Closed-Loop Insulin Adoption in Type 1 Diabetes Care'
    - first author: 'Zonghai Yao'; DOI `10.1609/aaai.v40i46.41305`
  - OpenAlex:W4413974202 (2025) — 'ChatCLIDS: Simulating Persuasive AI Dialogues to Promote Closed-Loop Insulin Adoption in Type 1 Diabetes Care'
    - first author: 'Zonghai Yao'; DOI `10.1101/2025.09.02.25334973`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2509.01058 (2025) — 'Speaking at the Right Level: Literacy-Controlled Counterspeech Generation with RAG-RL'
    - first author: 'Xiaoying Song'; DOI `10.18653/v1/2025.findings-emnlp.153`
  - OpenAlex:W4414747030 (2025) — 'Speaking at the Right Level: Literacy-Controlled Counterspeech Generation with RAG-RL'
    - first author: 'Xiaofei Song'; DOI `10.48550/arxiv.2509.01058`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2509.17459 (2025) — 'PRINCIPLES: Synthetic Strategy Memory for Proactive Dialogue Agents'
    - first author: 'Namyoung Kim'; DOI `10.18653/v1/2025.findings-emnlp.1164`
  - OpenAlex:W4415254635 (2025) — 'PRINCIPLES: Synthetic Strategy Memory for Proactive Dialogue Agents'
    - first author: 'Nam‐Young Kim'; DOI `10.48550/arxiv.2509.17459`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2509.20810 (2025) — 'Enrich-on-Graph: Query-Graph Alignment for Complex Reasoning with LLM Enriching'
    - first author: 'Songze Li'; DOI `10.18653/v1/2025.emnlp-main.390`
  - OpenAlex:W4414789398 (2025) — 'Enrich-on-Graph: Query-Graph Alignment for Complex Reasoning with LLM Enriching'
    - first author: 'Songze Li'; DOI `10.48550/arxiv.2509.20810`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2511.07204 (2025) — 'Evaluating Online Moderation Via LLM-Powered Counterfactual Simulations'
    - first author: 'Giacomo Fidone'; DOI `10.1609/aaai.v40i45.41186`
  - OpenAlex:W7105507688 (2025) — 'Evaluating Online Moderation Via LLM-Powered Counterfactual Simulations'
    - first author: 'Fidone, Giacomo'; DOI `None`

## Behaviour on the required edge cases

- **large author list (21 authors, arXiv:2502.16923)**: the matcher reads only the first author ('Kiran Ramnath' -> surname 'ramnath'); list length is irrelevant, so a 100+ author collaboration neither helps nor breaks it.
- **150-author synthetic list**: first_surname -> 'family'; unaffected by the other 149.
- **`et al.` truncation**: arXiv marks a truncated list with a sentinel author. surname('et al.') is blanked to '' (was 'al' without the guard), so a truncated first author yields no surname and the gate fails closed rather than matching on the word 'al'. But if the *real* first author is present and only later ones are dropped, truncation is invisible to a first-author matcher — it cannot tell a 3-author paper from its 200-author twin.
- **non-ASCII author name (real sample)**: 'Patrice Béchard' -> surname 'bechard' (NFKD fold drops the accent, so it agrees with an ASCII spelling).
- **non-ASCII probe 'François Fleuret'**: surname -> 'fleuret'
- **non-ASCII probe 'Kyunghyun Cho'**: surname -> 'cho'
- **non-ASCII probe 'Ming‐Wei Chang'**: surname -> 'chang'
- **`Family, Given` vs `Given Family`: 'Vaswani, Ashish' / 'Ashish Vaswani'**: surnames 'vaswani' / 'vaswani' -> agree
- **`Family, Given` vs `Given Family`: 'Faronius, Håkan Karlsson' / 'Håkan Karlsson Faronius'**: surnames 'faronius' / 'faronius' -> agree
- **`Family, Given` vs `Given Family`: 'van der Berg, Jan' / 'Jan van der Berg'**: surnames 'vanderberg' / 'berg' -> DISAGREE
- **LaTeX title 'Sorting in $O(n\\log n)$ Time'**: normalized -> 'sorting in time'
- **LaTeX title 'A $\\mathcal{O}(1)$ Data Structure'**: normalized -> 'a data structure'
- **subtitle after a colon**: title_similarity('BERT', full) = 0.10 (main-title-only Jaccard = 1.00); a record that stored only the acronym scores far below any usable threshold against the full title.

## Reading of the numbers

- **No threshold separates right from wrong.** Precision is flat from 0.50 to 1.00, because the wrong matches have title Jaccard 1.00 — a byte-identical title. There is no knee to tune to; a stricter threshold buys nothing and costs recall. This holds for the uncorrected and the harm-corrected precision alike.
- **The uncorrected precision (~0.84) overstates the harm by counting a paper's own arXiv mirror as a false merge.** With mirrors read as the same work, precision is ~0.98 and the harmful-merge rate is 2/86. The conclusion does not depend on the larger number, and this report no longer leans on it.
- Of 14 false merges at threshold 0.80, 12 point at the *same paper* under a second OpenAlex record (chiefly the arXiv-preprint DOI `10.48550/arxiv.*`, which OpenAlex keeps separate from the published record the arXiv metadata names). Merging those still attaches the wrong source record — a different DOI, a different peer-review status — to the paper. The remaining 2 point at a genuinely distinct source: an AAAI paper vs a medRxiv posting of the same title (arXiv:2509.00891), a dataset registered under two DOIs (arXiv:2506.18120).
- In 35 of 97 papers, two or more distinct records cleared every gate simultaneously. Even when the matcher happened to pick the DOI-true record, it did so with no signal distinguishing it from an equally-scoring decoy — the successes are as blind as the failures.

**Recommendation.** No title+author+year threshold is safe to auto-merge on. The failures are not low-similarity near-misses that a higher bar would exclude; they are exact-title, same-author, same-year collisions between distinct source records, which is precisely the case #76 says no threshold saves. Title, author and year are identical across the benign duplicate and the harmful one, so nothing the matcher can see separates them. Implement the fallback as #75 specifies — surface a candidate for the human gate, never merge, and remember a rejected pairing so it is not re-proposed. The measured floor for #75's threshold constant: precision ~0.84 on published papers with clean metadata (a friendly upper-bound population), with the residual error being a category the matcher is structurally unable to resolve.

**What this spike could not measure.** (1) The real target population — fresh preprints without DOIs (#57) — has no DOI ground truth, so its false-merge rate is unmeasured, and this sample does not bound it in either direction. (2) OpenAlex title search is the candidate generator; a different generator (filtered search, fuzzy title) would change recall and the decoy pool. (3) Precision here counts a wrong DOI as a false merge; whether merging an arXiv-preprint record into its published twin is *harmful* to a specific KB depends on what was already imported — a question for #75's flow, not this measurement. (4) One day's OpenAlex budget capped the resolved sample at 97/113; the 9 papers whose DOI-true record did not surface in the title search (recall denominator 88) are a retrieval limit, not a matcher limit.
