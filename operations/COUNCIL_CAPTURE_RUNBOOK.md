# Council usefulness capture operating runbook

## Retired on 2026-09-10

This runbook is retained as historical evidence. Do not execute its activation,
renewal, restart, repair or backfill procedures. The principal retired V2
usefulness capture after both the original and fresh studies had activated. New
Councils use the V1 workflow described in `COUNCIL_CAPTURE_ROUTING.md` and are
outside the V2 completeness denominator. Preserve all V2 stores and interrupted
cycles read-only.

The legacy ledger remains the V1 append target. V2 analysis reads only its
retirement-time prefix, bound by byte count and SHA-256; V1 analysis reads the
whole append-only file. This boundary preserves cumulative V1 governance without
allowing later V1 rows to change the ended V2 result.

The durable retirement record is
`/var/lib/ai-council-evidence/CAPTURE_RETIRED.json`. Both maintenance services
are condition-fenced by that record and both timers are disabled. Reversing any
of those controls is a new activation, not maintenance.

## Historical prospective restart routing

The historical activation below is retained evidence and is closed to new
collection after the option D cutover. New reviews use the installed wrapper
with `--study council-fresh-20260910`; historical reads and outcome resolutions
use `--study council-legacy`. Never use an omitted selector or V1 fallback.

The fresh store paths are source-defined by `council_tools.study_routes` under
`.claude/knowledge/council-eval/studies/council-fresh-20260910` for the ledger
and sidecars, and `/var/lib/ai-council-evidence/fresh-capture-20260910-v2`
for artifacts, controls and maintenance-cycle receipts. The hash-bound config
remains under `.local/state/council-tools/studies/council-fresh-20260910`.
Both studies use
`.local/state/council-tools/evidence.lock`.

Before sealing the old issuance digest, disable its timer, finish or preserve
in-flight attempts and prove all collection writers quiescent. Keep file
descriptor 8 exclusively locked on the lock's parent directory and descriptor
9 exclusively locked on the shared evidence lock from that digest
through runtime installation, operator-routing replacement, the activation
append and closed-old refusal. Invoke `capture-activate` with
`--preheld-coordination-parent-fd 8 --preheld-coordination-lock-fd 9`; do not
invoke any other lock-taking Council
command while the external lock is held. Rollback disables the fresh timer and
keeps both collection routes held. Release both descriptors only after the
installed fresh route and exact maintenance config are verified. The parent
lock preserves the namespace custody used by the safe coordination-lock path.

Do not reuse the historical September 8-14 timer, September 15 expiry, config,
root, ACTIVE.json or unit hash for the fresh study. The approved fresh timer is
`2026-09-11..17 00,12:30:00 UTC` (00:30 and 12:30 UTC); its authorization expires at
`2026-09-18T00:00:00Z` and its first checkpoint is September 17. The 16-cycle
ceiling covers preparation, the immediate service renewal and 14 scheduled
renewals. The new timer/config must bind the reviewed runtime and operations
source, fresh paths, and the limits 1,024 objects, 2,049 adapter calls, 64 MiB
and 30 minutes. Reserve $1.60 within the approved $2 planning allowance.
Real off-host preparation and restore proof must pass before fresh activation.
Run each study report plus the global cumulative operations report before and
after every fresh Council.

Before the fresh activation exists, the global report is expected to refuse:
missing fresh store files are invalid state (exit 1), and initialized but
unhealthy fresh capture blocks finalization (exit 3). Create the three empty
fresh ledger/sidecar files under the held cutover lock, then clear exit 3 only
by completing the reviewed evidence-bound activation and immediate renewal.
Neither transient authorizes a Council or a fallback to the historical route.

