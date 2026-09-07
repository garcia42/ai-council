# Active Council usefulness capture routing

When `/var/lib/ai-council-evidence/live-capture-20260907/ACTIVE.json` exists, every new
`/council` invocation must follow the V2 workflow in
`/var/lib/ai-council-evidence/live-capture-20260907/COUNCIL_CAPTURE_RUNBOOK.md`.
Read the live capture report before launching seats. Use its actual activation ID, the
installed wrapper, the explicit external artifact root and the shared evidence lock.
A V1-only review after activation is missing usefulness data and remains in the denominator;
never silently fall back to V1 or backfill a completed review as V2.

Calls already underway when activation is appended retain their original issuance and
format; do not rewrite them. Count any resulting missingness honestly. Do not make a
synthetic council just to claim that capture has begun.
