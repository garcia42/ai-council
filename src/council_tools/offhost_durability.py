"""Generation-pinned off-host evidence backup and restore rehearsal.

The orchestration in this module is provider neutral.  In particular it does
not discover credentials, a default bucket, or live evidence paths.  Callers
must inject both a verified snapshot exporter and a versioned object store.
The small :class:`SnapshotExport` seam is also suitable for an exporter which
keeps filesystem descriptors in custody while materialising member bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence

from .evidence_backup import (
    export_verified_evidence_snapshot,
    restore_evidence_snapshot,
    verify_evidence_snapshot,
)


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SAFE_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MAX_METADATA_TEXT = 512
_INDEX_NAME = "index.json"


class OffHostDurabilityError(ValueError):
    """A non-reflective off-host rehearsal failure.

    ``detail`` is restricted to implementation-owned labels.  Provider output,
    object bytes, credentials, and local paths must never be supplied here.
    """

    def __init__(self, code: str, stage: str, detail: str = ""):
        self.code = code
        self.stage = stage
        self.detail = detail
        message = f"off-host durability {code} during {stage}"
        if detail:
            message += f": {detail}"
        super().__init__(message)


class OffHostPolicyError(OffHostDurabilityError):
    """The frozen policy or provider posture is insufficient."""


class OffHostIntegrityError(OffHostDurabilityError):
    """Uploaded, read-back, reconstructed, or restored bytes do not agree."""


class OffHostTransportError(OffHostDurabilityError):
    """A remote create or exact-generation read failed."""


def _safe_text(value: Any, field: str, *, allow_uri: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_METADATA_TEXT:
        raise OffHostPolicyError("invalid-policy", "validation", field)
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise OffHostPolicyError("invalid-policy", "validation", field)
    if not allow_uri and ("\n" in value or "\r" in value):
        raise OffHostPolicyError("invalid-policy", "validation", field)
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OffHostPolicyError("invalid-policy", "validation", field)
    return value


def _canonical_json(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        # A round trip rejects custom mapping/list objects and guarantees that
        # the certificate has only strict JSON values.
        decoded = json.loads(encoded)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise OffHostPolicyError("not-json-serializable", "certificate") from None
    if json.dumps(
        decoded,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") != encoded:
        raise OffHostPolicyError("noncanonical-json", "certificate")
    return encoded


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise OffHostPolicyError("invalid-time", "validation", field)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _validate_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise OffHostIntegrityError("unsafe-member-path", "snapshot-export")
    if "\\" in value or "\x00" in value:
        raise OffHostIntegrityError("unsafe-member-path", "snapshot-export")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise OffHostIntegrityError("unsafe-member-path", "snapshot-export")
    if str(PurePosixPath(*parts)) != value:
        raise OffHostIntegrityError("unsafe-member-path", "snapshot-export")
    return value


@dataclass(frozen=True)
class DurabilityPolicy:
    """Frozen activation policy whose canonical digest is certificate-bound."""

    target_uri: str
    retention_seconds: int
    rpo_seconds: int
    rto_seconds: int
    max_snapshot_age_seconds: int
    max_restore_evidence_age_seconds: int
    encryption_access_posture: str
    access_posture: str
    failure_domain_caveats: str
    failure_domain_caveat_acknowledged: bool
    automatic_application_deletion: bool = False
    schema_version: int = 1

    def document(self) -> dict[str, Any]:
        if self.schema_version != 1:
            raise OffHostPolicyError("unsupported-policy-version", "validation")
        target = _safe_text(self.target_uri, "targetUri", allow_uri=True)
        if not target.startswith("gs://"):
            raise OffHostPolicyError("invalid-target", "validation")
        if self.automatic_application_deletion is not False:
            raise OffHostPolicyError("automatic-deletion-enabled", "validation")
        if self.failure_domain_caveat_acknowledged is not True:
            raise OffHostPolicyError("failure-domain-not-acknowledged", "validation")
        retention_seconds = _positive_int(
            self.retention_seconds, "retentionSeconds"
        )
        if retention_seconds % 86400:
            raise OffHostPolicyError("retention-not-whole-days", "validation")
        return {
            "accessPosture": _safe_text(self.access_posture, "accessPosture"),
            "automaticApplicationDeletion": False,
            "encryptionAccessPosture": _safe_text(
                self.encryption_access_posture, "encryptionAccessPosture"
            ),
            "failureDomainCaveats": _safe_text(
                self.failure_domain_caveats, "failureDomainCaveats"
            ),
            "failureDomainCaveatAcknowledged": True,
            "maxRestoreEvidenceAgeSeconds": _positive_int(
                self.max_restore_evidence_age_seconds,
                "maxRestoreEvidenceAgeSeconds",
            ),
            "maxSnapshotAgeSeconds": _positive_int(
                self.max_snapshot_age_seconds, "maxSnapshotAgeSeconds"
            ),
            "retentionSeconds": retention_seconds,
            "rpoSeconds": _positive_int(self.rpo_seconds, "rpoSeconds"),
            "rtoSeconds": _positive_int(self.rto_seconds, "rtoSeconds"),
            "schemaVersion": 1,
            "targetUri": target,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.document())

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True)
class StorageConfiguration:
    """Minimum provider facts required by the activation contract."""

    provider: str
    bucket_uri: str
    failure_domain: str
    versioning_enabled: bool
    public_access_prevention: str
    uniform_bucket_access: bool
    retention_seconds: int
    retention_locked: bool
    encryption_at_rest: str
    access_posture: str
    automatic_application_deletion: bool

    def document(self) -> dict[str, Any]:
        provider = _safe_text(self.provider, "provider")
        bucket_uri = _safe_text(self.bucket_uri, "bucketUri", allow_uri=True)
        if not bucket_uri.startswith("gs://"):
            raise OffHostPolicyError("invalid-target", "provider-configuration")
        encryption = _safe_text(self.encryption_at_rest, "encryptionAtRest")
        if encryption not in {"provider-managed", "customer-managed"}:
            raise OffHostPolicyError(
                "insufficient-encryption", "provider-configuration"
            )
        if self.versioning_enabled is not True:
            raise OffHostPolicyError("versioning-disabled", "provider-configuration")
        if self.public_access_prevention != "enforced":
            raise OffHostPolicyError(
                "public-access-prevention-not-enforced", "provider-configuration"
            )
        if self.uniform_bucket_access is not True:
            raise OffHostPolicyError(
                "uniform-access-disabled", "provider-configuration"
            )
        if self.automatic_application_deletion is not False:
            raise OffHostPolicyError(
                "automatic-deletion-enabled", "provider-configuration"
            )
        return {
            "accessPosture": _safe_text(self.access_posture, "accessPosture"),
            "automaticApplicationDeletion": False,
            "bucketUri": bucket_uri,
            "encryptionAtRest": encryption,
            "failureDomain": _safe_text(self.failure_domain, "failureDomain"),
            "provider": provider,
            "publicAccessPrevention": "enforced",
            "retentionLocked": bool(self.retention_locked),
            "retentionSeconds": _positive_int(
                self.retention_seconds, "retentionSeconds"
            ),
            "uniformBucketAccess": True,
            "versioningEnabled": True,
        }


@dataclass(frozen=True)
class ObjectVersion:
    object_name: str
    generation: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        _validate_object_name(self.object_name)
        if not isinstance(self.generation, str) or not self.generation.isdigit():
            raise OffHostTransportError("invalid-generation", "remote-metadata")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise OffHostTransportError("invalid-size", "remote-metadata")
        if not isinstance(self.sha256, str) or not _SHA256_RE.fullmatch(self.sha256):
            raise OffHostTransportError("invalid-digest", "remote-metadata")

    def document(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "objectName": self.object_name,
            "sha256": self.sha256,
            "size": self.size,
        }


class VersionedObjectStore(Protocol):
    """Create-only object storage with exact-generation reads and no delete."""

    def configuration(self) -> StorageConfiguration:
        ...

    def create_if_absent(self, object_name: str, content: bytes) -> ObjectVersion:
        ...

    def read_generation(self, object_name: str, generation: str) -> bytes:
        ...


@dataclass(frozen=True)
class SnapshotMember:
    """One descriptor-custodied snapshot member materialised for transport."""

    relative_path: str
    kind: str
    mode: int
    content: bytes = b""

    def __post_init__(self) -> None:
        _validate_relative_path(self.relative_path)
        if self.kind not in {"file", "directory"}:
            raise OffHostIntegrityError("invalid-member-kind", "snapshot-export")
        if isinstance(self.mode, bool) or not isinstance(self.mode, int):
            raise OffHostIntegrityError("invalid-member-mode", "snapshot-export")
        if self.mode < 0 or self.mode > 0o7777:
            raise OffHostIntegrityError("invalid-member-mode", "snapshot-export")
        if not isinstance(self.content, bytes):
            raise OffHostIntegrityError("invalid-member-bytes", "snapshot-export")
        if self.kind == "directory" and self.content:
            raise OffHostIntegrityError("directory-has-content", "snapshot-export")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass(frozen=True)
class SnapshotExport:
    """Verified member set returned while the snapshot exporter owns custody."""

    verification: Mapping[str, Any]
    members: Sequence[SnapshotMember]
    cut_at: datetime


SnapshotExporter = Callable[[Path], SnapshotExport]
RestoredStateValidator = Callable[[Path], Mapping[str, Any]]


@dataclass(frozen=True)
class DurabilityCertificate:
    """Strict JSON certificate plus its external content address."""

    document: Mapping[str, Any]
    canonical_bytes: bytes
    sha256: str

    def as_artifact(self) -> dict[str, Any]:
        return {
            "bytes": len(self.canonical_bytes),
            "document": dict(self.document),
            "sha256": self.sha256,
        }


def _validate_object_name(value: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise OffHostTransportError("invalid-object-name", "remote-metadata")
    parts = value.split("/")
    if any(not _SAFE_COMPONENT_RE.fullmatch(part) for part in parts):
        raise OffHostTransportError("invalid-object-name", "remote-metadata")
    return value


def _validate_prefix(value: str) -> str:
    try:
        return _validate_object_name(value)
    except OffHostTransportError as exc:
        raise OffHostPolicyError("invalid-prefix", "remote-prefix") from exc


def _verification_document(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("verified") is not True:
        raise OffHostIntegrityError("snapshot-not-verified", "snapshot-export")
    digest = value.get("manifestSha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise OffHostIntegrityError("invalid-manifest-digest", "snapshot-export")
    expected = {
        "entryCount",
        "formatVersion",
        "manifestSha256",
        "scope",
        "sourceCount",
        "verified",
    }
    if set(value) != expected:
        raise OffHostIntegrityError("invalid-verification", "snapshot-export")
    result = dict(value)
    _canonical_json(result)
    return result


def snapshot_export_from_directory(
    snapshot_root: str | os.PathLike[str],
    *,
    cut_at: datetime | None = None,
) -> SnapshotExport:
    """Compatibility adapter for an already completed local snapshot.

    Production integration should prefer the descriptor-custody exporter seam
    and return :class:`SnapshotExport` directly.  This helper is intentionally
    narrow and mainly supports migration and local tests.
    """

    root = Path(os.path.abspath(os.fspath(snapshot_root)))
    verification = verify_evidence_snapshot(root)
    members: list[SnapshotMember] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        directory_names.sort(key=os.fsencode)
        file_names.sort(key=os.fsencode)
        for name in directory_names:
            path = current_path / name
            item = os.lstat(path)
            if not stat.S_ISDIR(item.st_mode) or stat.S_ISLNK(item.st_mode):
                raise OffHostIntegrityError("unsafe-member", "snapshot-export")
            members.append(
                SnapshotMember(
                    path.relative_to(root).as_posix(),
                    "directory",
                    stat.S_IMODE(item.st_mode),
                )
            )
        for name in file_names:
            path = current_path / name
            before = os.lstat(path)
            if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
                raise OffHostIntegrityError("unsafe-member", "snapshot-export")
            flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            try:
                opened = os.fstat(fd)
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                after = os.fstat(fd)
            finally:
                os.close(fd)
            identity = lambda item: (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
                item.st_nlink,
            )
            if identity(before) != identity(opened) or identity(opened) != identity(after):
                raise OffHostIntegrityError("member-changed", "snapshot-export")
            members.append(
                SnapshotMember(
                    path.relative_to(root).as_posix(),
                    "file",
                    stat.S_IMODE(opened.st_mode),
                    b"".join(chunks),
                )
            )
    final_verification = verify_evidence_snapshot(root)
    if final_verification != verification:
        raise OffHostIntegrityError("snapshot-changed", "snapshot-export")
    return SnapshotExport(
        MappingProxyType(dict(verification)),
        tuple(sorted(members, key=lambda member: member.relative_path)),
        _utc(cut_at or datetime.now(timezone.utc), "cutAt"),
    )


def filesystem_snapshot_exporter(
    create_snapshot: Callable[[Path], Mapping[str, Any]],
    *,
    clock: Callable[[], datetime] | None = None,
) -> SnapshotExporter:
    """Compatibility name for :func:`descriptor_custody_snapshot_exporter`."""

    return descriptor_custody_snapshot_exporter(create_snapshot, clock=clock)


def descriptor_custody_snapshot_exporter(
    create_snapshot: Callable[[Path], Mapping[str, Any]],
    *,
    clock: Callable[[], datetime] | None = None,
) -> SnapshotExporter:
    """Create then export through evidence_backup's pinned-descriptor API."""

    use_clock = clock or (lambda: datetime.now(timezone.utc))

    def export(target: Path) -> SnapshotExport:
        reported = create_snapshot(target)
        custodied = export_verified_evidence_snapshot(target)
        if dict(custodied.verification) != dict(reported):
            raise OffHostIntegrityError("creator-verification-mismatch", "snapshot-export")
        members = tuple(
            SnapshotMember(
                member.relative_path,
                member.kind,
                member.mode,
                member.content,
            )
            for member in custodied.members
        )
        return SnapshotExport(
            MappingProxyType(dict(custodied.verification)),
            members,
            _utc(use_clock(), "cutAt"),
        )

    return export


