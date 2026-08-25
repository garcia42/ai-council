# Bounded-run guard

The run guard prevents a difficult task from silently becoming a ten-hour review-and-repair
loop. It is a local, deterministic control: Codex lifecycle hooks count bounded resources,
compare repeated tool outcomes, preserve state across compaction, and deny further tool calls
when the contract says to stop.

It does not lower the acceptance bar. It changes what happens when the bar cannot be met within
the agreed budget: the result is a machine-written `NOT READY` handoff instead of more unbounded
agent work.

## Default contract

| Control | Limit | Behavior at the limit |
| --- | ---: | --- |
| Checkpoint | 90 minutes | Write a checkpoint and require a restated acceptance contract |
| Time lease | 4 hours | Deny more tools and write a handoff |
| Renewal | 120 minutes maximum | Accept only from an actual `UserPromptSubmit` event |
| Review/repair rounds | 2 | Deny a third round |
| Lifetime agent starts | 8 | Deny the ninth start |
| Full qualification runs | 2 | Deny a third full-suite/end-to-end run |
| Exact unchanged repeat | 3 consecutive occurrences | Require one materially different replan |
| Alternating cycle | `A/B` repeated 3 times | Require one materially different replan |
| Replans after a stuck signal | 1 | Stop if a stuck signal recurs |
| No-progress review rounds | 2 | Treat as a stuck signal |
| Blocking severities | P0 and P1 only | Preserve P2/P3 as backlog |

These severities grade **review findings**, not tickets. Ticket priority is a
separate scale with only `priority:P0` and `priority:P1` — a P2 ticket cannot
exist, while a P2 finding is the ordinary non-blocking backlog. See the priority
rubric in `runtime/ticket-sizing-contract.md`.

The versioned source of truth is [`.ai-council/run-guard.json`](../.ai-council/run-guard.json).
The parser rejects missing or extra keys, non-positive limits, renewal windows above two hours,
and any attempt to make P2 or P3 blocking.

## Why there are two hook routes

AI Council checks in both routes intentionally:

1. [`.codex/hooks.json`](../.codex/hooks.json) makes the policy part of this repository. A fresh
   clone does not need a separately installed plugin for the hook definition to be discoverable.
2. [`plugins/ai-council-run-guard`](../plugins/ai-council-run-guard) is a portable plugin for other
   repositories. Its hooks are inert unless the current Git root contains
   `.ai-council/run-guard.json`.

Codex can load both routes at once. Event IDs make delivery idempotent, so an agent start or test
run is counted once rather than twice.

Codex requires a user to review and trust non-managed hooks by exact hash. That is a security
boundary, not something this repository should bypass. For organization-wide enforcement that
users cannot disable, administrators must distribute the same script through managed Codex
`requirements.toml`, pin hooks on, and optionally allow only managed hooks. Repository code alone
cannot create that administrative guarantee.

## Fresh-machine setup

After cloning the repository:

```sh
cd ai-council
python3 plugins/ai-council-run-guard/scripts/run_guard.py doctor --probe
```

Open the repository in Codex and approve the exact checked-in project hook when Codex presents its
trust review. Start a new thread after installing or updating hook/plugin code.

To make the portable plugin available to other repositories with the Codex CLI:

```sh
codex plugin marketplace add "$(pwd)"
codex plugin add ai-council-run-guard@ai-council
```

The repository marketplace marks the plugin `INSTALLED_BY_DEFAULT`. The explicit `plugin add`
command is still useful as a deterministic bootstrap check and on clients that do not reconcile
the policy immediately after adding a marketplace.

Every opted-in target repository needs its own `.ai-council/run-guard.json`. Copy the AI Council
policy first, then change limits only through ordinary review; do not mutate runtime state to
evade a stop.

## Runtime behavior

`SessionStart` announces the remaining lease and restores state after resume or compaction.
`PreToolUse` enforces elapsed time and resource counters before a supported local tool runs.
`PostToolUse` compares normalized action and outcome fingerprints, detects exact and alternating
loops, and observes real Git working-tree/HEAD progress. `UserPromptSubmit` is the only route that
can renew an expired time lease. `SessionEnd` writes a final handoff.

Parallel reviewer seats are grouped by the current repository progress generation. Seat briefs
can include `review-round-N` to identify a new round explicitly, including a deliberate rerun on
an unchanged candidate.

State and artifacts do not dirty the repository. In Git worktrees they live under the shared Git
directory:

```text
<git-common-dir>/ai-council-run-guard/
  sessions/<session-id>.json
  checkpoints/<session-id>-<run-id>-checkpoint.md
  handoffs/<session-id>-<run-id>-handoff.md
```

Writes use an exclusive file lock and atomic replacement. Hook events are cached by stable event
ID to tolerate duplicate project/plugin delivery.

The handoff records the reason, run and session IDs, lease, last semantic progress, Git HEAD,
working-tree state, counters, replans, P0/P1 blockers, and P2/P3 backlog. It is an operational
handoff, not evidence that the underlying task passed.

## Operator commands

Validate all checked-in routes and exercise the real stuck-to-replan-to-stop state machine:

```sh
python3 plugins/ai-council-run-guard/scripts/run_guard.py doctor --probe
```

Inspect the most recently updated run:

```sh
python3 plugins/ai-council-run-guard/scripts/run_guard.py status
```

If more than one Codex session has state in the repository, `status`, `progress`, and `finding`
fail closed until `--session-id <id>` is supplied. The hook's session-start context reports that
ID; the CLI never guesses based on whichever session wrote most recently.

Record progress that is semantic but does not change Git state:

```sh
python3 plugins/ai-council-run-guard/scripts/run_guard.py progress \
  "independent reproduction disproved the original failure hypothesis"
```

Record review findings so the generated handoff separates blockers from backlog:

```sh
python3 plugins/ai-council-run-guard/scripts/run_guard.py finding P1 \
  "the accepted path can overwrite evidence"
python3 plugins/ai-council-run-guard/scripts/run_guard.py finding P2 \
  "error copy should be clearer"
```

After an elapsed-time stop, a real user may extend the existing run without resetting counters:

```text
CONTINUE BOUNDED RUN FOR 120 MINUTES: finish the already accepted P1 fix
```

The hook rejects a longer renewal. Time renewal cannot override a stuck-loop or counter stop.
A different real-user prompt after expiry starts a new bounded run and leaves the prior handoff
on disk.

## Detection boundary

The guard can observe Bash, `apply_patch`, MCP calls, agent starts, and most other local Codex
tools. Hosted tools and specialized paths may not traverse local tool hooks. Review-round and
full-qualification classification therefore uses checked-in regular expressions as well as the
hard agent-start and elapsed-time caps.

Elapsed time is checked at lifecycle boundaries. A single already-running tool cannot be killed
mid-call by `PreToolUse`; the next supported hook event stops the run. External process timeouts
remain necessary for individually long shell commands.

This is a strong repository guardrail, not a security sandbox. A process outside Codex can edit
the policy or state, and a user who controls local Codex configuration can disable non-managed
hooks. Use managed hooks for an adversarial enforcement boundary.
