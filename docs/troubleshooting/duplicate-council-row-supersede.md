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

If that returns `0`, activate it — the check above has no procedure attached anywhere else,
and the two mistakes available here are both worse than not starting.

> **Run `install.py` only from `/home/trader/council-tools`. Never from a session worktree.**
>
> `install.py` sets `REPO = Path(__file__).resolve().parent` and renders that path into the
> installed runtime as `SOURCE_ROOT`. Run it from the worktree you happen to be standing in
> and the live runtime is pinned to a disposable directory; when that worktree is rebased or
> removed, every council tool on the box fails closed. `/home/trader/council-tools` is a
> git worktree of this repository kept for exactly this purpose, and it is the only correct
> source root.

> **There is an outage window, and it opens at the checkout, not at the install.**
>
> `predictions_report.py` calls `_assert_source_integrity()` at import: it compares
> `git -C /home/trader/council-tools rev-parse HEAD` and a digest of `src/council_tools`
> against the values baked in at install time, and raises `SystemExit` on any difference.
> So from the moment you check out a new commit there until `install.py` finishes, **every
> tool routed through that runtime exits rather than runs** — including the supersede
> appender in step 3. Do not start the checkout until you are ready to finish the install.

```
# 1a. rehearse first, from the session worktree, against a copy — see step 2
# 1b. then, in the real source root:
git -C /home/trader/council-tools fetch origin main
git -C /home/trader/council-tools checkout <the merge commit>
git -C /home/trader/council-tools status --porcelain=v1 --untracked-files=all   # must be empty
# 1c. check what would change, then install — from there, not from anywhere else:
/home/trader/ai-council/.venv/bin/python /home/trader/council-tools/install.py check
/home/trader/ai-council/.venv/bin/python /home/trader/council-tools/install.py install
```

`check` exits 1 and prints a `DRIFT:` line per difference; `install` prints the backup
directory it wrote. **Keep that path** — it is the only input to the runtime rollback below.

The empty-status check is not advice: for a live install `install.py` runs the same command
and refuses with `live install requires a clean council-tools source commit`. Running it
first only means you find out before it does.

Confirm both halves moved afterwards — the reader and the pin, not just one:

```
grep -c _apply_supersedes ~/.claude/knowledge/council-eval/blind_seat_kill_criterion.py
grep -n 'EXPECTED_COMMIT\|SOURCE_ROOT' ~/.claude/knowledge/council-eval/predictions_report.py
git -C /home/trader/council-tools rev-parse HEAD
```

`EXPECTED_COMMIT` must equal that `HEAD`, and `SOURCE_ROOT` must read
`/home/trader/council-tools`. If `SOURCE_ROOT` names anything under
`/home/trader/ai-council-sessions/`, the install was run from the wrong directory: re-run it
from the correct source root before doing anything else.

**2. Rehearse against a copy.** Copy the ledger somewhere outside the live tree, run the
supersede against the copy, and diff the gate before and after.

**Several counters move, and an earlier version of this document said none but two may.**
Taken literally that instruction aborts a correct cure — and aborting between the checkout
and the install is how a half-applied retirement becomes the unretractable state the rest of
this document exists to avoid. What actually moves, per row retired, measured rather than
expected:

| Field | Direction | Why |
| --- | --- | --- |
| `completedRuns` | −1 | the retired row leaves the completed set |
| `supersededRows` | +1 | it is counted as retired |
| `nonCouncilRecords` | **+1** | the supersede record is itself a counted record, so this **rises** |
| `errors` | → 0 | the duplicate error clears; this is the outcome being sought, not an anomaly |
| `changedDecisionRuns` | −1 **only if** the retired row recorded `changedDecision: true` | otherwise unchanged |
| `unchangedDecisionRuns` | −1 **only if** the retired row recorded `changedDecision: false` | otherwise unchanged |
| `decisionChangingRate` | **either direction** | see below |