def _validate_export(export: SnapshotExport) -> tuple[dict[str, Any], tuple[SnapshotMember, ...]]:
    if not isinstance(export, SnapshotExport):
        raise OffHostIntegrityError("invalid-export", "snapshot-export")
    verification = _verification_document(export.verification)
    _utc(export.cut_at, "cutAt")
    members = tuple(export.members)
    if not members:
        raise OffHostIntegrityError("empty-export", "snapshot-export")
    paths = [member.relative_path for member in members]
    if paths != sorted(paths) or len(set(paths)) != len(paths):
        raise OffHostIntegrityError("invalid-member-order", "snapshot-export")
    by_path = {member.relative_path: member for member in members}
    for member in members:
        parts = member.relative_path.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            parent = "/".join(parts[:depth])
            parent_member = by_path.get(parent)
            if parent_member is None or parent_member.kind != "directory":
                raise OffHostIntegrityError("missing-member-parent", "snapshot-export")
    return verification, members


def _member_object_name(prefix: str, index: int, member: SnapshotMember) -> str:
    path_digest = hashlib.sha256(member.relative_path.encode("utf-8")).hexdigest()
    return f"{prefix}/members/{index:08d}-{path_digest}"


def _stored_version(
    returned: ObjectVersion,
    *,
    expected_name: str,
    content: bytes,
) -> ObjectVersion:
    digest = hashlib.sha256(content).hexdigest()
    if (
        returned.object_name != expected_name
        or returned.size != len(content)
        or returned.sha256 != digest
    ):
        raise OffHostIntegrityError("remote-metadata-mismatch", "upload")
    return returned


