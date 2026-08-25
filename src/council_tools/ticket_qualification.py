"""Compose the ticket-governance primitives into the two-phase seal.

The primitives are pure and well tested, but until now nothing composed them:
every real qualification was performed by a hand-written operator script, and
one of those went stale against a review-schema change and failed mid-session.
This module is the composing layer, so the procedure that is actually used is
covered by the same suite as the pieces it uses.

The procedure is the two-phase seal described in
``runtime/ticket-sizing-contract.md``:

1. **Phase one.**  Build the contract with any placeholder for the review-derived
   fields and hand the seats :func:`phase_one_material` — the sizing projection
   and its digest, never ``points`` or ``priority``.
2. **Phase two.**  Pass the seat reviews to :func:`seal_qualification`.  It
   derives the decision, writes the derived values into the contract, proves the
   projection did not move, and returns the sealed contract with a review record
   bound to both digests.

:func:`render_ticket_body` then turns that into a governed issue body.

Everything here is pure: no network, no GitHub, no filesystem, no subprocess.
Sealing a qualification is an integrity operation, not an authorization one —
one process can still construct both seat reviews and recompute every digest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from council_tools import ticket_contracts, ticket_policy, ticket_review
from council_tools.ticket_contracts import (
    TicketContractError,
    contract_sha256,
    sizing_projection,
    sizing_projection_sha256,
)
from council_tools.ticket_review import TicketReview, validate_ticket_review


class TicketQualificationError(ValueError):
    """A stable, code-addressed failure in the two-phase seal."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"ticket qualification {code}" + (f": {detail}" if detail else ""))


@dataclass(frozen=True)
class PhaseOneMaterial:
    """Exactly what a sizing seat may be shown, and its digest."""

    projection: Mapping[str, Any]
    projection_sha256: str


@dataclass(frozen=True)
class SealedQualification:
    """A contract carrying its derived values, bound to the review that derived them."""

    contract: Mapping[str, Any]
    review_record: Mapping[str, Any]
    review: TicketReview
    contract_sha256: str
    projection_sha256: str

    def review_ref(self) -> dict[str, str]:
        """The ``reviewRef`` block for the issue body."""

        return {
            "runId": self.review.run_id,
            "contractSha256": self.contract_sha256,
        }


def phase_one_material(contract: Mapping[str, Any]) -> PhaseOneMaterial:
    """Return the reviewed content and its digest for a raw contract.

    The projection excludes the review-derived fields, so a seat is never shown a
    proposed value for a value it determines.
    """

    return PhaseOneMaterial(
        projection=sizing_projection(contract),
        projection_sha256=sizing_projection_sha256(contract),
    )


def seal_qualification(
    contract: Mapping[str, Any],
    seat_reviews: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
) -> SealedQualification:
    """Derive the decision, record it in the contract, and bind the review to it.

    Refuses a decision that is not ``eligible``, and refuses if writing the
    derived values moved the projection.  That second check is the reason the
    qualification converges at all, so it is verified rather than assumed.
    """

    if not isinstance(contract, Mapping):
        raise TicketQualificationError("invalid-contract", "contract must be a mapping")
    if not isinstance(run_id, str) or not run_id.strip() or run_id != run_id.strip():
        raise TicketQualificationError("invalid-run-id", "run id must be canonical text")
    if isinstance(seat_reviews, (str, bytes)) or not isinstance(seat_reviews, Sequence):
        raise TicketQualificationError("invalid-seat-reviews", "expected a sequence")

    before = sizing_projection_sha256(contract)
    reviews = [dict(seat) for seat in seat_reviews]

    # Derive against the projection digest first: at this point the contract still
    # carries placeholders, so its own digest is meaningless.
    probe = {
        "schemaVersion": ticket_review.REVIEW_SCHEMA_VERSION,
        "runId": run_id,
        "contractSha256": before,
        "sizingProjectionSha256": before,
        "requiredSeats": list(ticket_review.REQUIRED_SEATS),
        "seatReviews": reviews,
    }
    derived = validate_ticket_review(probe, expected_contract_sha256=before)
    if derived.state != "eligible":
        raise TicketQualificationError(
            "review-not-eligible",
            ", ".join(f"{reason.code}:{reason.seat_id}" for reason in derived.reasons),
        )

    sealed = dict(contract)
    sealed["points"] = derived.points
    sealed["priority"] = derived.priority

    after = sizing_projection_sha256(sealed)
    if after != before:
        raise TicketQualificationError(
            "projection-moved",
            "writing the derived values changed the reviewed content",
        )

    digest = contract_sha256(sealed)
    record = {
        "schemaVersion": ticket_review.REVIEW_SCHEMA_VERSION,
        "runId": run_id,
        "contractSha256": digest,
        "sizingProjectionSha256": after,
        "requiredSeats": list(ticket_review.REQUIRED_SEATS),
        "seatReviews": reviews,
    }
    review = validate_ticket_review(record, expected_contract_sha256=digest)
    ticket_contracts.validate_ticket_envelope(
        {
            "contract": sealed,
            "reviewRef": {"runId": run_id, "contractSha256": digest},
        }
    )
    return SealedQualification(
        contract=sealed,
        review_record=record,
        review=review,
        contract_sha256=digest,
        projection_sha256=after,
    )


def _markers(policy: ticket_policy.TicketPolicy) -> tuple[str, ...]:
    return (
        policy.contract_start_marker,
        policy.contract_end_marker,
        policy.review_ref_start_marker,
        policy.review_ref_end_marker,
    )


def render_ticket_body(
    prose: str,
    contract: Mapping[str, Any],
    review_ref: Mapping[str, Any],
    *,
    policy: ticket_policy.TicketPolicy = ticket_policy.TICKET_POLICY_V1,
) -> str:
    """Render one governed issue body, and prove the parser accepts it.

    Marker text inside the prose would make a marker non-unique and fail the
    parser closed, so it is refused here rather than published.
    """

    if not isinstance(prose, str):
        raise TicketQualificationError("invalid-prose", "prose must be text")
    for marker in _markers(policy):
        if marker in prose:
            raise TicketQualificationError(
                "prose-contains-marker", f"prose must not contain {marker!r}"
            )
    try:
        contract_json = json.dumps(dict(contract), indent=2, ensure_ascii=False)
        review_json = json.dumps(dict(review_ref), indent=2, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise TicketQualificationError("non-renderable-json", str(exc)) from exc

    body = "\n".join(
        (
            prose.rstrip(),
            "",
            policy.contract_start_marker,
            contract_json,
            policy.contract_end_marker,
            "",
            policy.review_ref_start_marker,
            review_json,
            policy.review_ref_end_marker,
            "",
        )
    )
    # A body this module renders must be one the policy parser accepts; proving
    # it here means a caller cannot publish a body that fails admission later.
    try:
        ticket_policy.parse_ticket_issue_body(body)
    except (ticket_policy.TicketPolicyError, TicketContractError) as exc:
        raise TicketQualificationError("unparseable-body", str(exc)) from exc
    return body


__all__ = [
    "PhaseOneMaterial",
    "SealedQualification",
    "TicketQualificationError",
    "phase_one_material",
    "render_ticket_body",
    "seal_qualification",
]
