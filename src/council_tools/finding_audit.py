"""Prospective, blinded audit protocol for council finding capture.

The module is intentionally storage- and runtime-agnostic.  It constructs and
validates JSON-serializable records which a caller may append while holding its
own evidence lock.  In particular, it never discovers live rows, backfills an
assignment, writes an artifact, or changes a council denominator.

The frozen protocol has four important separations:

* a family is selected before its first attempt and that persisted answer is
  inherited by retries (including the explicit ``not-selected`` answer);
* an adjudicator packet contains opaque, case-local subject aliases while the
  identity map remains a separate object;
* adjudicator claims bind exact character spans to the retained output digest;
* classification, grouping, and omitted-span agreement remain separate
  measures.  They are not averaged into a forgiving scalar.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from itertools import combinations
from pathlib import PurePosixPath
from typing import Any


SCHEMA_VERSION = 1
PROTOCOL_VERSION = "finding-audit-v1"
CERTIFICATE_VERSION = "finding-audit-rehearsal-v1"
SELECTION_ALGORITHM = "sha256-domain-v1"
SELECTION_DOMAIN = "ai-council/finding-audit-assignment/v1"
SELECTION_MODULUS = 5
DEFAULT_SELECTION_RESIDUE = 0
MINIMUM_AGREEMENT = 0.60
AGREEMENT_METRICS = (
    "classificationAgreement",
    "pairwiseGroupingAgreement",
    "omittedSpanOverlap",
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PROTOCOL_VERSION = re.compile(r"^finding-audit-v[1-9][0-9]*$")
_ACTIVATION_ID = re.compile(r"^activation-[0-9a-f]{32}$")
_RUN_ID = re.compile(r"^run-[0-9a-f]{32}$")
_FAMILY_ID = re.compile(r"^family-[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_FINDING_ID = re.compile(r"^finding-[0-9a-f]{32}$")
_CASE_ID = re.compile(r"^audit-case-[0-9a-f]{32}$")
_SUBJECT_ALIAS = re.compile(r"^subject-[0-9a-f]{12}$")
_CLAIM_ID = re.compile(r"^audit-claim-[0-9a-f]{32}$")
_RESULT_ID = re.compile(r"^audit-result-[0-9a-f]{32}$")
_LOCAL_GROUP_ID = re.compile(
    r"^local-group-[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$"
)

_ARTIFACT_KEYS = frozenset({"path", "sha256", "bytes"})
_PROTOCOL_KEYS = frozenset(
    {
        "kind",
        "schemaVersion",
        "protocolVersion",
        "frozenProtocolArtifact",
        "selection",
        "adjudication",
        "prospective",
    }
)
_SELECTION_KEYS = frozenset(
    {"algorithm", "domainSeparator", "modulus", "residue"}
)
_ADJUDICATION_KEYS = frozenset(
    {"requiredAdjudicators", "minimumAgreement", "agreementMetrics"}
)
_PROSPECTIVE_KEYS = frozenset(
    {
        "backfillAllowed",
        "assignmentMoment",
        "retryPolicy",
        "denominatorEffect",
    }
)
_ASSIGNMENT_KEYS = frozenset(
    {
        "kind",
        "schemaVersion",
        "activationId",
        "decisionFamilyId",
        "firstRunId",
        "protocolVersion",
        "protocolSha256",
        "assignedAt",
        "selectionDigest",
        "bucket",
        "selected",
        "assignmentState",
        "provenance",
    }
)
_SOURCE_SUBJECT_KEYS = frozenset(
    {
        "seatId",
        "role",
        "modelId",
        "agentVersion",
        "sourceOutputText",
        "sourceOutputSha256",
        "visibleAnswer",
        "selfIdentificationRisk",
        "capturedFindings",
    }
)
_SOURCE_FINDING_KEYS = frozenset(
    {
        "findingId",
        "seatId",
        "category",
        "claim",
        "severity",
        "proposedAction",
        "evidenceSummary",
    }
)
_PACKET_KEYS = frozenset(
    {
        "kind",
        "schemaVersion",
        "auditCaseId",
        "protocolVersion",
        "protocolSha256",
        "activationId",
        "decisionFamilyId",
        "caseRunId",
        "assignmentSelectionDigest",
        "capturedAt",
        "anonymizationRisk",
        "subjects",
    }
)
_PACKET_SUBJECT_KEYS = frozenset(
    {
        "subjectAlias",
        "visibleAnswer",
        "visibleAnswerSha256",
        "sourceOutputSha256",
        "anonymizationRisk",
        "capturedFindings",
    }
)
_PACKET_FINDING_KEYS = frozenset(
    {
        "findingId",
        "category",
        "claim",
        "severity",
        "proposedAction",
        "evidenceSummary",
    }
)
_ALIAS_MAP_KEYS = frozenset(
    {
        "kind",
        "schemaVersion",
        "auditCaseId",
        "auditCaseSha256",
        "mappings",
    }
)
_ALIAS_MAPPING_KEYS = frozenset(
    {
        "subjectAlias",
        "seatId",
        "role",
        "modelId",
        "agentVersion",
        "sourceOutputSha256",
    }
)
_ANNOTATION_KEYS = frozenset(
    {
        "subjectAlias",
        "start",
        "end",
        "captureStatus",
        "capturedFindingIds",
        "localGroupId",
        "material",
        "actionable",
        "confidentlyWrong",
    }
)
_CLAIM_KEYS = frozenset(
    {
        "claimId",
        "subjectAlias",
        "sourceOutputSha256",
        "start",
        "end",
        "quotedText",
        "quotedSpanSha256",
        "captureStatus",
        "capturedFindingIds",
        "localGroupId",
        "material",
        "actionable",
        "confidentlyWrong",
    }
)
_RESULT_KEYS = frozenset(
    {
        "kind",
        "schemaVersion",
        "auditResultId",
        "auditCaseId",
        "auditCaseSha256",
        "protocolVersion",
        "protocolSha256",
        "adjudicatorIdentitySha256",
        "adjudicatedAt",
        "claims",
    }
)
_CERTIFICATE_KEYS = frozenset(
    {
        "kind",
        "schemaVersion",
        "certificateVersion",
        "runtimeCommit",
        "sourceTreeSha256",
        "frozenProtocolArtifact",
        "protocolVersion",
        "protocolSha256",
        "rehearsedAt",
        "checks",
        "syntheticRehearsalCounts",
        "actualProspectiveCounts",
        "certificateSha256",
    }
)
_REHEARSAL_CHECKS = (
    "deterministicSelection",
    "assignedBeforeAttempt",
    "persistedNonSelection",
    "retryInheritance",
    "noBackfill",
    "packetBlinding",
    "aliasMapExcluded",
    "selfIdentificationRiskReported",
    "sourceOutputDigestBound",
    "quotedSpanDigestBound",
    "omittedClaimDetection",
    "localRegroupingRecorded",
    "materialActionableClassified",
    "confidentlyWrongNonScalar",
    "twoAdjudicatorSupport",
    "separateAgreementMetrics",
    "unmeasurableGateVoid",
    "belowThresholdGateVoid",
    "prospectiveCountsRemainZero",
)


class FindingAuditError(ValueError):
    """An audit record violates the frozen prospective protocol."""


def _exact_mapping(value: Any, expected: frozenset[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FindingAuditError(f"{field} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing keys {missing}")
        if unknown:
            details.append(f"unknown keys {unknown}")
        raise FindingAuditError(f"{field} has invalid keys: {', '.join(details)}")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise FindingAuditError(f"{field} must be a list")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise FindingAuditError(f"{field} must be non-empty trimmed text")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise FindingAuditError(f"{field} must be boolean")
    return value


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise FindingAuditError(f"{field} must be an integer >= {minimum}")
    return value


def _digest(value: Any, field: str) -> str:
    value = _text(value, field)
    if _SHA256.fullmatch(value) is None:
        raise FindingAuditError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _stable_id(value: Any, pattern: re.Pattern[str], field: str) -> str:
    value = _text(value, field)
    if pattern.fullmatch(value) is None:
        raise FindingAuditError(f"{field} has an invalid stable id")
    return value


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise FindingAuditError("value is not canonically JSON-serializable") from exc


def _strict_json_object(value: str, field: str) -> Mapping[str, Any]:
    """Parse a retained output while rejecting duplicate object keys."""

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise FindingAuditError(f"{field} contains duplicate JSON keys")
            result[key] = item
        return result

    try:
        parsed = json.loads(value, object_pairs_hook=reject_duplicates)
    except FindingAuditError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        raise FindingAuditError(f"{field} must be strict JSON") from exc
    if not isinstance(parsed, Mapping):
        raise FindingAuditError(f"{field} must contain a JSON object")
    return parsed


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _content_sha256(value: Any) -> str:
    return _sha256(_canonical_json(value))


def _canonical_timestamp(value: datetime | str, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise FindingAuditError(f"{field} must be an ISO-8601 timestamp") from exc
    else:
        raise FindingAuditError(f"{field} must be an aware datetime or ISO-8601 text")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FindingAuditError(f"{field} must include a timezone")
    utc = parsed.astimezone(timezone.utc)
    return utc.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validated_canonical_timestamp(value: Any, field: str) -> str:
    raw = _text(value, field)
    canonical = _canonical_timestamp(raw, field)
    if raw != canonical:
        raise FindingAuditError(f"{field} must be canonical UTC with microseconds")
    return raw


def _validate_artifact(value: Any, field: str) -> dict[str, Any]:
    artifact = _exact_mapping(value, _ARTIFACT_KEYS, field)
    path = _text(artifact["path"], f"{field}.path")
    posix = PurePosixPath(path)
    if posix.is_absolute() or ".." in posix.parts or path in {".", ".."}:
        raise FindingAuditError(f"{field}.path must be a safe relative path")
    if "\\" in path or "\x00" in path or any(ord(character) < 0x20 for character in path):
        raise FindingAuditError(f"{field}.path must use safe POSIX syntax")
    digest = _digest(artifact["sha256"], f"{field}.sha256")
    byte_count = _integer(artifact["bytes"], f"{field}.bytes")
    return {"path": path, "sha256": digest, "bytes": byte_count}


def make_audit_protocol(
    *,
    frozen_protocol_artifact: Mapping[str, Any],
    selection_residue: int = DEFAULT_SELECTION_RESIDUE,
    protocol_version: str = PROTOCOL_VERSION,
) -> dict[str, Any]:
    """Construct the frozen one-in-five finding-audit protocol."""

    artifact = _validate_artifact(frozen_protocol_artifact, "frozenProtocolArtifact")
    _stable_id(protocol_version, _PROTOCOL_VERSION, "protocolVersion")
    residue = _integer(selection_residue, "selectionResidue")
    if residue >= SELECTION_MODULUS:
        raise FindingAuditError("selectionResidue must be below the selection modulus")
    protocol = {
        "kind": "finding-audit-protocol",
        "schemaVersion": SCHEMA_VERSION,
        "protocolVersion": protocol_version,
        "frozenProtocolArtifact": artifact,
        "selection": {
            "algorithm": SELECTION_ALGORITHM,
            "domainSeparator": SELECTION_DOMAIN,
            "modulus": SELECTION_MODULUS,
            "residue": residue,
        },
        "adjudication": {
            "requiredAdjudicators": 2,
            "minimumAgreement": MINIMUM_AGREEMENT,
            "agreementMetrics": list(AGREEMENT_METRICS),
        },
        "prospective": {
            "backfillAllowed": False,
            "assignmentMoment": "before-first-attempt",
            "retryPolicy": "inherit-first-family-assignment",
            "denominatorEffect": "none",
        },
    }
    validate_audit_protocol(protocol)
    return protocol


def validate_audit_protocol(protocol: Any) -> dict[str, Any]:
    """Strictly validate a protocol and return a detached canonical copy."""

    protocol = _exact_mapping(protocol, _PROTOCOL_KEYS, "protocol")
    if protocol["kind"] != "finding-audit-protocol":
        raise FindingAuditError("protocol.kind is invalid")
    if protocol["schemaVersion"] != SCHEMA_VERSION:
        raise FindingAuditError("protocol.schemaVersion is unsupported")
    _stable_id(protocol["protocolVersion"], _PROTOCOL_VERSION, "protocol.protocolVersion")
    _validate_artifact(protocol["frozenProtocolArtifact"], "protocol.frozenProtocolArtifact")

    selection = _exact_mapping(protocol["selection"], _SELECTION_KEYS, "protocol.selection")
    expected_selection = {
        "algorithm": SELECTION_ALGORITHM,
        "domainSeparator": SELECTION_DOMAIN,
        "modulus": SELECTION_MODULUS,
    }
    for key, expected in expected_selection.items():
        if selection[key] != expected:
            raise FindingAuditError(f"protocol.selection.{key} differs from the frozen rule")
    residue = _integer(selection["residue"], "protocol.selection.residue")
    if residue >= SELECTION_MODULUS:
        raise FindingAuditError("protocol.selection.residue is outside the modulus")

    adjudication = _exact_mapping(
        protocol["adjudication"], _ADJUDICATION_KEYS, "protocol.adjudication"
    )
    if adjudication["requiredAdjudicators"] != 2:
        raise FindingAuditError("protocol requires exactly two adjudicators")
    threshold = adjudication["minimumAgreement"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise FindingAuditError("protocol minimum agreement must be numeric")
    if not math.isfinite(float(threshold)) or float(threshold) != MINIMUM_AGREEMENT:
        raise FindingAuditError("protocol minimum agreement differs from the frozen rule")
    if adjudication["agreementMetrics"] != list(AGREEMENT_METRICS):
        raise FindingAuditError("protocol agreement metrics differ from the frozen rule")

    prospective = _exact_mapping(
        protocol["prospective"], _PROSPECTIVE_KEYS, "protocol.prospective"
    )
    frozen_prospective = {
        "backfillAllowed": False,
        "assignmentMoment": "before-first-attempt",
        "retryPolicy": "inherit-first-family-assignment",
        "denominatorEffect": "none",
    }
    if dict(prospective) != frozen_prospective:
        raise FindingAuditError("protocol prospective controls differ from the frozen rule")
    _canonical_json(protocol)
    return copy.deepcopy(dict(protocol))


def audit_protocol_sha256(protocol: Any) -> str:
    """Return the digest which binds assignments, packets, and certificates."""

    return _content_sha256(validate_audit_protocol(protocol))


def _selection_values(
    protocol: Mapping[str, Any], activation_id: str, decision_family_id: str
) -> tuple[str, int, bool]:
    protocol = validate_audit_protocol(protocol)
    activation_id = _stable_id(activation_id, _ACTIVATION_ID, "activationId")
    decision_family_id = _stable_id(
        decision_family_id, _FAMILY_ID, "decisionFamilyId"
    )
    payload = b"\x00".join(
        (
            protocol["selection"]["domainSeparator"].encode("ascii"),
            protocol["protocolVersion"].encode("ascii"),
            activation_id.encode("ascii"),
            decision_family_id.encode("ascii"),
        )
    )
    digest = _sha256(payload)
    bucket = int(digest, 16) % protocol["selection"]["modulus"]
    return digest, bucket, bucket == protocol["selection"]["residue"]


def deterministic_family_selection(
    protocol: Mapping[str, Any], *, activation_id: str, decision_family_id: str
) -> dict[str, Any]:
    """Pure selection primitive; persistence remains the caller's responsibility."""

    digest, bucket, selected = _selection_values(
        protocol, activation_id, decision_family_id
    )
    return {"selectionDigest": digest, "bucket": bucket, "selected": selected}