def _reconstruct_snapshot(
    root: Path,
    members: Sequence[tuple[SnapshotMember, ObjectVersion]],
    store: VersionedObjectStore,
) -> int:
    root.mkdir(mode=0o700)
    directory_modes: list[tuple[Path, int]] = []
    byte_count = 0
    for member, version in members:
        destination = root.joinpath(*member.relative_path.split("/"))
        if member.kind == "directory":
            destination.mkdir(mode=0o700)
            directory_modes.append((destination, member.mode))
            expected = b""
        else:
            expected = store.read_generation(version.object_name, version.generation)
            byte_count += len(expected)
            if len(expected) != member.size or hashlib.sha256(expected).hexdigest() != member.sha256:
                raise OffHostIntegrityError("readback-mismatch", "exact-generation-read")
            fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
            )
            try:
                view = memoryview(expected)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("short write")
                    view = view[written:]
                os.fchmod(fd, member.mode)
                os.fsync(fd)
            finally:
                os.close(fd)
        if member.kind == "directory":
            remote_empty = store.read_generation(version.object_name, version.generation)
            if remote_empty != expected:
                raise OffHostIntegrityError("readback-mismatch", "exact-generation-read")
    for path, mode in sorted(directory_modes, key=lambda row: len(row[0].parts), reverse=True):
        os.chmod(path, mode)
    return byte_count


