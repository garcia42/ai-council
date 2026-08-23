"""Private, content-addressed custody for exact Tier-1 council artifacts.

The ledger stores only the small mapping returned by :meth:`ArtifactStore.capture`.
Artifact bytes stay below an explicitly configured root.  The store deliberately
does not discover a default location: callers must select a private path outside a
Git working tree.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


ARTIFACT_REF_KEYS = frozenset({"path", "sha256", "bytes"})
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_REF_PATH_RE = re.compile(
    r"sha256/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})\.bin\Z"
)
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_READ_CHUNK = 1024 * 1024
_GIT_OBJECT_FORMATS = {"sha1": 40, "sha256": 64}
_AT_EMPTY_PATH = 0x1000
_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.linkat.argtypes = [
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_int,
]
_LIBC.linkat.restype = ctypes.c_int

# These are intentionally narrow.  Broad matches such as ``password=`` and JWTs
# produce too many false positives for exact prompt capture.  A launcher that
# knows actual credentials must also pass them via ``secret_tokens``.
_BUILTIN_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
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


@dataclass(frozen=True)
class ArtifactIncident:
    """Non-secret metadata suitable for an append-only invalidation event."""

    code: str
    stage: str
    artifact_path: str | None = None
    detectors: tuple[str, ...] = ()
    recovery_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "stage": self.stage}
        if self.artifact_path is not None:
            result["artifactPath"] = self.artifact_path
        if self.detectors:
            result["detectors"] = list(self.detectors)
        if self.recovery_path is not None:
            result["recoveryPath"] = self.recovery_path
        return result


class ArtifactError(ValueError):
    """Base failure carrying safe incident metadata, never captured bytes."""

    def __init__(self, incident: ArtifactIncident):
        self.incident = incident
        message = f"artifact incident {incident.code} during {incident.stage}"
        if incident.recovery_path is not None:
            message += f"; recoverable artifact retained: {incident.recovery_path}"
        super().__init__(message)


class ArtifactPolicyError(ArtifactError):
    """The configured root or artifact reference violates custody policy."""


class SecretDetectedError(ArtifactError):
    """Capture was rejected by preflight before any filesystem write."""


class ArtifactIntegrityError(ArtifactError):
    """An artifact is absent, aliased, changed, or otherwise unverifiable."""


class ArtifactWriteError(ArtifactError):
    """An artifact could not be durably created."""


def _incident(
    code: str,
    stage: str,
    *,
    artifact_path: str | None = None,
    detectors: Iterable[str] = (),
    recovery_path: str | None = None,
) -> ArtifactIncident:
    return ArtifactIncident(
        code=code,
        stage=stage,
        artifact_path=artifact_path,
        detectors=tuple(detectors),
        recovery_path=recovery_path,
    )


def _coerce_bytes(data: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("artifact data must be bytes-like")
    return bytes(data)


def secret_detectors(
    data: bytes | bytearray | memoryview,
    *,
    secret_tokens: Iterable[bytes | bytearray | memoryview] = (),
) -> tuple[str, ...]:
    """Return detector identifiers only; never return matching secret bytes."""

    content = _coerce_bytes(data)
    found = [name for name, pattern in _BUILTIN_SECRET_PATTERNS if pattern.search(content)]
    caller_match = False
    for token in secret_tokens:
        candidate = _coerce_bytes(token)
        if not candidate:
            raise ValueError("secret tokens must be non-empty")
        if candidate in content:
            caller_match = True
    if caller_match:
        found.append("caller-token")
    return tuple(dict.fromkeys(found))


def _artifact_path(digest: str) -> str:
    return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}.bin"


def compute_git_blob_oid(
    data: bytes | bytearray | memoryview, *, object_format: str = "sha1"
) -> str:
    """Compute the Git object ID for exact blob bytes.

    A Git blob ID is *not* the digest of the payload alone.  Git hashes the
    object header ``b"blob <length>\\0"`` followed by the exact bytes.  SHA-1 is
    the normal repository format; SHA-256 is supported for repositories created
    with Git's SHA-256 object format.
    """

    content = _coerce_bytes(data)
    if object_format not in _GIT_OBJECT_FORMATS:
        raise ArtifactPolicyError(_incident("unsupported-git-object-format", "git-blob"))
    header = b"blob " + str(len(content)).encode("ascii") + b"\0"
    digest = hashlib.new(object_format, usedforsecurity=False)
    digest.update(header)
    digest.update(content)
    return digest.hexdigest()


def verify_git_blob_oid(
    data: bytes | bytearray | memoryview, expected_oid: str
) -> str:
    """Verify a 40- or 64-hex Git blob ID and return its normalized value."""

    if not isinstance(expected_oid, str):
        raise ArtifactPolicyError(_incident("invalid-git-blob-oid", "git-blob"))
    if len(expected_oid) == _GIT_OBJECT_FORMATS["sha1"]:
        object_format = "sha1"
    elif len(expected_oid) == _GIT_OBJECT_FORMATS["sha256"]:
        object_format = "sha256"
    else:
        raise ArtifactPolicyError(_incident("invalid-git-blob-oid", "git-blob"))
    if any(character not in "0123456789abcdef" for character in expected_oid):
        raise ArtifactPolicyError(_incident("invalid-git-blob-oid", "git-blob"))
    if compute_git_blob_oid(data, object_format=object_format) != expected_oid:
        raise ArtifactIntegrityError(_incident("git-blob-mismatch", "git-blob"))
    return expected_oid


def validate_artifact_ref(ref: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize the exact ledger-safe artifact reference shape."""

    if not isinstance(ref, Mapping) or set(ref) != ARTIFACT_REF_KEYS:
        raise ArtifactPolicyError(_incident("invalid-reference", "reference"))
    path = ref.get("path")
    digest = ref.get("sha256")
    byte_count = ref.get("bytes")
    if not isinstance(path, str) or not isinstance(digest, str):
        raise ArtifactPolicyError(_incident("invalid-reference", "reference"))
    if not _DIGEST_RE.fullmatch(digest):
        raise ArtifactPolicyError(_incident("invalid-reference", "reference"))
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
        raise ArtifactPolicyError(_incident("invalid-reference", "reference"))
    match = _REF_PATH_RE.fullmatch(path)
    if match is None:
        raise ArtifactPolicyError(_incident("invalid-reference", "reference"))
    first, second, path_digest = match.groups()
    if path_digest != digest or first != digest[:2] or second != digest[2:4]:
        raise ArtifactPolicyError(_incident("invalid-reference", "reference"))
    return {"path": path, "sha256": digest, "bytes": byte_count}


