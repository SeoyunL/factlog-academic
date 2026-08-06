# Relation aliases
#
# Beacon's maintainer is stated as `owned_by` in facts/candidates.csv. This
# mapping is what makes the canonical rule in logic-policy.md reach it, and what
# makes check_conflicts fold the surface form into `maintained_by` instead of
# counting it as a separate relation.
- `owned_by` -> `maintained_by`