`decisionChangingRate` is `changedDecisionRuns / completedRuns`. Retiring an *unchanged* row
shrinks only the denominator, so the rate **rises**; retiring a *changed* row shrinks both, so
it **falls**. Both are correct. Seeing the direction you were not expecting is not a reason to
stop.

`criterion` moves only if the retirement takes `completedRuns` below 10, and
`operationalState` does not move at all.

Compare the two gate outputs field by field rather than eyeballing a summary line:

```
cp ~/.claude/knowledge/futures-panel-log.jsonl /tmp/ledger-rehearsal.jsonl
/home/trader/ai-council/.venv/bin/python \
  ~/.claude/knowledge/council-eval/blind_seat_kill_criterion.py \
  --log /tmp/ledger-rehearsal.jsonl --json > /tmp/gate-before.json
# ... run the supersede against /tmp/ledger-rehearsal.jsonl ...
/home/trader/ai-council/.venv/bin/python \
  ~/.claude/knowledge/council-eval/blind_seat_kill_criterion.py \
  --log /tmp/ledger-rehearsal.jsonl --json > /tmp/gate-after.json
diff <(python3 -m json.tool /tmp/gate-before.json) \
     <(python3 -m json.tool /tmp/gate-after.json)
```

> **The gate exits non-zero in exactly the situation you are running it in.** It exits **1**
> when `errors` is non-empty — which is true before the cure, by definition — and **2** when
> `operationalState` is `BLOCKED_DEGRADED`. It does **not** exit non-zero merely because
> `--json` was passed. So never chain it with `&&`: the next command silently does not run,
> and the procedure appears to have completed. Redirect to a file and read the file, as above.

**3. Append the record**, on the ledger authority host, with principal approval:

Re-derive the line numbers and digests **now**, at execution time. Other sessions append to
this ledger concurrently, so any number written down earlier — in this document, in a review,
or in your own notes from an hour ago — may already name a different row.

```
# line numbers the gate currently objects to:
/home/trader/ai-council/.venv/bin/python \
  ~/.claude/knowledge/council-eval/blind_seat_kill_criterion.py --json > /tmp/gate-now.json
python3 -m json.tool /tmp/gate-now.json | grep -A20 '"errors"'

# the digest of one exact line, interpreter-pinned so it cannot pick up a different python:
/home/trader/ai-council/.venv/bin/python -c '
import hashlib, sys
raw = open("/home/trader/.claude/knowledge/futures-panel-log.jsonl", "rb").read().splitlines(keepends=True)
n = int(sys.argv[1])
print(n, hashlib.sha256(raw[n - 1]).hexdigest())
' <line>

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

**4. Re-run the gate.** It should reach exit 0, with `superseded_rows` risen by the number of
rows retired and `errors` empty. Exit 1 means errors remain — read them; exit 2 means
`operationalState` is `BLOCKED_DEGRADED`, which is a different problem this playbook does not
cure.

**5. Rollback: one half is reversible and the other is not.** Be sure of this before step 3.

*The runtime install is reversible, in one order and not the other.* The full procedure is
**Rolling back a runtime install** below. Read it before step 3, not after: run the two
commands in the wrong order and you get a total council-tooling outage with no council tooling
available to diagnose it.

*The appended supersede record is not.* Nothing in this repository reverses one, because the
store is append-only and `validate_supersede` accepts only a `kind: "council"` target — **a
supersede record cannot itself be superseded.** Once appended against the wrong row it stays,
and the gate stays red on a row nobody can edit.

That asymmetry is the reason for everything upstream of it: the digest is required rather than
derived, step 2 rehearses against a copy rather than dry-running against the live ledger,
`--check-only` exists, and `recover-brief` now refuses in code any line a supersede record
names (#35). If a procedure for retracting a supersede is ever written, cross-reference it
here.

## Rolling back a runtime install

Measured on a staged root (`RestoreDoesNotRollBackTheReaderTest` in `tests/test_install.py`),
after an install that changed the gate reader:

| Runtime target | What `install.py restore` puts back |
| --- | --- |
| `predictions_report.py` | the pre-install bytes — **rolls back** |
| `SKILL.md`, `CLAUDE.md` | the pre-install bytes — **roll back** |
| `blind_seat_kill_criterion.py` | the **post**-install bytes — **does not roll back** |

**`restore` re-applies both rendering transforms to the payload it puts back**, so the
criterion it restores is the reader you were trying to leave, not the one you were trying to
return to.

### Why the order matters

The restored `predictions_report.py` pins `SOURCE_ROOT` and `EXPECTED_COMMIT`, and asserts at
import that the source directory still holds that commit. Restore the shim to the old commit
while `/home/trader/council-tools` still stands on the new one and **every invocation exits
with `installed pin does not match source`** — including the ones you would use to work out
what happened. Move the checkout first.

```
# 1. move the source back FIRST
git -C /home/trader/council-tools checkout <the previous commit>
git -C /home/trader/council-tools status --porcelain=v1 --untracked-files=all   # must be empty

