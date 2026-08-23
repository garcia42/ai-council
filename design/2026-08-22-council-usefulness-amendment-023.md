# Council usefulness preregistration amendment 023

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Timestamp: 2026-08-23T11:39:37Z
Reason: after amendment 022 passed its focused 29-test installer suite, an independent preflight
audit reproduced a post-authentication handoff race. The audit rewrote a backup payload and its
manifest digest after authentication but before latest-pointer/runtime publication; installation
reported success and a later restore installed the substitute bytes.
Independent preflight audit: `installer_race_design` amendment-022 follow-up.

This file appends to the preregistration and amendments 001 through 022. This finding was made
before a formal council run. No V2 activation or eligible V2 observation exists.

## Authenticated backup custody spans the complete transaction

Authenticated backup payload and manifest descriptors, or immutable transaction-owned copies made
from those descriptors, remain in installer custody through latest-pointer publication, runtime
publication, rollback, and the final success boundary. Backup payload and manifest bytes are
reauthenticated from that same held snapshot at the mutation boundary and again before success.
Rollback consumes only those retained authenticated descriptors or immutable transaction copies;
it never reopens a mutable backup pathname.

A deterministic regression rewrites both a backup payload and its matching manifest digest during
`_publish_latest_pointer`. Installation must fail and roll back before reporting success, and a
later restore must not install the substitute bytes.

## Restore retains an immutable verified source through success

Every current and future restore snapshots the verified on-disk backup into immutable
transaction-owned custody before mutating a target. Restore revalidates that source backup through
the success boundary, and all rollback data is descriptor-backed or transaction-owned. A mutable
pathname is never treated as continuing proof of authenticated content.

Live V2 activation remains disabled by the independent-audit and off-host-durability blockers.
