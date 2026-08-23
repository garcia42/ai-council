# Council usefulness preregistration amendment 011

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Timestamp: 2026-08-23T08:32:03Z
Reason: the ninth integrated implementation council reproduced outcome-identity, finding-attribution,
report-eligibility, mutation-boundary authorization, snapshot-destination, and parser-nondisclosure
defects in an exact tree with 311 passing tests and one intentional rehearsal-only skip.
Independent review run ID: `run-f505e749ee2447ecbc5d1e79fcf91553`.

This file appends to the preregistration and amendments 001 through 010. No V2 activation or
eligible V2 observation exists. The reviewed shared outcome remains intervention-sensitive and is
excluded from the exogenous V2 Brier headline.

## Repeated outcome identity

When a V2 attempt has the same outcome fingerprint as a prior attempt, the later attempt must
prospectively link the prior outcome ID in `relatedOutcomeIds`; an unlinked recurrence fails closed.
Reports distinguish forecast issuances from unique underlying outcome fingerprints. Outcome counts,
polarity diagnostics, and repeated-issuance labels cannot silently describe two run-derived IDs for
one event as two independent outcomes.

## Seat-originated non-empty findings

Every submitted visible output contains an exact structured seat-originated finding list. For a
seat with findings, that list must match the completion's attributed finding objects before any
operator disposition fields are added; seat-owned fields are canonical and exact. A seat with no
findings retains the existing artifact-bound structured empty path. Writer-time and report-time
checks reparse the retained output. An operator cannot invent, delete, or alter a seat's claim,
category, severity, proposed action, evidence summary, grouping, or finding identity.

Operator dispositions remain a separate post-seat layer. Report-time validation re-reads the
sealed decision baseline and rechecks every `already-known` consideration ID and quoted subclaim,
as well as global finding and group identity, before eligibility or headline finding counts.

## Disjoint report classifications

`rawRecordCount`, `validatedV2RecordCount`, and `invalidV2RecordCount` have explicit, non-overlapping
physical-record semantics. A structurally valid record whose boundary timestamp is future-invalid
is denominator-visible but is not also counted as validated. Provenance-invalid records likewise
cannot remain in the validated count. The report provides a reconciliation that cannot classify
more ledger records than physically exist.

## Mutation-bound authority and pinned destinations

Authorization is bound to the pinned parent and target identity used for mutation, not only to a
lexical path classification performed earlier. Replacing or renaming a plain directory between
classification and append cannot turn a non-live authorized path into the live store. The live
activation blockers and authority-host decision are rechecked against the exact pinned mutation
target before any row or derived lock is created.

Snapshot destination containment likewise pins the validated destination parent through creation,
copy, manifest, and completion. Symlink or plain-directory namespace substitution cannot redirect
any snapshot byte outside the authorized target, even when the operation later fails. Partial
outside snapshots and outside `SNAPSHOT.COMPLETE` markers are forbidden.

All snapshot metadata and manifest JSON use strict duplicate-key parsing. User-facing failures are
generic and never include caller-controlled keys or values.

Live V2 activation remains disabled by the independent-audit and off-host-durability blockers.
