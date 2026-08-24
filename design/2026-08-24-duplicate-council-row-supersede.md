# Duplicate council rows and supersede composition

Status: SPECIFICATION; NO BEHAVIOUR CHANGE

Issue: #32. This document is normative for #33. The fixtures named below are
executable examples of this contract. PR #29 must not be activated or used to alter the
live ledger until the implementation tickets have landed and passed review.

## Decision

A `council-superseded` record means **the row named by `supersedes` is a duplicate of
the council row named by `duplicateOf`**. It does not mean merely that the target should
not count. Both physical rows are pinned, the duplicate relationship is re-derived from
their identifiers, and the proof row must be retained when the assertion is appended.

Validity is evaluated incrementally at append time. A reader obtains the same result by
replaying the file from line 1 and never revising the validity of an earlier supersede.

## Live-ledger evidence

The measurement used one coherent read of
`~/.claude/knowledge/futures-panel-log.jsonl` at 2026-08-24T03:02:49Z:

| Measure | Count |
|---|---:|
| Physical/nonblank rows | 131 |
| Bytes | 437,298 |
| `council` rows | 69 |
| Rows counted as `completedRuns` | 62 |
| Completed rows admitted by PR #29's old no-forecast guard | 20 |
| Issue #25 duplicate targets in that set | 2 |
| Genuine pre-forecast-contract councils in that set | 18 |
| Council rows retained after excluding only the two issue #25 targets | 67 |
| Completed rows retained on that basis | 60 |

The snapshot SHA-256 is
`fa419bff543b7a9426edc897245d20a919a83454f10cb5d366d738a4e495a98b`.
The live ledger is append-only and grows, so these counts and line numbers describe that
snapshot, not a permanent coordinate system.

Among the 67 measurement-retained council rows:

| Identifier | Present | Distinct | Duplicate groups | Missing/null/unusable |
|---|---:|---:|---:|---:|
| exact `runId` | 39 | 39 | 0 | 28 |
| normalized `blindSeat.brief` | 66 | 66 | 0 | 1 |

The 28 retained rows without a usable `runId` are lines 15–19, 21–23, 27, 29–33,
35–48. The only retained row without a usable brief is line 15. No present identifier
has a duplicate group.

The exceptions matter:

- Line 114 duplicates retained line 113 by both exact `runId` and normalized brief.
- Line 117 duplicates retained line 116 by normalized brief; line 117 has no usable
  `runId`.
- Retained line 15 has neither usable identifier. It cannot be superseded under this
  contract without some future, separately specified identity proof.
- The other 18 rows admitted by the old guard have no duplicate counterpart. Seventeen
  have unique normalized briefs and no `runId`; line 15 has neither identifier. They are
  lines 15–17, 23, 27, 29–33, 35–37, and 41–45.

The issue-time tally was 60 completed / 58 after excluding the targets. Two unrelated
completions were appended before this measurement; the unsafe set remained 20 = 2 + 18.
This observation supports the predicate on the current ledger. It is not a substitute
for the rules below.

## Record and row identity

The future record shape is:

```json
{
  "schemaVersion": 1,
  "kind": "council-superseded",
  "ts": "2030-01-01T00:02:00Z",
  "supersedes": {"line": 1, "rawLineSha256": "<lowercase SHA-256>"},
  "duplicateOf": {"line": 2, "rawLineSha256": "<lowercase SHA-256>"},
  "reason": "why the duplicate was written",
  "approval": {
    "operator": "who asserted it",
    "approvedAt": "2030-01-01T00:02:00Z",
    "reference": "durable approval reference"
  }
}
```

`supersedes` and `duplicateOf` each identify one complete physical JSONL line by a
one-based line number and the SHA-256 of its exact bytes, including the line ending. A
line number alone is not row identity. The two references must be distinct, must name
earlier rows, must match their bytes, and must both name `kind: "council"` rows.

The target must also satisfy the existing evidence-preservation guard: `forecastState`
is absent or null and `predictions` is absent or an empty list. A malformed predictions
value is not an empty list. This guard remains necessary to prevent choosing the sealed
original as the target, but it is not evidence of duplication and is never sufficient.

## Identifier normalization

`runId` is an opaque identifier. It is usable only when it is a non-empty JSON string,
and comparison is exact Unicode-code-point equality. Do not trim it, fold case, parse a
prefix, or coerce another JSON type. Missing, null, empty, and non-string values are
unavailable identifiers.

