"""Pure v1 sizing and splitting review records for implementation tickets.

``reviewSha256`` is an unauthenticated content address.  A valid record proves
structure and integrity only: it does not prove that either named seat ran, and
one process can mint both seat reviews.  Authorization is deliberately deferred
to protected evidence outside this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Any, Mapping

import council_tools.ticket_contracts as ticket_contracts


REVIEW_SCHEMA_VERSION = 2
REQUIRED_SEATS = ("claude", "codex")
MAX_ENGINEER_DAYS = 30
ELIGIBLE_MAX_DAYS = 3
MAX_CONFIDENCE = 100
MAX_SPLIT_REASONS = 16
MAX_REASON_LENGTH = 2_048
MAX_REVIEW_RECORD_BYTES = 65_536
MAX_REVIEW_JSON_BYTES = 2 * MAX_REVIEW_RECORD_BYTES

REVIEW_KEYS = frozenset(
    {
        "schemaVersion",
        "runId",
        "contractSha256",
        "sizingProjectionSha256",
        "requiredSeats",
        "seatReviews",
    }
)
SUBMITTED_REVIEW_KEYS = frozenset(
    {
        "seatId",
        "status",
        "engineerDays",
        "singleOutcome",
        "splitReasons",
        "priority",
        "confidence",
    }
)
UNAVAILABLE_REVIEW_KEYS = frozenset({"seatId", "status", "reason"})

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class TicketReviewError(ValueError):
    """A stable, field-addressed ticket-review validation failure."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"ticket review {code} at {field}")


