# Open Questions

This file tracks decisions about candidate facts that require human review.

## 중복 (Duplicate Review)

No duplicate facts detected. `Claude Code,developed_by,Anthropic` and
`Anthropic,develops,Claude Code` express the same relationship in both
directions — the reverse direction row is marked `needs_review` pending
a decision on whether bidirectional facts should be retained.

## 모호 (Ambiguity Review)

- The term "Claude model family" in the `uses` relation is broad; future sources
  should clarify which specific model versions are referenced.

## 출처 (Source Review)

All confirmed facts link to sections within `sources/example.md`. The source
document is a summary; primary Anthropic documentation should be added when
available.

## 충돌 (Conflict Review)

No conflicts detected in the current fact set. The `developed_by` and `develops`
relations are complementary, not conflicting.

## Pending decisions

- `Anthropic,develops,Claude Code` — marked `needs_review`; decide whether to
  keep bidirectional facts or canonicalise to `developed_by` only.
