# Council usefulness preregistration amendment 015

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Timestamp: 2026-08-23T10:06:04Z
Reason: the amendment-014 adversarial integration audit reproduced an unpreflighted V2 resolution
identifier disclosure and reverse-order lifecycle-state capture after 372 tests and the copied-live
rehearsal passed.
Independent preflight audit: `amendment14_adversary`.

This file appends to the preregistration and amendments 001 through 014. No V2 activation or
eligible V2 observation exists.

## Resolution arguments enter the same secret boundary

The complete raw V2 resolution argument bundle is serialized and secret-scanned before outcome
lookup, cross-record validation, value-bearing errors, or sidecar construction. An unknown or
otherwise invalid secret-shaped outcome ID is never reflected through the runtime or CLI and
appends no sidecar bytes. Runtime and subprocess CLI regressions cover the pre-issuance lookup
failure as well as secrets in later resolution fields.

## Invalid rows cannot consume a clean future lineage

An invalid or excluded lifecycle boundary is retained as its own physical denominator event but
does not consume the clean incomplete lifecycle state that a later schema-valid boundary must
complete. This rule is order-invariant: both valid-then-invalid and invalid-then-valid duplicate
completion orderings preserve the valid completion's lineage and keep the malformed occurrence in
its own excluded state. Integrated tests assert cohort membership, completion counts, headline and
excluded finding summaries, dispositions, and grouping for the reverse ordering.

## Report readers reject secret-bearing ledger bytes

Report-time tolerant schema handling does not make secret-bearing raw evidence printable. Before
decoded rows, invalid-record diagnostics, or cohort projections can reach a report, the exact raw
JSONL bytes are scanned with the same fixed secret policy. A compromised or old writer's malformed
record containing a secret-shaped identifier makes the report fail with one fixed non-reflective
error and no JSON stdout; it is not copied into invalid-row or cohort output. Runtime and subprocess
CLI regressions cover an identifiable malformed V2 completion in an otherwise valid ledger.

Live V2 activation remains disabled by the independent-audit and off-host-durability blockers.
