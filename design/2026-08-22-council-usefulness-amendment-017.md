# Council usefulness preregistration amendment 017

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Timestamp: 2026-08-23T10:18:15Z
Reason: the amendment-016 reproduction audit passed non-text malformed dispatch but found that an
unknown textual V2 kind is retained in invalid-ledger diagnostics while omitted from the physical
denominator.
Independent preflight audit: `amendment14_adversary` final follow-up.

This file appends to the preregistration and amendments 001 through 016. No V2 activation or
eligible V2 observation exists.

## Unknown textual V2 kinds use the malformed-dispatch sentinel

If a schema-version-2 row is marked schema-invalid and its textual `kind` is not a recognized V1 or
V2 kind, report projection treats it as `invalid-v2-record`, exactly like a non-text kind. The row
therefore occupies its own excluded physical denominator position and cannot disappear merely
because its caller-controlled dispatch value is hashable text. Integrated coverage parametrizes
non-text and unknown-text malformed dispatch before a later valid completion and requires identical
cohort, headline, excluded, finding, disposition, and grouping results.

Live V2 activation remains disabled by the independent-audit and off-host-durability blockers.
