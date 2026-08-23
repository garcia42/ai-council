# Council usefulness preregistration amendment 002

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Timestamp: 2026-08-23T03:36:00Z
Reason: the second pre-implementation council found a non-uniform prompt binding, a pre-ledger
failure hole, missing invalidation state, ambiguous missing-time behavior, and vacuous empty-finding
capture. No V2 code or eligible observation exists.
Independent reviewers: code, methodology, operations, and blind seats from
`run-64c1b3a99fab449e9519803ad92bf3d0`.

This file appends to the original preregistration and amendment 001. Neither earlier artifact is
rewritten. Attempt two remains visible as a procedurally invalid pilot because only the operations
and blind seat prompts literally contained all three baseline bindings.

## Durable initiation and retry identity

`capture-initiation` is the first V2 write, before brief preparation. It is appended under the
ledger lock with system-generated timestamp, stable `runId`, `initiationId`, `activationId`, and an
operator-supplied idempotency key. The unique pair `(activationId, idempotencyKey)` means concurrent
or repeated execution of the same initiate command returns the existing initiation and cannot take
another cohort position.

`council-attempt-v2` must reference exactly one prior initiation and is unique per run. A crash
after initiation but before attempt remains an eligible incomplete run. A process may resume that
same run only until its attempt exists. After an attempt exists, abandoning and reconvening the
whole council requires a new idempotency key and new run; it consumes a new cohort position but
keeps the same decision-family ID. Seat-launcher retries inside one still-open run do not create a
new initiation and must be recorded in that seat's execution metadata.

An orphan completion without initiation is separately invalid and counts as one incomplete
initiation at its own ledger position. The first-ten report deduplicates only exact idempotent
initiation retries, never distinct run IDs.

## System-generated boundary time

The initiation append timestamp is `handlingStartedAt`. The attempt append timestamp is
`seatsLaunchedAt`. After the last expected seat reaches `submitted`, `abstained`, or `unavailable`,
the harness invokes a `council-seats-finished` command whose append timestamp is `seatsFinishedAt`.
The V2 completion append timestamp is `finalizedAt`. Live commands generate all four timestamps
inside the ledger lock and reject injected values.

Required ordering is:

`handlingStartedAt <= seatsLaunchedAt <= seatsFinishedAt <= finalizedAt`.

Active and elapsed formulas remain those in amendment 001. Any missing, future, reversed, or
otherwise invalid boundary makes the run incomplete and makes the cohort time gate false. The
report also represents its duration as positive infinity, reports `validDurationCount`, sorts the
ten values, and defines the even-sized median as the arithmetic mean of one-indexed positions five
and six. No available-case median may replace the frozen cohort.

## Minimal append-only invalidation

The MVP adds `capture-invalidation` with exact keys: stable event ID, run ID, enumerated reason,
operator, system-generated timestamp, and non-secret evidence reference. Duplicate event IDs fail;
multiple distinct invalidations may accumulate. Any valid invalidation permanently makes the run
capture-incomplete. This event marks a bad capture but does not rewrite or correct a finding.

Initial reasons are `artifact-compromised`, `disposition-error`, `identity-error`,
`secret-detected`, and `timing-invalid`. Full finding supersession remains deferred.

## Empty findings and baseline linkage

Every submitted seat must either contribute at least one atomic finding or include a structured
`no-findings` declaration that was part of the seat's exact visible output artifact. Operator-only
empty arrays are incomplete. The one-in-five anonymized audit sample compares the visible answer
against normalized findings and reports omitted actionable claims.

An `already-known` disposition includes both a valid consideration ID and an exact quoted subclaim
from the sealed baseline artifact. Reports continue to label it operator-reported novelty.

## Conservative outcome classification

Every V2 attempt includes a non-empty prospective outcome-class rationale. If any plausible direct
or indirect pathway exists from council output or the reviewed decision to the outcome, it must be
`intervention-sensitive`. The schema can enforce the enum and rationale; the policy is checked in
review and sampled audit.

## Uniform prompt-binding preflight

The third council may authorize code only if every seat-visible prompt literally contains the same
repository commit, author-baseline Git blob, and author-baseline SHA-256, and the forecast attempt's
stored `decisionLink` contains the same values. A preflight check compares those byte strings before
any seat is launched. Looking them up after receipt is not compliance.

## Added acceptance conditions

- Fault injection after initiation, attempt, every artifact/fsync boundary, seats-finished, and
  completion always leaves either a complete run or an observable incomplete cohort member.
- Disk-full, timeout, secret detection, duplicate/concurrent initiation, duplicate finalization,
  and restore reconstruct the identical first-ten cohort.
- A submitted empty finding set without a seat-originated declaration is incomplete.
- An invalidation is append-only, idempotence-checked, and permanently removes completeness.
- Secret detection persists no secret bytes to artifact, ledger, stderr, quarantine, or backup.
- A snapshot taken concurrently with append represents one coherent cut before or after the append.

The off-host restore and timed multi-run rehearsal remain activation gates, not implementation
gates.
