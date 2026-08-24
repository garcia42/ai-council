# AI Council ticket sizing contract v1

This is the exact output contract for deciding whether proposed work is one
independently shippable implementation ticket of no more than three human
engineer-days, including tests and review.

The required v1 seats are exactly `claude` followed by `codex`. A different
seat set or order requires a new schema version. A caller cannot omit a seat or
choose a smaller approving subset.

## Required record

Return one JSON object with these exact top-level keys:

```json
{
  "schemaVersion": 1,
  "runId": "review-run-id",
  "contractSha256": "lowercase-64-hex-ticket-contract-digest",
  "requiredSeats": ["claude", "codex"],
  "seatReviews": [
    {
      "seatId": "claude",
      "status": "submitted",
      "engineerDays": 2,
      "singleOutcome": true,
      "splitReasons": [],
      "priority": "P1",
      "confidence": 80
    },
    {
      "seatId": "codex",
      "status": "unavailable",
      "reason": "The required seat could not complete the review."
    }
  ]
}
```

Do not add Markdown fences when a machine requests raw JSON.

## Submitted seat review

A submitted seat must provide exactly:

- `seatId`: its assigned exact seat ID.
- `status`: `submitted`.
- `engineerDays`: an integer from 1 through 30. Include implementation,
  testing, review fixes, documentation, and integration work.
- `singleOutcome`: `true` only when the proposed change is one independently
  shippable outcome.
- `splitReasons`: empty exactly when `singleOutcome` is true; otherwise one or
  more unique, concrete reasons identifying independent outcomes.
- `priority`: exactly `P0` or `P1` under the rubric below.
- `confidence`: an integer from 0 through 100 expressing confidence in this
  sizing and decomposition judgment.

`confidence` is self-reported review confidence. It is not a forecast
probability and must never enter Brier scoring, forecast-ledger denominators,
or calibration statistics.

## Unavailable seat review

An unavailable seat provides exactly `seatId`, `status: unavailable`, and a
nonblank canonical `reason`. Do not copy a favorable estimate, priority, or
confidence into this variant.

An abstention is represented as `unavailable`, with the reason stating that the
seat abstained. V1 does not define a third `abstained` status. An unavailable or
abstaining required seat always prevents eligibility; absence never counts as
approval.

## Priority rubric

Use `P0` for an active safety, security, or integrity failure; risk of
irreversible loss; or an enforcement bypass that blocks safe activation.

Use `P1` for a required bounded governance capability or correctness gap that
does not meet the P0 impact bar. No P2 or lower implementation priority exists
in this policy.

When submitted seats disagree, P0 wins. This permits one seat to escalate a
live integrity concern conservatively.

## Deterministic decision

The validator derives the decision; seats never submit it.

- `eligible` requires both seats submitted, every estimate at most three days,
  and every `singleOutcome` true.
- Any unavailable seat, estimate over three, or multiple-outcome judgment
  derives `needs-split`.
- Eligible points are the maximum submitted estimate and can only be 1, 2, or
  3. A needs-split decision has no points.
- If any seat is unavailable, derived priority and confidence are absent.
  Otherwise priority is P0 if either seat submitted P0, and confidence is the
  minimum submitted confidence.
- Reasons are fixed codes ordered by seat and rule. Free-form reason text is
  retained in the input record but never reflected in validator errors.

Downstream label policy maps `eligible` to the derived `size:N` and permits a
later admission control to assign `agent:ready`. It maps `needs-split` to the
`needs-split` label, no size label, and either no agent state or
`agent:blocked`. This module does not mutate labels.

## Integrity is not authorization

`reviewSha256` is an unauthenticated content address computed only after the
record validates. It detects changes to the canonical reviewed content. It is
not a cryptographic seal, approval, signature, or proof that either named seat
ran. One process can construct both seat reviews and recompute the digest.

Protected review evidence and authorization are separate downstream controls.
Until those controls verify the named run and its custody, a structurally valid
record must not authorize implementation, GitHub mutation, or activation.
