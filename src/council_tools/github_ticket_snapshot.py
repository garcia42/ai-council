"""Normalize acquired GitHub issue payloads into exact admission inputs.

``evaluate_ticket_admission`` is a pure predicate over an issue snapshot, an
admission context and review evidence.  Nothing built those shapes from GitHub's
own payload format, so every admission decision so far used a snapshot assembled
by hand in an ad hoc script and the predicate's guarantees began at a boundary no
reviewed code enforced.

Two errors follow from that, and both are shape errors rather than judgment
errors.  GitHub reports issue state in upper case, so a snapshot that passes the
payload value through unchanged fails the predicate for a reason unrelated to the
ticket.  GitHub returns labels as objects rather than strings, so the conversion
has to happen somewhere and had no single reviewed home.

This module is deliberately **not** where eligibility is decided.  It converts
shapes and refuses payloads it cannot convert faithfully.  Whether a label is
unknown, whether a namespace repeats, whether a dependency is closed, whether a
review is eligible: every one of those is the predicate's determination, and
re-deciding any of them here would put one rule in two places.

It performs no input-output.  Acquiring the payload, resolving the dependency
closure, and resolving commit ancestry or changed paths are separate outcomes.
Their results arrive here as caller-supplied arguments.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import council_tools.ticket_admission as ticket_admission


class GitHubSnapshotError(ValueError):
    """A stable, field-addressed payload normalization failure."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"github ticket snapshot {code} at {field}")


def _require_plain_mapping(value: Any, field: str) -> Mapping[str, Any]:
    # ``type() is not dict`` because a Mapping subclass can override __getitem__
    # and serve different values to the validator than to the consumer.
    if type(value) is not dict:
        raise GitHubSnapshotError("invalid-payload", field)
    for key in value:
        if type(key) is not str:
            raise GitHubSnapshotError("invalid-payload-key", field)
    return value


def _require_text(value: Any, field: str) -> str:
    if type(value) is not str:
        raise GitHubSnapshotError("invalid-text", field)
    return value


def _require_issue_number(value: Any, field: str) -> int:
    # bool is an int subclass and must not pass as an issue number.
    if type(value) is not int or value <= 0:
        raise GitHubSnapshotError("invalid-issue-number", field)
    return value


def normalize_issue_state(value: Any) -> str:
    """Fold GitHub's upper-case state into the predicate's exact vocabulary.

    Folding is a shape conversion, so it is total and refuses anything outside
    the vocabulary rather than defaulting.  A state this module cannot recognise
    is a payload it cannot convert faithfully, not a payload to guess about.
    """

    text = _require_text(value, "issue.state")
    folded = text.lower()
    if folded not in ticket_admission.ISSUE_STATES:
        raise GitHubSnapshotError("unknown-issue-state", "issue.state")
    return folded


def extract_label_names(value: Any) -> list[str]:
    """Take label names out of GitHub's label objects, and adjudicate nothing.

    Whether a name is governed, unknown, or repeated within a namespace is the
    predicate's ``invalid-labels`` determination via ``parse_ticket_labels``.
    Deciding it here would duplicate a rule this module does not own.  Order is
    preserved exactly as received so the snapshot is a faithful reading of the
    payload rather than a rearrangement of it.
    """

    if type(value) is not list:
        raise GitHubSnapshotError("invalid-labels", "issue.labels")
    names: list[str] = []
    for index, item in enumerate(value):
        field = f"issue.labels[{index}]"
        label = _require_plain_mapping(item, field)
        if "name" not in label:
            raise GitHubSnapshotError("missing-label-name", field)
        names.append(_require_text(label["name"], f"{field}.name"))
    return names


