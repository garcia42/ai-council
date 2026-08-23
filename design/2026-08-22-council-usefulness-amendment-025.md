# Council usefulness preregistration amendment 025

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Timestamp: 2026-08-23T13:17:42Z
Reason: the sixteenth integrated council approved resolution methodology but independently
reproduced final namespace handoff gaps in temporary cleanup, exclusive creation, and artifact-root
custody after 427 tests and the copied-live rehearsal passed.
Independent review run ID: `run-b755f318120d4d6786a67541e240bcfc`.

This file appends to the preregistration and amendments 001 through 024. No V2 activation or
eligible V2 observation exists.

## Uncertain temporary names are never destructively cleaned

Temporary or escrow cleanup never performs a separate identity check followed by unlinking the same
mutable name. If ownership of the name can change, cleanup retains the entry and reports its exact
recoverable location; it does not delete, overwrite, or claim a replacement inode. A deterministic
regression substitutes the name after the last identity observation and immediately before the
former unlink, and requires the replacement inode and bytes to survive.

## Exclusive creation publishes completed bytes before advertising the name

`create_bytes_exclusive` prepares, writes, synchronizes, and authenticates bytes under retained
descriptor custody before atomically publishing the advertised pathname without replacement. It
revalidates the published leaf against the retained descriptor at the final success boundary. A
deterministic substitution during the former create-before-write handoff must fail without claiming
the replacement path; success may never name bytes other than the authenticated payload. This
contract applies to torn-tail quarantine and every other exclusive-create caller.

## Artifact success requires configured-root namespace custody

Artifact capture binds both the retained root inode and the configured root pathname. Before
returning a content reference, it revalidates through the pinned root parent that the configured
root name still identifies the retained inode, and it revalidates the published leaf and content.
If the root is renamed and replaced after opening, capture fails without returning a reference; it
must never report success for content reachable only through a detached directory inode. The same
root-namespace binding applies when verifying a reference.

Live V2 activation remains disabled by the independent-audit and off-host-durability blockers.