`blindSeat.brief` is usable only when `blindSeat` is an object and `brief` is a non-empty
absolute POSIX path string after removing leading and trailing ASCII space, tab, carriage
return, and line feed. A string containing NUL is unusable. Normalize it without
consulting the filesystem:

1. Collapse repeated `/` separators and remove `.` segments.
2. Resolve each `..` segment against the preceding ordinary segment. If that would move
   above `/`, the value is unusable.
3. Remove a trailing slash except for `/`; `/` itself is unusable as a brief file.
4. Preserve case and Unicode code points. Do not expand `~` or environment variables,
   resolve symlinks, call `samefile`, or depend on whether the path exists.

Pure lexical normalization makes the assertion re-derivable on another host and at a
later date. Filesystem resolution is wrong because a symlink or mount change could alter
the validity of an immutable ledger record.

## Duplicate predicate

Let `T` be the active council row named by `supersedes` and `R` the active council row
named by `duplicateOf`. Compare an identifier only when it is usable on both rows.

The assertion `T duplicates R` is valid exactly when all of the following hold:

1. At least one identifier is comparable.
2. Every comparable identifier is equal.
3. For every equal comparable identifier, `R` is its only active council-row owner after
   tentatively removing `T`.

Equivalently, one matching identifier is sufficient only when the other identifier is
unavailable on at least one side. A mismatch is conflicting evidence, not an identifier
to ignore.

Consequences:

- A null or missing target `runId` is allowed when the normalized briefs match uniquely.
- A target with neither identifier is refused.
- Equal `runId` values with unequal normalized briefs are refused.
- Equal normalized briefs with unequal `runId` values are refused.
- A matching identifier shared by a third active council row is ambiguous and is refused.

The uniqueness check is part of validation, not a permanent assumption derived from the
2026-08-24 census.

## Incremental composition

Process physical rows in order while maintaining an active set of council rows and the
accepted supersede edges:

1. An ordinary council row enters the active set.
2. For a supersede, validate its complete shape and attribution, both pinned references,
   target evidence guard, duplicate predicate, and uniqueness against the prefix ending
   immediately before that supersede.
3. A valid supersede records the directed edge `T -> R` and removes only `T` from the
   active set. An invalid supersede records an error and changes no state.
4. An accepted edge is never reconsidered because of later rows.

This gives the required ordering and composition behaviour:

- `R` may appear later in the file than `T`, provided both precede the supersede. A
  forward reference to a row after the supersede is invalid.
- Of two supersedes naming one target, the first valid record wins. The second is invalid
  because its target is no longer active.
- Chains compose. If `T -> R` is accepted and a later valid record says `R -> U`, both
  `T` and `R` remain retired and `U` is active.
- Retiring `R` later does not invalidate `T -> R` or reactivate `T`.
- A cycle cannot close: an edge back to an earlier member would name an inactive
  `duplicateOf` row and is invalid. Self-edges are invalid because the references must
  be distinct.

Append-time validity is the only model the appender and reader can enforce identically.
The appender can know the prefix but not future rows, and append-only audit facts must not
change meaning when more rows arrive. Final-state validation is wrong because a later
supersede could retroactively invalidate an earlier one, reactivate its target, and make
chains or cycles require an arbitrary fixed-point rule. It would also make a record the
appender accepted become invalid when the reader next opened the ledger.

## Fixtures

`tests/fixtures/duplicate-council-row-supersede/manifest.json` records the expected
result for each content-free JSONL fixture:

- `live-ledger-shapes.jsonl` anonymizes the relevant identifier shapes of the 20 rows
  admitted by the old guard plus their two retained witnesses: exactly two have a valid
  duplicate assertion and 18 do not.
- `retained-row-later.jsonl`, `forward-reference.jsonl`, `double-supersede.jsonl`,
  `chain.jsonl`, and `cycle.jsonl` cover ordering and composition.
- `null-runid-normalized-brief.jsonl`, `neither-identifier.jsonl`,
  `runid-match-brief-conflict.jsonl`, and `brief-match-runid-conflict.jsonl` cover
  normalization, missing identifiers, and disagreement.

Fixture paths, identifiers, timestamps, attribution, and seat state are synthetic. They
carry no question, verdict, prompt, answer, forecast, or other council content.
