### Council forecast scoring

Every `/council` invocation follows the installed forecast contract in the council skill. It first
records a price-free `council-attempt`, gives every seated reviewer one byte-identical material
shared outcome, seals the returned probabilities, validates the completion record, and appends it
through the version-controlled forecast tool. Never resolve by timestamp or prediction-list index.

Forecast scores are descriptive operational calibration, not a reviewer leaderboard. Grading debt
does not stop a new council from convening, but three or more outcomes over 14 days late block
decision finalization unless the principal records a time-limited override. Manual grades require
independent review and durable evidence. A `submitted` seat requires exactly one probability;
`abstained` and `unavailable` are explicit accounted states and require none.

`manny` is the sole authority for live council-ledger and resolution-sidecar writes. Other hosts may
read or rehearse copied data but must not append. Run the report both before launching seats and
after appending the sealed completion. Its exit status is the process-boundary alert: exit 1 stops
the council for invalid state; exit 3 permits collection but blocks decision finalization. Exit 2
is reserved for command-line usage errors.

Grading debt never blocks incident containment or rollback. A rollback preserves the blind-seat
tally's forward-compatible `council-attempt` allowlist because appended attempt rows are permanent.
The runtime reporter is pinned to the installed source commit and source digest and fails closed on
working-tree drift; develop later changes in a separate worktree and activate a new clean commit.

`PANEL_LOG` plus `PANEL_RESOLVED` selects the pinned legacy compatibility reporter for the separate
T&R store. That mode rejects live-knowledge paths, filesystem aliases, and non-legacy arguments;
both variables are required together. It never reads or writes the council ledger and retains the
legacy timestamp/index resolution interface only for the T&R store.