Render `council-fresh-capture-evidence.service` only after the final reviewed
commit is known. It runs
`/home/trader/council-tools/operations/capture_evidence_cycle.py` with the
fresh config under `.local/state/council-tools/studies/council-fresh-20260910`
and its exact SHA-256. The config points maintenance custody at
`/var/lib/ai-council-evidence/fresh-capture-20260910-v2`. It also binds the exact
external blind-criterion path
and SHA-256 used by `study-operations-report`. Install the separate fresh
service, non-persistent timer and alert unit disabled; never overwrite or
retarget the historical units. Hash and retain the rendered config, driver,
criterion, service, timer, alert unit and alert script in the release packet.

Run the real prepare cycle and generation-pinned GCS restore before starting
the continuously locked cutover. Install the fresh timer disabled. After the
activation append, read the ledger directly while descriptor 9 remains held,
verify its activation ID and manifest digest, create the acknowledgment files,
and prove the installed historical route refuses collection. Then release the
descriptor, run the immediate renewal service, and enable the timer only if
that renewal succeeds. `Persistent=false` deliberately drops missed calendar
slots; no catch-up run consumes the zero-headroom 16-cycle allowance.

For rollback, stop and disable `council-fresh-capture-evidence.timer`, restore
the reviewed predecessor runtime and routing, verify both timers disabled, and
leave collection held for a new governed decision. The historical issuance
ledger remains sealed; its governed V1/V2 resolution sidecars may still receive
valid resolutions for outcomes that were already issued.

## Historical fixed bindings

- Principal/study owner: the user. Execution/backup operator: `manny-capture-maintenance`, the pinned scheduled process on manny.
- Installed runtime: `/home/trader/council-tools`, commit `4ca5c08b97e1ef10ed2c3c5d585a6747275c658c`.
- Writer: `python3 -B /home/trader/.claude/knowledge/council-eval/predictions_report.py`.
- Ledger: `/home/trader/.claude/knowledge/futures-panel-log.jsonl`.
- V2 resolutions: `/home/trader/.claude/knowledge/council-eval/capture_resolved.jsonl`.
- Artifact root: `/var/lib/ai-council-evidence/live-capture-20260907/artifacts`.
- Coordination lock: `/home/trader/.local/state/council-tools/evidence.lock`.
- Configuration, actual activation ID/time and approved manifest digest: `config-v2.json` and `ACTIVE.json` in the same evidence root. Never invent an activation ID from this document.

The reviewer assignments in `OWNERSHIP.json` are operational assignments, not proof that any finding has been checked. Each sampled case gets two fresh adjudicator sessions that did not produce its original review. Keep the alias map from them. Retain actual model/version/definition identity digests and exact output artifacts; never replace unavailable reviewers with invented grades. Shared base-model errors may remain correlated even across fresh sessions.

## Historical workflow before the prospective restart

Run the installed V1 report and blind tally as usual. Also run `capture-report --json` with explicit `--log`, `--events`, and `--artifact-root` above. Inspect `activationReadiness.currentlyHealthy`, not just native exit: a valid unhealthy report can exit zero. Refuse decision finalization on invalid or unhealthy evidence; preserve incident containment/rollback exceptions. Inspect `systemctl --user status council-capture-evidence.service` and its timer on unhealthy evidence.

For a new invocation, first run `capture-initiate --log <ledger> --activation-id <actual ID> --idempotency-key <unique stable invocation key> --coordination-lock <lock>`. This is before preparing prompts. Persist its returned runId/initiationId. A retry of this command uses the same key; another council attempt gets another key but retains its underlying decisionFamilyId (use the required family- prefix, for example family-issue-184). Record active human handling time honestly; model waiting time is separate. Do not subtract inconvenient preparation or repair effort.

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

## Historical activation handoff