def validate_audit_assignment(
    assignment: Any, *, protocol: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate and recompute one persisted selected or non-selected assignment."""

    protocol = validate_audit_protocol(protocol)
    assignment = _exact_mapping(assignment, _ASSIGNMENT_KEYS, "assignment")
    if assignment["kind"] != "finding-audit-assignment":
        raise FindingAuditError("assignment.kind is invalid")
    if assignment["schemaVersion"] != SCHEMA_VERSION:
        raise FindingAuditError("assignment.schemaVersion is unsupported")
    activation_id = _stable_id(
        assignment["activationId"], _ACTIVATION_ID, "assignment.activationId"
    )
    family_id = _stable_id(
        assignment["decisionFamilyId"], _FAMILY_ID, "assignment.decisionFamilyId"
    )
    _stable_id(assignment["firstRunId"], _RUN_ID, "assignment.firstRunId")
    if assignment["protocolVersion"] != protocol["protocolVersion"]:
        raise FindingAuditError("assignment protocol version mismatch")
    if assignment["protocolSha256"] != audit_protocol_sha256(protocol):
        raise FindingAuditError("assignment protocol digest mismatch")
    _validated_canonical_timestamp(assignment["assignedAt"], "assignment.assignedAt")
    expected_digest, expected_bucket, expected_selected = _selection_values(
        protocol, activation_id, family_id
    )
    if assignment["selectionDigest"] != expected_digest:
        raise FindingAuditError("assignment selection digest mismatch")
    if assignment["bucket"] != expected_bucket:
        raise FindingAuditError("assignment bucket mismatch")
    if assignment["selected"] is not expected_selected:
        raise FindingAuditError("assignment selected value mismatch")
    expected_state = "selected" if expected_selected else "not-selected"
    if assignment["assignmentState"] != expected_state:
        raise FindingAuditError("assignment state does not persist the selection result")
    if assignment["provenance"] != "first-observation-prospective":
        raise FindingAuditError("assignment provenance is not prospective")
    return copy.deepcopy(dict(assignment))


def assign_decision_family(
    protocol: Mapping[str, Any],
    *,
    activation_id: str,
    decision_family_id: str,
    run_id: str,
    assigned_at: datetime | str,
    attempt_started_at: datetime | str,
    existing_assignments: Iterable[Mapping[str, Any]] = (),
    first_observation: bool,
) -> dict[str, Any]:
    """Return an inherited assignment or create one before the first attempt.

    A caller must append a newly returned record before appending the attempt.
    Passing ``first_observation=False`` without a prior record is a prohibited
    backfill and fails closed.
    """

    protocol = validate_audit_protocol(protocol)
    activation_id = _stable_id(activation_id, _ACTIVATION_ID, "activationId")
    family_id = _stable_id(decision_family_id, _FAMILY_ID, "decisionFamilyId")
    run_id = _stable_id(run_id, _RUN_ID, "runId")
    first_observation = _boolean(first_observation, "firstObservation")
    prior_matches: list[dict[str, Any]] = []
    for index, raw in enumerate(existing_assignments):
        prior = validate_audit_assignment(raw, protocol=protocol)
        if (
            prior["activationId"] == activation_id
            and prior["decisionFamilyId"] == family_id
        ):
            prior_matches.append(prior)
    if len(prior_matches) > 1:
        raise FindingAuditError("duplicate persisted assignments for decision family")
    if prior_matches:
        return prior_matches[0]
    if not first_observation:
        raise FindingAuditError("audit assignment backfill is prohibited")

    assigned = _canonical_timestamp(assigned_at, "assignedAt")
    attempt_started = _canonical_timestamp(attempt_started_at, "attemptStartedAt")
    assigned_dt = datetime.fromisoformat(assigned.replace("Z", "+00:00"))
    attempt_dt = datetime.fromisoformat(attempt_started.replace("Z", "+00:00"))
    if assigned_dt >= attempt_dt:
        raise FindingAuditError("assignment must be recorded before the first attempt")
    digest, bucket, selected = _selection_values(protocol, activation_id, family_id)
    assignment = {
        "kind": "finding-audit-assignment",
        "schemaVersion": SCHEMA_VERSION,
        "activationId": activation_id,
        "decisionFamilyId": family_id,
        "firstRunId": run_id,
        "protocolVersion": protocol["protocolVersion"],
        "protocolSha256": audit_protocol_sha256(protocol),
        "assignedAt": assigned,
        "selectionDigest": digest,
        "bucket": bucket,
        "selected": selected,
        "assignmentState": "selected" if selected else "not-selected",
        "provenance": "first-observation-prospective",
    }
    return validate_audit_assignment(assignment, protocol=protocol)


def _packet_case_id(
    protocol_sha256: str, selection_digest: str, case_run_id: str
) -> str:
    identity = _sha256(
        b"\x00".join(
            (
                b"ai-council/finding-audit-case/v1",
                protocol_sha256.encode("ascii"),
                selection_digest.encode("ascii"),
                case_run_id.encode("ascii"),
            )
        )
    )
    return f"audit-case-{identity[:32]}"


def _validate_source_subject(value: Any, field: str) -> dict[str, Any]:
    subject = _exact_mapping(value, _SOURCE_SUBJECT_KEYS, field)
    for key in ("seatId", "role", "modelId", "agentVersion"):
        _text(subject[key], f"{field}.{key}")
    source_output = _text(subject["sourceOutputText"], f"{field}.sourceOutputText")
    output_digest = _digest(
        subject["sourceOutputSha256"], f"{field}.sourceOutputSha256"
    )
    if _sha256(source_output.encode("utf-8")) != output_digest:
        raise FindingAuditError(f"{field}.sourceOutputSha256 does not bind sourceOutputText")
    decoded_output = _strict_json_object(source_output, f"{field}.sourceOutputText")
    answer = _text(subject["visibleAnswer"], f"{field}.visibleAnswer")
    if decoded_output.get("answer") != answer:
        raise FindingAuditError(
            f"{field}.visibleAnswer is not the exact answer in sourceOutputText"
        )
    _boolean(subject["selfIdentificationRisk"], f"{field}.selfIdentificationRisk")
    findings = _list(subject["capturedFindings"], f"{field}.capturedFindings")
    seen: set[str] = set()
    for index, raw_finding in enumerate(findings):
        finding_field = f"{field}.capturedFindings[{index}]"
        finding = _exact_mapping(raw_finding, _SOURCE_FINDING_KEYS, finding_field)
        finding_id = _stable_id(
            finding["findingId"], _FINDING_ID, f"{finding_field}.findingId"
        )
        if finding_id in seen:
            raise FindingAuditError(f"{field} has duplicate captured finding ids")
        seen.add(finding_id)
        if finding["seatId"] != subject["seatId"]:
            raise FindingAuditError(f"{finding_field}.seatId belongs to another subject")
        for key in (
            "category",
            "claim",
            "severity",
            "proposedAction",
            "evidenceSummary",
        ):
            _text(finding[key], f"{finding_field}.{key}")
    return copy.deepcopy(dict(subject))


def _case_alias(case_id: str, subject: Mapping[str, Any]) -> str:
    identity = _canonical_json(
        {
            "case": case_id,
            "seatId": subject["seatId"],
            "role": subject["role"],
            "modelId": subject["modelId"],
            "agentVersion": subject["agentVersion"],
            "sourceOutputSha256": subject["sourceOutputSha256"],
        }
    )
    alias_payload = b"ai-council/finding-audit-alias/v1" + bytes((0,)) + identity
    return f"subject-{_sha256(alias_payload)[:12]}"


def _validate_packet_finding(value: Any, field: str) -> dict[str, Any]:
    finding = _exact_mapping(value, _PACKET_FINDING_KEYS, field)
    _stable_id(finding["findingId"], _FINDING_ID, f"{field}.findingId")
    for key in ("category", "claim", "severity", "proposedAction", "evidenceSummary"):
        _text(finding[key], f"{field}.{key}")
    return copy.deepcopy(dict(finding))


def validate_audit_case_packet(
    packet: Any, *, protocol: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate a packet without requiring or revealing its separate alias map."""

    protocol = validate_audit_protocol(protocol)
    packet = _exact_mapping(packet, _PACKET_KEYS, "packet")
    if packet["kind"] != "finding-audit-case":
        raise FindingAuditError("packet.kind is invalid")
    if packet["schemaVersion"] != SCHEMA_VERSION:
        raise FindingAuditError("packet.schemaVersion is unsupported")
    if packet["protocolVersion"] != protocol["protocolVersion"]:
        raise FindingAuditError("packet protocol version mismatch")
    protocol_digest = audit_protocol_sha256(protocol)
    if packet["protocolSha256"] != protocol_digest:
        raise FindingAuditError("packet protocol digest mismatch")
    activation_id = _stable_id(
        packet["activationId"], _ACTIVATION_ID, "packet.activationId"
    )
    family_id = _stable_id(
        packet["decisionFamilyId"], _FAMILY_ID, "packet.decisionFamilyId"
    )
    case_run_id = _stable_id(packet["caseRunId"], _RUN_ID, "packet.caseRunId")
    selection_digest = _digest(
        packet["assignmentSelectionDigest"], "packet.assignmentSelectionDigest"
    )
    expected_selection_digest, _, selected = _selection_values(
        protocol, activation_id, family_id
    )
    if selection_digest != expected_selection_digest:
        raise FindingAuditError("packet assignment selection digest mismatch")
    if not selected:
        raise FindingAuditError("packet decision family was not selected for audit")
    expected_case_id = _packet_case_id(protocol_digest, selection_digest, case_run_id)
    case_id = _stable_id(packet["auditCaseId"], _CASE_ID, "packet.auditCaseId")
    if case_id != expected_case_id:
        raise FindingAuditError("packet audit case id mismatch")
    _validated_canonical_timestamp(packet["capturedAt"], "packet.capturedAt")
    if packet["anonymizationRisk"] not in {"reported", "not-reported"}:
        raise FindingAuditError("packet.anonymizationRisk is invalid")
    subjects = _list(packet["subjects"], "packet.subjects")
    if not subjects:
        raise FindingAuditError("packet.subjects must not be empty")
    aliases: set[str] = set()
    output_digests: set[str] = set()
    finding_ids: set[str] = set()
    risks: list[str] = []
    for index, raw_subject in enumerate(subjects):
        field = f"packet.subjects[{index}]"
        subject = _exact_mapping(raw_subject, _PACKET_SUBJECT_KEYS, field)
        alias = _stable_id(subject["subjectAlias"], _SUBJECT_ALIAS, f"{field}.subjectAlias")
        if alias in aliases:
            raise FindingAuditError("packet has duplicate subject aliases")
        aliases.add(alias)
        answer = _text(subject["visibleAnswer"], f"{field}.visibleAnswer")
        answer_digest = _digest(
            subject["visibleAnswerSha256"], f"{field}.visibleAnswerSha256"
        )
        if _sha256(answer.encode("utf-8")) != answer_digest:
            raise FindingAuditError(f"{field}.visibleAnswerSha256 does not bind visibleAnswer")
        output_digest = _digest(
            subject["sourceOutputSha256"], f"{field}.sourceOutputSha256"
        )
        if output_digest in output_digests:
            raise FindingAuditError("packet has duplicate source output digests")
        output_digests.add(output_digest)
        risk = subject["anonymizationRisk"]
        if risk not in {"reported", "not-reported"}:
            raise FindingAuditError(f"{field}.anonymizationRisk is invalid")
        risks.append(risk)
        for finding_index, raw_finding in enumerate(
            _list(subject["capturedFindings"], f"{field}.capturedFindings")
        ):
            finding = _validate_packet_finding(
                raw_finding, f"{field}.capturedFindings[{finding_index}]"
            )
            if finding["findingId"] in finding_ids:
                raise FindingAuditError("packet has duplicate captured finding ids")
            finding_ids.add(finding["findingId"])
    if subjects != sorted(subjects, key=lambda item: item["subjectAlias"]):
        raise FindingAuditError("packet subjects must use canonical alias order")
    expected_risk = "reported" if "reported" in risks else "not-reported"
    if packet["anonymizationRisk"] != expected_risk:
        raise FindingAuditError("packet aggregate anonymization risk mismatch")
    _canonical_json(packet)
    return copy.deepcopy(dict(packet))


def audit_case_sha256(packet: Any, *, protocol: Mapping[str, Any]) -> str:
    return _content_sha256(validate_audit_case_packet(packet, protocol=protocol))


def validate_alias_map(
    alias_map: Any,
    *,
    packet: Mapping[str, Any],
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the separately-custodied identity map against a blinded packet."""

    packet = validate_audit_case_packet(packet, protocol=protocol)
    alias_map = _exact_mapping(alias_map, _ALIAS_MAP_KEYS, "aliasMap")
    if alias_map["kind"] != "finding-audit-alias-map":
        raise FindingAuditError("aliasMap.kind is invalid")
    if alias_map["schemaVersion"] != SCHEMA_VERSION:
        raise FindingAuditError("aliasMap.schemaVersion is unsupported")
    if alias_map["auditCaseId"] != packet["auditCaseId"]:
        raise FindingAuditError("aliasMap audit case id mismatch")
    if alias_map["auditCaseSha256"] != audit_case_sha256(packet, protocol=protocol):
        raise FindingAuditError("aliasMap audit case digest mismatch")
    mappings = _list(alias_map["mappings"], "aliasMap.mappings")
    if len(mappings) != len(packet["subjects"]):
        raise FindingAuditError("aliasMap must map every packet subject exactly once")
    expected = {
        item["subjectAlias"]: item["sourceOutputSha256"] for item in packet["subjects"]
    }
    seen: set[str] = set()
    for index, raw_mapping in enumerate(mappings):
        field = f"aliasMap.mappings[{index}]"
        mapping = _exact_mapping(raw_mapping, _ALIAS_MAPPING_KEYS, field)
        alias = _stable_id(mapping["subjectAlias"], _SUBJECT_ALIAS, f"{field}.subjectAlias")
        if alias in seen or alias not in expected:
            raise FindingAuditError("aliasMap has duplicate or unknown subject alias")
        seen.add(alias)
        for key in ("seatId", "role", "modelId", "agentVersion"):
            _text(mapping[key], f"{field}.{key}")
        source_digest = _digest(
            mapping["sourceOutputSha256"], f"{field}.sourceOutputSha256"
        )
        if source_digest != expected[alias]:
            raise FindingAuditError("aliasMap source output digest mismatch")
        expected_alias = _case_alias(
            packet["auditCaseId"],
            {
                "seatId": mapping["seatId"],
                "role": mapping["role"],
                "modelId": mapping["modelId"],
                "agentVersion": mapping["agentVersion"],
                "sourceOutputSha256": source_digest,
            },
        )
        if alias != expected_alias:
            raise FindingAuditError("aliasMap opaque alias does not bind its identity mapping")
    if mappings != sorted(mappings, key=lambda item: item["subjectAlias"]):
        raise FindingAuditError("aliasMap mappings must use canonical alias order")
    return copy.deepcopy(dict(alias_map))


def build_blinded_audit_case(
    protocol: Mapping[str, Any],
    *,
    assignment: Mapping[str, Any],
    case_run_id: str,
    captured_at: datetime | str,
    subjects: Sequence[Mapping[str, Any]],
    first_capture_complete: bool,
    prior_case_packets: Iterable[Mapping[str, Any]] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a blinded case and its separate alias map for a selected family."""

    protocol = validate_audit_protocol(protocol)
    assignment = validate_audit_assignment(assignment, protocol=protocol)
    if not assignment["selected"]:
        raise FindingAuditError("a not-selected family cannot produce an audit case")
    if not _boolean(first_capture_complete, "firstCaptureComplete"):
        raise FindingAuditError("audit cases may only use the first capture-complete run")
    case_run_id = _stable_id(case_run_id, _RUN_ID, "caseRunId")
    captured_at_text = _canonical_timestamp(captured_at, "capturedAt")
    if datetime.fromisoformat(captured_at_text.replace("Z", "+00:00")) < datetime.fromisoformat(
        assignment["assignedAt"].replace("Z", "+00:00")
    ):
        raise FindingAuditError("audit case predates its family assignment")
    protocol_digest = audit_protocol_sha256(protocol)
    case_id = _packet_case_id(
        protocol_digest, assignment["selectionDigest"], case_run_id
    )
    for raw_prior in prior_case_packets:
        prior = validate_audit_case_packet(raw_prior, protocol=protocol)
        if (
            prior["activationId"] == assignment["activationId"]
            and prior["decisionFamilyId"] == assignment["decisionFamilyId"]
        ):
            raise FindingAuditError("audit family already has a case; backfill or replacement denied")

    if not isinstance(subjects, Sequence) or isinstance(subjects, (str, bytes)) or not subjects:
        raise FindingAuditError("subjects must be a non-empty sequence")
    validated_subjects = [
        _validate_source_subject(raw, f"subjects[{index}]")
        for index, raw in enumerate(subjects)
    ]
    seat_ids = [item["seatId"] for item in validated_subjects]
    if len(seat_ids) != len(set(seat_ids)):
        raise FindingAuditError("subjects have duplicate seatId values")

    packet_subjects: list[dict[str, Any]] = []
    mappings: list[dict[str, Any]] = []
    for subject in validated_subjects:
        alias = _case_alias(case_id, subject)
        identity_tokens = (
            subject["seatId"],
            subject["role"],
            subject["modelId"],
            subject["agentVersion"],
        )
        detected_risk = subject["selfIdentificationRisk"] or any(
            token.casefold() in subject["visibleAnswer"].casefold() for token in identity_tokens
        )
        risk = "reported" if detected_risk else "not-reported"
        packet_findings = [
            {key: finding[key] for key in _PACKET_FINDING_KEYS}
            for finding in subject["capturedFindings"]
        ]
        packet_subjects.append(
            {
                "subjectAlias": alias,
                "visibleAnswer": subject["visibleAnswer"],
                "visibleAnswerSha256": _sha256(subject["visibleAnswer"].encode("utf-8")),
                "sourceOutputSha256": subject["sourceOutputSha256"],
                "anonymizationRisk": risk,
                "capturedFindings": packet_findings,
            }
        )
        mappings.append(
            {
                "subjectAlias": alias,
                "seatId": subject["seatId"],
                "role": subject["role"],
                "modelId": subject["modelId"],
                "agentVersion": subject["agentVersion"],
                "sourceOutputSha256": subject["sourceOutputSha256"],
            }
        )
    packet_subjects.sort(key=lambda item: item["subjectAlias"])
    mappings.sort(key=lambda item: item["subjectAlias"])
    packet = {
        "kind": "finding-audit-case",
        "schemaVersion": SCHEMA_VERSION,
        "auditCaseId": case_id,
        "protocolVersion": protocol["protocolVersion"],
        "protocolSha256": protocol_digest,
        "activationId": assignment["activationId"],
        "decisionFamilyId": assignment["decisionFamilyId"],
        "caseRunId": case_run_id,
        "assignmentSelectionDigest": assignment["selectionDigest"],
        "capturedAt": captured_at_text,
        "anonymizationRisk": (
            "reported"
            if any(item["anonymizationRisk"] == "reported" for item in packet_subjects)
            else "not-reported"
        ),
        "subjects": packet_subjects,
    }
    packet = validate_audit_case_packet(packet, protocol=protocol)
    alias_map = {
        "kind": "finding-audit-alias-map",
        "schemaVersion": SCHEMA_VERSION,
        "auditCaseId": case_id,
        "auditCaseSha256": audit_case_sha256(packet, protocol=protocol),
        "mappings": mappings,
    }
    alias_map = validate_alias_map(alias_map, packet=packet, protocol=protocol)
    return packet, alias_map


def _packet_subjects_by_alias(packet: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {item["subjectAlias"]: item for item in packet["subjects"]}


def _claim_identity(claim: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        claim["subjectAlias"],
        claim["sourceOutputSha256"],
        claim["start"],
        claim["end"],
        claim["quotedSpanSha256"],
    )


def _claim_id(claim_without_id: Mapping[str, Any]) -> str:
    return f"audit-claim-{_content_sha256(claim_without_id)[:32]}"


def _result_id(result_without_id: Mapping[str, Any]) -> str:
    return f"audit-result-{_content_sha256(result_without_id)[:32]}"


def make_adjudicator_result(
    protocol: Mapping[str, Any],
    *,
    packet: Mapping[str, Any],
    adjudicator_identity_sha256: str,
    adjudicated_at: datetime | str,
    annotations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind adjudicator classifications to exact spans in a blinded packet."""

    protocol = validate_audit_protocol(protocol)
    packet = validate_audit_case_packet(packet, protocol=protocol)
    identity_digest = _digest(
        adjudicator_identity_sha256, "adjudicatorIdentitySha256"
    )
    adjudicated_at_text = _canonical_timestamp(adjudicated_at, "adjudicatedAt")
    if datetime.fromisoformat(adjudicated_at_text.replace("Z", "+00:00")) < datetime.fromisoformat(
        packet["capturedAt"].replace("Z", "+00:00")
    ):
        raise FindingAuditError("adjudication cannot precede audit-case capture")
    if not isinstance(annotations, Sequence) or isinstance(annotations, (str, bytes)):
        raise FindingAuditError("annotations must be a sequence")
    if not annotations:
        raise FindingAuditError("annotations must not be empty")
    subjects = _packet_subjects_by_alias(packet)
    claims: list[dict[str, Any]] = []
    seen_spans: set[tuple[Any, ...]] = set()
    for index, raw_annotation in enumerate(annotations):
        field = f"annotations[{index}]"
        annotation = _exact_mapping(raw_annotation, _ANNOTATION_KEYS, field)
        alias = _stable_id(
            annotation["subjectAlias"], _SUBJECT_ALIAS, f"{field}.subjectAlias"
        )
        if alias not in subjects:
            raise FindingAuditError(f"{field}.subjectAlias is not present in packet")
        subject = subjects[alias]
        start = _integer(annotation["start"], f"{field}.start")
        end = _integer(annotation["end"], f"{field}.end")
        answer = subject["visibleAnswer"]
        if end <= start or end > len(answer):
            raise FindingAuditError(f"{field} has an invalid or empty source span")
        quoted = answer[start:end]
        status = annotation["captureStatus"]
        if status not in {"captured", "omitted"}:
            raise FindingAuditError(f"{field}.captureStatus is invalid")
        finding_ids = _list(
            annotation["capturedFindingIds"], f"{field}.capturedFindingIds"
        )
        if len(finding_ids) != len(set(finding_ids)):
            raise FindingAuditError(f"{field}.capturedFindingIds has duplicates")
        available_ids = {item["findingId"] for item in subject["capturedFindings"]}
        for finding_index, finding_id in enumerate(finding_ids):
            _stable_id(
                finding_id,
                _FINDING_ID,
                f"{field}.capturedFindingIds[{finding_index}]",
            )
            if finding_id not in available_ids:
                raise FindingAuditError(f"{field} references a finding outside its subject")
        if status == "captured" and not finding_ids:
            raise FindingAuditError(f"{field} captured claims require a captured finding")
        if status == "omitted" and finding_ids:
            raise FindingAuditError(f"{field} omitted claims cannot reference captured findings")
        local_group = _stable_id(
            annotation["localGroupId"], _LOCAL_GROUP_ID, f"{field}.localGroupId"
        )
        base_claim = {
            "subjectAlias": alias,
            "sourceOutputSha256": subject["sourceOutputSha256"],
            "start": start,
            "end": end,
            "quotedText": quoted,
            "quotedSpanSha256": _sha256(quoted.encode("utf-8")),
            "captureStatus": status,
            "capturedFindingIds": list(finding_ids),
            "localGroupId": local_group,
            "material": _boolean(annotation["material"], f"{field}.material"),
            "actionable": _boolean(annotation["actionable"], f"{field}.actionable"),
            "confidentlyWrong": _boolean(
                annotation["confidentlyWrong"], f"{field}.confidentlyWrong"
            ),
        }
        span_identity = _claim_identity(base_claim)
        if span_identity in seen_spans:
            raise FindingAuditError("adjudicator result has duplicate source spans")
        seen_spans.add(span_identity)
        claims.append({"claimId": _claim_id(base_claim), **base_claim})
    claims.sort(key=lambda item: (item["subjectAlias"], item["start"], item["end"]))
    result_without_id = {
        "kind": "finding-audit-result",
        "schemaVersion": SCHEMA_VERSION,
        "auditCaseId": packet["auditCaseId"],
        "auditCaseSha256": audit_case_sha256(packet, protocol=protocol),
        "protocolVersion": protocol["protocolVersion"],
        "protocolSha256": audit_protocol_sha256(protocol),
        "adjudicatorIdentitySha256": identity_digest,
        "adjudicatedAt": adjudicated_at_text,
        "claims": claims,
    }
    result = {
        **result_without_id,
        "auditResultId": _result_id(result_without_id),
    }
    # Canonical order does not affect JSON hashing, but returning the identifier
    # beside the record's kind is friendlier to append-only event tooling.
    result = {
        "kind": result["kind"],
        "schemaVersion": result["schemaVersion"],
        "auditResultId": result["auditResultId"],
        **{key: value for key, value in result.items() if key not in {"kind", "schemaVersion", "auditResultId"}},
    }
    return validate_adjudicator_result(result, packet=packet, protocol=protocol)


def validate_adjudicator_result(
    result: Any,
    *,
    packet: Mapping[str, Any],
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    """Strictly revalidate source binding, semantics, and deterministic ids."""

    protocol = validate_audit_protocol(protocol)
    packet = validate_audit_case_packet(packet, protocol=protocol)
    result = _exact_mapping(result, _RESULT_KEYS, "result")
    if result["kind"] != "finding-audit-result":
        raise FindingAuditError("result.kind is invalid")
    if result["schemaVersion"] != SCHEMA_VERSION:
        raise FindingAuditError("result.schemaVersion is unsupported")
    result_id = _stable_id(result["auditResultId"], _RESULT_ID, "result.auditResultId")
    if result["auditCaseId"] != packet["auditCaseId"]:
        raise FindingAuditError("result audit case id mismatch")
    if result["auditCaseSha256"] != audit_case_sha256(packet, protocol=protocol):
        raise FindingAuditError("result audit case digest mismatch")
    if result["protocolVersion"] != protocol["protocolVersion"]:
        raise FindingAuditError("result protocol version mismatch")
    if result["protocolSha256"] != audit_protocol_sha256(protocol):
        raise FindingAuditError("result protocol digest mismatch")
    _digest(result["adjudicatorIdentitySha256"], "result.adjudicatorIdentitySha256")
    adjudicated_at = _validated_canonical_timestamp(
        result["adjudicatedAt"], "result.adjudicatedAt"
    )
    if datetime.fromisoformat(adjudicated_at.replace("Z", "+00:00")) < datetime.fromisoformat(
        packet["capturedAt"].replace("Z", "+00:00")
    ):
        raise FindingAuditError("adjudication cannot precede audit-case capture")
    claims = _list(result["claims"], "result.claims")
    if not claims:
        raise FindingAuditError("result.claims must not be empty")
    subjects = _packet_subjects_by_alias(packet)
    seen_ids: set[str] = set()
    seen_spans: set[tuple[Any, ...]] = set()
    for index, raw_claim in enumerate(claims):
        field = f"result.claims[{index}]"
        claim = _exact_mapping(raw_claim, _CLAIM_KEYS, field)
        claim_id = _stable_id(claim["claimId"], _CLAIM_ID, f"{field}.claimId")
        if claim_id in seen_ids:
            raise FindingAuditError("result has duplicate claim ids")
        seen_ids.add(claim_id)
        alias = _stable_id(claim["subjectAlias"], _SUBJECT_ALIAS, f"{field}.subjectAlias")
        if alias not in subjects:
            raise FindingAuditError(f"{field}.subjectAlias is not present in packet")
        subject = subjects[alias]
        if claim["sourceOutputSha256"] != subject["sourceOutputSha256"]:
            raise FindingAuditError(f"{field}.sourceOutputSha256 mismatch")
        start = _integer(claim["start"], f"{field}.start")
        end = _integer(claim["end"], f"{field}.end")
        answer = subject["visibleAnswer"]
        if end <= start or end > len(answer):
            raise FindingAuditError(f"{field} has an invalid or empty source span")
        quoted = answer[start:end]
        if claim["quotedText"] != quoted:
            raise FindingAuditError(f"{field}.quotedText does not match source span")
        if claim["quotedSpanSha256"] != _sha256(quoted.encode("utf-8")):
            raise FindingAuditError(f"{field}.quotedSpanSha256 mismatch")
        if claim["captureStatus"] not in {"captured", "omitted"}:
            raise FindingAuditError(f"{field}.captureStatus is invalid")
        finding_ids = _list(claim["capturedFindingIds"], f"{field}.capturedFindingIds")
        if len(finding_ids) != len(set(finding_ids)):
            raise FindingAuditError(f"{field}.capturedFindingIds has duplicates")
        available_ids = {item["findingId"] for item in subject["capturedFindings"]}
        for finding_id in finding_ids:
            _stable_id(finding_id, _FINDING_ID, f"{field}.capturedFindingIds item")
            if finding_id not in available_ids:
                raise FindingAuditError(f"{field} references a finding outside its subject")
        if claim["captureStatus"] == "captured" and not finding_ids:
            raise FindingAuditError(f"{field} captured claims require a captured finding")
        if claim["captureStatus"] == "omitted" and finding_ids:
            raise FindingAuditError(f"{field} omitted claims cannot reference captured findings")
        _stable_id(claim["localGroupId"], _LOCAL_GROUP_ID, f"{field}.localGroupId")
        for key in ("material", "actionable", "confidentlyWrong"):
            _boolean(claim[key], f"{field}.{key}")
        base = {key: claim[key] for key in _CLAIM_KEYS if key != "claimId"}
        if claim_id != _claim_id(base):
            raise FindingAuditError(f"{field}.claimId mismatch")
        span_identity = _claim_identity(claim)
        if span_identity in seen_spans:
            raise FindingAuditError("result has duplicate source spans")
        seen_spans.add(span_identity)
    if claims != sorted(claims, key=lambda item: (item["subjectAlias"], item["start"], item["end"])):
        raise FindingAuditError("result claims must use canonical source order")
    without_id = {key: result[key] for key in _RESULT_KEYS if key != "auditResultId"}
    if result_id != _result_id(without_id):
        raise FindingAuditError("result auditResultId mismatch")
    _canonical_json(result)
    return copy.deepcopy(dict(result))


def validate_adjudicator_pair(
    results: Sequence[Mapping[str, Any]],
    *,
    packet: Mapping[str, Any],
    protocol: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Require exactly two unique result records from distinct adjudicators."""

    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
        raise FindingAuditError("results must be a sequence")
    if len(results) != 2:
        raise FindingAuditError("exactly two adjudicator results are required")
    validated = [
        validate_adjudicator_result(item, packet=packet, protocol=protocol)
        for item in results
    ]
    identities = [item["adjudicatorIdentitySha256"] for item in validated]
    if len(set(identities)) != 2:
        raise FindingAuditError("adjudicators must have distinct identity digests")
    result_ids = [item["auditResultId"] for item in validated]
    if len(set(result_ids)) != 2:
        raise FindingAuditError("duplicate adjudicator result")
    validated.sort(key=lambda item: item["adjudicatorIdentitySha256"])
    return validated[0], validated[1]


def _metric(
    *,
    numerator: int,
    denominator: int,
    applicable: bool,
) -> dict[str, Any]:
    if not applicable:
        return {
            "applicable": False,
            "status": "not-applicable",
            "numerator": 0,
            "denominator": 0,
            "value": None,
        }
    if denominator == 0:
        return {
            "applicable": True,
            "status": "unmeasurable",
            "numerator": numerator,
            "denominator": denominator,
            "value": None,
        }
    return {
        "applicable": True,
        "status": "measured",
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator,
    }


def evaluate_adjudicator_agreement(
    protocol: Mapping[str, Any],
    *,
    packet: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compute separate agreement metrics and an explicit gate-void verdict."""

    protocol = validate_audit_protocol(protocol)
    packet = validate_audit_case_packet(packet, protocol=protocol)
    first, second = validate_adjudicator_pair(
        results, packet=packet, protocol=protocol
    )
    first_claims = {_claim_identity(item): item for item in first["claims"]}
    second_claims = {_claim_identity(item): item for item in second["claims"]}
    shared_keys = sorted(set(first_claims) & set(second_claims))

    classification_numerator = 0
    for key in shared_keys:
        left = first_claims[key]
        right = second_claims[key]
        left_class = (
            left["captureStatus"],
            left["material"],
            left["actionable"],
            left["confidentlyWrong"],
        )
        right_class = (
            right["captureStatus"],
            right["material"],
            right["actionable"],
            right["confidentlyWrong"],
        )
        classification_numerator += int(left_class == right_class)
    classification = _metric(
        numerator=classification_numerator,
        denominator=len(shared_keys),
        applicable=True,
    )

    grouping_numerator = 0
    grouping_denominator = 0
    for left_key, right_key in combinations(shared_keys, 2):
        grouping_denominator += 1
        first_same = (
            first_claims[left_key]["localGroupId"]
            == first_claims[right_key]["localGroupId"]
        )
        second_same = (
            second_claims[left_key]["localGroupId"]
            == second_claims[right_key]["localGroupId"]
        )
        grouping_numerator += int(first_same == second_same)
    grouping = _metric(
        numerator=grouping_numerator,
        denominator=grouping_denominator,
        applicable=True,
    )

    def omitted_spans(claims: Mapping[tuple[Any, ...], Mapping[str, Any]]) -> set[tuple[Any, ...]]:
        return {
            key
            for key, claim in claims.items()
            if claim["captureStatus"] == "omitted"
        }

    first_omitted = omitted_spans(first_claims)
    second_omitted = omitted_spans(second_claims)
    omitted_union = first_omitted | second_omitted
    omitted = _metric(
        numerator=len(first_omitted & second_omitted),
        denominator=len(omitted_union),
        applicable=bool(omitted_union),
    )
    metrics = {
        "classificationAgreement": classification,
        "pairwiseGroupingAgreement": grouping,
        "omittedSpanOverlap": omitted,
    }
    threshold = float(protocol["adjudication"]["minimumAgreement"])
    reasons: list[str] = []
    for name in AGREEMENT_METRICS:
        metric = metrics[name]
        if not metric["applicable"]:
            continue
        if metric["status"] == "unmeasurable":
            reasons.append(f"{name}:unmeasurable")
        elif metric["value"] < threshold:
            reasons.append(f"{name}:below-{threshold:.2f}")
    result_digests = sorted(_content_sha256(item) for item in (first, second))
    evaluation = {
        "kind": "finding-audit-agreement",
        "schemaVersion": SCHEMA_VERSION,
        "auditCaseId": packet["auditCaseId"],
        "auditCaseSha256": audit_case_sha256(packet, protocol=protocol),
        "protocolVersion": protocol["protocolVersion"],
        "protocolSha256": audit_protocol_sha256(protocol),
        "requiredAdjudicators": 2,
        "adjudicatorCount": 2,
        "adjudicatorResultSha256s": result_digests,
        "minimumAgreement": threshold,
        "metrics": metrics,
        "gateStatus": "void" if reasons else "eligible",
        "gateVoidReasons": reasons,
        "denominatorEffect": "none",
    }
    _canonical_json(evaluation)
    return evaluation


def _find_family_for_selection(
    protocol: Mapping[str, Any], activation_id: str, *, selected: bool
) -> str:
    for index in range(10_000):
        family = f"family-rehearsal-{index:04d}"
        if deterministic_family_selection(
            protocol, activation_id=activation_id, decision_family_id=family
        )["selected"] is selected:
            return family
    raise FindingAuditError("could not construct deterministic rehearsal family")


def _rehearsal_subject() -> dict[str, Any]:
    answer = "Captured concern. Omitted actionable repair. A related detail."
    source_output = _canonical_json(
        {"answer": answer, "capture": {"kind": "findings"}}
    ).decode("utf-8")
    return {
        "seatId": "rehearsal-seat",
        "role": "rehearsal-role",
        "modelId": "rehearsal-model",
        "agentVersion": "rehearsal-agent-v1",
        "sourceOutputText": source_output,
        "sourceOutputSha256": _sha256(source_output.encode("utf-8")),
        "visibleAnswer": answer,
        "selfIdentificationRisk": True,
        "capturedFindings": [
            {
                "findingId": "finding-11111111111111111111111111111111",
                "seatId": "rehearsal-seat",
                "category": "integrity",
                "claim": "Captured concern.",
                "severity": "block",
                "proposedAction": "Repair the capture.",
                "evidenceSummary": "The retained answer contains the concern.",
            }
        ],
    }


def _rehearsal_annotations(packet: Mapping[str, Any], *, disagree: bool = False) -> list[dict[str, Any]]:
    subject = packet["subjects"][0]
    answer = subject["visibleAnswer"]
    captured_text = "Captured concern."
    omitted_text = "Omitted actionable repair."
    related_text = "A related detail."
    captured_start = answer.index(captured_text)
    omitted_start = answer.index(omitted_text)
    related_start = answer.index(related_text)
    captured_group = "local-group-primary"
    related_group = "local-group-other" if disagree else captured_group
    return [
        {
            "subjectAlias": subject["subjectAlias"],
            "start": captured_start,
            "end": captured_start + len(captured_text),
            "captureStatus": "captured",
            "capturedFindingIds": ["finding-11111111111111111111111111111111"],
            "localGroupId": captured_group,
            "material": True,
            "actionable": True,
            "confidentlyWrong": disagree,
        },
        {
            "subjectAlias": subject["subjectAlias"],
            "start": omitted_start,
            "end": omitted_start + len(omitted_text),
            "captureStatus": "omitted",
            "capturedFindingIds": [],
            "localGroupId": "local-group-omitted",
            "material": True,
            "actionable": True,
            "confidentlyWrong": disagree,
        },
        {
            "subjectAlias": subject["subjectAlias"],
            "start": related_start,
            "end": related_start + len(related_text),
            "captureStatus": "omitted",
            "capturedFindingIds": [],
            "localGroupId": related_group,
            "material": False,
            "actionable": False,
            "confidentlyWrong": False,
        },
    ]


def _rehearsal_rejects(call: Any) -> bool:
    """Return true only when a deliberately invalid rehearsal record is rejected."""

    try:
        call()
    except FindingAuditError:
        return True
    return False


def _certificate_body_without_digest(certificate: Mapping[str, Any]) -> dict[str, Any]:
    return {key: certificate[key] for key in _CERTIFICATE_KEYS if key != "certificateSha256"}


def validate_protocol_rehearsal_certificate(
    certificate: Any, *, protocol: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate a source-bound rehearsal certificate without trusting its label."""

    protocol = validate_audit_protocol(protocol)
    certificate = _exact_mapping(certificate, _CERTIFICATE_KEYS, "certificate")
    if certificate["kind"] != "finding-audit-rehearsal-certificate":
        raise FindingAuditError("certificate.kind is invalid")
    if certificate["schemaVersion"] != SCHEMA_VERSION:
        raise FindingAuditError("certificate.schemaVersion is unsupported")
    if certificate["certificateVersion"] != CERTIFICATE_VERSION:
        raise FindingAuditError("certificate.certificateVersion is unsupported")
    runtime_commit = _text(certificate["runtimeCommit"], "certificate.runtimeCommit")
    if _COMMIT.fullmatch(runtime_commit) is None:
        raise FindingAuditError("certificate.runtimeCommit must be a Git object id")
    _digest(certificate["sourceTreeSha256"], "certificate.sourceTreeSha256")
    artifact = _validate_artifact(
        certificate["frozenProtocolArtifact"], "certificate.frozenProtocolArtifact"
    )
    if artifact != protocol["frozenProtocolArtifact"]:
        raise FindingAuditError("certificate frozen protocol artifact mismatch")
    if certificate["protocolVersion"] != protocol["protocolVersion"]:
        raise FindingAuditError("certificate protocol version mismatch")
    if certificate["protocolSha256"] != audit_protocol_sha256(protocol):
        raise FindingAuditError("certificate protocol digest mismatch")
    _validated_canonical_timestamp(certificate["rehearsedAt"], "certificate.rehearsedAt")
    checks = certificate["checks"]
    if not isinstance(checks, Mapping) or set(checks) != set(_REHEARSAL_CHECKS):
        raise FindingAuditError("certificate checks differ from the frozen rehearsal")
    if any(value is not True for value in checks.values()):
        raise FindingAuditError("certificate contains a failed rehearsal check")
    synthetic = certificate["syntheticRehearsalCounts"]
    expected_synthetic_keys = {"assignments", "selectedFamilies", "auditCases", "adjudicatorResults"}
    if not isinstance(synthetic, Mapping) or set(synthetic) != expected_synthetic_keys:
        raise FindingAuditError("certificate synthetic counts are invalid")
    for key, value in synthetic.items():
        _integer(value, f"certificate.syntheticRehearsalCounts.{key}")
    actual = certificate["actualProspectiveCounts"]
    if not isinstance(actual, Mapping) or set(actual) != expected_synthetic_keys:
        raise FindingAuditError("certificate actual prospective counts are invalid")
    if any(value != 0 for value in actual.values()):
        raise FindingAuditError("pre-activation actual prospective counts must remain zero")
    expected_digest = _content_sha256(_certificate_body_without_digest(certificate))
    if certificate["certificateSha256"] != expected_digest:
        raise FindingAuditError("certificate content digest mismatch")
    _canonical_json(certificate)
    return copy.deepcopy(dict(certificate))


def rehearse_audit_protocol(
    protocol: Mapping[str, Any],
    *,
    runtime_commit: str,
    source_tree_sha256: str,
    rehearsed_at: datetime | str,
) -> dict[str, Any]:
    """Exercise the frozen protocol and issue a source-bound certificate.

    All exercised records are in-memory synthetic fixtures.  The certificate's
    actual prospective counters therefore remain exactly zero.
    """

    protocol = validate_audit_protocol(protocol)
    if _COMMIT.fullmatch(_text(runtime_commit, "runtimeCommit")) is None:
        raise FindingAuditError("runtimeCommit must be a Git object id")
    source_tree_sha256 = _digest(source_tree_sha256, "sourceTreeSha256")
    rehearsed_at_text = _canonical_timestamp(rehearsed_at, "rehearsedAt")
    activation_id = "activation-11111111111111111111111111111111"
    first_run = "run-11111111111111111111111111111111"
    retry_run = "run-22222222222222222222222222222222"
    selected_family = _find_family_for_selection(protocol, activation_id, selected=True)
    unselected_family = _find_family_for_selection(protocol, activation_id, selected=False)
    assigned_at = "2026-01-01T00:00:00.000000Z"
    attempt_at = "2026-01-01T00:00:01.000000Z"
    captured_at = "2026-01-01T00:00:02.000000Z"

    selected_assignment = assign_decision_family(
        protocol,
        activation_id=activation_id,
        decision_family_id=selected_family,
        run_id=first_run,
        assigned_at=assigned_at,
        attempt_started_at=attempt_at,
        existing_assignments=(),
        first_observation=True,
    )
    repeated_selection = deterministic_family_selection(
        protocol, activation_id=activation_id, decision_family_id=selected_family
    )
    deterministic_ok = repeated_selection == {
        "selectionDigest": selected_assignment["selectionDigest"],
        "bucket": selected_assignment["bucket"],
        "selected": selected_assignment["selected"],
    }
    unselected_assignment = assign_decision_family(
        protocol,
        activation_id=activation_id,
        decision_family_id=unselected_family,
        run_id=first_run,
        assigned_at=assigned_at,
        attempt_started_at=attempt_at,
        existing_assignments=(),
        first_observation=True,
    )
    inherited = assign_decision_family(
        protocol,
        activation_id=activation_id,
        decision_family_id=unselected_family,
        run_id=retry_run,
        assigned_at="2026-01-01T00:00:03.000000Z",
        attempt_started_at="2026-01-01T00:00:04.000000Z",
        existing_assignments=[unselected_assignment],
        first_observation=False,
    )
    no_backfill = _rehearsal_rejects(
        lambda: assign_decision_family(
            protocol,
            activation_id=activation_id,
            decision_family_id="family-rehearsal-unobserved",
            run_id=retry_run,
            assigned_at="2026-01-01T00:00:03.000000Z",
            attempt_started_at="2026-01-01T00:00:04.000000Z",
            existing_assignments=(),
            first_observation=False,
        )
    )
    packet, alias_map = build_blinded_audit_case(
        protocol,
        assignment=selected_assignment,
        case_run_id=first_run,
        captured_at=captured_at,
        subjects=[_rehearsal_subject()],
        first_capture_complete=True,
    )
    packet_encoded = _canonical_json(packet)
    alias_encoded = _canonical_json(alias_map)
    structural_values = (
        b"rehearsal-seat",
        b"rehearsal-role",
        b"rehearsal-model",
        b"rehearsal-agent-v1",
    )
    packet_blind = all(value not in packet_encoded for value in structural_values) and all(
        value in alias_encoded for value in structural_values
    )
    tampered_source = _rehearsal_subject()
    tampered_source["sourceOutputSha256"] = "f" * 64
    source_output_digest_bound = _rehearsal_rejects(
        lambda: build_blinded_audit_case(
            protocol,
            assignment=selected_assignment,
            case_run_id=first_run,
            captured_at=captured_at,
            subjects=[tampered_source],
            first_capture_complete=True,
        )
    )
    first_result = make_adjudicator_result(
        protocol,
        packet=packet,
        adjudicator_identity_sha256="a" * 64,
        adjudicated_at="2026-01-01T00:00:03.000000Z",
        annotations=_rehearsal_annotations(packet),
    )
    second_result = make_adjudicator_result(
        protocol,
        packet=packet,
        adjudicator_identity_sha256="b" * 64,
        adjudicated_at="2026-01-01T00:00:04.000000Z",
        annotations=_rehearsal_annotations(packet),
    )
    agreement = evaluate_adjudicator_agreement(
        protocol, packet=packet, results=[first_result, second_result]
    )
    tampered_result = copy.deepcopy(first_result)
    tampered_result["claims"][0]["quotedSpanSha256"] = "f" * 64
    quoted_span_digest_bound = _rehearsal_rejects(
        lambda: validate_adjudicator_result(
            tampered_result, packet=packet, protocol=protocol
        )
    )
    disagreeing_result = make_adjudicator_result(
        protocol,
        packet=packet,
        adjudicator_identity_sha256="c" * 64,
        adjudicated_at="2026-01-01T00:00:05.000000Z",
        annotations=_rehearsal_annotations(packet, disagree=True),
    )
    void_evaluation = evaluate_adjudicator_agreement(
        protocol, packet=packet, results=[first_result, disagreeing_result]
    )
    single_annotation = _rehearsal_annotations(packet)[:1]
    short_first = make_adjudicator_result(
        protocol,
        packet=packet,
        adjudicator_identity_sha256="d" * 64,
        adjudicated_at="2026-01-01T00:00:06.000000Z",
        annotations=single_annotation,
    )
    short_second = make_adjudicator_result(
        protocol,
        packet=packet,
        adjudicator_identity_sha256="e" * 64,
        adjudicated_at="2026-01-01T00:00:07.000000Z",
        annotations=single_annotation,
    )
    unmeasurable_evaluation = evaluate_adjudicator_agreement(
        protocol, packet=packet, results=[short_first, short_second]
    )
    omitted_detected = any(
        claim["captureStatus"] == "omitted" and claim["actionable"]
        for claim in first_result["claims"]
    )
    checks = {
        "deterministicSelection": deterministic_ok,
        "assignedBeforeAttempt": (
            datetime.fromisoformat(selected_assignment["assignedAt"].replace("Z", "+00:00"))
            < datetime.fromisoformat(attempt_at.replace("Z", "+00:00"))
        ),
        "persistedNonSelection": (
            unselected_assignment["assignmentState"] == "not-selected"
            and unselected_assignment["selected"] is False
        ),
        "retryInheritance": inherited == unselected_assignment,
        "noBackfill": no_backfill,
        "packetBlinding": packet_blind,
        "aliasMapExcluded": "mappings" not in packet and "seatId" not in packet_encoded.decode("utf-8"),
        "selfIdentificationRiskReported": packet["anonymizationRisk"] == "reported",
        "sourceOutputDigestBound": source_output_digest_bound,
        "quotedSpanDigestBound": quoted_span_digest_bound,
        "omittedClaimDetection": omitted_detected,
        "localRegroupingRecorded": (
            all(claim["localGroupId"].startswith("local-group-") for claim in first_result["claims"])
            and agreement["metrics"]["pairwiseGroupingAgreement"]["status"] == "measured"
        ),
        "materialActionableClassified": all(
            isinstance(claim["material"], bool) and isinstance(claim["actionable"], bool)
            for claim in first_result["claims"]
        ),
        "confidentlyWrongNonScalar": (
            all(isinstance(claim["confidentlyWrong"], bool) for claim in first_result["claims"])
            and any(claim["confidentlyWrong"] for claim in disagreeing_result["claims"])
            and "confidentlyWrong" not in agreement["metrics"]
        ),
        "twoAdjudicatorSupport": (
            agreement["adjudicatorCount"] == 2
            and agreement["gateStatus"] == "eligible"
        ),
        "separateAgreementMetrics": set(agreement["metrics"]) == set(AGREEMENT_METRICS),
        "unmeasurableGateVoid": (
            unmeasurable_evaluation["gateStatus"] == "void"
            and any(reason.endswith(":unmeasurable") for reason in unmeasurable_evaluation["gateVoidReasons"])
        ),
        "belowThresholdGateVoid": (
            void_evaluation["gateStatus"] == "void"
            and any(":below-0.60" in reason for reason in void_evaluation["gateVoidReasons"])
        ),
        "prospectiveCountsRemainZero": True,
    }
    if any(value is not True for value in checks.values()):
        failed = sorted(key for key, value in checks.items() if not value)
        raise FindingAuditError(f"protocol rehearsal failed checks: {failed}")
    certificate_without_digest = {
        "kind": "finding-audit-rehearsal-certificate",
        "schemaVersion": SCHEMA_VERSION,
        "certificateVersion": CERTIFICATE_VERSION,
        "runtimeCommit": runtime_commit,
        "sourceTreeSha256": source_tree_sha256,
        "frozenProtocolArtifact": copy.deepcopy(protocol["frozenProtocolArtifact"]),
        "protocolVersion": protocol["protocolVersion"],
        "protocolSha256": audit_protocol_sha256(protocol),
        "rehearsedAt": rehearsed_at_text,
        "checks": checks,
        "syntheticRehearsalCounts": {
            "assignments": 2,
            "selectedFamilies": 1,
            "auditCases": 1,
            "adjudicatorResults": 5,
        },
        "actualProspectiveCounts": {
            "assignments": 0,
            "selectedFamilies": 0,
            "auditCases": 0,
            "adjudicatorResults": 0,
        },
    }
    certificate = {
        **certificate_without_digest,
        "certificateSha256": _content_sha256(certificate_without_digest),
    }
    return validate_protocol_rehearsal_certificate(certificate, protocol=protocol)


__all__ = [
    "AGREEMENT_METRICS",
    "CERTIFICATE_VERSION",
    "DEFAULT_SELECTION_RESIDUE",
    "FindingAuditError",
    "MINIMUM_AGREEMENT",
    "PROTOCOL_VERSION",
    "SCHEMA_VERSION",
    "SELECTION_DOMAIN",
    "SELECTION_MODULUS",
    "assign_decision_family",
    "audit_case_sha256",
    "audit_protocol_sha256",
    "build_blinded_audit_case",
    "deterministic_family_selection",
    "evaluate_adjudicator_agreement",
    "make_adjudicator_result",
    "make_audit_protocol",
    "rehearse_audit_protocol",
    "validate_adjudicator_pair",
    "validate_adjudicator_result",
    "validate_alias_map",
    "validate_audit_assignment",
    "validate_audit_case_packet",
    "validate_audit_protocol",
    "validate_protocol_rehearsal_certificate",
]