def _safe_close(fd: int | None) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


@dataclass(frozen=True)
class _RootCustody:
    """Open descriptors binding the configured root name to its inode."""

    root_fd: int
    parent_fd: int
    name: str


class ArtifactStore:
    """Capture and verify immutable Tier-1 artifacts below one private root.

    Construction performs no filesystem I/O.  In particular, secret preflight in
    :meth:`capture` happens before the root or any content directory is created.
    """

    def __init__(self, root: str | os.PathLike[str]):
        raw = os.fspath(root)
        if not isinstance(raw, str) or not raw or not os.path.isabs(raw):
            raise ArtifactPolicyError(_incident("invalid-root", "configuration"))
        parts = Path(raw).parts
        if raw == os.path.sep or any(part in {".", ".."} for part in parts):
            raise ArtifactPolicyError(_incident("invalid-root", "configuration"))
        self._root = os.path.normpath(raw)
        self._root_parts = Path(self._root).parts[1:]

    @property
    def root(self) -> Path:
        return Path(self._root)

    def capture(
        self,
        data: bytes | bytearray | memoryview,
        *,
        secret_tokens: Iterable[bytes | bytearray | memoryview] = (),
    ) -> dict[str, Any]:
        """Durably store exact bytes or return their already verified reference."""

        content = _coerce_bytes(data)
        detectors = secret_detectors(content, secret_tokens=secret_tokens)
        if detectors:
            raise SecretDetectedError(
                _incident("secret-detected", "preflight", detectors=detectors)
            ) from None

        digest = hashlib.sha256(content).hexdigest()
        ref = {
            "path": _artifact_path(digest),
            "sha256": digest,
            "bytes": len(content),
        }
        custody: _RootCustody | None = None
        parent_fd: int | None = None
        try:
            custody = self._open_root(create=True)
            parent_fd = self._open_content_parent(custody.root_fd, digest, create=True)
            fcntl.flock(parent_fd, fcntl.LOCK_EX)
            leaf_identity = self._capture_locked(parent_fd, digest, content, ref)
            self._revalidate_success(
                custody,
                parent_fd,
                digest,
                ref,
                stage="capture",
                expected_leaf_identity=leaf_identity,
            )
            return ref
        except ArtifactError:
            raise
        except OSError:
            raise ArtifactWriteError(
                _incident("write-failed", "capture", artifact_path=ref["path"])
            ) from None
        finally:
            if parent_fd is not None:
                try:
                    fcntl.flock(parent_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            _safe_close(parent_fd)
            if custody is not None:
                _safe_close(custody.root_fd)
                _safe_close(custody.parent_fd)

    def verify(self, ref: Mapping[str, Any]) -> dict[str, Any]:
        """Re-read and authenticate an artifact through safely opened descriptors."""

        normalized = validate_artifact_ref(ref)
        digest = normalized["sha256"]
        custody: _RootCustody | None = None
        parent_fd: int | None = None
        try:
            custody = self._open_root(create=False)
            parent_fd = self._open_content_parent(custody.root_fd, digest, create=False)
            fcntl.flock(parent_fd, fcntl.LOCK_SH)
            self._revalidate_success(
                custody, parent_fd, digest, normalized, stage="verify"
            )
            return normalized
        except ArtifactError:
            raise
        except OSError:
            raise ArtifactIntegrityError(
                _incident(
                    "artifact-unverifiable",
                    "verify",
                    artifact_path=normalized["path"],
                )
            ) from None
        finally:
            if parent_fd is not None:
                try:
                    fcntl.flock(parent_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            _safe_close(parent_fd)
            if custody is not None:
                _safe_close(custody.root_fd)
                _safe_close(custody.parent_fd)

    def read_verified(self, ref: Mapping[str, Any]) -> bytes:
        """Return exact bytes authenticated through one safely pinned open file."""

        normalized = validate_artifact_ref(ref)
        digest = normalized["sha256"]
        custody: _RootCustody | None = None
        parent_fd: int | None = None
        try:
            custody = self._open_root(create=False)
            parent_fd = self._open_content_parent(custody.root_fd, digest, create=False)
            fcntl.flock(parent_fd, fcntl.LOCK_SH)
            content = self._revalidate_success(
                custody,
                parent_fd,
                digest,
                normalized,
                stage="read-verified",
                return_content=True,
            )
            assert content is not None
            return content
        except ArtifactError:
            raise
        except OSError:
            raise ArtifactIntegrityError(
                _incident(
                    "artifact-unverifiable",
                    "read-verified",
                    artifact_path=normalized["path"],
                )
            ) from None
        finally:
            if parent_fd is not None:
                try:
                    fcntl.flock(parent_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            _safe_close(parent_fd)
            if custody is not None:
                _safe_close(custody.root_fd)
                _safe_close(custody.parent_fd)

    def _open_root(self, *, create: bool) -> _RootCustody:
        current = os.open(
            os.path.sep,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            self._reject_git_marker(current)
            for index, component in enumerate(self._root_parts):
                final = index == len(self._root_parts) - 1
                child, created = self._open_or_create_directory(
                    current,
                    component,
                    create=create,
                    stage="artifact-root",
                )
                try:
                    self._reject_git_marker(child)
                    if created:
                        os.fchmod(child, _DIRECTORY_MODE)
                        os.fsync(child)
                        # Persist every newly created root-path component, not only
                        # the final content directory.  Otherwise a successful
                        # artifact fsync could still point through a directory entry
                        # lost on power failure.
                        os.fsync(current)
                except Exception:
                    _safe_close(child)
                    raise
                if final:
                    try:
                        self._require_directory_policy(child, "artifact-root")
                        custody = _RootCustody(
                            root_fd=child,
                            parent_fd=current,
                            name=component,
                        )
                        self._require_root_binding(custody, stage="artifact-root")
                    except Exception:
                        _safe_close(child)
                        raise
                    return custody
                _safe_close(current)
                current = child
            raise ArtifactPolicyError(_incident("invalid-root", "configuration"))
        except Exception:
            _safe_close(current)
            raise

    def _require_root_binding(self, custody: _RootCustody, *, stage: str) -> None:
        """Require the configured leaf name to identify the retained root inode."""

        opened = os.fstat(custody.root_fd)
        configured_parent_fd: int | None = None
        try:
            configured_parent_fd = self._open_configured_root_parent()
            pinned_parent = os.fstat(custody.parent_fd)
            configured_parent = os.fstat(configured_parent_fd)
            if (
                not stat.S_ISDIR(pinned_parent.st_mode)
                or not stat.S_ISDIR(configured_parent.st_mode)
                or (pinned_parent.st_dev, pinned_parent.st_ino)
                != (configured_parent.st_dev, configured_parent.st_ino)
            ):
                raise ArtifactPolicyError(
                    _incident("artifact-root-detached", stage)
                ) from None
            current = os.stat(
                custody.name,
                dir_fd=configured_parent_fd,
                follow_symlinks=False,
            )
        except ArtifactError:
            raise
        except OSError:
            raise ArtifactPolicyError(
                _incident("artifact-root-detached", stage)
            ) from None
        finally:
            _safe_close(configured_parent_fd)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or stat.S_IMODE(opened.st_mode) != _DIRECTORY_MODE
            or stat.S_IMODE(current.st_mode) != _DIRECTORY_MODE
            or opened.st_uid != os.geteuid()
            or current.st_uid != os.geteuid()
        ):
            raise ArtifactPolicyError(
                _incident("artifact-root-detached", stage)
            ) from None

    def _open_configured_root_parent(self) -> int:
        """Safely reopen the configured absolute namespace down to root's parent."""

        current = os.open(
            os.path.sep,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            self._reject_git_marker(current)
            for component in self._root_parts[:-1]:
                child, _created = self._open_or_create_directory(
                    current,
                    component,
                    create=False,
                    stage="artifact-root",
                )
                try:
                    self._reject_git_marker(child)
                except Exception:
                    _safe_close(child)
                    raise
                _safe_close(current)
                current = child
            return current
        except Exception:
            _safe_close(current)
            raise

    def _revalidate_success(
        self,
        custody: _RootCustody,
        retained_parent_fd: int,
        digest: str,
        ref: Mapping[str, Any],
        *,
        stage: str,
        return_content: bool = False,
        expected_leaf_identity: tuple[int, int] | None = None,
    ) -> bytes | None:
        """Authenticate the current configured namespace at the success boundary."""

        # The first binding check prevents a verification request from accepting
        # bytes reached only through a root descriptor detached before traversal.
        self._require_root_binding(custody, stage=stage)
        name = f"{digest}.bin"
        if expected_leaf_identity is None:
            expected_leaf_identity = self._named_leaf_identity(
                retained_parent_fd, name, ref["path"], stage=stage
            )
        current_parent_fd: int | None = None
        final_parent_fd: int | None = None
        try:
            current_parent_fd = self._open_content_parent(
                custody.root_fd, digest, create=False
            )
            self._require_same_directory(
                retained_parent_fd, current_parent_fd, ref["path"], stage=stage
            )
            self._require_leaf_identity(
                current_parent_fd,
                name,
                expected_leaf_identity,
                ref["path"],
                stage=stage,
            )
            self._verify_named_file(
                current_parent_fd, digest, ref, stage=stage
            )
            self._require_leaf_identity(
                current_parent_fd,
                name,
                expected_leaf_identity,
                ref["path"],
                stage=stage,
            )

            # Reopen through the retained, still-named root and authenticate the
            # leaf again.  This closes both the content-directory and leaf
            # check/use handoffs at the point at which success is reported.
            self._require_root_binding(custody, stage=stage)
            final_parent_fd = self._open_content_parent(
                custody.root_fd, digest, create=False
            )
            self._require_same_directory(
                retained_parent_fd, final_parent_fd, ref["path"], stage=stage
            )
            self._require_leaf_identity(
                final_parent_fd,
                name,
                expected_leaf_identity,
                ref["path"],
                stage=stage,
            )
            content = self._verify_named_file(
                final_parent_fd,
                digest,
                ref,
                stage=stage,
                return_content=return_content,
            )
            self._require_root_binding(custody, stage=stage)
            self._require_leaf_identity(
                final_parent_fd,
                name,
                expected_leaf_identity,
                ref["path"],
                stage=stage,
            )
            return content
        finally:
            _safe_close(final_parent_fd)
            _safe_close(current_parent_fd)

    @staticmethod
    def _require_same_directory(
        retained_fd: int,
        current_fd: int,
        artifact_path: str,
        *,
        stage: str,
    ) -> None:
        retained = os.fstat(retained_fd)
        current = os.fstat(current_fd)
        if (
            not stat.S_ISDIR(retained.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (retained.st_dev, retained.st_ino)
            != (current.st_dev, current.st_ino)
            or stat.S_IMODE(retained.st_mode) != _DIRECTORY_MODE
            or stat.S_IMODE(current.st_mode) != _DIRECTORY_MODE
            or retained.st_uid != os.geteuid()
            or current.st_uid != os.geteuid()
        ):
            raise ArtifactIntegrityError(
                _incident(
                    "artifact-directory-aliased",
                    stage,
                    artifact_path=artifact_path,
                )
            ) from None

    @staticmethod
    def _named_leaf_identity(
        parent_fd: int,
        name: str,
        artifact_path: str,
        *,
        stage: str,
    ) -> tuple[int, int]:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            raise ArtifactIntegrityError(
                _incident("artifact-missing", stage, artifact_path=artifact_path)
            ) from None
        if not stat.S_ISREG(current.st_mode):
            raise ArtifactIntegrityError(
                _incident("unsafe-artifact", stage, artifact_path=artifact_path)
            ) from None
        if stat.S_IMODE(current.st_mode) != _FILE_MODE:
            raise ArtifactIntegrityError(
                _incident("wrong-mode", stage, artifact_path=artifact_path)
            ) from None
        if current.st_uid != os.geteuid():
            raise ArtifactIntegrityError(
                _incident("wrong-owner", stage, artifact_path=artifact_path)
            ) from None
        if current.st_nlink != 1:
            raise ArtifactIntegrityError(
                _incident("hard-link-alias", stage, artifact_path=artifact_path)
            ) from None
        return current.st_dev, current.st_ino

    @classmethod
    def _require_leaf_identity(
        cls,
        parent_fd: int,
        name: str,
        expected: tuple[int, int],
        artifact_path: str,
        *,
        stage: str,
    ) -> None:
        if cls._named_leaf_identity(
            parent_fd, name, artifact_path, stage=stage
        ) != expected:
            raise ArtifactIntegrityError(
                _incident("artifact-aliased", stage, artifact_path=artifact_path)
            ) from None

    def _open_content_parent(self, root_fd: int, digest: str, *, create: bool) -> int:
        current = os.dup(root_fd)
        try:
            for component in ("sha256", digest[:2], digest[2:4]):
                child, created = self._open_or_create_directory(
                    current,
                    component,
                    create=create,
                    stage="artifact-directory",
                )
                try:
                    if created:
                        os.fchmod(child, _DIRECTORY_MODE)
                        os.fsync(child)
                        os.fsync(current)
                    self._require_directory_policy(child, "artifact-directory")
                except Exception:
                    _safe_close(child)
                    raise
                _safe_close(current)
                current = child
            return current
        except Exception:
            _safe_close(current)
            raise

    @staticmethod
    def _open_or_create_directory(
        parent_fd: int,
        component: str,
        *,
        create: bool,
        stage: str,
    ) -> tuple[int, bool]:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            return os.open(component, flags, dir_fd=parent_fd), False
        except FileNotFoundError:
            if not create:
                raise ArtifactIntegrityError(
                    _incident("artifact-missing", stage)
                ) from None
            try:
                os.mkdir(component, _DIRECTORY_MODE, dir_fd=parent_fd)
                created = True
                # A restrictive umask can produce a mode-000 directory that
                # cannot be reopened even by its owner.  Tighten/restore the
                # exact private mode through the already opened parent before
                # opening the new directory descriptor.  The new entry is
                # owner-only (and therefore cannot be populated by another
                # user) throughout this transition.
                os.chmod(component, _DIRECTORY_MODE, dir_fd=parent_fd)
            except FileExistsError:
                created = False
            try:
                return os.open(component, flags, dir_fd=parent_fd), created
            except OSError:
                raise ArtifactPolicyError(
                    _incident("unsafe-directory", stage)
                ) from None
        except OSError:
            raise ArtifactPolicyError(_incident("unsafe-directory", stage)) from None

    @staticmethod
    def _reject_git_marker(directory_fd: int) -> None:
        try:
            os.stat(".git", dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError:
            raise ArtifactPolicyError(
                _incident("git-boundary-unverifiable", "artifact-root")
            ) from None
        raise ArtifactPolicyError(
            _incident("root-inside-git", "artifact-root")
        ) from None

    @staticmethod
    def _require_directory_policy(directory_fd: int, stage: str) -> None:
        info = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != _DIRECTORY_MODE
            or info.st_uid != os.geteuid()
        ):
            raise ArtifactPolicyError(
                _incident("unsafe-directory", stage)
            ) from None

    def _capture_locked(
        self,
        parent_fd: int,
        digest: str,
        content: bytes,
        ref: Mapping[str, Any],
    ) -> tuple[int, int]:
        name = f"{digest}.bin"
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC
        )
        fd: int | None = None
        created = False
        try:
            try:
                fd = os.open(name, flags, _FILE_MODE, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                self._verify_named_file(parent_fd, digest, ref, stage="capture")
                return self._named_leaf_identity(
                    parent_fd, name, ref["path"], stage="capture"
                )
            os.fchmod(fd, _FILE_MODE)
            view = memoryview(content)
            offset = 0
            while offset < len(view):
                written = os.write(fd, view[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "short artifact write")
                offset += written
            self._verify_open_file(fd, ref, stage="capture")
            os.fsync(fd)
            self._require_same_directory_entry(
                parent_fd, name, fd, ref["path"], stage="capture"
            )
            os.fsync(parent_fd)
            opened = os.fstat(fd)
            return opened.st_dev, opened.st_ino
        except ArtifactError as exc:
            if created:
                recovery_path = self._retain_created_escrow(
                    parent_fd, name, fd, ref["path"]
                )
                raise self._with_recovery_path(exc, recovery_path) from exc
            raise
        except OSError:
            recovery_path = None
            if created:
                recovery_path = self._retain_created_escrow(
                    parent_fd, name, fd, ref["path"]
                )
            raise ArtifactWriteError(
                _incident(
                    "write-failed",
                    "capture",
                    artifact_path=ref["path"],
                    recovery_path=recovery_path,
                )
            ) from None
        finally:
            _safe_close(fd)

    @staticmethod
    def _with_recovery_path(exc: ArtifactError, recovery_path: str) -> ArtifactError:
        incident = exc.incident
        return type(exc)(
            _incident(
                incident.code,
                incident.stage,
                artifact_path=incident.artifact_path,
                detectors=incident.detectors,
                recovery_path=recovery_path,
            )
        )

    def _retain_created_escrow(
        self,
        parent_fd: int,
        original_name: str,
        fd: int | None,
        artifact_path: str,
    ) -> str:
        """Durably retain the opened inode without touching the mutable leaf name."""

        if fd is None:
            raise ArtifactWriteError(
                _incident("recovery-failed", "capture", artifact_path=artifact_path)
            ) from None
        escrow_name: str | None = None
        for _attempt in range(16):
            candidate = f".{original_name}.recovery.{uuid.uuid4().hex}.escrow"
            try:
                self._link_open_file(fd, parent_fd, candidate)
                escrow_name = candidate
                break
            except FileExistsError:
                continue
            except OSError:
                raise ArtifactWriteError(
                    _incident(
                        "recovery-failed", "capture", artifact_path=artifact_path
                    )
                ) from None
        if escrow_name is None:
            raise ArtifactWriteError(
                _incident("recovery-failed", "capture", artifact_path=artifact_path)
            ) from None

        directory = artifact_path.rsplit("/", 1)[0]
        relative_path = f"{directory}/{escrow_name}"
        recovery_path = os.path.join(self._root, *relative_path.split("/"))
        try:
            # A failed first durability attempt may be transient.  The recovery
            # receipt is returned only after both inode bytes and its directory
            # entry have been synchronized and the entry still names this fd.
            os.fsync(fd)
            os.fsync(parent_fd)
            opened = os.fstat(fd)
            retained = os.stat(
                escrow_name, dir_fd=parent_fd, follow_symlinks=False
            )
            if (
                not stat.S_ISREG(retained.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (retained.st_dev, retained.st_ino)
                or stat.S_IMODE(retained.st_mode) != _FILE_MODE
                or retained.st_uid != os.geteuid()
            ):
                raise OSError(errno.EIO, "recovery escrow identity changed")
        except OSError:
            raise ArtifactWriteError(
                _incident(
                    "recovery-failed",
                    "capture",
                    artifact_path=artifact_path,
                    recovery_path=recovery_path,
                )
            ) from None
        return recovery_path

    @staticmethod
    def _link_open_file(fd: int, parent_fd: int, name: str) -> None:
        result = _LIBC.linkat(
            fd,
            ctypes.c_char_p(b""),
            parent_fd,
            ctypes.c_char_p(os.fsencode(name)),
            _AT_EMPTY_PATH,
        )
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), name)

    def _verify_named_file(
        self,
        parent_fd: int,
        digest: str,
        ref: Mapping[str, Any],
        *,
        stage: str = "verify",
        return_content: bool = False,
    ) -> bytes | None:
        name = f"{digest}.bin"
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            raise ArtifactIntegrityError(
                _incident("artifact-missing", stage, artifact_path=ref["path"])
            ) from None
        except OSError:
            raise ArtifactIntegrityError(
                _incident("unsafe-artifact", stage, artifact_path=ref["path"])
            ) from None
        try:
            content = self._verify_open_file(
                fd, ref, stage=stage, return_content=return_content
            )
            self._require_same_directory_entry(
                parent_fd, name, fd, ref["path"], stage=stage
            )
            return content
        finally:
            _safe_close(fd)

    @staticmethod
    def _verify_open_file(
        fd: int,
        ref: Mapping[str, Any],
        *,
        stage: str,
        return_content: bool = False,
    ) -> bytes | None:
        before = os.fstat(fd)
        path = ref["path"]
        if not stat.S_ISREG(before.st_mode):
            raise ArtifactIntegrityError(
                _incident("unsafe-artifact", stage, artifact_path=path)
            ) from None
        if stat.S_IMODE(before.st_mode) != _FILE_MODE:
            raise ArtifactIntegrityError(
                _incident("wrong-mode", stage, artifact_path=path)
            ) from None
        if before.st_uid != os.geteuid():
            raise ArtifactIntegrityError(
                _incident("wrong-owner", stage, artifact_path=path)
            ) from None
        if before.st_nlink != 1:
            raise ArtifactIntegrityError(
                _incident("hard-link-alias", stage, artifact_path=path)
            ) from None
        if before.st_size != ref["bytes"]:
            raise ArtifactIntegrityError(
                _incident("length-mismatch", stage, artifact_path=path)
            ) from None

        os.lseek(fd, 0, os.SEEK_SET)
        hasher = hashlib.sha256()
        byte_count = 0
        chunks: list[bytes] | None = [] if return_content else None
        while True:
            chunk = os.read(fd, _READ_CHUNK)
            if not chunk:
                break
            hasher.update(chunk)
            byte_count += len(chunk)
            if chunks is not None:
                chunks.append(chunk)
        after = os.fstat(fd)
        identity_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in identity_fields):
            raise ArtifactIntegrityError(
                _incident("changed-during-verification", stage, artifact_path=path)
            ) from None
        if byte_count != ref["bytes"]:
            raise ArtifactIntegrityError(
                _incident("length-mismatch", stage, artifact_path=path)
            ) from None
        if hasher.hexdigest() != ref["sha256"]:
            raise ArtifactIntegrityError(
                _incident("digest-mismatch", stage, artifact_path=path)
            ) from None
        return b"".join(chunks) if chunks is not None else None

    @staticmethod
    def _require_same_directory_entry(
        parent_fd: int,
        name: str,
        opened_fd: int,
        artifact_path: str,
        *,
        stage: str = "verify",
    ) -> None:
        opened = os.fstat(opened_fd)
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            raise ArtifactIntegrityError(
                _incident("artifact-missing", stage, artifact_path=artifact_path)
            ) from None
        if (
            not stat.S_ISREG(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or stat.S_IMODE(current.st_mode) != _FILE_MODE
            or current.st_uid != os.geteuid()
            or current.st_nlink != 1
            or current.st_size != opened.st_size
            or current.st_mtime_ns != opened.st_mtime_ns
            or current.st_ctime_ns != opened.st_ctime_ns
        ):
            raise ArtifactIntegrityError(
                _incident("artifact-aliased", stage, artifact_path=artifact_path)
            ) from None


def capture_artifact(
    root: str | os.PathLike[str],
    data: bytes | bytearray | memoryview,
    *,
    secret_tokens: Iterable[bytes | bytearray | memoryview] = (),
) -> dict[str, Any]:
    """Functional wrapper for one exact artifact capture."""

    return ArtifactStore(root).capture(data, secret_tokens=secret_tokens)


def verify_artifact(
    root: str | os.PathLike[str], ref: Mapping[str, Any]
) -> dict[str, Any]:
    """Functional wrapper for one artifact verification."""

    return ArtifactStore(root).verify(ref)
