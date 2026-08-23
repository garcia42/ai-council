# Council usefulness preregistration amendment 001

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Timestamp: 2026-08-23T03:27:00Z
Reason: the first pre-implementation council identified binding, denominator, state-machine, and
custody defects before any V2 code or eligible observation existed.
Independent reviewers: code seat, methodology seat, operations seat, and blind seat from
`run-38415675d04b410c886ae8576b8a38f0`.

This file appends to and does not rewrite
`design/2026-08-22-council-usefulness-preregistration.md`.

## Procedural correction

The first council attempt is retained and labelled
`procedurally-invalid-preimplementation-pilot`. The preregistration promised that the author
baseline's Git blob and SHA-256 would both be present in the brief and forecast attempt; that
attempt omitted the blob. It cannot authorize implementation.

A second attempt must bind, in every seat-visible brief and the forecast attempt:

- the repository commit;
- the author-baseline Git blob; and
- the author-baseline byte SHA-256.

The replacement structured baseline is
`design/2026-08-22-council-usefulness-author-baseline-v2.json`. The second council occurs only after
that artifact and this amendment are committed.

## Cohort and denominator

The capture soak is identified by one immutable `activationId` appended before its first eligible
run. The first ten eligible initiations are ordered by ledger line after that activation record.
The cohort never resets after a retry, amendment, code change, or scope reduction.

Every council initiation after activation is eligible. An initiation is any attempt record, plus
any orphan completion without an attempt. Abandoned attempts, launcher failures, timeouts,
rejected completions, old-schema attempts, and crashes remain in the denominator and are
capture-incomplete. There are no discretionary exclusions. Pure activities that do not convene a
council produce no attempt and are outside the cohort by construction.

If fewer than ten eligible initiations exist by 2026-10-31 23:59:59 America/New_York, the shared
operational outcome resolves false.

## Timing rule

The attempt records `handlingStartedAt`; its append timestamp is `seatsLaunchedAt`. The completion
records `seatsFinishedAt`; its append timestamp is `finalizedAt`.

- active handling seconds = `(seatsLaunchedAt - handlingStartedAt) +
  (finalizedAt - seatsFinishedAt)`;
- elapsed seconds = `finalizedAt - handlingStartedAt`.

There are no pause exclusions. Missing or negative intervals make the run incomplete. The primary
time bar is median active handling seconds at or below 180 over the same ten-run cohort. Elapsed
time is always reported beside it.

## Version and record dispatch

V1 validation and interpretation remain byte-for-byte unchanged. V2 uses explicit record kinds
`capture-activation`, `council-attempt-v2`, and `council-v2` with `schemaVersion: 2`. Every V2
object has an exact allowed-key set; unknown versions and keys fail closed. Old readers must exclude
V2 predictions rather than interpret them as legacy V1 forecasts.

The activation record freezes `activationId`, timestamp, cohort name, capture version, runtime
source commit, and artifact-root policy. Every V2 attempt prospectively seals:

- activation and decision-family IDs;
- the decision-before artifact reference, Git blob, and SHA-256;
- outcome class in `exogenous` or `intervention-sensitive`;
- expected seat, role (`voting`, `shadow`, `control`), and version metadata;
- evidence cutoff and handling start.

The V2 completion copies those identities and cannot redefine them. Submitted seats require Tier-1
input and output artifacts. Abstained and unavailable seats retain explicit states and do not gain
fabricated artifacts or probabilities.

## Artifact custody

Artifacts live under one configured private root outside Git. They are immutable,
content-addressed files created exclusively with no symlink following, mode 0600 under a mode-0700
root. Capture verifies path containment, bytes, length, and SHA-256 from the opened descriptor,
then fsyncs the file and parent directory before the ledger references it. Reports re-verify every
artifact; missing or changed bytes make the run incomplete. Secret detection makes the run
incomplete and raises an explicit incident state; it never silently redacts an artifact while
calling the capture exact.

The MVP captures only the exact visible prompt and final visible answer plus Tier-1 metadata.
Hidden reasoning, environment dumps, credentials, irrelevant tool output, synthetic corpus-tree
hashes, and replay claims remain prohibited.

## Finding state machine

Atomic findings, group assignment, and operator dispositions are sealed in the V2 completion.
Corrections are not in-place edits; a future typed supersession event is required and is out of
scope for the initial soak. Until that event exists, an erroneous sealed disposition remains
visible and the run is incomplete for gate use.

Each finding belongs to exactly one submitted seat and one within-run group. It has stable IDs,
category, claim, severity, proposed action, and evidence summary. Each has exactly one disposition:
`already-known`, `new-acted`, `new-rejected`, or `new-deferred`. `already-known` must reference a
consideration ID in the sealed baseline. Rejected and deferred dispositions require a reason;
deferred may also name a review date. The operator is the grouping authority under a frozen rubric;
the one-in-five audit sample is seat-anonymized and independently regrouped.

Reports call the group statistic `within-run finding overlap`, dispositions `operator-reported
novelty`, and Brier `descriptive forecast accuracy`. They never render `calibration proof`,
`redundancy`, `replaceability`, `decision value`, `marginal value`, or causal language.

## Data-health output

The MVP reports, without a composite score:

- eligible and complete initiations, completion fraction, orphan/aborted/failure counts;
- artifact completeness and integrity failures;
- median active and elapsed seconds;
- empty-finding rate, findings per submitted seat, disposition mix, and overlap groups;
- decision-family counts and agent-definition version strata;
- exogenous, intervention-sensitive, and legacy outcome counts;
- resolved exogenous outcome polarity warning above 80% either way; and
- durability snapshot age and last verified restore when supplied.

Only exogenous V2 outcomes enter headline Brier. V1, legacy, and intervention-sensitive outcomes
are structurally excluded and shown separately.

## Backup and rollout gate

The repository implements a transport-neutral, content-manifested evidence snapshot, verification,
and clean-target restore. Local targets are rehearsal-only. Capture-only activation remains
blocked until an explicit off-host target, access/encryption policy, retention, RPO/RTO, remote
readback, and clean restore rehearsal are recorded. A configured upload without readback is not a
verified backup.

Before activation, a timed end-to-end rehearsal must cover structured outputs, partial writes,
timeouts, digest mismatch, duplicate finalization, crash boundaries, aliases/traversal, secret
preflight, snapshot concurrency, clean restore, and proof that live evidence remained unchanged.

## Implementation ownership

One integration owner alone changes the V1 parser, top-level CLI, runtime installer, rehearsal,
record-kind compatibility, and shared report contract. Parallel workers own isolated V2 schema,
finding, data-health, and evidence-backup modules with their own tests. Integration occurs only
after golden mixed-ledger fixtures pass.
