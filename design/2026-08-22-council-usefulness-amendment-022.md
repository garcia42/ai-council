# Council usefulness preregistration amendment 022

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Timestamp: 2026-08-23T11:23:42Z
Reason: the fourteenth integrated implementation council approved methodology and operations but
reproduced pathname-based backup poisoning and an AWS secret detector syntax gap in an exact tree
with 402 passing tests and one intentional rehearsal-only skip.
Independent review run ID: `run-38d17877fa4b4e39b0485298d9a3dc5c`.

This file appends to the preregistration and amendments 001 through 021. No V2 activation or
eligible V2 observation exists.

## Backup bytes come from pinned source descriptors

Installer backups never copy a managed target through a mutable pathname after validation. Each
source is opened without following links beneath its pinned parent, its exact device/inode/type/
link-count/mode identity is verified, and backup bytes are streamed from that retained descriptor.
The same descriptor is reverified through EOF and synchronization; the destination bytes and
manifest digest must match the pinned source bytes before any runtime publication. A deterministic
race substitutes a target during the former `copy2` boundary and restores the original name;
installation must either back up the original pinned inode or fail before publication, never create
a manifest-valid backup of the substitute.

## Secret detection recognizes assignment syntax after JSON serialization

The fixed AWS detector recognizes both shell-style assignments and canonical JSON object syntax,
including quoted keys, optional whitespace, the colon separator, and a quoted 40-character secret
value. Artifact-byte and standalone schema detectors remain parity-tested. Runtime and subprocess
CLI regressions place a canonical JSON `AWS_SECRET_ACCESS_KEY` assignment in resolution evidence and
require rejection before append, unchanged stores, empty stdout, and non-reflective stderr.

Live V2 activation remains disabled by the independent-audit and off-host-durability blockers.
