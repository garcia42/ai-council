# Qualification and review efficiency

Review a bounded operational path before expensive qualification: producer, handoff,
consumer, failure and recovery. Include the actual command interface and execution
identity. For each relied-on control record its setter, clearer and owner, including
whether the proposed repair clears it. Do not claim a later candidate's defect was
visible in an earlier candidate.

Use operator closure worksheets to avoid rediscovering the same unresolved issue.
`PYTHONPATH=src python3 -m council_tools.closure --worksheet worksheet.json
--evidence-root /absolute/evidence` reads files and emits a provenance report. It does
not write the council ledger, change a verdict, resolve a forecast, or approve a merge.

The version-1 worksheet contains `schemaVersion: 1` and an `entries` array. Each entry
has `familyId`, `source`, `sourcePointers`, `runId`, `candidateCommit`, `finding`,
`repairedRange`, `reproduction`, `positiveControl`, `adjacentPaths`, `disposition`, and
`reason`. `source` and control artifacts use `{path, bytes, sha256}` references relative
to the evidence root. `sourcePointers` maps `runId`, `candidateCommit`, and `finding`
to their JSON pointers in the retained original source. All three must equal the
worksheet, including every one of the seven seat-owned finding fields. An original
artifact lacking those fields cannot acquire them through an operator reconstruction.
Keep historical summaries in a separate descriptive worksheet with missing provenance
explicit; they cannot pass this validator as original atomic findings.

`familyId` is an operator hypothesis linking paths across runs, not a seat finding group.
`repairedRange` is null or `{base, head}` full commits; controls are null or artifact
references. `OPERATOR_CLOSED` requires both controls and a repair range. `OPEN` and
`DEFERRED` preserve missing work. Adjacent paths and a reason are always required.
The validator proves bytes and references, not the truth of a technical closure or
ancestry of the stated range. A reviewer still evaluates the reproduction, positive
control, and repair. Closure is never a substitute for current source qualification,
two exact APPROVE verdicts with no BLOCK, the independent blind gate, or the forecast
contract. Counts describe operator dispositions, not causal usefulness or seat quality.

## Durable local test jobs

`python3 -m council_tools.qualification_jobs` provides explicit `init`, `submit` and
`report` commands, each with `--root /absolute/operator-owned/job-directory`. Initialize
once with `--spec capacities.json`, for example `{"cpu": 4, "systemd_fixture": 1}`.
These are named reservation units, not measured CPU or memory limits. Every cooperating
job that shares a fixture must use the same directory and resource name. Ordinary tests
can reserve CPU units while a systemd job reserves its fixture. There is no global one-job
rule, and this tool cannot account for unrelated jobs that do not participate.

Submit with `--spec job.json`:

```json
{
  "programId": "example-qualification",
  "argv": ["/absolute/venv/bin/python", "-B", "/absolute/repo/test_runner.py"],
  "cwd": "/absolute/repo",
  "inputs": ["/absolute/repo/test_runner.py", "/absolute/evidence/source-manifest.json"],
  "environment": {"TANDR_V21_OFFLINE_ONLY": "1"},
  "resources": {"cpu": 2}
}
```

The caller must list the relevant input files and use its canonical source-bound runner
and verifier. Hashing a manifest does not itself verify files named inside that manifest.
This tool never infers qualification scope. Do not put secrets in the explicit specification;
inherited environment values are hashed rather than retained. Queued jobs recheck input,
executable, supervisor and identity bindings before spawning an argv command. The worker
runs in a separate session, keeps the native child exit (including nonzero or signal exit),
timestamps and log hash, and atomically fsyncs its state. Observer exit does not stop it.

`report` counts each job ID once per program. A completed native zero is process evidence,
not qualification. The canonical receipt verifier still decides whether tests and proofs
are complete. A dead/reused PID or reboot produces `UNKNOWN_SUPERVISOR_LOST`, never zero.
The Linux supervisor adopts orphaned descendants, including children that create another
session. A parent exit with a surviving descendant records the actual parent exit and
`UNKNOWN_DESCENDANTS`, retaining the claim. Admitted claims also remain held after
supervisor loss or ambiguous execution. There is no
automatic stale-lock expiry or retry. Reconcile the exact process tree and retained evidence
before any manual intervention; do not create another manager to evade the unresolved claim.
The report exposes incomplete work without changing run-guard limits, resetting counters,
renewing a session, replaying a command or granting a new activation authority.

## Status reconciliation

`python3 -m council_tools.status_view --spec view.json --evidence-root /evidence`
derives status axes from a retained existing contract; it creates no competing activation
contract. The version-1 spec has `schemaVersion`, a `contract` artifact reference and
`observations`. Each observation names `contractPointer`, `artifact`, `evidencePointer`,
`disposition` and `reason`. Both artifacts use `{path, bytes, sha256}`. Dispositions are
operator statements: `RECONCILE`, `EVIDENCE_NEWER`, or `NOT_COMPARABLE`.

The report retains declared and observed text separately. Differing scopes or a newer
runtime receipt may explain a difference; it is not automatically a contradiction. Missing
observations remain UNOBSERVED. A hash-valid file can still be stale, and the tool makes
no freshness, installation, approval or runtime-readiness inference. Review unresolved
differences before repeating qualification based on an outdated prose status. Infrastructure
work uses the separate prospective template in `INFRASTRUCTURE_FIXTURE_AUTHORIZATION.md`.
