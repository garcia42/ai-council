# Council usefulness preregistration amendment 018

Status: FROZEN BEFORE PROSPECTIVE V2 DATA
Timestamp: 2026-08-23T10:22:22Z
Reason: the amendment-017 reproduction audit passed every dispatch and secret regression but found
that a schema-invalid row retaining recognized kind `capture-invalidation` still exerted real
invalidation semantics against a valid lifecycle.
Independent preflight audit: `amendment14_adversary` control-semantics follow-up.

This file appends to the preregistration and amendments 001 through 017. No V2 activation or
eligible V2 observation exists.

## Invalid control rows cannot exert control semantics

A `capture-invalidation` row affects a lifecycle only when it is structurally valid and valid as of
the report observation time. A schema-invalid, malformed, duplicate, orphan, or future-as-of
invalidation remains visible in invalid-ledger diagnostics but does not attach an invalidation to a
run, change completeness, remove headline eligibility, change finding strata, or increment valid
invalidation counts. Integrated regressions append malformed and future invalidations after a
complete valid run and require the valid run and its headline findings to remain unchanged.

Live V2 activation remains disabled by the independent-audit and off-host-durability blockers.
