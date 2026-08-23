# Council usefulness preregistration amendment 020

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Timestamp: 2026-08-23T10:52:44Z
Reason: the amendment-019 reproduction audits passed its primary target-substitution and literal-
secret tests but found a JSON-escape semantic secret bypass and a residual check-then-unlink race
in installer escrow cleanup.
Independent preflight audits: `amendment14_adversary` and `installer_race_design` follow-ups.

This file appends to the preregistration and amendments 001 through 019. No V2 activation or
eligible V2 observation exists.

## Secret scanning covers raw and decoded semantics

Every exact report JSONL snapshot is scanned twice before lookup, validation, or reflective error:
once as the exact raw bytes and once after strict decoding in a canonical serialization that exposes
JSON-escaped keys and values. Encodings such as `sk\u002dproj\u002d...` are equivalent to literal
secret-shaped text. Direct runtime and subprocess CLI tests cover escaped secret-shaped kinds,
keys, identifiers, and values while retaining the same one-read byte snapshot, empty stdout,
non-reflective stderr, and unchanged stores.

## Cleanup never deletes through a stale namespace observation

No installer cleanup unlinks an escrow, staging, or rollback name merely because a prior `stat`
matched an installer-owned inode. Linux has no conditional unlink-by-inode, so a name that can be
substituted between identity observation and deletion is retained or moved through a non-deleting
quarantine protocol and reported for later verified cleanup. A deterministic race replaces the
escrow name after identity observation and before the former unlink boundary; tests require the
concurrent inode and bytes to remain recoverable, no successful-cleanup claim, and no loss of the
installer-owned or original target inodes.

Live V2 activation remains disabled by the independent-audit and off-host-durability blockers.
