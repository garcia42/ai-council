# Council usefulness measurement preregistration

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Date: 2026-08-22 America/New_York
Decision family: `family-ai-council-usefulness-v2`

## Purpose

Measure whether the existing four-seat council produces complete, auditable evidence that can
later support claims about correctness, novelty, redundancy, harm, and marginal value. This MVP is
capture-only. It must not rank, weight, admit, or retire seats.

The sealed author baseline is
`design/2026-08-22-council-usefulness-author-baseline.json`. Its repository blob and SHA-256 are
bound into the pre-implementation council brief and forecast attempt before any reviewer sees the
design.

## Units that must remain distinct

- **Run:** one sealed set of expected seat executions.
- **Outcome:** one binary forecast target with a stable identity.
- **Finding:** one independently attributable, actionable claim from one seat.
- **Finding group:** findings judged to raise the same actionable concern within one sealed run.
- **Decision family:** the root decision plus its retries, implementation iterations, release
  reviews, and direct remediation until a final disposition.

Runs are in the same decision family when they can change the same root action or repair a defect
in that action. A new commit, date, retry, or wording change does not create a new family. A new
family requires a different action that could be accepted or rejected independently. The family ID
is recorded before seats run. Corrections require an append-only, outcome-blind amendment reviewed
by someone other than the original assigner.

No report may collapse forecast calibration, finding support, novelty, decision disposition,
redundancy, latency, cost, or completeness into one score.

## Primary operational estimand

The first rollout estimates the fraction of eligible council runs that are capture-complete.
A run is complete only when it has:

1. a verified structured decision-before artifact sealed before any seat starts;
2. a Tier-1 visible-input and visible-output artifact for every submitted seat;
3. an explicit execution state for every expected seat;
4. atomic findings and exactly one operator disposition for every finding;
5. a pre-assigned decision family and outcome class; and
6. no invalid, missing, tampered, or reconstructed prospective record.

The capture soak passes only at at least 90% completeness and no more than three minutes of human
handling time per run. Below either bar, reduce scope and repeat the soak. Do not interpret seat
performance.

## Tier-1 capture contract

Capture auditability, not replayability:

- exact visible prompt and final visible answer references, byte counts, and SHA-256 digests;
- model ID, agent-definition digest, tool policy, evidence cutoff, repository commit, and diff
  digest where applicable;
- seat role in `voting`, `shadow`, or `control`; and
- execution state, latency, token counts, and cost only when the launcher supplies them.

Never capture hidden reasoning, credentials, environment dumps, or irrelevant raw tool output.
An actual tool-read manifest is optional and valid only when emitted by the harness. A corpus-tree
hash must not be substituted for what the seat actually read. Raw artifacts remain private and
outside the Git repository; the ledger carries verified references and digests.

## Findings and dispositions

Every finding has a stable ID, seat, category, claim, severity, proposed action, evidence summary,
and finding-group ID. The operator must record exactly one:

- `already-known`;
- `new-acted`;
- `new-rejected`, with a reason; or
- `new-deferred`, with a reason or review date.

Within-run co-occurrence is the primary redundancy diagnostic: a finding group is duplicated when
two or more independently sealed seats contribute a finding. It measures overlapping coverage,
not correlated forecast error. Leave-one-seat-out output is labelled an upper bound on unique
finding coverage, never a causal decision-value estimate.

## Outcomes and forecast reporting

Every new outcome is classified before issuance as `exogenous` or `intervention-sensitive`.
Only exogenous outcomes enter headline Brier summaries. Intervention-sensitive outcomes remain
visible in a separate, explicitly non-comparable stratum. Legacy rows without a prospective class
remain legacy-incomplete and cannot be silently promoted.

Reports retain the constant-50% reference and label the in-sample base-rate score as a hindsight
bound. Resolved-outcome polarity above 80% in either direction is a data-health warning.

Matched error correlations, effective seat count, and seat leaderboards are not MVP outputs.

## Adjudication

Universal per-finding adjudication is deferred. Sample one in five eligible decision families for
independent, seat-anonymized re-scoring. Anything later used for a seat gate requires two
adjudicators. Report their agreement; agreement below 60% voids gates based on that rubric until it
is repaired. `confidently-wrong` is a review flag, not a scalar performance score.

## Statistical and governance gates

- No seat comparison verdict below 20 capture-complete independent decision families.
- Any later verdict restates family N, tie rate, missingness, and minimum detectable effect, or says
  `NO VERDICT`.
- Paired non-tied comparisons use the preselected sign test with `p < 0.1`; this rule may be
  amended only before seat identities or outcomes are inspected.
- Control-arm assignment is frozen only after the capture soak. Candidate seats remain absent,
  including shadow seats, until finding capture is live.
- Agent-definition changes create a new version stratum. Findings per run per seat are monitored
  for drift and Goodhart behavior.
- Preregistration amendments are new append-only files with timestamp, outcome-blind reason, author,
  and independent reviewer. This file is never rewritten after prospective V2 data begins.

## Kill criteria

Retire or radically simplify the whole council apparatus if, after 20 capture-complete independent
families and at least four months:

- it has produced zero independently supported unique material findings that were acted on and
  zero correct blockers missed by the sealed author baseline; or
- capture completeness remains below 90% or median human handling time remains above three minutes
  after one scope-reduction cycle.

This criterion does not replace the existing blind-seat kill criterion.

## Implementation slices

1. Additive schema and artifact verification, including outcome class and seat role.
2. Atomic findings, forced dispositions, and deterministic co-occurrence summaries.
3. Capture-only data-health reports with exogenous Brier separation.
4. Evidence snapshot, manifest verification, and restore rehearsal to an explicit external target.
5. Runtime integration, copied-live rehearsal, full review, and only then capture-only activation.

Each slice owns its tests. Integration must preserve append-only writes, legacy classification,
strict parsing, concurrency behavior, source pinning, and live-data isolation.
