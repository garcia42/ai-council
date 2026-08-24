"""Pure v1 policy for governed ticket labels and issue-body blocks.

This module parses structure only.  It does not decide whether a ticket is
admissible for implementation, mutate labels, call GitHub, or treat a valid
review-reference digest as authorization.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import council_tools.ticket_contracts as ticket_contracts


POLICY_VERSION = 1
PRIORITY_PREFIX = "priority:"
SIZE_PREFIX = "size:"
AGENT_STATE_PREFIX = "agent:"
WORK_TYPE_PREFIX = "work:"

PRIORITY_LABELS = frozenset({"priority:P0", "priority:P1"})
SIZE_LABELS = frozenset({"size:1", "size:2", "size:3"})
AGENT_STATE_LABELS = frozenset(
    {"agent:ready", "agent:claimed", "agent:blocked"}
)
WORK_TYPE_LABELS = frozenset(
    {"work:bug", "work:change", "work:investigation"}
)
NEEDS_SPLIT_LABEL = "needs-split"

CONTRACT_START_MARKER = "<!-- ai-council:ticket-contract:v1:start -->"
CONTRACT_END_MARKER = "<!-- ai-council:ticket-contract:v1:end -->"
REVIEW_REF_START_MARKER = "<!-- ai-council:ticket-review-ref:v1:start -->"
REVIEW_REF_END_MARKER = "<!-- ai-council:ticket-review-ref:v1:end -->"

MAX_ISSUE_BODY_CHARACTERS = 65_536
MAX_ISSUE_BODY_BYTES = 196_608
_ASCII_JSON_WHITESPACE = " \t\n\r"


class TicketPolicyError(ValueError):
    """A stable, field-addressed ticket-policy parsing failure."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"ticket policy {code} at {field}")


@dataclass(frozen=True)
class TicketPolicy:
    """The complete immutable configuration for one ticket-policy version."""

    policy_version: int
    priority_prefix: str
    size_prefix: str
    agent_state_prefix: str
    work_type_prefix: str
    priority_labels: frozenset[str]
    size_labels: frozenset[str]
    agent_state_labels: frozenset[str]
    work_type_labels: frozenset[str]
    needs_split_label: str
    contract_start_marker: str
    contract_end_marker: str
    review_ref_start_marker: str
    review_ref_end_marker: str
    max_issue_body_characters: int
    max_issue_body_bytes: int


@dataclass(frozen=True)
class ParsedTicketLabels:
    """Structurally parsed labels using contract-shaped optional values."""

    priority: str | None
    points: int | None
    agent_state: str | None
    work_type: str | None
    needs_split: bool


TICKET_POLICY_V1 = TicketPolicy(
    policy_version=POLICY_VERSION,
    priority_prefix=PRIORITY_PREFIX,
    size_prefix=SIZE_PREFIX,
    agent_state_prefix=AGENT_STATE_PREFIX,
    work_type_prefix=WORK_TYPE_PREFIX,
    priority_labels=PRIORITY_LABELS,
    size_labels=SIZE_LABELS,
    agent_state_labels=AGENT_STATE_LABELS,
    work_type_labels=WORK_TYPE_LABELS,
    needs_split_label=NEEDS_SPLIT_LABEL,
    contract_start_marker=CONTRACT_START_MARKER,
    contract_end_marker=CONTRACT_END_MARKER,
    review_ref_start_marker=REVIEW_REF_START_MARKER,
    review_ref_end_marker=REVIEW_REF_END_MARKER,
    max_issue_body_characters=MAX_ISSUE_BODY_CHARACTERS,
    max_issue_body_bytes=MAX_ISSUE_BODY_BYTES,
)


def _governed_group(
    labels: set[str],
    *,
    prefix: str,
    allowed: frozenset[str],
    unknown_code: str,
    multiple_code: str,
) -> str | None:
    folded_prefix = prefix.casefold()
    governed = {
        label for label in labels if label.casefold().startswith(folded_prefix)
    }
    if any(label not in allowed for label in governed):
        raise TicketPolicyError(unknown_code, "labels")
    selected = governed & allowed
    if len(selected) > 1:
        raise TicketPolicyError(multiple_code, "labels")
    if not selected:
        return None
    return next(iter(selected))


