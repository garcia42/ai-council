"""Capture-only data-health summaries for the prospective council V2 ledger.

The analyzer deliberately accepts already-decoded rows.  It does not read a ledger,
validate JSON schemas, mutate evidence, or import the V1 runtime.  The integration
layer remains responsible for strict record dispatch and should provide an artifact
integrity callback when producing a live report.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


COHORT_SIZE = 10
CAPTURE_FRACTION_BAR = 0.90
ACTIVE_HANDLING_BAR_SECONDS = 180.0
COHORT_CUTOFF = datetime(
    2026, 10, 31, 23, 59, 59, tzinfo=ZoneInfo("America/New_York")
)

V2_PRIMARY_KINDS = {
    "capture-initiation",
    "council-attempt-v2",
    "council-v2",
}
V1_ATTEMPT_KIND = "council-attempt"
V1_COMPLETION_KIND = "council"
TERMINAL_SEAT_STATES = {"submitted", "abstained", "unavailable"}
OUTCOME_CLASSES = {"exogenous", "intervention-sensitive"}
# The tolerant mixed-ledger reader uses this stable kind when schemaVersion=2
# and runId identify a physical V2 observation but ``kind`` itself is malformed.
# It is denominator-eligible even though it cannot be assigned to a lifecycle
# boundary.
INVALID_V2_RECORD_KIND = "invalid-v2-record"
RECOGNIZED_RECORD_KINDS = V2_PRIMARY_KINDS | {
    "capture-activation",
    "capture-invalidation",
    "finding-audit-case-v2",
    "council-seats-finished",
    "grading-debt-override",
    "outcome-resolution",
    V1_ATTEMPT_KIND,
    V1_COMPLETION_KIND,
    INVALID_V2_RECORD_KIND,
}
COPIED_ATTEMPT_FIELDS = (
    "decisionFamilyId",
    "question",
    "decisionBeforeArtifact",
    "outcomeClass",
    "outcomeClassRationale",
    "evidenceCutoffAt",
    "seatPlan",
    "sharedOutcome",
)

ArtifactIntegrity = Callable[[Mapping[str, Any]], bool | Mapping[str, Any]]

# Report-only metadata supplied by the exact-byte JSONL reader.  It is never a
# durable schema field.  A decoded mapping without this annotation cannot prove
# that it is a byte-exact retry and therefore must retain its own ledger position.
RAW_RECORD_SHA256_ANNOTATION = "_captureRawRecordSha256"


class DataHealthError(ValueError):
    """The parsed row stream cannot identify the frozen capture cohort."""


def _as_utc(value: datetime | str, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DataHealthError(f"{field} must be an ISO-8601 timestamp") from exc
    else:
        raise DataHealthError(f"{field} must be an aware datetime or ISO-8601 text")
    if parsed.tzinfo is None:
        raise DataHealthError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _enum_text(value: Any, choices: set[str]) -> str | None:
    """Return a known textual enum without hashing an untrusted JSON value."""

    if not isinstance(value, str):
        return None
    return value if value in choices else None


def _row_kind(row: Mapping[str, Any]) -> str | None:
    """Project report-retained malformed V2 dispatch to a safe text sentinel."""

    kind = row.get("kind")
    if isinstance(kind, str) and kind in RECOGNIZED_RECORD_KINDS:
        return kind
    version = row.get("schemaVersion")
    if (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version == 2
        and "_captureSchemaError" in row
        and _text(row.get("runId")) is not None
    ):
        return INVALID_V2_RECORD_KIND
    return kind if isinstance(kind, str) else None


def _run_key(row: Mapping[str, Any], line_number: int) -> str:
    run_id = _text(row.get("runId"))
    if run_id is not None:
        return run_id
    return f"missing-run-id-at-line-{line_number}"


def _median_for_frozen_cohort(values: list[float]) -> float | None:
    if len(values) != COHORT_SIZE:
        return None
    ordered = sorted(values)
    # Frozen one-indexed positions five and six are zero-indexed positions four and five.
    return (ordered[4] + ordered[5]) / 2.0


def _mean_or_none(total: float, count: int) -> float | None:
    return total / count if count else None


def _raw_record_identity(row: Mapping[str, Any]) -> str | None:
    value = row.get(RAW_RECORD_SHA256_ANNOTATION)
    if (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    ):
        return value
    return None


def finding_summary_record_key(
    line_number: int, row: Mapping[str, Any]
) -> str | None:
    """Return the occurrence identity for a physical completion record.

    The byte hash alone is insufficient because an exact duplicate JSONL line
    has the same digest while remaining a distinct denominator observation.
    Append order plus exact bytes identifies the physical occurrence.
    """

    raw_identity = _raw_record_identity(row)
    if raw_identity is None:
        return None
    return f"physical-record:{line_number}:{raw_identity}"


def _group_rows(
    rows: list[Mapping[str, Any]], activation_line: int
) -> tuple[dict[str, dict[str, Any]], list[str], int]:
    states: dict[str, dict[str, Any]] = {}
    eligible_order: list[str] = []
    states_by_identity: dict[tuple[str, str | None], list[str]] = defaultdict(list)
    v1_states_by_run: dict[str, list[str]] = defaultdict(list)
    state_keys_by_run: dict[str, list[str]] = defaultdict(list)
    invalidations_by_run: dict[str, list[tuple[int, Mapping[str, Any]]]] = (
        defaultdict(list)
    )
    seen_initiation_retries: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    duplicate_initiation_retries = 0
    valid_boundary_lines: dict[
        tuple[tuple[str, str | None], str], list[int]
    ] = defaultdict(list)
    for candidate_line, candidate_row in enumerate(rows, 1):
        candidate_kind = _row_kind(candidate_row)
        if (
            candidate_line <= activation_line
            or candidate_kind in {None, "capture-initiation"}
            or "_captureSchemaError" in candidate_row
        ):
            continue
        candidate_run = _run_key(candidate_row, candidate_line)
        candidate_identity = (
            candidate_run,
            _text(candidate_row.get("initiationId")),
        )
        valid_boundary_lines[(candidate_identity, candidate_kind)].append(
            candidate_line
        )

    def new_state(
        run_id: str,
        line_number: int,
        identity: tuple[str, str | None] | None = None,
    ) -> str:
        state_key = f"ledger-state-at-line-{line_number}"
        state = _new_run_state(run_id)
        state["invalidations"] = list(invalidations_by_run[run_id])
        states[state_key] = state
        state_keys_by_run[run_id].append(state_key)
        if identity is not None:
            states_by_identity[identity].append(state_key)
        return state_key

    def latest_identity_state_missing(
        identity: tuple[str, str | None],
        kind: str,
        *,
        require_prior_lifecycle_event: bool,
        prefer_strict_lineage: bool = False,
    ) -> str | None:
        candidates: list[str] = []
        for candidate in reversed(states_by_identity[identity]):
            events = states[candidate]["events"]
            if events.get(kind):
                continue
            if require_prior_lifecycle_event and not any(events.values()):
                continue
            candidates.append(candidate)
        if prefer_strict_lineage:
            required_prefix = {
                "council-attempt-v2": ("capture-initiation",),
                "council-seats-finished": (
                    "capture-initiation",
                    "council-attempt-v2",
                ),
                "council-v2": (
                    "capture-initiation",
                    "council-attempt-v2",
                    "council-seats-finished",
                ),
            }.get(kind, ())
            for candidate in candidates:
                events = states[candidate]["events"]
                clean_records = all(
                    "_captureSchemaError" not in event
                    for kind_events in events.values()
                    for _line_number, event in kind_events
                )
                exact_prefix = all(
                    len(events.get(required_kind, [])) == 1
                    for required_kind in required_prefix
                ) and all(
                    not events.get(other_kind)
                    for other_kind in (
                        "capture-initiation",
                        "council-attempt-v2",
                        "council-seats-finished",
                        "council-v2",
                    )
                    if other_kind not in required_prefix and other_kind != kind
                )
                if clean_records and exact_prefix:
                    return candidate
        return candidates[0] if candidates else None

    for line_number, row in enumerate(rows, 1):
        kind = _row_kind(row)
        if kind == "capture-invalidation":
            if "_captureSchemaError" in row:
                # The tolerant report reader retains rejected control rows for
                # ledger diagnostics.  They must never acquire the control
                # semantics that strict validation denied them.
                continue
            run = _run_key(row, line_number)
            invalidation = (line_number, row)
            invalidations_by_run[run].append(invalidation)
            for state_key in state_keys_by_run[run]:
                states[state_key]["invalidations"].append(invalidation)
            continue
        if line_number <= activation_line:
            continue

        is_primary = kind == INVALID_V2_RECORD_KIND or kind in V2_PRIMARY_KINDS or kind in {
            V1_ATTEMPT_KIND,
            V1_COMPLETION_KIND,
        }
        if not is_primary and kind not in {
            "council-seats-finished",
        }:
            continue

        run = _run_key(row, line_number)
        if kind == INVALID_V2_RECORD_KIND:
            # The tolerant reader cannot assign a malformed dispatch boundary
            # to any lifecycle role.  It is therefore always an independent
            # physical denominator occurrence, even when its caller-controlled
            # identities match a clean partial lifecycle.
            identity = (run, _text(row.get("initiationId")))
            state_key = new_state(run, line_number, identity)
        elif kind == "capture-initiation":
            row_activation_id = _text(row.get("activationId"))
            idempotency_key = _text(row.get("idempotencyKey"))
            raw_identity = _raw_record_identity(row)
            retry_identity = (
                (row_activation_id, idempotency_key, run)
                if row_activation_id is not None and idempotency_key is not None
                else None
            )
            if (
                retry_identity is not None
                and raw_identity is not None
                and raw_identity in seen_initiation_retries[retry_identity]
            ):
                duplicate_initiation_retries += 1
                continue
            if retry_identity is not None and raw_identity is not None:
                seen_initiation_retries[retry_identity].add(raw_identity)

            identity = (run, _text(row.get("initiationId")))
            # A late initiation can complete an already-observed orphan lifecycle.
            # Once an initiation exists, however, every non-identical initiation is
            # a distinct denominator event even when its writer reused runId (or all
            # identity fields). Strict validation makes that new state invalid.
            state_key = latest_identity_state_missing(
                identity,
                "capture-initiation",
                require_prior_lifecycle_event=True,
            )
            if state_key is None:
                state_key = new_state(run, line_number, identity)
        elif kind == V1_ATTEMPT_KIND:
            # V1 has no durable initiation/idempotency boundary. Every observed
            # attempt is therefore its own ledger event, even when runId is reused.
            state_key = new_state(run, line_number)
            v1_states_by_run[run].append(state_key)
        elif kind == V1_COMPLETION_KIND:
            state_key = next(
                (
                    candidate
                    for candidate in reversed(v1_states_by_run[run])
                    if states[candidate]["events"].get(V1_ATTEMPT_KIND)
                    and not states[candidate]["events"].get(V1_COMPLETION_KIND)
                ),
                None,
            )
            if state_key is None:
                state_key = new_state(run, line_number)
                v1_states_by_run[run].append(state_key)
        else:
            identity = (run, _text(row.get("initiationId")))
            matching_valid_lines = valid_boundary_lines[(identity, str(kind))]
            later_valid_boundary = bool(
                matching_valid_lines and matching_valid_lines[-1] > line_number
            )
            if "_captureSchemaError" in row and later_valid_boundary:
                # A malformed physical boundary is its own denominator event.
                # Attaching it to a clean partial lifecycle would consume the
                # slot that a later valid boundary must complete, making report
                # eligibility depend on duplicate ordering.
                state_key = new_state(run, line_number, identity)
            else:
                state_key = latest_identity_state_missing(
                    identity,
                    str(kind),
                    require_prior_lifecycle_event=True,
                    # The tolerant reader validates against a strict prior chain.
                    # A valid later boundary must continue a clean lineage, not
                    # the most recent report-invalid duplicate that happens to
                    # reuse its identity. If no clean candidate exists, retaining
                    # the latest invalid/orphan state preserves late-fill
                    # observability.
                    prefer_strict_lineage="_captureSchemaError" not in row,
                )
            if state_key is None:
                state_key = new_state(run, line_number, identity)

        state = states[state_key]

        state["events"][kind].append((line_number, row))
        if is_primary and state["eligibilityLine"] is None:
            state["eligibilityLine"] = line_number
            state["eligibilityKind"] = kind
            eligible_order.append(state_key)

    return states, eligible_order, duplicate_initiation_retries


def _new_run_state(run_id: str) -> dict[str, Any]:
    return {
        "runId": run_id,
        "events": defaultdict(list),
        "invalidations": [],
        "eligibilityLine": None,
        "eligibilityKind": None,
    }


def _only_event(
    state: Mapping[str, Any], kind: str
) -> tuple[int, Mapping[str, Any]] | None:
    events = state["events"].get(kind, [])
    return events[0] if len(events) == 1 else None


def _identity_matches(
    row: Mapping[str, Any], initiation: Mapping[str, Any], activation_id: str
) -> bool:
    return (
        row.get("runId") == initiation.get("runId")
        and row.get("initiationId") == initiation.get("initiationId")
        and row.get("activationId") == initiation.get("activationId")
        and row.get("activationId") == activation_id
    )


def _seat_result_map(completion: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    results = completion.get("seatResults")
    if not isinstance(results, list):
        return {}
    mapped: dict[str, Mapping[str, Any]] = {}
    for result in results:
        if not isinstance(result, Mapping):
            continue
        seat = _text(result.get("seatId"))
        if seat is None or seat in mapped:
            return {}
        mapped[seat] = result
    return mapped


def _binding_reasons(state: Mapping[str, Any], activation_id: str) -> list[str]:
    events = state["events"]
    reasons: list[str] = []
    v1_attempts = events.get(V1_ATTEMPT_KIND, [])
    v1_completions = events.get(V1_COMPLETION_KIND, [])
    if v1_attempts or v1_completions:
        return ["post-activation-v1-run"]

    for kind in (
        "capture-initiation",
        "council-attempt-v2",
        "council-seats-finished",
        "council-v2",
    ):
        count = len(events.get(kind, []))
        if count == 0:
            reasons.append(f"missing-{kind}")
        elif count > 1:
            reasons.append(f"duplicate-{kind}")

    initiation_event = _only_event(state, "capture-initiation")
    attempt_event = _only_event(state, "council-attempt-v2")
    finished_event = _only_event(state, "council-seats-finished")
    completion_event = _only_event(state, "council-v2")
    if not all((initiation_event, attempt_event, finished_event, completion_event)):
        return reasons

    initiation_line, initiation = initiation_event
    attempt_line, attempt = attempt_event
    finished_line, finished = finished_event
    completion_line, completion = completion_event
    if not (initiation_line < attempt_line < finished_line < completion_line):
        reasons.append("invalid-lifecycle-order")
    if initiation.get("activationId") != activation_id:
        reasons.append("activation-identity-mismatch")
    for label, row in (("attempt", attempt), ("seats-finished", finished), ("completion", completion)):
        if not _identity_matches(row, initiation, activation_id):
            reasons.append(f"{label}-identity-mismatch")
    for field in COPIED_ATTEMPT_FIELDS:
        if completion.get(field) != attempt.get(field):
            reasons.append(f"completion-{field}-mismatch")
    if completion.get("decisionFamilyId") in (None, ""):
        reasons.append("missing-decision-family")
    if _enum_text(completion.get("outcomeClass"), OUTCOME_CLASSES) is None:
        reasons.append("missing-outcome-class")
    if _text(completion.get("outcomeClassRationale")) is None:
        reasons.append("missing-outcome-class-rationale")

    plan = attempt.get("seatPlan")
    if not isinstance(plan, list) or not plan:
        reasons.append("missing-seat-plan")
        return reasons
    planned_seats = [
        _text(item.get("seatId")) if isinstance(item, Mapping) else None for item in plan
    ]
    if None in planned_seats or len(set(planned_seats)) != len(planned_seats):
        reasons.append("invalid-seat-plan")
        return reasons

    seat_states = finished.get("seatStates")
    results = _seat_result_map(completion)
    if not isinstance(seat_states, Mapping) or set(seat_states) != set(planned_seats):
        reasons.append("seat-state-set-mismatch")
    elif any(
        _enum_text(state_value, TERMINAL_SEAT_STATES) is None
        for state_value in seat_states.values()
    ):
        reasons.append("invalid-seat-state")
    elif "unavailable" in seat_states.values():
        # The execution failure remains observable and denominator-eligible, but the
        # frozen soak does not call that initiation capture-complete.
        reasons.append("seat-execution-failure")
    if set(results) != set(planned_seats):
        reasons.append("seat-result-set-mismatch")
    elif isinstance(seat_states, Mapping) and any(
        result.get("state") != seat_states.get(seat)
        for seat, result in results.items()
    ):
        reasons.append("seat-result-state-mismatch")

    findings = completion.get("findings")
    no_findings = completion.get("noFindings")
    if not isinstance(findings, list) or not isinstance(no_findings, list):
        reasons.append("missing-finding-capture")
    else:
        finding_seats = {
            seat
            for item in findings
            if isinstance(item, Mapping)
            for seat in [_text(item.get("seatId"))]
            if seat is not None
        }
        declaration_seats = {
            seat
            for item in no_findings
            if isinstance(item, Mapping)
            for seat in [_text(item.get("seatId"))]
            if seat is not None
        }
        for seat, result in results.items():
            if result.get("state") != "submitted":
                continue
            if seat not in finding_seats and seat not in declaration_seats:
                reasons.append(f"submitted-seat-without-finding-declaration:{seat}")
    return reasons


def _duration(
    state: Mapping[str, Any], as_of: datetime
) -> tuple[float, float, bool]:
    initiation_event = _only_event(state, "capture-initiation")
    attempt_event = _only_event(state, "council-attempt-v2")
    finished_event = _only_event(state, "council-seats-finished")
    completion_event = _only_event(state, "council-v2")
    if not all((initiation_event, attempt_event, finished_event, completion_event)):
        return math.inf, math.inf, False
    try:
        started = _as_utc(initiation_event[1].get("handlingStartedAt"), "handlingStartedAt")
        launched = _as_utc(attempt_event[1].get("seatsLaunchedAt"), "seatsLaunchedAt")
        finished = _as_utc(finished_event[1].get("seatsFinishedAt"), "seatsFinishedAt")
        finalized = _as_utc(completion_event[1].get("finalizedAt"), "finalizedAt")
    except DataHealthError:
        return math.inf, math.inf, False
    if not (started <= launched <= finished <= finalized <= as_of):
        return math.inf, math.inf, False
    active = (launched - started).total_seconds() + (finalized - finished).total_seconds()
    elapsed = (finalized - started).total_seconds()
    return active, elapsed, True


def _artifact_result_ok(result: bool | Mapping[str, Any]) -> bool:
    if isinstance(result, bool):
        return result
    if isinstance(result, Mapping):
        for key in ("ok", "valid", "verified"):
            if isinstance(result.get(key), bool):
                return bool(result[key])
        # The project artifact store returns the authenticated, normalized
        # reference on success and raises on failure.
        return {"path", "sha256", "bytes"}.issubset(result)
    return False


def _artifact_reference(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    path = value.get("path")
    digest = value.get("sha256")
    byte_count = value.get("bytes")
    if (
        not isinstance(path, str)
        or not path
        or not isinstance(digest, str)
        or not digest
        or isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or byte_count < 0
    ):
        return None
    # decisionBeforeArtifact also carries gitBlob; the custody verifier owns
    # only the exact three-key artifact reference.
    return {"path": path, "sha256": digest, "bytes": byte_count}


def _artifact_health(
    state: Mapping[str, Any], verifier: ArtifactIntegrity | None
) -> tuple[dict[str, int | bool], list[str]]:
    attempt_event = _only_event(state, "council-attempt-v2")
    completion_event = _only_event(state, "council-v2")
    refs: list[tuple[str, Any]] = []
    missing: list[str] = []
    if attempt_event is not None:
        decision_ref = _artifact_reference(
            attempt_event[1].get("decisionBeforeArtifact")
        )
        if decision_ref is not None:
            refs.append(("decision-before", decision_ref))
        else:
            missing.append("decision-before")
    if completion_event is not None:
        for seat, result in sorted(_seat_result_map(completion_event[1]).items()):
            required = [("inputArtifact", "input")]
            if result.get("state") == "submitted":
                required.append(("outputArtifact", "output"))
            for field, label in required:
                ref = _artifact_reference(result.get(field))
                role = f"{seat}:{label}"
                if ref is not None:
                    refs.append((role, ref))
                else:
                    missing.append(role)

    checked = 0
    integrity_failures: list[str] = []
    if verifier is not None:
        for role, ref in refs:
            checked += 1
            try:
                valid = _artifact_result_ok(verifier(ref))
            except Exception:  # verifier failures are captured as failed integrity checks
                valid = False
            if not valid:
                integrity_failures.append(role)
    failures = [f"missing-artifact:{role}" for role in missing]
    failures.extend(f"artifact-integrity-failure:{role}" for role in integrity_failures)
    return (
        {
            "requiredArtifactCount": len(refs) + len(missing),
            "presentArtifactCount": len(refs),
            "integrityCheckApplied": verifier is not None,
            "integrityCheckedCount": checked,
            "artifactCompletenessFailureCount": len(missing),
            "artifactIntegrityFailureCount": len(integrity_failures),
        },
        failures,
    )


def _schema_annotation_reasons(state: Mapping[str, Any]) -> list[str]:
    """Return fail-closed reasons added by the tolerant report reader.

    Writers still reject invalid V2 rows.  The report reader may retain a known-kind
    row with an identifiable run ID and annotate it with ``_captureSchemaError`` so
    the frozen denominator remains observable.  Such a row must never become
    eligible for headline finding or forecast summaries.
    """

    reasons: list[str] = []
    for kind, events in state["events"].items():
        for _line_number, row in events:
            if "_captureSchemaError" in row:
                label = _text(kind) or "unknown"
                reasons.append(f"schema-invalid-record:{label}")
    for _line_number, row in state["invalidations"]:
        if "_captureSchemaError" in row:
            label = _text(row.get("kind")) or "unknown"
            reasons.append(f"schema-invalid-record:{label}")
    return reasons


def _run_analysis_state(
    state: Mapping[str, Any],
    *,
    activation_id: str,
    as_of: datetime,
    artifact_integrity: ArtifactIntegrity | None,
) -> dict[str, Any]:
    """Build the one eligibility state shared by all headline analyses."""

    reasons = _schema_annotation_reasons(state)
    reasons.extend(_binding_reasons(state, activation_id))
    active, elapsed, valid_duration = _duration(state, as_of)
    if not valid_duration:
        reasons.append("invalid-duration")
    artifact_health, artifact_reasons = _artifact_health(state, artifact_integrity)
    reasons.extend(artifact_reasons)
    if (
        artifact_integrity is None
        and artifact_health["requiredArtifactCount"] > 0
    ):
        reasons.append("artifact-integrity-not-checked")
    if state["invalidations"]:
        reasons.append("capture-invalidated")
    reasons = list(dict.fromkeys(reasons))
    return {
        "eligibleForHeadlineAnalysis": not reasons,
        "incompleteReasons": reasons,
        "activeHandlingSeconds": active,
        "elapsedSeconds": elapsed,
        "validDuration": valid_duration,
        "artifactHealth": artifact_health,
    }


def _raw_finding_summary(completion: Mapping[str, Any]) -> dict[str, Any] | None:
    findings = completion.get("findings")
    declarations = completion.get("noFindings")
    if not isinstance(findings, list) or not isinstance(declarations, list):
        return None
    submitted = {
        seat
        for seat, result in _seat_result_map(completion).items()
        if result.get("state") == "submitted"
    }
    finding_counts: Counter[str] = Counter()
    disposition_counts: Counter[str] = Counter()
    groups: dict[str, dict[str, set[str]]] = {}
    for item in findings:
        if not isinstance(item, Mapping):
            continue
        seat = _text(item.get("seatId"))
        if seat is not None:
            finding_counts[seat] += 1
        disposition = item.get("operatorDisposition")
        if isinstance(disposition, Mapping):
            kind = _text(disposition.get("kind"))
            if kind is not None:
                disposition_counts[kind] += 1
        group = item.get("group")
        group_id = _text(group.get("findingGroupId")) if isinstance(group, Mapping) else None
        if group_id is not None:
            bucket = groups.setdefault(group_id, {"seats": set()})
            if seat is not None:
                bucket["seats"].add(seat)
    overlap_count = sum(len(value["seats"]) >= 2 for value in groups.values())
    declaration_count = len(
        {
            seat
            for item in declarations
            if isinstance(item, Mapping)
            for seat in [_text(item.get("seatId"))]
            if seat is not None and seat in submitted
        }
    )
    total_findings = sum(finding_counts.values())
    return {
        "submittedSeatCount": len(submitted),
        "findingCount": total_findings,
        "findingsPerSubmittedSeat": dict(sorted(finding_counts.items())),
        "emptyDeclarationRate": {
            "declarationCount": declaration_count,
            "submittedSeatCount": len(submitted),
            "rate": _mean_or_none(float(declaration_count), len(submitted)),
        },
        "operatorReportedDispositionMix": {
            kind: {
                "findingCount": count,
                "share": _mean_or_none(float(count), total_findings),
            }
            for kind, count in sorted(disposition_counts.items())
        },
        "withinRunFindingOverlap": {
            "findingGroupCount": len(groups),
            "overlapGroupCount": overlap_count,
            "overlapGroups": [],
        },
    }


def _aggregate_finding_health(
    runs: list[str],
    states: Mapping[str, Mapping[str, Any]],
    supplied: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    submitted_by_seat: Counter[str] = Counter()
    findings_by_seat: Counter[str] = Counter()
    disposition_counts: Counter[str] = Counter()
    submitted_total = 0
    finding_total = 0
    declaration_total = 0
    overlap_groups = 0
    finding_groups = 0
    summarized_runs = 0

    for run in runs:
        completion_event = _only_event(states[run], "council-v2")
        if completion_event is None:
            continue
        completion_line, completion = completion_event
        for seat, result in _seat_result_map(completion).items():
            if result.get("state") == "submitted":
                submitted_by_seat[seat] += 1
        # A public run ID is caller supplied and can be reused by a later bad
        # physical record.  Report-time provenance summaries therefore bind to
        # the exact completion line when the JSONL reader supplied its hash.  A
        # decoded-only caller has no physical bytes to bind, so the grouping
        # layer's synthetic lifecycle-state key is the fail-closed fallback.
        summary_identity = (
            finding_summary_record_key(completion_line, completion) or run
        )
        summary = supplied.get(summary_identity) if supplied is not None else None
        if summary is None:
            summary = _raw_finding_summary(completion)
        if not isinstance(summary, Mapping):
            continue
        summarized_runs += 1
        run_submitted = summary.get("submittedSeatCount", 0)
        run_findings = summary.get("findingCount", 0)
        if isinstance(run_submitted, int) and not isinstance(run_submitted, bool):
            submitted_total += run_submitted
        if isinstance(run_findings, int) and not isinstance(run_findings, bool):
            finding_total += run_findings
        by_seat = summary.get("findingsPerSubmittedSeat")
        if isinstance(by_seat, Mapping):
            for seat, count in by_seat.items():
                if isinstance(seat, str) and isinstance(count, int) and not isinstance(count, bool):
                    findings_by_seat[seat] += count
        empty = summary.get("emptyDeclarationRate")
        if isinstance(empty, Mapping):
            count = empty.get("declarationCount")
            if isinstance(count, int) and not isinstance(count, bool):
                declaration_total += count
        dispositions = summary.get("operatorReportedDispositionMix")
        if isinstance(dispositions, Mapping):
            for kind, detail in dispositions.items():
                if not isinstance(kind, str) or not isinstance(detail, Mapping):
                    continue
                count = detail.get("findingCount")
                if isinstance(count, int) and not isinstance(count, bool):
                    disposition_counts[kind] += count
        overlap = summary.get("withinRunFindingOverlap")
        if isinstance(overlap, Mapping):
            groups = overlap.get("findingGroupCount")
            overlaps = overlap.get("overlapGroupCount")
            if isinstance(groups, int) and not isinstance(groups, bool):
                finding_groups += groups
            if isinstance(overlaps, int) and not isinstance(overlaps, bool):
                overlap_groups += overlaps

    all_seats = sorted(set(submitted_by_seat) | set(findings_by_seat))
    return {
        "summarizedRunCount": summarized_runs,
        "findingCount": finding_total,
        "submittedSeatCount": submitted_total,
        "findingsPerSubmittedSeat": {
            "findingCount": finding_total,
            "submittedSeatCount": submitted_total,
            "rate": _mean_or_none(float(finding_total), submitted_total),
            "bySeat": {
                seat: {
                    "findingCount": findings_by_seat[seat],
                    "submittedSeatCount": submitted_by_seat[seat],
                    "rate": _mean_or_none(
                        float(findings_by_seat[seat]), submitted_by_seat[seat]
                    ),
                }
                for seat in all_seats
            },
        },
        "emptyFindingDeclarationRate": {
            "declarationCount": declaration_total,
            "submittedSeatCount": submitted_total,
            "rate": _mean_or_none(float(declaration_total), submitted_total),
        },
        "operatorReportedDispositionMix": {
            kind: {
                "findingCount": count,
                "share": _mean_or_none(float(count), finding_total),
            }
            for kind, count in sorted(disposition_counts.items())
        },
        "withinRunFindingOverlap": {
            "findingGroupCount": finding_groups,
            "overlapGroupCount": overlap_groups,
            "overlapFraction": _mean_or_none(float(overlap_groups), finding_groups),
        }
        if summarized_runs
        else None,
    }


def _finding_health(
    cohort: list[str],
    states: Mapping[str, Mapping[str, Any]],
    supplied: Mapping[str, Mapping[str, Any]] | None,
    analysis_states: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    eligible = [
        run
        for run in cohort
        if analysis_states[run]["eligibleForHeadlineAnalysis"]
    ]
    eligible_set = set(eligible)
    excluded = [run for run in cohort if run not in eligible_set]
    headline = _aggregate_finding_health(eligible, states, supplied)
    invalid = _aggregate_finding_health(excluded, states, supplied)
    return {
        "scope": "capture-complete first-ten V2 runs only",
        "eligibleRunCount": len(eligible),
        **headline,
        "excludedOrInvalidStratum": {
            "runCount": len(excluded),
            "runs": [
                {
                    "runId": states[run]["runId"],
                    "incompleteReasons": list(
                        analysis_states[run]["incompleteReasons"]
                    ),
                }
                for run in excluded
            ],
            **invalid,
        },
    }


def _legacy_outcome_key(prediction: Mapping[str, Any], fallback: str) -> tuple[Any, ...]:
    outcome_id = _text(prediction.get("outcomeId"))
    if outcome_id is not None:
        return ("id", outcome_id)
    claim = _text(prediction.get("claim"))
    resolution_date = _text(prediction.get("resolutionDate"))
    resolved_by = _text(prediction.get("resolvedBy"))
    if all((claim, resolution_date, resolved_by)):
        return ("content", claim, resolution_date, resolved_by)
    return ("row", fallback)


def _outcome_health(
    rows: list[Mapping[str, Any]],
    resolution_events: list[Mapping[str, Any]],
    activation_line: int,
    activation_id: str,
    states: Mapping[str, Mapping[str, Any]],
    analysis_states: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    legacy_outcomes: set[tuple[Any, ...]] = set()
    ledger_resolution_diagnostics: list[dict[str, Any]] = []
    for line_number, row in enumerate(rows, 1):
        kind = _row_kind(row)
        if kind == "outcome-resolution":
            diagnostic = {
                "lineNumber": line_number,
                "kind": "outcome-resolution",
                "error": "ledger-origin-resolution-not-eligible",
            }
            outcome_id = _text(row.get("outcomeId"))
            if outcome_id is not None:
                diagnostic["outcomeId"] = outcome_id
            ledger_resolution_diagnostics.append(diagnostic)
        if kind == V1_ATTEMPT_KIND:
            outcome = row.get("sharedOutcome")
            if isinstance(outcome, Mapping):
                legacy_outcomes.add(_legacy_outcome_key(outcome, f"attempt-{line_number}"))
        elif kind == V1_COMPLETION_KIND:
            predictions = row.get("predictions")
            if isinstance(predictions, list):
                for index, prediction in enumerate(predictions):
                    if isinstance(prediction, Mapping):
                        legacy_outcomes.add(
                            _legacy_outcome_key(prediction, f"completion-{line_number}-{index}")
                        )

    # Resolution provenance is a separate input channel. The integration layer
    # supplies only sidecar events after complete schema, append-chain, temporal,
    # and outcome-integrity validation. A main-ledger row can therefore never
    # acquire resolution semantics merely by resembling the sidecar schema.
    resolutions: dict[str, Mapping[str, Any]] = {}
    for event in resolution_events:
        if _row_kind(event) != "outcome-resolution":
            continue
        outcome_id = _text(event.get("outcomeId"))
        if outcome_id is not None:
            resolutions[outcome_id] = event

    issuances: list[dict[str, Any]] = []
    for state_key, state in states.items():
        attempt_event = _only_event(state, "council-attempt-v2")
        if attempt_event is None or attempt_event[0] <= activation_line:
            continue
        attempt = attempt_event[1]
        outcome = attempt.get("sharedOutcome")
        outcome_id = (
            _text(outcome.get("outcomeId"))
            if isinstance(outcome, Mapping)
            else None
        )
        outcome_fingerprint = (
            _text(outcome.get("fingerprint"))
            if isinstance(outcome, Mapping)
            else None
        )
        outcome_class = _enum_text(attempt.get("outcomeClass"), OUTCOME_CLASSES)
        reasons = list(analysis_states[state_key]["incompleteReasons"])
        if outcome_id is None:
            reasons.append("missing-or-invalid-outcome-id")
        if outcome_fingerprint is None:
            reasons.append("missing-or-invalid-outcome-fingerprint")
        if outcome_class is None:
            reasons.append("missing-or-invalid-outcome-class")
        issuances.append(
            {
                "lineNumber": attempt_event[0],
                "stateKey": state_key,
                "runId": state["runId"],
                "outcomeId": outcome_id,
                "outcomeFingerprint": outcome_fingerprint,
                "outcomeClass": outcome_class or "invalid-or-missing",
                "attempt": attempt,
                "exclusionReasons": list(dict.fromkeys(reasons)),
            }
        )

    # Outcome IDs are deliberately run-derived issuance IDs. Fingerprints are the
    # cross-run underlying-event identity. Label every recurrence before applying
    # integrity eligibility so retries cannot later look like independent events.
    prior_ids_by_fingerprint: dict[str, list[str]] = defaultdict(list)
    for issuance in sorted(issuances, key=lambda item: item["lineNumber"]):
        fingerprint = issuance["outcomeFingerprint"]
        prior_ids = (
            list(prior_ids_by_fingerprint[fingerprint])
            if isinstance(fingerprint, str)
            else []
        )
        issuance["underlyingOutcomeIssuanceOrdinal"] = len(prior_ids) + 1
        issuance["isRepeatedUnderlyingOutcome"] = bool(prior_ids)
        issuance["priorOutcomeIdsForFingerprint"] = prior_ids
        outcome_id = issuance["outcomeId"]
        if isinstance(fingerprint, str) and isinstance(outcome_id, str):
            prior_ids_by_fingerprint[fingerprint].append(outcome_id)

    # The prospective contract assigns one outcome class to an underlying event,
    # not independently to each issuance. Historical or corrupt rows can violate
    # that invariant. In that case every observed issuance for the fingerprint is
    # conservatively treated as intervention-sensitive, so no relabelled retry can
    # enter the exogenous Brier headline.
    declared_classes_by_fingerprint: dict[str, set[str]] = defaultdict(set)
    for issuance in issuances:
        fingerprint = issuance["outcomeFingerprint"]
        outcome_class = issuance["outcomeClass"]
        if (
            isinstance(fingerprint, str)
            and outcome_class in OUTCOME_CLASSES
        ):
            declared_classes_by_fingerprint[fingerprint].add(str(outcome_class))
    conflicting_class_fingerprints = {
        fingerprint
        for fingerprint, classes in declared_classes_by_fingerprint.items()
        if len(classes) > 1
    }
    for issuance in issuances:
        fingerprint = issuance["outcomeFingerprint"]
        issuance["declaredOutcomeClass"] = issuance["outcomeClass"]
        issuance["outcomeClassConflict"] = (
            isinstance(fingerprint, str)
            and fingerprint in conflicting_class_fingerprints
        )
        if issuance["outcomeClassConflict"]:
            issuance["outcomeClass"] = "intervention-sensitive"

    # Headline outcome identity is defined solely by integrity-eligible issuances.
    # Every other observed issuance remains explicit below, including unresolved
    # attempts and attempts that reuse the ID of an otherwise valid outcome.
    eligible_outcomes: dict[str, dict[str, Any]] = {}
    excluded_issuances: list[dict[str, Any]] = []
    for issuance in sorted(issuances, key=lambda item: item["lineNumber"]):
        outcome_id = issuance["outcomeId"]
        eligible = (
            not issuance["exclusionReasons"]
            and isinstance(outcome_id, str)
            and isinstance(issuance["outcomeFingerprint"], str)
            and issuance["outcomeClass"] in OUTCOME_CLASSES
        )
        if eligible and outcome_id not in eligible_outcomes:
            eligible_outcomes[outcome_id] = issuance
            continue
        if eligible:
            issuance = dict(issuance)
            issuance["exclusionReasons"] = ["duplicate-outcome-id-issuance"]
        excluded_issuances.append(issuance)

    # Post-activation V1 observations are denominator-visible but cannot enter a V2
    # outcome headline. Retain their outcome issuance separately where identifiable.
    for state_key, state in states.items():
        analysis = analysis_states[state_key]
        attempt_event = _only_event(state, V1_ATTEMPT_KIND)
        completion_event = _only_event(state, V1_COMPLETION_KIND)
        candidates: list[tuple[int, str | None, str]] = []
        if attempt_event is not None:
            outcome = attempt_event[1].get("sharedOutcome")
            candidates.append(
                (
                    attempt_event[0],
                    _text(outcome.get("outcomeId"))
                    if isinstance(outcome, Mapping)
                    else None,
                    V1_ATTEMPT_KIND,
                )
            )
        elif completion_event is not None:
            predictions = completion_event[1].get("predictions")
            seen_ids: set[str | None] = set()
            if isinstance(predictions, list):
                for prediction in predictions:
                    if not isinstance(prediction, Mapping):
                        continue
                    outcome_id = _text(prediction.get("outcomeId"))
                    if outcome_id in seen_ids:
                        continue
                    seen_ids.add(outcome_id)
                    candidates.append(
                        (completion_event[0], outcome_id, V1_COMPLETION_KIND)
                    )
            if not candidates:
                candidates.append((completion_event[0], None, V1_COMPLETION_KIND))
        for line_number, outcome_id, source_kind in candidates:
            if line_number <= activation_line:
                continue
            reasons = list(analysis["incompleteReasons"])
            if outcome_id is None:
                reasons.append("missing-or-invalid-outcome-id")
            excluded_issuances.append(
                {
                    "lineNumber": line_number,
                    "stateKey": state_key,
                    "runId": state["runId"],
                    "outcomeId": outcome_id,
                    "outcomeClass": "v1-or-legacy",
                    "attempt": attempt_event[1] if attempt_event is not None else {},
                    "sourceKind": source_kind,
                    "exclusionReasons": list(dict.fromkeys(reasons)),
                }
            )

    resolved_issuance_by_class: Counter[str] = Counter()
    resolved_values_by_fingerprint: dict[str, dict[str, set[bool]]] = {
        "exogenous": defaultdict(set),
        "intervention-sensitive": defaultdict(set),
    }
    resolved_exogenous_issuance_values: dict[str, bool] = {}

    def resolution_for_issuance(
        issuance: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        """Return only a sidecar label bound to this exact outcome fingerprint."""

        outcome_id = issuance.get("outcomeId")
        fingerprint = issuance.get("outcomeFingerprint")
        if not isinstance(outcome_id, str) or not isinstance(fingerprint, str):
            return None
        resolution = resolutions.get(outcome_id)
        if (
            resolution is None
            or _text(resolution.get("outcomeFingerprint")) != fingerprint
        ):
            return None
        return resolution

    for outcome_id, issuance in eligible_outcomes.items():
        outcome_class = issuance["outcomeClass"]
        resolution = resolution_for_issuance(issuance)
        if (
            resolution is not None
            and resolution.get("status") == "resolved"
            and isinstance(resolution.get("cameTrue"), bool)
        ):
            resolved_issuance_by_class[outcome_class] += 1
            fingerprint = str(issuance["outcomeFingerprint"])
            resolved_values_by_fingerprint[outcome_class][fingerprint].add(
                bool(resolution["cameTrue"])
            )
            if outcome_class == "exogenous":
                resolved_exogenous_issuance_values[outcome_id] = bool(
                    resolution["cameTrue"]
                )

    resolved_unique_by_class = {
        outcome_class: sum(
            len(values) == 1
            for values in resolved_values_by_fingerprint[outcome_class].values()
        )
        for outcome_class in ("exogenous", "intervention-sensitive")
    }
    conflicting_resolution_by_class = {
        outcome_class: sum(
            len(values) > 1
            for values in resolved_values_by_fingerprint[outcome_class].values()
        )
        for outcome_class in ("exogenous", "intervention-sensitive")
    }
    resolved_exogenous_values = {
        fingerprint: next(iter(values))
        for fingerprint, values in resolved_values_by_fingerprint["exogenous"].items()
        if len(values) == 1
    }

    score_rows: list[tuple[str, str, str, str, float, bool, str, str]] = []
    excluded_score_rows: list[dict[str, Any]] = []

    def valid_predictions_for(
        issuance: Mapping[str, Any], outcome_id: str
    ) -> list[tuple[str, int]]:
        state = states[str(issuance["stateKey"])]
        completion_event = _only_event(state, "council-v2")
        completion = completion_event[1] if completion_event is not None else {}
        predictions = completion.get("predictions")
        valid: list[tuple[str, int]] = []
        if not isinstance(predictions, list):
            return valid
        for prediction in predictions:
            if (
                not isinstance(prediction, Mapping)
                or prediction.get("outcomeId") != outcome_id
            ):
                continue
            probability = prediction.get("probability")
            seat = _text(prediction.get("seat"))
            if (
                isinstance(probability, bool)
                or not isinstance(probability, int)
                or not 0 <= probability <= 100
                or seat is None
            ):
                continue
            valid.append((seat, probability))
        return valid

    for outcome_id, came_true in sorted(resolved_exogenous_issuance_values.items()):
        issuance = eligible_outcomes[outcome_id]
        fingerprint = str(issuance["outcomeFingerprint"])
        if len(resolved_values_by_fingerprint["exogenous"][fingerprint]) != 1:
            # Contradictory resolutions for one underlying event are observable
            # diagnostics, never scoreable labels.
            continue
        attempt = issuance["attempt"]
        plan_by_seat: dict[str, Mapping[str, Any]] = {}
        plan = attempt.get("seatPlan")
        if isinstance(plan, list):
            for item in plan:
                if not isinstance(item, Mapping):
                    continue
                planned_seat = _text(item.get("seatId"))
                if planned_seat is not None:
                    plan_by_seat[planned_seat] = item
        for seat, probability in valid_predictions_for(issuance, outcome_id):
            plan = plan_by_seat.get(seat, {})
            version = _text(plan.get("agentVersion")) or "unknown"
            role = _text(plan.get("role")) or "unknown"
            digest = _text(plan.get("agentDefinitionDigest")) or "unknown"
            observed = 1.0 if came_true else 0.0
            score = ((probability / 100.0) - observed) ** 2
            score_rows.append(
                (
                    seat,
                    role,
                    version,
                    digest,
                    score,
                    came_true,
                    outcome_id,
                    fingerprint,
                )
            )

    for issuance in excluded_issuances:
        outcome_id = issuance["outcomeId"]
        if (
            not isinstance(outcome_id, str)
            or issuance["outcomeClass"] != "exogenous"
        ):
            continue
        resolution = resolution_for_issuance(issuance)
        if (
            resolution is None
            or resolution.get("status") != "resolved"
            or not isinstance(resolution.get("cameTrue"), bool)
        ):
            continue
        predictions = valid_predictions_for(issuance, outcome_id)
        excluded_score_rows.append(
            {
                "runId": issuance["runId"],
                "outcomeId": outcome_id,
                "outcomeFingerprint": issuance.get("outcomeFingerprint"),
                "predictionCount": len(predictions),
                "exclusionReasons": list(issuance["exclusionReasons"]),
                # Compatibility alias for the earlier report shape.
                "incompleteReasons": list(issuance["exclusionReasons"]),
            }
        )

    score_total = sum(item[4] for item in score_rows)
    strata: dict[tuple[str, str, str, str], list[float]] = defaultdict(list)
    for seat, role, version, digest, score, _came_true, _outcome_id, _fingerprint in score_rows:
        strata[(seat, role, version, digest)].append(score)

    resolved_count = len(resolved_exogenous_values)
    true_count = sum(resolved_exogenous_values.values())
    false_count = resolved_count - true_count
    polarity_warning = (
        resolved_count > 0 and max(true_count, false_count) * 5 > resolved_count * 4
    )
    prediction_weighted_true_count = sum(item[5] for item in score_rows)
    prediction_weighted_base_rate = _mean_or_none(
        float(prediction_weighted_true_count), len(score_rows)
    )
    hindsight = (
        None
        if prediction_weighted_base_rate is None
        else prediction_weighted_base_rate * (1.0 - prediction_weighted_base_rate)
    )

    excluded_outcome_rows: list[dict[str, Any]] = []
    excluded_status_counts: Counter[str] = Counter()
    excluded_class_counts: Counter[str] = Counter()
    for issuance in sorted(excluded_issuances, key=lambda item: item["lineNumber"]):
        outcome_id = issuance["outcomeId"]
        resolution = resolution_for_issuance(issuance)
        status = (
            _enum_text(resolution.get("status"), {"resolved", "void"})
            if isinstance(resolution, Mapping)
            else None
        ) or "unresolved"
        excluded_status_counts[status] += 1
        excluded_class_counts[str(issuance["outcomeClass"])] += 1
        item = {
            "lineNumber": issuance["lineNumber"],
            "runId": issuance["runId"],
            "outcomeId": outcome_id,
            "outcomeFingerprint": issuance.get("outcomeFingerprint"),
            "outcomeClass": issuance["outcomeClass"],
            "declaredOutcomeClass": issuance.get(
                "declaredOutcomeClass", issuance["outcomeClass"]
            ),
            "outcomeClassConflict": bool(
                issuance.get("outcomeClassConflict", False)
            ),
            "underlyingOutcomeIssuanceOrdinal": issuance.get(
                "underlyingOutcomeIssuanceOrdinal"
            ),
            "isRepeatedUnderlyingOutcome": issuance.get(
                "isRepeatedUnderlyingOutcome", False
            ),
            "priorOutcomeIdsForFingerprint": list(
                issuance.get("priorOutcomeIdsForFingerprint", [])
            ),
            "resolutionStatus": status,
            "exclusionReasons": list(issuance["exclusionReasons"]),
        }
        if "sourceKind" in issuance:
            item["sourceKind"] = issuance["sourceKind"]
        if (
            status == "resolved"
            and isinstance(resolution, Mapping)
            and isinstance(resolution.get("cameTrue"), bool)
        ):
            item["cameTrue"] = bool(resolution["cameTrue"])
        excluded_outcome_rows.append(item)

    eligible_issuance_rows = [
        {
            "runId": issuance["runId"],
            "outcomeId": issuance["outcomeId"],
            "outcomeFingerprint": issuance["outcomeFingerprint"],
            "outcomeClass": issuance["outcomeClass"],
            "declaredOutcomeClass": issuance.get(
                "declaredOutcomeClass", issuance["outcomeClass"]
            ),
            "outcomeClassConflict": bool(
                issuance.get("outcomeClassConflict", False)
            ),
            "underlyingOutcomeIssuanceOrdinal": issuance[
                "underlyingOutcomeIssuanceOrdinal"
            ],
            "isRepeatedUnderlyingOutcome": issuance[
                "isRepeatedUnderlyingOutcome"
            ],
            "priorOutcomeIdsForFingerprint": list(
                issuance["priorOutcomeIdsForFingerprint"]
            ),
        }
        for issuance in sorted(
            eligible_outcomes.values(), key=lambda item: item["lineNumber"]
        )
    ]
    eligible_fingerprints_by_class = {
        outcome_class: {
            str(issuance["outcomeFingerprint"])
            for issuance in eligible_outcomes.values()
            if issuance["outcomeClass"] == outcome_class
        }
        for outcome_class in ("exogenous", "intervention-sensitive")
    }

    return {
        "resolutionProvenanceDiagnostics": {
            "resolutionSource": "validated fingerprint-bound sidecar only",
            "sidecarResolutionEventCount": len(resolution_events),
            "invalidLedgerResolutionEventCount": len(
                ledger_resolution_diagnostics
            ),
            "invalidLedgerResolutionEvents": ledger_resolution_diagnostics,
        },
        "outcomeCounts": {
            "v1OrLegacy": len(legacy_outcomes),
            # Compatibility fields retain their prior names but now count the
            # only defensible outcome unit: unique underlying fingerprints.
            "exogenousV2": len(eligible_fingerprints_by_class["exogenous"]),
            "interventionSensitiveV2": len(
                eligible_fingerprints_by_class["intervention-sensitive"]
            ),
            "resolvedExogenousV2": resolved_unique_by_class["exogenous"],
            "resolvedInterventionSensitiveV2": resolved_unique_by_class[
                "intervention-sensitive"
            ],
            "eligibleV2IssuanceCount": len(eligible_outcomes),
            "uniqueUnderlyingFingerprintCount": len(
                {
                    str(value["outcomeFingerprint"])
                    for value in eligible_outcomes.values()
                }
            ),
            "exogenousV2IssuanceCount": sum(
                value["outcomeClass"] == "exogenous"
                for value in eligible_outcomes.values()
            ),
            "interventionSensitiveV2IssuanceCount": sum(
                value["outcomeClass"] == "intervention-sensitive"
                for value in eligible_outcomes.values()
            ),
            "resolvedExogenousV2IssuanceCount": resolved_issuance_by_class[
                "exogenous"
            ],
            "resolvedInterventionSensitiveV2IssuanceCount": resolved_issuance_by_class[
                "intervention-sensitive"
            ],
        },
        "outcomeIdentityDiagnostics": {
            "issuanceUnit": "run-derived outcomeId",
            "underlyingOutcomeUnit": "sharedOutcome fingerprint",
            "eligibleIssuanceCount": len(eligible_issuance_rows),
            "uniqueUnderlyingFingerprintCount": len(
                {item["outcomeFingerprint"] for item in eligible_issuance_rows}
            ),
            "repeatedIssuanceCount": sum(
                bool(item["isRepeatedUnderlyingOutcome"])
                for item in eligible_issuance_rows
            ),
            "conflictingResolvedExogenousFingerprintCount": (
                conflicting_resolution_by_class["exogenous"]
            ),
            "conflictingResolvedInterventionSensitiveFingerprintCount": (
                conflicting_resolution_by_class["intervention-sensitive"]
            ),
            "outcomeClassConflictFingerprintCount": len(
                conflicting_class_fingerprints
            ),
            "outcomeClassConflicts": [
                {
                    "outcomeFingerprint": fingerprint,
                    "declaredOutcomeClasses": sorted(
                        declared_classes_by_fingerprint[fingerprint]
                    ),
                    "issuanceCount": sum(
                        issuance["outcomeFingerprint"] == fingerprint
                        for issuance in issuances
                    ),
                    "outcomeIds": sorted(
                        {
                            str(issuance["outcomeId"])
                            for issuance in issuances
                            if issuance["outcomeFingerprint"] == fingerprint
                            and isinstance(issuance["outcomeId"], str)
                        }
                    ),
                    "effectiveOutcomeClass": "intervention-sensitive",
                }
                for fingerprint in sorted(conflicting_class_fingerprints)
            ],
            "eligibleIssuances": eligible_issuance_rows,
        },
        "excludedOrInvalidOutcomeStratum": {
            "scope": "outcome issuances excluded from eligible V2 headlines",
            "issuanceCount": len(excluded_outcome_rows),
            "resolvedIssuanceCount": excluded_status_counts["resolved"],
            "resolvedOutcomeCount": len(
                {
                    item["outcomeFingerprint"]
                    for item in excluded_outcome_rows
                    if item["resolutionStatus"] == "resolved"
                    and isinstance(item["outcomeFingerprint"], str)
                }
            ),
            "resolvedUniqueUnderlyingFingerprintCount": len(
                {
                    item["outcomeFingerprint"]
                    for item in excluded_outcome_rows
                    if item["resolutionStatus"] == "resolved"
                    and isinstance(item["outcomeFingerprint"], str)
                }
            ),
            "unresolvedIssuanceCount": excluded_status_counts["unresolved"],
            "voidIssuanceCount": excluded_status_counts["void"],
            "knownOutcomeIds": sorted(
                {
                    item["outcomeId"]
                    for item in excluded_outcome_rows
                    if isinstance(item["outcomeId"], str)
                }
            ),
            "knownOutcomeFingerprints": sorted(
                {
                    item["outcomeFingerprint"]
                    for item in excluded_outcome_rows
                    if isinstance(item["outcomeFingerprint"], str)
                }
            ),
            "byOutcomeClass": dict(sorted(excluded_class_counts.items())),
            "issuances": excluded_outcome_rows,
        },
        "resolvedExogenousPolarity": {
            "unit": "unique underlying sharedOutcome fingerprint",
            "resolvedIssuanceCount": resolved_issuance_by_class["exogenous"],
            "resolvedOutcomeCount": resolved_count,
            "trueCount": true_count,
            "falseCount": false_count,
            "majorityFraction": _mean_or_none(float(max(true_count, false_count)), resolved_count),
            "warningAboveEightyPercent": polarity_warning,
            "conflictingResolutionFingerprintCount": (
                conflicting_resolution_by_class["exogenous"]
            ),
        },
        "descriptiveForecastAccuracy": {
            "scope": (
                "resolved exogenous V2 predictions from capture-complete, "
                "artifact-verified runs only"
            ),
            "predictionCount": len(score_rows),
            "resolvedIssuanceCount": len({item[6] for item in score_rows}),
            "uniqueUnderlyingFingerprintCount": len(
                {item[7] for item in score_rows}
            ),
            "conflictingResolutionFingerprintCount": (
                conflicting_resolution_by_class["exogenous"]
            ),
            "meanBrier": _mean_or_none(score_total, len(score_rows)),
            "constantFiftyBrier": 0.25,
            "hindsightBaseRateBrierBound": hindsight,
            "hindsightBaseRateWeighting": "same prediction rows as meanBrier",
            "predictionWeightedObservedTrueFraction": prediction_weighted_base_rate,
            "seatVersionStrata": [
                {
                    "seatId": key[0],
                    "role": key[1],
                    "agentVersion": key[2],
                    "agentDefinitionDigest": key[3],
                    "predictionCount": len(values),
                    "meanBrier": sum(values) / len(values),
                }
                for key, values in sorted(strata.items())
            ],
            "excludedOrInvalidStratum": {
                "resolvedIssuanceCount": len(excluded_score_rows),
                "resolvedOutcomeCount": len(
                    {
                        item["outcomeFingerprint"]
                        for item in excluded_score_rows
                        if isinstance(item.get("outcomeFingerprint"), str)
                    }
                ),
                "resolvedUniqueUnderlyingFingerprintCount": len(
                    {
                        item["outcomeFingerprint"]
                        for item in excluded_score_rows
                        if isinstance(item.get("outcomeFingerprint"), str)
                    }
                ),
                "predictionCount": sum(
                    item["predictionCount"] for item in excluded_score_rows
                ),
                "runs": excluded_score_rows,
            },
        },
    }


def analyze_capture_data(
    rows: Iterable[Mapping[str, Any]],
    *,
    as_of: datetime | str,
    resolution_events: Iterable[Mapping[str, Any]] = (),
    artifact_integrity: ArtifactIntegrity | None = None,
    finding_summaries: Mapping[str, Mapping[str, Any]] | None = None,
    activation_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic, capture-only report for the frozen first-ten cohort.

    ``rows`` must retain append order and already be decoded into mappings. Outcome
    resolutions in this stream are ledger-origin invalid diagnostics and never
    grade V2 outcomes. ``resolution_events`` is the separate trust channel for
    fingerprint-bound sidecar rows that the integration layer has already fully
    validated. A live
    integration must pass its report-time artifact verifier as ``artifact_integrity``;
    omitting it intentionally reports that integrity re-verification was not applied.
    ``finding_summaries`` may contain the output of ``summarize_findings`` keyed
    by ``finding_summary_record_key(line_number, completion)``.  This occurrence
    identity binds append position and ``RAW_RECORD_SHA256_ANNOTATION`` so even
    byte-exact duplicate rows remain distinct.  For decoded-only inputs without
    exact-byte annotations, callers may instead use the synthetic lifecycle key
    ``ledger-state-at-line-N``.  Public run IDs are deliberately not accepted as
    summary identities because they are reusable.
    """

    parsed_rows = list(rows)
    if any(not isinstance(row, Mapping) for row in parsed_rows):
        raise DataHealthError("rows must contain parsed mapping objects")
    parsed_resolution_events = list(resolution_events)
    if any(not isinstance(row, Mapping) for row in parsed_resolution_events):
        raise DataHealthError(
            "resolution_events must contain parsed mapping objects"
        )
    now = _as_utc(as_of, "as_of")
    activation_events = [
        (line, row)
        for line, row in enumerate(parsed_rows, 1)
        if row.get("kind") == "capture-activation"
    ]
    if len(activation_events) != 1:
        raise DataHealthError(
            f"expected exactly one capture-activation, found {len(activation_events)}"
        )
    activation_line, activation = activation_events[0]
    activation_id = _text(activation.get("activationId"))
    if activation_id is None:
        raise DataHealthError("capture-activation requires activationId")

    states, eligible_order, duplicate_retries = _group_rows(parsed_rows, activation_line)
    analysis_states = {
        run: _run_analysis_state(
            state,
            activation_id=activation_id,
            as_of=now,
            artifact_integrity=artifact_integrity,
        )
        for run, state in states.items()
    }
    cohort = eligible_order[:COHORT_SIZE]
    run_reports: list[dict[str, Any]] = []
    complete_count = 0
    active_durations: list[float] = []
    elapsed_durations: list[float] = []
    valid_duration_count = 0
    artifact_totals: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    seat_strata: Counter[tuple[str, str, str, str]] = Counter()

    for position, run in enumerate(cohort, 1):
        state = states[run]
        analysis = analysis_states[run]
        reasons = list(analysis["incompleteReasons"])
        active = analysis["activeHandlingSeconds"]
        elapsed = analysis["elapsedSeconds"]
        valid_duration = bool(analysis["validDuration"])
        active_durations.append(active)
        elapsed_durations.append(elapsed)
        if valid_duration:
            valid_duration_count += 1

        artifact_health = analysis["artifactHealth"]
        for key, value in artifact_health.items():
            if isinstance(value, int) and not isinstance(value, bool):
                artifact_totals[key] += value
        complete = bool(analysis["eligibleForHeadlineAnalysis"])
        complete_count += int(complete)

        attempt_event = _only_event(state, "council-attempt-v2")
        if attempt_event is not None:
            attempt = attempt_event[1]
            family = _text(attempt.get("decisionFamilyId"))
            if family is not None:
                family_counts[family] += 1
            plan = attempt.get("seatPlan")
            if isinstance(plan, list):
                for item in plan:
                    if not isinstance(item, Mapping):
                        continue
                    seat = _text(item.get("seatId")) or "missing"
                    role = _text(item.get("role")) or "missing"
                    version = _text(item.get("agentVersion")) or "missing"
                    digest = _text(item.get("agentDefinitionDigest")) or "missing"
                    seat_strata[(seat, role, version, digest)] += 1

        run_reports.append(
            {
                "position": position,
                "runId": state["runId"],
                "eligibilityKind": state["eligibilityKind"],
                "complete": complete,
                "eligibleForHeadlineAnalysis": complete,
                "incompleteReasons": reasons,
                "activeHandlingSeconds": active,
                "elapsedSeconds": elapsed,
                "invalidationCount": len(state["invalidations"]),
            }
        )

    eligible_count = len(cohort)
    capture_fraction = _mean_or_none(float(complete_count), eligible_count)
    cohort_filled = eligible_count == COHORT_SIZE
    median_active = _median_for_frozen_cohort(active_durations)
    median_elapsed = _median_for_frozen_cohort(elapsed_durations)
    capture_gate = bool(
        cohort_filled
        and capture_fraction is not None
        and capture_fraction >= CAPTURE_FRACTION_BAR
    )
    time_gate = bool(
        cohort_filled
        and valid_duration_count == COHORT_SIZE
        and median_active is not None
        and median_active <= ACTIVE_HANDLING_BAR_SECONDS
    )
    cutoff_reached = now >= COHORT_CUTOFF.astimezone(timezone.utc)
    if cohort_filled:
        operational_outcome: bool | None = capture_gate and time_gate
    elif cutoff_reached:
        operational_outcome = False
    else:
        operational_outcome = None

    pre_attempt_crashes = 0
    abandoned_attempts = 0
    failed_attempts = 0
    rejected_completions = 0
    orphan_completions = 0
    orphan_attempts = 0
    v1_runs = 0
    for run in cohort:
        state = states[run]
        events = state["events"]
        initiation_count = len(events.get("capture-initiation", []))
        attempt_count = len(events.get("council-attempt-v2", []))
        completion_count = len(events.get("council-v2", []))
        if events.get(V1_ATTEMPT_KIND) or events.get(V1_COMPLETION_KIND):
            v1_runs += 1
            if events.get(V1_COMPLETION_KIND) and not events.get(V1_ATTEMPT_KIND):
                orphan_completions += 1
            continue
        if initiation_count and not attempt_count:
            pre_attempt_crashes += 1
        if attempt_count and not initiation_count:
            orphan_attempts += 1
        finished_event = _only_event(state, "council-seats-finished")
        seat_states = finished_event[1].get("seatStates") if finished_event else None
        execution_failed = isinstance(seat_states, Mapping) and any(
            _enum_text(value, TERMINAL_SEAT_STATES) == "unavailable"
            for value in seat_states.values()
        )
        if execution_failed:
            failed_attempts += 1
        if attempt_count and not completion_count and not execution_failed:
            abandoned_attempts += 1
        if completion_count and not initiation_count:
            orphan_completions += 1
        rejection_reasons = set(_binding_reasons(state, activation_id)) - {
            "seat-execution-failure"
        }
        schema_invalid_completion = any(
            "_captureSchemaError" in row
            for _line_number, row in events.get("council-v2", [])
        )
        if completion_count and (rejection_reasons or schema_invalid_completion):
            rejected_completions += completion_count

    audit_assignments = {
        (row.get("activationId"), row.get("decisionFamilyId")): row.get(
            "auditAssignment"
        )
        for row in parsed_rows
        if row.get("kind") == "council-attempt-v2"
        and "_captureSchemaError" not in row
        and isinstance(row.get("auditAssignment"), Mapping)
    }
    selected_audit_families = {
        family
        for family, assignment in audit_assignments.items()
        if assignment.get("selected") is True
    }
    audit_cases = [
        row
        for row in parsed_rows
        if row.get("kind") == "finding-audit-case-v2"
        and "_captureSchemaError" not in row
    ]
    evidence_ready = bool(
        isinstance(activation_evidence, Mapping)
        and activation_evidence.get("activationVerdict", {}).get("ready") is True
    )
    current_healthy = bool(
        isinstance(activation_evidence, Mapping)
        and activation_evidence.get("currentHealth", {}).get("healthy") is True
    )
    evidence_blockers = (
        list(activation_evidence.get("blockers", []))
        if isinstance(activation_evidence, Mapping)
        else [
            "prospective-audit-not-implemented",
            "durability-evidence-not-supplied",
        ]
    )

    result = {
        "reportKind": "council-capture-data-health",
        "captureOnly": True,
        "reportLabels": {
            "forecast": "descriptive forecast accuracy",
            "findingOverlap": "within-run finding overlap",
            "novelty": "operator-reported novelty",
            "seatComparison": "NO VERDICT",
        },
        "activationId": activation_id,
        "cohort": {
            "targetInitiationCount": COHORT_SIZE,
            "eligibleInitiationCount": eligible_count,
            "completeInitiationCount": complete_count,
            "captureFraction": capture_fraction,
            "cohortFilled": cohort_filled,
            "cutoffReached": cutoff_reached,
            "captureFractionGatePassed": capture_gate,
            "timeGatePassed": time_gate,
            "sharedOperationalOutcome": operational_outcome,
            "duplicateIdempotentInitiationRetryCount": duplicate_retries,
            "runs": run_reports,
        },
        "lifecycleCounts": {
            "preAttemptCrashCount": pre_attempt_crashes,
            "abandonedAttemptCount": abandoned_attempts,
            "failedAttemptCount": failed_attempts,
            "rejectedCompletionCount": rejected_completions,
            "orphanAttemptCount": orphan_attempts,
            "orphanCompletionCount": orphan_completions,
            "postActivationV1RunCount": v1_runs,
        },
        "timing": {
            "validDurationCount": valid_duration_count,
            "activeHandlingSeconds": active_durations,
            "elapsedSeconds": elapsed_durations,
            "medianActiveHandlingSeconds": median_active,
            "medianElapsedSeconds": median_elapsed,
        },
        "artifacts": {
            "integrityCheckApplied": artifact_integrity is not None,
            "requiredArtifactCount": artifact_totals["requiredArtifactCount"],
            "presentArtifactCount": artifact_totals["presentArtifactCount"],
            "integrityCheckedCount": artifact_totals["integrityCheckedCount"],
            "artifactCompletenessFailureCount": artifact_totals[
                "artifactCompletenessFailureCount"
            ],
            "artifactIntegrityFailureCount": artifact_totals[
                "artifactIntegrityFailureCount"
            ],
        },
        "decisionFamilies": {
            "familyCount": len(family_counts),
            "runCounts": dict(sorted(family_counts.items())),
        },
        "seatVersionStrata": [
            {
                "seatId": key[0],
                "role": key[1],
                "agentVersion": key[2],
                "agentDefinitionDigest": key[3],
                "plannedRunCount": count,
            }
            for key, count in sorted(seat_strata.items())
        ],
        "findings": _finding_health(
            cohort, states, finding_summaries, analysis_states
        ),
        "prospectiveAudit": {
            "status": "PROTOCOL_READY" if evidence_ready else "NOT_IMPLEMENTED",
            "activationBlocking": not evidence_ready,
            "samplingRule": "one in five eligible decision families",
            "independentSeatAnonymizedRegrouping": evidence_ready,
            "omittedActionableClaimReview": evidence_ready,
            "agreementReported": evidence_ready,
            "assignedFamilyCount": len(audit_assignments),
            "selectedFamilyCount": len(selected_audit_families),
            "auditCaseCount": len(audit_cases),
            "actualProspectiveCountsBackfilled": False,
        },
        "durability": {
            "status": "HEALTHY" if current_healthy else "NOT_SUPPLIED",
            "activationBlocking": not current_healthy,
            "snapshotAgeSeconds": None,
            "lastVerifiedRestoreAt": None,
            "offHostReadbackVerifiedAt": None,
            "historicallyValidAtActivation": evidence_ready,
            "currentBlockers": (
                list(activation_evidence.get("currentHealth", {}).get("blockers", []))
                if isinstance(activation_evidence, Mapping)
                else ["durability-evidence-not-supplied"]
            ),
        },
        "activationReadiness": {
            "status": "READY" if evidence_ready and current_healthy else "BLOCKED",
            "blockingReasons": evidence_blockers,
            "historicallyReady": evidence_ready,
            "currentlyHealthy": current_healthy,
        },
    }
    result.update(
        _outcome_health(
            parsed_rows,
            parsed_resolution_events,
            activation_line,
            activation_id,
            states,
            analysis_states,
        )
    )
    return result


__all__ = [
    "DataHealthError",
    "RAW_RECORD_SHA256_ANNOTATION",
    "analyze_capture_data",
    "finding_summary_record_key",
]
