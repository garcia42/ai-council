"""Strict atomic-finding capture and explicitly non-causal summaries.

This module deliberately does not adjudicate claims, infer semantic similarity, or
estimate the causal value of a seat.  The operator supplies within-run finding groups;
the validator only makes that capture complete, internally consistent, and auditable.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter, defaultdict
from datetime import date
from typing import Any, Iterable, Mapping


FINDING_ID_RE = re.compile(r"^finding-[0-9a-f]{32}$")
FINDING_GROUP_ID_RE = re.compile(r"^finding-group-[0-9a-f]{32}$")
RUN_ID_RE = re.compile(r"^run-[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

FINDING_KEYS = frozenset(
    {
        "findingId",
        "seatId",
        "category",
        "claim",
        "severity",
        "proposedAction",
        "evidenceSummary",
        "group",
        "operatorDisposition",
    }
)
SEAT_OWNED_FINDING_ORDER = (
    "findingId",
    "seatId",
    "category",
    "claim",
    "severity",
    "proposedAction",
    "evidenceSummary",
)
SEAT_OWNED_FINDING_KEYS = frozenset(SEAT_OWNED_FINDING_ORDER)
GROUP_KEYS = frozenset({"findingGroupId", "runId"})
NO_FINDINGS_KEYS = frozenset({"kind", "seatId", "outputArtifact"})
ARTIFACT_REFERENCE_KEYS = frozenset({"path", "bytes", "sha256"})

DISPOSITION_ORDER = (
    "already-known",
    "new-acted",
    "new-rejected",
    "new-deferred",
)
DISPOSITION_KEYS = {
    "already-known": frozenset({"kind", "considerationId", "quotedSubclaim"}),
    "new-acted": frozenset({"kind"}),
    "new-rejected": frozenset({"kind", "reason"}),
    # reviewDate is optional, but a reason is mandatory under amendment 001.
    "new-deferred": (
        frozenset({"kind", "reason"}),
        frozenset({"kind", "reason", "reviewDate"}),
    ),
}

# The preregistration prohibits these interpretations in reports.  This list is
# applied to field names, not finding prose: a finding may legitimately discuss a
# causal claim or flag misleading language.
FORBIDDEN_SUMMARY_LABEL_FRAGMENTS = (
    "calibrationproof",
    "causal",
    "decisionvalue",
    "marginalvalue",
    "redundancy",
    "replaceability",
)
SUMMARY_FIELD_LABELS = (
    "submittedSeatCount",
    "findingCount",
    "withinRunFindingOverlap",
    "findingGroupCount",
    "overlapGroupCount",
    "overlapGroups",
    "findingGroupId",
    "findingIds",
    "submittedSeats",
    "uniqueFindingCoverageUpperBounds",
    "findingGroupCountUpperBound",
    "findingGroupShareUpperBound",
    "findingGroupIds",
    "findingsPerSubmittedSeat",
    "emptyDeclarationRate",
    "declarationCount",
    "rate",
    "operatorReportedDispositionMix",
    "share",
)


class FindingError(ValueError):
    """A finding capture or requested summary violates the frozen contract."""


def new_finding_id() -> str:
    """Return a syntactically valid, collision-resistant finding identifier."""

    return f"finding-{uuid.uuid4().hex}"


def new_finding_group_id() -> str:
    """Return a syntactically valid, collision-resistant finding-group identifier."""

    return f"finding-group-{uuid.uuid4().hex}"


def _require_exact_keys(value: Any, expected: frozenset[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FindingError(f"{field} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing keys {missing}")
        if unknown:
            details.append(f"unknown keys {unknown}")
        raise FindingError(f"{field} has invalid keys: {', '.join(details)}")
    return value


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FindingError(f"{field} must be non-empty text")
    if value != value.strip():
        raise FindingError(f"{field} must not have leading or trailing whitespace")
    return value


def _require_id(value: Any, pattern: re.Pattern[str], field: str) -> str:
    value = _require_text(value, field)
    if not pattern.fullmatch(value):
        raise FindingError(f"{field} has an invalid stable id")
    return value


def _require_sequence(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise FindingError(f"{field} must be a list")
    return value


def _validate_artifact_reference(value: Any, field: str) -> dict[str, Any]:
    artifact = _require_exact_keys(value, ARTIFACT_REFERENCE_KEYS, field)
    _require_text(artifact["path"], f"{field}.path")
    byte_count = artifact["bytes"]
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
        raise FindingError(f"{field}.bytes must be a non-negative integer")
    sha256 = _require_text(artifact["sha256"], f"{field}.sha256")
    if not SHA256_RE.fullmatch(sha256):
        raise FindingError(f"{field}.sha256 must be 64 lowercase hexadecimal characters")
    return artifact


def _baseline_considerations(baseline: Any) -> dict[str, str]:
    if not isinstance(baseline, Mapping):
        raise FindingError("baseline must be an object")
    raw_considerations = baseline.get("knownConsiderations")
    considerations = _require_sequence(raw_considerations, "baseline.knownConsiderations")
    result: dict[str, str] = {}
    for index, raw in enumerate(considerations):
        field = f"baseline.knownConsiderations[{index}]"
        if not isinstance(raw, Mapping):
            raise FindingError(f"{field} must be an object")
        consideration_id = _require_text(raw.get("considerationId"), f"{field}.considerationId")
        claim = _require_text(raw.get("claim"), f"{field}.claim")
        if consideration_id in result:
            raise FindingError(f"duplicate baseline considerationId: {consideration_id}")
        result[consideration_id] = claim
    return result


def validate_group(group: Any, *, run_id: str, field: str = "group") -> None:
    """Validate one explicit within-run grouping assignment."""

    _require_id(run_id, RUN_ID_RE, "runId")
    group = _require_exact_keys(group, GROUP_KEYS, field)
    _require_id(group["findingGroupId"], FINDING_GROUP_ID_RE, f"{field}.findingGroupId")
    group_run_id = _require_id(group["runId"], RUN_ID_RE, f"{field}.runId")
    if group_run_id != run_id:
        raise FindingError(f"{field} belongs to a different run")


def validate_operator_disposition(
    disposition: Any,
    *,
    baseline_considerations: Mapping[str, str],
    field: str = "operatorDisposition",
) -> None:
    """Validate exactly one operator-reported disposition and its evidence."""

    if not isinstance(disposition, dict):
        raise FindingError(f"{field} must be an object")
    kind = _require_text(disposition.get("kind"), f"{field}.kind")
    if kind not in DISPOSITION_KEYS:
        raise FindingError(f"{field}.kind is not an allowed disposition")

    expected = DISPOSITION_KEYS[kind]
    if isinstance(expected, frozenset):
        _require_exact_keys(disposition, expected, field)
    elif set(disposition) not in expected:
        allowed = [sorted(keys) for keys in expected]
        raise FindingError(f"{field} has invalid keys for {kind}; allowed key sets: {allowed}")

    if kind == "already-known":
        consideration_id = _require_text(
            disposition["considerationId"], f"{field}.considerationId"
        )
        if consideration_id not in baseline_considerations:
            raise FindingError(f"{field}.considerationId is not present in the sealed baseline")
        quoted_subclaim = _require_text(
            disposition["quotedSubclaim"], f"{field}.quotedSubclaim"
        )
        baseline_claim = baseline_considerations[consideration_id]
        if quoted_subclaim not in baseline_claim:
            raise FindingError(
                f"{field}.quotedSubclaim is not an exact subclaim of the referenced baseline claim"
            )
    elif kind in {"new-rejected", "new-deferred"}:
        _require_text(disposition["reason"], f"{field}.reason")

    if kind == "new-deferred" and "reviewDate" in disposition:
        raw_review_date = _require_text(disposition["reviewDate"], f"{field}.reviewDate")
        try:
            date.fromisoformat(raw_review_date)
        except ValueError as exc:
            raise FindingError(f"{field}.reviewDate must be YYYY-MM-DD") from exc


def validate_finding(
    finding: Any,
    *,
    run_id: str,
    submitted_seats: set[str],
    baseline_considerations: Mapping[str, str],
    field: str = "finding",
) -> None:
    """Validate one structurally atomic finding.

    Atomicity is represented as one claim and one proposed action.  Semantic
    atomicity cannot be inferred safely and remains part of operator review.
    """

    finding = _require_exact_keys(finding, FINDING_KEYS, field)
    _require_id(finding["findingId"], FINDING_ID_RE, f"{field}.findingId")
    seat_id = _require_text(finding["seatId"], f"{field}.seatId")
    if seat_id not in submitted_seats:
        raise FindingError(f"{field}.seatId is not a submitted seat")
    for key in ("category", "claim", "severity", "proposedAction", "evidenceSummary"):
        _require_text(finding[key], f"{field}.{key}")
    validate_group(finding["group"], run_id=run_id, field=f"{field}.group")
    validate_operator_disposition(
        finding["operatorDisposition"],
        baseline_considerations=baseline_considerations,
        field=f"{field}.operatorDisposition",
    )


def _validate_seat_owned_finding(
    finding: Any,
    *,
    run_id: str,
    seat_id: str,
    field: str,
) -> dict[str, Any]:
    """Validate and copy the exact fields a seat owns before disposition."""

    # Retain the run parameter so the writer and report share one stable API,
    # but do not make the operator-owned grouping layer part of a seat output.
    _require_id(run_id, RUN_ID_RE, "runId")
    _require_text(seat_id, "seatId")
    finding = _require_exact_keys(finding, SEAT_OWNED_FINDING_KEYS, field)
    _require_id(finding["findingId"], FINDING_ID_RE, f"{field}.findingId")
    actual_seat = _require_text(finding["seatId"], f"{field}.seatId")
    if actual_seat != seat_id:
        raise FindingError(f"{field}.seatId belongs to a different seat")
    for key in ("category", "claim", "severity", "proposedAction", "evidenceSummary"):
        _require_text(finding[key], f"{field}.{key}")
    return {key: finding[key] for key in SEAT_OWNED_FINDING_ORDER}


def seat_owned_finding_projection(
    finding: Any,
    *,
    run_id: str,
    seat_id: str,
    field: str = "finding",
) -> dict[str, Any]:
    """Return the canonical seat-owned projection of a completion finding.

    The operator-owned ``group`` and ``operatorDisposition`` are intentionally
    excluded. Their semantics are revalidated separately against the run and
    sealed baseline by :func:`validate_findings`.
    """

    completion = _require_exact_keys(finding, FINDING_KEYS, field)
    projection = {key: completion[key] for key in SEAT_OWNED_FINDING_ORDER}
    return _validate_seat_owned_finding(
        projection,
        run_id=run_id,
        seat_id=seat_id,
        field=field,
    )


def parse_visible_output_findings(
    value: Any,
    *,
    run_id: str,
    seat_id: str,
    field: str = "visible output capture.findings",
) -> list[dict[str, Any]]:
    """Parse one retained output's exact canonical seat-owned finding list.

    The runtime artifact-checks and strict-JSON parses the surrounding output.
    Canonical list order is ascending ``findingId``; alternate order is rejected
    instead of normalized so writer and report checks share one representation.
    """

    findings = _require_sequence(value, field)
    parsed: list[dict[str, Any]] = []
    previous_id: str | None = None
    for index, finding in enumerate(findings):
        item = _validate_seat_owned_finding(
            finding,
            run_id=run_id,
            seat_id=seat_id,
            field=f"{field}[{index}]",
        )
        finding_id = item["findingId"]
        if previous_id == finding_id:
            raise FindingError(f"{field} contains duplicate findingId: {finding_id}")
        if previous_id is not None and previous_id > finding_id:
            raise FindingError(f"{field} must be sorted by findingId")
        previous_id = finding_id
        parsed.append(item)
    return parsed


def validate_visible_output_findings(
    *,
    run_id: str,
    submitted_seats: Iterable[str],
    visible_findings_by_seat: Mapping[str, Any],
    completion_findings: Any,
    no_findings_seats: Iterable[str],
) -> dict[str, list[dict[str, Any]]]:
    """Bind each submitted seat's retained finding list to the completion.

    ``visible_findings_by_seat`` is extracted from strict-parsed retained output
    envelopes. ``completion_findings`` contains full ledger objects, including
    later operator groups and dispositions. Artifact-bound no-findings
    declarations remain enforced by :func:`validate_findings`; here their visible
    lists must be empty.
    """

    _require_id(run_id, RUN_ID_RE, "runId")
    if isinstance(submitted_seats, (str, bytes)):
        raise FindingError("submittedSeats must be an iterable of seat IDs")
    seats_list = [_require_text(seat, "submittedSeats item") for seat in submitted_seats]
    if len(set(seats_list)) != len(seats_list):
        raise FindingError("submittedSeats contains duplicates")
    seats = set(seats_list)

    if not isinstance(visible_findings_by_seat, Mapping):
        raise FindingError("visibleFindingsBySeat must be an object")
    if set(visible_findings_by_seat) != seats:
        raise FindingError("visibleFindingsBySeat must exactly match submittedSeats")

    if isinstance(no_findings_seats, (str, bytes)):
        raise FindingError("noFindingsSeats must be an iterable of seat IDs")
    empty_list = [_require_text(seat, "noFindingsSeats item") for seat in no_findings_seats]
    if len(set(empty_list)) != len(empty_list):
        raise FindingError("noFindingsSeats contains duplicates")
    empty_seats = set(empty_list)
    unknown_empty_seats = empty_seats - seats
    if unknown_empty_seats:
        raise FindingError(
            f"noFindingsSeats contains non-submitted seats: {sorted(unknown_empty_seats)}"
        )

    completion_rows = _require_sequence(completion_findings, "completionFindings")
    expected_by_seat: dict[str, list[dict[str, Any]]] = {seat: [] for seat in seats}
    completion_ids: set[str] = set()
    for index, finding in enumerate(completion_rows):
        field = f"completionFindings[{index}]"
        if not isinstance(finding, dict):
            raise FindingError(f"{field} must be an object")
        finding_seat = _require_text(finding.get("seatId"), f"{field}.seatId")
        if finding_seat not in seats:
            raise FindingError(f"{field}.seatId is not a submitted seat")
        projection = seat_owned_finding_projection(
            finding,
            run_id=run_id,
            seat_id=finding_seat,
            field=field,
        )
        finding_id = projection["findingId"]
        if finding_id in completion_ids:
            raise FindingError(f"duplicate completion findingId: {finding_id}")
        completion_ids.add(finding_id)
        expected_by_seat[finding_seat].append(projection)

    checked: dict[str, list[dict[str, Any]]] = {}
    for seat in sorted(seats):
        expected = sorted(expected_by_seat[seat], key=lambda item: item["findingId"])
        visible = parse_visible_output_findings(
            visible_findings_by_seat[seat],
            run_id=run_id,
            seat_id=seat,
            field=f"visibleFindingsBySeat[{seat!r}]",
        )
        if seat in empty_seats:
            if expected:
                raise FindingError(f"no-findings seat {seat} has completion findings")
            if visible:
                raise FindingError(f"no-findings seat {seat} has visible findings")
        elif not expected:
            raise FindingError(f"findings-path seat {seat} has no completion findings")
        if visible != expected:
            raise FindingError(
                f"visible findings for seat {seat} do not exactly match completion findings"
            )
        checked[seat] = visible
    return checked


def validate_no_findings_declaration(
    declaration: Any,
    *,
    submitted_seats: set[str],
    output_artifacts: Mapping[str, Mapping[str, Any]],
    field: str = "noFindings declaration",
) -> str:
    """Validate a seat-originated no-findings declaration's artifact binding."""

    declaration = _require_exact_keys(declaration, NO_FINDINGS_KEYS, field)
    if declaration["kind"] != "no-findings":
        raise FindingError(f"{field}.kind must be no-findings")
    seat_id = _require_text(declaration["seatId"], f"{field}.seatId")
    if seat_id not in submitted_seats:
        raise FindingError(f"{field}.seatId is not a submitted seat")
    artifact = _validate_artifact_reference(
        declaration["outputArtifact"], f"{field}.outputArtifact"
    )
    if seat_id not in output_artifacts:
        raise FindingError(f"{field} has no submitted-seat output artifact to bind")
    if artifact != output_artifacts[seat_id]:
        raise FindingError(f"{field}.outputArtifact does not match the submitted seat output")
    return seat_id


