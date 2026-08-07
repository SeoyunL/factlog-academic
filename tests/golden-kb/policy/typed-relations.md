# Typed (comparable-literal) relations
#
# One relation per declared type, so typed projection is exercised for all four,
# plus a second amount relation. The side-relation names on the right are what
# policy/logic-policy.extra.dl compares against.
#
# valuation carries an inline unit table; market_cap deliberately does not, so it
# resolves through literal_types.DEFAULT_AMOUNT_UNITS. With only the inline form
# declared, the default table was never read by this KB and could be corrupted
# outright while the run stayed green.
- `released_on` : date as release_date
- `headcount` : number as headcount_value
- `league_rank` : ordinal as rank_value
- `valuation` : amount as valuation_won (억=1e8, 만=1e4, 원=1)
- `market_cap` : amount as market_cap_won
- `load_factor` : number as load_factor_value
