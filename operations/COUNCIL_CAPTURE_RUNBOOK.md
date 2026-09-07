# Live usefulness capture operating runbook

## Fixed bindings

- Principal/study owner: the user. Execution/backup operator: `manny-capture-maintenance`, the pinned scheduled process on manny.
- Installed runtime: `/home/trader/ai-council-sessions/capture-renewal-20260907`, commit `4ca5c08b97e1ef10ed2c3c5d585a6747275c658c`.
- Writer: `python3 -B /home/trader/.claude/knowledge/council-eval/predictions_report.py`.
- Ledger: `/home/trader/.claude/knowledge/futures-panel-log.jsonl`.
- V2 resolutions: `/home/trader/.claude/knowledge/council-eval/capture_resolved.jsonl`.
- Artifact root: `/var/lib/ai-council-evidence/live-capture-20260907/artifacts`.
- Coordination lock: `/home/trader/.local/state/council-tools/evidence.lock`.
- Configuration, actual activation ID/time and approved manifest digest: `config.json` and `ACTIVE.json` in the same evidence root. Never invent an activation ID from this document.

The reviewer assignments in `OWNERSHIP.json` are operational assignments, not proof that any finding has been checked. Each sampled case gets two fresh adjudicator sessions that did not produce its original review. Keep the alias map from them. Retain actual model/version/definition identity digests and exact output artifacts; never replace unavailable reviewers with invented grades. Shared base-model errors may remain correlated even across fresh sessions.

## Before every genuine council

Run the installed V1 report and blind tally as usual. Also run `capture-report --json` with explicit `--log`, `--events`, and `--artifact-root` above. Inspect `activationReadiness.currentlyHealthy`, not just native exit: a valid unhealthy report can exit zero. Refuse decision finalization on invalid or unhealthy evidence; preserve incident containment/rollback exceptions. Inspect `systemctl status council-capture-evidence.service` and its timer on unhealthy evidence.

For a new invocation, first run `capture-initiate --log <ledger> --activation-id <actual ID> --idempotency-key <unique stable invocation key> --coordination-lock <lock>`. This is before preparing prompts. Persist its returned runId/initiationId. A retry of this command uses the same key; another council attempt gets another key but retains its underlying decisionFamilyId. Record active human handling time honestly; model waiting time is separate. Do not subtract inconvenient preparation or repair effort.

## Preserve original inputs and answers

1. Seal a baseline JSON with schemaVersion 3 and `knownConsiderations` before reviewers see the problem. Each consideration has a stable considerationId and its specific claim. Retain exact bytes, compute its Git blob identity and capture the artifact through `capture-artifact` with run ID/operator/evidence reference.
2. Choose one binary shared outcome and classify it prospectively as exogenous or intervention-sensitive with a rationale. Many “will this reviewed change ship without a defect?” questions are intervention-sensitive. Only resolved exogenous V2 outcomes enter headline descriptive Brier scores.
3. Construct the exact canonical request using the installed source's `outcome_id_v2`, `outcome_fingerprint_v2` and `forecast_request_binding_v2` functions. Use their signatures, not guessed JSON keys. Include runtime commit/source digest, baseline Git blob/SHA, canonical request, actual outcome/date/resolution rule/cutoff in every seat prompt. Do not show peer conclusions or probabilities.
4. Capture each final prompt, build `seat_input_manifest_sha256` from returned references, and append `capture-attempt` with the initiation ID, stable decision family, question, baseline reference, outcome class/rationale/cutoff, seat plan, exact input references and shared outcome. Supply `--decision-before-file` and one `--visible-input seat=/path` for each seat. The CLI requires and re-verifies retained visible bytes. Model, role, agent version and definition digest must describe the actual launcher, not a preferred model.
5. Launch the normal three house seats and the required independent blind seat. V2 `capture-attempt` is the price-free attempt for this run; do not also create a V1 attempt. For V2, set blindSeat.brief with blind_brief_identity(runId, captured_input_path), as required by the V2 validator; do not use the V1 prepare-brief command for a V2 attempt. Preserve the independent role and isolated provider configuration.
6. Each seat's actual visible JSON answer must contain its ordinary reasoning/verdict and the canonical `capture` object: run/outcome/fingerprint/cutoff/request digest, seatId, inputArtifactSha256, sharedProbability and the seven-field seat-owned findings list. A genuine no-findings response uses `kind: no-findings` and an empty findings list. Never add those fields to an answer after the model returned it. A failed format is invalid evidence; retain it and account for failure.
7. Append `capture-seats-finished` with submitted/abstained/unavailable states. Capture exact original answers. Construct cross-seat grouping and one operator disposition per finding separately; do not invent, delete or rewrite a seat finding. Record known claims against the sealed baseline, including its quoted subclaim.
8. Seal through `capture-complete`, supplying exact visible input/output files again, the baseline, seat results and probabilities, grouping/dispositions, no-findings declarations, and blindSeat. The exact completion payload keys are runId, seatResults, findings, noFindings, probabilities and blindSeat; V1 councilFields are not accepted. Handling time is derived from the system-owned initiation/launch/finish/finalization boundaries. Keep any separately measured human effort in retained supporting receipts, not invented payload fields. Use `--help` and the committed schema for exact fields. Do not call V1 `complete` as well. Then run V1 report, V2 report and blind tally, retaining native exit statuses and JSON.

