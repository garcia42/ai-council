"""Strict, read-only evaluation of evidence required for V2 activation.

The evaluator intentionally has no ledger or filesystem write capability.  It
accepts exact manifest bytes plus an injected content-addressed reader (for
example :class:`council_tools.artifacts.ArtifactStore`) and returns a verdict.
Status strings inside caller-supplied documents are not part of the contract.

The activation verdict is evaluated at the historical activation boundary.
Current health is evaluated independently at ``as_of`` so an expired backup
certificate cannot rewrite history and a formerly valid activation cannot make
stale controls look healthy today.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol, Sequence

from .artifacts import secret_detectors, validate_artifact_ref
from .capture_schema import strict_json_loads
from .finding_audit import (
    AGREEMENT_METRICS,
    FindingAuditError,
    audit_protocol_sha256,
    validate_audit_protocol,
    validate_protocol_rehearsal_certificate,
)
from .offhost_durability import (
    OffHostDurabilityError,
    verify_durability_certificate,
)


SCHEMA_VERSION = 2
POLICY_SCHEMA_VERSION = 1
CERTIFICATE_SCHEMA_VERSION = 1
CONTROL_KEYS = (
    "cleanRestore",
    "encryptionAccess",
    "independentAuditProtocol",
    "offHostCustody",
    "remoteReadback",
    "retention",
    "rpoRto",
    "timedRehearsal",
)
AUDIT_CONTROL_KEY = "independentAuditProtocol"
DURABILITY_CONTROL_KEYS = tuple(
    key for key in CONTROL_KEYS if key != AUDIT_CONTROL_KEY
)

_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DECIMAL_GENERATION = re.compile(r"[1-9][0-9]*\Z")
_MAX_JSON_BYTES = 1024 * 1024


class VerifiedArtifactReader(Protocol):
    """The read-only subset of :class:`ArtifactStore` used here."""

    def read_verified(self, ref: Mapping[str, Any]) -> bytes:
        """Return exact bytes for an already content-addressed reference."""


class ActivationEvidenceError(ValueError):
    """Strict parse failure with a stable, non-reflective blocker code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise ActivationEvidenceError(code)


