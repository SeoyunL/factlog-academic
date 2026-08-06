# Typed (comparable-literal) relations
#
# One relation per declared type, so typed projection is exercised for all four.
# The side-relation names on the right are what policy/logic-policy.extra.dl
# compares against.
- `released_on` : date as release_date
- `headcount` : number as headcount_value
- `league_rank` : ordinal as rank_value
- `valuation` : amount as valuation_won (억=1e8, 만=1e4, 원=1)