Exact worked schema construction is in the pinned `tests/test_capture_integration.py` and the copied-runtime proof `tests/test_runtime_contract.py`. Their synthetic content is illustrative only; none may be copied as a live observation. Human handling time and unavailable usage receipts remain unknown if not measured; no zero-cost/zero-time imputation.

## Audit, grading and interpretation

At each council, inspect whether a selected-family audit case was emitted. Weekly (first checkpoint September 14), review capture completeness, active handling time, failed attempts and available audit cases. Have the two assigned adjudicators independently score the blinded case; retain their answers and actual identities. Agreement below the frozen 60% bar, or unmeasurable agreement, means NO VERDICT for any later gate depending on that rubric. Preserve the original one-in-five selection, 20-independent-family minimum and October 31 capture-soak deadline. The four-month minimum starts at actual prospective activation; it is not backdated to the design.

Grade matured exogenous outcomes using durable evidence and independent review. Use V2's separate sidecar and stable IDs; V1 forecasts stay in V1. Resolve only after the full specified resolution date. A recorded operator action is not evidence that the action helped, and several seats repeating a finding are not several prevented incidents.

## Evidence renewal and failure recovery

The system timer runs the hash-bound maintenance driver every 12 hours. It generates fresh protocol rehearsal and off-host generation-pinned restore proof, then requests renewal through the installed wrapper. It cannot change original cohort, source or frozen controls. A fresh manifest alone cannot repair missing accepted history.

Every cycle has a new immutable prefix and private local receipts. An incomplete cycle stops subsequent cycles; inspect command-attempt/result receipts and exact remote generations. Do not delete the cycle or rerun its prefix to turn UNKNOWN into success. Restore missing historical artifacts byte-for-byte from verified off-host evidence. A snapshot race can refuse source-changed before upload; preserve that attempt and reconcile its confirmed non-write state before a new cycle is authorized.

The initial schedule expires September 15 UTC and has a 16-cycle ceiling. Review the actual first-week burden and extend the hash-bound configuration deliberately before expiry; no silent unlimited spend. No application remote deletes, bucket configuration edits or reader rollback are authorized. Disable `council-capture-evidence.timer` for containment; that stops renewal, not the underlying immutable cohort. Health then visibly expires. Historical validity is retained.

## Exact-prompt launcher detail

For the V2 blind seat use the reviewed `operations/capture_blind.py`, not the old `ask_blind.sh` wrapper. Include the independent role text and complete V2 output instructions in the retained prompt itself; the launcher sends those bytes verbatim. Before capturing the prompt, choose a stable absolute local filename and instruct the seat that its only permitted tool action is `sha256sum` on that exact file. It obtains inputArtifactSha256 from that tool result; putting the prompt's own digest inside itself would create a circular binding. The launcher checks the prompt hash before/after, runs a fresh blindness probe in an isolated CODEX_HOME, retains original answers/transcripts, and rejects a wrong digest. Inspect the transcript: any tool access beyond hashing its own prompt invalidates the claimed blind independence. This is a constrained tool policy verified by transcript, not an OS claim that all other reads were impossible.

House seats similarly hash their own retained input file, in addition to their ordinary approved read-only review tools. Their agent/system context is described by the retained agent-definition digest and actual launcher metadata. The original user prompt is the input artifact. Never silently add a late hash-binding instruction or other user message after that artifact has been captured. Use the actual returned answer bytes, not a repaired JSON object, for output capture.
