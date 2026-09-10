# Fresh Council study routing

## Problem

A prospective restart needs distinct evidence stores without losing historical
resolution obligations, failed attempts or operational counters.

## Root cause

Historical CLI defaults independently selected ledger and artifact paths. A new
study name alone did not prevent an old default from receiving new observations.

## Implementation

The global `--study` option, before the subcommand, selects exactly
`council-legacy` or `council-fresh-20260910`. The former is closed to collection;
the latter permits V2 collection. Ledger and sidecar paths for the fresh study
are under `.claude/knowledge/council-eval/studies/council-fresh-20260910`.
Its artifacts and controls are under
`/var/lib/ai-council-evidence/fresh-capture-20260910-v2`, outside the account Git
worktree; `ArtifactStore` rejects a root below any Git boundary. The hash-bound
maintenance config remains under
`.local/state/council-tools/studies/council-fresh-20260910`.
Paths use the OS account home, not the `HOME` environment variable. Both studies
retain `.local/state/council-tools/evidence.lock`.

Explicit path arguments must exactly match the selected route, even when an
argument equals an old default. Unknown studies, conflicting repeated arguments
and symlink aliases refuse before command dispatch. Closed collection permits
read-only reporting, historical V1/V2 sidecar resolutions and snapshots to an
external destination. It rejects new attempts, completions, renewal, activation,
artifacts, overrides and retrospective repair. Fresh collection rejects V1
issuance. A missing selector refuses protected live mutations; explicit local
fixtures remain usable. Auxiliary destination writers do not support selected
study mode and cannot use its absence to write into protected stores.

Selection leaves existing host, installed-source, clock and transaction checks
in force. Path checks are point-in-time admission checks, not a claim of
hostile-filesystem custody. Existing descriptor-pinned transactions and the
shared evidence lock remain necessary. Old writers must actually be quiesced
at cutover; installing new CLI code cannot fence an already-running old process.

The evidence maintenance driver validates its `study`, ledger, both sidecars,
artifact root and shared lock before creating a cycle. It passes the same study
to the installed wrapper. Closed or mismatched maintenance targets refuse;
wholly external fixtures remain permitted.

## Diagnosis steps

Use the installed wrapper with `--study council-legacy report --json` and
`--study council-legacy capture-report` for historical views. Use the fresh
selector for prospective results. An empty study has no activation and its
capture report refuses; creating its paths is not activation.

After actual closure and fresh activation, invoke the installed wrapper's
`study-operations-report --criterion-sha256 HASH --legacy-ledger-sha256 HASH`
without a study selector. Supply the installed blind criterion hash and the
closed issuance hash retained in the actual cutover receipt. This source-pinned
report requires both ledgers and all sidecars and reads under their shared lock.
It verifies the closed issuance digest and executes the exact hash-verified
installed criterion bytes. Missing or invalid inputs refuse with exit 1.

Research reports remain separate. The operational view sums historical and fresh
blind counts, carries consecutive required non-runs across the boundary, rejects
cross-study run/brief identity reuse, and preserves existing grading-debt gates.
Old health remains visible as historical evidence. Old debt, cumulative blind
degradation or unhealthy fresh capture blocks finalization with exit 3. Exit 0
requires all of these gates to pass. Spending remains in the separate cumulative
budget receipts; this report does not reset or replace that accounting.

## Solutions and prevention

Install this combined routing, inherited audit assignment repair, maintenance
binding and operational reporting only after exact-source tests, release Council
and copied-store cutover/rollback rehearsal. Update the operational runbook to
call both per-study views and the cumulative report before and after reviews.
Do not deploy the route helper alone or resume an old driver after closure.

At cutover, retain the old health result honestly, quiesce all collection writers,
seal actual issuance bytes, then install the final reviewed runtime before a
fresh evidence-gated activation. Historical resolutions may continue in their
own sidecars. Preserve old failures, families, assignments, spending and immutable
records. Never rename families, backfill observations or silently use V1 capture.

## Related files

- `src/council_tools/study_routes.py`
- `src/council_tools/cli.py`
- `src/council_tools/study_report.py`
- `src/council_tools/capture_runtime.py`
- `operations/capture_evidence_cycle.py`
- `tests/test_study_routes.py`, `tests/test_cli.py`, `tests/test_study_report.py`
- `tests/test_capture_evidence_cycle.py`

## History

2026-09-10: Implemented the principal-selected prospective restart. Collection
state in code describes the intended policy; it is not a closure receipt or live
activation evidence. Deployment and the new study clocks remain separate gates.

2026-09-10: Moved fresh artifact, control and cycle custody to `/var/lib` after
the first real prepare correctly refused the original path beneath the account
Git worktree. A 22-byte synthetic artifact written by an incompletely isolated
test remains in the first external namespace; the final route uses the pristine
`fresh-capture-20260910-v2` namespace. Neither failed namespace was replayed.
