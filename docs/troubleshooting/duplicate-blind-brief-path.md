# Two councils, one blind brief

## Problem

`blind_seat_kill_criterion.py --json` exits 1 with a single error of the form:

```
line N: brief path also used on line M: <path>
```

The ledger is append-only and the criterion accumulates errors across every row, so
**one duplicated brief path fails the whole ledger**. Councils that completed
successfully hours earlier are invalidated along with the offending pair, and the
process contract stops any further council until the state is valid again.

The rows themselves are well-formed JSON. `repair-tail` does **not** apply: its
contract covers exactly one malformed final line, and neither of these rows is torn.

**First check which problem you have.** The same error is raised when one council was
simply written down twice — a hand-appended duplicate with no `forecastState` and no
predictions. That is a different root cause with a different cure; see
[duplicate-council-row-supersede.md](duplicate-council-row-supersede.md). This playbook is
for two genuinely distinct councils that collided on one brief path, which you can tell
because both rows carry sealed forecasts.

## Root cause

The blind brief was written to a path derived from the date and the topic, e.g.
`2026-08-23-<topic>.md`. Two sessions working on related questions derived the same
name. Brief creation happened outside the ledger append lock and did not use the
`runId` that the attempt had already emitted, so:

1. Council A created the file and launched its blind seat against those bytes.
2. Council B overwrote the same path 36 seconds later and launched its own seat.
3. Both completions recorded the same path. The file now holds only B's text, so A's
   recorded probability can no longer be matched to the question that produced it.

Completion order is independent of creation order, so the ledger row order does not
tell you which council owns the path. Ownership is decided by which seat read which
bytes, which is evidence work — see Diagnosis.

## Diagnosis steps

Establish, in this order, and write down the hashes as you go:

```
sha256sum ~/.claude/knowledge/futures-panel-log.jsonl
wc -lc     ~/.claude/knowledge/futures-panel-log.jsonl
python3 ~/.claude/knowledge/council-eval/blind_seat_kill_criterion.py --json
python3 ~/.claude/knowledge/council-eval/predictions_report.py report
```

Then identify which run owns the surviving file:

- Compare the current file's SHA-256 against each council's attempt/completion
  artifacts and, if available, the launcher transcript that recorded what each seat
  was actually shown. The owner is the run whose seat read *these* bytes.
- The other run needs its brief reconstructed from its own artifacts. Reconstruct it,
  then verify the reconstruction reproduces the hash recorded at the time. If nothing
  records that hash, say so explicitly rather than asserting provenance you do not have.

Do not proceed on row order. It reverses.

## Solutions

Safest first.

**1. Nothing, if the duplicate is the most recent line and no seat has answered yet.**
Abandon the attempt and start a new council with a run-scoped brief. An abandoned
`council-attempt` row is a designed, observable state.

**2. Operator-approved brief recovery.** For two valid completed rows, this is the only
sanctioned path. It requires the ledger authority host and explicit principal approval.

```
python3 ~/.claude/knowledge/council-eval/predictions_report.py \
  plan-brief-recovery --ledger <ledger> --target-line <line> \
  --replacement-source <the bytes that seat actually read> \
  --operator <name> --approval-reference <where approval was given> \
  --approval-reason <why> > <spec.json>

# read the spec, then:
python3 ~/.claude/knowledge/council-eval/predictions_report.py \
  recover-brief --spec <spec.json> --confirm-operator-approved-rewrite
```

The spec pins the ledger hash, line count, byte count, mode, both raw line hashes and
the expected after-image. Execution re-derives all of it independently and refuses on
any disagreement, so a wrong plan repairs nothing. It writes a SHA-named byte-exact
backup and a prepared/completed audit pair under
`~/.local/state/council-tools/ledger-recovery-<runId>/`.

Rehearse first against a copy. `--rehearsal-root <dir>` maps every absolute path in the
spec beneath a mirror; run both validators against the mirrored ledger before touching
the live one.

**3. If it was interrupted**, re-enter with `--resume`. Resume refuses to start a new
recovery, requires the artifact directory, verifies every artifact it finds against the
same spec, and accepts the ledger at either the before image or the repaired image —
including when a legitimate append has landed on top of the repaired one. If both
`--resume` and re-planning refuse, the escape is to re-plan with a fresh
`--artifact-dir` and `--destination`.

**Never** hand-edit either JSONL. Never use `repair-tail` on a valid row.

## Prevention

`prepare-brief` creates the brief with `O_EXCL` at mode 0444, so two sessions racing on
one topic collide at creation rather than silently sharing bytes:

```
python3 ~/.claude/knowledge/council-eval/predictions_report.py \
  prepare-brief --run-id <runId> --source <draft> \
  --destination <briefs-dir>/<date>-<topic>-<runId>.md \
  --expected-sha256 <sha256 of the draft>
```

The append validator then rejects any completion whose brief path is relative,
non-normalized, missing its `runId`, or already owned by another row. Every row with an
explicit `ran` state needs a brief, whether the seat ran or was skipped — that matches
the kill criterion exactly, and the two must not diverge: a shape the appender admits
and the reader refuses becomes an unremovable ledger line.

A rejected completion loses no seat work. The completion spec is a file: rename the
brief, fix the one string, re-run `complete`.

Known gaps, deliberately not closed here:

- The row records a **path, not a digest**. Same path with mutated content is uncaught.
  Binding `blindSeat.briefSha256` is the highest-value next step.
- A repaired row is not marked as repaired *in the ledger*; the erratum lives only in
  the artifact directory.
- The kill criterion still fails the whole ledger for one bad row.

## Related files

- `src/council_tools/brief_recovery.py` — `prepare_blind_brief`, `plan_blind_brief_recovery`, `recover_blind_brief`
- `src/council_tools/forecasts.py` — `validate_blind_brief_identity`
- `runtime/council-forecast-contract.md` — the contract text installed into the council skill
- `tests/test_brief_recovery.py` — crash injection at all seven checkpoints, and the cross-validator agreement test
- `~/.claude/knowledge/council-eval/blind_seat_kill_criterion.py` — the reader whose rules the appender must match

## History

**2026-08-23.** Two concurrent sessions collided on
`2026-08-23-v21-p01-repair-review-round-2.md`. The kill criterion failed the ledger and
invalidated an unrelated council that had already completed. Recovery moved the ledger
from `8d1ac5c7` to `ceb34305` at 103 lines, rewriting one `blindSeat.brief` field on one
row; predictions digest and all other rows byte-identical, both validators returned to
green.

Review of the mechanism then found the appender had been written to admit a briefless
skipped-seat row that the kill criterion refuses — the same outage through a different
door — which was fixed before merge and is now guarded by a test that fails against the
old predicate. The blind seat could not be seated (launcher quota exhausted) and is
recorded as a required non-run.
