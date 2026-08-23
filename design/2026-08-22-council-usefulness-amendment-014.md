# Council usefulness preregistration amendment 014

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Timestamp: 2026-08-23T09:49:53Z
Reason: the twelfth integrated implementation council reproduced cross-lifecycle finding-summary
inheritance and pre-schema secret disclosure in an exact tree with 364 passing tests and one
intentional rehearsal-only skip.
Independent review run ID: `run-4164a62cecbc455694d6dcbab79eae7b`.

This file appends to the preregistration and amendments 001 through 013. No V2 activation or
eligible V2 observation exists.

## Physical lifecycle identity for finding summaries

Report-time finding counts, grouping, and dispositions bind to the physical or synthetic lifecycle
state produced from a particular ledger record, not merely to its public `runId`. If an invalid or
excluded duplicate reuses a valid run ID, it remains its own denominator row and cannot inherit the
valid lifecycle's findings or post-seat summaries. The invalid duplicate contributes only its own
safe, explicitly projected values. Integrated tests cover a valid completion followed by a
schema-invalid duplicate completion with the same public run ID.

## Secret preflight before value-bearing schema errors

Raw request payloads are serialized and scanned with the fixed secret policy before schema
construction can interpolate caller-controlled identifiers or values into an exception. Schema
errors for duplicate or invalid identifiers are generic and never echo the identifier. Where an
initiated run can be identified safely, rejection appends a fixed, secret-free invalidation before
the transaction exits; otherwise it fails closed without persisting the raw input. CLI stdout,
stderr, ledgers, artifacts, and reports never contain the detected secret. Tests include a
secret-shaped duplicate seat ID and assert both generic output and safe ledger behavior.

Live V2 activation remains disabled by the independent-audit and off-host-durability blockers.
