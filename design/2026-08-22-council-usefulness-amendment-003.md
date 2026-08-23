# Council usefulness preregistration amendment 003

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Timestamp: 2026-08-23T04:47:04Z
Reason: the first integrated implementation council found contradictions that component tests did
not expose: strict report validation erased preregistered failures, invalid evidence could enter
headline summaries, the Git-blob and blind-seat bindings were incomplete, and activation controls
were advisory.
Independent reviewers: code, methodology, operations, and blind seats from
`run-cd571c3dfb5845ad954f526343c60d31`.

This file appends to the preregistration and amendments 001 and 002. It does not reinterpret V1,
change the first-ten denominator, or claim that prospective capture has begun. The reviewed shared
outcome was intervention-sensitive because its council findings governed repair and push; it is
not eligible for the exogenous V2 Brier headline.

## Strict writes and denominator-preserving reports

Writer paths retain exact schema and lifecycle validation and refuse invalid rows. Report ingestion
also rejects malformed JSON, duplicate keys, non-finite numbers, unknown schema versions, unknown
V2 kinds, invalid activation rows, and invalid lifecycle rows without an identifiable run ID.

A known V2 lifecycle row with an identifiable run ID that fails schema, binding, or future-boundary
validation is instead typed as report-invalid. Its bytes remain untouched, its run keeps its frozen
ledger position, its duration is positive infinity, and it is capture-incomplete. Report-time
classification never makes such a row valid for a later write.

## One analysis eligibility state

Each run receives one integrity-aware analysis eligibility state after structural, timing,
invalidation, and report-time artifact checks. The same state governs capture completeness,
headline finding summaries, and descriptive Brier scoring. Excluded runs remain visible in a
separate invalid/excluded stratum; they are never silently dropped or allowed to contaminate a
headline.

The pooled mean Brier and its hindsight base-rate bound both use prediction rows as their weighting
unit. If outcomes have unequal numbers of submitted predictions, each observed outcome is repeated
once per scored prediction for the comparator. Outcome polarity remains a separate outcome-weighted
diagnostic. The hindsight value remains descriptive and unavailable ex ante.

## Exact custody and blind identity

The decision-before bytes must authenticate both their SHA-256 artifact reference and their claimed
Git object ID, computed over `blob <byte-count>\0<bytes>` using the repository object format.

`blindSeat` is reconciled with the canonical `blind` seat in the plan and terminal results. `ran`
is true exactly when that seat submitted. A non-run blind seat uses the installed tally's `SKIPPED`
role and a non-empty blocked reason. Its brief identity is run-scoped as
`<content-addressed-input-path>#<runId>`, so identical prompt bytes reused in a later run do not
masquerade as the same blind invocation.

Canonical ledger rows and every append-time artifact byte receive secret preflight. If rejection
occurs after initiation, the runtime appends a non-secret `secret-detected` invalidation for that
run. Prospective artifact capture is run-bound and incident-capable; pre-run control artifacts
require an explicit control-artifact mode.

V2 appends are staged in a same-directory private temporary file, fsynced, atomically replaced, and
followed by a parent-directory fsync. Snapshot sources have exact file/directory types. Restore is
staged and rehashed before atomic publication and refuses a target within the supplied Git root.

## Machine-enforced activation and remaining blockers

Live `capture-activate` must run through the installed source-pinned wrapper. Its runtime commit
must match the activation spec. The spec must bind a verified content-addressed approval manifest
with non-empty approval and evidence references for off-host custody, encryption/access, retention,
RPO/RTO, remote readback, clean restore, timed rehearsal, and the independent audit protocol.

The deterministic one-in-five independent regrouping and omitted-claim audit is not implemented.
No durability snapshot age or last verified off-host restore has been supplied. Reports must expose
both states explicitly as activation-blocking; documentation must not describe the full usefulness
measurement system as operational while either is absent. A later append-only amendment may freeze
the audit assignment and durability evidence format before activation, but may not backfill the
first-ten cohort.

No live V2 activation is authorized by this amendment.
