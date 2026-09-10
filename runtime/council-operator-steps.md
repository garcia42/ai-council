## Steps

### Active study routing after the prospective restart

Before every Council command, run the installed cumulative operations report
and select the study explicitly. New review collection uses:

```
python3 /home/trader/.claude/knowledge/council-eval/predictions_report.py \
  --study council-fresh-20260910 <capture-command> ...
```

The historical store is `--study council-legacy`. It is closed to initiation,
attempt, completion, activation, renewal, artifacts, overrides, supersession and
repair. Use it only for read-only reports, an external snapshot, or a governed
resolution of an already-issued V1/V2 outcome. Never omit `--study` on a live
mutation and never fall back to V1 in the fresh study.

Run `study-operations-report` without a study selector. Supply the installed
blind-criterion digest and the old issuance digest from the actual closure
receipt. Both digests must match the final release config. Its study results stay separate while grading obligations, blind-seat
availability and spending remain cumulative.

The approved fresh maintenance timer is
`2026-09-11..17 00,12:30:00 UTC` (00:30 and 12:30 UTC), expires at
`2026-09-18T00:00:00Z`, and has a 16-cycle ceiling. The per-cycle limits are
1,024 objects, 2,049 adapter calls, 64 MiB and 30 minutes. Use the separate
`council-fresh-capture-evidence.service` and `.timer`; never retarget the
historical unit.

Install the fresh service, non-persistent timer and fresh alert unit with the
timer disabled. Run the real prepare cycle and generation-pinned GCS restore
before the final cutover lock. Missed timer slots are deliberately skipped.

At cutover, open file descriptor 8 on the lock's parent directory and file
descriptor 9 on the shared evidence lock. Hold exclusive `flock`s on both
continuously from the final old-ledger digest through
installation, replacement of these instructions, activation, and verified
closed-route refusal. Pass the inherited descriptors only to `capture-activate`
as `--preheld-coordination-parent-fd 8 --preheld-coordination-lock-fd 9`;
other Council commands would try to
reacquire the lock and must not run inside the hold. Read the activation row
directly for the in-lock acknowledgment. Release both descriptors only after the
installed route is verified. Then run the immediate renewal and enable the
fresh timer only after it succeeds. A rollback stops and disables the fresh
timer, restores the predecessor while both collection routes remain held, and
does not authorize resuming the old default writer. The fresh
maintenance unit and config require their own exact source/config hashes and a
principal-approved future window. Never reuse the September 8-14 timer or the
September 15 expiry for a fresh activation.

1. **Establish what is under review -- commit it first.** A council reviews a *named
   commit range*, never a working tree. `git diff` alone shows neither staged changes nor
   **untracked files**, so a review driven off it silently misses whole new modules: on
   2026-08-23 a change whose core was a new 775-line file would have been reviewed as 321
   lines of edits to existing ones. An uncommitted tree can also move under the seats
   while they read it, and leaves the row unable to say what was read.

   So: commit the work on the session branch, then convene on the range.

   ```
   git -C <worktree> status --short                    # must be clean
   git -C <worktree> log --oneline <base>..HEAD
   git -C <worktree> diff <base>..HEAD                 # this is what the seats read
   ```

   `<base>` is the branch point -- `git merge-base origin/main HEAD`, or `origin/develop`
   for pysystemtrade. Anything still untracked or unstaged is **not under review**; commit
   it or say explicitly that it is out of scope.

   Record the reviewed commits in the row's `commits` field as a **JSON array of full
   40-character SHAs**, so the row states exactly what was read and can be joined back to
   git history:

   ```
   git -C <worktree> rev-list <base>..HEAD | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().split()))'
   ```

   Existing rows are inconsistent here -- 30 objects, 19 nulls, 15 arrays as of
   2026-08-23 -- so the array is the form to write from now on. An object like
   `{"base": "<sha>"}` does not say which commits were reviewed.

   If there is no diff yet, review the decision instead.

2. **Classify the change.** This decides whether the blind seat is seated:

   For an operational change, include one bounded path in the review packet: producer,
   handoff, consumer, failure, recovery, actual CLI and execution identities. Link prior
   unresolved findings and source-bound operator closure evidence where available
   (`docs/QUALIFICATION_EFFICIENCY.md`). Ask seats to check adjacent paths exposed by
   the repair. Operator family links and closure dispositions never replace original
   findings, current verdicts, or independent qualification. Review the named candidate;
   do not attribute a defect introduced by a later repair to an earlier review.

   | Shape | Three lenses | Blind seat |
   |---|---|---|
   | Bug fix, mechanical refactor, test-only | yes | **no** — it cannot see the code; a diff is the lazy brief its skill warns about |
   | New mechanism, threshold, or policy | yes | **yes** |
   | Anything published to clients or the public (SLO snapshot, track record, client note) | yes | **yes** |
   | Capital, fee, capacity, universe, client-onboarding, infrastructure posture | yes | **yes**, role `allocator` |
   | Pure business/compliance document | no (they have no purchase on prose) | **yes** |

   If you seat it, pick the role: `allocator` for business / client / fee / capacity /
   operational-readiness; `generic` for everything else.

