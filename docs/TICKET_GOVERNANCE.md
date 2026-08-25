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

`SIZING_PROJECTION_KEYS` and `SIZING_DERIVED_KEYS` partition `CONTRACT_KEYS`, so a new
contract field cannot be added without being classified as reviewed or derived.

Consequently `contractSha256` binds a contract containing two fields the seats never saw.
The projection digest is what proves the reviewed content, and it belongs in the issue's
Sizing prose so an auditor can re-derive it from the published contract. Like every other
digest here, it is integrity, not authorization.

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
- `TicketPolicyError` covers policy and marker failures.
- `TicketContractError` covers strict JSON and contract failures.

Both error classes derive from `ValueError`, which is the stable common catch
boundary for clients that do not need field-specific diagnostics. Neither
error reflects untrusted label or body content.
