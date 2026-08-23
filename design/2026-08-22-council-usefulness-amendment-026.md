# Council usefulness preregistration amendment 026

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Timestamp: 2026-08-23T13:41:27Z
Reason: the seventeenth integrated council approved methodology but reproduced an artifact cleanup
race, undiscoverable successful-append escrows, and same-inode installer staging-byte mutation after
439 tests and the copied-live rehearsal passed.
Independent review run ID: `run-b370a41a3420425181e427e12919f71e`.

This file appends to the preregistration and amendments 001 through 025. No V2 activation or
eligible V2 observation exists.

## Artifact failure cleanup is non-destructive and discoverable

Artifact capture never performs a separate identity check followed by unlinking a created leaf.
When a post-creation failure occurs, the authentic created inode is retained under an exact
recoverable escrow name and that location is included in the raised domain error. If a replacement
repopulates the former leaf name at any cleanup cut, the replacement inode and bytes survive. A
deterministic fsync-failure regression injects substitution immediately before the former unlink and
requires both the authentic escrow and replacement content to remain recoverable.

## Successful JSONL escrows are operator-visible and governed

Every successful JSONL replacement that retains the displaced prior inode returns its exact escrow
location through the low-level transaction API and propagates it to the caller or a durable,
read-only-reportable inventory. Capture and legacy reporters enumerate retained transaction escrows,
their byte sizes, and aggregate retained bytes without treating them as ledger input. CLI success
responses expose newly retained paths. The README documents why retention exists, its potentially
quadratic byte growth, the filename convention, monitoring, and a quiescent manual reconciliation
and disposal procedure. No ordinary append or report silently deletes these files.

Regressions require multiple successful appends to produce exactly discoverable receipts and report
inventory entries; no retained snapshot may exist without a returned or reportable location.

## Installer staged bytes remain authenticated through exchange

Installer staging descriptors remain open from complete write and synchronization through the
target exchange and final success checks. The staged inode's bytes are reauthenticated against the
expected rendered digest after the final pre-exchange hook, immediately after exchange through the
same descriptor, and before success. A deterministic same-inode in-place staging mutation at the
former gap must fail and restore the original runtime without publishing or blessing substitute
bytes.

Live V2 activation remains disabled by the independent-audit and off-host-durability blockers.
