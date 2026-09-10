# Fresh Council study routing

## Problem

Starting a new prospective study needs distinct evidence paths. Reusing a
historical default can mix new observations with the closed study or send a
resolution to the wrong sidecar.

## Root cause

The existing CLI defaults select the historical store. Explicit path arguments
can override them independently; the defaults do not express study identity.

## Current implementation

`council_tools.study_routes.resolve_study_route` returns an immutable path value
for exactly two IDs. `council-legacy` describes closed collection and retains
the historical ledger, sidecars, external artifact root and control store.
`council-fresh-20260910` describes the prospective collection route: ledger and
sidecars under `.claude/knowledge/council-eval/studies/council-fresh-20260910`,
artifacts and controls under `.local/state/council-tools/studies/council-fresh-20260910`.
Home-relative paths come from the OS account record, not the `HOME` environment
variable. Both routes retain `.local/state/council-tools/evidence.lock`.

Supplied store fields use `log`, `v1_events`, `v2_events`, `artifact_root`,
`control_store` and `coordination_lock`. Omitted fields take the selected value;
an explicit field must match it exactly. Unknown IDs or fields, noncanonical
path strings and symlink redirections fail with `StudyRouteError`. Selecting a
route creates no directories and writes no evidence.

## Diagnosis and integration

The helper alone does **not** route CLI calls, close historical collection or
activate a study. Its `collection_state` describes the proposed write policy,
not an observed activation or retirement receipt. It does not provide a live
writer authorization boundary. CLI integration must preserve whether each path
option was explicitly supplied and distinguish V1 and V2 `--events` arguments.
It must enforce closed-study mutation policy before dispatch and retain existing
host, source, outcome and transaction checks.

Path selection checks are a point-in-time check, not filesystem custody or a
race-proof mutation authorization. Existing dirfd-pinned writer transactions
and evidence locks remain necessary. Hardlink identity, live control custody,
command classification and direct-library writers are outside this helper's
guarantees. A source digest binds these fixed route definitions only when the
exact reviewed package is installed through the existing installation gate.

## Prevention and activation gates

Test wrong-study arguments even when equal to an old default, and test aliases
both in explicit paths and in selected defaults. Before installation, complete
CLI write enforcement, cumulative operational-accounting handling, assignment
repair integration and exact-source qualification. Rehearse old collection
refusal, permitted historical resolutions with unchanged issuance bytes, fresh
activation, one-writer exclusion and rollback. Preserve old evidence and all
spend; do not deploy this helper as a partially completed restart.

## Related files

- `src/council_tools/study_routes.py`
- `tests/test_study_routes.py`
- `src/council_tools/cli.py`
- `src/council_tools/safe_files.py`

## History

2026-09-10: Added source-pinned route resolution for the principal-selected
restart of both studies. CLI integration and live activation remain separate.
