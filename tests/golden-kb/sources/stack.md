# Orbit stack notes

Synthetic source for the golden policy-gate KB. Every row in
`facts/candidates.csv` cites a section below, so `tools/validate.py` can resolve
each fact back to a source. Nothing here describes a real product.

## deps

Orbit depends on Beacon. Beacon depends on Ledger. Ledger depends on Vault.
The chain is three hops so a `path` query has something transitive to resolve.

## dates

Orbit was released on 2031-02-01. Beacon was released on 2030-05-14. The two
dates sit on either side of the threshold in `policy/logic-policy.extra.dl`, so
the typed comparison has both a hit and a miss.

## team

Orbit has a headcount of 120. Vault runs at a load factor of 1.0005.

## rank

Orbit holds league rank 3rd.

## funding

Orbit carries a valuation of 100억. Ledger carries a market cap of 2조 —
written without an inline unit table so it resolves through the built-in one.

## owners

Orbit is maintained by Aria. A later note says Orbit is maintained by Bran — the
two are recorded as stated, which is the contradiction `maintained_by` being
single-valued is meant to surface. Beacon is owned by Cyra, written with the
surface form `owned_by` rather than the canonical `maintained_by`.