def parse_ticket_labels(labels: Any) -> ParsedTicketLabels:
    """Parse governed label structure without making an admission decision.

    A governed group may be absent because newly filed and ``needs-split``
    issues are valid structural inputs.  Later admission policy is responsible
    for requiring complete implementation labels and the ``ready`` state.
    """

    if type(labels) is not list and type(labels) is not tuple:
        raise TicketPolicyError("invalid-label-collection", "labels")
    if any(type(label) is not str for label in labels):
        raise TicketPolicyError("invalid-label-type", "labels")
    label_set = set(labels)
    if len(label_set) != len(labels):
        raise TicketPolicyError("duplicate-label", "labels")

    priority_label = _governed_group(
        label_set,
        prefix=PRIORITY_PREFIX,
        allowed=PRIORITY_LABELS,
        unknown_code="unknown-priority-label",
        multiple_code="multiple-priority-labels",
    )
    size_label = _governed_group(
        label_set,
        prefix=SIZE_PREFIX,
        allowed=SIZE_LABELS,
        unknown_code="unknown-size-label",
        multiple_code="multiple-size-labels",
    )
    agent_state_label = _governed_group(
        label_set,
        prefix=AGENT_STATE_PREFIX,
        allowed=AGENT_STATE_LABELS,
        unknown_code="unknown-agent-state-label",
        multiple_code="multiple-agent-state-labels",
    )
    work_type_label = _governed_group(
        label_set,
        prefix=WORK_TYPE_PREFIX,
        allowed=WORK_TYPE_LABELS,
        unknown_code="unknown-work-type-label",
        multiple_code="multiple-work-type-labels",
    )

    needs_split_near_misses = {
        label
        for label in label_set
        if label.casefold() == NEEDS_SPLIT_LABEL.casefold()
        and label != NEEDS_SPLIT_LABEL
    }
    if needs_split_near_misses:
        raise TicketPolicyError("unknown-needs-split-label", "labels")
    needs_split = NEEDS_SPLIT_LABEL in label_set
    if needs_split and agent_state_label in {"agent:ready", "agent:claimed"}:
        raise TicketPolicyError("needs-split-eligible", "labels")

    return ParsedTicketLabels(
        priority=(
            priority_label.removeprefix(PRIORITY_PREFIX)
            if priority_label is not None
            else None
        ),
        points=(
            int(size_label.removeprefix(SIZE_PREFIX))
            if size_label is not None
            else None
        ),
        agent_state=(
            agent_state_label.removeprefix(AGENT_STATE_PREFIX)
            if agent_state_label is not None
            else None
        ),
        work_type=(
            work_type_label.removeprefix(WORK_TYPE_PREFIX)
            if work_type_label is not None
            else None
        ),
        needs_split=needs_split,
    )


def _marker_index(body: str, marker: str, *, field: str) -> int:
    count = body.count(marker)
    if count == 0:
        raise TicketPolicyError("missing-marker", field)
    if count != 1:
        raise TicketPolicyError("duplicate-marker", field)
    return body.index(marker)


def _json_object_shape(
    payload: str, *, code_name: str, field: str
) -> None:
    try:
        parsed = json.loads(payload)
    except (RecursionError, ValueError):
        raise TicketPolicyError(f"invalid-{code_name}-json", field) from None
    if type(parsed) is not dict:
        raise TicketPolicyError(f"invalid-{code_name}-json-object", field)


def parse_ticket_issue_body(body: Any) -> ticket_contracts.TicketEnvelope:
    """Extract and validate the unique v1 contract and review-ref blocks.

    Marker and shape failures raise :class:`TicketPolicyError`.  Strict JSON or
    contract failures from the delegated loader remain ``TicketContractError``
    instances.  Both derive from ``ValueError``, which is the stable catch base
    for callers that do not need field-specific diagnostics.
    """

    if type(body) is not str:
        raise TicketPolicyError("invalid-issue-body-type", "body")
    if len(body) > MAX_ISSUE_BODY_CHARACTERS:
        raise TicketPolicyError("issue-body-too-large", "body")
    try:
        encoded_length = len(body.encode("utf-8"))
    except UnicodeEncodeError:
        raise TicketPolicyError("invalid-issue-body-encoding", "body") from None
    if encoded_length > MAX_ISSUE_BODY_BYTES:
        raise TicketPolicyError("issue-body-too-large", "body")

    contract_start = _marker_index(
        body, CONTRACT_START_MARKER, field="contractStartMarker"
    )
    contract_end = _marker_index(
        body, CONTRACT_END_MARKER, field="contractEndMarker"
    )
    review_ref_start = _marker_index(
        body, REVIEW_REF_START_MARKER, field="reviewRefStartMarker"
    )
    review_ref_end = _marker_index(
        body, REVIEW_REF_END_MARKER, field="reviewRefEndMarker"
    )
    if not (contract_start < contract_end < review_ref_start < review_ref_end):
        raise TicketPolicyError("invalid-marker-order", "body")

    contract_payload = body[
        contract_start + len(CONTRACT_START_MARKER) : contract_end
    ].strip(_ASCII_JSON_WHITESPACE)
    review_ref_payload = body[
        review_ref_start + len(REVIEW_REF_START_MARKER) : review_ref_end
    ].strip(_ASCII_JSON_WHITESPACE)

    _json_object_shape(
        contract_payload, code_name="contract", field="contractBlock"
    )
    _json_object_shape(
        review_ref_payload, code_name="review-ref", field="reviewRefBlock"
    )
    envelope = (
        '{"contract":'
        + contract_payload
        + ',"reviewRef":'
        + review_ref_payload
        + "}"
    )
    return ticket_contracts.load_ticket_envelope_json(envelope)


__all__ = [
    "AGENT_STATE_LABELS",
    "AGENT_STATE_PREFIX",
    "CONTRACT_END_MARKER",
    "CONTRACT_START_MARKER",
    "MAX_ISSUE_BODY_BYTES",
    "MAX_ISSUE_BODY_CHARACTERS",
    "NEEDS_SPLIT_LABEL",
    "POLICY_VERSION",
    "PRIORITY_LABELS",
    "PRIORITY_PREFIX",
    "ParsedTicketLabels",
    "REVIEW_REF_END_MARKER",
    "REVIEW_REF_START_MARKER",
    "SIZE_LABELS",
    "SIZE_PREFIX",
    "TICKET_POLICY_V1",
    "TicketPolicy",
    "TicketPolicyError",
    "WORK_TYPE_LABELS",
    "WORK_TYPE_PREFIX",
    "parse_ticket_issue_body",
    "parse_ticket_labels",
]