The activating operator owns this handoff. Create the V2 resolution sidecar only if absent. Append activation through the installed writer with the reviewed manifest. Re-read the live ledger under the evidence lock and match the actual activation ID and manifest digest to the prepared cycle. Only after that verification, exclusively create ACTIVE.json (actual ID/time, manifest, source and prospective study anchor) and the prepare cycle complete.json (state ACTIVATION_CONFIRMED, actual ID, manifest digest and acknowledgment time). Never mark a failed cycle successful. If interrupted after the append, reconcile that existing immutable row; do not append a replacement activation. Verify both records, current capture health and routing, then perform the first renewal using systemctl --user start council-capture-evidence.service. Enable the timer only after that real service invocation succeeds.

## Historical evidence renewal and failure recovery

The system timer runs the hash-bound maintenance driver every 12 hours. It generates fresh protocol rehearsal and off-host generation-pinned restore proof, then requests renewal through the installed wrapper. It cannot change original cohort, source or frozen controls. A fresh manifest alone cannot repair missing accepted history.

Every cycle has a new immutable prefix and private local receipts. An incomplete cycle stops subsequent cycles; inspect command-attempt/result receipts and exact remote generations. Do not delete the cycle or rerun its prefix to turn UNKNOWN into success. Restore missing historical artifacts byte-for-byte from verified off-host evidence. A snapshot race can refuse source-changed before upload; preserve that attempt and reconcile its confirmed non-write state before a new cycle is authorized.

The initial schedule expires September 15 UTC and has a 16-cycle ceiling. Review the actual first-week burden and extend the hash-bound configuration deliberately before expiry; no silent unlimited spend. No application remote deletes, bucket configuration edits or reader rollback are authorized. Disable `council-capture-evidence.timer` for containment; that stops renewal, not the underlying immutable cohort. Health then visibly expires. Historical validity is retained.

## Exact-prompt launcher detail

For the V2 blind seat use the reviewed `operations/capture_blind.py`, not the old `ask_blind.sh` wrapper. Include the independent role text and complete V2 output instructions in the retained prompt itself; the launcher sends those bytes verbatim. Before capturing the prompt, choose a stable absolute local filename and instruct the seat that its only permitted tool action is `/usr/bin/sha256sum` on that exact file. It obtains inputArtifactSha256 from that tool result; putting the prompt's own digest inside itself would create a circular binding. The launcher checks the prompt hash before/after, runs a fresh blindness probe in an isolated CODEX_HOME, retains original answers/transcripts, and rejects a wrong digest. Inspect the transcript: any tool access beyond hashing its own prompt invalidates the claimed blind independence. The launcher applies a named filesystem permission profile to model commands: minimal system files and the exact prompt are readable; unrelated host files and credentials are inaccessible, host processes are hidden behind private proc, and command networking is disabled. A retained negative-access test must pass before any provider call. The launcher clears inherited environment values, disables web search and ignores user configuration/rules. The authenticated provider client still uses its established login; these restrictions apply to model tools. Transcript inspection remains required and is recorded as pending, not falsely attested.

House seats similarly hash their own retained input file, in addition to their ordinary approved read-only review tools. Their agent/system context is described by the retained agent-definition digest and actual launcher metadata. The original user prompt is the input artifact. Never silently add a late hash-binding instruction or other user message after that artifact has been captured. Use the actual returned answer bytes, not a repaired JSON object, for output capture.

The timer has explicit September 8–14 UTC calendar dates and cannot keep firing after the initial window. OnFailure invokes a single bounded Pushover notification; its journal records acceptance or failure without credentials. /usr/bin/gcloud and an explicit PATH are checked by ExecStartPre before a cycle directory is created. The runtime source /home/trader/council-tools and the operational source /home/trader/ai-council-sessions/usefulness-activation-20260907 are live dependencies: never rebase, edit or remove them while installed.

At the September 14 checkpoint, active human handling-time and causal net-value claims remain NO VERDICT unless separately measured. Store per-run operator effort, false-positive/rework costs and missed-defect evidence in supporting receipts; system wall-clock duration must not substitute for active effort. Review capture exclusions, member growth, remaining cycle allowance and failed notifications. Pending rubric agreement and the lack of a counterfactual comparison remain explicit limits.
