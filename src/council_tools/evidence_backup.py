"""Content-manifested local evidence snapshots and clean restore rehearsals.

This module deliberately stops at a transport-neutral filesystem snapshot.  A
successful call proves neither off-host custody nor encryption, retention,
remote readback, RPO, or RTO.  Callers must supply every source, the append lock
shared by their writers, a new snapshot target, and the repository boundary
that target must not enter.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Mapping

from .safe_files import (
    DirectoryIdentity,
    PinnedLock,
    PinnedParent,
    SafeFileError,
    capture_directory_identity,
    pinned_parent,
    pinned_parent_ancestry_identities,
    revalidate_pinned_parent,
    shared_lock,
)


FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
COMPLETION_NAME = "SNAPSHOT.COMPLETE"
PAYLOAD_NAME = "payload"
SCOPE = "local-filesystem-rehearsal-only"
_READ_CHUNK = 1024 * 1024
_LOCK_TIMEOUT_SECONDS = 10.0
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | _NOFOLLOW
_SOURCE_NAMES = (
    "artifact-root",
    "control-store",
    "ledger",
    "resolution-store",
)
_SOURCE_TYPES = {
    "artifact-root": "directory",
    "control-store": "directory",
    "ledger": "file",
    "resolution-store": "file",
}


class EvidenceBackupError(ValueError):
    """Base class for snapshot, verification, and restore failures."""

    def __init__(self, code: str, stage: str, detail: str = ""):
        self.code = code
        self.stage = stage
        self.detail = detail
        message = f"evidence backup {code} during {stage}"
        if detail:
            message += f": {detail}"
        super().__init__(message)


class SnapshotPolicyError(EvidenceBackupError):
    """A path, source, or destination violates the custody policy."""


class SnapshotIntegrityError(EvidenceBackupError):
    """A source or snapshot is partial, aliased, or does not match its manifest."""


class SnapshotWriteError(EvidenceBackupError):
    """A new snapshot could not be written durably."""


class RestoreError(EvidenceBackupError):
    """A clean restore target could not be created and verified."""


def _error(
    error_type: type[EvidenceBackupError], code: str, stage: str, detail: str = ""
) -> EvidenceBackupError:
    return error_type(code, stage, detail)


def _path(
    value: str | os.PathLike[str],
    field: str,
    *,
    error_type: type[EvidenceBackupError] = SnapshotPolicyError,
) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\x00" in raw or not os.path.isabs(raw):
        raise _error(error_type, "invalid-path", field, "path must be absolute")
    if any(part in {".", ".."} for part in raw.split(os.sep)):
        raise _error(error_type, "path-traversal", field)
    normalized = Path(os.path.normpath(raw))
    if normalized == Path(os.path.sep):
        raise _error(error_type, "invalid-path", field, "filesystem root is not allowed")
    return normalized


def _lstat_chain(
    path: Path,
    *,
    allow_missing_leaf: bool,
    stage: str,
    error_type: type[EvidenceBackupError],
) -> os.stat_result | None:
    current = Path(os.path.sep)
    parts = path.parts[1:]
    for index, part in enumerate(parts):
        current /= part
        final = index == len(parts) - 1
        try:
            item = os.lstat(current)
        except FileNotFoundError:
            if final and allow_missing_leaf:
                return None
            raise _error(error_type, "path-missing", stage, str(current)) from None
        if stat.S_ISLNK(item.st_mode):
            raise _error(error_type, "symlink-alias", stage, str(current))
        if not final and not stat.S_ISDIR(item.st_mode):
            raise _error(error_type, "unsafe-path-component", stage, str(current))
    return item


def _is_within(candidate: Path, boundary: Path) -> bool:
    try:
        return os.path.commonpath((str(candidate), str(boundary))) == str(boundary)
    except ValueError:
        return False


def _same_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
        stat.S_IMODE(left.st_mode),
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
        left.st_nlink,
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
        stat.S_IMODE(right.st_mode),
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
        right.st_nlink,
    )


def _root_identity(item: os.stat_result) -> tuple[int, int, int]:
    """Return the stable, type-sensitive identity of one source root."""

    return item.st_dev, item.st_ino, stat.S_IFMT(item.st_mode)


def _capture_root_stat(
    path: Path,
    *,
    expected_type: str,
    stage: str,
    error_type: type[EvidenceBackupError],
) -> os.stat_result:
    """Capture a root inode through a no-follow parent/name walk."""

    try:
        with pinned_parent(path, create_parents=False) as parent:
            try:
                before = os.stat(
                    parent.name,
                    dir_fd=parent.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                raise _error(error_type, "path-missing", stage) from None
            actual_type = (
                "file"
                if stat.S_ISREG(before.st_mode)
                else "directory"
                if stat.S_ISDIR(before.st_mode)
                else "special"
            )
            if actual_type == "special":
                raise _error(error_type, "special-file", stage)
            if actual_type != expected_type:
                raise _error(
                    error_type,
                    "wrong-source-type",
                    stage,
                    f"must be a {expected_type}",
                )
            flags = os.O_RDONLY | os.O_CLOEXEC | _NOFOLLOW
            if expected_type == "directory":
                flags |= os.O_DIRECTORY
            descriptor = os.open(parent.name, flags, dir_fd=parent.descriptor)
            try:
                opened = os.fstat(descriptor)
                after = os.stat(
                    parent.name,
                    dir_fd=parent.descriptor,
                    follow_symlinks=False,
                )
                if not _same_stat(before, opened) or not _same_stat(opened, after):
                    raise _error(error_type, "source-changed", stage)
                return opened
            finally:
                os.close(descriptor)
    except EvidenceBackupError:
        raise
    except SafeFileError as exc:
        raise _error(error_type, "unsafe-path-component", stage) from exc
    except OSError as exc:
        raise _error(error_type, "unsafe-path-component", stage) from exc


def _capture_source_root_stats(
    sources: Mapping[str, Path],
    *,
    error_type: type[EvidenceBackupError],
) -> dict[str, os.stat_result]:
    return {
        name: _capture_root_stat(
            path,
            expected_type=_SOURCE_TYPES[name],
            stage=name,
            error_type=error_type,
        )
        for name, path in sources.items()
    }


def _assert_same_root_identities(
    expected: Mapping[str, os.stat_result],
    actual: Mapping[str, os.stat_result],
) -> None:
    for name in _SOURCE_NAMES:
        if _root_identity(expected[name]) != _root_identity(actual[name]):
            raise SnapshotIntegrityError("source-changed", "snapshot", name)


def _assert_snapshot_destination_allowed(
    destination_parent: PinnedParent,
    *,
    repository_identity: DirectoryIdentity,
    source_stats: Mapping[str, os.stat_result],
) -> None:
    """Authorize the physical destination inode, never just its pathname."""

    protected: dict[tuple[int, int], str] = {
        (repository_identity.device, repository_identity.inode): "repository"
    }
    for name, item in source_stats.items():
        protected[(item.st_dev, item.st_ino)] = name
    for ancestor in pinned_parent_ancestry_identities(destination_parent):
        label = protected.get((ancestor.device, ancestor.inode))
        if label == "repository":
            raise SnapshotPolicyError("target-inside-repository", "snapshot")
        if label is not None:
            raise SnapshotPolicyError("target-overlaps-source", "snapshot", label)


def _require_missing_pinned_target(destination_parent: PinnedParent) -> None:
    try:
        os.stat(
            destination_parent.name,
            dir_fd=destination_parent.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    raise SnapshotPolicyError("target-exists", "snapshot")


def _safe_mode(kind: str, source_mode: int) -> int:
    if kind == "file":
        return 0o600 if source_mode & stat.S_IWUSR else 0o400
    if kind == "directory":
        return 0o700 if source_mode & stat.S_IWUSR else 0o500
    raise AssertionError(f"unexpected entry kind: {kind}")


def _mode_text(mode: int) -> str:
    return f"{mode & 0o7777:04o}"


def _parse_mode(value: Any, field: str) -> int:
    if not isinstance(value, str) or len(value) != 4 or any(c not in "01234567" for c in value):
        raise SnapshotIntegrityError("invalid-manifest", "verification", field)
    return int(value, 8)


def _directory_digest(children: Iterable[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for kind, name in children:
        digest.update(kind.encode("ascii"))
        digest.update(b"\0")
        digest.update(os.fsencode(name))
        digest.update(b"\0")
    return digest.hexdigest()


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory_fd(fd: int) -> None:
    item = os.fstat(fd)
    if not stat.S_ISDIR(item.st_mode):
        raise OSError("snapshot destination descriptor is not a directory")
    os.fsync(fd)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_private_file(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        _write_all(fd, content)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_private_file_at(
    parent_fd: int,
    name: str,
    content: bytes,
    *,
    mode: int = 0o600,
) -> None:
    """Create and fsync one file relative to an already-pinned directory."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | _NOFOLLOW
    fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise OSError("snapshot metadata identity changed")
        _write_all(fd, content)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _create_pinned_directory(parent_fd: int, name: str) -> int:
    """Create, open, and identity-check an owner-private child directory."""

    os.mkdir(name, 0o700, dir_fd=parent_fd)
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise OSError("snapshot destination is not a directory")
    fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        expected = (before.st_dev, before.st_ino)
        if (
            (opened.st_dev, opened.st_ino) != expected
            or (after.st_dev, after.st_ino) != expected
            or os.listdir(fd)
        ):
            raise OSError("snapshot destination identity changed")
        os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _claim_inode(
    item: os.stat_result,
    seen: dict[tuple[int, int], str],
    label: str,
    *,
    regular_file: bool,
    stage: str,
) -> None:
    identity = (item.st_dev, item.st_ino)
    if identity in seen:
        raise SnapshotIntegrityError(
            "filesystem-alias", stage, f"{label} aliases {seen[identity]}"
        )
    if regular_file and item.st_nlink != 1:
        raise SnapshotIntegrityError("hard-link-alias", stage, label)
    seen[identity] = label


