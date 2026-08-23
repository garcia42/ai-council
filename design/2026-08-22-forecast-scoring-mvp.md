# Council forecast scoring MVP

Status: IMPLEMENTED, SHADOW REHEARSAL AND ACTIVATION REVIEW PENDING
Date: 2026-08-22

## Objective

Make council forecasts mandatory, identifiable, evidence-resolvable, and Brier-scored
without rewriting the existing append-only panel ledger. The first release measures
forecast discipline and descriptive calibration. It does not rank reviewer intelligence.

## Current evidence

- The active council skill logs verdicts and blind-seat state but does not require forecasts.
- The shared ledger contains 29 predictions across several record kinds and no resolutions.
- Eleven calibration predictions lack usable dates and resolution procedures.
- The current scorer silently skips malformed JSON, keys resolutions by timestamp and array
  index, pools record kinds, and treats a zero probability as 50 percent when scoring.
- Repeated forecasts in adjacent council rows can be counted twice as if they were independent.

## MVP contract

1. A council invocation first appends a `council-attempt` record with a stable `runId`.
2. A completed `council` row references that `runId`.
3. Before seats run, the operator defines one neutral, material binary outcome. Every seated
   reviewer prices the byte-identical shared claim under one `outcomeId`.
4. Each prediction has a stable `predictionId`, canonical seat, probability, issuance time,
   resolution deadline, and concrete resolution procedure.
5. Probabilities from 0 through 100, including 50, are valid. Materiality is enforced by the
   shared outcome rather than by forcing a directional probability.
6. Resolutions are append-only sidecar records with `true`, `false`, or an enumerated `void`,
   evidence, resolver, and resolution time. Corrections reference the prior resolution.
7. Council reporting excludes other record kinds by default. Repeated forecasts remain in the
   audit history but do not silently inflate headline seat/outcome counts.
8. Ordinary grading debt never prevents a new council from convening. Three or more forecasts
   more than 14 days overdue block decision finalization unless a logged override exists.
9. All early scores are descriptive. Any future comparison must use paired shared outcomes and
   state that normal seat operation includes unequal information access and correlated models.
10. The attempt row contains no seat prices. A completion record seals the full required set;
    retries and unavailable seats are explicit and cannot silently replace an earlier issuance.
    Exact-outcome retries link the prior `outcomeId` through the attempt CLI.
11. Every outcome names its decision link, materiality, and the action implied by true and false.
12. Reports expose resolution completeness and label scores incomplete when due outcomes remain
    selectively unresolved.

## Runtime consumers

- Council operator: must be able to start a run, validate a completion row, and see debt.
- Decision finalizer: must receive a clear clean/warn/block grading-debt state.
- Resolver: must record evidence without changing the original ledger.
- Reporter: must show emission coverage, due debt, voids, unique outcomes, and descriptive
  per-seat Brier scores.
- Blind-seat kill criterion: must ignore `council-attempt` as a non-seat record while continuing
  to fail closed on unknown kinds.

## Out of scope for the MVP

- A public seat leaderboard or claim that Brier differences identify reasoning quality.
- Formal power calculations or calibration plots.
- Scheduled paging, remote deployment, or a public dashboard. Report exit codes are mandatory
  process-boundary alerts before seats run and after completion is appended.
- Reconstructing probabilities or dates that were not recorded contemporaneously.
- Rewriting any existing ledger line.

## Acceptance gates

- Tests use temporary ledgers and cannot write runtime files.
- Malformed JSON, unknown seats, duplicate IDs, invalid dates, and invalid resolutions fail.
- A true outcome forecast at zero percent scores exactly 1.0; a false outcome at zero scores 0.0.
- Non-council predictions never enter the default council score.
- Every current forecast is classified as future, due, resolved, void, or legacy-ineligible.
- A copied-ledger rehearsal leaves the blind-seat tally unchanged at its pre-install baseline.
- A copied-runtime write command refuses imported-source drift before appending.
- Rollback preserves the forward-compatible `council-attempt` blind-tally allowlist.
- The installed council skill contains attempt, shared-forecast, validation, and debt steps.
