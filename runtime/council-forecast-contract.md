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

4. **Bind one immutable brief to this run before the blind seat reads anything.** The path must
   contain the emitted `runId`, and it is created exclusively — never with a shell redirect, and
   never named from the date and topic alone:

   ```
   python3 /home/trader/.claude/knowledge/council-eval/predictions_report.py \
     prepare-brief --run-id <runId> --source <draft-brief> \
     --destination <briefs-dir>/<date>-<topic>-<runId>.md \
     --expected-sha256 <sha256 of the draft>
   ```

   The command refuses to overwrite, so two councils drafting the same topic at the same moment
   cannot silently share one file. Launch the blind seat against the created path and record that
   exact path in `blindSeat.brief`. Completion rejects a brief that is relative, omits its `runId`,
   or already belongs to another council row. **Every row carrying an explicit `ran` state needs a
   brief, whether the seat ran or was skipped** — a skipped seat was still given a question, and the
   kill criterion refuses a briefless explicit-run-state row. If a completion is rejected for its
   brief path, no seat work is lost: rename the file with `prepare-brief`, correct the one string in
   the completion spec, and re-run `complete`.

   This exists because on 2026-08-23 two concurrent sessions chose the same date/topic path, the
   second overwrote the first between its creation and its blind launch, and one blind answer
   became unattributable to the question it was actually given.

5. Put the byte-identical shared outcome and evidence cutoff in every seated reviewer's prompt.
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

The resolution timestamp is system-owned. New events bind the issued outcome fingerprint, and
reporting rejects a changed resolution date, a pre-issuance grade, or a resolution later than the
report observation time.

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

### Duplicate blind-brief recovery

A valid row that names a brief another council already owns is not a torn write, and `repair-tail`
must not be used on it. It is repaired only with explicit principal approval, only on the ledger
authority host, and only through the hash-pinned command below. Derive the spec from the live bytes,
read it, then run it:

```
python3 /home/trader/.claude/knowledge/council-eval/predictions_report.py \
  plan-brief-recovery --ledger <ledger> --target-line <line> \
  --replacement-source <the bytes that seat actually read> \
  --operator <name> --approval-reference <where approval was given> \
  --approval-reason <why> > <spec.json>

python3 /home/trader/.claude/knowledge/council-eval/predictions_report.py \
  recover-brief --spec <spec.json> --confirm-operator-approved-rewrite
```

It holds the append lock, refuses any drift from the spec's pinned hashes, proves in advance that no
prediction and no other row can change, saves a byte-exact SHA-named backup, writes a prepared and a
completed audit record, and rewrites exactly one `blindSeat.brief` field. An interrupted run is
re-entered with `--resume`, which refuses to start a new recovery and verifies every artifact it
finds against the same spec. Reconstructing the replacement brief is evidence work, not a command:
identify the bytes that seat actually read before planning anything.

### V2 usefulness capture activation gate

V2 is additive and capture-only. It records durable initiation before work begins, exact input
artifact references for every planned seat, exact output artifact references for submitted seats,
seat roles and versions, atomic findings, operator dispositions, outcome class, system boundary
timestamps, invalidations, and a first-ten data-health report. It does not rank seats or claim
causal decision value. Only resolved exogenous V2 outcomes enter its descriptive Brier output; V1
and intervention-sensitive outcomes remain visible but excluded.

Every planned input contains one canonical machine-readable forecast-request block disclosing the
actual claim, resolution procedure and date, materiality, actions, run, outcome, fingerprint, and
evidence cutoff. Its `forecastRequestSha256` is derived from those visible fields. Every submitted
visible JSON output has a `capture` object that binds the same request plus its seat, input-artifact
digest, and integer `sharedProbability`. A `no-findings` result uses the same object with `kind:
no-findings` and `findings: []`. Otherwise `capture.findings` contains the canonical exact
seven seat-owned finding fields; later operator grouping and dispositions cannot invent or alter
them. Reporting parses and re-reads the retained bytes and rechecks those bindings.
`finalizedAt` is the issuance boundary and must precede the shared outcome's resolution date.

All evidence writers take the same evidence coordination lock. `evidence-snapshot` takes the
corresponding shared lock so a local snapshot is wholly before or after an append. This is still a
local-filesystem rehearsal, not an off-host backup claim.

The live `capture-activate` command is evidence-gated and is never implied by installation. Through
the installed source-pinned wrapper it accepts only a version-2 content-addressed approval manifest
whose frozen audit and durability policies and source-bound rehearsal certificates all revalidate
while the evidence lock and pinned ledger transaction are held. Missing, stale, mismatched, or
unrehearsed evidence appends nothing. A selected decision family's first complete run produces a
blinded one-in-five audit case and a separately retained alias map; retries inherit the prospective
assignment, and audit rows never enter lifecycle or Brier denominators.
Before activation, exercise `capture-artifact`, `capture-initiate`, `capture-attempt`,
`capture-seats-finished`, `capture-complete`, `capture-report`, and the evidence snapshot commands
only against copied or explicit non-live paths. No capture command accepts an operator-supplied
lifecycle timestamp.
