# Ticket governance v1

This document defines the shared structural policy for AI Council tickets. The
normative implementation is `council_tools.ticket_policy.TICKET_POLICY_V1`.
Councils, Codex and Claude hooks, and GitHub checks must consume that policy
instead of maintaining their own label or issue-body rules.

Parsing is not admission. A structurally valid ticket can still be untriaged,
blocked, oversized, stale, or unauthorized. The admission predicate is a
separate downstream control.

## Canonical labels

The governed namespaces and exact v1 spellings are:

| Group | Exact labels | Structural cardinality |
| --- | --- | --- |
| Priority | `priority:P0`, `priority:P1` | zero or one |
| Size | `size:1`, `size:2`, `size:3` | zero or one |
| Agent state | `agent:ready`, `agent:claimed`, `agent:blocked` | zero or one |
| Work type | `work:bug`, `work:change`, `work:investigation` | zero or one |
| Split state | `needs-split` | zero or one |

Unknown labels outside these namespaces are permitted. Unknown or mis-cased
labels that look governed are rejected, including `Priority:P0`, `size:4`, and
`Needs-Split`. Duplicate labels and multiple labels from one governed group are
also rejected.

The parser returns contract-shaped values, not label spellings: `P0`, integer
points `1` through `3`, agent state `ready`, and work type `change`. Any group
may be absent from a structurally valid result.

## Agent and split states

These are the legal structural combinations:

| `needs-split` | Agent state | Structural result |
| --- | --- | --- |
| absent | absent, ready, claimed, or blocked | valid |
| present | absent or blocked | valid but non-implementable |
| present | ready or claimed | invalid |

`needs-split` means the work has not been decomposed to a one-, two-, or
three-point implementation ticket. It therefore does not require a size label.
Later admission policy must require a complete priority, size, work type,
reviewed contract, and the appropriate ready state before implementation.

## Issue-body blocks

A governed issue embeds two raw JSON objects inside prose. The JSON blocks are
normative when prose and machine data disagree. Each of the following four
markers must occur exactly once, with the contract block before the review
reference block. These four rendered spellings are exact; clients must not
accept case or whitespace variants:

1. <code>&lt;!-- ai-council:ticket-contract:v1:start --&gt;</code>
2. <code>&lt;!-- ai-council:ticket-contract:v1:end --&gt;</code>
3. <code>&lt;!-- ai-council:ticket-review-ref:v1:start --&gt;</code>
4. <code>&lt;!-- ai-council:ticket-review-ref:v1:end --&gt;</code>

The raw contract JSON object goes between markers 1 and 2. The raw
`reviewRef` JSON object goes between markers 3 and 4.

Only JSON whitespace (`space`, `tab`, `LF`, and `CR`) is removed at block
boundaries. Markdown code fences do not belong inside the markers. Prose may
appear before, between, and after the blocks, but the four-marker order cannot
be interleaved.

Marker text inside prose or JSON makes the marker non-unique and fails closed.
Documentation that discusses markers should therefore escape the HTML comment
delimiters as this file does.

Each payload is first required to be one complete JSON object. The parser then
constructs one ticket envelope and delegates exactly once to
`load_ticket_envelope_json`. That strict loader rejects duplicate keys at every
depth, non-finite numbers, malformed JSON, invalid UTF-8, unknown fields, digest
mismatches, and non-canonical contract values.

The issue body is limited to 65,536 characters and 196,608 UTF-8 bytes. The
synthetic envelope retains its separate 131,072-byte loader limit, so an issue
body may pass its outer bound and still fail the stricter machine-data bound.

## The sizing projection

`points` and `priority` are **outputs** of the sizing review, not author choices. The
content a sizing seat actually reviews is therefore the contract minus those two fields —
the *sizing projection*, computed by `sizing_projection` and digested by
`sizing_projection_sha256`.

The projection is identical for every value of the derived fields. That is deliberate:
`contractSha256` binds the whole contract, so recording a derived value changes the digest
that bound the review that derived it. Because the projection does not move across that
write, the qualification converges rather than chasing its own declared size. The full
procedure is the two-phase seal in `runtime/ticket-sizing-contract.md`.

`SIZING_PROJECTION_KEYS` and `SIZING_DERIVED_KEYS` are **declared independently** and their
partition is checked at import. Defining the reviewed set as a subtraction would make that
check unfalsifiable: a new field would be auto-classified as reviewed, and a reviewed field
could be hidden from the sizing seats by moving it into the derived set. Adding a field to
`CONTRACT_KEYS` now raises until it is deliberately classified.

`contractSha256` binds a contract containing two fields the seats never saw. What ties a
review to the content its seats did see is `sizingProjectionSha256` in the review record:
admission re-derives the projection from the published contract and returns
`review-projection-mismatch` when the two differ. Editing a reviewed field after the review
therefore fails admission even though `contractSha256` was recomputed; editing only the
derived fields does not, because the seats determined them.

The copy of that digest in an issue's Sizing prose is a human-auditable convenience. Nothing
parses it. Like every other digest here, all of this is integrity, not authorization: it does
not prove a seat ran, and one process can still construct both seat reviews and recompute
every digest.

## What the base commit guarantees

A contract pins a `baseCommit`, and that value is reviewed content. What admission checks
about it is deliberately **scope-shaped, not tip-shaped**.

Admission requires two things of the context's base commit:

1. The contract's base commit is an **ancestor** of it. Implementing against a diverged or
   unrelated history fails closed with `base-commit-not-descendant`.
2. **No path that changed between the two commits falls inside the contract's
   `allowedPaths`.** A change that does fails closed with `base-commit-scope-changed`.

