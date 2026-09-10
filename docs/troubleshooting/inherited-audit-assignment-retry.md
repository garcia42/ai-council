# Related Council attempt refuses inherited audit assignment

## Problem
A third V2 Council attempt in the same decision family fails before reviewer
launch with `finding audit assignment failed safely`. The underlying error is
`duplicate persisted assignments for decision family`. The initiation and
captured prompts may already exist; their presence does not prove an attempt
or provider call happened.

## Root Cause
Each valid retry embeds the family's original audit assignment unchanged.
`append_council_attempt_v2` previously passed every embedded copy to
`assign_decision_family`, which expects distinct logical assignments and
correctly refuses duplicates. The second attempt passes, but it supplies the
duplicate input that prevents the third.

## Diagnosis Steps
1. Run the installed source-pinned report and capture report. Check native exits
   and current capture health.
2. Inspect the exact run's initiation, attempt and completion records read-only.
   Do not infer a provider call from a launcher failure.
3. Compare prior assignments for the same activation and decision family.
   Identical copies inherited by different attempts are not conflicting
   assignments. A changed first run, timestamp, protocol or selection is a
   conflict and must remain a refusal.
4. Reproduce with non-live fixtures or the storage-agnostic assignment function.
   Do not invoke the live append again as a diagnostic.

## Solutions
The runtime caller now removes only equal inherited assignment objects before
calling the unchanged strict primitive. Existing validation runs first.
No ledger row, assignment, selection or denominator is removed or rewritten.

Develop and qualify the repair in an isolated worktree. Review the committed
change and follow the installed-runtime activation procedure before using it
live. Never edit an installed source to clear its pin, switch the affected
decision to another family, or fall back to V1.

Preserve the failed invocation and original visible artifacts. After a reviewed
runtime change, reconcile the existing initiation and source bindings before
choosing a supported continuation. Do not relaunch an ambiguous reviewer call.

## Prevention
Runtime integration tests exercise four attempts for both selected and
non-selected families, interleaved with the other family, preserve ledger
prefixes, and verify family-level reporting counts. A conflicting-history
test still refuses before an append. The pure primitive's duplicate and
tampered-selection tests remain unchanged.

## Related Files
- `src/council_tools/capture_runtime.py`
- `src/council_tools/finding_audit.py`
- `src/council_tools/capture_schema.py`
- `tests/test_capture_integration.py`
- `tests/test_finding_audit.py`

## History
2026-09-08: issue #186 reproduced the third-attempt refusal on the installed
V2 caller. No reviewer calls were launched by that refused attempt. This
document describes the repair, not proof of its deployment.
