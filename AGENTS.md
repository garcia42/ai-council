# AI Council agent contract

Before substantive work, run:

```sh
python3 plugins/ai-council-run-guard/scripts/run_guard.py doctor --probe
```

The checked-in `.ai-council/run-guard.json` policy applies to Codex work in this repository.
Do not disable, bypass, or loosen it to complete a task. The hook writes runtime state and
handoffs outside the working tree under the Git directory.

Freeze the current acceptance contract before implementation. Only P0 and P1 findings block
that contract; retain P2 and P3 as backlog. Stop when the hook says to stop and return the
machine-written `NOT READY` handoff path.

Those severities grade findings, not tickets: ticket priority is `P0` or `P1` only.

For work spanning multiple tickets or ending in runtime activation, freeze an
`initiativeScope` before implementation and supply current
`initiativeScopeEvidence` at every ticket admission. "Continue autonomously"
does not expand that envelope. Stop with `SCOPE_REVIEW_REQUIRED` when a
cumulative budget is exceeded, the critical path grows at two consecutive
checkpoints, or an unapproved daemon, unit, schema, journal, IPC protocol,
framework, or runtime dependency appears.

Every newly discovered finding must record observed evidence, the exact bounded-canary
failure, maximum consequence, existing-control gap, smallest mitigation, acceptance test,
classification, severity, and disposition. A P2/P3 finding cannot block canary.
`INDUCED_BY_DESIGN` cannot silently become more implementation: if its mitigation expands
the design, disposition it as a principal scope choice. `POST_CANARY_HARDENING` is backlog
or rejected.

Sizing a ticket follows `runtime/ticket-sizing-contract.md`. Give each seat
`sizing_projection(contract)` and its `sizing_projection_sha256` — never `points` or
`priority`, which are what the seat's own review derives. Showing a seat a proposed value
for them is the anchoring failure the projection exists to prevent.