Both facts are **resolved by the caller** and supplied in the context as `baseCommitEvidence`,
in exactly the way `dependencyClosure` supplies dependency state. The predicate performs no
repository access, and that is not an oversight to be fixed later: it is what keeps the
decision a pure function of its inputs. Missing or malformed evidence is an `invalid-context`,
never an absent constraint, so a caller cannot obtain admission by leaving it out.

Where the two base commits are identical, nothing can have changed. Evidence claiming otherwise
is self-contradictory and fails closed with `base-commit-mismatch`.

### What this deliberately does not guarantee

**An unrelated change no longer invalidates a qualification.** If a commit lands that touches
no file inside a ticket's `allowedPaths`, that ticket stays admissible. That is the intent.

What is guaranteed unmoved is the reviewed **scope**, not the reviewed **repository**. So a
ticket whose work depends on an interface living outside its own allowed paths is not
re-reviewed when that interface changes — **unless it declares the dependency**.

**A ticket records that dependency in `readPaths`.** It names paths whose change invalidates
the qualification but which the implementation may **not** write. Admission checks changed
paths against it and refuses with `base-commit-read-dependency-changed`, reported separately
from `base-commit-scope-changed`: the first says something the ticket reads moved, the second
says the ticket's own files did, and whoever re-qualifies needs to know which.

An earlier version of this section advised recording such a dependency in `allowedPaths` or
`dependencies` instead. That advice was withdrawn, because neither can hold one: `allowedPaths`
grants **write** access, which is exactly what the ticket's own `outOfScope` forbids, and
`dependencies` holds **issue numbers** and expresses closure rather than any relationship to a
file. `readPaths` grants no write permission anywhere; that separation is the whole reason it
exists as its own field.

#### The case that measured this

#102 added `baseCommitEvidence` to `ticket_admission.CONTEXT_KEYS`. #88's whole job is to build
an admission context, so its work changed materially: its acceptance criteria do not mention the
new key, and an implementation following them would produce a context the predicate rejects.

Admission against the merged `main` nevertheless returned `structurally_eligible = True` with no
reasons, because `ticket_admission.py` is not inside #88's `allowedPaths`, and correctly must
not be. The gap was caught by inspection immediately after the merge, not by any control. A
contract declaring that read dependency would have been refused.

#### Using the field

`readPaths` is **optional, and absence is the only way to say "none"**. An explicitly empty list
is rejected. That is a compatibility requirement rather than a style choice, and it follows from
two properties of the schema:

- contract parsing requires the key set to match exactly, so a key every contract had to carry
  would make every already-published ticket body fail to parse; and
- the canonical form serializes the contract as it stands, so a contract omitting the key
  digests exactly as it did before the field existed, while one carrying it — even as an empty
  list — digests differently.

So a contract written before this field, or one that simply has no read dependencies, is
untouched: it parses, its digest is unchanged, and its admission behaviour is what it always
was. Adding `readPaths` to an existing ticket changes its digest and is a re-qualification.

A path may not appear in both `allowedPaths` and `readPaths`; a path the implementation may
write is not a path whose change should invalidate it, and declaring both is a contradiction
rather than caution.

`readPaths` is **reviewed** content, not derived: what a ticket depends on reading is part of
what a sizing seat is judging, so it appears in the sizing projection.

None of this is an argument for reverting #102. Under exact base-commit equality #88 would have
needed re-qualification too — along with every other qualified ticket, including those with no
relationship to the change. #102 narrowed re-qualification from *every ticket* to *tickets that
depend on what moved*; `readPaths` is what lets a ticket say what it depends on.

### Why it is not exact equality

Exact equality was the original rule. It could not distinguish a commit that changed the
ticket's own files from one that changed something the ticket never names, and treated both as
fatal. The consequence was that a queue of ready tickets was not a reachable state: two tickets
qualified against one commit could never both be admitted, because merging either one moved the
branch and invalidated the other, and every qualification after the first needed a fresh pair of
seat runs against a new projection.

That was measured, not predicted. Issues #88 and #90 were qualified against the same commit with
disjoint allowed paths; #88 admitted cleanly until #90 merged, and then reported
`base-commit-mismatch`.

## Integrity and authorization

`contractSha256` binds the canonical parsed `contract` value. It does not hash
the block's indentation, key order, or escape spelling. Reviewers verifying a
digest must canonicalize the contract using the v1 contract module.

A valid body, a matching digest, and a named review run prove only structure
and integrity. They do not prove that a trusted council or human approved the
ticket. Authorization must come from protected evidence checked by downstream
admission and GitHub controls. Likewise, `agent:claimed` in v1 does not identify
an owner or establish a lease.

## Shared API

- `TICKET_POLICY_V1` is the immutable source of label and marker configuration.
- `parse_ticket_labels(labels)` returns optional structural label values.
- `parse_ticket_issue_body(body)` returns a validated `TicketEnvelope`.
- `sizing_projection(contract)` returns the contract without the review-derived
  fields; `sizing_projection_sha256(contract)` digests it.
- `SIZING_DERIVED_KEYS` and `SIZING_PROJECTION_KEYS` partition `CONTRACT_KEYS`.
- `OPTIONAL_CONTRACT_KEYS` names the keys a contract may omit, and
  `REQUIRED_CONTRACT_KEYS` is the remainder. `readPaths` is the only optional key.
- `TicketPolicyError` covers policy and marker failures.
- `TicketContractError` covers strict JSON and contract failures.

Both error classes derive from `ValueError`, which is the stable common catch
boundary for clients that do not need field-specific diagnostics. Neither
error reflects untrusted label or body content.