@dataclass(frozen=True)
class SeatReview:
    """One normalized submitted or unavailable required-seat review."""

    seat_id: str
    status: str
    engineer_days: int | None = None
    single_outcome: bool | None = None
    split_reasons: tuple[str, ...] = ()
    priority: str | None = None
    confidence: int | None = None
    unavailable_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        if self.status == "unavailable":
            return {
                "seatId": self.seat_id,
                "status": self.status,
                "reason": self.unavailable_reason,
            }
        return {
            "seatId": self.seat_id,
            "status": self.status,
            "engineerDays": self.engineer_days,
            "singleOutcome": self.single_outcome,
            "splitReasons": list(self.split_reasons),
            "priority": self.priority,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class DecisionReason:
    """One deterministic non-reflective reason for a needs-split result."""

    code: str
    seat_id: str


@dataclass(frozen=True)
class TicketReview:
    """A deeply immutable normalized record plus its derived decision."""

    schema_version: int
    run_id: str
    contract_sha256: str
    sizing_projection_sha256: str
    required_seats: tuple[str, ...]
    seat_reviews: tuple[SeatReview, ...]
    state: str
    points: int | None
    priority: str | None
    confidence: int | None
    reasons: tuple[DecisionReason, ...]
    review_sha256: str

    def as_record_dict(self) -> dict[str, Any]:
        """Return only the reviewed input record, excluding derived fields."""

        return {
            "schemaVersion": self.schema_version,
            "runId": self.run_id,
            "contractSha256": self.contract_sha256,
            "sizingProjectionSha256": self.sizing_projection_sha256,
            "requiredSeats": list(self.required_seats),
            "seatReviews": [seat.as_dict() for seat in self.seat_reviews],
        }


def _mapping_with_exact_keys(
    value: Any,
    expected: frozenset[str],
    *,
    code: str,
    field: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise TicketReviewError(code, field)
    return value


def _canonical_text(value: Any, *, max_length: int) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= max_length
        and value == value.strip()
        and "\x00" not in value
        and not any(0xD800 <= ord(character) <= 0xDFFF for character in value)
        and unicodedata.normalize("NFC", value) == value
    )


def _canonical_run_id(value: Any) -> bool:
    """Mirror the public ticket-contract run-id wire constraint."""

    return _canonical_text(value, max_length=ticket_contracts.MAX_RUN_ID_LENGTH)


def _exact_int(value: Any, *, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _split_reasons(value: Any, *, field: str) -> tuple[str, ...]:
    if type(value) is not list or len(value) > MAX_SPLIT_REASONS:
        raise TicketReviewError("invalid-split-reasons", field)
    result: list[str] = []
    seen: set[str] = set()
    for reason in value:
        if (
            not _canonical_text(reason, max_length=MAX_REASON_LENGTH)
            or reason in seen
        ):
            raise TicketReviewError("invalid-split-reasons", field)
        result.append(reason)
        seen.add(reason)
    return tuple(result)


def _normalize_seat_review(
    raw: Any, *, expected_seat_id: str, index: int
) -> SeatReview:
    field = f"review.seatReviews[{index}]"
    if not isinstance(raw, Mapping):
        raise TicketReviewError("invalid-seat-review", field)
    status = raw.get("status")
    if type(status) is not str or status not in {"submitted", "unavailable"}:
        raise TicketReviewError("invalid-seat-status", f"{field}.status")

    if status == "unavailable":
        item = _mapping_with_exact_keys(
            raw,
            UNAVAILABLE_REVIEW_KEYS,
            code="invalid-unavailable-review-keys",
            field=field,
        )
        if (
            type(item["seatId"]) is not str
            or item["seatId"] != expected_seat_id
        ):
            raise TicketReviewError("invalid-seat-id", f"{field}.seatId")
        reason = item["reason"]
        if not _canonical_text(reason, max_length=MAX_REASON_LENGTH):
            raise TicketReviewError(
                "invalid-unavailable-reason", f"{field}.reason"
            )
        return SeatReview(
            seat_id=expected_seat_id,
            status="unavailable",
            unavailable_reason=reason,
        )

    item = _mapping_with_exact_keys(
        raw,
        SUBMITTED_REVIEW_KEYS,
        code="invalid-submitted-review-keys",
        field=field,
    )
    if type(item["seatId"]) is not str or item["seatId"] != expected_seat_id:
        raise TicketReviewError("invalid-seat-id", f"{field}.seatId")

    engineer_days = item["engineerDays"]
    if not _exact_int(
        engineer_days, minimum=1, maximum=MAX_ENGINEER_DAYS
    ):
        raise TicketReviewError(
            "invalid-engineer-days", f"{field}.engineerDays"
        )

    single_outcome = item["singleOutcome"]
    if type(single_outcome) is not bool:
        raise TicketReviewError(
            "invalid-single-outcome", f"{field}.singleOutcome"
        )
    split_reasons = _split_reasons(
        item["splitReasons"], field=f"{field}.splitReasons"
    )
    if single_outcome != (not split_reasons):
        raise TicketReviewError(
            "inconsistent-single-outcome", f"{field}.singleOutcome"
        )

    priority = item["priority"]
    if type(priority) is not str or priority not in ticket_contracts.PRIORITIES:
        raise TicketReviewError("invalid-priority", f"{field}.priority")

    confidence = item["confidence"]
    if not _exact_int(confidence, minimum=0, maximum=MAX_CONFIDENCE):
        raise TicketReviewError("invalid-confidence", f"{field}.confidence")

    return SeatReview(
        seat_id=expected_seat_id,
        status="submitted",
        engineer_days=engineer_days,
        single_outcome=single_outcome,
        split_reasons=split_reasons,
        priority=priority,
        confidence=confidence,
    )


def _canonical_review_bytes(review: TicketReview) -> bytes:
    try:
        return json.dumps(
            review.as_record_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise TicketReviewError("invalid-normalized-review", "review") from exc


def canonical_review_bytes(review: Any) -> bytes:
    """Return canonical bytes for an already validated review value."""

    if type(review) is not TicketReview:
        raise TicketReviewError("invalid-normalized-review", "review")
    return _canonical_review_bytes(review)


def review_sha256(review: Any) -> str:
    """Return the unauthenticated content address of a validated review."""

    return hashlib.sha256(canonical_review_bytes(review)).hexdigest()


def validate_ticket_review(
    value: Mapping[str, Any], *, expected_contract_sha256: str
) -> TicketReview:
    """Validate and derive one immutable v1 review bound to a contract digest."""

    if (
        type(expected_contract_sha256) is not str
        or not _SHA256_RE.fullmatch(expected_contract_sha256)
    ):
        raise TicketReviewError(
            "invalid-expected-contract-sha256", "expectedContractSha256"
        )

    if not isinstance(value, Mapping):
        raise TicketReviewError("invalid-review-keys", "review")
    if "schemaVersion" in value:
        schema_version = value["schemaVersion"]
        if (
            type(schema_version) is not int
            or schema_version != REVIEW_SCHEMA_VERSION
        ):
            raise TicketReviewError(
                "unsupported-schema-version", "review.schemaVersion"
            )
    raw = _mapping_with_exact_keys(
        value, REVIEW_KEYS, code="invalid-review-keys", field="review"
    )

    run_id = raw["runId"]
    if not _canonical_run_id(run_id):
        raise TicketReviewError("invalid-run-id", "review.runId")

    contract_sha256 = raw["contractSha256"]
    if type(contract_sha256) is not str or not _SHA256_RE.fullmatch(
        contract_sha256
    ):
        raise TicketReviewError(
            "invalid-contract-sha256", "review.contractSha256"
        )
    if contract_sha256 != expected_contract_sha256:
        raise TicketReviewError(
            "contract-sha256-mismatch", "review.contractSha256"
        )

    sizing_projection_sha256 = raw["sizingProjectionSha256"]
    if type(sizing_projection_sha256) is not str or not _SHA256_RE.fullmatch(
        sizing_projection_sha256
    ):
        raise TicketReviewError(
            "invalid-sizing-projection-sha256", "review.sizingProjectionSha256"
        )

    required_seats = raw["requiredSeats"]
    if (
        type(required_seats) is not list
        or any(type(seat_id) is not str for seat_id in required_seats)
        or required_seats != list(REQUIRED_SEATS)
    ):
        raise TicketReviewError(
            "invalid-required-seats", "review.requiredSeats"
        )

    raw_seat_reviews = raw["seatReviews"]
    if (
        type(raw_seat_reviews) is not list
        or len(raw_seat_reviews) != len(REQUIRED_SEATS)
    ):
        raise TicketReviewError("invalid-seat-reviews", "review.seatReviews")
    seat_reviews = tuple(
        _normalize_seat_review(item, expected_seat_id=seat_id, index=index)
        for index, (seat_id, item) in enumerate(
            zip(REQUIRED_SEATS, raw_seat_reviews, strict=True)
        )
    )

    reasons: list[DecisionReason] = []
    submitted: list[SeatReview] = []
    has_unavailable = False
    for seat in seat_reviews:
        if seat.status == "unavailable":
            has_unavailable = True
            reasons.append(
                DecisionReason(code="seat-unavailable", seat_id=seat.seat_id)
            )
            continue
        submitted.append(seat)
        if seat.engineer_days is not None and seat.engineer_days > ELIGIBLE_MAX_DAYS:
            reasons.append(
                DecisionReason(
                    code="estimate-over-three", seat_id=seat.seat_id
                )
            )
        if seat.single_outcome is False:
            reasons.append(
                DecisionReason(code="multiple-outcomes", seat_id=seat.seat_id)
            )

    state = "eligible" if not reasons else "needs-split"
    points = (
        max(seat.engineer_days for seat in submitted)
        if state == "eligible" and submitted
        else None
    )
    if has_unavailable or not submitted:
        priority = None
        confidence = None
    else:
        priority = (
            "P0" if any(seat.priority == "P0" for seat in submitted) else "P1"
        )
        confidence = min(seat.confidence for seat in submitted)

    normalized = TicketReview(
        schema_version=REVIEW_SCHEMA_VERSION,
        run_id=run_id,
        contract_sha256=contract_sha256,
        sizing_projection_sha256=sizing_projection_sha256,
        required_seats=REQUIRED_SEATS,
        seat_reviews=seat_reviews,
        state=state,
        points=points,
        priority=priority,
        confidence=confidence,
        reasons=tuple(reasons),
        review_sha256="",
    )
    canonical = _canonical_review_bytes(normalized)
    if len(canonical) > MAX_REVIEW_RECORD_BYTES:
        raise TicketReviewError("review-too-large", "review")
    return replace(
        normalized, review_sha256=hashlib.sha256(canonical).hexdigest()
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TicketReviewError("duplicate-json-key", "$")
        result[key] = value
    return result


def _reject_json_constant(_name: str) -> None:
    raise TicketReviewError("non-finite-json-number", "$")


def load_ticket_review_json(
    document: Any, *, expected_contract_sha256: str
) -> TicketReview:
    """Strictly decode and validate one v1 ticket-review JSON document."""

    if type(document) is str:
        if len(document) > MAX_REVIEW_JSON_BYTES:
            raise TicketReviewError("review-json-too-large", "$")
        try:
            encoded_length = len(document.encode("utf-8"))
        except UnicodeEncodeError:
            raise TicketReviewError(
                "invalid-review-json-encoding", "$"
            ) from None
        if encoded_length > MAX_REVIEW_JSON_BYTES:
            raise TicketReviewError("review-json-too-large", "$")
        text = document
    elif type(document) is bytes or type(document) is bytearray:
        if len(document) > MAX_REVIEW_JSON_BYTES:
            raise TicketReviewError("review-json-too-large", "$")
        try:
            text = bytes(document).decode("utf-8")
        except UnicodeDecodeError:
            raise TicketReviewError(
                "invalid-review-json-encoding", "$"
            ) from None
    else:
        raise TicketReviewError("invalid-review-json-type", "$")

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except TicketReviewError:
        raise
    except RecursionError:
        raise TicketReviewError("review-json-too-deep", "$") from None
    except ValueError:
        raise TicketReviewError("invalid-review-json", "$") from None

    if type(parsed) is not dict:
        raise TicketReviewError("invalid-review-json-top-level", "$")
    return validate_ticket_review(
        parsed, expected_contract_sha256=expected_contract_sha256
    )


__all__ = [
    "DecisionReason",
    "ELIGIBLE_MAX_DAYS",
    "MAX_CONFIDENCE",
    "MAX_ENGINEER_DAYS",
    "MAX_REASON_LENGTH",
    "MAX_REVIEW_JSON_BYTES",
    "MAX_REVIEW_RECORD_BYTES",
    "MAX_SPLIT_REASONS",
    "REQUIRED_SEATS",
    "REVIEW_SCHEMA_VERSION",
    "SeatReview",
    "TicketReview",
    "TicketReviewError",
    "canonical_review_bytes",
    "load_ticket_review_json",
    "review_sha256",
    "validate_ticket_review",
]
