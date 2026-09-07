"""Re-evaluate immutable activation evidence and an additive renewal chain."""

from datetime import datetime
from collections.abc import Mapping, Sequence
from typing import Any

from .activation_evidence import VerifiedArtifactReader, evaluate_activation_evidence, parse_activation_manifest_v2
from .artifacts import ArtifactError
from .capture_schema import strict_json_loads, validate_v2_record


def _duration(document: Mapping[str, Any]):
    def timestamp(value):
        return value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return timestamp(document["expiresAt"]) - timestamp(document["issuedAt"])


def _policy_parameters(document: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items()
            if key not in {"policyId", "issuedAt", "expiresAt"}}


def evaluate_renewal_chain(
    activation: Mapping[str, Any], renewals: Sequence[Mapping[str, Any]], *,
    reader: VerifiedArtifactReader, as_of: datetime | str,
) -> dict[str, Any]:
    """Preserve historical validity; never skip a broken renewal to find green.

    Each renewal must have been ready at its append boundary. Only the final
    manifest supplies current health. Policy windows may be renewed but neither
    their duration nor any control parameter may be relaxed.
    """
    original_bytes = reader.read_verified(activation["approvalManifest"])
    binding = dict(expected_runtime_commit=activation["runtimeSourceCommit"],
                   expected_source_sha256=activation["runtimeSourceSha256"])
    original = evaluate_activation_evidence(
        original_bytes, reader=reader, **binding,
        activation_time=activation["activatedAt"], as_of=as_of)
    if not renewals:
        return original
    result = dict(original)
    try:
        if not original["activationVerdict"]["ready"]:
            raise ValueError("original activation evidence invalid")
        original_manifest = parse_activation_manifest_v2(original_bytes)
        original_policy = strict_json_loads(reader.read_verified(original_manifest["policyRef"]))
        prior = [activation]
        for renewal in renewals:
            if "_captureSchemaError" in renewal:
                raise ValueError("invalid renewal record")
            validate_v2_record(renewal, prior, now=as_of)
            data = reader.read_verified(renewal["approvalManifest"])
            manifest = parse_activation_manifest_v2(data)
            if manifest["activationId"] != activation["activationId"]:
                raise ValueError("renewal activation mismatch")
            policy = strict_json_loads(reader.read_verified(manifest["policyRef"]))
            if (_policy_parameters(policy) != _policy_parameters(original_policy)
                    or _duration(policy) > _duration(original_policy)
                    or _duration(manifest) > _duration(original_manifest)):
                raise ValueError("renewal changes frozen policy")
            current = evaluate_activation_evidence(
                data, reader=reader, **binding,
                activation_time=renewal["renewedAt"], as_of=as_of)
            if not current["activationVerdict"]["ready"]:
                raise ValueError("renewal was not ready at append time")
            prior.append(renewal)
        result["currentHealth"] = current["currentHealth"]
        result["blockers"] = current["currentHealth"]["blockers"]
        result["appendReady"] = current["currentHealth"]["healthy"]
        result["effectiveRenewalId"] = renewals[-1]["renewalId"]
    except (ArtifactError, KeyError, TypeError, ValueError):
        result["currentHealth"] = {"healthy": False, "blockers": ["renewal-chain-invalid"]}
        result["blockers"] = ["renewal-chain-invalid"]
        result["appendReady"] = False
    return result
