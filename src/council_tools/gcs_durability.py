"""Google Cloud Storage adapter for the off-host durability pipeline.

The adapter uses ``gcloud storage`` with fixed argv and no shell.  It neither
prints nor returns provider diagnostics.  Tests inject a command runner, while
production uses the same narrow runner boundary with the operator's existing
gcloud credential configuration.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

from .offhost_durability import (
    ObjectVersion,
    OffHostPolicyError,
    OffHostTransportError,
    StorageConfiguration,
)


_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9._-]{1,221}[a-z0-9]\Z")
_GENERATION_RE = re.compile(r"[1-9][0-9]*\Z")
_OBJECT_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


CommandRunner = Callable[..., Any]


def _strict_json(content: bytes, stage: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    try:
        value = json.loads(
            content,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise OffHostTransportError("invalid-provider-response", stage) from None
    if not isinstance(value, dict):
        raise OffHostTransportError("invalid-provider-response", stage)
    return value


def _lookup(value: Mapping[str, Any], *paths: Sequence[str]) -> Any:
    for path in paths:
        current: Any = value
        for part in path:
            if not isinstance(current, Mapping) or part not in current:
                break
            current = current[part]
        else:
            return current
    return None


def _retention_seconds(value: Any) -> int:
    if isinstance(value, bool):
        raise OffHostPolicyError("invalid-retention", "provider-configuration")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isdigit():
        result = int(value)
    elif isinstance(value, str) and value.endswith("s") and value[:-1].isdigit():
        result = int(value[:-1])
    else:
        raise OffHostPolicyError("invalid-retention", "provider-configuration")
    if result <= 0:
        raise OffHostPolicyError("invalid-retention", "provider-configuration")
    return result


def _bool(value: Any, code: str) -> bool:
    if value is not True and value is not False:
        raise OffHostPolicyError(code, "provider-configuration")
    return value


class GcsVersionedObjectStore:
    """Create-only GCS objects and read only explicitly returned generations."""

    def __init__(
        self,
        *,
        bucket: str,
        access_posture: str,
        automatic_application_deletion: bool = False,
        executable: str = "gcloud",
        runner: CommandRunner | None = None,
    ):
        if not isinstance(bucket, str) or not _BUCKET_RE.fullmatch(bucket):
            raise OffHostPolicyError("invalid-bucket", "gcs-configuration")
        if (
            not isinstance(access_posture, str)
            or not access_posture
            or len(access_posture) > 512
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in access_posture)
        ):
            raise OffHostPolicyError("invalid-access-posture", "gcs-configuration")
        if automatic_application_deletion is not False:
            raise OffHostPolicyError("automatic-deletion-enabled", "gcs-configuration")
        if not isinstance(executable, str) or not executable or "/" in executable:
            raise OffHostPolicyError("invalid-executable", "gcs-configuration")
        self._bucket = bucket
        self._access_posture = access_posture
        self._automatic_application_deletion = automatic_application_deletion
        self._executable = executable
        self._runner = runner or subprocess.run

    @property
    def bucket_uri(self) -> str:
        return f"gs://{self._bucket}"

    def _run(
        self,
        arguments: Sequence[str],
        *,
        input_data: bytes | None = None,
        stage: str,
    ) -> CommandResult:
        try:
            completed = self._runner(
                list(arguments),
                input=input_data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise OffHostTransportError("command-failed", stage) from None
        returncode = getattr(completed, "returncode", None)
        stdout = getattr(completed, "stdout", None)
        stderr = getattr(completed, "stderr", None)
        if (
            isinstance(returncode, bool)
            or not isinstance(returncode, int)
            or not isinstance(stdout, bytes)
            or not isinstance(stderr, bytes)
        ):
            raise OffHostTransportError("invalid-command-result", stage)
        if returncode != 0:
            # Provider output may contain object payload fragments, account
            # names, tokens, or credential paths.  It is intentionally not
            # reflected into this exception.
            raise OffHostTransportError("command-failed", stage)
        return CommandResult(returncode, stdout, stderr)

    def configuration(self) -> StorageConfiguration:
        result = self._run(
            (
                self._executable,
                "storage",
                "buckets",
                "describe",
                self.bucket_uri,
                "--format=json",
                "--quiet",
            ),
            stage="bucket-configuration",
        )
        metadata = _strict_json(result.stdout, "bucket-configuration")
        versioning = _lookup(
            metadata,
            ("versioning", "enabled"),
            ("versioning_enabled",),
            ("versioningEnabled",),
        )
        public_access_prevention = _lookup(
            metadata,
            ("iamConfiguration", "publicAccessPrevention"),
            ("public_access_prevention",),
            ("publicAccessPrevention",),
        )
        uniform_access = _lookup(
            metadata,
            ("iamConfiguration", "uniformBucketLevelAccess", "enabled"),
            ("uniform_bucket_level_access",),
            ("uniformBucketLevelAccess", "enabled"),
        )
        retention = _lookup(
            metadata,
            ("retentionPolicy", "retentionPeriod"),
            ("retention_policy", "retention_period"),
            ("retention_policy", "retentionPeriod"),
            ("retentionPeriod",),
        )
        retention_locked = _lookup(
            metadata,
            ("retentionPolicy", "isLocked"),
            ("retention_policy", "is_locked"),
            ("retention_policy", "isLocked"),
        )
        location = _lookup(metadata, ("location",))
        location_type = _lookup(metadata, ("locationType",), ("location_type",))
        if not isinstance(public_access_prevention, str):
            raise OffHostPolicyError(
                "invalid-public-access-prevention", "provider-configuration"
            )
        if not isinstance(location, str) or not location or len(location) > 128:
            raise OffHostPolicyError("invalid-failure-domain", "provider-configuration")
        if not isinstance(location_type, str) or not location_type or len(location_type) > 128:
            raise OffHostPolicyError("invalid-failure-domain", "provider-configuration")
        if any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in location + location_type
        ):
            raise OffHostPolicyError("invalid-failure-domain", "provider-configuration")
        kms_key = _lookup(
            metadata,
            ("encryption", "defaultKmsKeyName"),
            ("default_kms_key",),
            ("defaultKmsKeyName",),
        )
        encryption = "customer-managed" if isinstance(kms_key, str) and kms_key else "provider-managed"
        configuration = StorageConfiguration(
            provider="gcs",
            bucket_uri=self.bucket_uri,
            failure_domain=f"gcs:{location_type}:{location}",
            versioning_enabled=_bool(versioning, "invalid-versioning"),
            public_access_prevention=public_access_prevention,
            uniform_bucket_access=_bool(uniform_access, "invalid-uniform-access"),
            retention_seconds=_retention_seconds(retention),
            retention_locked=False
            if retention_locked is None
            else _bool(retention_locked, "invalid-retention-lock"),
            encryption_at_rest=encryption,
            access_posture=self._access_posture,
            automatic_application_deletion=self._automatic_application_deletion,
        )
        # Evaluate the hard minimum here so a caller cannot accidentally use
        # this adapter as an apparently healthy store outside the pipeline.
        configuration.document()
        return configuration

    def _object_uri(self, object_name: str, generation: str | None = None) -> str:
        if not isinstance(object_name, str) or not object_name:
            raise OffHostTransportError("invalid-object-name", "gcs-object")
        parts = object_name.split("/")
        if any(not _OBJECT_COMPONENT_RE.fullmatch(part) for part in parts):
            raise OffHostTransportError("invalid-object-name", "gcs-object")
        encoded = "/".join(quote(part, safe="._-") for part in parts)
        uri = f"{self.bucket_uri}/{encoded}"
        if generation is not None:
            if not isinstance(generation, str) or not _GENERATION_RE.fullmatch(generation):
                raise OffHostTransportError("invalid-generation", "gcs-object")
            uri += f"#{generation}"
        return uri

    def create_if_absent(self, object_name: str, content: bytes) -> ObjectVersion:
        if not isinstance(content, bytes):
            raise OffHostTransportError("invalid-content", "upload")
        uri = self._object_uri(object_name)
        result = self._run(
            (
                self._executable,
                "storage",
                "cp",
                "-",
                uri,
                "--if-generation-match=0",
                "--print-created-message",
                "--quiet",
            ),
            input_data=content,
            stage="upload",
        )
        # --print-created-message prints a version-specific URL.  Parse only
        # the numeric generation and never reflect the surrounding provider
        # output if it is absent or malformed.
        expected = re.escape(uri).encode("ascii") + rb"#([1-9][0-9]*)"
        matches = re.findall(expected, result.stdout + b"\n" + result.stderr)
        if len(matches) != 1:
            raise OffHostTransportError("missing-created-generation", "upload")
        generation = matches[0].decode("ascii")
        return ObjectVersion(
            object_name,
            generation,
            len(content),
            hashlib.sha256(content).hexdigest(),
        )

    def read_generation(self, object_name: str, generation: str) -> bytes:
        uri = self._object_uri(object_name, generation)
        return self._run(
            (
                self._executable,
                "storage",
                "cat",
                uri,
                "--quiet",
            ),
            stage="exact-generation-read",
        ).stdout
