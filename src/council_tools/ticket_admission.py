"""Pure structural admission for already-fetched implementation tickets.

A structurally eligible result proves only that caller-supplied snapshot,
execution context, dependency state, and review content agree.  It is not
authorization.  Context must be observed independently rather than derived
from the ticket body, and review/dependency custody remains untrusted until a
protected-evidence control is added.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import council_tools.ticket_contracts as ticket_contracts
import council_tools.ticket_policy as ticket_policy
import council_tools.ticket_review as ticket_review
from council_tools.ticket_contracts import (
    MAX_ISSUE_NUMBER,
    MAX_LIST_ITEMS,
    MAX_REPOSITORY_LENGTH,
    MAX_TARGET_BRANCH_LENGTH,
    AllowedPath,
    TicketEnvelope,
)
from council_tools.ticket_policy import ParsedTicketLabels
from council_tools.ticket_review import TicketReview


ADMISSION_VERSION = 1
MAX_ADMISSION_LABELS = MAX_LIST_ITEMS
MAX_ADMISSION_DEPENDENCIES = MAX_LIST_ITEMS
MAX_ADMISSION_EVIDENCE = MAX_LIST_ITEMS
MAX_ADMISSION_CHANGED_PATHS = MAX_LIST_ITEMS

SNAPSHOT_KEYS = frozenset(
    {"repository", "issueNumber", "state", "labels", "body"}
)
CONTEXT_KEYS = frozenset(
    {
        "repository",
        "issueNumber",
        "targetBranch",
        "baseCommit",
        "baseCommitEvidence",
        "dependencyClosure",
    }
)
BASE_COMMIT_EVIDENCE_KEYS = frozenset({"contractBaseIsAncestor", "changedPaths"})
CLOSURE_KEYS = frozenset({"issueNumber", "state"})
ISSUE_STATES = frozenset({"open", "closed"})

REASON_CODES = (
    "invalid-snapshot",
    "invalid-context",
    "invalid-labels",
    "invalid-ticket-body",
    "issue-not-open",
    "missing-priority",
    "missing-size",
    "missing-work-type",
    "agent-not-ready",
    "needs-split",
    "repository-mismatch",
    "issue-number-mismatch",
    "target-branch-mismatch",
    "base-commit-mismatch",
    "base-commit-not-descendant",
    "base-commit-scope-changed",
    "base-commit-read-dependency-changed",
    "priority-mismatch",
    "size-mismatch",
    "work-type-mismatch",
    "invalid-dependency-closure",
    "dependency-not-closed",
    "invalid-review-evidence",
    "missing-review-evidence",
    "ambiguous-review-evidence",
    "review-not-eligible",
    "review-size-mismatch",
    "review-priority-mismatch",
    "review-projection-mismatch",
)

_BASE_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


@dataclass(frozen=True)
class _IssueSnapshot:
    repository: str
    issue_number: int
    state: str
    labels: list[str]
    body: str


@dataclass(frozen=True)
class _DependencyState:
    issue_number: int
    state: str


@dataclass(frozen=True)
class _BaseCommitEvidence:
    """Caller-resolved facts relating the contract base to the context base.

    The predicate performs no repository access, so ancestry and the changed-path
    set arrive the same way dependency state does: resolved by the caller and
    supplied as evidence.  Resolving them is the adapter's job, not this one's.
    """

    contract_base_is_ancestor: bool
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class _AdmissionContext:
    repository: str
    issue_number: int
    target_branch: str
    base_commit: str
    base_commit_evidence: _BaseCommitEvidence
    dependency_closure: tuple[_DependencyState, ...]


@dataclass(frozen=True)
class TicketAdmissionResult:
    """One total structural result that deliberately cannot act as a boolean."""

    structurally_eligible: bool
    reasons: tuple[str, ...]
    envelope: TicketEnvelope | None = None
    labels: ParsedTicketLabels | None = None
    review: TicketReview | None = None

    def __bool__(self) -> bool:
        raise TypeError("structural admission is not authorization")


def _bounded_text(value: Any, *, max_length: int) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= max_length
        and value == value.strip()
        and "\x00" not in value
    )


def _positive_issue_number(value: Any) -> bool:
    return type(value) is int and 1 <= value <= MAX_ISSUE_NUMBER


def _is_repository_relative_path(value: Any) -> bool:
    """Defer the definition of a repository path to the contract module.

    ``AllowedPath.allows`` already applies the single normative path rule, and a
    file scope admits exactly its own path, so probing with the candidate as both
    scope and subject asks that rule the question without restating it here.  A
    second copy of the rule is what would drift.
    """

    return AllowedPath(kind="file", path=value).allows(value)


def _has_exact_plain_keys(value: Any, expected: frozenset[str]) -> bool:
    """Check exact plain-dict keys without invoking untrusted key equality."""

    return (
        type(value) is dict
        and len(value) == len(expected)
        and all(type(key) is str and key in expected for key in value)
    )


def _snapshot(value: Any) -> _IssueSnapshot | None:
    if not _has_exact_plain_keys(value, SNAPSHOT_KEYS):
        return None
    repository = value["repository"]
    issue_number = value["issueNumber"]
    state = value["state"]
    labels = value["labels"]
    body = value["body"]
    if not _bounded_text(repository, max_length=MAX_REPOSITORY_LENGTH):
        return None
    if not _positive_issue_number(issue_number):
        return None
    if type(state) is not str or state not in ISSUE_STATES:
        return None
    if (
        type(labels) is not list
        or len(labels) > MAX_ADMISSION_LABELS
        or any(type(label) is not str for label in labels)
    ):
        return None
    if type(body) is not str:
        return None
    return _IssueSnapshot(
        repository=repository,
        issue_number=issue_number,
        state=state,
        labels=labels,
        body=body,
    )


def _base_commit_evidence(value: Any) -> _BaseCommitEvidence | None:
    """Validate caller-resolved base-commit evidence, or refuse the context.

    Absent or malformed evidence is an invalid context rather than an absent
    constraint: a caller must not be able to buy admission by leaving it out.
    """

    if not _has_exact_plain_keys(value, BASE_COMMIT_EVIDENCE_KEYS):
        return None
    is_ancestor = value["contractBaseIsAncestor"]
    changed_paths = value["changedPaths"]
    # ``type() is not bool`` so that 1 and 0 cannot stand in for the claim.
    if type(is_ancestor) is not bool:
        return None
    if (
        type(changed_paths) is not list
        or len(changed_paths) > MAX_ADMISSION_CHANGED_PATHS
    ):
        return None
    for path in changed_paths:
        if not _is_repository_relative_path(path):
            return None
    return _BaseCommitEvidence(
        contract_base_is_ancestor=is_ancestor,
        changed_paths=tuple(changed_paths),
    )


def _context(value: Any) -> _AdmissionContext | None:
    if not _has_exact_plain_keys(value, CONTEXT_KEYS):
        return None
    repository = value["repository"]
    issue_number = value["issueNumber"]
    target_branch = value["targetBranch"]
    base_commit = value["baseCommit"]
    evidence_value = value["baseCommitEvidence"]
    closure = value["dependencyClosure"]
    if not _bounded_text(repository, max_length=MAX_REPOSITORY_LENGTH):
        return None
    if not _positive_issue_number(issue_number):
        return None
    if not _bounded_text(target_branch, max_length=MAX_TARGET_BRANCH_LENGTH):
        return None
    if type(base_commit) is not str or not _BASE_COMMIT_RE.fullmatch(base_commit):
        return None
    evidence = _base_commit_evidence(evidence_value)
    if evidence is None:
        return None
    if (
        type(closure) is not list
        or len(closure) > MAX_ADMISSION_DEPENDENCIES
    ):
        return None

    normalized: list[_DependencyState] = []
    for item in closure:
        if not _has_exact_plain_keys(item, CLOSURE_KEYS):
            return None
        dependency_issue_number = item["issueNumber"]
        state = item["state"]
        if not _positive_issue_number(dependency_issue_number):
            return None
        if type(state) is not str or state not in ISSUE_STATES:
            return None
        normalized.append(
            _DependencyState(
                issue_number=dependency_issue_number,
                state=state,
            )
        )
    return _AdmissionContext(
        repository=repository,
        issue_number=issue_number,
        target_branch=target_branch,
        base_commit=base_commit,
        base_commit_evidence=evidence,
        dependency_closure=tuple(normalized),
    )


def _evidence_shape(value: Any) -> list[TicketReview] | None:
    if (
        type(value) is not list
        or len(value) > MAX_ADMISSION_EVIDENCE
        or any(type(item) is not TicketReview for item in value)
    ):
        return None
    try:
        for item in value:
            if (
                type(item.run_id) is not str
                or type(item.contract_sha256) is not str
            ):
                return None
    except Exception:
        return None
    return value


def _ordered_reasons(found: set[str]) -> tuple[str, ...]:
    return tuple(code for code in REASON_CODES if code in found)


def evaluate_ticket_admission(
    snapshot: Any, context: Any, review_evidence: Any
) -> TicketAdmissionResult:
    """Return a total structural decision for caller-supplied point-in-time data.

    Input errors become fixed reason codes.  The function does not authenticate
    its inputs, authorize implementation, or preserve partially validated
    values on an ineligible result.
    """

    found: set[str] = set()
    parsed_snapshot = _snapshot(snapshot)
    parsed_context = _context(context)
    if parsed_snapshot is None:
        found.add("invalid-snapshot")
    if parsed_context is None:
        found.add("invalid-context")

    parsed_labels: ParsedTicketLabels | None = None
    envelope: TicketEnvelope | None = None
    if parsed_snapshot is not None:
        try:
            parsed_labels = ticket_policy.parse_ticket_labels(
                parsed_snapshot.labels
            )
        except ValueError:
            found.add("invalid-labels")
        try:
            envelope = ticket_policy.parse_ticket_issue_body(
                parsed_snapshot.body
            )
        except ValueError:
            found.add("invalid-ticket-body")
        if parsed_snapshot.state != "open":
            found.add("issue-not-open")

    if parsed_labels is not None:
        if parsed_labels.priority is None:
            found.add("missing-priority")
        if parsed_labels.points is None:
            found.add("missing-size")
        if parsed_labels.work_type is None:
            found.add("missing-work-type")
        if parsed_labels.agent_state != "ready":
            found.add("agent-not-ready")
        if parsed_labels.needs_split:
            found.add("needs-split")

    if parsed_snapshot is not None and parsed_context is not None:
        if parsed_snapshot.repository != parsed_context.repository:
            found.add("repository-mismatch")
        if parsed_snapshot.issue_number != parsed_context.issue_number:
            found.add("issue-number-mismatch")

    if envelope is not None and parsed_context is not None:
        contract = envelope.contract
        if contract.repository != parsed_context.repository:
            found.add("repository-mismatch")
        if contract.issue_number != parsed_context.issue_number:
            found.add("issue-number-mismatch")
        if contract.target_branch != parsed_context.target_branch:
            found.add("target-branch-mismatch")
        # The base-commit guarantee is scope-shaped, not tip-shaped.  What must
        # not have moved is the work the review actually described, and the
        # contract already declares that in ``allowedPaths``.  Requiring the two
        # tips to be identical could not tell a commit that touched this
        # ticket's own files from one that touched something it never names, and
        # treated both as fatal, so no two tickets sharing a base commit could
        # both be admitted.
        evidence = parsed_context.base_commit_evidence
        if contract.base_commit == parsed_context.base_commit:
            # Identical commits admit nothing to have changed.  Evidence that
            # says otherwise is self-contradictory, so it fails closed rather
            # than being read as the permissive half of the claim.
            if not evidence.contract_base_is_ancestor or evidence.changed_paths:
                found.add("base-commit-mismatch")
        else:
            if not evidence.contract_base_is_ancestor:
                found.add("base-commit-not-descendant")
            if any(
                contract.allows_path(path) for path in evidence.changed_paths
            ):
                found.add("base-commit-scope-changed")
            # A declared read dependency invalidates the qualification for a
            # different reason than a write-scope change, and it is reported
            # separately: the first says the ticket's own files moved, the
            # second says something it reads but must not write moved. Reading
            # one as the other would misdirect whoever re-qualifies.
            if any(
                contract.reads_path(path) for path in evidence.changed_paths
            ):
                found.add("base-commit-read-dependency-changed")

        closure_issue_numbers = [
            item.issue_number for item in parsed_context.dependency_closure
        ]
        if (
            len(closure_issue_numbers) != len(set(closure_issue_numbers))
            or set(closure_issue_numbers) != set(contract.dependencies)
        ):
            found.add("invalid-dependency-closure")
        elif any(
            item.state != "closed"
            for item in parsed_context.dependency_closure
        ):
            found.add("dependency-not-closed")

    if parsed_labels is not None and envelope is not None:
        contract = envelope.contract
        if (
            parsed_labels.priority is not None
            and parsed_labels.priority != contract.priority
        ):
            found.add("priority-mismatch")
        if (
            parsed_labels.points is not None
            and parsed_labels.points != contract.points
        ):
            found.add("size-mismatch")
        if (
            parsed_labels.work_type is not None
            and parsed_labels.work_type != contract.work_type
        ):
            found.add("work-type-mismatch")

    evidence = _evidence_shape(review_evidence)
    matched_review: TicketReview | None = None
    if evidence is None:
        found.add("invalid-review-evidence")
    elif envelope is not None:
        ref = envelope.review_ref
        matches = [
            item
            for item in evidence
            if item.run_id == ref.run_id
            and item.contract_sha256 == ref.contract_sha256
        ]
        if not matches:
            found.add("missing-review-evidence")
        elif len(matches) != 1:
            found.add("ambiguous-review-evidence")
        else:
            match = matches[0]
            try:
                matched_review = ticket_review.validate_ticket_review(
                    match.as_record_dict(),
                    expected_contract_sha256=ref.contract_sha256,
                )
            except Exception:
                found.add("invalid-review-evidence")
                matched_review = None
            if matched_review is not None:
                if matched_review.state != "eligible":
                    found.add("review-not-eligible")
                else:
                    if matched_review.points != envelope.contract.points:
                        found.add("review-size-mismatch")
                    if matched_review.priority != envelope.contract.priority:
                        found.add("review-priority-mismatch")
                    # The contract digest binds the sealed contract, which the
                    # seats never saw: it carries the two fields their review
                    # derived, and it is recomputed after those are recorded.
                    # Re-deriving the projection from the published contract is
                    # what ties the review to the content actually reviewed, so
                    # a reviewed field edited after the review fails here.
                    try:
                        published_projection = (
                            ticket_contracts.sizing_projection_sha256(
                                envelope.contract.as_dict()
                            )
                        )
                    except ValueError:
                        published_projection = None
                    if (
                        published_projection is None
                        or published_projection
                        != matched_review.sizing_projection_sha256
                    ):
                        found.add("review-projection-mismatch")

    reasons = _ordered_reasons(found)
    structurally_eligible = not reasons
    if not structurally_eligible:
        return TicketAdmissionResult(
            structurally_eligible=False,
            reasons=reasons,
        )
    return TicketAdmissionResult(
        structurally_eligible=True,
        reasons=(),
        envelope=envelope,
        labels=parsed_labels,
        review=matched_review,
    )


__all__ = [
    "ADMISSION_VERSION",
    "BASE_COMMIT_EVIDENCE_KEYS",
    "CONTEXT_KEYS",
    "CLOSURE_KEYS",
    "ISSUE_STATES",
    "MAX_ADMISSION_CHANGED_PATHS",
    "MAX_ADMISSION_DEPENDENCIES",
    "MAX_ADMISSION_EVIDENCE",
    "MAX_ADMISSION_LABELS",
    "REASON_CODES",
    "SNAPSHOT_KEYS",
    "TicketAdmissionResult",
    "evaluate_ticket_admission",
]
