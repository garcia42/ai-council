"""Assemble one deterministic ticket-creation plan from a council decomposition.

Two sibling modules each answer one question about a set of proposed tickets and
neither produces anything an operator can act on.
:func:`~council_tools.ticket_labels.derive_ticket_labels` turns a sizing decision
into the governed label set that decision authorizes.
:func:`~council_tools.ticket_ordering.order_children` turns sibling references
named by local key into a creation order.  Between them sits the document that
says what will be created, and it is the artifact the planning step exists to
produce: without it, the two results live in a caller's memory and the plan a
reviewer reads is still whatever someone typed.

**The children do not all have the same shape.**  ``seal_qualification`` refuses
any decision that is not ``eligible``, so a child whose seats found it
``needs-split`` has no sealed contract, and ``render_ticket_body`` has no
contract and no review reference to render against.  A plan that pretended
otherwise would have to invent one.  So a planned child is either sealed and
rendered and ready to become an issue body, or sized, flagged for splitting, and
able to become only a placeholder for a decomposition someone still has to do.

**Reproducibility is what makes a plan worth writing down.**  The only use for a
plan is comparing it against something: a second run, to see that nothing moved;
what was actually created, to see that nothing was missed; a reviewer's
expectation.  Sorted keys and an order fixed by the sibling relations rather than
by the caller's sequence are what make that hold, and both come from the pieces
this composes.

**One rule this must not restate.**  Whether a set of seat reviews is eligible is
derived by ``validate_ticket_review`` and by nothing else, and the two-phase seal
derives it against the *projection* digest rather than the contract digest,
because at that point the contract still carries placeholder values for the two
review-derived fields.  Getting a child's decision therefore means asking that
function against the digest ``phase_one_material`` returns.  Every relation
refusal likewise stays with ``order_children``, which is called before any check
here so its narrower messages are not pre-empted.

Everything here is pure: no network, no GitHub, no filesystem, no subprocess.
The plan says what *would* be created; applying it is not part of this.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from council_tools.ticket_labels import derive_ticket_labels
from council_tools.ticket_ordering import order_children
from council_tools.ticket_qualification import (
    TicketQualificationError,
    phase_one_material,
    render_ticket_body,
    seal_qualification,
)
from council_tools.ticket_review import (
    REQUIRED_SEATS,
    REVIEW_SCHEMA_VERSION,
    validate_ticket_review,
)

PLAN_SCHEMA_VERSION = 1

DOCUMENT_KEYS = frozenset({"repository", "targetBranch", "baseCommit", "children"})
CHILD_KEYS = frozenset({"prose", "contract", "seatReviews", "dependsOn"})


class TicketPlanError(ValueError):
    """A stable, field-addressed planning failure."""

    def __init__(self, code: str, field: str, detail: str = ""):
        self.code = code
        self.field = field
        self.detail = detail
        super().__init__(
            f"ticket plan {code} at {field}" + (f": {detail}" if detail else "")
        )


@dataclass(frozen=True)
class PlannedChild:
    """One child of a plan, in whichever of the two shapes its decision allows."""

    local_key: str
    labels: tuple[str, ...]
    decision: str
    depends_on: tuple[str, ...]
    contract: Mapping[str, Any] | None
    contract_sha256: str | None
    projection_sha256: str
    body: str | None
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        planned: dict[str, Any] = {
            "localKey": self.local_key,
            "labels": list(self.labels),
            "decision": self.decision,
            "dependsOn": list(self.depends_on),
            "projectionSha256": self.projection_sha256,
        }
        if self.contract is not None:
            planned["contract"] = dict(self.contract)
            planned["contractSha256"] = self.contract_sha256
            planned["body"] = self.body
        else:
            planned["reasons"] = list(self.reasons)
        return planned


@dataclass(frozen=True)
class TicketPlan:
    """A deterministic description of the tickets a decomposition would create."""

    schema_version: int
    repository: str
    target_branch: str
    base_commit: str
    run_id: str
    children: tuple[PlannedChild, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "repository": self.repository,
            "targetBranch": self.target_branch,
            "baseCommit": self.base_commit,
            "runId": self.run_id,
            "children": [child.as_dict() for child in self.children],
        }


def canonical_plan_bytes(plan: TicketPlan) -> bytes:
    """Return the canonical JSON bytes for a plan.

    Same canonical form as the contract module's: sorted keys, compact
    separators, Unicode emitted directly, no non-finite values.  The children
    keep their creation order, because that order is the plan's content rather
    than an incidental sequence.
    """

    if not isinstance(plan, TicketPlan):
        raise TicketPlanError("invalid-plan", "plan")
    try:
        return json.dumps(
            plan.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise TicketPlanError("non-canonical-json", "plan") from exc


def _mapping(value: Any, code: str, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TicketPlanError(code, field)
    return value


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    if set(value) != expected:
        raise TicketPlanError("unknown-keys", field, ",".join(sorted(set(value) ^ expected)))


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TicketPlanError("invalid-text", field)
    return value


def plan_tickets(document: Any, *, run_id: str) -> TicketPlan:
    """Build the plan one decomposition document describes."""

    doc = _mapping(document, "invalid-document", "document")
    _exact_keys(doc, DOCUMENT_KEYS, "document")
    repository = _text(doc["repository"], "document.repository")
    target_branch = _text(doc["targetBranch"], "document.targetBranch")
    base_commit = _text(doc["baseCommit"], "document.baseCommit")
    run = _text(run_id, "runId")

    children = _mapping(doc["children"], "invalid-children", "document.children")
    prepared: dict[str, Mapping[str, Any]] = {}
    for key, child in children.items():
        field = f"document.children[{key!r}]"
        value = _mapping(child, "invalid-child", field)
        _exact_keys(value, CHILD_KEYS, field)
        prepared[key] = value

    # Ordering runs first, deliberately.  Every relation refusal belongs to
    # order_children and is narrower than anything below; running a check here
    # first would report a disagreement where the real answer is that the
    # relations do not describe an order at all.
    order = order_children({key: value["dependsOn"] for key, value in prepared.items()})

    planned = tuple(
        _plan_child(
            key,
            prepared[key],
            repository=repository,
            target_branch=target_branch,
            run_id=run,
        )
        for key in order
    )
    # Last, deliberately.  Every refusal above is narrower and names one child;
    # this one is about the set and would otherwise pre-empt their messages.
    seen: dict[int, str] = {}
    for child in planned:
        number = child.contract["issueNumber"] if child.contract is not None else None
        if number is None:
            number = prepared[child.local_key]["contract"].get("issueNumber")
        if number in seen:
            raise TicketPlanError(
                "duplicate-issue-number",
                "document.children",
                f"{seen[number]} and {child.local_key} both claim {number}",
            )
        seen[number] = child.local_key

    return TicketPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        repository=repository,
        target_branch=target_branch,
        base_commit=base_commit,
        run_id=run,
        children=planned,
    )


def _plan_child(
    local_key: str,
    child: Mapping[str, Any],
    *,
    repository: str,
    target_branch: str,
    run_id: str,
) -> PlannedChild:
    field = f"document.children[{local_key!r}]"
    contract = _mapping(child["contract"], "invalid-contract", f"{field}.contract")
    prose = child["prose"]
    if not isinstance(prose, str):
        raise TicketPlanError("invalid-prose", f"{field}.prose")

    seat_reviews = child["seatReviews"]
    if isinstance(seat_reviews, (str, bytes)) or not isinstance(seat_reviews, Sequence):
        raise TicketPlanError("invalid-seat-reviews", f"{field}.seatReviews")

    # A plan that mixed repositories, branches or issue numbers would create
    # tickets against the wrong thing, and nothing downstream re-checks it.
    if contract.get("repository") != repository:
        raise TicketPlanError("repository-mismatch", f"{field}.contract")
    if contract.get("targetBranch") != target_branch:
        raise TicketPlanError("target-branch-mismatch", f"{field}.contract")

    material = phase_one_material(contract)
    projection_sha256 = material.projection_sha256
    probe = {
        "schemaVersion": REVIEW_SCHEMA_VERSION,
        "runId": run_id,
        "contractSha256": projection_sha256,
        "sizingProjectionSha256": projection_sha256,
        "requiredSeats": list(REQUIRED_SEATS),
        "seatReviews": [dict(seat) for seat in seat_reviews],
    }
    review = validate_ticket_review(probe, expected_contract_sha256=projection_sha256)
    labels = derive_ticket_labels(review, contract.get("workType"))
    depends_on = tuple(child["dependsOn"])

    if review.state != "eligible":
        return PlannedChild(
            local_key=local_key,
            labels=labels,
            decision=review.state,
            depends_on=depends_on,
            contract=None,
            contract_sha256=None,
            projection_sha256=projection_sha256,
            body=None,
            reasons=tuple(
                f"{reason.code}:{reason.seat_id}" for reason in review.reasons
            ),
        )

    try:
        sealed = seal_qualification(contract, probe["seatReviews"], run_id=run_id)
    except TicketQualificationError as exc:
        raise TicketPlanError("seal-failed", f"{field}.contract", exc.code) from exc

    body = render_ticket_body(
        prose,
        sealed.contract,
        {"runId": run_id, "contractSha256": sealed.contract_sha256},
    )
    return PlannedChild(
        local_key=local_key,
        labels=labels,
        decision=sealed.review.state,
        depends_on=depends_on,
        contract=sealed.contract,
        contract_sha256=sealed.contract_sha256,
        projection_sha256=sealed.projection_sha256,
        body=body,
        reasons=(),
    )