def _exact(value: Any, keys: set[str] | frozenset[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        _fail(code)
    return value


def _text(value: Any, code: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value.strip() != value
        or any(ord(character) < 0x20 for character in value)
    ):
        _fail(code)
    return value


def _identifier(value: Any, code: str) -> str:
    result = _text(value, code, maximum=200)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", result):
        _fail(code)
    return result


def _commit(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _GIT_COMMIT.fullmatch(value):
        _fail(code)
    return value


def _digest(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _HEX_64.fullmatch(value):
        _fail(code)
    return value


def _integer(
    value: Any,
    code: str,
    *,
    minimum: int = 0,
    maximum: int = 10**9,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        _fail(code)
    return value


def _number(
    value: Any,
    code: str,
    *,
    minimum: float = 0.0,
    maximum: float = 10**12,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < minimum
        or value > maximum
    ):
        _fail(code)
    return float(value)


def _true(value: Any, code: str) -> None:
    if value is not True:
        _fail(code)


def _false(value: Any, code: str) -> None:
    if value is not False:
        _fail(code)


def _parse_time(value: Any, code: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        _fail(code)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _fail(code)
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _fail(code)
    return parsed.astimezone(timezone.utc)


def _api_time(value: str | datetime, field: str) -> datetime:
    if isinstance(value, str):
        try:
            return _parse_time(value, field)
        except ActivationEvidenceError as exc:
            raise ValueError(f"{field} must be an RFC3339 UTC timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _strict_json(data: bytes | bytearray | memoryview | str, code: str) -> Any:
    if isinstance(data, memoryview):
        data = data.tobytes()
    if isinstance(data, (bytes, bytearray)) and len(data) > _MAX_JSON_BYTES:
        _fail(code)
    try:
        return strict_json_loads(data)
    except (TypeError, ValueError):
        _fail(code)


def _normalize_ref(value: Any, code: str) -> dict[str, Any]:
    try:
        return validate_artifact_ref(value)
    except (TypeError, ValueError):
        _fail(code)


def parse_activation_manifest_v2(
    data: bytes | bytearray | memoryview | str,
) -> dict[str, Any]:
    """Decode the exact activation-manifest-v2 structure.

    This function only validates the manifest envelope.  Referenced artifacts
    are authenticated and semantically evaluated by
    :func:`evaluate_activation_evidence`.
    """

    if isinstance(data, str):
        secret_input = data.encode("utf-8", errors="strict")
    elif isinstance(data, memoryview):
        secret_input = data.tobytes()
    else:
        secret_input = bytes(data)
    if secret_detectors(secret_input):
        _fail("manifest-secret-detected")
    decoded = _strict_json(data, "manifest-invalid-json")
    manifest = _exact(
        decoded,
        {
            "schemaVersion",
            "activationId",
            "runtimeSourceCommit",
            "runtimeSourceSha256",
            "issuedAt",
            "expiresAt",
            "policyRef",
            "controls",
        },
        "manifest-invalid-schema",
    )
    if manifest["schemaVersion"] != SCHEMA_VERSION:
        _fail("manifest-invalid-version")
    activation_id = _identifier(manifest["activationId"], "manifest-invalid-schema")
    commit = _commit(manifest["runtimeSourceCommit"], "manifest-invalid-schema")
    source_sha = _digest(manifest["runtimeSourceSha256"], "manifest-invalid-schema")
    issued = _parse_time(manifest["issuedAt"], "manifest-invalid-schema")
    expires = _parse_time(manifest["expiresAt"], "manifest-invalid-schema")
    if expires <= issued:
        _fail("manifest-invalid-validity-window")
    policy_ref = _normalize_ref(manifest["policyRef"], "manifest-invalid-reference")
    controls = _exact(
        manifest["controls"], frozenset(CONTROL_KEYS), "manifest-invalid-controls"
    )
    normalized_controls = {
        key: _normalize_ref(controls[key], "manifest-invalid-reference")
        for key in CONTROL_KEYS
    }
    audit_ref = normalized_controls[AUDIT_CONTROL_KEY]
    durability_ref = normalized_controls[DURABILITY_CONTROL_KEYS[0]]
    if any(normalized_controls[key] != durability_ref for key in DURABILITY_CONTROL_KEYS):
        _fail("manifest-durability-reference-mismatch")
    if audit_ref == durability_ref:
        _fail("manifest-control-reference-alias")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "activationId": activation_id,
        "runtimeSourceCommit": commit,
        "runtimeSourceSha256": source_sha,
        "issuedAt": issued,
        "expiresAt": expires,
        "policyRef": policy_ref,
        "auditRef": audit_ref,
        "durabilityRef": durability_ref,
        "controls": normalized_controls,
    }


def _read_artifact(
    reader: VerifiedArtifactReader,
    ref: Mapping[str, Any],
    label: str,
) -> tuple[bytes, Mapping[str, Any]]:
    normalized = _normalize_ref(ref, f"{label}-invalid-reference")
    try:
        content = reader.read_verified(normalized)
    except (KeyError, OSError, TypeError, ValueError):
        _fail(f"{label}-artifact-unreadable")
    if not isinstance(content, (bytes, bytearray, memoryview)):
        _fail(f"{label}-artifact-unreadable")
    exact = bytes(content)
    if (
        len(exact) != normalized["bytes"]
        or hashlib.sha256(exact).hexdigest() != normalized["sha256"]
    ):
        _fail(f"{label}-artifact-integrity")
    if secret_detectors(exact):
        _fail(f"{label}-artifact-secret-detected")
    decoded = _strict_json(exact, f"{label}-artifact-invalid-json")
    if not isinstance(decoded, Mapping):
        _fail(f"{label}-invalid-schema")
    return exact, decoded


def _read_raw_artifact(
    reader: VerifiedArtifactReader,
    ref: Mapping[str, Any],
    label: str,
) -> bytes:
    normalized = _normalize_ref(ref, f"{label}-invalid-reference")
    try:
        content = reader.read_verified(normalized)
    except (KeyError, OSError, TypeError, ValueError):
        _fail(f"{label}-artifact-unreadable")
    if not isinstance(content, (bytes, bytearray, memoryview)):
        _fail(f"{label}-artifact-unreadable")
    exact = bytes(content)
    if (
        len(exact) != normalized["bytes"]
        or hashlib.sha256(exact).hexdigest() != normalized["sha256"]
    ):
        _fail(f"{label}-artifact-integrity")
    if secret_detectors(exact):
        _fail(f"{label}-artifact-secret-detected")
    return exact


def _validate_common_binding(
    artifact: Mapping[str, Any],
    *,
    expected_kind: str,
    commit: str,
    source_sha: str,
    schema_code: str,
    source_code: str,
) -> tuple[datetime, datetime]:
    if artifact.get("schemaVersion") != CERTIFICATE_SCHEMA_VERSION:
        _fail(schema_code)
    if artifact.get("kind") != expected_kind:
        _fail(schema_code)
    if artifact.get("runtimeSourceCommit") != commit:
        _fail(source_code)
    if artifact.get("runtimeSourceSha256") != source_sha:
        _fail(source_code)
    issued = _parse_time(artifact.get("issuedAt"), schema_code)
    expires = _parse_time(artifact.get("expiresAt"), schema_code)
    if expires <= issued:
        _fail(schema_code)
    return issued, expires


def _validate_remote_object(
    value: Any, *, prefix: str, code: str
) -> dict[str, Any]:
    item = _exact(value, {"name", "generation", "sha256", "bytes"}, code)
    name = _text(item["name"], code, maximum=1024)
    if not name.startswith(prefix + "/") or "/../" in f"/{name}/" or "//" in name:
        _fail(code)
    generation = item["generation"]
    if not isinstance(generation, str) or not _DECIMAL_GENERATION.fullmatch(generation):
        _fail(code)
    return {
        "name": name,
        "generation": generation,
        "sha256": _digest(item["sha256"], code),
        "bytes": _integer(item["bytes"], code),
    }


def _validate_durability_certificate(
    artifact: Mapping[str, Any],
    *,
    commit: str,
    source_sha: str,
    policy: Mapping[str, Any],
    policy_sha: str,
) -> dict[str, Any]:
    certificate = _exact(
        artifact,
        {
            "schemaVersion",
            "kind",
            "certificateId",
            "runtimeSourceCommit",
            "runtimeSourceSha256",
            "policySha256",
            "issuedAt",
            "expiresAt",
            "provider",
            "bucket",
            "prefix",
            "snapshot",
            "upload",
            "remoteObjects",
            "indexObject",
            "readback",
            "restore",
            "elapsedSeconds",
            "failureDomainCaveatAcknowledged",
        },
        "durability-invalid-schema",
    )
    issued, expires = _validate_common_binding(
        certificate,
        expected_kind="off-host-durability-certificate",
        commit=commit,
        source_sha=source_sha,
        schema_code="durability-invalid-schema",
        source_code="durability-runtime-source-mismatch",
    )
    _identifier(certificate["certificateId"], "durability-invalid-schema")
    if certificate["policySha256"] != policy_sha:
        _fail("durability-policy-mismatch")
    if certificate["provider"] != "gcs":
        _fail("durability-provider-policy-mismatch")
    _true(
        certificate["failureDomainCaveatAcknowledged"],
        "durability-policy-mismatch",
    )

    bucket = _exact(
        certificate["bucket"],
        {
            "uri",
            "private",
            "publicAccessPrevention",
            "uniformBucketAccess",
            "versioning",
            "encryptionAtRest",
            "retentionPolicy",
            "automaticApplicationDeletion",
        },
        "durability-invalid-schema",
    )
    uri = _text(bucket["uri"], "durability-invalid-schema", maximum=1024)
    if not re.fullmatch(r"gs://[a-z0-9][a-z0-9._-]{1,221}[a-z0-9]", uri):
        _fail("durability-invalid-schema")
    _true(bucket["private"], "durability-encryption-access-not-proven")
    if bucket["publicAccessPrevention"] != "enforced":
        _fail("durability-encryption-access-not-proven")
    _true(bucket["uniformBucketAccess"], "durability-encryption-access-not-proven")
    _true(bucket["versioning"], "durability-off-host-custody-not-proven")
    if bucket["encryptionAtRest"] not in {"provider-managed", "customer-managed"}:
        _fail("durability-encryption-access-not-proven")
    retention = _exact(
        bucket["retentionPolicy"], {"configured", "days", "locked"}, "durability-invalid-schema"
    )
    _true(retention["configured"], "durability-retention-not-proven")
    if retention["days"] != policy["retentionDays"]:
        _fail("durability-policy-mismatch")
    if not isinstance(retention["locked"], bool):
        _fail("durability-invalid-schema")
    _false(bucket["automaticApplicationDeletion"], "durability-retention-not-proven")

    prefix = _text(certificate["prefix"], "durability-invalid-schema", maximum=768)
    if prefix.startswith("/") or prefix.endswith("/") or ".." in prefix.split("/"):
        _fail("durability-invalid-schema")
    snapshot = _exact(
        certificate["snapshot"],
        {"manifestSha256", "manifestObjectName", "cutAt", "bytes"},
        "durability-invalid-schema",
    )
    manifest_sha = _digest(snapshot["manifestSha256"], "durability-invalid-schema")
    manifest_name = _text(snapshot["manifestObjectName"], "durability-invalid-schema")
    cut = _parse_time(snapshot["cutAt"], "durability-invalid-schema")
    snapshot_bytes = _integer(snapshot["bytes"], "durability-invalid-schema", minimum=1)

    upload = _exact(
        certificate["upload"],
        {"startedAt", "completedAt", "uploadedBytes"},
        "durability-invalid-schema",
    )
    upload_start = _parse_time(upload["startedAt"], "durability-invalid-schema")
    upload_end = _parse_time(upload["completedAt"], "durability-invalid-schema")
    uploaded_bytes = _integer(upload["uploadedBytes"], "durability-invalid-schema", minimum=1)

    objects = certificate["remoteObjects"]
    if not isinstance(objects, list) or not objects:
        _fail("durability-object-chain-invalid")
    normalized_objects = [
        _validate_remote_object(item, prefix=prefix, code="durability-object-chain-invalid")
        for item in objects
    ]
    object_identities = [(item["name"], item["generation"]) for item in normalized_objects]
    if len(set(object_identities)) != len(object_identities) or len(
        {item["name"] for item in normalized_objects}
    ) != len(normalized_objects):
        _fail("durability-object-chain-invalid")
    if sum(item["bytes"] for item in normalized_objects) != snapshot_bytes:
        _fail("durability-object-chain-invalid")
    manifest_objects = [item for item in normalized_objects if item["name"] == manifest_name]
    if len(manifest_objects) != 1 or manifest_objects[0]["sha256"] != manifest_sha:
        _fail("durability-object-chain-invalid")

    index_object = _validate_remote_object(
        certificate["indexObject"], prefix=prefix, code="durability-object-chain-invalid"
    )
    if index_object["name"] in {item["name"] for item in normalized_objects}:
        _fail("durability-object-chain-invalid")
    if index_object["name"] != prefix + "/index.json":
        _fail("durability-object-chain-invalid")
    if uploaded_bytes != snapshot_bytes + index_object["bytes"]:
        _fail("durability-object-chain-invalid")

    readback = _exact(
        certificate["readback"],
        {
            "startedAt",
            "completedAt",
            "generationPinned",
            "indexGeneration",
            "downloadedBytes",
            "manifestVerified",
        },
        "durability-invalid-schema",
    )
    readback_start = _parse_time(readback["startedAt"], "durability-invalid-schema")
    readback_end = _parse_time(readback["completedAt"], "durability-invalid-schema")
    _true(readback["generationPinned"], "durability-readback-not-proven")
    if readback["indexGeneration"] != index_object["generation"]:
        _fail("durability-object-chain-invalid")
    if readback["downloadedBytes"] != uploaded_bytes:
        _fail("durability-object-chain-invalid")
    _true(readback["manifestVerified"], "durability-readback-not-proven")

    restore = _exact(
        certificate["restore"],
        {
            "startedAt",
            "completedAt",
            "cleanTarget",
            "snapshotVerified",
            "restoredEvidenceValidated",
            "restoredBytes",
        },
        "durability-invalid-schema",
    )
    restore_start = _parse_time(restore["startedAt"], "durability-invalid-schema")
    restore_end = _parse_time(restore["completedAt"], "durability-invalid-schema")
    for key in ("cleanTarget", "snapshotVerified", "restoredEvidenceValidated"):
        _true(restore[key], "durability-restore-not-proven")
    if restore["restoredBytes"] != snapshot_bytes:
        _fail("durability-object-chain-invalid")

    if not (
        cut <= upload_start <= upload_end <= readback_start <= readback_end
        <= restore_start <= restore_end <= issued
    ):
        _fail("durability-invalid-event-order")
    computed_elapsed = (restore_end - upload_start).total_seconds()
    supplied_elapsed = _number(certificate["elapsedSeconds"], "durability-invalid-schema")
    if abs(computed_elapsed - supplied_elapsed) > 0.001:
        _fail("durability-elapsed-mismatch")
    if computed_elapsed > policy["rtoSeconds"]:
        _fail("durability-rto-exceeded")
    return {
        "issuedAt": issued,
        "expiresAt": expires,
        "evidenceAt": restore_end,
        "snapshotAt": cut,
        "elapsedSeconds": computed_elapsed,
    }


def _validate_native_activation_policy(
    artifact: Mapping[str, Any], commit: str, source_sha: str
) -> dict[str, Any]:
    """Validate the small source-bound policy which links native controls."""

    policy = _exact(
        artifact,
        {
            "schemaVersion",
            "kind",
            "policyId",
            "runtimeSourceCommit",
            "runtimeSourceSha256",
            "issuedAt",
            "expiresAt",
            "maxClockSkewSeconds",
            "maxCertificateAgeSeconds",
            "auditProtocolRef",
            "durabilityPolicyRef",
            "requiredControls",
        },
        "policy-invalid-schema",
    )
    issued, expires = _validate_common_binding(
        policy,
        expected_kind="activation-evidence-policy",
        commit=commit,
        source_sha=source_sha,
        schema_code="policy-invalid-schema",
        source_code="policy-runtime-source-mismatch",
    )
    _identifier(policy["policyId"], "policy-invalid-schema")
    controls = policy["requiredControls"]
    if not isinstance(controls, list) or controls != list(CONTROL_KEYS):
        _fail("policy-invalid-controls")
    return {
        "issuedAt": issued,
        "expiresAt": expires,
        "maxClockSkewSeconds": _integer(
            policy["maxClockSkewSeconds"], "policy-invalid-schema", maximum=3600
        ),
        "maxCertificateAgeSeconds": _integer(
            policy["maxCertificateAgeSeconds"], "policy-invalid-schema", minimum=1
        ),
        "auditProtocolRef": _normalize_ref(
            policy["auditProtocolRef"], "policy-invalid-reference"
        ),
        "durabilityPolicyRef": _normalize_ref(
            policy["durabilityPolicyRef"], "policy-invalid-reference"
        ),
    }


def _validate_native_audit_protocol(artifact: Mapping[str, Any]) -> dict[str, Any]:
    try:
        protocol = validate_audit_protocol(artifact)
    except FindingAuditError:
        _fail("audit-protocol-invalid")
    selection = protocol["selection"]
    adjudication = protocol["adjudication"]
    prospective = protocol["prospective"]
    if (
        selection["algorithm"] != "sha256-domain-v1"
        or selection["modulus"] != 5
        or not 0 <= selection["residue"] < 5
        or not selection["domainSeparator"]
        or adjudication["requiredAdjudicators"] != 2
        or adjudication["minimumAgreement"] != 0.60
        or adjudication["agreementMetrics"] != list(AGREEMENT_METRICS)
        or prospective["backfillAllowed"] is not False
        or prospective["assignmentMoment"] != "before-first-attempt"
        or prospective["retryPolicy"] != "inherit-first-family-assignment"
        or prospective["denominatorEffect"] != "none"
    ):
        _fail("audit-protocol-policy-mismatch")
    return protocol


def _validate_native_durability_policy(artifact: Mapping[str, Any]) -> dict[str, Any]:
    policy = _exact(
        artifact,
        {
            "accessPosture",
            "automaticApplicationDeletion",
            "encryptionAccessPosture",
            "failureDomainCaveats",
            "failureDomainCaveatAcknowledged",
            "maxRestoreEvidenceAgeSeconds",
            "maxSnapshotAgeSeconds",
            "retentionSeconds",
            "rpoSeconds",
            "rtoSeconds",
            "schemaVersion",
            "targetUri",
        },
        "durability-policy-invalid",
    )
    if policy["schemaVersion"] != 1:
        _fail("durability-policy-invalid")
    target = _text(policy["targetUri"], "durability-policy-invalid", maximum=1024)
    if not re.fullmatch(r"gs://[a-z0-9][a-z0-9._-]{1,221}[a-z0-9]", target):
        _fail("durability-policy-invalid")
    access = _text(policy["accessPosture"], "durability-policy-invalid")
    encryption_access = _text(
        policy["encryptionAccessPosture"], "durability-policy-invalid"
    )
    _text(policy["failureDomainCaveats"], "durability-policy-invalid", maximum=1000)
    _true(
        policy["failureDomainCaveatAcknowledged"],
        "durability-policy-invalid",
    )
    _false(
        policy["automaticApplicationDeletion"],
        "durability-retention-not-proven",
    )
    retention_seconds = _integer(
        policy["retentionSeconds"], "durability-policy-invalid", minimum=86400
    )
    if retention_seconds % 86400:
        _fail("durability-policy-invalid")
    return {
        "document": dict(policy),
        "targetUri": target,
        "accessPosture": access,
        "encryptionAccessPosture": encryption_access,
        "maxRestoreEvidenceAgeSeconds": _integer(
            policy["maxRestoreEvidenceAgeSeconds"],
            "durability-policy-invalid",
            minimum=1,
        ),
        "maxSnapshotAgeSeconds": _integer(
            policy["maxSnapshotAgeSeconds"], "durability-policy-invalid", minimum=1
        ),
        "retentionSeconds": retention_seconds,
        "rpoSeconds": _integer(
            policy["rpoSeconds"], "durability-policy-invalid", minimum=1
        ),
        "rtoSeconds": _integer(
            policy["rtoSeconds"], "durability-policy-invalid", minimum=1
        ),
    }


def _validate_native_audit_certificate(
    artifact: Mapping[str, Any],
    *,
    protocol: Mapping[str, Any],
    commit: str,
    source_sha: str,
    maximum_age: int,
) -> dict[str, Any]:
    try:
        certificate = validate_protocol_rehearsal_certificate(
            artifact, protocol=protocol
        )
    except FindingAuditError:
        _fail("audit-certificate-invalid")
    if certificate["runtimeCommit"] != commit or certificate["sourceTreeSha256"] != source_sha:
        _fail("audit-runtime-source-mismatch")
    if certificate["protocolSha256"] != audit_protocol_sha256(protocol):
        _fail("audit-protocol-mismatch")
    if certificate["frozenProtocolArtifact"] != protocol["frozenProtocolArtifact"]:
        _fail("audit-protocol-mismatch")
    # The native certificate proves these checks by replay, not by a caller's
    # approval label.  The native validator also checks its self-digest and
    # requires every actual prospective count to remain zero.
    if any(value is not True for value in certificate["checks"].values()):
        _fail("audit-rehearsal-incomplete")
    if any(value != 0 for value in certificate["actualProspectiveCounts"].values()):
        _fail("audit-backfill-detected")
    rehearsed = _parse_time(certificate["rehearsedAt"], "audit-certificate-invalid")
    return {
        "issuedAt": rehearsed,
        "expiresAt": rehearsed + timedelta(seconds=maximum_age),
        "evidenceAt": rehearsed,
    }


def _validate_aligned_durability_certificate(
    certificate_bytes: bytes,
    artifact: Mapping[str, Any],
    *,
    commit: str,
    source_sha: str,
    activation_policy_sha: str,
    durability_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the canonical certificate emitted by offhost_durability."""

    try:
        verified = verify_durability_certificate(
            certificate_bytes,
            expected_runtime_commit=commit,
            expected_source_tree_sha256=source_sha,
        )
    except OffHostDurabilityError as exc:
        if exc.code in {"runtime-commit-mismatch", "source-tree-mismatch"}:
            _fail("durability-runtime-source-mismatch")
        _fail("durability-certificate-invalid")
    if dict(verified.document) != dict(artifact):
        _fail("durability-certificate-invalid")
    bucket = artifact.get("bucket")
    if not isinstance(bucket, Mapping) or bucket.get("uri") != durability_policy["targetUri"]:
        _fail("durability-provider-policy-mismatch")
    evaluator_policy = {
        "retentionDays": durability_policy["retentionSeconds"] // 86400,
        "rtoSeconds": durability_policy["rtoSeconds"],
    }
    validated = _validate_durability_certificate(
        artifact,
        commit=commit,
        source_sha=source_sha,
        policy=evaluator_policy,
        policy_sha=activation_policy_sha,
    )
    expected_expiry = min(
        validated["snapshotAt"]
        + timedelta(seconds=durability_policy["maxSnapshotAgeSeconds"]),
        validated["evidenceAt"]
        + timedelta(seconds=durability_policy["maxRestoreEvidenceAgeSeconds"]),
    )
    if validated["expiresAt"] != expected_expiry:
        _fail("durability-expiry-mismatch")
    return validated


def _time_blockers(
    *,
    at: datetime,
    manifest: Mapping[str, Any],
    policy: Mapping[str, Any],
    audit: Mapping[str, Any],
    durability: Mapping[str, Any],
) -> list[str]:
    blockers: list[str] = []
    skew = policy["maxClockSkewSeconds"]
    for label, item in (
        ("manifest", manifest),
        ("policy", policy),
        ("audit", audit),
        ("durability", durability),
    ):
        issued = item["issuedAt"]
        if (issued - at).total_seconds() > skew:
            blockers.append(f"{label}-issued-in-future")
        if at >= item["expiresAt"]:
            blockers.append(f"{label}-expired")
        if (at - issued).total_seconds() > policy["maxCertificateAgeSeconds"]:
            blockers.append(f"{label}-stale")
    for label, item in (("audit", audit), ("durability", durability)):
        if (item["evidenceAt"] - at).total_seconds() > skew:
            blockers.append(f"{label}-evidence-in-future")
    snapshot_age = (at - durability["snapshotAt"]).total_seconds()
    if snapshot_age > policy["snapshotMaxAgeSeconds"]:
        blockers.append("durability-snapshot-stale")
    if snapshot_age > policy["rpoSeconds"]:
        blockers.append("durability-rpo-exceeded")
    if (at - durability["evidenceAt"]).total_seconds() > policy["restoreEvidenceMaxAgeSeconds"]:
        blockers.append("durability-restore-evidence-stale")
    return sorted(set(blockers))


def _base_result(
    *,
    activation_time: datetime,
    as_of: datetime,
    expected_runtime_commit: str,
    expected_source_sha256: str,
    blockers: Sequence[str],
    activation_id: str | None = None,
) -> dict[str, Any]:
    normalized = sorted(set(blockers))
    return {
        "schemaVersion": 1,
        "activationId": activation_id,
        "runtimeSourceCommit": expected_runtime_commit,
        "runtimeSourceSha256": expected_source_sha256,
        "activationVerdict": {
            "evaluatedAt": _timestamp(activation_time),
            "ready": not normalized,
            "blockers": normalized,
        },
        "currentHealth": {
            "asOf": _timestamp(as_of),
            "healthy": not normalized,
            "blockers": normalized,
        },
        "appendReady": False,
        "blockers": normalized,
    }


def evaluate_activation_evidence(
    manifest_data: bytes | bytearray | memoryview | str,
    *,
    reader: VerifiedArtifactReader,
    expected_runtime_commit: str,
    expected_source_sha256: str,
    activation_time: str | datetime,
    as_of: str | datetime | None = None,
) -> dict[str, Any]:
    """Authenticate and evaluate a complete activation evidence chain.

    Evidence failures are represented as stable blocker codes; they never
    authorize a partial append.  Invalid trusted API parameters (the installed
    source binding or evaluation clocks) raise :class:`ValueError`.
    """

    if not isinstance(expected_runtime_commit, str) or not _GIT_COMMIT.fullmatch(
        expected_runtime_commit
    ):
        raise ValueError("expected_runtime_commit must be a lowercase Git object id")
    if not isinstance(expected_source_sha256, str) or not _HEX_64.fullmatch(
        expected_source_sha256
    ):
        raise ValueError("expected_source_sha256 must be a lowercase SHA-256 digest")
    activation_at = _api_time(activation_time, "activation_time")
    current_at = activation_at if as_of is None else _api_time(as_of, "as_of")
    if current_at < activation_at:
        raise ValueError("as_of must not precede activation_time")

    try:
        manifest = parse_activation_manifest_v2(manifest_data)
    except ActivationEvidenceError as exc:
        return _base_result(
            activation_time=activation_at,
            as_of=current_at,
            expected_runtime_commit=expected_runtime_commit,
            expected_source_sha256=expected_source_sha256,
            blockers=[exc.code],
        )
    activation_id = manifest["activationId"]
    fixed_blockers: list[str] = []
    if manifest["runtimeSourceCommit"] != expected_runtime_commit:
        fixed_blockers.append("manifest-runtime-source-mismatch")
    if manifest["runtimeSourceSha256"] != expected_source_sha256:
        fixed_blockers.append("manifest-runtime-source-mismatch")
    if fixed_blockers:
        return _base_result(
            activation_time=activation_at,
            as_of=current_at,
            expected_runtime_commit=expected_runtime_commit,
            expected_source_sha256=expected_source_sha256,
            blockers=fixed_blockers,
            activation_id=activation_id,
        )

    try:
        _policy_bytes, policy_artifact = _read_artifact(
            reader, manifest["policyRef"], "policy"
        )
        activation_policy = _validate_native_activation_policy(
            policy_artifact, expected_runtime_commit, expected_source_sha256
        )
        _audit_protocol_bytes, audit_protocol_artifact = _read_artifact(
            reader, activation_policy["auditProtocolRef"], "audit-protocol"
        )
        audit_protocol = _validate_native_audit_protocol(audit_protocol_artifact)
        _read_raw_artifact(
            reader,
            audit_protocol["frozenProtocolArtifact"],
            "audit-frozen-protocol",
        )
        _durability_policy_bytes, durability_policy_artifact = _read_artifact(
            reader,
            activation_policy["durabilityPolicyRef"],
            "durability-policy",
        )
        durability_policy = _validate_native_durability_policy(
            durability_policy_artifact
        )
        policy = {
            **activation_policy,
            "snapshotMaxAgeSeconds": durability_policy["maxSnapshotAgeSeconds"],
            "restoreEvidenceMaxAgeSeconds": durability_policy[
                "maxRestoreEvidenceAgeSeconds"
            ],
            "rpoSeconds": durability_policy["rpoSeconds"],
            "rtoSeconds": durability_policy["rtoSeconds"],
        }
        _audit_bytes, audit_artifact = _read_artifact(
            reader, manifest["auditRef"], "audit"
        )
        durability_bytes, durability_artifact = _read_artifact(
            reader, manifest["durabilityRef"], "durability"
        )
        audit = _validate_native_audit_certificate(
            audit_artifact,
            protocol=audit_protocol,
            commit=expected_runtime_commit,
            source_sha=expected_source_sha256,
            maximum_age=activation_policy["maxCertificateAgeSeconds"],
        )
        durability = _validate_aligned_durability_certificate(
            durability_bytes,
            durability_artifact,
            commit=expected_runtime_commit,
            source_sha=expected_source_sha256,
            activation_policy_sha=manifest["policyRef"]["sha256"],
            durability_policy=durability_policy,
        )
    except ActivationEvidenceError as exc:
        return _base_result(
            activation_time=activation_at,
            as_of=current_at,
            expected_runtime_commit=expected_runtime_commit,
            expected_source_sha256=expected_source_sha256,
            blockers=[exc.code],
            activation_id=activation_id,
        )

    activation_blockers = _time_blockers(
        at=activation_at,
        manifest=manifest,
        policy=policy,
        audit=audit,
        durability=durability,
    )
    current_blockers = _time_blockers(
        at=current_at,
        manifest=manifest,
        policy=policy,
        audit=audit,
        durability=durability,
    )
    all_blockers = sorted(set(activation_blockers + current_blockers))
    activation_ready = not activation_blockers
    current_healthy = not current_blockers
    return {
        "schemaVersion": 1,
        "activationId": activation_id,
        "runtimeSourceCommit": expected_runtime_commit,
        "runtimeSourceSha256": expected_source_sha256,
        "policySha256": manifest["policyRef"]["sha256"],
        "auditProtocolSha256": policy["auditProtocolRef"]["sha256"],
        "durabilityPolicySha256": policy["durabilityPolicyRef"]["sha256"],
        "auditCertificateSha256": manifest["auditRef"]["sha256"],
        "durabilityCertificateSha256": manifest["durabilityRef"]["sha256"],
        "activationVerdict": {
            "evaluatedAt": _timestamp(activation_at),
            "ready": activation_ready,
            "blockers": activation_blockers,
        },
        "currentHealth": {
            "asOf": _timestamp(current_at),
            "healthy": current_healthy,
            "blockers": current_blockers,
        },
        "appendReady": activation_ready and current_healthy,
        "blockers": all_blockers,
    }


__all__ = [
    "AUDIT_CONTROL_KEY",
    "ActivationEvidenceError",
    "CERTIFICATE_SCHEMA_VERSION",
    "CONTROL_KEYS",
    "DURABILITY_CONTROL_KEYS",
    "POLICY_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "VerifiedArtifactReader",
    "evaluate_activation_evidence",
    "parse_activation_manifest_v2",
]
