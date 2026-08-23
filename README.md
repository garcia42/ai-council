# AI Council

**An evidence layer for finding out whether a multi-agent review council is actually useful.**

AI Council records what each reviewer saw, what it returned, how the operator dispositioned its
findings, and how every reviewer priced the same future outcome. Later, it can measure forecast
accuracy, finding overlap, capture quality, and grading debt without pretending those are all the
same thing.

The central question is deliberately uncomfortable: are four agents catching different, useful
problems—or are they correlated copies producing expensive reassurance?

## Project status

| Capability | Status |
| --- | --- |
| V1 append-only forecasts and Brier reporting | Operational |
| V2 core lifecycle, exact custody, and finding capture | Implemented and tested |
| V2 integrity-aware capture health and exogenous-only Brier | Implemented; not activated |
| Bounded Codex runs, stuck detection, and durable handoffs | Implemented; repository hook plus portable plugin |
| One-in-five independent finding audit | Implemented and tested; prospective counts begin only after activation |
| Off-host durability age and verified-restore evidence | Implemented and evidence-gated |
| Content-manifested local snapshot and clean restore | Rehearsal-only |
| Live V2 capture activation | Evidence-gated; not activated by install or this release |
| Seat ranking, weighting, admission, or retirement | Deliberately not implemented |

Installing this repository does not activate V2 on a live ledger. The first prospective cohort is
frozen in advance, and activation remains a separate operational decision.

## Why this exists

Multi-agent review has several easy failure modes:

- every seat repeats the same underlying model bias;
- consensus looks like independent confirmation when it is not;
- long reviews create a feeling of safety without changing the decision;
- successes get remembered while abandoned runs and missed grades disappear;
- a seat with good forecasts may still contribute no novel, actionable findings.

AI Council collects the evidence needed to test those failure modes. It separately reports forecast
accuracy, seat-supplied finding evidence summaries, operator-reported novelty, within-run overlap,
latency, cost, and capture completeness. It does not independently adjudicate evidentiary support,
and it does not yet estimate error correlation, statistical independence, or
replaceability, and it never collapses the measures into a composite “council score.”

## Two additive contracts

### V1: forecast discipline

Before reviewers answer, V1 appends a price-free `council-attempt` with one material, binary,
resolvable shared outcome. Every submitted seat independently returns a probability for that same
claim. The sealed completion records explicit `submitted`, `abstained`, or `unavailable` states.
Outcomes are resolved later by stable ID and scored with the Brier score.

V1 reports coverage, unresolved outcomes, voids, grading debt, repeated issuances, a constant-50%
reference, and descriptive per-seat Brier scores.

### V2: usefulness evidence

V2 adds a durable lifecycle around each council run:

```mermaid
flowchart LR
    A[Activate frozen cohort] --> B[Durable initiation]
    B --> C[Capture decision baseline]
    C --> D[Capture and bind exact seat inputs]
    D --> E[Launch seats independently]
    E --> F[Record terminal seat states]
    F --> G[Capture exact visible outputs]
    G --> H[Normalize findings and dispositions]
    H --> I[Seal forecasts and completion]
    I --> J[Reverify artifacts and report health]
    J --> K[Resolve exogenous outcomes later]
```

The V2 ledger records:

- a system-timestamped initiation before brief preparation;
- a preassigned decision family and prospective outcome class;
- content-addressed references for the decision baseline, every planned seat's exact visible input,
  and every submitted seat's exact visible output;
- seat role, agent version, agent-definition digest, model, tool policy, repository commit, and
  optional supplied latency/token/cost metadata;
- atomic findings, within-run finding groups, and one forced operator disposition per finding;
- an artifact-bound structured `no-findings` declaration when a submitted seat found nothing;
- an append-only invalidation when custody, identity, timing, disposition, or secret handling fails
  while the process remains able to record it; a crash still leaves the durable initiation visible
  and incomplete.

Only resolved **exogenous V2** outcomes enter the V2 headline Brier summary.
Intervention-sensitive and V1 outcomes remain visible in separate counts and are not silently
promoted.

## Install and test

AI Council requires Python 3.11 or newer and a Unix-like system because it uses `fcntl` locks. It
has no third-party runtime dependencies.

