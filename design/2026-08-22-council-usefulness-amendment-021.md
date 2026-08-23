# Council usefulness preregistration amendment 021

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Timestamp: 2026-08-23T11:05:27Z
Reason: the amendment-020 audits approved decoded-semantic secret handling and installer mutation
safety, but reproduced loss of recovery discoverability when a retained-entry report itself failed.
Independent preflight audit: `installer_race_design` retained-custody follow-up.

This file appends to the preregistration and amendments 001 through 020. No V2 activation or
eligible V2 observation exists.

## Retained custody survives report failure

If creation, writing, synchronization, or publication of `RETAINED_ESCROWS.tsv` or
`RETAINED_BACKUP_POINTERS.tsv` fails, the raised installer domain error still includes the backup
location, every exact retained path known to the transaction, and an explicit publication state.
It never exposes only a raw filesystem exception after targets have changed. Tests inject failures
at report creation, write, file synchronization, and directory synchronization after both committed
and rolled-back transactions, then verify that an operator can locate every retained inode from the
error alone.

## Operator runbook for intentional escrow retention

The README explains that secure install and restore retain one old runtime inode per managed target,
that repeated operations intentionally accumulate these files, where the durable TSV inventories
live, and why the installer never auto-deletes them. Manual disposition requires an exclusive,
quiescent maintenance window, verification against the named backup and current target identities,
and explicit operator ownership. No ordinary install, restore, rehearsal, or report path performs
that cleanup.

Live V2 activation remains disabled by the independent-audit and off-host-durability blockers.