# 2. then restore, using the backup path the install printed
/home/trader/ai-council/.venv/bin/python /home/trader/council-tools/install.py \
  restore --backup <that path>

# 3. confirm the pin and the source agree again
git -C /home/trader/council-tools rev-parse HEAD
grep -n 'EXPECTED_COMMIT\|SOURCE_ROOT' ~/.claude/knowledge/council-eval/predictions_report.py
```

The outage window is between step 1 and the end of step 2, exactly as it is for a forward
install. It is not avoidable; it is only kept short and expected.

### The reader is restored by re-installing, not by restoring

Because the criterion does not roll back, **`restore` alone leaves the new reader in place**.
To return to the previous reader, check the previous commit out in the source root and run
`install.py install` from there — the forward path, run backwards. `restore` is for undoing
everything *except* the reader.

Confirm with `grep -c _apply_supersedes` as in step 1: it reports the reader that is actually
installed, not the one the backup contained.

### Should `restore` be changed to preserve the pre-change reader?

**No, and the reason is not inertia.** The same code path also re-applies the record-kind
allowlist, and that half must be re-applied: a criterion restored without the record kinds it
has to recognise would misread the live ledger — a wrong answer rather than a refusal, which is
worse than not rolling back at all. Separating the two transforms so one is re-applied and the
other is not is a real change to the installer with its own failure modes, and it would buy a
convenience that `install.py install` from the previous commit already provides correctly.

What was missing was never the capability. It was this section. If that judgement is revisited,
the change belongs in its own ticket, specified as: split the restore-time rendering so
`_with_attempt_allowlist` is re-applied and `_with_superseded_reader` is not, and prove on a
staged root that the restored criterion equals the pre-install bytes while still carrying the
allowlist.

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

**2026-08-24.** Two hand-appended rows in the panel log duplicated two earlier rows — the
second live instance of the pattern issue #3 fixed at the instruction level. Excluding the
duplicates moved the tally by two completed runs and one changed run, and the verdict stayed
`KEEP` either way. That is exactly why the contamination mattered more than the number: a gate
stuck at exit 1 on rows nobody could touch is what trains reviewers to walk past a red gate.

*Line numbers and totals are deliberately not recorded here.* Other sessions append to this
ledger concurrently, so both had already moved twice during the review that produced this
document, and a reader who trusted them would be working from a description of a ledger that
no longer exists. Re-derive them from the gate at execution time — step 3 shows how.

**2026-08-25.** This playbook was rewritten (#37) after an attempt to follow it end to end.
The activation step was a check with no procedure and did not name the only correct source
root; the outage window between checkout and install was unwritten; and the acceptance
criterion said nothing but two counters may move, which is false and would abort a correct
cure. The field table in step 2 is measured rather than expected.

Separately, `recover-brief` now refuses in code any line a `council-superseded` record names
(#35). The rule used to live only in prose in this document and its sibling.