def validate_findings(
    *,
    run_id: str,
    submitted_seats: Iterable[str],
    findings: Any,
    no_findings: Any,
    baseline: Mapping[str, Any],
    output_artifacts: Mapping[str, Mapping[str, Any]],
    prior_findings: Iterable[Mapping[str, Any]] = (),
) -> None:
    """Validate a run's complete finding/no-finding state.

    ``output_artifacts`` must map every submitted seat ID to its exact Tier-1
    output artifact reference.  A submitted seat must use exactly one path:
    one-or-more findings, or one artifact-bound ``no-findings`` declaration.
    ``prior_findings`` lets the ledger integration enforce global finding-ID
    uniqueness and prevent a group ID from being reused for another run.
    """

    _require_id(run_id, RUN_ID_RE, "runId")
    if isinstance(submitted_seats, (str, bytes)):
        raise FindingError("submittedSeats must be an iterable of seat IDs")
    raw_seats = list(submitted_seats)
    seats = [_require_text(seat, "submittedSeats item") for seat in raw_seats]
    if len(set(seats)) != len(seats):
        raise FindingError("submittedSeats contains duplicates")
    submitted = set(seats)

    if not isinstance(output_artifacts, Mapping):
        raise FindingError("outputArtifacts must be an object")
    if set(output_artifacts) != submitted:
        raise FindingError("outputArtifacts must exactly match submittedSeats")
    for seat_id in sorted(submitted):
        _validate_artifact_reference(output_artifacts[seat_id], f"outputArtifacts[{seat_id!r}]")

    baseline_considerations = _baseline_considerations(baseline)
    findings = _require_sequence(findings, "findings")
    declarations = _require_sequence(no_findings, "noFindings")

    prior_finding_ids: set[str] = set()
    prior_group_runs: dict[str, str] = {}
    for index, prior in enumerate(prior_findings):
        field = f"priorFindings[{index}]"
        if not isinstance(prior, Mapping):
            raise FindingError(f"{field} must be an object")
        finding_id = _require_id(prior.get("findingId"), FINDING_ID_RE, f"{field}.findingId")
        if finding_id in prior_finding_ids:
            raise FindingError(f"duplicate prior findingId: {finding_id}")
        prior_finding_ids.add(finding_id)
        group = prior.get("group")
        if not isinstance(group, Mapping):
            raise FindingError(f"{field}.group must be an object")
        group_id = _require_id(
            group.get("findingGroupId"), FINDING_GROUP_ID_RE, f"{field}.group.findingGroupId"
        )
        group_run_id = _require_id(group.get("runId"), RUN_ID_RE, f"{field}.group.runId")
        previous_run = prior_group_runs.setdefault(group_id, group_run_id)
        if previous_run != group_run_id:
            raise FindingError(f"finding group {group_id} crosses runs")

    finding_ids: set[str] = set()
    seats_with_findings: set[str] = set()
    group_runs: dict[str, str] = {}
    for index, finding in enumerate(findings):
        field = f"findings[{index}]"
        validate_finding(
            finding,
            run_id=run_id,
            submitted_seats=submitted,
            baseline_considerations=baseline_considerations,
            field=field,
        )
        finding_id = finding["findingId"]
        if finding_id in finding_ids or finding_id in prior_finding_ids:
            raise FindingError(f"duplicate findingId: {finding_id}")
        finding_ids.add(finding_id)
        seats_with_findings.add(finding["seatId"])
        group_id = finding["group"]["findingGroupId"]
        group_run = finding["group"]["runId"]
        if group_id in prior_group_runs and prior_group_runs[group_id] != group_run:
            raise FindingError(f"finding group {group_id} crosses runs")
        previous_run = group_runs.setdefault(group_id, group_run)
        if previous_run != group_run:
            raise FindingError(f"finding group {group_id} crosses runs")

    declaration_seats: set[str] = set()
    for index, declaration in enumerate(declarations):
        seat_id = validate_no_findings_declaration(
            declaration,
            submitted_seats=submitted,
            output_artifacts=output_artifacts,
            field=f"noFindings[{index}]",
        )
        if seat_id in declaration_seats:
            raise FindingError(f"duplicate no-findings declaration for seat: {seat_id}")
        declaration_seats.add(seat_id)

    both = seats_with_findings & declaration_seats
    if both:
        raise FindingError(f"submitted seats cannot have findings and no-findings: {sorted(both)}")
    missing = submitted - seats_with_findings - declaration_seats
    if missing:
        raise FindingError(
            "submitted seats need findings or an artifact-bound no-findings declaration: "
            f"{sorted(missing)}"
        )