3. **Fire all four concurrently, in a single message.** Three `Agent` calls plus one
   `Bash` call — not sequentially, and the blind seat must never see the lenses' output
   nor they its.

   - `pysystemtrade-expert` — code / upstream convention: would upstream accept this,
     idioms, recurring rejection reasons, known gotchas.
   - `qoppac-blog-expert` — Carver theory: costs, optimisation, risk, sizing doctrine.
   - `et-futures-journal-expert` — live ops: IB pacing and market-data limits, execution
     interference, what breaks at 3am, recalibration follow-ups.
   - Blind seat:
     ```
     /home/trader/.claude/skills/blind-seat/ask_blind.sh <role> <brief-file>
     ```

4. **Write the blind brief properly — this is the binding constraint.** Under a page:
   the facts available *now*, the options, the constraint that binds, and the question.
   **Never** the verdict you or the lenses already reached; that destroys the
   independence you are paying for. Write it to a file under
   `~/.claude/knowledge/council-eval/briefs/` so it can be re-scored later. Bind one
   immutable brief path to one logged question; never reuse a brief path for a separate
   council row, even when the facts overlap. A lazy brief produces a generic answer and
   has already failed the seat's kill criterion while looking busy.

5. **Run the matching tests.** A green diff is not a green suite:
   `pytest <dir>/tests/test_<base>*.py tests/test_<base>*.py` for every changed source.
   Report the actual result, including failures.

6. **Report, in this order:**
   - **Verdict table** — lens, APPROVE / CONCERN / BLOCK, one-line reason. A merge is
     eligible only when at least two of the code, theory, and operations lenses return
     exactly `APPROVE` and none returns `BLOCK`. A qualified or conditional response is
     not an `APPROVE`. The independent blind-seat requirements remain a separate gate;
     that seat is not one of the three counted lenses.
   - **Independent blind seat** — its own section, labelled as having seen neither the
     lenses nor this machine. Its answer, then explicitly: where it agrees with the
     lenses, and where it does not. Agreement from an independent seat is evidence;
     disagreement is a reason to go get more evidence, not something to average away.
     Sanity-check any claim it makes about platform mechanics — it cannot see the code
     and is occasionally confidently wrong there (1 in 7 in evaluation).
   - **Follow-ups** — non-blocking items, named per lens.
   - If you skipped the blind seat, **say so and say why**. Never drop it silently.

7. **Log it.** `complete` already appended the `kind: "council"` row to
   `/home/trader/.claude/knowledge/futures-panel-log.jsonl` when you sealed the council
   ("Seal and append the completed council", above). **Do not append it again by hand.**
   A second row carries no forecasts and reuses the same brief path, which the kill
   criterion below then rejects for brief reuse -- on a row the append-only ledger gives
   no sanctioned way to remove. This step is about getting the blind-seat block
   *right in the completion spec*, not about writing a line.

   The shape `complete` writes, and the fields you supply in `councilFields`:
   ```json
   {"ts":"<date -u +%Y-%m-%dT%H:%M:%SZ>","kind":"council","question":"<what was reviewed>",
    "commits":["<full 40-char commit SHA>","<...>"],
    "verdicts":{"code":"...","theory":"...","ops":"..."},
    "blindSeat":{"role":"generic|allocator|SKIPPED","brief":"<unique path>",
                 "required":true|false,"ran":true|false,
                 "notRequiredReason":null|"<applicable exemption and rationale>",
                 "agreedWithPanel":true|false|"partial"|null,
                 "changedDecision":true|false|null,"blockedReason":null|"<reason>",
                 "note":"<what it surfaced, or why skipped>"}}
   ```
   Record `required: true` for every decision-shaped council and `required: false` only
   when the classification table explicitly exempts the blind seat. Every
   `required: false` row must carry a non-empty `notRequiredReason` naming that exemption
   and why the change has no decision surface; `required: true` uses null or omits the
   field. An exempt non-run still records `role: "SKIPPED"`, `ran: false`, null agreement
   and decision effect, plus a `blockedReason` stating that the classification made the
   seat not required. For a completed call,
   record `ran: true`, a Boolean `changedDecision`, and a null or omitted
   `blockedReason`. For a launcher failure, timeout or other required non-run, record
   `role: "SKIPPED"`, `ran: false`, `agreedWithPanel: null`,
   `changedDecision: null` and a non-empty `blockedReason`. Never encode instrument
   availability as a negative result.

   `changedDecision` is what the blind seat's pre-committed kill criterion is scored on.
   Its denominator is **only rows where `blindSeat.ran == true`**: if after 10 completed
   runs it has never changed a decision or surfaced something acted on, retire it — remove
   it from this skill and from `/futures-panel`, and delete the `blind-seat` skill. That
   criterion was written on 2026-08-06 precisely so it could not be argued away later.
   Honour it.

   The tally also treats two or more consecutive required non-runs as
   `BLOCKED_DEGRADED` and exits non-zero. This is an availability alarm, not part of the
   kill-criterion denominator.

   `complete` refuses a `blindSeat` that breaks these couplings, so a refusal here means
   the spec is wrong, not the tool. Correct the spec and re-run `complete`; never edit the
   ledger JSONL to get past it.

   Immediately after `complete` returns, run:
   ```
   python3 /home/trader/.claude/knowledge/council-eval/blind_seat_kill_criterion.py
   ```
   Surface any non-zero result in the council report. Exit 1 is an invalid ledger; exit 2
   is `BLOCKED_DEGRADED` and blocks a decision-shaped gate until a required seat completes.

For runtime replacement, seal this council's completion under the existing pin before
installing anything. Verify a passing rehearsal of the exact reviewed runtime candidate,
install those identical bytes, then run the newly pinned report and blind tally. Preserve
other in-flight councils' pins until their completions are sealed.