```sh
git clone https://github.com/garcia42/ai-council.git
cd ai-council
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
PYTHONPATH=src:. python -m unittest discover -s tests -v
python3 plugins/ai-council-run-guard/scripts/run_guard.py doctor --probe
```

Codex work in this repository is governed by the checked-in bounded-run policy. It creates a
90-minute checkpoint, stops tool use after four hours without a real-user renewal, caps council
repair and full-qualification loops, and writes a durable `NOT READY` handoff when progress stalls.
See [docs/RUN_GUARD.md](docs/RUN_GUARD.md) for the exact contract, new-machine setup, and the
boundary between repository hooks and administrator-managed enforcement.

For standalone use, always supply explicit local stores. Private prompts and answers belong outside
the repository. When an explicit ledger is outside live council state and `--coordination-lock` is
omitted, the CLI derives a sibling local evidence lock; it never falls back to the live deployment
lock. Supply one explicit shared lock when coordinating several stores or snapshots.

### Installer retained-escrow runbook

`install.py install` and `install.py restore` use atomic Linux name exchange and deliberately do
not auto-delete displaced runtime files. Each successful operation retains one old runtime inode
per managed target—currently four—and records their exact relative paths in the operation backup's
`RETAINED_ESCROWS.tsv`. Replacing an existing backup `LATEST` pointer similarly retains the prior
pointer and records it in `RETAINED_BACKUP_POINTERS.tsv`. The CLI prints the backup and retained
escrow report location. Accumulation is intentional: a mutable namespace cannot support a safe
check-then-delete cleanup.

Treat any custody-report error as authoritative even when publication is already committed; the
error names the state, backup, report, and every retained path known. Do not rerun or remove an
escrow until the installer and all runtime editors are quiescent. In that exclusive maintenance
window, verify the active targets against the intended source commit, verify `MANIFEST.tsv`, and
reconcile each reported escrow with its corresponding backup payload. Only then may an operator
archive or remove the reviewed escrows. The installer never performs that disposition itself.

### JSONL transaction-escrow runbook

Crash-atomic JSONL replacement retains the displaced prior inode instead of using an unsafe
identity-check-then-unlink cleanup. The filename convention is
`.<ledger-name>.<32-hex>.tmp.escrow.<32-hex>` in the ledger's directory. Successful CLI append
responses list newly retained paths in `transactionEscrows`; `report --json`, `capture-report
--json`, and their human output inventory every matching path, its byte size, and aggregate bytes.
Reporters inspect names and no-follow metadata only. They never parse an escrow as ledger evidence.

Monitor `transactionEscrows.count` and `transactionEscrows.aggregateBytes`. Because each append
writes the whole ledger and retains the prior generation, byte usage can grow quadratically with
record count. This is expected custody overhead, not a backup policy, and there is no automatic
deletion or rotation.

Reconcile or dispose of escrows only in a maintenance window with every writer, reporter snapshot,
and restore process quiescent. Preserve an off-host copy first; match each reported path to its
store, confirm it is the expected private regular file, hash and compare it with the applicable
prior ledger generation, and record the operator disposition. Only after that review may the
operator archive or remove the named entries. Restart writers only after rerunning the report and
confirming its inventory matches the recorded disposition.

## V1 quick start

Create an attempt specification:

```json
{
  "question": "Should we deploy the proposed change?",
  "expectedSeats": ["code", "theory", "ops", "blind"],
  "sharedOutcome": {
    "claim": "The change completes its observation window without rollback",
    "resolutionDate": "2027-03-31",
    "resolvedBy": "Inspect the deployment and rollback records",
    "decisionLink": "change-1042",
    "materiality": "A rollback would reject the deployment decision",
    "actionIfTrue": "Retain the change",
    "actionIfFalse": "Revert and investigate",
    "evidenceCutoffAt": "2027-03-01T12:00:00Z"
  }
}
```

Append the attempt, run the reviewers without sharing probabilities, then validate and append the
sealed completion:

```sh
python -m council_tools.cli attempt \
  --log ./council.jsonl --spec ./attempt.json --ts 2027-03-01T12:00:00Z
python -m council_tools.cli complete \
  --log ./council.jsonl --spec ./completion.json --check-only
python -m council_tools.cli complete \
  --log ./council.jsonl --spec ./completion.json
```

Resolve by stable `outcomeId` only after the resolution date has fully ended in America/New_York:

