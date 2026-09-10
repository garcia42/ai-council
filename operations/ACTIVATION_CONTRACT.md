# Usefulness activation contract — 2026-09-07

## Fresh prospective restart contract — approved 2026-09-10

The principal approved the exact fresh maintenance window in
`PRINCIPAL_FRESH_WINDOW_APPROVAL.json`: scheduled renewals run on
`2026-09-11..17 00,12:30:00 UTC` (00:30 and 12:30 UTC), authorization expires at
`2026-09-18T00:00:00Z`, and the first checkpoint is September 17. The ceiling
is 16 cycles: one preparation, one immediate service renewal, and 14 scheduled
renewals. Each cycle is limited to 1,024 objects, 2,049 adapter calls,
67,108,864 bytes and 1,800 seconds. Reserve $0.10 per attempted cycle, or
$1.60 total, within the approved $2 planning allowance.

The fresh study is `council-fresh-20260910`. Its ledger and sidecars are under
`/home/trader/.claude/knowledge/council-eval/studies/council-fresh-20260910`;
its artifacts, controls and cycles are under
`/var/lib/ai-council-evidence/fresh-capture-20260910-v2`, outside the account's Git
worktree. Its hash-bound config is under
`/home/trader/.local/state/council-tools/studies/council-fresh-20260910`.
Both studies retain the single lock
`/home/trader/.local/state/council-tools/evidence.lock`.

The new `council-fresh-capture-evidence.service` runs only from the final
reviewed `/home/trader/council-tools` commit and an exact hash-bound fresh
config. Its timer is `council-fresh-capture-evidence.timer`. The historical
unit is disabled before the old issuance seal and is never retargeted. The
fresh timer is non-persistent, installed disabled, and enabled only after the
activation acknowledgment and successful immediate renewal. The fresh config
binds the external blind-criterion bytes used by the global report.

## Historical activation contract

> Historical activation only. The option D prospective restart preserves this
> contract and its BLOCKED evidence but does not reuse its ledger, timer dates,
> September 15 expiry, config digest, ACTIVE.json or maintenance root. A fresh
> contract must bind `council-fresh-20260910`, the final reviewed runtime and
> operations source, a principal-approved future window, the shared evidence
> lock, and limits of 1,024 objects, 2,049 adapter calls, 64 MiB and 30 minutes.
> The shared lock remains exclusively held from the historical issuance seal
> through installed closed-route verification. Rollback keeps collection held.

User authority: “Ok, finish activation” after the measurement status and remaining activation steps were explained. Runtime stays at reviewed and rehearsed commit 4ca5c08b97e1ef10ed2c3c5d585a6747275c658c. This operational package does not replace it.

- Prepare fresh source-bound audit and real GCS readback/restore certificates using the existing adapters; preserve failed namespaces and ambiguous outcomes without automatic replay.
- Use /var/lib/ai-council-evidence/live-capture-20260907 outside Git, the existing live evidence lock, and the installed wrapper for activation, renewal and reporting.
- Keep study thresholds and historical rows unchanged. Set the prospective four-month anchor from the actual activation time, not the August design date; retain October 31 as the existing capture-soak deadline.
- Route all new council attempts through V2 after activation, with exact visible input/output custody, operator handling-time intervals, and original seat findings. No synthetic demonstration is credited as an observation.
- Operate a 12-hour evidence renewal timer on manny. Bound each cycle to a new GCS prefix, at most 512 objects, 64 MiB each direction, 1,025 adapter commands and 30 minutes. Disable gcloud storage retries; cap each subprocess at 60 seconds. Refuse uncertain previous cycles until reconciled. No bucket configuration change, overwrites, public ACLs or remote deletes.
- Prepare a first-week maintenance authorization ending September 15 UTC, at most 16 cycles including initial preparation. Reserve $0.10 per attempted cycle against a $2 planning allowance; this is an execution reservation, not a GCP billing cap. Refuse larger snapshots and report failure. Later continuation needs an explicit extension after reviewing capture burden.
- Validate the concrete operational package, current-source readiness and council approval before live cohort append. Install the timer and capture instructions only after the operational review.
- Immediately after the installed writer appends activation, the activating operator re-reads the live ledger and verifies the exact activation ID and approval-manifest digest against the prepared cycle. Only then create ACTIVE.json and that prepare cycle's complete.json, recording the actual ID, manifest digest, activation timestamp and acknowledgment time. The prepare cycle intentionally cannot mark itself complete before this handoff. Verify both files and run the first renewal through systemd before enabling the timer.
- Completion means source pin verified, real off-host restore proof current, timer enabled with next run, cohort activation appended, live capture report readable, instructions routed to V2. Zero observations remain zero until the next genuine council begins.

Controls: installer/wrapper sets and verifies source identity; only an explicit runtime replacement changes it. The evidence and ledger locks are acquired/released by each transaction; renewal does not remove the locks. The timer is enabled/disabled by the operator; its deadline and unresolved-attempt refusal cannot be cleared by successful health reporting. Capture activation is an immutable ledger row. Artifact digests are immutable. The GCS adapter uses generation-match-zero creation and exact-generation reads. The existing source/retention policy cannot be relaxed by renewal.
