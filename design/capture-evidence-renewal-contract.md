# Capture evidence renewal acceptance contract

Frozen before implementation on 2026-09-07, base 7ef9a1783e5a3775d23a992df364c3a6d0b39009.

- Append one new capture-evidence-renewal kind to the existing ledger, with system-owned time, activation identity, predecessor manifest digest, new manifest reference, operator and approval evidence reference.
- Require installed-wrapper identity for live CLI use. Verify all evidence within the shared evidence lock and pinned ledger transaction before append.
- Preserve original activation bytes, cohort, assignments, denominators and forecasts. Renewal source must equal the activation source. Source migration is outside this contract.
- Permit only policy ID/issuance/expiry changes; audit protocol and durability policy references and all control parameters must remain identical. Manifest and policy validity durations may not exceed the original durations.
- Preserve historical activation verdict. Current health follows the complete validated renewal chain in ledger order; missing, malformed, conflicting, stale-at-issuance, substituted or future evidence cannot fall back to older healthy evidence.
- Invalid append attempts write no renewal. Successfully appended evidence that later becomes stale remains visible and unhealthy.
- Verify schema, unchanged denominators, independent artifact re-read, unauthorized CLI rejection, two-renewal sequencing, duplicate/predecessor refusal, snapshot/restore and lock contention; run full tests and exact-commit rehearsal.
- Document explicit artifact routing, renewal-before-expiry operation, and rollback compatibility: old readers cannot consume the new record kind once it is live.

Clarification from integration evidence: the existing snapshot implementation pins policy identities
before taking the shared lock. A concurrent atomic ledger replacement may therefore produce an
explicit `source-changed` refusal with no published snapshot. Preserve that refusal; a new snapshot
attempt after the completed renewal must restore a complete after-state. Do not weaken identity
checks to make the racing attempt succeed. Forecast denominators remain unchanged even though
the existing atomic writer may retain new transaction escrow metadata.

Scope: capture_schema.py, evidence_renewal.py (new pure evaluator), capture_runtime.py, cli.py, data_health.py, relevant tests, rehearsal/runtime compatibility allowlists where required, README and this contract. No provider calls, scheduler installation, live renewal or capture activation in implementation qualification.
