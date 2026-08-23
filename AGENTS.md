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
