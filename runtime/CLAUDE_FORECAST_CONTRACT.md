### Council forecast scoring

Every `/council` invocation follows the installed forecast contract in the council skill. It first
records a price-free `council-attempt`, gives every seated reviewer one byte-identical material
shared outcome, seals the returned probabilities, validates the completion record, and appends it
through the version-controlled forecast tool. Never resolve by timestamp or prediction-list index.

Forecast scores are descriptive forecast accuracy, not a reviewer leaderboard. Grading debt
does not stop a new council from convening, but three or more outcomes over 14 days late block
decision finalization unless the principal records a time-limited override. Manual grades require
independent review and durable evidence. A `submitted` seat requires exactly one probability;
`abstained` and `unavailable` are explicit accounted states and require none.

`manny` is the sole authority for live council-ledger and resolution-sidecar writes. Other hosts may
read or rehearse copied data but must not append. Run the report both before launching seats and
after appending the sealed completion. Its exit status is the process-boundary alert: exit 1 stops
the council for invalid state; exit 3 permits collection but blocks decision finalization. Exit 2
is reserved for command-line usage errors.

Grading debt never blocks incident containment or rollback. A rollback preserves the blind-seat
tally's forward-compatible `council-attempt` allowlist because appended attempt rows are permanent.
The runtime reporter is pinned to the installed source commit and source digest and fails closed on
working-tree drift; develop later changes in a separate worktree and activate a new clean commit.

`PANEL_LOG` plus `PANEL_RESOLVED` selects the pinned legacy compatibility reporter for the separate
T&R store. That mode rejects live-knowledge paths, filesystem aliases, and non-legacy arguments;
both variables are required together. It never reads or writes the council ledger and retains the
legacy timestamp/index resolution interface only for the T&R store.

### Prospective usefulness capture (implemented, not automatically activated)

The runtime also contains additive V2 capture commands for exact visible prompts and answers,
seat/version metadata, atomic findings, operator dispositions, outcome class, capture-health
reporting, and local snapshot/restore rehearsal. V2 never reinterprets V1 forecasts, and V2
resolutions use the separate `capture_resolved.jsonl` sidecar.

Every V2 prompt discloses one canonical machine-readable forecast request with the actual target,
resolution rule and date, materiality, actions, run, outcome, fingerprint, and cutoff. Its request
digest is derived from that visible block. Every submitted visible JSON answer binds the request,
its input artifact, seat, and shared probability in the structured `capture` object. Completion
and reporting parse the retained prompt and reject substituted questions, prompts, probabilities,
or issuance on or after the outcome resolution date. The same object contains a canonical
`findings` list with the exact seven seat-owned fields; operator grouping and dispositions remain
separate and cannot invent, delete, or alter a seat finding.

Live `capture-activation` is evidence-gated and is never implied by installation. The command
requires the installed commit and source digest plus a version-2, content-addressed approval
manifest. While holding the evidence lock and pinned ledger transaction, it dereferences and
revalidates the frozen audit and durability policies and their source-bound rehearsal certificates;
missing, stale, mismatched, or unrehearsed evidence appends nothing. Local `evidence-snapshot`,
`evidence-verify`, and `evidence-restore` results remain rehearsal-only and are not off-host proof.

For governed V2 activations, every first decision-family attempt persists its deterministic
one-in-five audit assignment before seats launch, and retries inherit that assignment. A selected
family's first complete run emits a blinded finding-audit case plus a separately retained alias
map. Audit rows do not alter lifecycle or Brier denominators. Installation and rehearsal do not
activate V2; use copied or explicit non-live stores unless the principal separately authorizes a
manifest that passes `activation-readiness` at the append boundary.