def _assert_non_causal_summary_labels(labels: Iterable[str]) -> None:
    # Only inspect labels owned by this module.  Seat IDs and disposition values are
    # data, not report claims, and may legitimately contain one of these words.
    for label in labels:
        normalized = re.sub(r"[^a-z]", "", label.casefold())
        for forbidden in FORBIDDEN_SUMMARY_LABEL_FRAGMENTS:
            if forbidden in normalized:
                raise FindingError(f"forbidden causal or redundancy summary label: {label}")


def summarize_findings(
    *,
    run_id: str,
    submitted_seats: Iterable[str],
    findings: Any,
    no_findings: Any,
    baseline: Mapping[str, Any],
    output_artifacts: Mapping[str, Mapping[str, Any]],
    prior_findings: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Validate and return deterministic, explicitly non-causal run summaries."""

    seats = sorted(list(submitted_seats))
    # Validate using the materialized list so a generator cannot be consumed twice.
    validate_findings(
        run_id=run_id,
        submitted_seats=seats,
        findings=findings,
        no_findings=no_findings,
        baseline=baseline,
        output_artifacts=output_artifacts,
        prior_findings=prior_findings,
    )

    group_findings: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seat_finding_counts: Counter[str] = Counter()
    disposition_counts: Counter[str] = Counter()
    for finding in findings:
        group_findings[finding["group"]["findingGroupId"]].append(finding)
        seat_finding_counts[finding["seatId"]] += 1
        disposition_counts[finding["operatorDisposition"]["kind"]] += 1

    overlap_groups = []
    unique_group_ids_by_seat: dict[str, list[str]] = {seat: [] for seat in seats}
    for group_id in sorted(group_findings):
        members = group_findings[group_id]
        member_seats = sorted({finding["seatId"] for finding in members})
        if len(member_seats) >= 2:
            overlap_groups.append(
                {
                    "findingGroupId": group_id,
                    "findingIds": sorted(finding["findingId"] for finding in members),
                    "submittedSeats": member_seats,
                }
            )
        elif member_seats:
            unique_group_ids_by_seat[member_seats[0]].append(group_id)

    group_count = len(group_findings)
    finding_count = len(findings)
    declaration_count = len(no_findings)
    unique_upper_bounds = {}
    for seat in seats:
        group_ids = unique_group_ids_by_seat[seat]
        unique_upper_bounds[seat] = {
            "findingGroupCountUpperBound": len(group_ids),
            "findingGroupShareUpperBound": len(group_ids) / group_count if group_count else 0.0,
            "findingGroupIds": group_ids,
        }

    disposition_mix = {}
    for disposition in DISPOSITION_ORDER:
        count = disposition_counts[disposition]
        disposition_mix[disposition] = {
            "findingCount": count,
            "share": count / finding_count if finding_count else 0.0,
        }

    summary = {
        "submittedSeatCount": len(seats),
        "findingCount": finding_count,
        "withinRunFindingOverlap": {
            "findingGroupCount": group_count,
            "overlapGroupCount": len(overlap_groups),
            "overlapGroups": overlap_groups,
        },
        "uniqueFindingCoverageUpperBounds": unique_upper_bounds,
        "findingsPerSubmittedSeat": {
            seat: seat_finding_counts[seat] for seat in seats
        },
        "emptyDeclarationRate": {
            "declarationCount": declaration_count,
            "submittedSeatCount": len(seats),
            "rate": declaration_count / len(seats) if seats else 0.0,
        },
        "operatorReportedDispositionMix": disposition_mix,
    }
    _assert_non_causal_summary_labels(SUMMARY_FIELD_LABELS)
    return summary


__all__ = [
    "ARTIFACT_REFERENCE_KEYS",
    "DISPOSITION_ORDER",
    "FINDING_KEYS",
    "FindingError",
    "GROUP_KEYS",
    "NO_FINDINGS_KEYS",
    "SEAT_OWNED_FINDING_KEYS",
    "SEAT_OWNED_FINDING_ORDER",
    "new_finding_group_id",
    "new_finding_id",
    "parse_visible_output_findings",
    "seat_owned_finding_projection",
    "summarize_findings",
    "validate_finding",
    "validate_findings",
    "validate_group",
    "validate_no_findings_declaration",
    "validate_operator_disposition",
    "validate_visible_output_findings",
]
