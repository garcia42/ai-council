# Council activation controls amendment 027

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Timestamp: 2026-08-23T14:14:46Z
Reason: the user authorized closing the two P1 live-V2 activation blockers and pushing the inactive
implementation. This amendment freezes the evidence required before the unconditional blockers may
be replaced. No live V2 activation or eligible V2 observation exists.

This file appends to the council usefulness preregistration and amendments 001 through 026.

## Prospective one-in-five finding audit

Audit selection is a domain-separated SHA-256 of the immutable activation ID, audit-protocol
version, and decision-family ID, reduced modulo five against a frozen residue. The first observation
of every decision family records both selected and non-selected assignments before its council
attempt; retries and later runs inherit the original family assignment. The assignment mechanism
cannot inspect findings, outcomes, seat identities, or operator dispositions.

For a selected family's first capture-complete run, the system produces a content-addressed audit
case containing each substantive visible answer and its normalized captured findings. Structural
seat, role, model, and agent-version identifiers are replaced with case-local opaque aliases. A
separate alias map is never included in the adjudicator packet. If answer prose self-identifies, the
case reports anonymization risk rather than claiming perfect blinding.

Audit results bind every extracted claim to the source output digest and exact quoted-span digest.
They distinguish captured claims, omitted actionable claims, local regrouping, material/actionable
classification, and the non-scalar `confidently-wrong` flag. Any later seat gate requires two
distinct adjudicator identity digests. Classification agreement, pairwise grouping agreement, and
omitted-span overlap are reported separately; unmeasurable agreement or any applicable agreement
below 60% makes that rubric gate-void. Audit records never alter lifecycle, finding, or Brier
denominators.

Activation requires a content-addressed protocol-and-rehearsal certificate proving deterministic
selection, persisted non-selection, retry inheritance, packet blinding, omitted-claim detection,
two-adjudicator support, and gate-void behavior. Actual prospective audit counts remain zero until
selected live families exist; they may not be backfilled.

## Off-host durability certificate chain

The deployment uses a dedicated private, versioned GCS bucket and unique immutable snapshot prefix.
The policy records target, access and encryption posture, retention, RPO/RTO, snapshot-age limit,
restore-evidence age, and provider/failure-domain caveats. The initial P1 contract requires public
access prevention, uniform bucket access, versioning, an explicit retention policy, provider-managed
or stronger encryption at rest, generation-pinned object identity, and no automatic application
deletion. A separate project, locked retention, customer-managed encryption key, and narrower
project IAM are P2 hardening and do not block capture-only activation.

Under the shared evidence lock, the system creates and verifies a local snapshot. It uploads every
verified member beneath a unique prefix, records exact remote generations, uploads an index last,
then downloads those exact generations into a clean temporary tree. It verifies the reconstructed
snapshot, restores it into a second clean target, and validates the restored evidence. A successful
upload or object listing without generation-pinned readback and clean restore is not evidence.

The resulting content-addressed certificate binds the runtime commit and installed source-tree
SHA-256; frozen policy artifact; snapshot manifest digest and cut time; bucket and object
generations; provider configuration; upload/readback/restore times; byte counts; restored-state
validation; elapsed rehearsal time; issue time; and expiry. The evaluator, not a certificate's own
status label, computes freshness and RPO/RTO compliance.

## Evidence-based activation boundary

Activation manifest version 2 binds the exact runtime commit and source-tree SHA-256 plus
content-addressed audit, durability, and policy artifacts. The activation verifier dereferences and
validates every artifact; non-empty strings or caller-supplied `APPROVED`/`VERIFIED` labels are not
proof. Commit and digest must agree across the installed wrapper, activation spec, approval
manifest, audit certificate, durability certificate, and policy.

Evidence is re-evaluated while holding the coordination lock and pinned ledger transaction at the
final append boundary. Direct live activation APIs remain denied. Reports distinguish immutable
controls-at-activation from current control health and recompute freshness at their `asOf` time.
Missing, invalid, stale, mismatched, or unrehearsed evidence remains P1 and appends nothing.

The unconditional audit and durability blocker constants may be removed only after the full fake
provider suite, real GCS generation-pinned readback/clean restore, copied-live rehearsal, and exact
source-bound readiness evaluation pass. This amendment authorizes no live V2 activation.
