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
