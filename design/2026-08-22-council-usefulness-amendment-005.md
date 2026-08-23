# Council usefulness preregistration amendment 005

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Timestamp: 2026-08-23T05:29:04Z
Reason: the third integrated implementation council reproduced raw-event identity, outcome-stratum,
blind-binding, finalization-boundary, lock-scope, rehearsal-isolation, install-containment, host-
authority, and crash-durability defects in a tree whose 211 tests passed.
Independent reviewers: code, methodology, operations, and blind seats from
`run-8e96a60ce9d146868c5cc2037a978c00`.

This file appends to the preregistration and amendments 001 through 004. No V2 activation or
eligible V2 observation exists. The reviewed shared outcome remains intervention-sensitive and is
excluded from the exogenous V2 Brier headline.

## Exact ledger-event identity and outcome strata

The tolerant report reader assigns every non-blank physical JSONL record a private identity derived
from its exact durable bytes. Idempotent retry deduplication requires that identity to match. Equal
decoded mappings, reused run IDs, repeated V1 attempts, and repeated orphan completions are not
sufficient evidence of a retry and therefore retain separate denominator positions. Report-only
annotations never change the durable bytes used for identity.

Headline V2 outcome counts, resolution-class counts, polarity diagnostics, and Brier scores use
only structurally valid, capture-eligible issuances. Every invalid or excluded issuance remains
visible in a separate outcome stratum whether it is unresolved, resolved, or reuses an otherwise
valid outcome ID. An invalid issuance cannot create, replace, reclassify, or suppress a headline
outcome.

## Exact blind binding and completion boundary

Every planned blind seat retains the exact run-bound blind input artifact identity regardless of
whether its terminal state is submitted, abstained, or unavailable. A different canonical-looking
path, including a content-addressed path for unrelated bytes, is invalid.

`finalizedAt` is a system-owned completion boundary. Artifact re-verification, visible-output and
finding normalization, and all other fallible precommit work occur first. The clock is sampled as
late as possible immediately before the strict append while the evidence coordination lock remains
held. Operator payloads cannot supply or override this timestamp.

On a normally handled secret rejection, the safe invalidation append occurs before the evidence
coordination lock is released. Amendment 004's narrower crash qualification remains: a process or
host crash before that append can leave an observable incomplete initiation, but secret bytes are
never persisted.

## Rehearsal, authority, containment, and durability

Rehearsal commands use only rehearsal-local ledgers, artifacts, control stores, resolution stores,
and coordination locks. The rehearsal neither creates nor opens the live coordination lock, and
its unchanged-live-state assertion covers every live path its embedded commands could otherwise
touch.

Fixed-host authority applies to every resolved write target within the live council knowledge or
runtime state, including non-default artifact subtrees, snapshots, and restore destinations.
Lexical aliases and symlinked ancestors do not weaken the boundary. Temporary roots outside live
state remain usable for isolated tests and rehearsals.

Before installation reads or replaces a target, the resolved target and every relevant ancestor
must remain contained beneath the resolved installation root. Symlink escape is a hard failure
before mutation. Backup payloads and their directory entries are fsynced before replacement;
successful replacements and their parent-directory entries are fsynced before success is
reported. Transactional rollback remains required on ordinary failures.

Live V2 activation remains disabled by the independent-audit and off-host-durability blockers.
