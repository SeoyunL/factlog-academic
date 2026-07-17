# Use cases — common workflows

> 🌐 **English** | [한국어](use-cases.md)

The uses listed in the [overview](concepts.en.md#overview) (reports, slides,
papers, code documentation, datasets, notes, wiki) unfold into real command flows
as follows. The skeleton is the same in every case — `sync`/the slash command
only produces **candidates**, a human has to confirm them with `factlog accept`
in the terminal to make them **accepted**, and only then do they back the answers
from `check`/`ask` (see the
[trust boundary](concepts.en.md#candidate-vs-accepted--the-trust-boundary)).
If you are new, start with the
[quick-start tutorial](../../examples/sample-kb/README.md) (Korean only), which
walks the whole flow through once without your own data.

**Verifying a report**

- Put the report file in the KB's `sources/`
- (run in Claude Code) `/factlog sync` to extract candidate facts
- (run in the terminal) `factlog review` to check the candidates and `factlog accept` to approve them
- (run in Claude Code) `/factlog check` or `/factlog ask` to see answers grounded in the accepted facts

**Checking the claims and evidence in slides (PPT)**

- Write the presentation material out as text and put it in the KB's `sources/`
- (run in Claude Code) `/factlog sync` to pull the slides' claims out as candidate facts
- (run in the terminal) `factlog review` to review them, and `factlog accept` to approve only the claims with clear evidence
- (run in Claude Code) `/factlog ask` to check that the approved claims do not contradict each other

**Organizing the core claims of a paper or technical document**

- Put the paper or design document in the KB's `sources/`
- (run in Claude Code) `/factlog sync` to extract the core claims as candidate facts
- (run in the terminal) `factlog review` to review them and `factlog accept` to confirm them
- (run in Claude Code) `/factlog check` to verify questions against the accepted facts

**Maintaining a wiki you already run**

- Bring the wiki documents into the KB's `sources/`
- (run in Claude Code) `/factlog sync` to refresh candidate facts from the document changes
- (run in the terminal) `factlog review` to check the new candidates and `factlog accept` to approve them
- (run in Claude Code) `/factlog check` to inspect the consistency of the accumulated accepted facts

**Tracing a fact to its source**

- (run in the terminal) `factlog search` to find the fact you want to check
- (run in the terminal) `factlog provenance` to trace which source it came from
- For details, see [Tracing a fact to its source](../reference/search-provenance.en.md#tracing-a-fact-to-its-source-factlog-provenance)

**Cleaning up badly extracted candidates**

- (run in Claude Code) `/factlog sync` to produce candidate facts
- (run in the terminal) `factlog review` to review the candidates
- (run in the terminal) retire badly extracted candidates with `factlog reject`; for candidates that only need their wording tidied, fix the value with `factlog amend` and then approve with `factlog accept` (or `factlog amend --accept`)
- For details, see [Reviewing facts](../reference/review.en.md#reviewing-facts-factlog-review--accept--reject)