```sh
python -m council_tools.cli resolve outcome-REPLACE true \
  --log ./council.jsonl \
  --events ./resolutions.jsonl \
  --evidence "deployment record 1042; observation window completed" \
  --resolver release-operator \
  --method deterministic

python -m council_tools.cli report \
  --log ./council.jsonl --events ./resolutions.jsonl
```

Resolution time is system-owned. New resolution events also bind the issued outcome fingerprint;
reports reject a changed resolution date, pre-issuance timestamp, or event later than the report's
observation time.

For a binary outcome, Brier is `(forecast probability - observed outcome)^2`. Lower is better:
`0` is perfect, `0.25` is the score from always forecasting 50%, and `1` is a fully confident miss.

## V2 workflow

V2 is intentionally more demanding because it is preserving evidence, not just a score. The
standalone sequence is:

```text
capture-activate          one frozen cohort, once
capture-initiate          first durable write for every attempted run
capture-artifact          exact baseline, prompt, and visible-answer bytes
capture-attempt           uniform-binding and input-manifest preflight
capture-seats-finished    explicit terminal state for every planned seat
capture-complete          custody verification, findings validation, and seal
capture-report            first-ten health and descriptive outcome report
capture-resolve           V2-only resolution sidecar
```

Use `python -m council_tools.cli <command> --help` for exact inputs. Lifecycle timestamps are
system-generated; V2 commands do not accept operator-supplied boundary times. `capture-attempt` and
`capture-complete` require the exact visible input files again and verify them against their
content-addressed references. Structured empty results must be present in the seat's visible JSON
output, not invented later by an operator. Every submitted visible JSON output must also contain a
`capture` object binding the run, outcome, outcome fingerprint, evidence cutoff, forecast-request
digest, seat, input-artifact digest, and integer `sharedProbability`. Every visible input contains
one canonical, machine-readable forecast-request block showing the actual claim, resolution rule,
resolution date, materiality, and actions as well as the run and outcome identities. Its
`forecastRequestSha256` is derived from those disclosed fields. The operator cannot substitute a
different visible question, prompt, or forecast: completion and reporting parse and recheck the
retained bytes. The same `capture` object contains a canonical `findings` list with the exact
seven seat-owned finding fields; the operator assigns cross-seat groups and dispositions
separately, without changing what a seat returned. Completion must occur before the outcome's
resolution date.

The frozen first-ten report counts crashes, abandoned attempts, unavailable seats, invalidations,
rejected completions, and post-activation V1 runs as denominator members. It does not select only
the successful rows. The initial operational bars are at least 90% complete capture and median
active handling time no greater than three minutes.

## Evidence custody and recovery

Artifacts are immutable SHA-256-addressed files under a private root outside Git. Directories are
mode `0700`; files are mode `0600`. Capture and verification reject traversal, symlinks, hard-link
aliases, changed bytes, wrong ownership/modes, and built-in or caller-supplied secret tokens.

All evidence writers use one coordination lock. A local rehearsal snapshot takes a shared lock,
copies the ledger, V2 resolution store, control store, and artifact root, writes its content
manifest last, verifies it, and restores only into a clean target:

```sh
python -m council_tools.cli evidence-snapshot \
  --log ./council.jsonl \
  --events ./capture-resolved.jsonl \
  --control-store ./control \
  --artifact-root /absolute/private/artifacts \
  --coordination-lock /absolute/private/evidence.lock \
  --repository-root "$PWD" \
  --target /absolute/new/snapshot

python -m council_tools.cli evidence-verify /absolute/new/snapshot
python -m council_tools.cli evidence-restore \
  /absolute/new/snapshot /absolute/new/restore-target \
  --repository-root "$PWD"
```

A passing local snapshot is **not** proof of off-host backup. The off-host rehearsal uploads each
verified snapshot member with create-only semantics, records its exact object generation, uploads
the index last, reads those exact generations into a clean tree, and verifies a second clean
restore. Its certificate is useful only while its source, policy, snapshot, restore, and freshness
bindings still pass.

