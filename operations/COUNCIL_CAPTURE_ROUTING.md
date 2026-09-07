# Active Council usefulness capture routing

When the live capture report shows a capture activation, every new
`/council` invocation must follow the V2 workflow in
`/var/lib/ai-council-evidence/live-capture-20260907/COUNCIL_CAPTURE_RUNBOOK.md`.
Read the live capture report before launching seats. Use its actual activation ID, the
installed wrapper, the explicit external artifact root and the shared evidence lock.
ACTIVE.json in that evidence root is a convenience record, not the activation authority;
its absence never authorizes a V1 fallback after the immutable activation row exists.
A V1-only review after activation is missing usefulness data and remains in the denominator;
never silently fall back to V1 or backfill a completed review as V2.

Calls already underway when activation is appended retain their original issuance and
format; do not rewrite them. Count any resulting missingness honestly. Do not make a
synthetic council just to claim that capture has begun.
