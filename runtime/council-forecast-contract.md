## Forecast contract — mandatory for every council invocation

The forecast ledger is an operational discipline instrument. Scores are descriptive only; normal
seat operation includes unequal information access and correlated house models.

The authoritative live writer is host `manny`. Other hosts may read reports or rehearse copied
data, but the CLI refuses their writes under `~/.claude/knowledge`. Do not bypass that guard by
manually editing JSONL. Before moving council execution to another host, update this versioned
authority rule, rehearse on that host, and rerun the council activation review.

### Before firing seats

1. Run the report:

   ```
   python3 /home/trader/.claude/knowledge/council-eval/predictions_report.py report
   ```

   Exit 1 is invalid state and stops the council. Restore a damaged sidecar from its verified
   installer/operator backup or use the narrowly scoped torn-tail procedure below; never skip an
   invalid record. Exit 3 is grading debt: continue convening and
   collecting the council, but mark decision finalization or shipping BLOCKED until the debt is
   resolved or a principal-approved override is logged. Exit 2 is a command usage error. `--today`
   is test-only and is rejected for live council paths.

   Exit 3 never blocks incident containment or rollback. The installed reporter verifies its pinned
   git commit and source digest before every command and refuses source drift. Develop future changes
   in another worktree; do not edit the active source tree.

2. Define one neutral **shared outcome** before any seat answers. It must be binary, material to
   the reviewed decision, and recorded with:

   - `claim`, `resolutionDate`, and `resolvedBy`;
   - `decisionLink` and `materiality`;
   - `actionIfTrue` and `actionIfFalse`;
   - `evidenceCutoffAt`.

   A merely convenient uptime or delivery claim is invalid unless it changes the decision.

3. Write an attempt spec to a unique temporary file. `expectedSeats` is code/theory/ops plus blind
   whenever blind is required; a later launcher failure is recorded as `unavailable`, never removed
   from the expected set. A business-only council may contain blind alone. Append the attempt:

   ```
   python3 /home/trader/.claude/knowledge/council-eval/predictions_report.py \
     attempt --spec <attempt-spec.json>
   ```

   This emits `runId` and `outcomeId`. The resulting `council-attempt` ledger row contains no seat
   prices. It makes failed or abandoned council invocations observable without leaking forecasts.

4. Put the byte-identical shared outcome and evidence cutoff in every seated reviewer's prompt.
   Each reviewer must end with:

   `SHARED_PROBABILITY: <0-100>%`

Fifty percent is permitted. Do not show any reviewer another seat's probability. A seat that
cannot price the claim records `abstained`; a failed launcher records `unavailable`. Those are
explicitly accounted states, not missing probabilities. Every `submitted` seat must have exactly
one probability; `abstained` and `unavailable` seats must have none.

### Seal and append the completed council

After all seat calls have returned, write one completion spec containing:

- the attempt `runId`;
- `councilFields`, including verdicts, blind-seat state, notes, outcome, commits, and artifacts;
- a sealed `forecastState` generated from the submitted `seatStates`;
- `seatStates` for every expected seat (`submitted`, `abstained`, or `unavailable`);
- one integer probability for every and only every submitted seat.

No partial seat probability is appended before sealing. A retry is a new council attempt and never
silently replaces an earlier issuance. If it reuses the exact shared-outcome fingerprint, put the
prior `outcomeId` in `sharedOutcome.relatedOutcomeIds` in the new attempt spec; the CLI retains and
validates that relationship.

Validate the completion spec without writing, then append it:

```
python3 /home/trader/.claude/knowledge/council-eval/predictions_report.py \
  complete --spec <completion-spec.json> --check-only
python3 /home/trader/.claude/knowledge/council-eval/predictions_report.py \
  complete --spec <completion-spec.json>
```

The `complete` command constructs stable prediction IDs, copies the sealed shared outcome, validates
the expected seat set, and appends under a file lock. Never append the council JSON manually.

Finally run the forecast report and blind-seat kill criterion. Missing forecasts block decision
finalization even when ordinary grading debt has not reached its escalation threshold.

### Resolution

Resolve by stable `outcomeId`, never timestamp or list index:

```
python3 /home/trader/.claude/knowledge/council-eval/predictions_report.py \
  resolve <outcomeId> true|false|void --evidence <durable-evidence> \
  --resolver <name> --method deterministic|manual-reviewed
```

Manual resolutions require a different `--reviewer`. Voids require an enumerated reason and remain
visible in the report. Every void uses `manual-reviewed` with an independent reviewer. Non-void
resolutions cannot be recorded until the full `resolutionDate` has ended in America/New_York.
Corrections use `--supersedes`; the original resolution is never rewritten. Reports include a
constant-50-percent Brier reference, an `inSampleBaseRateBrier` hindsight bound, and void rate over
due-or-void outcomes. The in-sample base-rate value is not an achievable ex-ante climatology. All
scores remain descriptive and outcomes may be seat-controlled and non-independent.

### Corrupt trailing write recovery

Malformed JSON fails closed. Never delete or silently skip an invalid row. If inspection proves
that exactly the final nonblank line is a torn write, record its line number and run:

```
python3 /home/trader/.claude/knowledge/council-eval/predictions_report.py \
  repair-tail --path <exact-ledger-or-sidecar-path> \
  --confirm-final-line <line-number> --backup-dir <quarantine-directory>
```

The command holds the append lock, refuses earlier or multiple corruption, saves the byte-exact
original with its SHA-256 in the backup name, and atomically removes only the confirmed final line.
