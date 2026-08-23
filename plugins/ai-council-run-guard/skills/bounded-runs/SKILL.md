---
name: bounded-runs
description: Keep long Codex, council, review, and qualification work within the repository's deterministic run budgets. Use when work may involve agents, repeated repair rounds, full-suite reruns, or a long autonomous task in an opted-in repository.
---

# Bounded runs

Run the repository doctor before substantial work:

```sh
python3 plugins/ai-council-run-guard/scripts/run_guard.py doctor --probe
```

Treat the checked-in policy and hook state as authoritative. The default contract is a
90-minute checkpoint, four-hour hard stop, two review/repair rounds, eight lifetime agent
starts, two full qualification runs, one replan after a stuck signal, and a principal-only
renewal of at most 120 minutes.

Freeze the acceptance contract before the first broad implementation or review. A review
round is one collection of reviewers working from the same candidate, not one round per
seat. A qualification is a complete suite or end-to-end gate; focused diagnostics do not
consume it.

Include `review-round-N` in review seat task names or briefs. The guard otherwise groups
parallel review seats by the current repository progress generation.

Record semantic progress when it is not visible from the Git working tree:

```sh
python3 plugins/ai-council-run-guard/scripts/run_guard.py progress "what materially changed"
```

Use the session ID from the hook context with `--session-id` when more than one Codex
session has state in the same repository.

Record review findings with `finding P0|P1|P2|P3 "summary"`. Only P0 and P1 block the
current acceptance contract. Preserve P2 and P3 in the handoff or backlog; they do not
justify an additional repair round.

At a replan signal, state the failed assumption and choose one materially different action.
At a stop, do not call more tools. Return a clean `NOT READY` handoff using the path written
by the hook. Do not weaken or edit the policy to finish a task.

Only a real user prompt can extend an expired time lease. The exact form is:

```text
CONTINUE BOUNDED RUN FOR 120 MINUTES: concrete reason
```

Renewal extends time only. It never resets counters or stuck-loop history.