def _open_source(path: Path, expected: os.stat_result, *, directory: bool) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    actual = os.fstat(fd)
    if not _same_stat(expected, actual):
        os.close(fd)
        raise SnapshotIntegrityError("source-changed", "snapshot", str(path))
    return fd


def _entry(
    *,
    source: str,
    relative_path: str,
    kind: str,
    size: int,
    digest: str,
    source_mode: int,
) -> dict[str, Any]:
    return {
        "mode": _mode_text(_safe_mode(kind, source_mode)),
        "path": relative_path,
        "sha256": digest,
        "size": size,
        "source": source,
        "sourceMode": _mode_text(source_mode),
        "type": kind,
    }


def _copy_file_from_fd(
    source_fd: int,
    source_stat: os.stat_result,
    destination_parent_fd: int,
    destination_name: str,
    *,
    source_name: str,
    relative_path: str,
) -> dict[str, Any]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    destination_fd = os.open(
        destination_name,
        flags,
        0o600,
        dir_fd=destination_parent_fd,
    )
    digest = hashlib.sha256()
    byte_count = 0
    try:
        while True:
            chunk = os.read(source_fd, _READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            byte_count += len(chunk)
            _write_all(destination_fd, chunk)
        final_source_stat = os.fstat(source_fd)
        if not _same_stat(source_stat, final_source_stat) or byte_count != source_stat.st_size:
            raise SnapshotIntegrityError(
                "source-changed", "snapshot", f"{source_name}:{relative_path}"
            )
        safe_mode = _safe_mode("file", stat.S_IMODE(source_stat.st_mode))
        os.fchmod(destination_fd, safe_mode)
        os.fsync(destination_fd)
    finally:
        os.close(destination_fd)
    return _entry(
        source=source_name,
        relative_path=relative_path,
        kind="file",
        size=byte_count,
        digest=digest.hexdigest(),
        source_mode=stat.S_IMODE(source_stat.st_mode),
    )


def _copy_directory_from_fd(
    source_fd: int,
    source_stat: os.stat_result,
    destination_parent_fd: int,
    destination_name: str,
    *,
    source_name: str,
    relative_path: str,
    seen: dict[tuple[int, int], str],
) -> list[dict[str, Any]]:
    source_mode = stat.S_IMODE(source_stat.st_mode)
    destination_fd = _create_pinned_directory(
        destination_parent_fd,
        destination_name,
    )
    names = sorted(os.listdir(source_fd), key=os.fsencode)
    child_descriptions: list[tuple[str, str]] = []
    entries: list[dict[str, Any]] = []
    try:
        for name in names:
            if name in {".", ".."} or os.sep in name or "\x00" in name:
                raise SnapshotIntegrityError("path-traversal", "snapshot", name)
            child_stat = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            if stat.S_ISLNK(child_stat.st_mode):
                raise SnapshotIntegrityError(
                    "symlink-alias", "snapshot", f"{source_name}:{relative_path}/{name}"
                )
            if stat.S_ISREG(child_stat.st_mode):
                kind = "file"
                child_flags = os.O_RDONLY | os.O_CLOEXEC | _NOFOLLOW
            elif stat.S_ISDIR(child_stat.st_mode):
                kind = "directory"
                child_flags = _DIRECTORY_FLAGS
            else:
                raise SnapshotIntegrityError(
                    "special-file", "snapshot", f"{source_name}:{relative_path}/{name}"
                )
            child_fd = os.open(name, child_flags, dir_fd=source_fd)
            try:
                opened_stat = os.fstat(child_fd)
                if not _same_stat(child_stat, opened_stat):
                    raise SnapshotIntegrityError(
                        "source-changed", "snapshot", f"{source_name}:{relative_path}/{name}"
                    )
                label_path = name if relative_path == "." else f"{relative_path}/{name}"
                label = f"{source_name}:{label_path}"
                _claim_inode(
                    child_stat,
                    seen,
                    label,
                    regular_file=kind == "file",
                    stage="snapshot",
                )
                child_descriptions.append((kind, name))
                if kind == "file":
                    entries.append(
                        _copy_file_from_fd(
                            child_fd,
                            child_stat,
                            destination_fd,
                            name,
                            source_name=source_name,
                            relative_path=label_path,
                        )
                    )
                else:
                    entries.extend(
                        _copy_directory_from_fd(
                            child_fd,
                            child_stat,
                            destination_fd,
                            name,
                            source_name=source_name,
                            relative_path=label_path,
                            seen=seen,
                        )
                    )
            finally:
                os.close(child_fd)
        if sorted(os.listdir(source_fd), key=os.fsencode) != names or not _same_stat(
            source_stat, os.fstat(source_fd)
        ):
            raise SnapshotIntegrityError(
                "source-changed", "snapshot", f"{source_name}:{relative_path}"
            )
        os.fchmod(destination_fd, _safe_mode("directory", source_mode))
        _fsync_directory_fd(destination_fd)
        entries.append(
            _entry(
                source=source_name,
                relative_path=relative_path,
                kind="directory",
                size=len(names),
                digest=_directory_digest(child_descriptions),
                source_mode=source_mode,
            )
        )
        return entries
    finally:
        os.close(destination_fd)


def _copy_source(
    source_name: str,
    source_path: Path,
    destination_parent_fd: int,
    destination_name: str,
    seen: dict[tuple[int, int], str],
    *,
    expected_stat: os.stat_result | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    source_stat = expected_stat if expected_stat is not None else os.lstat(source_path)
    if stat.S_ISLNK(source_stat.st_mode):
        raise SnapshotIntegrityError("symlink-alias", "snapshot", str(source_path))
    if stat.S_ISREG(source_stat.st_mode):
        kind = "file"
    elif stat.S_ISDIR(source_stat.st_mode):
        kind = "directory"
    else:
        raise SnapshotIntegrityError("special-file", "snapshot", str(source_path))
    _claim_inode(
        source_stat,
        seen,
        str(source_path),
        regular_file=kind == "file",
        stage="snapshot",
    )
    source_fd = _open_source(source_path, source_stat, directory=kind == "directory")
    try:
        if kind == "file":
            return kind, [
                _copy_file_from_fd(
                    source_fd,
                    source_stat,
                    destination_parent_fd,
                    destination_name,
                    source_name=source_name,
                    relative_path=".",
                )
            ]
        return kind, _copy_directory_from_fd(
            source_fd,
            source_stat,
            destination_parent_fd,
            destination_name,
            source_name=source_name,
            relative_path=".",
            seen=seen,
        )
    finally:
        os.close(source_fd)


@contextmanager
def _shared_lock(lock_path: Path) -> Iterator[PinnedLock]:
    """Use the same pinned directory and lock inode boundary as writers."""

    try:
        with shared_lock(
            lock_path,
            timeout_seconds=_LOCK_TIMEOUT_SECONDS,
        ) as lock:
            yield lock
    except SafeFileError as exc:
        raise SnapshotIntegrityError("unsafe-lock", "lock") from exc


def _source_paths(
    *,
    ledger_path: str | os.PathLike[str],
    resolution_store_path: str | os.PathLike[str],
    control_store_path: str | os.PathLike[str],
    artifact_root: str | os.PathLike[str],
) -> dict[str, Path]:
    values = {
        "ledger": _path(ledger_path, "ledger_path"),
        "resolution-store": _path(resolution_store_path, "resolution_store_path"),
        "control-store": _path(control_store_path, "control_store_path"),
        "artifact-root": _path(artifact_root, "artifact_root"),
    }
    for name, path in values.items():
        item = _lstat_chain(
            path,
            allow_missing_leaf=False,
            stage=name,
            error_type=SnapshotPolicyError,
        )
        assert item is not None
        actual_type = (
            "file"
            if stat.S_ISREG(item.st_mode)
            else "directory"
            if stat.S_ISDIR(item.st_mode)
            else "special"
        )
        if actual_type == "special":
            raise SnapshotPolicyError("special-file", name, str(path))
        expected_type = _SOURCE_TYPES[name]
        if actual_type != expected_type:
            raise SnapshotPolicyError(
                "wrong-source-type", name, f"must be a {expected_type}"
            )
    pairs = list(values.items())
    for index, (left_name, left) in enumerate(pairs):
        for right_name, right in pairs[index + 1 :]:
            if _is_within(left, right) or _is_within(right, left):
                raise SnapshotPolicyError(
                    "overlapping-sources", "configuration", f"{left_name} and {right_name}"
                )
    return values


def create_evidence_snapshot(
    *,
    ledger_path: str | os.PathLike[str],
    resolution_store_path: str | os.PathLike[str],
    control_store_path: str | os.PathLike[str],
    artifact_root: str | os.PathLike[str],
    lock_path: str | os.PathLike[str],
    snapshot_target: str | os.PathLike[str],
    repository_root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Create one coherent, deterministic, local rehearsal snapshot.

    Writers must take an exclusive ``flock`` on ``lock_path`` for every source
    mutation.  This function takes one shared lock across all four source copies.
    ``snapshot_target`` must not exist and must be outside every source and the
    supplied repository root.
    """

    lock = _path(lock_path, "lock_path")
    repository = _path(repository_root, "repository_root")
    target = _path(snapshot_target, "snapshot_target")

    try:
        # Pin the destination before granting policy approval.  Later checks
        # use this descriptor's physical ancestry, never a re-resolved target.
        with pinned_parent(target, create_parents=False) as destination_parent:
            _require_missing_pinned_target(destination_parent)
            sources = _source_paths(
                ledger_path=ledger_path,
                resolution_store_path=resolution_store_path,
                control_store_path=control_store_path,
                artifact_root=artifact_root,
            )
            repository_item = _lstat_chain(
                repository,
                allow_missing_leaf=False,
                stage="repository_root",
                error_type=SnapshotPolicyError,
            )
            if repository_item is None or not stat.S_ISDIR(repository_item.st_mode):
                raise SnapshotPolicyError("wrong-source-type", "repository_root")
            if _is_within(target, repository):
                raise SnapshotPolicyError(
                    "target-inside-repository", "snapshot", str(target)
                )
            for name, source in sources.items():
                if _is_within(target, source) or _is_within(source, target):
                    raise SnapshotPolicyError(
                        "target-overlaps-source", "snapshot", name
                    )

            policy_source_stats = _capture_source_root_stats(
                sources,
                error_type=SnapshotPolicyError,
            )
            try:
                repository_identity = capture_directory_identity(repository)
            except SafeFileError as exc:
                raise SnapshotPolicyError(
                    "unsafe-path-component", "repository_root"
                ) from exc
            _assert_snapshot_destination_allowed(
                destination_parent,
                repository_identity=repository_identity,
                source_stats=policy_source_stats,
            )

            with _shared_lock(lock) as coordination_lock:
                # Revalidate every policy root after acquiring the cut-defining
                # lock, then authorize the exact mutation parent once more.
                coordination_lock.revalidate()
                snapshot_source_stats = _capture_source_root_stats(
                    sources,
                    error_type=SnapshotIntegrityError,
                )
                _assert_same_root_identities(
                    policy_source_stats,
                    snapshot_source_stats,
                )
                try:
                    current_repository_identity = capture_directory_identity(repository)
                except SafeFileError as exc:
                    raise SnapshotIntegrityError(
                        "source-changed", "snapshot", "repository"
                    ) from exc
                if current_repository_identity != repository_identity:
                    raise SnapshotIntegrityError(
                        "source-changed", "snapshot", "repository"
                    )
                _require_missing_pinned_target(destination_parent)
                _assert_snapshot_destination_allowed(
                    destination_parent,
                    repository_identity=repository_identity,
                    source_stats=snapshot_source_stats,
                )

                target_fd = _create_pinned_directory(
                    destination_parent.descriptor,
                    destination_parent.name,
                )
                try:
                    payload_fd = _create_pinned_directory(target_fd, PAYLOAD_NAME)
                    try:
                        seen: dict[tuple[int, int], str] = {}
                        entries: list[dict[str, Any]] = []
                        source_records: list[dict[str, str]] = []
                        for source_name in _SOURCE_NAMES:
                            source_kind, source_entries = _copy_source(
                                source_name,
                                sources[source_name],
                                payload_fd,
                                source_name,
                                seen,
                                expected_stat=snapshot_source_stats[source_name],
                            )
                            source_records.append(
                                {"name": source_name, "type": source_kind}
                            )
                            entries.extend(source_entries)
                        _fsync_directory_fd(payload_fd)
                    finally:
                        os.close(payload_fd)

                    # Do not write a completion marker if an attacker replaced
                    # the cut-defining lock name while bytes were copied.
                    coordination_lock.revalidate()
                    entries.sort(key=lambda row: (row["source"], row["path"]))
                    manifest = {
                        "entries": entries,
                        "formatVersion": FORMAT_VERSION,
                        "scope": SCOPE,
                        "sources": source_records,
                    }
                    manifest_bytes = _canonical_json(manifest)
                    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()

                    _write_private_file_at(target_fd, MANIFEST_NAME, manifest_bytes)
                    _fsync_directory_fd(target_fd)
                    completion = {
                        "formatVersion": FORMAT_VERSION,
                        "manifestSha256": manifest_digest,
                        "manifestSize": len(manifest_bytes),
                        "scope": SCOPE,
                        "status": "complete",
                    }
                    _write_private_file_at(
                        target_fd,
                        COMPLETION_NAME,
                        _canonical_json(completion),
                    )
                    _fsync_directory_fd(target_fd)
                    _fsync_directory_fd(destination_parent.descriptor)

                    # Verify through the retained root fd; even a later target
                    # name replacement cannot redirect this readback.
                    verified_manifest, verified_digest = _load_verified_snapshot_fd(
                        target_fd
                    )
                    result = _verification_result(
                        verified_manifest,
                        verified_digest,
                    )
                finally:
                    os.close(target_fd)
    except EvidenceBackupError:
        raise
    except SafeFileError as exc:
        # Preserve the established, precise symlink/component diagnostic when
        # the initial no-follow destination pin itself fails.  This fallback
        # is diagnostic only and never grants mutation authority.
        try:
            _lstat_chain(
                target,
                allow_missing_leaf=True,
                stage="snapshot_target",
                error_type=SnapshotPolicyError,
            )
        except SnapshotPolicyError as diagnostic:
            raise diagnostic from exc
        raise SnapshotPolicyError("unsafe-destination", "snapshot") from exc
    except OSError as exc:
        raise SnapshotWriteError("write-failed", "snapshot", exc.__class__.__name__) from None

    return result


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotIntegrityError("duplicate-json-key", "verification")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> None:
    raise SnapshotIntegrityError("invalid-json", "verification")


def _read_metadata_at(parent_fd: int, name: str) -> bytes:
    try:
        item = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise SnapshotIntegrityError("missing-metadata", "verification", name) from None
    if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
        raise SnapshotIntegrityError("unsafe-metadata", "verification", name)
    if stat.S_IMODE(item.st_mode) != 0o600:
        raise SnapshotIntegrityError("mode-mismatch", "verification", name)
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if not _same_stat(item, opened):
            raise SnapshotIntegrityError("snapshot-changed", "verification", name)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, _READ_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
        if not _same_stat(item, os.fstat(fd)):
            raise SnapshotIntegrityError("snapshot-changed", "verification", name)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _load_json(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            content,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
        if not isinstance(value, dict):
            raise SnapshotIntegrityError("invalid-json", "verification", label)
        canonical = _canonical_json(value)
    except SnapshotIntegrityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise SnapshotIntegrityError("invalid-json", "verification", label) from None
    if canonical != content:
        raise SnapshotIntegrityError("noncanonical-json", "verification", label)
    return value


def _validate_relative_path(value: Any) -> str:
    if value == ".":
        return value
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise SnapshotIntegrityError("path-traversal", "verification")
    parts = value.split("/")
    if any(not part or part in {".", ".."} or "\x00" in part for part in parts):
        raise SnapshotIntegrityError("path-traversal", "verification")
    if str(PurePosixPath(*parts)) != value:
        raise SnapshotIntegrityError("path-traversal", "verification")
    return value


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if set(manifest) != {"entries", "formatVersion", "scope", "sources"}:
        raise SnapshotIntegrityError("invalid-manifest", "verification", "top-level keys")
    if manifest.get("formatVersion") != FORMAT_VERSION or manifest.get("scope") != SCOPE:
        raise SnapshotIntegrityError("invalid-manifest", "verification", "version or scope")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or len(sources) != len(_SOURCE_NAMES):
        raise SnapshotIntegrityError("invalid-manifest", "verification", "sources")
    expected_names = list(_SOURCE_NAMES)
    actual_names: list[str] = []
    source_types: dict[str, str] = {}
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"name", "type"}:
            raise SnapshotIntegrityError("invalid-manifest", "verification", "source")
        name = source.get("name")
        kind = source.get("type")
        if name not in _SOURCE_NAMES or kind != _SOURCE_TYPES.get(name):
            raise SnapshotIntegrityError("invalid-manifest", "verification", "source")
        actual_names.append(name)
        source_types[name] = kind
    if actual_names != expected_names:
        raise SnapshotIntegrityError("invalid-manifest", "verification", "source order")

    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise SnapshotIntegrityError("invalid-manifest", "verification", "entries")
    identities: set[tuple[str, str]] = set()
    roots: dict[str, int] = {name: 0 for name in _SOURCE_NAMES}
    previous: tuple[str, str] | None = None
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "mode",
            "path",
            "sha256",
            "size",
            "source",
            "sourceMode",
            "type",
        }:
            raise SnapshotIntegrityError("invalid-manifest", "verification", "entry keys")
        source = entry.get("source")
        path = _validate_relative_path(entry.get("path"))
        kind = entry.get("type")
        if source not in _SOURCE_NAMES or kind not in {"file", "directory"}:
            raise SnapshotIntegrityError("invalid-manifest", "verification", "entry type")
        identity = (source, path)
        if identity in identities:
            raise SnapshotIntegrityError("duplicate-entry", "verification")
        identities.add(identity)
        if previous is not None and identity <= previous:
            raise SnapshotIntegrityError("invalid-manifest", "verification", "entry order")
        previous = identity
        if path == ".":
            roots[source] += 1
            if kind != source_types[source]:
                raise SnapshotIntegrityError("invalid-manifest", "verification", "root type")
        digest = entry.get("sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise SnapshotIntegrityError("invalid-manifest", "verification", "sha256")
        size = entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise SnapshotIntegrityError("invalid-manifest", "verification", "size")
        source_mode = _parse_mode(entry.get("sourceMode"), "sourceMode")
        mode = _parse_mode(entry.get("mode"), "mode")
        if mode != _safe_mode(kind, source_mode):
            raise SnapshotIntegrityError("unsafe-mode", "verification")
    if any(count != 1 for count in roots.values()):
        raise SnapshotIntegrityError("invalid-manifest", "verification", "source roots")


def _scan_file_fd(
    fd: int,
    item: os.stat_result,
    *,
    source: str,
    relative_path: str,
) -> dict[str, Any]:
    digest = hashlib.sha256()
    byte_count = 0
    while True:
        chunk = os.read(fd, _READ_CHUNK)
        if not chunk:
            break
        digest.update(chunk)
        byte_count += len(chunk)
    if not _same_stat(item, os.fstat(fd)) or byte_count != item.st_size:
        raise SnapshotIntegrityError(
            "snapshot-changed", "verification", f"{source}:{relative_path}"
        )
    return {
        "mode": _mode_text(stat.S_IMODE(item.st_mode)),
        "path": relative_path,
        "sha256": digest.hexdigest(),
        "size": byte_count,
        "source": source,
        "type": "file",
    }


def _scan_directory_fd(
    fd: int,
    item: os.stat_result,
    *,
    source: str,
    relative_path: str,
    seen: dict[tuple[int, int], str],
) -> list[dict[str, Any]]:
    names = sorted(os.listdir(fd), key=os.fsencode)
    descriptions: list[tuple[str, str]] = []
    records: list[dict[str, Any]] = []
    for name in names:
        if name in {".", ".."} or os.sep in name or "\x00" in name:
            raise SnapshotIntegrityError("path-traversal", "verification", name)
        child_stat = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISREG(child_stat.st_mode):
            kind = "file"
            flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        elif stat.S_ISDIR(child_stat.st_mode):
            kind = "directory"
            flags = (
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0)
            )
        elif stat.S_ISLNK(child_stat.st_mode):
            raise SnapshotIntegrityError(
                "symlink-alias", "verification", f"{source}:{relative_path}/{name}"
            )
        else:
            raise SnapshotIntegrityError(
                "special-file", "verification", f"{source}:{relative_path}/{name}"
            )
        child_fd = os.open(name, flags, dir_fd=fd)
        try:
            opened = os.fstat(child_fd)
            if not _same_stat(child_stat, opened):
                raise SnapshotIntegrityError(
                    "snapshot-changed", "verification", f"{source}:{relative_path}/{name}"
                )
            child_path = name if relative_path == "." else f"{relative_path}/{name}"
            _claim_inode(
                child_stat,
                seen,
                f"{source}:{child_path}",
                regular_file=kind == "file",
                stage="verification",
            )
            descriptions.append((kind, name))
            if kind == "file":
                records.append(
                    _scan_file_fd(
                        child_fd,
                        child_stat,
                        source=source,
                        relative_path=child_path,
                    )
                )
            else:
                records.extend(
                    _scan_directory_fd(
                        child_fd,
                        child_stat,
                        source=source,
                        relative_path=child_path,
                        seen=seen,
                    )
                )
        finally:
            os.close(child_fd)
    if sorted(os.listdir(fd), key=os.fsencode) != names or not _same_stat(item, os.fstat(fd)):
        raise SnapshotIntegrityError(
            "snapshot-changed", "verification", f"{source}:{relative_path}"
        )
    records.append(
        {
            "mode": _mode_text(stat.S_IMODE(item.st_mode)),
            "path": relative_path,
            "sha256": _directory_digest(descriptions),
            "size": len(names),
            "source": source,
            "type": "directory",
        }
    )
    return records


def _scan_payload_fd(
    payload_fd: int,
    sources: list[dict[str, str]],
) -> list[dict[str, Any]]:
    payload_item = os.fstat(payload_fd)
    if not stat.S_ISDIR(payload_item.st_mode) or stat.S_IMODE(payload_item.st_mode) != 0o700:
        raise SnapshotIntegrityError("mode-mismatch", "verification", PAYLOAD_NAME)
    actual_names = sorted(os.listdir(payload_fd), key=os.fsencode)
    if actual_names != sorted(_SOURCE_NAMES, key=os.fsencode):
        raise SnapshotIntegrityError("payload-membership-mismatch", "verification")
    seen: dict[tuple[int, int], str] = {}
    records: list[dict[str, Any]] = []
    for source_record in sources:
        source = source_record["name"]
        expected_kind = source_record["type"]
        item = os.stat(source, dir_fd=payload_fd, follow_symlinks=False)
        if stat.S_ISLNK(item.st_mode):
            raise SnapshotIntegrityError("symlink-alias", "verification", source)
        actual_kind = (
            "file"
            if stat.S_ISREG(item.st_mode)
            else "directory"
            if stat.S_ISDIR(item.st_mode)
            else "special"
        )
        if actual_kind != expected_kind:
            raise SnapshotIntegrityError("type-mismatch", "verification", source)
        _claim_inode(
            item,
            seen,
            source,
            regular_file=actual_kind == "file",
            stage="verification",
        )
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        if actual_kind == "directory":
            flags |= os.O_DIRECTORY
        fd = os.open(source, flags, dir_fd=payload_fd)
        try:
            if not _same_stat(item, os.fstat(fd)):
                raise SnapshotIntegrityError("snapshot-changed", "verification", source)
            if actual_kind == "file":
                records.append(
                    _scan_file_fd(fd, item, source=source, relative_path=".")
                )
            else:
                records.extend(
                    _scan_directory_fd(
                        fd,
                        item,
                        source=source,
                        relative_path=".",
                        seen=seen,
                    )
                )
        finally:
            os.close(fd)
    records.sort(key=lambda row: (row["source"], row["path"]))
    return records


def _scan_payload(payload: Path, sources: list[dict[str, str]]) -> list[dict[str, Any]]:
    payload_item = _lstat_chain(
        payload,
        allow_missing_leaf=False,
        stage="verification",
        error_type=SnapshotIntegrityError,
    )
    assert payload_item is not None
    fd = os.open(payload, _DIRECTORY_FLAGS)
    try:
        if not _same_stat(payload_item, os.fstat(fd)):
            raise SnapshotIntegrityError("snapshot-changed", "verification", PAYLOAD_NAME)
        return _scan_payload_fd(fd, sources)
    finally:
        os.close(fd)


def _load_verified_snapshot_fd(root_fd: int) -> tuple[dict[str, Any], str]:
    root_item = os.fstat(root_fd)
    if not stat.S_ISDIR(root_item.st_mode) or stat.S_IMODE(root_item.st_mode) != 0o700:
        raise SnapshotIntegrityError("mode-mismatch", "verification", "snapshot root")
    root_names = sorted(os.listdir(root_fd), key=os.fsencode)
    expected_names = sorted((PAYLOAD_NAME, MANIFEST_NAME, COMPLETION_NAME), key=os.fsencode)
    if root_names != expected_names:
        raise SnapshotIntegrityError("snapshot-membership-mismatch", "verification")

    completion_bytes = _read_metadata_at(root_fd, COMPLETION_NAME)
    completion = _load_json(completion_bytes, COMPLETION_NAME)
    if set(completion) != {
        "formatVersion",
        "manifestSha256",
        "manifestSize",
        "scope",
        "status",
    }:
        raise SnapshotIntegrityError("invalid-completion", "verification")
    if (
        completion.get("formatVersion") != FORMAT_VERSION
        or completion.get("scope") != SCOPE
        or completion.get("status") != "complete"
        or isinstance(completion.get("manifestSize"), bool)
        or not isinstance(completion.get("manifestSize"), int)
    ):
        raise SnapshotIntegrityError("invalid-completion", "verification")

    manifest_bytes = _read_metadata_at(root_fd, MANIFEST_NAME)
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    if completion.get("manifestSize") != len(manifest_bytes) or completion.get(
        "manifestSha256"
    ) != digest:
        raise SnapshotIntegrityError("manifest-digest-mismatch", "verification")
    manifest = _load_json(manifest_bytes, MANIFEST_NAME)
    _validate_manifest(manifest)

    payload_fd = os.open(PAYLOAD_NAME, _DIRECTORY_FLAGS, dir_fd=root_fd)
    try:
        actual = _scan_payload_fd(payload_fd, manifest["sources"])
    finally:
        os.close(payload_fd)
    expected = [
        {key: value for key, value in entry.items() if key != "sourceMode"}
        for entry in manifest["entries"]
    ]
    if actual != expected:
        actual_by_key = {(row["source"], row["path"]): row for row in actual}
        expected_by_key = {(row["source"], row["path"]): row for row in expected}
        if actual_by_key.keys() != expected_by_key.keys():
            code = "payload-membership-mismatch"
        elif any(
            actual_by_key[key]["mode"] != expected_by_key[key]["mode"]
            for key in actual_by_key
        ):
            code = "mode-mismatch"
        elif any(
            actual_by_key[key]["size"] != expected_by_key[key]["size"]
            for key in actual_by_key
        ):
            code = "size-mismatch"
        else:
            code = "digest-mismatch"
        raise SnapshotIntegrityError(code, "verification")
    return manifest, digest


def _load_verified_snapshot(snapshot_root: Path) -> tuple[dict[str, Any], str]:
    root_item = _lstat_chain(
        snapshot_root,
        allow_missing_leaf=False,
        stage="verification",
        error_type=SnapshotIntegrityError,
    )
    assert root_item is not None
    fd = os.open(snapshot_root, _DIRECTORY_FLAGS)
    try:
        if not _same_stat(root_item, os.fstat(fd)):
            raise SnapshotIntegrityError("snapshot-changed", "verification", "snapshot root")
        return _load_verified_snapshot_fd(fd)
    finally:
        os.close(fd)


def _verification_result(manifest: dict[str, Any], digest: str) -> dict[str, Any]:
    return {
        "entryCount": len(manifest["entries"]),
        "formatVersion": FORMAT_VERSION,
        "manifestSha256": digest,
        "scope": SCOPE,
        "sourceCount": len(manifest["sources"]),
        "verified": True,
    }


def verify_evidence_snapshot(
    snapshot_root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Reopen and rehash every snapshot entry and authenticate its completion."""

    root = _path(snapshot_root, "snapshot_root", error_type=SnapshotIntegrityError)
    manifest, digest = _load_verified_snapshot(root)
    return _verification_result(manifest, digest)


def _stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_directory_at(parent_fd: int, name: str) -> int:
    before = _stat_at(parent_fd, name)
    if before is None or not stat.S_ISDIR(before.st_mode):
        raise RestoreError("unsafe-target", "restore")
    fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        after = _stat_at(parent_fd, name)
        if after is None or not _same_stat(before, opened) or not _same_stat(before, after):
            raise RestoreError("target-changed", "restore")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _prepare_restore_target(parent: PinnedParent) -> os.stat_result | None:
    """Validate the target through the retained parent descriptor."""

    item = _stat_at(parent.descriptor, parent.name)
    if item is None:
        return None
    if not stat.S_ISDIR(item.st_mode):
        raise RestoreError("unsafe-target", "restore")
    fd = _open_directory_at(parent.descriptor, parent.name)
    try:
        if os.listdir(fd):
            raise RestoreError("target-not-empty", "restore")
    finally:
        os.close(fd)
    return item


def _new_restore_staging(parent: PinnedParent) -> tuple[str, int, tuple[int, int]]:
    """Create and pin an owner-private sibling under the authorized parent."""

    for _attempt in range(128):
        name = f".{parent.name}.restore-{uuid.uuid4().hex}.tmp"
        try:
            fd = _create_pinned_directory(parent.descriptor, name)
        except FileExistsError:
            continue
        except OSError as exc:
            raise RestoreError(
                "target-create-failed", "restore", exc.__class__.__name__
            ) from None
        opened = os.fstat(fd)
        return name, fd, (opened.st_dev, opened.st_ino)
    raise RestoreError("target-create-failed", "restore", "name-exhausted")


def _clear_restore_directory(fd: int) -> None:
    """Remove all descendants through one already-open directory descriptor."""

    os.fchmod(fd, 0o700)
    for name in os.listdir(fd):
        item = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(item.st_mode):
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=fd)
            try:
                if not _same_stat(item, os.fstat(child_fd)):
                    raise OSError("restore cleanup child identity changed")
                _clear_restore_directory(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)


def _remove_restore_staging(
    parent: PinnedParent,
    staging_name: str,
    staging_fd: int,
    staging_identity: tuple[int, int],
) -> None:
    """Best-effort cleanup of only the exact pinned staging directory."""

    try:
        opened = os.fstat(staging_fd)
        if (opened.st_dev, opened.st_ino) != staging_identity:
            return
        _clear_restore_directory(staging_fd)
        named = _stat_at(parent.descriptor, staging_name)
        if named is not None and (named.st_dev, named.st_ino) == staging_identity:
            os.rmdir(staging_name, dir_fd=parent.descriptor)
            _fsync_directory_fd(parent.descriptor)
    except OSError:
        # Clearing through the retained staging fd prevents copied evidence
        # from being redirected even if its sibling name was disturbed.  A
        # cleanup failure must not mask the authoritative restore exception.
        pass


def _restore_parent_matches(
    parent: PinnedParent,
    forbidden_roots: frozenset[DirectoryIdentity],
) -> None:
    revalidate_pinned_parent(parent)
    if forbidden_roots & pinned_parent_ancestry_identities(parent):
        raise RestoreError("unsafe-destination", "restore")


def _publish_restore(
    parent: PinnedParent,
    staging_name: str,
    staging_fd: int,
    staging_identity: tuple[int, int],
    initial_target: os.stat_result | None,
    forbidden_roots: frozenset[DirectoryIdentity],
) -> None:
    """Atomically publish using only the pinned parent and staging identity."""

    _restore_parent_matches(parent, forbidden_roots)
    staged = _stat_at(parent.descriptor, staging_name)
    if staged is None or (staged.st_dev, staged.st_ino) != staging_identity:
        raise RestoreError("staging-changed", "restore")

    current = _stat_at(parent.descriptor, parent.name)
    if initial_target is None:
        if current is not None:
            raise RestoreError("target-changed", "restore")
    else:
        if current is None or not _same_stat(initial_target, current):
            raise RestoreError("target-changed", "restore")
        current_fd = _open_directory_at(parent.descriptor, parent.name)
        try:
            if os.listdir(current_fd):
                raise RestoreError("target-changed", "restore")
        finally:
            os.close(current_fd)

    renamed = False
    try:
        os.rename(
            staging_name,
            parent.name,
            src_dir_fd=parent.descriptor,
            dst_dir_fd=parent.descriptor,
        )
        renamed = True
        _restore_parent_matches(parent, forbidden_roots)
        _fsync_directory_fd(parent.descriptor)
    except (OSError, SafeFileError, RestoreError) as exc:
        # The rename is the publication cut.  Withdraw only the same staging
        # inode through the retained parent before reporting any late failure.
        try:
            published = _stat_at(parent.descriptor, parent.name)
            if published is not None and (
                published.st_dev,
                published.st_ino,
            ) == staging_identity:
                # Empty the exact retained inode first, then remove only its
                # still-matching published name.  This neither re-resolves the
                # target pathname nor risks replacing an attacker-created
                # sibling at the old staging name.
                _clear_restore_directory(staging_fd)
                named = _stat_at(parent.descriptor, parent.name)
                if named is None or (named.st_dev, named.st_ino) != staging_identity:
                    raise OSError("published restore identity changed")
                os.rmdir(parent.name, dir_fd=parent.descriptor)
                restore_empty_target = initial_target is not None
                if restore_empty_target:
                    try:
                        _restore_parent_matches(parent, forbidden_roots)
                    except (RestoreError, SafeFileError):
                        restore_empty_target = False
                if restore_empty_target:
                    os.mkdir(
                        parent.name,
                        stat.S_IMODE(initial_target.st_mode),
                        dir_fd=parent.descriptor,
                    )
                    os.chmod(
                        parent.name,
                        stat.S_IMODE(initial_target.st_mode),
                        dir_fd=parent.descriptor,
                        follow_symlinks=False,
                    )
        except OSError:
            pass
        if isinstance(exc, RestoreError):
            raise
        if isinstance(exc, SafeFileError):
            raise RestoreError("target-changed", "restore") from exc
        raise RestoreError(
            "target-fsync-failed" if renamed else "target-publish-failed",
            "restore",
            exc.__class__.__name__,
        ) from None


def _entry_destination(root: Path, source: str, relative_path: str) -> Path:
    base = root / source
    if relative_path == ".":
        return base
    return base.joinpath(*relative_path.split("/"))


def _restore_entry_parts(source: str, relative_path: str) -> tuple[str, ...]:
    if relative_path == ".":
        return (source,)
    return (source, *relative_path.split("/"))


def _open_restore_path(root_fd: int, parts: tuple[str, ...]) -> int:
    fd = os.dup(root_fd)
    try:
        for part in parts:
            child_fd = _open_directory_at(fd, part)
            os.close(fd)
            fd = child_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _mkdir_restore_path(root_fd: int, parts: tuple[str, ...]) -> None:
    parent_fd = _open_restore_path(root_fd, parts[:-1])
    try:
        child_fd = _create_pinned_directory(parent_fd, parts[-1])
        os.close(child_fd)
    finally:
        os.close(parent_fd)


def _copy_manifested_file(
    source: Path,
    destination_root_fd: int,
    destination_parts: tuple[str, ...],
    expected: dict[str, Any],
) -> None:
    source_item = os.lstat(source)
    if not stat.S_ISREG(source_item.st_mode) or source_item.st_nlink != 1:
        raise RestoreError("unsafe-snapshot-entry", "restore", str(source))
    source_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, source_flags)
    destination_parent_fd = -1
    destination_fd = -1
    digest = hashlib.sha256()
    size = 0
    try:
        destination_parent_fd = _open_restore_path(
            destination_root_fd,
            destination_parts[:-1],
        )
        destination_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0)
        )
        destination_fd = os.open(
            destination_parts[-1],
            destination_flags,
            0o600,
            dir_fd=destination_parent_fd,
        )
        while True:
            chunk = os.read(source_fd, _READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            _write_all(destination_fd, chunk)
        if not _same_stat(source_item, os.fstat(source_fd)):
            raise RestoreError("snapshot-changed", "restore", str(source))
        if size != expected["size"] or digest.hexdigest() != expected["sha256"]:
            raise RestoreError("snapshot-entry-mismatch", "restore", str(source))
        os.fchmod(destination_fd, _parse_mode(expected["mode"], "mode"))
        os.fsync(destination_fd)
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if destination_parent_fd >= 0:
            os.close(destination_parent_fd)
        os.close(source_fd)


def _finalize_restore_directory(
    staging_fd: int,
    parts: tuple[str, ...],
    mode: int,
) -> None:
    fd = _open_restore_path(staging_fd, parts)
    try:
        os.fchmod(fd, mode)
        _fsync_directory_fd(fd)
    finally:
        os.close(fd)


def restore_evidence_snapshot(
    snapshot_root: str | os.PathLike[str],
    restore_target: str | os.PathLike[str],
    *,
    repository_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Transactionally restore into a new or empty target, then rehash it.

    The target receives one child per named source.  Existing content is never
    overwritten.  Snapshot and restored bytes are both verified around the copy.
    Bytes are assembled in a private sibling and atomically published only after
    verification, so copy failures never leave a partially populated final target.
    """

    snapshot = _path(snapshot_root, "snapshot_root", error_type=SnapshotIntegrityError)
    target = _path(restore_target, "restore_target", error_type=RestoreError)
    if _is_within(target, snapshot) or _is_within(snapshot, target):
        raise RestoreError("target-overlaps-snapshot", "restore", str(target))
    repository: Path | None = None
    if repository_root is not None:
        repository = _path(repository_root, "repository_root", error_type=RestoreError)
        repository_item = _lstat_chain(
            repository,
            allow_missing_leaf=False,
            stage="repository-root",
            error_type=RestoreError,
        )
        if repository_item is None or not stat.S_ISDIR(repository_item.st_mode):
            raise RestoreError("wrong-source-type", "repository-root")
        if _is_within(target, repository):
            raise RestoreError("target-inside-repository", "restore", str(target))
    manifest, digest = _load_verified_snapshot(snapshot)
    try:
        forbidden_roots: set[DirectoryIdentity] = {
            capture_directory_identity(snapshot),
        }
        if repository is not None:
            forbidden_roots.add(capture_directory_identity(repository))
    except SafeFileError as exc:
        raise RestoreError("unsafe-destination", "restore") from exc
    entries = manifest["entries"]
    try:
        with pinned_parent(target, create_parents=False) as destination_parent:
            forbidden = frozenset(forbidden_roots)
            _restore_parent_matches(destination_parent, forbidden)
            initial_target = _prepare_restore_target(destination_parent)
            staging_name, staging_fd, staging_identity = _new_restore_staging(
                destination_parent
            )
            published = False
            try:
                directories = sorted(
                    (entry for entry in entries if entry["type"] == "directory"),
                    key=lambda entry: (
                        entry["source"],
                        0
                        if entry["path"] == "."
                        else len(entry["path"].split("/")),
                        entry["path"],
                    ),
                )
                for entry in directories:
                    _mkdir_restore_path(
                        staging_fd,
                        _restore_entry_parts(entry["source"], entry["path"]),
                    )

                for entry in entries:
                    if entry["type"] != "file":
                        continue
                    source = _entry_destination(
                        snapshot / PAYLOAD_NAME,
                        entry["source"],
                        entry["path"],
                    )
                    _copy_manifested_file(
                        source,
                        staging_fd,
                        _restore_entry_parts(entry["source"], entry["path"]),
                        entry,
                    )

                for entry in reversed(directories):
                    _finalize_restore_directory(
                        staging_fd,
                        _restore_entry_parts(entry["source"], entry["path"]),
                        _parse_mode(entry["mode"], "mode"),
                    )
                _fsync_directory_fd(staging_fd)

                # Catch changes to the source snapshot during restore, then
                # rehash through the retained staging descriptor.
                _, final_digest = _load_verified_snapshot(snapshot)
                if final_digest != digest:
                    raise RestoreError("snapshot-changed", "restore")
                restored = _scan_payload_fd(staging_fd, manifest["sources"])
                expected = [
                    {
                        key: value
                        for key, value in entry.items()
                        if key != "sourceMode"
                    }
                    for entry in entries
                ]
                if restored != expected:
                    raise RestoreError("restore-verification-failed", "restore")
                _publish_restore(
                    destination_parent,
                    staging_name,
                    staging_fd,
                    staging_identity,
                    initial_target,
                    forbidden,
                )
                published = True
            finally:
                if not published:
                    _remove_restore_staging(
                        destination_parent,
                        staging_name,
                        staging_fd,
                        staging_identity,
                    )
                os.close(staging_fd)
    except EvidenceBackupError:
        raise
    except SafeFileError as exc:
        raise RestoreError("unsafe-destination", "restore") from exc
    except OSError as exc:
        raise RestoreError("restore-failed", "restore", exc.__class__.__name__) from None

    return {
        "entryCount": len(entries),
        "formatVersion": FORMAT_VERSION,
        "manifestSha256": digest,
        "scope": SCOPE,
        "sourceCount": len(manifest["sources"]),
        "verified": True,
    }
