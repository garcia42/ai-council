# AI Council ticket sizing contract v1

This is the exact output contract for deciding whether proposed work is one
independently shippable implementation ticket of no more than three human
engineer-days, including tests and review.

The required v1 seats are exactly `claude` followed by `codex`. A different
seat set or order requires a new schema version. A caller cannot omit a seat or
choose a smaller approving subset.

## What the seats are shown

Sizing seats are shown the **sizing projection** of the ticket contract, never the raw
contract. The projection is the contract with the review-derived fields removed:

- `points` — derived from the submitted estimates.
- `priority` — derived from the submitted priorities.

A seat determines those two values, so showing a seat a proposed value for them invites
anchor-and-adjust drift. `council_tools.ticket_contracts.sizing_projection` computes the
projection and `sizing_projection_sha256` digests it. That digest goes in the review
record's `sizingProjectionSha256`, which is what binds a review to the content its seats
saw.

The classification is not inferred. `SIZING_PROJECTION_KEYS` and `SIZING_DERIVED_KEYS` are
declared independently and their partition is checked at import, so adding a field to
`CONTRACT_KEYS` raises until someone classifies it, and a reviewed field cannot be hidden
from the seats by quietly reclassifying it.

This was not a hypothetical. Qualifying issue #61 twice produced `eligible` and
`singleOutcome: true` from both seats in both rounds, but the derived size tracked the
declared size upward — declared 1 derived 2, then declared 2 derived 3 — and no review was
ever bound to a contract declaring the size that review derived.

## Reviewed content must not size itself

The projection withholds `points` and `priority`. That removes the **structured** anchors.
It does nothing about **semantic** ones, and those are the same failure wearing prose.

Reviewed content — `problemStatement`, `acceptanceCriteria`, `testCommands`, `allowedPaths`,
`outOfScope`, `rollbackPlan` — must not assert its own size or priority. No "this is a
one-day change", no "size:1", no "P0", no "trivial".

This is not hypothetical. Re-qualifying issue #61 required rewording an acceptance criterion
that read:

> Stop at two engineer-days, the independently reviewed size.

to:

> Stop at the independently reviewed size recorded on this ticket.

It did two kinds of damage at once:

1. **It anchored the seats.** A seat reading "two engineer-days" in the work it is sizing is
   being told the answer.
2. **It moved the projection digest whenever the size moved.** A derived value inside
   reviewed content puts the size back inside the reviewed bytes, which is exactly the
   circularity the projection exists to break. The qualification stops converging again.

The second is the one people miss. The first is a bias; the second is a mechanical
regression to the pre-projection behaviour.

Refer to "the independently reviewed size recorded on this ticket" instead. Scope belongs in
`allowedPaths` and `outOfScope`, effort belongs to the seats.

**A lexical rule is not a defence.** This prohibition catches "two engineer-days" and
"size:1". It does not catch "a quick tidy-up", "should be straightforward", "just a doc fix",
or a problem statement written to sound small. There is no wording rule that closes semantic
anchoring, and treating this one as if it did is worse than knowing it is partial.

So the rule has a second half: **a seat that notices an anchor claim reports it rather than
silently absorbing it.** Say what the claim was, and size the work as described regardless.
An anchor a seat names is evidence; an anchor a seat quietly accepts is invisible drift, and
drift is what this whole mechanism exists to stop.

## Required record

Return one JSON object with these exact top-level keys:

```json
{
  "schemaVersion": 2,
  "runId": "review-run-id",
  "contractSha256": "lowercase-64-hex-sealed-contract-digest",
  "sizingProjectionSha256": "lowercase-64-hex-sizing-projection-digest",
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

## Two-phase seal

`reviewRef.contractSha256` binds the whole contract, including the two derived fields, and
admission rejects a review whose derived `points` or `priority` differ from the contract's.
Recording a derived value therefore changes the digest that bound the review that derived it.
Qualification converges only because the *reviewed content* — the projection — does not move
when those values are recorded.

1. **Phase one, review.** Build the contract with any placeholder for `points` and
   `priority`. Compute `sizing_projection_sha256`. Give every required seat the projection
   and that digest. Collect the seat reviews and derive the decision.
2. **Phase two, seal.** Write the derived `points` and `priority` into the contract. The
   projection digest is byte-identical across this write — assert it. Compute
   `contract_sha256` over the sealed contract, bind `reviewRef` to that digest, and record
   the projection digest in the review record's `sizingProjectionSha256`.

`sizingProjectionSha256` is what the seats were shown. Admission re-derives the projection
from the published contract and rejects the review with `review-projection-mismatch` if the
two differ, so a reviewed field edited after the review fails even though the contract digest
was recomputed. A change to a derived field does not, because the seats determined it.

Record the projection digest in the issue's Sizing prose as well. **The prose copy is a
human-auditable convenience, not the enforced binding** — nothing parses it. The enforced
binding is `sizingProjectionSha256` in the review record.

### What is enforced, and what is not

Enforced in code:

- The key classification partitions `CONTRACT_KEYS`, checked at import.
- The projection is byte-identical for every value of the derived fields, including omitted ones.
- `contract_sha256` binds the sealed contract; a mismatch is `contract-sha256-mismatch`.
- Admission re-derives the projection from the published contract and rejects a differing
  attested digest with `review-projection-mismatch`, so a reviewed field edited after the
  review fails even though the contract digest was recomputed.
- Admission rejects derived values that disagree with the review, by size and priority.

Operator-enforced, with nothing checking it:

- That the seats were actually shown the projection whose digest is recorded.
- That the named seats ran at all, and that their results are theirs.
- That the prose digest in the issue matches the one in the review record.
- That reviewed content carries no self-sizing assertion.

If a seat is re-run, re-run it against the same projection digest. A new projection digest
is different work: its estimate is a first opinion on that work, not a second opinion on the
old work.

That is a statement about **sizing identity only**. It resets nothing. The run guard's
`maxReviewRounds` budget is a session counter in
`plugins/ai-council-run-guard/scripts/run_guard.py`, it is independent of projection
identity, and editing reviewed content does not give a session more rounds.

## Integrity is not authorization

`reviewSha256` is an unauthenticated content address computed only after the
record validates. It detects changes to the canonical reviewed content. It is
not a cryptographic seal, approval, signature, or proof that either named seat
ran. One process can construct both seat reviews and recompute the digest.

Protected review evidence and authorization are separate downstream controls.
Until those controls verify the named run and its custody, a structurally valid
record must not authorize implementation, GitHub mutation, or activation.