def build_issue_snapshot(
    payload: Any,
    *,
    repository: str,
    issue_number: int,
) -> dict[str, Any]:
    """Build one snapshot whose key set is exactly ``SNAPSHOT_KEYS``.

    ``repository`` and ``issue_number`` are what the caller asked for.  The
    payload's own self-report is checked against them rather than trusted: a
    redirected or misrouted response is otherwise indistinguishable from the
    response that was wanted.
    """

    body = _require_plain_mapping(payload, "issue")
    requested_repository = _require_text(repository, "repository")
    requested_issue_number = _require_issue_number(issue_number, "issueNumber")

    for key in ("number", "state", "labels", "body"):
        if key not in body:
            raise GitHubSnapshotError("missing-payload-field", f"issue.{key}")

    reported_number = _require_issue_number(body["number"], "issue.number")
    if reported_number != requested_issue_number:
        raise GitHubSnapshotError("issue-number-mismatch", "issue.number")

    if "repository" in body:
        reported_repository = _require_text(
            body["repository"], "issue.repository"
        )
        if reported_repository != requested_repository:
            raise GitHubSnapshotError("repository-mismatch", "issue.repository")

    snapshot = {
        "repository": requested_repository,
        "issueNumber": requested_issue_number,
        "state": normalize_issue_state(body["state"]),
        "labels": extract_label_names(body["labels"]),
        "body": _require_text(body["body"], "issue.body"),
    }
    _assert_exact_keys(snapshot, ticket_admission.SNAPSHOT_KEYS, "snapshot")
    return snapshot


def build_admission_context(
    *,
    repository: str,
    issue_number: int,
    target_branch: str,
    base_commit: str,
    dependency_closure: Sequence[Any],
    base_commit_evidence: Any,
) -> dict[str, Any]:
    """Build one context whose key set is exactly ``CONTEXT_KEYS``.

    The closure and the evidence are resolved elsewhere and validated here, so a
    caller cannot hand the predicate a shape it will reject for reasons this
    module could have caught.  Nothing here resolves either of them.
    """

    context = {
        "repository": _require_text(repository, "repository"),
        "issueNumber": _require_issue_number(issue_number, "issueNumber"),
        "targetBranch": _require_text(target_branch, "targetBranch"),
        "baseCommit": _require_text(base_commit, "baseCommit"),
        "dependencyClosure": _normalize_dependency_closure(dependency_closure),
        "baseCommitEvidence": _normalize_base_commit_evidence(
            base_commit_evidence
        ),
    }
    _assert_exact_keys(context, ticket_admission.CONTEXT_KEYS, "context")
    return context


def _normalize_dependency_closure(value: Any) -> list[dict[str, Any]]:
    if type(value) is not list and type(value) is not tuple:
        raise GitHubSnapshotError("invalid-dependency-closure", "dependencyClosure")
    closure: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        field = f"dependencyClosure[{index}]"
        entry = _require_plain_mapping(item, field)
        if set(entry) != set(ticket_admission.CLOSURE_KEYS):
            raise GitHubSnapshotError("invalid-dependency-closure", field)
        state = _require_text(entry["state"], f"{field}.state")
        if state not in ticket_admission.ISSUE_STATES:
            raise GitHubSnapshotError("unknown-issue-state", f"{field}.state")
        closure.append(
            {
                "issueNumber": _require_issue_number(
                    entry["issueNumber"], f"{field}.issueNumber"
                ),
                "state": state,
            }
        )
    return closure


def _normalize_base_commit_evidence(value: Any) -> dict[str, Any]:
    field = "baseCommitEvidence"
    evidence = _require_plain_mapping(value, field)
    if set(evidence) != set(ticket_admission.BASE_COMMIT_EVIDENCE_KEYS):
        raise GitHubSnapshotError("invalid-base-commit-evidence", field)
    is_ancestor = evidence["contractBaseIsAncestor"]
    # ``type() is not bool`` so 1 and 0 cannot stand in for the claim.
    if type(is_ancestor) is not bool:
        raise GitHubSnapshotError("invalid-base-commit-evidence", f"{field}.contractBaseIsAncestor")
    changed = evidence["changedPaths"]
    if type(changed) is not list and type(changed) is not tuple:
        raise GitHubSnapshotError("invalid-base-commit-evidence", f"{field}.changedPaths")
    paths = [
        _require_text(path, f"{field}.changedPaths[{index}]")
        for index, path in enumerate(changed)
    ]
    return {"contractBaseIsAncestor": is_ancestor, "changedPaths": paths}


def _assert_exact_keys(built: Mapping[str, Any], expected: Any, what: str) -> None:
    """Prove the emitted key set against the predicate's own declaration.

    The expected set is read from ``ticket_admission`` rather than restated, so
    a change to the predicate's key set surfaces here instead of producing a
    context the predicate silently rejects.
    """

    if set(built) != set(expected):
        raise GitHubSnapshotError("emitted-key-set-drift", what)


__all__ = [
    "GitHubSnapshotError",
    "build_admission_context",
    "build_issue_snapshot",
    "extract_label_names",
    "normalize_issue_state",
]