def _default_restored_state_validator(restored: Path) -> Mapping[str, Any]:
    required = {"artifact-root", "control-store", "ledger", "resolution-store"}
    actual = {path.name for path in restored.iterdir()}
    return {
        "expectedSourceNames": sorted(required),
        "sourceMembershipMatches": actual == required,
    }


def run_offhost_durability_rehearsal(
    *,
    snapshot_exporter: SnapshotExporter,
    object_store: VersionedObjectStore,
    policy: DurabilityPolicy,
    runtime_commit: str,
    source_tree_sha256: str,
    activation_policy_sha256: str,
    restored_state_validator: RestoredStateValidator | None = None,
    workspace_parent: str | os.PathLike[str] | None = None,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
    prefix_factory: Callable[[], str] | None = None,
) -> DurabilityCertificate:
    """Run upload, generation-pinned readback, clean restore, and certify it."""

    if not isinstance(runtime_commit, str) or not _COMMIT_RE.fullmatch(runtime_commit):
        raise OffHostPolicyError("invalid-runtime-commit", "validation")
    if not isinstance(source_tree_sha256, str) or not _SHA256_RE.fullmatch(
        source_tree_sha256
    ):
        raise OffHostPolicyError("invalid-source-tree-digest", "validation")
    if not isinstance(activation_policy_sha256, str) or not _SHA256_RE.fullmatch(
        activation_policy_sha256
    ):
        raise OffHostPolicyError("invalid-activation-policy-digest", "validation")
    policy_document = policy.document()
    policy_bytes = policy.canonical_bytes()
    policy_sha256 = hashlib.sha256(policy_bytes).hexdigest()
    configuration = object_store.configuration()
    configuration_document = configuration.document()
    if configuration.bucket_uri != policy.target_uri:
        raise OffHostPolicyError("target-mismatch", "provider-configuration")
    if configuration.retention_seconds != policy.retention_seconds:
        raise OffHostPolicyError("retention-policy-mismatch", "provider-configuration")
    if configuration.access_posture != policy.access_posture:
        raise OffHostPolicyError("access-posture-mismatch", "provider-configuration")

    use_clock = clock or (lambda: datetime.now(timezone.utc))
    use_monotonic = monotonic or time.monotonic
    started_at = _utc(use_clock(), "startedAt")
    elapsed_start = use_monotonic()
    if prefix_factory is None:
        prefix_factory = lambda: (
            "snapshots/"
            + started_at.strftime("%Y%m%dT%H%M%S%fZ")
            + "-"
            + uuid.uuid4().hex
        )
    prefix = _validate_prefix(prefix_factory())
    validator = restored_state_validator or _default_restored_state_validator
    parent = None if workspace_parent is None else os.fspath(workspace_parent)

    with tempfile.TemporaryDirectory(prefix="council-offhost-", dir=parent) as temporary:
        work = Path(temporary)
        local_snapshot = work / "local-snapshot"
        exported = snapshot_exporter(local_snapshot)
        verification, members = _validate_export(exported)
        snapshot_cut_at = _utc(exported.cut_at, "cutAt")
        after_export = _utc(use_clock(), "afterExport")
        if snapshot_cut_at > after_export:
            raise OffHostPolicyError("future-snapshot-cut", "snapshot-export")
        if (after_export - snapshot_cut_at).total_seconds() > policy.max_snapshot_age_seconds:
            raise OffHostPolicyError("snapshot-too-old", "snapshot-export")

        upload_started_at = after_export

        uploaded: list[tuple[SnapshotMember, ObjectVersion]] = []
        member_records: list[dict[str, Any]] = []
        uploaded_bytes = 0
        for index, member in enumerate(members):
            object_name = _member_object_name(prefix, index, member)
            version = _stored_version(
                object_store.create_if_absent(object_name, member.content),
                expected_name=object_name,
                content=member.content,
            )
            uploaded.append((member, version))
            uploaded_bytes += member.size
            record = version.document()
            record.update(
                {
                    "kind": member.kind,
                    "mode": f"{member.mode:04o}",
                    "relativePath": member.relative_path,
                }
            )
            member_records.append(record)

        index_document = {
            "formatVersion": 1,
            "members": member_records,
            "policySha256": policy_sha256,
            "durabilityPolicy": policy_document,
            "activationPolicySha256": activation_policy_sha256,
            "prefix": prefix,
            "providerConfiguration": configuration_document,
            "runtimeCommit": runtime_commit,
            "snapshotManifestSha256": verification["manifestSha256"],
            "sourceTreeSha256": source_tree_sha256,
        }
        index_bytes = _canonical_json(index_document)
        index_name = f"{prefix}/{_INDEX_NAME}"
        index_version = _stored_version(
            object_store.create_if_absent(index_name, index_bytes),
            expected_name=index_name,
            content=index_bytes,
        )

        upload_completed_at = _utc(use_clock(), "uploadCompletedAt")
        readback_started_at = _utc(use_clock(), "readbackStartedAt")

        remote_index = object_store.read_generation(
            index_version.object_name, index_version.generation
        )
        if remote_index != index_bytes:
            raise OffHostIntegrityError("index-readback-mismatch", "index-readback")
        try:
            decoded_index = json.loads(remote_index)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise OffHostIntegrityError("invalid-index", "index-readback") from None
        if decoded_index != index_document or _canonical_json(decoded_index) != remote_index:
            raise OffHostIntegrityError("invalid-index", "index-readback")

        reconstructed = work / "remote-readback"
        readback_bytes = _reconstruct_snapshot(reconstructed, uploaded, object_store)
        if readback_bytes != uploaded_bytes:
            raise OffHostIntegrityError("readback-byte-count-mismatch", "exact-generation-read")
        reconstructed_verification = verify_evidence_snapshot(reconstructed)
        if reconstructed_verification != verification:
            raise OffHostIntegrityError(
                "reconstructed-verification-mismatch", "snapshot-verification"
            )

        readback_completed_at = _utc(use_clock(), "readbackCompletedAt")
        restore_started_at = _utc(use_clock(), "restoreStartedAt")

        restored = work / "clean-restore"
        restore_result = restore_evidence_snapshot(reconstructed, restored)
        if restore_result != verification:
            raise OffHostIntegrityError(
                "restore-verification-mismatch", "restored-state"
            )
        validation_document = dict(validator(restored))
        _canonical_json(validation_document)
        if validation_document.get("sourceMembershipMatches") is not True:
            raise OffHostIntegrityError("restored-state-invalid", "restored-state")

        restore_completed_at = _utc(use_clock(), "restoreCompletedAt")
        verified_at = _utc(use_clock(), "verifiedAt")
        observed_times = (
            snapshot_cut_at,
            upload_started_at,
            upload_completed_at,
            readback_started_at,
            readback_completed_at,
            restore_started_at,
            restore_completed_at,
            verified_at,
        )
        if tuple(sorted(observed_times)) != observed_times:
            raise OffHostPolicyError("invalid-event-order", "rehearsal")
        elapsed_seconds = (restore_completed_at - upload_started_at).total_seconds()
        if elapsed_seconds > policy.rto_seconds:
            raise OffHostPolicyError("rto-exceeded", "rehearsal")
        # Retain a monotonic measurement as a local guard against a wall-clock
        # jump hiding an RTO breach.  The certificate uses the independently
        # checkable event-time elapsed value required by activation_evidence.
        monotonic_elapsed = max(0.0, use_monotonic() - elapsed_start)
        if monotonic_elapsed > policy.rto_seconds:
            raise OffHostPolicyError("rto-exceeded", "rehearsal")
        expires_at = min(
            snapshot_cut_at + timedelta(seconds=policy.max_snapshot_age_seconds),
            verified_at
            + timedelta(seconds=policy.max_restore_evidence_age_seconds),
        )
        if expires_at <= verified_at:
            raise OffHostPolicyError("certificate-already-expired", "rehearsal")
        remote_objects = [
            {
                "bytes": record["size"],
                "generation": record["generation"],
                "name": record["objectName"],
                "sha256": record["sha256"],
            }
            for record in member_records
        ]
        index_object = {
            "bytes": index_version.size,
            "generation": index_version.generation,
            "name": index_version.object_name,
            "sha256": index_version.sha256,
        }
        manifest_records = [
            record
            for record in member_records
            if record["relativePath"] == "manifest.json" and record["kind"] == "file"
        ]
        if len(manifest_records) != 1:
            raise OffHostIntegrityError("manifest-member-missing", "snapshot-export")
        manifest_object_name = manifest_records[0]["objectName"]
        certificate_id = (
            "durability-"
            + verification["manifestSha256"][:16]
            + "-"
            + index_version.generation
        )
        certificate_document = {
            "bucket": {
                "automaticApplicationDeletion": False,
                "encryptionAtRest": configuration.encryption_at_rest,
                "private": True,
                "publicAccessPrevention": configuration.public_access_prevention,
                "retentionPolicy": {
                    "configured": True,
                    "days": configuration.retention_seconds // 86400,
                    "locked": bool(configuration.retention_locked),
                },
                "uniformBucketAccess": configuration.uniform_bucket_access,
                "uri": configuration.bucket_uri,
                "versioning": configuration.versioning_enabled,
            },
            "certificateId": certificate_id,
            "elapsedSeconds": elapsed_seconds,
            "expiresAt": _timestamp(expires_at),
            "failureDomainCaveatAcknowledged": True,
            "indexObject": index_object,
            "issuedAt": _timestamp(verified_at),
            "kind": "off-host-durability-certificate",
            "policySha256": activation_policy_sha256,
            "prefix": prefix,
            "provider": configuration.provider,
            "readback": {
                "completedAt": _timestamp(readback_completed_at),
                "downloadedBytes": uploaded_bytes + len(index_bytes),
                "generationPinned": True,
                "indexGeneration": index_version.generation,
                "manifestVerified": reconstructed_verification == verification,
                "startedAt": _timestamp(readback_started_at),
            },
            "remoteObjects": remote_objects,
            "restore": {
                "cleanTarget": True,
                "completedAt": _timestamp(restore_completed_at),
                "restoredBytes": uploaded_bytes,
                "restoredEvidenceValidated": validation_document.get(
                    "sourceMembershipMatches"
                )
                is True,
                "snapshotVerified": restore_result == verification,
                "startedAt": _timestamp(restore_started_at),
            },
            "runtimeSourceCommit": runtime_commit,
            "runtimeSourceSha256": source_tree_sha256,
            "schemaVersion": 1,
            "snapshot": {
                "bytes": uploaded_bytes,
                "cutAt": _timestamp(snapshot_cut_at),
                "manifestSha256": verification["manifestSha256"],
                "manifestObjectName": manifest_object_name,
            },
            "upload": {
                "completedAt": _timestamp(upload_completed_at),
                "startedAt": _timestamp(upload_started_at),
                "uploadedBytes": uploaded_bytes + len(index_bytes),
            },
        }
        certificate_bytes = _canonical_json(certificate_document)
        return DurabilityCertificate(
            MappingProxyType(certificate_document),
            certificate_bytes,
            hashlib.sha256(certificate_bytes).hexdigest(),
        )


