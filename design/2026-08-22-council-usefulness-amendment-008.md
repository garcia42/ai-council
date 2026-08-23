# Council usefulness preregistration amendment 008

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Timestamp: 2026-08-23T07:05:33Z
Reason: the sixth integrated implementation council reproduced outcome-provenance, report-time
reverification, resolution-time, excluded-count, lock-identity, and parser-redaction defects in a
tree with 268 collected tests. Three seats blocked and the operations seat approved.
Independent reviewers: code, methodology, operations, and blind seats from
`run-bfd795a42d854697b758a3f665031773`.

This file appends to the preregistration and amendments 001 through 007. No V2 activation or
eligible V2 observation exists. The reviewed shared outcome remains intervention-sensitive and is
excluded from the exogenous V2 Brier headline.

## Exact forecast-request and response identity

Every planned seat's visible input contains a canonical forecast-request binding derived from the
run ID, outcome ID, outcome fingerprint, and evidence cutoff. The input-manifest digest continues
to bind the exact prompt bytes. An input lacking the canonical request identity is rejected before
launch evidence can be sealed.

Every submitted seat's visible output capture declaration exact-binds the same request identity,
the seat's input-artifact SHA-256, its seat ID, and its shared probability. A retained output cannot
be reused for another run, outcome, cutoff, or prompt merely because the numeric probability is the
same.

Report-time custody re-reads verified retained inputs and outputs and re-derives those bindings and
probabilities. Ledger values that disagree with retained evidence make the issuance ineligible and
cannot change headline Brier. Writer-time validation alone is not report-time provenance.

## Resolution identity and observation time

New resolution events bind the exact prospective outcome fingerprint as well as outcome ID and
resolution date. V1 audit and V2 capture reporting cross-check those fields against the issued
outcome. V2 requires the fingerprint; older V1 events without one remain explicitly legacy-
compatible but cannot establish a V2 grade.

A resolution timestamp must follow the sealed forecast issuance and must not follow the report's
observation time. Future-dated events and pre-issuance grades fail closed instead of being scored.
CLI resolution timestamps are system-owned; operators cannot use `--resolved-at` to time-travel.
Internal timestamp injection remains available only for isolated deterministic tests.

Excluded outcome reporting distinguishes issuance rows from unique outcomes.
`resolvedIssuanceCount` counts excluded issuances; `resolvedOutcomeCount` counts unique excluded
outcome IDs.

## Lock-to-mutation identity and parser redaction

One pinned parent-directory identity spans ledger-lock acquisition, ledger validation, and ledger
mutation. A directory rename or replacement after lock acquisition cannot cause the lock to protect
one directory while the append reaches another. V1 and V2 append and repair paths share this
transaction primitive while retaining their fsync and concurrency contracts.

Strict JSON/spec parse failures are generic at the CLI boundary and never echo caller bytes,
including a secret-shaped duplicate key. Recognized incident invalidation and parse failure both
preserve non-disclosure, though malformed input that never identifies a valid run cannot create a
run-scoped invalidation.

The MVP collects evidence needed to study correlated or redundant seats; it does not yet estimate
error correlation, statistical independence, or replaceability. Live V2 activation remains
disabled by the independent-audit and off-host-durability blockers.
