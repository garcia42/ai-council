# Pre-mortem: council forecast scoring MVP

Status: COMPLETE BEFORE IMPLEMENTATION
Date: 2026-08-22

The named `/pre-mortem` skill was absent from the runtime skill directory, so the gate was
reconstructed from its durable contract: a frozen author baseline, one independent stripped-
context blind seat, and exactly one house code lens. Neither reviewer saw the other's response.

## Independent blind seat

The blind seat permitted only a shadow-measurement MVP. It added four requirements not explicit
in the author baseline:

1. **Seal seat submissions.** The attempt row may expose the common question and outcome, but no
   seat probability may be appended until the complete set is assembled. States must distinguish
   submitted, abstained, and unavailable.
2. **Govern retries.** One immutable issuance per seat/outcome is headline-scoreable. Later retries
   remain visible, carry a supersession link and evidence cutoff, and never silently replace the
   earlier forecast.
3. **Expose resolution selection.** Reports must show eligible, resolved, unresolved, overdue,
   void, and excluded counts. When due resolution coverage is incomplete, the score must be marked
   incomplete rather than presented as representative.
4. **Link the outcome to the decision.** A shared event must state why it is material and what
   action changes if it is true or false. A gradeable but decision-irrelevant proxy is invalid.

## House code lens

The code lens added four mechanical requirements:

1. Add every new ledger record kind to the blind-seat tally's explicit non-seat allowlist in the
   same change, with an interleaved-ledger regression test.
2. Generate and enforce globally unique IDs under two writers sharing the same mocked second.
3. Detect reuse of an open outcome. Exact fingerprint reuse must link to the existing outcome or
   require an explicit relationship; semantic paraphrases remain a documented manual audit limit.
4. Never resolve by array position. Resolutions bind to stable outcome IDs, and prediction IDs do
   not change when another issuance is added.

## Design decisions after review

- `council-attempt` contains the common outcome but no probabilities. The completed council record
  seals all required seat prices together.
- UUIDs are required for new run, outcome, prediction, resolution, and override IDs. Exact
  duplicate IDs are rejected during locked append.
- The earliest valid issuance per seat/outcome is the descriptive headline forecast. Later
  issuances remain auditable and do not replace it.
- Outcome resolution is recorded once per `outcomeId`, so seats pricing the same event cannot be
  graded inconsistently.
- Every outcome requires `decisionLink`, `materiality`, `actionIfTrue`, and `actionIfFalse`.
- Incomplete due-resolution coverage suppresses any implication that observed Brier is
  representative.
- `council-attempt` is added to the kill-criterion allowlist. The implementation will not add
  separate per-prediction ledger kinds.

## Tests that precede implementation

1. Same-second writers receive distinct stable IDs and resolving one outcome cannot affect the
   other.
2. Interleaving attempt and council records leaves the blind-seat legacy count unchanged.
3. Adding or retrying a prediction cannot rebind an existing outcome resolution.
4. Resolving only winning forecasts marks the score incomplete and exposes the unresolved debt.
5. Outcomes without explicit decision linkage fail validation.
