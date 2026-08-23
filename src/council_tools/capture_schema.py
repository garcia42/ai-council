"""Strict schema and append-order validation for prospective council V2 capture.

This module is deliberately independent of :mod:`council_tools.forecasts`.  It
does not dispatch, normalize, or reinterpret V1 rows.  Callers are expected to
construct and append a row while holding their ledger lock; the constructors
accept a clock so the four lifecycle boundaries are created inside that lock.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Any

SCHEMA_VERSION = 2
V1_SCHEMA_VERSION = 1

V2_KINDS = (
    "capture-activation",
    "capture-initiation",
    "council-attempt-v2",
    "council-seats-finished",
    "council-v2",
    "capture-invalidation",
    "finding-audit-case-v2",
)
V1_KINDS = {
    "council-attempt",
    "council",
    "outcome-resolution",
    "grading-debt-override",
}

SEAT_STATES = {"submitted", "abstained", "unavailable"}
SEAT_ROLES = {"voting", "shadow", "control"}
OUTCOME_CLASSES = {"exogenous", "intervention-sensitive"}
INVALIDATION_REASONS = {
    "artifact-compromised",
    "disposition-error",
    "identity-error",
    "secret-detected",
    "timing-invalid",
}

_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_HEX_40_OR_64 = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FAMILY_ID = re.compile(r"^family-[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_ARTIFACT_PATH = re.compile(
    r"^sha256/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})\.bin$"
)
_INPUT_MANIFEST_BINDING = re.compile(
    r"(?:^|;)inputManifestSha256=([0-9a-f]{64})(?=;|$)"
)
_FORECAST_REQUEST_BEGIN = "-----BEGIN COUNCIL FORECAST REQUEST V1-----"
_FORECAST_REQUEST_END = "-----END COUNCIL FORECAST REQUEST V1-----"
_FINDING_ID = re.compile(r"^finding-[0-9a-f]{32}$")
_FINDING_GROUP_ID = re.compile(r"^finding-group-[0-9a-f]{32}$")
_ID_PREFIXES = {"activation", "initiation", "run", "outcome", "prediction", "invalidation"}
# Keep this module standalone-loadable for the copied-runtime rehearsal.  These
# patterns intentionally mirror ``artifacts.secret_detectors``; schema tests
# enforce parity over every detector family and caller-supplied tokens.
_RAW_PAYLOAD_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "private-key",
        re.compile(
            rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
        ),
    ),
    (
        "github-token",
        re.compile(
            rb"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36,255}|"
            rb"github_pat_[A-Za-z0-9_]{22,255})(?![A-Za-z0-9_])"
        ),
    ),
    (
        "openai-api-key",
        re.compile(
            rb"(?<![A-Za-z0-9_-])sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}"
            rb"(?![A-Za-z0-9_-])"
        ),
    ),
    (
        "slack-token",
        re.compile(
            rb"(?<![A-Za-z0-9-])xox[baprs]-[A-Za-z0-9-]{20,}"
            rb"(?![A-Za-z0-9-])"
        ),
    ),
    (
        "aws-secret-assignment",
        re.compile(
            rb"(?i)(?:"
            rb"(?:aws_)?secret(?:_access)?_key\s*[:=]\s*"
            rb"[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])"
            rb"|(?P<aws_json_escape>\\?)\""
            rb"(?:aws_)?secret(?:_access)?_key(?P=aws_json_escape)\"\s*:\s*"
            rb"(?P=aws_json_escape)\"[A-Za-z0-9/+=]{40}"
            rb"(?P=aws_json_escape)\""
            rb")"
        ),
    ),
)
_SYSTEM_TIME_KEYS = {
    "activatedAt",
    "handlingStartedAt",
    "seatsLaunchedAt",
    "seatsFinishedAt",
    "finalizedAt",
    "invalidatedAt",
    "ts",
}
_AUDIT_PROTOCOL_KEYS = {
    "kind",
    "schemaVersion",
    "protocolVersion",
    "frozenProtocolArtifact",
    "selection",
    "adjudication",
    "prospective",
}
_AUDIT_ASSIGNMENT_KEYS = {
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
_AUDIT_SELECTION_DOMAIN = "ai-council/finding-audit-assignment/v1"


class CaptureSchemaError(ValueError):
    """A V2 capture row violates its exact schema or lifecycle contract."""


Clock = Callable[[], datetime | str]
IdFactory = Callable[[str], str]


def _coerce_secret_scan_bytes(data: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("secret scan data must be bytes-like")
    return bytes(data)


def _secret_detector_labels(
    data: bytes | bytearray | memoryview,
    *,
    secret_tokens: Iterable[bytes | bytearray | memoryview] = (),
) -> tuple[str, ...]:
    content = _coerce_secret_scan_bytes(data)
    found = [
        name
        for name, pattern in _RAW_PAYLOAD_SECRET_PATTERNS
        if pattern.search(content)
    ]
    caller_match = False
    for token in secret_tokens:
        candidate = _coerce_secret_scan_bytes(token)
        if not candidate:
            raise ValueError("secret tokens must be non-empty")
        if candidate in content:
            caller_match = True
    if caller_match:
        found.append("caller-token")
    return tuple(dict.fromkeys(found))


def raw_payload_secret_detectors(
    payload: Any,
    *,
    secret_tokens: Iterable[bytes | bytearray | memoryview] = (),
) -> tuple[str, ...]:
    """Scan serialized request data before value-bearing schema construction.

    This deliberately returns detector names rather than raising an exception:
    the integrated runtime owns the lifecycle-aware rejection and, when a run
    has already been initiated, its fixed ``secret-detected`` invalidation.
    Serialization failures are normalized so neither a value nor its repr can
    enter a public exception before the typed schema has rejected the payload.
    """

    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=True,
        ).encode("utf-8")
    except (OverflowError, TypeError, ValueError, RecursionError) as exc:
        raise CaptureSchemaError("raw payload must be JSON-serializable") from exc
    return _secret_detector_labels(encoded, secret_tokens=secret_tokens)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            # Keys are caller-controlled and can themselves contain secrets.
            # Keep this category fixed at every boundary that uses the strict
            # decoder rather than reflecting the offending key.
            raise CaptureSchemaError("invalid JSON: duplicate key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise CaptureSchemaError("invalid JSON: non-finite number")


def _reject_nonfinite_numbers(value: Any) -> None:
    """Reject overflowed finite-looking JSON numbers as well as named constants."""

    if isinstance(value, float) and not math.isfinite(value):
        raise CaptureSchemaError("invalid JSON: non-finite number")
    if isinstance(value, Mapping):
        for nested in value.values():
            _reject_nonfinite_numbers(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_nonfinite_numbers(nested)


def strict_json_loads(data: str | bytes | bytearray) -> Any:
    """Decode JSON without Python's duplicate-key or non-finite extensions."""

    if isinstance(data, (bytes, bytearray)):
        try:
            data = bytes(data).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CaptureSchemaError("JSON must be UTF-8") from exc
    if not isinstance(data, str):
        raise CaptureSchemaError("JSON input must be text or bytes")
    try:
        value = json.loads(
            data,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
        _reject_nonfinite_numbers(value)
    except json.JSONDecodeError as exc:
        # Decoder messages contain caller-derived line fragments and offsets.
        raise CaptureSchemaError("invalid JSON") from exc
    except RecursionError as exc:
        # A syntactically valid but excessively nested document is simply an
        # invalid input.  Never allow the interpreter recursion failure to
        # cross a retained-artifact, ledger, spec, or CLI boundary.
        raise CaptureSchemaError("invalid JSON") from exc
    return value


def new_v2_id(prefix: str) -> str:
    """Return a random stable V2 identifier with the requested typed prefix."""

    prefix = _require_text(prefix, "V2 id prefix")
    if prefix not in _ID_PREFIXES:
        raise CaptureSchemaError("unknown V2 id prefix")
    return f"{prefix}-{uuid.uuid4().hex}"


def _derived_id(prefix: str, *parts: str) -> str:
    prefix = _require_text(prefix, "V2 id prefix")
    if prefix not in _ID_PREFIXES:
        raise CaptureSchemaError("unknown V2 id prefix")
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CaptureSchemaError(f"{field} must be an object")
    return value


def _exact_keys(
    value: Any,
    *,
    required: set[str],
    optional: set[str] | None = None,
    field: str,
) -> Mapping[str, Any]:
    obj = _require_mapping(value, field)
    optional = optional or set()
    keys = set(obj)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise CaptureSchemaError(f"{field} is missing keys: {sorted(missing)}")
    if unknown:
        # Unknown keys are caller-controlled and can themselves be credentials.
        raise CaptureSchemaError(f"{field} has unknown keys")
    return obj


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CaptureSchemaError(f"{field} must be non-empty text")
    return value


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise CaptureSchemaError(f"{field} must be boolean")
    return value


def _require_nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CaptureSchemaError(f"{field} must be a non-negative integer")
    return value


def _require_nonnegative_number(value: Any, field: str) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise CaptureSchemaError(f"{field} must be a non-negative number")
    return value


def _require_id(value: Any, prefix: str, field: str) -> str:
    value = _require_text(value, field)
    expected = f"{prefix}-"
    if not value.startswith(expected) or not _HEX_32.fullmatch(value[len(expected) :]):
        raise CaptureSchemaError(f"{field} must be a {prefix} UUID id")
    return value


def _require_digest(value: Any, field: str) -> str:
    value = _require_text(value, field)
    if not _SHA256.fullmatch(value):
        raise CaptureSchemaError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _require_commit(value: Any, field: str) -> str:
    value = _require_text(value, field)
    if not _HEX_40_OR_64.fullmatch(value):
        raise CaptureSchemaError(f"{field} must be a lowercase 40- or 64-hex object id")
    return value


def _parse_timestamp(value: Any, field: str) -> datetime:
    value = _require_text(value, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CaptureSchemaError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise CaptureSchemaError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _parse_date(value: Any, field: str) -> date:
    value = _require_text(value, field)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CaptureSchemaError(f"{field} must be YYYY-MM-DD") from exc


def _clock_timestamp(clock: Clock, field: str) -> str:
    value = clock()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise CaptureSchemaError("caller-supplied clock must return a timezone-aware value")
        parsed = value.astimezone(timezone.utc)
    else:
        parsed = _parse_timestamp(value, field)
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _require_payload_keys(
    payload: Mapping[str, Any], *, required: set[str], optional: set[str] | None, kind: str
) -> Mapping[str, Any]:
    raw = _require_mapping(payload, f"{kind} payload")
    injected = set(raw) & _SYSTEM_TIME_KEYS
    if injected:
        raise CaptureSchemaError(
            f"{kind} payload cannot inject system-generated timestamps: {sorted(injected)}"
        )
    obj = _exact_keys(raw, required=required, optional=optional, field=f"{kind} payload")
    return obj


def _validate_artifact_ref(value: Any, field: str) -> None:
    ref = _exact_keys(
        value,
        required={"path", "sha256", "bytes"},
        field=field,
    )
    path = _require_text(ref["path"], f"{field}.path")
    digest = _require_digest(ref["sha256"], f"{field}.sha256")
    _require_nonnegative_int(ref["bytes"], f"{field}.bytes")
    match = _ARTIFACT_PATH.fullmatch(path)
    if match is None:
        raise CaptureSchemaError(f"{field}.path must be a canonical content-addressed path")
    first, second, path_digest = match.groups()
    if path_digest != digest or first != digest[:2] or second != digest[2:4]:
        raise CaptureSchemaError(f"{field}.path must agree with {field}.sha256")


def _audit_protocol_digest(value: Any) -> str:
    protocol = _exact_keys(
        value, required=_AUDIT_PROTOCOL_KEYS, field="auditProtocol"
    )
    if protocol["kind"] != "finding-audit-protocol" or protocol["schemaVersion"] != 1:
        raise CaptureSchemaError("auditProtocol header is invalid")
    if protocol["protocolVersion"] != "finding-audit-v1":
        raise CaptureSchemaError("auditProtocol version is invalid")
    _validate_artifact_ref(
        protocol["frozenProtocolArtifact"], "auditProtocol.frozenProtocolArtifact"
    )
    selection = _exact_keys(
        protocol["selection"],
        required={"algorithm", "domainSeparator", "modulus", "residue"},
        field="auditProtocol.selection",
    )
    if selection != {
        "algorithm": "sha256-domain-v1",
        "domainSeparator": _AUDIT_SELECTION_DOMAIN,
        "modulus": 5,
        "residue": selection["residue"],
    }:
        raise CaptureSchemaError("auditProtocol selection rule is invalid")
    residue = _require_nonnegative_int(
        selection["residue"], "auditProtocol.selection.residue"
    )
    if residue >= 5:
        raise CaptureSchemaError("auditProtocol selection residue is invalid")
    adjudication = _exact_keys(
        protocol["adjudication"],
        required={"requiredAdjudicators", "minimumAgreement", "agreementMetrics"},
        field="auditProtocol.adjudication",
    )
    if (
        adjudication["requiredAdjudicators"] != 2
        or adjudication["minimumAgreement"] != 0.60
        or adjudication["agreementMetrics"]
        != [
            "classificationAgreement",
            "pairwiseGroupingAgreement",
            "omittedSpanOverlap",
        ]
    ):
        raise CaptureSchemaError("auditProtocol adjudication rule is invalid")
    prospective = _exact_keys(
        protocol["prospective"],
        required={
            "backfillAllowed",
            "assignmentMoment",
            "retryPolicy",
            "denominatorEffect",
        },
        field="auditProtocol.prospective",
    )
    if prospective != {
        "backfillAllowed": False,
        "assignmentMoment": "before-first-attempt",
        "retryPolicy": "inherit-first-family-assignment",
        "denominatorEffect": "none",
    }:
        raise CaptureSchemaError("auditProtocol prospective rule is invalid")
    return hashlib.sha256(
        json.dumps(
            protocol,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _validate_audit_assignment_record(
    value: Any, *, protocol: Mapping[str, Any]
) -> dict[str, Any]:
    assignment = _exact_keys(
        value, required=_AUDIT_ASSIGNMENT_KEYS, field="auditAssignment"
    )
    if assignment["kind"] != "finding-audit-assignment" or assignment["schemaVersion"] != 1:
        raise CaptureSchemaError("auditAssignment header is invalid")
    activation_id = _require_id(
        assignment["activationId"], "activation", "auditAssignment.activationId"
    )
    family_id = _require_text(
        assignment["decisionFamilyId"], "auditAssignment.decisionFamilyId"
    )
    if not _FAMILY_ID.fullmatch(family_id):
        raise CaptureSchemaError("auditAssignment decision family is invalid")
    _require_id(assignment["firstRunId"], "run", "auditAssignment.firstRunId")
    if assignment["protocolVersion"] != protocol["protocolVersion"]:
        raise CaptureSchemaError("auditAssignment protocol version mismatch")
    protocol_sha = _audit_protocol_digest(protocol)
    if assignment["protocolSha256"] != protocol_sha:
        raise CaptureSchemaError("auditAssignment protocol digest mismatch")
    _parse_timestamp(assignment["assignedAt"], "auditAssignment.assignedAt")
    selection_payload = b"\x00".join(
        (
            _AUDIT_SELECTION_DOMAIN.encode("ascii"),
            protocol["protocolVersion"].encode("ascii"),
            activation_id.encode("ascii"),
            family_id.encode("ascii"),
        )
    )
    selection_digest = hashlib.sha256(selection_payload).hexdigest()
    bucket = int(selection_digest, 16) % 5
    selected = bucket == protocol["selection"]["residue"]
    if (
        assignment["selectionDigest"] != selection_digest
        or assignment["bucket"] != bucket
        or assignment["selected"] is not selected
        or assignment["assignmentState"]
        != ("selected" if selected else "not-selected")
        or assignment["provenance"] != "first-observation-prospective"
    ):
        raise CaptureSchemaError("auditAssignment selection proof is invalid")
    return deepcopy(dict(assignment))


def seat_input_manifest_sha256(
    references: Mapping[str, Mapping[str, Any]],
) -> str:
    """Hash the exact persisted per-seat input-artifact mapping."""

    raw = _require_mapping(references, "seat input artifact references")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_seat, raw_ref in raw.items():
        seat = _require_text(raw_seat, "seat input artifact seatId")
        if seat in normalized:
            raise CaptureSchemaError("duplicate seat input artifact reference")
        _validate_artifact_ref(raw_ref, "seat input artifact reference")
        normalized[seat] = deepcopy(dict(raw_ref))
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _input_manifest_binding(decision_link: str) -> str:
    matches = _INPUT_MANIFEST_BINDING.findall(decision_link)
    if len(matches) != 1:
        raise CaptureSchemaError(
            "sharedOutcome.decisionLink must contain exactly one "
            "inputManifestSha256 binding"
        )
    return matches[0]


def _validate_decision_artifact(value: Any) -> None:
    artifact = _exact_keys(
        value,
        required={"path", "sha256", "bytes", "gitBlob"},
        field="decisionBeforeArtifact",
    )
    _validate_artifact_ref(
        {key: artifact[key] for key in ("path", "sha256", "bytes")},
        "decisionBeforeArtifact",
    )
    _require_commit(artifact["gitBlob"], "decisionBeforeArtifact.gitBlob")


def _validate_seat_plan(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not value:
        raise CaptureSchemaError("seatPlan must be a non-empty list")
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        field = f"seatPlan[{index}]"
        seat = _exact_keys(
            raw,
            required={"seatId", "role", "agentVersion", "agentDefinitionDigest"},
            field=field,
        )
        seat_id = _require_text(seat["seatId"], f"{field}.seatId")
        if seat_id in seen:
            raise CaptureSchemaError("seatPlan contains duplicate seatId")
        seen.add(seat_id)
        role = _require_text(seat["role"], f"{field}.role")
        if role not in SEAT_ROLES:
            raise CaptureSchemaError(f"{field}.role must be one of {sorted(SEAT_ROLES)}")
        _require_text(seat["agentVersion"], f"{field}.agentVersion")
        _require_digest(seat["agentDefinitionDigest"], f"{field}.agentDefinitionDigest")
        result.append(seat)
    return result


def outcome_fingerprint_v2(
    claim: str, resolution_date: str, resolved_by: str, decision_link: str
) -> str:
    # The input manifest authenticates the exact prompt artifacts separately.
    # Its digest cannot itself participate in the prospective outcome identity:
    # prompts bind that identity, while the manifest is derived from those prompt
    # bytes.  Canonicalizing only that token breaks the otherwise self-referential
    # fingerprint -> prompt -> manifest -> decisionLink cycle.
    decision_identity = _INPUT_MANIFEST_BINDING.sub(
        "inputManifestSha256=<seat-input-manifest>",
        _require_text(decision_link, "outcome field"),
    )
    pieces = (claim, resolution_date, resolved_by, decision_identity)
    canonical = "\x1f".join(" ".join(_require_text(v, "outcome field").split()).casefold() for v in pieces)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def outcome_id_v2(run_id: str, claim: str) -> str:
    """Return the deterministic outcome identity used by a V2 attempt."""

    return _derived_id(
        "outcome",
        _require_id(run_id, "run", "runId"),
        _require_text(claim, "sharedOutcome.claim"),
    )


def forecast_request_identity_v2(
    run_id: str,
    outcome_id: str,
    outcome_fingerprint: str,
    evidence_cutoff_at: str,
    claim: str,
    resolution_date: str,
    resolved_by: str,
    materiality: str,
    action_if_true: str,
    action_if_false: str,
) -> dict[str, str]:
    """Return the canonical request identity and digest of its visible block."""

    block = forecast_request_block_v2(
        run_id,
        outcome_id,
        outcome_fingerprint,
        evidence_cutoff_at,
        claim,
        resolution_date,
        resolved_by,
        materiality,
        action_if_true,
        action_if_false,
    )
    encoded = canonical_forecast_request_json_v2(block)
    return {
        "runId": block["runId"],
        "outcomeId": block["outcomeId"],
        "outcomeFingerprint": block["outcomeFingerprint"],
        "evidenceCutoffAt": block["evidenceCutoffAt"],
        "forecastRequestSha256": hashlib.sha256(encoded).hexdigest(),
    }


def forecast_request_block_v2(
    run_id: str,
    outcome_id: str,
    outcome_fingerprint: str,
    evidence_cutoff_at: str,
    claim: str,
    resolution_date: str,
    resolved_by: str,
    materiality: str,
    action_if_true: str,
    action_if_false: str,
) -> dict[str, Any]:
    """Build the exact human-visible forecast target embedded in every prompt."""

    block: dict[str, Any] = {
        "schemaVersion": 1,
        "runId": _require_id(run_id, "run", "runId"),
        "outcomeId": _require_id(outcome_id, "outcome", "outcomeId"),
        "outcomeFingerprint": _require_digest(
            outcome_fingerprint, "outcomeFingerprint"
        ),
        "evidenceCutoffAt": _require_text(evidence_cutoff_at, "evidenceCutoffAt"),
        "claim": _require_text(claim, "claim"),
        "resolutionDate": str(_parse_date(resolution_date, "resolutionDate")),
        "resolvedBy": _require_text(resolved_by, "resolvedBy"),
        "materiality": _require_text(materiality, "materiality"),
        "actionIfTrue": _require_text(action_if_true, "actionIfTrue"),
        "actionIfFalse": _require_text(action_if_false, "actionIfFalse"),
    }
    _parse_timestamp(block["evidenceCutoffAt"], "evidenceCutoffAt")
    for field, value in block.items():
        if isinstance(value, str) and (
            _FORECAST_REQUEST_BEGIN in value or _FORECAST_REQUEST_END in value
        ):
            raise CaptureSchemaError(
                f"forecast request field {field} contains a reserved delimiter"
            )
    if outcome_id_v2(block["runId"], block["claim"]) != block["outcomeId"]:
        raise CaptureSchemaError("outcomeId does not match runId and claim")
    return block


def canonical_forecast_request_json_v2(block: Mapping[str, Any]) -> bytes:
    """Serialize one already validated request block canonically."""

    expected_keys = {
        "schemaVersion",
        "runId",
        "outcomeId",
        "outcomeFingerprint",
        "evidenceCutoffAt",
        "claim",
        "resolutionDate",
        "resolvedBy",
        "materiality",
        "actionIfTrue",
        "actionIfFalse",
    }
    if not isinstance(block, Mapping) or set(block) != expected_keys:
        raise CaptureSchemaError("forecast request block has missing or unknown keys")
    if block.get("schemaVersion") != 1:
        raise CaptureSchemaError("forecast request block schemaVersion must be 1")
    validated = forecast_request_block_v2(
        block["runId"],
        block["outcomeId"],
        block["outcomeFingerprint"],
        block["evidenceCutoffAt"],
        block["claim"],
        block["resolutionDate"],
        block["resolvedBy"],
        block["materiality"],
        block["actionIfTrue"],
        block["actionIfFalse"],
    )
    return json.dumps(
        validated,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def forecast_request_binding_v2(
    run_id: str,
    outcome_id: str,
    outcome_fingerprint: str,
    evidence_cutoff_at: str,
    claim: str,
    resolution_date: str,
    resolved_by: str,
    materiality: str,
    action_if_true: str,
    action_if_false: str,
) -> str:
    """Return the exact delimited canonical request block for a visible prompt."""

    block = forecast_request_block_v2(
        run_id,
        outcome_id,
        outcome_fingerprint,
        evidence_cutoff_at,
        claim,
        resolution_date,
        resolved_by,
        materiality,
        action_if_true,
        action_if_false,
    )
    canonical = canonical_forecast_request_json_v2(block).decode("utf-8")
    return f"{_FORECAST_REQUEST_BEGIN}\n{canonical}\n{_FORECAST_REQUEST_END}"


def parse_forecast_request_binding_v2(prompt: bytes | bytearray) -> dict[str, Any]:
    """Parse exactly one canonical request block from retained visible bytes."""

    if not isinstance(prompt, (bytes, bytearray)):
        raise CaptureSchemaError("visible prompt must be bytes")
    data = bytes(prompt)
    begin = _FORECAST_REQUEST_BEGIN.encode("ascii")
    end = _FORECAST_REQUEST_END.encode("ascii")
    if data.count(begin) != 1 or data.count(end) != 1:
        raise CaptureSchemaError(
            "visible prompt must contain exactly one forecast request block"
        )
    start = data.index(begin) + len(begin)
    try:
        finish = data.index(end, start)
    except ValueError as exc:
        raise CaptureSchemaError(
            "forecast request block delimiters are out of order"
        ) from exc
    if data[start : start + 1] != b"\n" or data[finish - 1 : finish] != b"\n":
        raise CaptureSchemaError("forecast request block delimiters are malformed")
    encoded = data[start + 1 : finish - 1]
    value = strict_json_loads(encoded)
    if not isinstance(value, Mapping):
        raise CaptureSchemaError("forecast request block must contain a JSON object")
    canonical = canonical_forecast_request_json_v2(value)
    if encoded != canonical:
        raise CaptureSchemaError("forecast request block JSON is not canonical")
    return dict(value)


_OUTCOME_KEYS = {
    "outcomeId",
    "claim",
    "resolutionDate",
    "resolvedBy",
    "decisionLink",
    "materiality",
    "actionIfTrue",
    "actionIfFalse",
    "evidenceCutoffAt",
    "relatedOutcomeIds",
    "fingerprint",
}


def _validate_shared_outcome(value: Any) -> Mapping[str, Any]:
    outcome = _exact_keys(value, required=_OUTCOME_KEYS, field="sharedOutcome")
    _require_id(outcome["outcomeId"], "outcome", "sharedOutcome.outcomeId")
    claim = _require_text(outcome["claim"], "sharedOutcome.claim")
    resolution_date = _parse_date(outcome["resolutionDate"], "sharedOutcome.resolutionDate")
    resolved_by = _require_text(outcome["resolvedBy"], "sharedOutcome.resolvedBy")
    decision_link = _require_text(outcome["decisionLink"], "sharedOutcome.decisionLink")
    _require_text(outcome["materiality"], "sharedOutcome.materiality")
    _require_text(outcome["actionIfTrue"], "sharedOutcome.actionIfTrue")
    _require_text(outcome["actionIfFalse"], "sharedOutcome.actionIfFalse")
    _parse_timestamp(outcome["evidenceCutoffAt"], "sharedOutcome.evidenceCutoffAt")
    related = outcome["relatedOutcomeIds"]
    if not isinstance(related, list):
        raise CaptureSchemaError("sharedOutcome.relatedOutcomeIds must be a list")
    if len(set(related)) != len(related):
        raise CaptureSchemaError("sharedOutcome.relatedOutcomeIds contains duplicates")
    for index, outcome_id in enumerate(related):
        _require_id(outcome_id, "outcome", f"sharedOutcome.relatedOutcomeIds[{index}]")
    expected = outcome_fingerprint_v2(
        claim, str(resolution_date), resolved_by, decision_link
    )
    if outcome["fingerprint"] != expected:
        raise CaptureSchemaError("sharedOutcome.fingerprint does not match its content")
    return outcome


def blind_brief_identity(run_id: str, input_artifact_path: str | None = None) -> str:
    """Return a run-unique blind brief identity, even when artifact bytes recur."""

    run_id = _require_id(run_id, "run", "runId")
    artifact_identity = (
        _require_text(input_artifact_path, "blind input artifact path")
        if input_artifact_path is not None
        else "no-visible-input"
    )
    return f"{artifact_identity}#{run_id}"


def _validate_run_scoped_blind_brief(
    brief: str, run_id: str, *, captured_input_required: bool
) -> None:
    suffix = f"#{run_id}"
    if not brief.endswith(suffix):
        raise CaptureSchemaError("blindSeat.brief must end with its runId")
    artifact_identity = brief[: -len(suffix)]
    if captured_input_required:
        match = _ARTIFACT_PATH.fullmatch(artifact_identity)
        if match is None:
            raise CaptureSchemaError(
                "planned blindSeat.brief must identify its content-addressed input artifact"
            )
        first, second, digest = match.groups()
        if first != digest[:2] or second != digest[2:4]:
            raise CaptureSchemaError(
                "planned blindSeat.brief must use a canonical content-addressed input path"
            )
    elif artifact_identity != "no-visible-input":
        raise CaptureSchemaError(
            "unplanned blindSeat.brief must use the explicit no-visible-input identity"
        )


def _validate_blind_seat(
    value: Any,
    *,
    run_id: str,
    seat_plan: list[Mapping[str, Any]],
    seat_results: Mapping[str, Mapping[str, Any]],
) -> None:
    blind = _exact_keys(
        value,
        required={"role", "required", "ran", "changedDecision", "brief"},
        optional={"blockedReason", "notRequiredReason"},
        field="blindSeat",
    )
    role = _require_text(blind["role"], "blindSeat.role")
    brief = _require_text(blind["brief"], "blindSeat.brief")
    required = _require_bool(blind["required"], "blindSeat.required")
    ran = _require_bool(blind["ran"], "blindSeat.ran")
    planned = [seat for seat in seat_plan if seat["seatId"] == "blind"]
    blind_result = seat_results.get("blind")
    if planned and planned[0]["role"] != "control":
        raise CaptureSchemaError("the canonical blind seatPlan role must be control")
    if required and not planned:
        raise CaptureSchemaError("required blindSeat must have canonical seatId blind in seatPlan")
    submitted = blind_result is not None and blind_result["state"] == "submitted"
    if ran != submitted:
        raise CaptureSchemaError(
            "blindSeat.ran must agree with canonical blind seatResults state"
        )
    if planned and blind_result is not None:
        expected_brief = blind_brief_identity(
            run_id, blind_result["inputArtifact"]["path"]
        )
        if brief != expected_brief:
            raise CaptureSchemaError(
                "blindSeat.brief must be the run-scoped canonical blind brief identity"
            )
    elif not planned:
        _validate_run_scoped_blind_brief(
            brief, run_id, captured_input_required=False
        )
    changed = blind["changedDecision"]
    if ran:
        if not isinstance(changed, bool):
            raise CaptureSchemaError("blindSeat.changedDecision must be boolean when ran=true")
        if role != "independent-control":
            raise CaptureSchemaError(
                "blindSeat.role must be independent-control when ran=true"
            )
        if blind.get("blockedReason") not in (None, ""):
            raise CaptureSchemaError("blindSeat.blockedReason is invalid when ran=true")
    else:
        if changed is not None:
            raise CaptureSchemaError("blindSeat.changedDecision must be null when ran=false")
        if role != "SKIPPED":
            raise CaptureSchemaError("blindSeat.role must be SKIPPED when ran=false")
        _require_text(blind.get("blockedReason"), "blindSeat.blockedReason")
    if required:
        if blind.get("notRequiredReason") not in (None, ""):
            raise CaptureSchemaError("blindSeat.notRequiredReason is invalid when required=true")
    else:
        _require_text(blind.get("notRequiredReason"), "blindSeat.notRequiredReason")


def _validate_seat_results(
    value: Any, seat_plan: list[Mapping[str, Any]]
) -> tuple[dict[str, str], dict[str, Mapping[str, Any]]]:
    if not isinstance(value, list):
        raise CaptureSchemaError("seatResults must be a list")
    plan = {seat["seatId"]: seat for seat in seat_plan}
    if len(value) != len(plan):
        raise CaptureSchemaError("seatResults must contain exactly one result per planned seat")
    states: dict[str, str] = {}
    results: dict[str, Mapping[str, Any]] = {}
    base = {
        "seatId",
        "role",
        "agentVersion",
        "agentDefinitionDigest",
        "state",
        "launcherAttempts",
        # The visible prompt is Tier-1 evidence even when the launcher later
        # abstains or is unavailable. Persist it for every planned seat so a
        # report can reverify the exact pre-launch information set.
        "inputArtifact",
    }
    submitted = {
        "outputArtifact",
        "modelId",
        "toolPolicy",
        "repositoryCommit",
    }
    metrics = {"latencyMs", "inputTokens", "outputTokens", "costUsd"}
    for index, raw in enumerate(value):
        field = f"seatResults[{index}]"
        obj = _require_mapping(raw, field)
        state = _require_text(obj.get("state"), f"{field}.state")
        if state not in SEAT_STATES:
            raise CaptureSchemaError(f"{field}.state must be one of {sorted(SEAT_STATES)}")
        required = base | (submitted if state == "submitted" else set())
        optional = metrics | ({"diffDigest"} if state == "submitted" else set())
        seat = _exact_keys(obj, required=required, optional=optional, field=field)
        seat_id = _require_text(seat["seatId"], f"{field}.seatId")
        if seat_id in results:
            raise CaptureSchemaError("seatResults contains duplicate seatId")
        if seat_id not in plan:
            raise CaptureSchemaError("seatResults contains unplanned seatId")
        role = _require_text(seat["role"], f"{field}.role")
        if role not in SEAT_ROLES:
            raise CaptureSchemaError(
                f"{field}.role must be one of {sorted(SEAT_ROLES)}"
            )
        for binding in ("role", "agentVersion", "agentDefinitionDigest"):
            if seat[binding] != plan[seat_id][binding]:
                raise CaptureSchemaError(f"{field}.{binding} differs from seatPlan")
        attempts = _require_nonnegative_int(seat["launcherAttempts"], f"{field}.launcherAttempts")
        if attempts < 1:
            raise CaptureSchemaError(f"{field}.launcherAttempts must be at least one")
        _validate_artifact_ref(seat["inputArtifact"], f"{field}.inputArtifact")
        for metric in metrics & set(seat):
            if metric in {"latencyMs", "inputTokens", "outputTokens"}:
                _require_nonnegative_int(seat[metric], f"{field}.{metric}")
            else:
                _require_nonnegative_number(seat[metric], f"{field}.{metric}")
        if state == "submitted":
            _validate_artifact_ref(seat["outputArtifact"], f"{field}.outputArtifact")
            _require_text(seat["modelId"], f"{field}.modelId")
            _require_text(seat["toolPolicy"], f"{field}.toolPolicy")
            _require_commit(seat["repositoryCommit"], f"{field}.repositoryCommit")
            if "diffDigest" in seat:
                _require_digest(seat["diffDigest"], f"{field}.diffDigest")
        states[seat_id] = state
        results[seat_id] = seat
    if set(results) != set(plan):
        raise CaptureSchemaError("seatResults seat IDs must exactly match seatPlan")
    return states, results


def _validate_findings_structure(
    findings: Any,
    no_findings: Any,
    *,
    run_id: str,
    submitted: set[str],
    seat_results: Mapping[str, Mapping[str, Any]],
    prior_rows: Iterable[Mapping[str, Any]],
) -> None:
    if not isinstance(findings, list):
        raise CaptureSchemaError("findings must be a list")
    if not isinstance(no_findings, list):
        raise CaptureSchemaError("noFindings must be a list")
    finding_seats: set[str] = set()
    finding_ids: set[str] = set()
    prior_finding_ids: set[str] = set()
    group_runs: dict[str, str] = {}
    for prior in _rows_of_kind(prior_rows, "council-v2"):
        for prior_finding in prior.get("findings", []):
            if not isinstance(prior_finding, Mapping):
                continue
            prior_id = prior_finding.get("findingId")
            if isinstance(prior_id, str):
                prior_finding_ids.add(prior_id)
            prior_group = prior_finding.get("group")
            if isinstance(prior_group, Mapping):
                group_id = prior_group.get("findingGroupId")
                group_run = prior_group.get("runId")
                if isinstance(group_id, str) and isinstance(group_run, str):
                    group_runs[group_id] = group_run
    for index, finding in enumerate(findings):
        field = f"findings[{index}]"
        obj = _exact_keys(
            finding,
            required={
                "findingId",
                "seatId",
                "category",
                "claim",
                "severity",
                "proposedAction",
                "evidenceSummary",
                "group",
                "operatorDisposition",
            },
            field=field,
        )
        finding_id = _require_text(obj["findingId"], f"{field}.findingId")
        if not _FINDING_ID.fullmatch(finding_id):
            raise CaptureSchemaError(f"{field}.findingId must be a stable finding id")
        seat_id = _require_text(obj["seatId"], f"{field}.seatId")
        if finding_id in finding_ids or finding_id in prior_finding_ids:
            raise CaptureSchemaError("duplicate findingId")
        if seat_id not in submitted:
            raise CaptureSchemaError(f"{field} belongs to a non-submitted seat")
        for key in ("category", "claim", "severity", "proposedAction", "evidenceSummary"):
            _require_text(obj[key], f"{field}.{key}")
        group = _exact_keys(
            obj["group"],
            required={"findingGroupId", "runId"},
            field=f"{field}.group",
        )
        group_id = _require_text(group["findingGroupId"], f"{field}.group.findingGroupId")
        if not _FINDING_GROUP_ID.fullmatch(group_id):
            raise CaptureSchemaError(
                f"{field}.group.findingGroupId must be a stable finding-group id"
            )
        _require_id(group["runId"], "run", f"{field}.group.runId")
        if group["runId"] != run_id:
            raise CaptureSchemaError(f"{field}.group belongs to a different run")
        previous_run = group_runs.setdefault(group_id, run_id)
        if previous_run != run_id:
            raise CaptureSchemaError("finding group crosses runs")
        disposition = _require_mapping(
            obj["operatorDisposition"], f"{field}.operatorDisposition"
        )
        disposition_kind = _require_text(
            disposition.get("kind"), f"{field}.operatorDisposition.kind"
        )
        disposition_keys: dict[str, tuple[set[str], ...]] = {
            "already-known": (
                {"kind", "considerationId", "quotedSubclaim"},
            ),
            "new-acted": ({"kind"},),
            "new-rejected": ({"kind", "reason"},),
            "new-deferred": (
                {"kind", "reason"},
                {"kind", "reason", "reviewDate"},
            ),
        }
        if disposition_kind not in disposition_keys:
            raise CaptureSchemaError(
                f"{field}.operatorDisposition.kind is not an allowed disposition"
            )
        if set(disposition) not in disposition_keys[disposition_kind]:
            raise CaptureSchemaError(
                f"{field}.operatorDisposition has invalid keys for {disposition_kind}"
            )
        if disposition_kind == "already-known":
            _require_text(
                disposition["considerationId"],
                f"{field}.operatorDisposition.considerationId",
            )
            _require_text(
                disposition["quotedSubclaim"],
                f"{field}.operatorDisposition.quotedSubclaim",
            )
        if disposition_kind in {"new-rejected", "new-deferred"}:
            _require_text(
                disposition["reason"], f"{field}.operatorDisposition.reason"
            )
        if "reviewDate" in disposition:
            _parse_date(
                disposition["reviewDate"], f"{field}.operatorDisposition.reviewDate"
            )
        finding_ids.add(finding_id)
        finding_seats.add(seat_id)
    declaration_seats: set[str] = set()
    for index, declaration in enumerate(no_findings):
        field = f"noFindings[{index}]"
        obj = _exact_keys(
            declaration,
            required={"kind", "seatId", "outputArtifact"},
            field=field,
        )
        declaration_kind = _require_text(obj["kind"], f"{field}.kind")
        if declaration_kind != "no-findings":
            raise CaptureSchemaError(f"{field}.kind must be no-findings")
        seat_id = _require_text(obj["seatId"], f"{field}.seatId")
        if seat_id not in submitted:
            raise CaptureSchemaError(f"{field} belongs to a non-submitted seat")
        if seat_id in declaration_seats:
            raise CaptureSchemaError("duplicate noFindings declaration for seat")
        _validate_artifact_ref(obj["outputArtifact"], f"{field}.outputArtifact")
        if obj["outputArtifact"] != seat_results[seat_id]["outputArtifact"]:
            raise CaptureSchemaError(f"{field} does not bind the seat output artifact")
        declaration_seats.add(seat_id)
    overlap = finding_seats & declaration_seats
    if overlap:
        raise CaptureSchemaError(
            "submitted seats cannot have both findings and noFindings"
        )
    uncovered = submitted - finding_seats - declaration_seats
    if uncovered:
        raise CaptureSchemaError(
            "submitted seats need findings or a noFindings declaration"
        )


def _validate_predictions(
    value: Any,
    *,
    submitted: set[str],
    outcome: Mapping[str, Any],
) -> None:
    if not isinstance(value, list):
        raise CaptureSchemaError("predictions must be a list")
    seen_ids: set[str] = set()
    seen_seats: set[str] = set()
    required = {
        "predictionId",
        "outcomeId",
        "seat",
        "type",
        "claim",
        "probability",
        "resolutionDate",
        "resolvedBy",
    }
    for index, raw in enumerate(value):
        field = f"predictions[{index}]"
        prediction = _exact_keys(raw, required=required, field=field)
        prediction_id = _require_id(
            prediction["predictionId"], "prediction", f"{field}.predictionId"
        )
        if prediction_id in seen_ids:
            raise CaptureSchemaError("duplicate predictionId")
        seat = _require_text(prediction["seat"], f"{field}.seat")
        if seat in seen_seats:
            raise CaptureSchemaError("multiple predictions for submitted seat")
        if seat not in submitted:
            raise CaptureSchemaError("prediction exists for non-submitted seat")
        prediction_type = _require_text(prediction["type"], f"{field}.type")
        if prediction_type != "shared":
            raise CaptureSchemaError(f"{field}.type must be shared")
        for key in ("outcomeId", "claim", "resolutionDate", "resolvedBy"):
            if prediction[key] != outcome[key]:
                raise CaptureSchemaError(f"{field}.{key} differs from sharedOutcome")
        probability = prediction["probability"]
        if isinstance(probability, bool) or not isinstance(probability, int):
            raise CaptureSchemaError(f"{field}.probability must be an integer")
        if not 0 <= probability <= 100:
            raise CaptureSchemaError(f"{field}.probability must be between 0 and 100")
        seen_ids.add(prediction_id)
        seen_seats.add(seat)
    if seen_seats != submitted:
        raise CaptureSchemaError("submitted seats must have exactly one prediction each")


_RECORD_KEYS = {
    "capture-activation": {
        "schemaVersion",
        "kind",
        "activationId",
        "activatedAt",
        "cohortName",
        "captureVersion",
        "runtimeSourceCommit",
        "runtimeSourceSha256",
        "artifactRootPolicy",
    },
    "capture-initiation": {
        "schemaVersion",
        "kind",
        "initiationId",
        "runId",
        "activationId",
        "idempotencyKey",
        "handlingStartedAt",
    },
    "council-attempt-v2": {
        "schemaVersion",
        "kind",
        "runId",
        "initiationId",
        "activationId",
        "seatsLaunchedAt",
        "decisionFamilyId",
        "question",
        "decisionBeforeArtifact",
        "outcomeClass",
        "outcomeClassRationale",
        "evidenceCutoffAt",
        "seatPlan",
        "sharedOutcome",
    },
    "council-seats-finished": {
        "schemaVersion",
        "kind",
        "runId",
        "initiationId",
        "activationId",
        "seatsFinishedAt",
        "seatStates",
    },
    "council-v2": {
        "schemaVersion",
        "kind",
        "runId",
        "initiationId",
        "activationId",
        "finalizedAt",
        "decisionFamilyId",
        "question",
        "decisionBeforeArtifact",
        "outcomeClass",
        "outcomeClassRationale",
        "evidenceCutoffAt",
        "seatPlan",
        "sharedOutcome",
        "seatResults",
        "findings",
        "noFindings",
        "predictions",
        "blindSeat",
    },
    "finding-audit-case-v2": {
        "schemaVersion",
        "kind",
        "activationId",
        "runId",
        "decisionFamilyId",
        "recordedAt",
        "protocolSha256",
        "auditCaseSha256",
        "caseArtifact",
        "aliasMapArtifact",
    },
    "capture-invalidation": {
        "schemaVersion",
        "kind",
        "invalidationId",
        "runId",
        "reason",
        "operator",
        "invalidatedAt",
        "evidenceRef",
    },
}

_RECORD_OPTIONAL_KEYS = {
    "capture-activation": {"approvalManifest", "auditProtocol"},
    "council-attempt-v2": {"auditAssignment"},
}


def _rows_of_kind(prior_rows: Iterable[Mapping[str, Any]], kind: str) -> list[Mapping[str, Any]]:
    return [
        row
        for row in prior_rows
        if row.get("schemaVersion") == SCHEMA_VERSION and row.get("kind") == kind
    ]


def _one_by(
    prior_rows: Iterable[Mapping[str, Any]], kind: str, field: str, value: str
) -> Mapping[str, Any] | None:
    matches = [row for row in _rows_of_kind(prior_rows, kind) if row.get(field) == value]
    if len(matches) > 1:
        raise CaptureSchemaError(f"duplicate prior {kind} {field}")
    return matches[0] if matches else None


def _ensure_record_header(row: Mapping[str, Any], expected_kind: str) -> None:
    _exact_keys(
        row,
        required=_RECORD_KEYS[expected_kind],
        optional=_RECORD_OPTIONAL_KEYS.get(expected_kind),
        field=expected_kind,
    )
    if row["schemaVersion"] != SCHEMA_VERSION:
        raise CaptureSchemaError(f"{expected_kind} schemaVersion must be 2")
    record_kind = _require_text(row["kind"], "record.kind")
    if record_kind != expected_kind:
        raise CaptureSchemaError(f"record kind must be {expected_kind}")


def _validate_activation(row: Mapping[str, Any], prior_rows: list[Mapping[str, Any]]) -> None:
    _ensure_record_header(row, "capture-activation")
    _require_id(row["activationId"], "activation", "activationId")
    _parse_timestamp(row["activatedAt"], "activatedAt")
    _require_text(row["cohortName"], "cohortName")
    _require_text(row["captureVersion"], "captureVersion")
    _require_commit(row["runtimeSourceCommit"], "runtimeSourceCommit")
    _require_digest(row["runtimeSourceSha256"], "runtimeSourceSha256")
    _require_text(row["artifactRootPolicy"], "artifactRootPolicy")
    if "approvalManifest" in row:
        _validate_artifact_ref(row["approvalManifest"], "approvalManifest")
    if "auditProtocol" in row:
        if "approvalManifest" not in row:
            raise CaptureSchemaError(
                "auditProtocol requires a content-addressed approvalManifest"
            )
        _audit_protocol_digest(row["auditProtocol"])
    if _rows_of_kind(prior_rows, "capture-activation"):
        raise CaptureSchemaError("capture ledger already has an activation")


def _validate_initiation(row: Mapping[str, Any], prior_rows: list[Mapping[str, Any]]) -> None:
    _ensure_record_header(row, "capture-initiation")
    initiation_id = _require_id(row["initiationId"], "initiation", "initiationId")
    run_id = _require_id(row["runId"], "run", "runId")
    activation_id = _require_id(row["activationId"], "activation", "activationId")
    idempotency_key = _require_text(row["idempotencyKey"], "idempotencyKey")
    started = _parse_timestamp(row["handlingStartedAt"], "handlingStartedAt")
    activation = _one_by(prior_rows, "capture-activation", "activationId", activation_id)
    if activation is None:
        raise CaptureSchemaError("capture-initiation has no prior activation")
    if started < _parse_timestamp(activation["activatedAt"], "activatedAt"):
        raise CaptureSchemaError("handlingStartedAt precedes activation")
    if _one_by(prior_rows, "capture-initiation", "initiationId", initiation_id):
        raise CaptureSchemaError("duplicate initiationId")
    if _one_by(prior_rows, "capture-initiation", "runId", run_id):
        raise CaptureSchemaError("duplicate capture-initiation runId")
    duplicate_key = [
        prior
        for prior in _rows_of_kind(prior_rows, "capture-initiation")
        if prior.get("activationId") == activation_id
        and prior.get("idempotencyKey") == idempotency_key
    ]
    if duplicate_key:
        raise CaptureSchemaError(
            "duplicate capture initiation for (activationId, idempotencyKey)"
        )


def _validate_attempt(row: Mapping[str, Any], prior_rows: list[Mapping[str, Any]]) -> None:
    _ensure_record_header(row, "council-attempt-v2")
    run_id = _require_id(row["runId"], "run", "runId")
    initiation_id = _require_id(row["initiationId"], "initiation", "initiationId")
    activation_id = _require_id(row["activationId"], "activation", "activationId")
    initiation = _one_by(prior_rows, "capture-initiation", "initiationId", initiation_id)
    if initiation is None:
        raise CaptureSchemaError("council-attempt-v2 has no prior initiation")
    if initiation["runId"] != run_id or initiation["activationId"] != activation_id:
        raise CaptureSchemaError("council-attempt-v2 identity differs from initiation")
    if _one_by(prior_rows, "council-attempt-v2", "runId", run_id):
        raise CaptureSchemaError("duplicate council-attempt-v2 for runId")
    launched = _parse_timestamp(row["seatsLaunchedAt"], "seatsLaunchedAt")
    started = _parse_timestamp(initiation["handlingStartedAt"], "handlingStartedAt")
    if launched < started:
        raise CaptureSchemaError("seatsLaunchedAt precedes handlingStartedAt")
    family_id = _require_text(row["decisionFamilyId"], "decisionFamilyId")
    if not _FAMILY_ID.fullmatch(family_id):
        raise CaptureSchemaError("decisionFamilyId must be a stable family-* id")
    _require_text(row["question"], "question")
    _validate_decision_artifact(row["decisionBeforeArtifact"])
    outcome_class = _require_text(row["outcomeClass"], "outcomeClass")
    if outcome_class not in OUTCOME_CLASSES:
        raise CaptureSchemaError(f"outcomeClass must be one of {sorted(OUTCOME_CLASSES)}")
    _require_text(row["outcomeClassRationale"], "outcomeClassRationale")
    cutoff = _parse_timestamp(row["evidenceCutoffAt"], "evidenceCutoffAt")
    if cutoff > launched:
        raise CaptureSchemaError("evidenceCutoffAt cannot follow seatsLaunchedAt")
    _validate_seat_plan(row["seatPlan"])
    outcome = _validate_shared_outcome(row["sharedOutcome"])
    _input_manifest_binding(outcome["decisionLink"])
    outcome_id = outcome["outcomeId"]
    for prior_attempt in _rows_of_kind(prior_rows, "council-attempt-v2"):
        prior_outcome = prior_attempt.get("sharedOutcome")
        if isinstance(prior_outcome, Mapping) and prior_outcome.get("outcomeId") == outcome_id:
            raise CaptureSchemaError("duplicate V2 outcomeId")
    if outcome_id in outcome["relatedOutcomeIds"]:
        raise CaptureSchemaError("sharedOutcome cannot relate to itself")
    prior_matching_attempts = [
        prior_attempt
        for prior_attempt in _rows_of_kind(prior_rows, "council-attempt-v2")
        if isinstance(prior_attempt.get("sharedOutcome"), Mapping)
        and prior_attempt["sharedOutcome"].get("fingerprint") == outcome["fingerprint"]
    ]
    prior_matching_outcome_ids = {
        prior_outcome["outcomeId"]
        for prior_attempt in prior_matching_attempts
        if isinstance((prior_outcome := prior_attempt.get("sharedOutcome")), Mapping)
        and isinstance(prior_outcome.get("outcomeId"), str)
    }
    if prior_matching_outcome_ids and prior_matching_outcome_ids.isdisjoint(
        outcome["relatedOutcomeIds"]
    ):
        raise CaptureSchemaError(
            "repeated sharedOutcome fingerprint must link a prior matching outcomeId"
        )
    prior_outcome_classes = {
        _require_text(
            prior_attempt.get("outcomeClass"), "prior repeated outcomeClass"
        )
        for prior_attempt in prior_matching_attempts
    }
    if prior_outcome_classes and prior_outcome_classes != {outcome_class}:
        raise CaptureSchemaError(
            "repeated sharedOutcome fingerprint must retain one outcomeClass"
        )
    if outcome["evidenceCutoffAt"] != row["evidenceCutoffAt"]:
        raise CaptureSchemaError("sharedOutcome evidenceCutoffAt differs from attempt")
    if launched.date() >= _parse_date(outcome["resolutionDate"], "resolutionDate"):
        raise CaptureSchemaError("seatsLaunchedAt must precede resolutionDate")
    activation = _one_by(prior_rows, "capture-activation", "activationId", activation_id)
    if activation is None:
        raise CaptureSchemaError("council-attempt-v2 has no prior activation")
    protocol = activation.get("auditProtocol")
    assignment = row.get("auditAssignment")
    if protocol is None:
        if assignment is not None:
            raise CaptureSchemaError("auditAssignment has no activated audit protocol")
    else:
        if assignment is None:
            raise CaptureSchemaError("activated audit protocol requires auditAssignment")
        normalized_assignment = _validate_audit_assignment_record(
            assignment, protocol=protocol
        )
        if (
            normalized_assignment["activationId"] != activation_id
            or normalized_assignment["decisionFamilyId"] != family_id
        ):
            raise CaptureSchemaError("auditAssignment identity differs from attempt")
        assigned = _parse_timestamp(
            normalized_assignment["assignedAt"], "auditAssignment.assignedAt"
        )
        if assigned >= launched:
            raise CaptureSchemaError("auditAssignment must precede seatsLaunchedAt")
        prior_family_attempts = [
            item
            for item in _rows_of_kind(prior_rows, "council-attempt-v2")
            if item.get("activationId") == activation_id
            and item.get("decisionFamilyId") == family_id
        ]
        if prior_family_attempts:
            inherited = prior_family_attempts[0].get("auditAssignment")
            if inherited is None or normalized_assignment != inherited:
                raise CaptureSchemaError(
                    "repeated decision family must inherit its auditAssignment"
                )
        elif normalized_assignment["firstRunId"] != run_id:
            raise CaptureSchemaError("first family auditAssignment must bind this run")
    bindings = (
        activation["runtimeSourceCommit"],
        row["decisionBeforeArtifact"]["gitBlob"],
        row["decisionBeforeArtifact"]["sha256"],
    )
    if any(binding not in outcome["decisionLink"] for binding in bindings):
        raise CaptureSchemaError(
            "sharedOutcome.decisionLink must contain runtime commit, baseline Git blob, and SHA-256"
        )


def _validate_finding_audit_case(
    row: Mapping[str, Any], prior_rows: list[Mapping[str, Any]]
) -> None:
    _ensure_record_header(row, "finding-audit-case-v2")
    activation_id = _require_id(row["activationId"], "activation", "activationId")
    run_id = _require_id(row["runId"], "run", "runId")
    family_id = _require_text(row["decisionFamilyId"], "decisionFamilyId")
    if not _FAMILY_ID.fullmatch(family_id):
        raise CaptureSchemaError("decisionFamilyId must be a stable family-* id")
    recorded = _parse_timestamp(row["recordedAt"], "recordedAt")
    _require_digest(row["protocolSha256"], "protocolSha256")
    _require_digest(row["auditCaseSha256"], "auditCaseSha256")
    _validate_artifact_ref(row["caseArtifact"], "caseArtifact")
    _validate_artifact_ref(row["aliasMapArtifact"], "aliasMapArtifact")
    completion = _one_by(prior_rows, "council-v2", "runId", run_id)
    if completion is None:
        raise CaptureSchemaError("finding-audit-case-v2 has no prior completion")
    if (
        completion.get("activationId") != activation_id
        or completion.get("decisionFamilyId") != family_id
    ):
        raise CaptureSchemaError("finding-audit-case-v2 identity differs from completion")
    attempt = _one_by(prior_rows, "council-attempt-v2", "runId", run_id)
    assignment = None if attempt is None else attempt.get("auditAssignment")
    if not isinstance(assignment, Mapping) or assignment.get("selected") is not True:
        raise CaptureSchemaError("finding-audit-case-v2 requires selected auditAssignment")
    activation = _one_by(prior_rows, "capture-activation", "activationId", activation_id)
    protocol = None if activation is None else activation.get("auditProtocol")
    if protocol is None or row["protocolSha256"] != _audit_protocol_digest(protocol):
        raise CaptureSchemaError("finding-audit-case-v2 protocol binding mismatch")
    if recorded < _parse_timestamp(completion["finalizedAt"], "finalizedAt"):
        raise CaptureSchemaError("finding audit case precedes council completion")
    prior_family_cases = [
        item
        for item in _rows_of_kind(prior_rows, "finding-audit-case-v2")
        if item.get("activationId") == activation_id
        and item.get("decisionFamilyId") == family_id
    ]
    if prior_family_cases:
        raise CaptureSchemaError("decision family already has a finding audit case")


def _validate_seats_finished(
    row: Mapping[str, Any], prior_rows: list[Mapping[str, Any]]
) -> None:
    _ensure_record_header(row, "council-seats-finished")
    run_id = _require_id(row["runId"], "run", "runId")
    initiation_id = _require_id(row["initiationId"], "initiation", "initiationId")
    activation_id = _require_id(row["activationId"], "activation", "activationId")
    attempt = _one_by(prior_rows, "council-attempt-v2", "runId", run_id)
    if attempt is None:
        raise CaptureSchemaError("council-seats-finished has no prior attempt")
    if attempt["initiationId"] != initiation_id or attempt["activationId"] != activation_id:
        raise CaptureSchemaError("council-seats-finished identity differs from attempt")
    if _one_by(prior_rows, "council-seats-finished", "runId", run_id):
        raise CaptureSchemaError("duplicate council-seats-finished for runId")
    finished = _parse_timestamp(row["seatsFinishedAt"], "seatsFinishedAt")
    launched = _parse_timestamp(attempt["seatsLaunchedAt"], "seatsLaunchedAt")
    if finished < launched:
        raise CaptureSchemaError("seatsFinishedAt precedes seatsLaunchedAt")
    states = _require_mapping(row["seatStates"], "seatStates")
    planned = {seat["seatId"] for seat in attempt["seatPlan"]}
    if set(states) != planned:
        raise CaptureSchemaError("seatStates must exactly match seatPlan")
    for seat_id, state in states.items():
        state = _require_text(state, f"seatStates.{seat_id}")
        if state not in SEAT_STATES:
            raise CaptureSchemaError("invalid terminal state for seat")


def _validate_completion_content(
    row: Mapping[str, Any], prior_rows: list[Mapping[str, Any]]
) -> Mapping[str, Any]:
    """Validate completion content that does not depend on ``finalizedAt``.

    Completion is a two-phase operation at the runtime boundary: artifact and
    finding checks operate on a fully validated prepared row, and only then is
    the system-owned append timestamp sampled.  Keeping the timestamp-independent
    checks here lets that preparation remain as strict as the persisted schema.
    """

    run_id = _require_id(row["runId"], "run", "runId")
    initiation_id = _require_id(row["initiationId"], "initiation", "initiationId")
    activation_id = _require_id(row["activationId"], "activation", "activationId")
    attempt = _one_by(prior_rows, "council-attempt-v2", "runId", run_id)
    if attempt is None:
        raise CaptureSchemaError("council-v2 has no prior attempt")
    finished = _one_by(prior_rows, "council-seats-finished", "runId", run_id)
    if finished is None:
        raise CaptureSchemaError("council-v2 has no prior seats-finished boundary")
    if _one_by(prior_rows, "council-v2", "runId", run_id):
        raise CaptureSchemaError("duplicate council-v2 for runId")
    if attempt["initiationId"] != initiation_id or attempt["activationId"] != activation_id:
        raise CaptureSchemaError("council-v2 identity differs from attempt")
    # Parse nested exact schemas before comparing their frozen copies so an
    # unknown nested key is reported as such rather than merely as a mismatch.
    seat_plan = _validate_seat_plan(row["seatPlan"])
    copied = {
        "decisionFamilyId",
        "question",
        "decisionBeforeArtifact",
        "outcomeClass",
        "outcomeClassRationale",
        "evidenceCutoffAt",
        "seatPlan",
        "sharedOutcome",
    }
    for field in copied:
        if row[field] != attempt[field]:
            raise CaptureSchemaError(f"council-v2 {field} differs from attempt")
    states, results = _validate_seat_results(row["seatResults"], seat_plan)
    if states != dict(finished["seatStates"]):
        raise CaptureSchemaError("council-v2 seat states differ from seats-finished")
    persisted_manifest = seat_input_manifest_sha256(
        {
            seat_id: result["inputArtifact"]
            for seat_id, result in results.items()
        }
    )
    expected_manifest = _input_manifest_binding(
        attempt["sharedOutcome"]["decisionLink"]
    )
    if persisted_manifest != expected_manifest:
        raise CaptureSchemaError(
            "council-v2 seat input artifacts differ from the attempt input manifest"
        )
    submitted = {seat for seat, state in states.items() if state == "submitted"}
    _validate_findings_structure(
        row["findings"],
        row["noFindings"],
        run_id=run_id,
        submitted=submitted,
        seat_results=results,
        prior_rows=prior_rows,
    )
    outcome = _validate_shared_outcome(row["sharedOutcome"])
    _validate_predictions(row["predictions"], submitted=submitted, outcome=outcome)
    prior_prediction_ids = {
        prediction.get("predictionId")
        for prior in _rows_of_kind(prior_rows, "council-v2")
        for prediction in prior.get("predictions", [])
        if isinstance(prediction, Mapping)
    }
    repeated_prediction_ids = {
        prediction["predictionId"] for prediction in row["predictions"]
    } & prior_prediction_ids
    if repeated_prediction_ids:
        raise CaptureSchemaError("predictionId reused across V2 completions")
    _validate_blind_seat(
        row["blindSeat"],
        run_id=run_id,
        seat_plan=seat_plan,
        seat_results=results,
    )
    return finished


def _validate_prepared_completion(
    row: Mapping[str, Any], prior_rows: list[Mapping[str, Any]]
) -> None:
    _exact_keys(
        row,
        required=_RECORD_KEYS["council-v2"] - {"finalizedAt"},
        field="prepared council-v2",
    )
    if row["schemaVersion"] != SCHEMA_VERSION:
        raise CaptureSchemaError("council-v2 schemaVersion must be 2")
    record_kind = _require_text(row["kind"], "record.kind")
    if record_kind != "council-v2":
        raise CaptureSchemaError("record kind must be council-v2")
    _validate_completion_content(row, prior_rows)


def _validate_completion(row: Mapping[str, Any], prior_rows: list[Mapping[str, Any]]) -> None:
    _ensure_record_header(row, "council-v2")
    finished = _validate_completion_content(row, prior_rows)
    finalized = _parse_timestamp(row["finalizedAt"], "finalizedAt")
    seats_finished = _parse_timestamp(finished["seatsFinishedAt"], "seatsFinishedAt")
    if finalized < seats_finished:
        raise CaptureSchemaError("finalizedAt precedes seatsFinishedAt")
    resolution_date = _parse_date(
        row["sharedOutcome"]["resolutionDate"], "sharedOutcome.resolutionDate"
    )
    if finalized.date() >= resolution_date:
        raise CaptureSchemaError("finalizedAt must precede sharedOutcome.resolutionDate")


def _validate_invalidation(
    row: Mapping[str, Any], prior_rows: list[Mapping[str, Any]]
) -> None:
    _ensure_record_header(row, "capture-invalidation")
    invalidation_id = _require_id(
        row["invalidationId"], "invalidation", "invalidationId"
    )
    run_id = _require_id(row["runId"], "run", "runId")
    initiation = _one_by(prior_rows, "capture-initiation", "runId", run_id)
    if initiation is None:
        raise CaptureSchemaError("capture-invalidation has no prior initiation")
    if _one_by(prior_rows, "capture-invalidation", "invalidationId", invalidation_id):
        raise CaptureSchemaError("duplicate invalidationId")
    reason = _require_text(row["reason"], "capture-invalidation reason")
    if reason not in INVALIDATION_REASONS:
        raise CaptureSchemaError(
            f"capture-invalidation reason must be one of {sorted(INVALIDATION_REASONS)}"
        )
    _require_text(row["operator"], "operator")
    _require_text(row["evidenceRef"], "evidenceRef")
    invalidated = _parse_timestamp(row["invalidatedAt"], "invalidatedAt")
    started = _parse_timestamp(initiation["handlingStartedAt"], "handlingStartedAt")
    if invalidated < started:
        raise CaptureSchemaError("invalidatedAt precedes handlingStartedAt")


_VALIDATORS: dict[
    str, Callable[[Mapping[str, Any], list[Mapping[str, Any]]], None]
] = {
    "capture-activation": _validate_activation,
    "capture-initiation": _validate_initiation,
    "council-attempt-v2": _validate_attempt,
    "council-seats-finished": _validate_seats_finished,
    "council-v2": _validate_completion,
    "capture-invalidation": _validate_invalidation,
    "finding-audit-case-v2": _validate_finding_audit_case,
}


def _boundary_time(row: Mapping[str, Any]) -> datetime | None:
    field_by_kind = {
        "capture-activation": "activatedAt",
        "capture-initiation": "handlingStartedAt",
        "council-attempt-v2": "seatsLaunchedAt",
        "council-seats-finished": "seatsFinishedAt",
        "council-v2": "finalizedAt",
        "capture-invalidation": "invalidatedAt",
        "finding-audit-case-v2": "recordedAt",
    }
    field = field_by_kind.get(row.get("kind"))
    return _parse_timestamp(row[field], field) if field else None


def validate_v2_record(
    row: Mapping[str, Any],
    prior_rows: Iterable[Mapping[str, Any]] = (),
    *,
    now: datetime | str | None = None,
) -> bool:
    """Validate one V2 row against already-appended rows.

    ``False`` means the row is V1/legacy and was deliberately ignored.  A V2
    row returns ``True``.  Unknown non-V1 versions and V2 kinds fail closed.
    ``prior_rows`` must be in ledger order and should already have been
    validated (use :func:`validate_v2_ledger` for an entire ledger).
    """

    row = _require_mapping(row, "record")
    version = row.get("schemaVersion")
    kind = row.get("kind")
    if version in (None, V1_SCHEMA_VERSION):
        if isinstance(kind, str) and kind in V2_KINDS:
            raise CaptureSchemaError(f"{kind} schemaVersion must be 2")
        return False
    if version != SCHEMA_VERSION:
        raise CaptureSchemaError("unknown capture schemaVersion")
    kind = _require_text(kind, "record.kind")
    if kind not in _VALIDATORS:
        raise CaptureSchemaError("unknown schemaVersion 2 record kind")
    prior = list(prior_rows)
    _VALIDATORS[kind](row, prior)
    if now is not None:
        observed = now
        if isinstance(observed, datetime):
            if observed.tzinfo is None:
                raise CaptureSchemaError("now must be timezone-aware")
            observed_at = observed.astimezone(timezone.utc)
        else:
            observed_at = _parse_timestamp(observed, "now")
        boundary = _boundary_time(row)
        if boundary is not None and boundary > observed_at:
            raise CaptureSchemaError(f"{row['kind']} has a future system boundary")
    return True


def validate_v2_ledger(
    rows: Iterable[Mapping[str, Any]], *, now: datetime | str | None = None
) -> list[Mapping[str, Any]]:
    """Table-driven validation over ledger order, returning only V2 rows."""

    prior: list[Mapping[str, Any]] = []
    for line_number, row in enumerate(rows, 1):
        try:
            is_v2 = validate_v2_record(row, prior, now=now)
        except CaptureSchemaError as exc:
            raise CaptureSchemaError(f"ledger row {line_number}: {exc}") from exc
        if is_v2:
            prior.append(row)
    return prior


def invalidated_run_ids(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    """Return the permanent invalidation set after validating the V2 ledger."""

    valid = validate_v2_ledger(rows)
    return {row["runId"] for row in valid if row["kind"] == "capture-invalidation"}


def make_capture_activation(
    payload: Mapping[str, Any],
    *,
    prior_rows: Iterable[Mapping[str, Any]] = (),
    clock: Clock,
    id_factory: IdFactory = new_v2_id,
) -> dict[str, Any]:
    payload = _require_payload_keys(
        payload,
        required={
            "cohortName",
            "captureVersion",
            "runtimeSourceCommit",
            "runtimeSourceSha256",
            "artifactRootPolicy",
        },
        optional={"approvalManifest", "auditProtocol"},
        kind="capture-activation",
    )
    row = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "capture-activation",
        "activationId": id_factory("activation"),
        "activatedAt": _clock_timestamp(clock, "activatedAt"),
        **deepcopy(dict(payload)),
    }
    validate_v2_record(row, prior_rows)
    return row


def make_capture_initiation(
    payload: Mapping[str, Any],
    *,
    prior_rows: Iterable[Mapping[str, Any]],
    clock: Clock,
) -> dict[str, Any]:
    payload = _require_payload_keys(
        payload,
        required={"activationId", "idempotencyKey"},
        optional=None,
        kind="capture-initiation",
    )
    prior = list(prior_rows)
    activation_id = _require_id(payload["activationId"], "activation", "activationId")
    key = _require_text(payload["idempotencyKey"], "idempotencyKey")
    existing = [
        row
        for row in _rows_of_kind(prior, "capture-initiation")
        if row.get("activationId") == activation_id and row.get("idempotencyKey") == key
    ]
    if len(existing) > 1:
        raise CaptureSchemaError(
            "ledger has duplicate capture initiations for (activationId, idempotencyKey)"
        )
    if existing:
        return deepcopy(dict(existing[0]))
    row = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "capture-initiation",
        "initiationId": _derived_id("initiation", activation_id, key),
        "runId": _derived_id("run", activation_id, key),
        "activationId": activation_id,
        "idempotencyKey": key,
        "handlingStartedAt": _clock_timestamp(clock, "handlingStartedAt"),
    }
    validate_v2_record(row, prior)
    return row


_OUTCOME_INPUT_REQUIRED = {
    "claim",
    "resolutionDate",
    "resolvedBy",
    "decisionLink",
    "materiality",
    "actionIfTrue",
    "actionIfFalse",
}


def make_council_attempt_v2(
    payload: Mapping[str, Any],
    *,
    prior_rows: Iterable[Mapping[str, Any]],
    clock: Clock,
) -> dict[str, Any]:
    payload = _require_payload_keys(
        payload,
        required={
            "initiationId",
            "decisionFamilyId",
            "question",
            "decisionBeforeArtifact",
            "outcomeClass",
            "outcomeClassRationale",
            "evidenceCutoffAt",
            "seatPlan",
            "sharedOutcome",
        },
        optional={"auditAssignment"},
        kind="council-attempt-v2",
    )
    prior = list(prior_rows)
    initiation = _one_by(
        prior, "capture-initiation", "initiationId", payload["initiationId"]
    )
    if initiation is None:
        raise CaptureSchemaError("council-attempt-v2 has no prior initiation")
    outcome_input = _exact_keys(
        payload["sharedOutcome"],
        required=_OUTCOME_INPUT_REQUIRED,
        optional={"relatedOutcomeIds"},
        field="council-attempt-v2 payload.sharedOutcome",
    )
    outcome_id = outcome_id_v2(initiation["runId"], outcome_input["claim"])
    outcome = {
        **outcome_input,
        "outcomeId": outcome_id,
        "evidenceCutoffAt": payload["evidenceCutoffAt"],
        "relatedOutcomeIds": list(outcome_input.get("relatedOutcomeIds", [])),
        "fingerprint": outcome_fingerprint_v2(
            outcome_input["claim"],
            outcome_input["resolutionDate"],
            outcome_input["resolvedBy"],
            outcome_input["decisionLink"],
        ),
    }
    row = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "council-attempt-v2",
        "runId": initiation["runId"],
        "initiationId": initiation["initiationId"],
        "activationId": initiation["activationId"],
        "seatsLaunchedAt": _clock_timestamp(clock, "seatsLaunchedAt"),
        "decisionFamilyId": payload["decisionFamilyId"],
        "question": payload["question"],
        "decisionBeforeArtifact": deepcopy(payload["decisionBeforeArtifact"]),
        "outcomeClass": payload["outcomeClass"],
        "outcomeClassRationale": payload["outcomeClassRationale"],
        "evidenceCutoffAt": payload["evidenceCutoffAt"],
        "seatPlan": deepcopy(payload["seatPlan"]),
        "sharedOutcome": outcome,
    }
    if "auditAssignment" in payload:
        row["auditAssignment"] = deepcopy(payload["auditAssignment"])
    validate_v2_record(row, prior)
    return row


def make_council_seats_finished(
    payload: Mapping[str, Any],
    *,
    prior_rows: Iterable[Mapping[str, Any]],
    clock: Clock,
) -> dict[str, Any]:
    payload = _require_payload_keys(
        payload,
        required={"runId", "seatStates"},
        optional=None,
        kind="council-seats-finished",
    )
    prior = list(prior_rows)
    attempt = _one_by(prior, "council-attempt-v2", "runId", payload["runId"])
    if attempt is None:
        raise CaptureSchemaError("council-seats-finished has no prior attempt")
    row = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "council-seats-finished",
        "runId": attempt["runId"],
        "initiationId": attempt["initiationId"],
        "activationId": attempt["activationId"],
        "seatsFinishedAt": _clock_timestamp(clock, "seatsFinishedAt"),
        "seatStates": deepcopy(payload["seatStates"]),
    }
    validate_v2_record(row, prior)
    return row


def prepare_council_v2(
    payload: Mapping[str, Any],
    *,
    prior_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build and validate completion content without sampling ``finalizedAt``."""

    payload = _require_payload_keys(
        payload,
        required={
            "runId",
            "seatResults",
            "findings",
            "noFindings",
            "probabilities",
            "blindSeat",
        },
        optional=None,
        kind="council-v2",
    )
    prior = list(prior_rows)
    attempt = _one_by(prior, "council-attempt-v2", "runId", payload["runId"])
    if attempt is None:
        raise CaptureSchemaError("council-v2 has no prior attempt")
    probabilities = _require_mapping(payload["probabilities"], "probabilities")
    states, _ = _validate_seat_results(payload["seatResults"], attempt["seatPlan"])
    submitted = {seat for seat, state in states.items() if state == "submitted"}
    if set(probabilities) != submitted:
        raise CaptureSchemaError("probabilities must exactly match submitted seats")
    outcome = attempt["sharedOutcome"]
    predictions = [
        {
            "predictionId": _derived_id("prediction", attempt["runId"], seat),
            "outcomeId": outcome["outcomeId"],
            "seat": seat,
            "type": "shared",
            "claim": outcome["claim"],
            "probability": probabilities[seat],
            "resolutionDate": outcome["resolutionDate"],
            "resolvedBy": outcome["resolvedBy"],
        }
        for seat in (plan["seatId"] for plan in attempt["seatPlan"])
        if seat in submitted
    ]
    copied = {
        key: deepcopy(attempt[key])
        for key in (
            "decisionFamilyId",
            "question",
            "decisionBeforeArtifact",
            "outcomeClass",
            "outcomeClassRationale",
            "evidenceCutoffAt",
            "seatPlan",
            "sharedOutcome",
        )
    }
    row = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "council-v2",
        "runId": attempt["runId"],
        "initiationId": attempt["initiationId"],
        "activationId": attempt["activationId"],
        **copied,
        "seatResults": deepcopy(payload["seatResults"]),
        "findings": deepcopy(payload["findings"]),
        "noFindings": deepcopy(payload["noFindings"]),
        "predictions": predictions,
        "blindSeat": deepcopy(payload["blindSeat"]),
    }
    _validate_prepared_completion(row, prior)
    return row


def finalize_council_v2(
    prepared: Mapping[str, Any],
    *,
    prior_rows: Iterable[Mapping[str, Any]],
    clock: Clock,
) -> dict[str, Any]:
    """Sample the system boundary and strictly validate a prepared completion."""

    prior = list(prior_rows)
    prepared = _require_mapping(prepared, "prepared council-v2")
    _validate_prepared_completion(prepared, prior)
    row = deepcopy(dict(prepared))
    row["finalizedAt"] = _clock_timestamp(clock, "finalizedAt")
    validate_v2_record(row, prior)
    return row


def make_council_v2(
    payload: Mapping[str, Any],
    *,
    prior_rows: Iterable[Mapping[str, Any]],
    clock: Clock,
) -> dict[str, Any]:
    """Construct a completion directly for schema-only callers.

    Integrated capture uses :func:`prepare_council_v2` and
    :func:`finalize_council_v2` separately so custody checks finish before the
    append-boundary clock is sampled.
    """

    prior = list(prior_rows)
    prepared = prepare_council_v2(payload, prior_rows=prior)
    return finalize_council_v2(prepared, prior_rows=prior, clock=clock)


def make_finding_audit_case_v2(
    payload: Mapping[str, Any],
    *,
    prior_rows: Iterable[Mapping[str, Any]],
    clock: Clock,
) -> dict[str, Any]:
    """Bind the first selected family completion to blinded audit artifacts."""

    payload = _require_payload_keys(
        payload,
        required={
            "runId",
            "protocolSha256",
            "auditCaseSha256",
            "caseArtifact",
            "aliasMapArtifact",
        },
        optional=None,
        kind="finding-audit-case-v2",
    )
    prior = list(prior_rows)
    completion = _one_by(prior, "council-v2", "runId", payload["runId"])
    if completion is None:
        raise CaptureSchemaError("finding-audit-case-v2 has no prior completion")
    row = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "finding-audit-case-v2",
        "activationId": completion["activationId"],
        "runId": completion["runId"],
        "decisionFamilyId": completion["decisionFamilyId"],
        "recordedAt": _clock_timestamp(clock, "recordedAt"),
        "protocolSha256": payload["protocolSha256"],
        "auditCaseSha256": payload["auditCaseSha256"],
        "caseArtifact": deepcopy(payload["caseArtifact"]),
        "aliasMapArtifact": deepcopy(payload["aliasMapArtifact"]),
    }
    validate_v2_record(row, prior)
    return row


def make_capture_invalidation(
    payload: Mapping[str, Any],
    *,
    prior_rows: Iterable[Mapping[str, Any]],
    clock: Clock,
    id_factory: IdFactory = new_v2_id,
) -> dict[str, Any]:
    payload = _require_payload_keys(
        payload,
        required={"runId", "reason", "operator", "evidenceRef"},
        optional=None,
        kind="capture-invalidation",
    )
    row = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "capture-invalidation",
        "invalidationId": id_factory("invalidation"),
        "runId": payload["runId"],
        "reason": payload["reason"],
        "operator": payload["operator"],
        "invalidatedAt": _clock_timestamp(clock, "invalidatedAt"),
        "evidenceRef": payload["evidenceRef"],
    }
    validate_v2_record(row, prior_rows)
    return row