def verify_durability_certificate(
    certificate_bytes: bytes,
    *,
    expected_runtime_commit: str,
    expected_source_tree_sha256: str,
) -> DurabilityCertificate:
    """Strictly parse and content-address a certificate for later evaluation."""

    if not isinstance(certificate_bytes, bytes):
        raise OffHostIntegrityError("invalid-certificate", "certificate-verification")
    try:
        value = json.loads(
            certificate_bytes,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise OffHostIntegrityError("invalid-certificate", "certificate-verification") from None
    if not isinstance(value, dict) or _canonical_json(value) != certificate_bytes:
        raise OffHostIntegrityError("noncanonical-certificate", "certificate-verification")
    if value.get("schemaVersion") != 1 or value.get("kind") != "off-host-durability-certificate":
        raise OffHostIntegrityError("invalid-certificate", "certificate-verification")
    if value.get("runtimeSourceCommit") != expected_runtime_commit:
        raise OffHostIntegrityError("runtime-commit-mismatch", "certificate-verification")
    if value.get("runtimeSourceSha256") != expected_source_tree_sha256:
        raise OffHostIntegrityError("source-tree-mismatch", "certificate-verification")
    return DurabilityCertificate(
        MappingProxyType(value),
        certificate_bytes,
        hashlib.sha256(certificate_bytes).hexdigest(),
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value