Live V2 activation now uses a fail-closed evidence gate rather than unconditional blocker strings.
The version-2 manifest must bind the installed commit and source digest to the frozen activation,
audit, and durability policies and to valid source-bound rehearsal certificates. The verifier
dereferences every artifact and recomputes freshness, RPO/RTO, provider posture, and restore
integrity at the final locked append boundary. Missing, stale, mismatched, or unrehearsed evidence
appends nothing. Check a candidate manifest without activating anything:

```sh
python -m council_tools.cli activation-readiness \
  --manifest-file /absolute/private/activation-manifest.json \
  --artifact-root /absolute/private/artifacts \
  --runtime-source-commit <installed-commit> \
  --runtime-source-sha256 <installed-source-digest>
```

The one-in-five audit assignment is deterministic and persisted before seats launch. Retries
inherit the family assignment. A selected family's first complete run creates a blinded case and a
separately retained alias map; these audit records never enter lifecycle or Brier denominators.
Two independent adjudicators and the frozen agreement rules are required before audit results can
support any later seat gate. Zero actual audit cases before live activation is expected and cannot
be backfilled from old runs.

## What the reports may—and may not—say

Allowed descriptive outputs include:

- capture fraction and lifecycle failure counts;
- artifact completeness and re-verification failures;
- active and elapsed timing;
- findings per submitted seat;
- operator-reported disposition mix;
- within-run finding overlap;
- an upper bound on unique finding coverage;
- exogenous-only descriptive forecast accuracy and outcome-polarity warnings.

The MVP does **not** establish causal decision value, statistical independence, redundancy,
replaceability, or a reviewer leaderboard. No seat comparison verdict is allowed below 20
capture-complete independent decision families, and later comparison requires separately frozen
adjudication and statistical rules.

## Repository map

| Path | Purpose |
| --- | --- |
| `src/council_tools/forecasts.py` | V1 append-only forecasts, resolution rules, audit, and Brier scoring |
| `src/council_tools/capture_schema.py` | Exact V2 record schemas and lifecycle state machine |
| `src/council_tools/artifacts.py` | Private content-addressed artifact custody and re-verification |
| `src/council_tools/findings.py` | Atomic findings, dispositions, and non-causal summaries |
| `src/council_tools/data_health.py` | Frozen first-ten capture and outcome report |
| `src/council_tools/evidence_backup.py` | Manifested local snapshot, verification, and clean restore |
| `src/council_tools/finding_audit.py` | Prospective one-in-five audit assignment, blinding, and rehearsal |
| `src/council_tools/offhost_durability.py` | Generation-pinned off-host readback and clean-restore rehearsal |
| `src/council_tools/gcs_durability.py` | Narrow create-only Google Cloud Storage adapter |
| `src/council_tools/activation_evidence.py` | Source-bound manifest validation and activation readiness evaluation |
| `src/council_tools/capture_runtime.py` | Cross-module atomic integration and uniform-binding preflight |
| `src/council_tools/cli.py` | Standalone and deployment command-line interface |
| `runtime/` | Pinned Claude-based runtime adapter and operating contract |
| `install.py` | Checked, backed-up, reversible local runtime installer |
| `rehearse.py` | Copied-runtime rehearsal; never writes live evidence |
| `design/` | Preregistration, amendments, author baselines, and design evidence |
| `tests/` | Unit, adversarial, concurrency, integration, installer, and rehearsal tests |

## Design evidence

The prospective measurement contract was frozen before V2 data collection and then amended
append-only when the council found flaws in the proposed denominator, custody, timing, empty-result,
and prompt-binding rules. See
[`design/2026-08-22-council-usefulness-preregistration.md`](design/2026-08-22-council-usefulness-preregistration.md)
and its sibling amendments.

That process is itself a dogfood test: seventeen integrated review rounds caught defects after green
component and full-suite test runs, and each blocked release until another repair. Those events are
retained; they are evidence that the process changed this implementation, not proof that the
council is generally useful.

## Deployment-specific adapter

The checked-in runtime adapter targets an existing Claude-based council under `/home/trader`. It
pins the installed entry point to a clean source commit and source-tree SHA-256, backs up changed
runtime files, rehearses against copied data, keeps a legacy forecasting store isolated, and
restricts live knowledge-store writes to the configured authority host.

That adapter is an auditable real deployment, not a claim of portability. The Python library and
explicit-path CLI are the reusable core.

## License

No license has been selected yet. Until one is added, the repository is source-visible but does
not grant reuse or redistribution rights.
