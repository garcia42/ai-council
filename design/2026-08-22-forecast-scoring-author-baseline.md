# Author baseline before pre-mortem

Frozen before external review: 2026-08-22.

Expected failure stories and invariants already identified by the author:

1. Tests accidentally append resolutions to the live sidecar. All paths and clocks must be
   injectable; tests must use temporary directories.
2. A council that crashes before its final append disappears from the forecast denominator.
   A start record must exist before seats run.
3. A missing seat is accepted as a complete council. Completion validation must derive the
   expected seat set from the recorded classification and blind-seat run state.
4. A probability is added after the outcome becomes observable. Issuance must precede the
   resolution deadline and retrospective additions must be rejected.
5. Invalid JSON is skipped and the score looks healthier. All scoring inputs fail closed.
6. Pre-mortem and calibration records contaminate council scores. Record kinds are isolated.
7. Repeated claims are counted as independent evidence. Stable outcome IDs and duplicate
   diagnostics are required.
8. The implementer grades their own forecast without evidence. Resolutions require durable
   evidence, and ambiguous manual grades require independent review.
9. `void` becomes a way to erase misses. Reasons are enumerated and void rates are reported.
10. Overdue grading blocks the review mechanism itself and encourages bypass. Collection stays
    open; only decision finalization escalates after a documented debt threshold.
11. Seat aliases split one reviewer into several score rows. Canonical names are validated.
12. A zero probability is treated as missing. Scoring must distinguish zero from null.
13. Small correlated samples are presented as a leaderboard. Reports must label results
    descriptive and show paired outcome counts.
14. Runtime skill text drifts away from tested code. An installed-contract regression test must
    verify the required workflow commands and schema language.
