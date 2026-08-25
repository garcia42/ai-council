# A council row that should not exist

> **STOP — not approved for activation.** Issues #33 and #34 implement the duplicate
> predicate, append-time composition, and fail-closed reader rules in
> [the issue #32 design](../../design/2026-08-24-duplicate-council-row-supersede.md).
> A merge council governs repository merge only: merging the code does not activate it
> or authorize a live supersede. Runtime activation and any live-ledger record each
> require their own explicit approval.

## Problem

Two rows on the panel log describe the same council. The second was appended by hand,
seconds after the first, and carries no `forecastState` and no predictions. The gate says:

```
line N: brief path also used on line M: <path>
```

which reads like the brief collision in
[duplicate-blind-brief-path.md](duplicate-blind-brief-path.md) and is not that problem at
all. Two councils sharing one brief is an evidence question about which seat read which
bytes. This is a bookkeeping duplicate: one council, written down twice.

It is worse than a red gate. Both rows carry `ran: true` and a Boolean `changedDecision`,
so the deployed kill criterion counts each as a completed run. **The denominator the blind
seat's retire/keep decision rests on is inflated**, and one of the 2026-08-23 pair
duplicated a `changedDecision: true` — the exact field the criterion is scored on.

A supersede asserts **"this pinned row duplicates that pinned retained row."** It does
not assert "this row does not count." Absence of forecasts protects the evidence-bearing
original, but it does not prove that the other row is a duplicate.

## Root cause

`complete` writes the panel-log row itself. Step 7 of the council skill also told the
reviewer to append it, so a reviewer following the instruction wrote a second row for a
council that already had one. Issue #3 removed that instruction; it could not remove the
rows already written.

Nothing in the toolchain could have produced them, which is how you recognize one:

| | tool-written row | hand-appended duplicate |
|---|---|---|
| `schemaVersion` | present | absent |
| `runId` | always | sometimes absent |
| `forecastState` | sealed | absent |
| `predictions` | one per submitted seat | absent or `[]` |
| keys | 12 | 5–6 |

The appender would have refused all three defects. The ledger is mode 0600 and every
session runs as the same user, so permissions cannot stop a hand append; only the
instruction that invited it could be removed.

## Diagnosis steps

```
sha256sum ~/.claude/knowledge/futures-panel-log.jsonl
wc -lc    ~/.claude/knowledge/futures-panel-log.jsonl
python3 ~/.claude/knowledge/council-eval/blind_seat_kill_criterion.py --json
```

For each line the gate names, compare it against the line it duplicates:

```
python3 - <<'PY'
import hashlib, json
raw = open("/home/trader/.claude/knowledge/futures-panel-log.jsonl","rb").read().splitlines(keepends=True)
for n in (<original>, <suspect>):
    row = json.loads(raw[n-1])
    print(n, hashlib.sha256(raw[n-1]).hexdigest(), sorted(row), "forecastState" in row)
PY
```

The original is the row carrying the forecasts. Establish that before anything else: the
whole cure turns on retiring the copy and never the original. If both rows carry sealed
forecasts, this playbook does not apply — you have two councils, not one written twice.

## Target-state solution

The procedure below describes the operational sequence only after the corrected code has
merged and its resulting runtime has separately been approved, rehearsed, activated, and
verified. Do not execute it merely because the repository contains the implementation.

**Nothing else can cure this.** `repair-tail` removes only a line that fails JSON parsing,
and these parse. `recover-brief` rewrites `blindSeat.brief`; pointing a duplicate at a
different brief would assert the seat read a brief it never read, which is fabricating
evidence to clear a validator. **It is also now refused in code** (#35): once a
`council-superseded` record names a line — through either `supersedes` or `duplicateOf` —
`recover-brief` refuses that line at both planning and execution, because rewriting it would
break the digest the record pins and that record cannot afterwards be withdrawn. `recover-valid-row` is allowlisted to
`blindSeat.role -> "SKIPPED"` on `ran=false` rows and must not grow a case for this. The
row is not wrong in a field — it should not exist, and an append-only store cannot say
that by editing.

**1. Activate the reader first.** The cure is a record the reader has to understand. A
supersede appended against the old reader retires nothing and merely adds two rows it
counts as legacy. Confirm the installed criterion carries it:

```
grep -c _apply_supersedes ~/.claude/knowledge/council-eval/blind_seat_kill_criterion.py
```

**2. Rehearse against a copy.** Copy the ledger somewhere outside the live tree, run the
supersede against the copy, and diff the gate before and after. Expect `completedRuns` and
`changedDecisionRuns` to fall by exactly the number of rows retired, `supersededRows` to
rise to match, and the named errors to disappear. Nothing else may move.

**3. Append the record**, on the ledger authority host, with principal approval:

```
sed -n '<line>p' ~/.claude/knowledge/futures-panel-log.jsonl | sha256sum

python3 ~/.claude/knowledge/council-eval/predictions_report.py \
  supersede --log ~/.claude/knowledge/futures-panel-log.jsonl \
  --line <line> --confirm-raw-line-sha256 <digest of that exact line> \
  --duplicate-of-line <retained-line> \
  --confirm-duplicate-of-raw-line-sha256 <digest of retained exact line> \
  --reason <why this row should not exist> \
  --operator <name> --reference <where approval was given> --check-only
```

Drop `--check-only` to append. The digest is required rather than derived: a line number
names a position, and only the digest names a row.

The appender pins both the target (`supersedes`) and its retained witness (`duplicateOf`)
by line and raw-line digest. It re-derives the design's duplicate predicate, uniqueness
rule, and prefix state. The no-forecast check remains a necessary evidence-preservation
guard on the target, but it is not evidence of duplication and is never sufficient. The
reader independently replays the same shape, identity, duplicate, and composition checks;
any record or raw JSON it cannot verify retires nothing.

**4. Re-run the gate.** It should reach exit 0 with `superseded_rows=<n>`.

## Prevention

Issue #3 removed the duplicate-write instruction from step 7 and stated in
`runtime/council-forecast-contract.md` that `complete` writes the panel-log row itself.
That is the fix; this record is only the cure for rows that landed before it.

The residual exposure is unchanged and worth naming: any process running as `trader` can
append any bytes to the ledger, and no in-tool mechanism can prevent it. What the supersede
record adds is that the correction is now itself append-only, attributed, and re-derivable
— an operator, a reason, an approval reference, and a digest that pins one exact row.

Known gaps and activation prerequisites:

- Activation must confirm the runtime-only `test_blind_seat_kill_criterion.py` supplies
  raw-line SHA-256 triples. If it still supplies two-item identities, update it first;
  the corrected reader deliberately fails those identities closed.
- The record retires a row from the tally; it does not mark the retired row itself. A
  reader that does not implement supersedes still counts it, which is why activation
  order matters.
- The kill criterion still fails the whole ledger for one bad row.

## Related files

- `design/2026-08-24-duplicate-council-row-supersede.md` — normative duplicate and
  composition specification
- `tests/fixtures/duplicate-council-row-supersede/` — content-free ledger-shaped cases
- `src/council_tools/forecasts.py` — `make_supersede`, `validate_supersede`, `superseded_lines`
- `src/council_tools/cli.py` — `command_supersede`
- `install.py` — `_with_superseded_reader`, the preimage-anchored reader change
- `tests/test_cli.py` — the appender/reader seam test
- `~/.claude/knowledge/council-eval/blind_seat_kill_criterion.py` — the reader

## History

**2026-08-24.** Lines 114 and 117 of the panel log were hand-appended duplicates of 113
and 116, the second live instance of the pattern issue #3 fixed at the instruction level.
As recorded the gate read 60 completed / 44 changed / 73.3%; excluding the two duplicates
it reads 58 / 43 / 74.1%. The verdict stayed KEEP either way, which is exactly why the
contamination mattered more than the number: a gate stuck at exit 1 on rows nobody could
touch is what trains reviewers to walk past a red gate.
