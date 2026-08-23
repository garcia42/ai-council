# Council usefulness preregistration amendment 004

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Timestamp: 2026-08-23T05:13:38Z
Reason: the second integrated implementation council reproduced measurement-identity collisions,
activation-boundary bypasses, an unavailable-blind incompatibility, and writer durability holes in
a tree whose 197 tests passed.
Independent reviewers: code, methodology, operations, and blind seats from
`run-459aa467e01f41f3a41d89740b50165b`.

This file appends to the preregistration and amendments 001 through 003. No V2 activation or
eligible V2 observation exists. The reviewed shared outcome remains intervention-sensitive and is
excluded from the exogenous V2 Brier headline.

## Ledger-position identity and invalid evidence isolation

The denominator unit is a distinct lifecycle event, not merely the text value of `runId`. A second
invalid initiation that reuses an existing run ID receives its own synthetic report identity and
ledger position. Only a byte-exact idempotent initiation retry is deduplicated; the report reader's
private schema-error annotation is ignored for that exact-byte comparison.

Outcome IDs retain all observed attempt issuances. A later report-invalid attempt cannot overwrite
the canonical valid run for a resolved outcome or suppress its headline predictions. Invalid
outcome reuse remains visible in the excluded stratum. A schema-invalid completion counts as a
rejected completion as well as an incomplete denominator member.

Only one V2 `capture-activation` is allowed in the mixed ledger. A differently named cohort is not
a second namespace and is rejected before append.

## Blind, numeric, and resolution boundaries

A planned blind seat receives its exact brief before launch. If that launcher later abstains or is
unavailable, `ran=false`, `role=SKIPPED`, and `blockedReason` remain required, while `brief` retains
the actual content-addressed path plus `#runId`. Only an absent, prospectively not-required blind
seat uses `no-visible-input#runId`.

V1 and V2 writer validation rejects non-finite values at any nested depth before append; canonical
serialization also sets `allow_nan=false`. The V2-only resolution path receives the same canonical
secret preflight and atomic replace/directory-fsync behavior as the main V2 lifecycle ledger.

New ledger and lock directories are created one ancestor at a time and each new directory entry is
fsynced. This applies before the first initiation so a successful append does not rely only on the
immediate parent's durability.

## Live activation is disabled in this release

Live-path identity derives from the operating-system account database, not caller-controlled
`HOME`. The sole authority host is the versioned constant and cannot be changed through an
environment variable. The installed integrity-checked wrapper passes its authenticated source
commit and live root directly into the CLI process; ordinary module invocation and environment
variables carry no activation capability.

Approval controls use exact `APPROVED` states. Evidence controls use exact `VERIFIED` states plus a
non-empty evidence reference, and known negative sentinel text is rejected. Independently of the
manifest, this release has hardcoded implementation blockers for the one-in-five audit and off-host
durability evidence. Therefore every live activation attempt fails and appends nothing. A later
source-reviewed release must remove those blockers only after implementing and rehearsing the
controls.

Run-bound artifact capture strictly validates the ledger under the shared evidence lock and
requires exactly one prior initiation for the named run. A normal secret rejection appends a safe
invalidation before releasing that lock. No claim is made that the explicit reason is crash-atomic:
a process or host crash between detection and the invalidation append leaves the already durable
initiation as an observable incomplete run, without persisting secret bytes. Closing that semantic
gap requires a prospective preflight-intent record and remains an activation follow-up.

The human capture report must display the same activation blockers as JSON. No live V2 activation
is authorized or technically possible in this release.
