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

Sizing a ticket follows `runtime/ticket-sizing-contract.md`. Give each seat
`sizing_projection(contract)` and its `sizing_projection_sha256` — never `points` or
`priority`, which are what the seat's own review derives. Showing a seat a proposed value
for them is the anchoring failure the projection exists to prevent.
