"""Dirfd-pinned filesystem mutations for append-only council evidence.

Every path component is opened relative to its already-pinned parent with
``O_NOFOLLOW``.  Mutations therefore never re-resolve an attacker-swappable
pathname, and existing files must be single-link regular files owned by the
current effective user.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


DirectoryFsyncCallback = Callable[[Path], None]


class SafeFileError(ValueError):
    """A path cannot be mutated without preserving filesystem identity."""


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    size: int


@dataclass(frozen=True)
class DirectoryIdentity:
    """Stable identity of one opened directory inode."""

    device: int
    inode: int


@dataclass(frozen=True)
class TransactionEscrow:
    """One retained JSONL transaction entry discovered without opening it."""

    path: Path
    size: int
    entry_type: str


@dataclass(frozen=True)
class PinnedMutationTarget:
    """Authorization facts derived from the actual pinned mutation parent.

    ``lexical_path`` is diagnostic context only.  Authority decisions must use
    ``parent_identity``, ``ancestor_identities``, and (when present)
    ``target_identity`` because a plain directory can be renamed after an
    earlier lexical classification.
    """

    lexical_path: Path
    name: str
    parent_identity: DirectoryIdentity
    ancestor_identities: frozenset[DirectoryIdentity]
    target_identity: FileIdentity | None

    def parent_is_within(self, root: DirectoryIdentity) -> bool:
        """Return whether ``root`` is the pinned parent's current ancestor."""

        return root in self.ancestor_identities


MutationAuthorizationCallback = Callable[[PinnedMutationTarget], None]


@dataclass
class PinnedParent:
    path: Path
    descriptor: int
    name: str

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


@dataclass(frozen=True)
class PinnedLock:
    parent: PinnedParent
    name: str
    descriptor: int
    identity: FileIdentity

    def revalidate(self) -> None:
        path = self.parent.path / self.name
        opened = os.fstat(self.descriptor)
        _validate_regular(opened, path)
        named = _stat_name(_sibling(self.parent, self.name))
        if named is None:
            raise SafeFileError(f"transaction lock name disappeared: {path}")
        _validate_regular(named, path)
        expected = (self.identity.device, self.identity.inode)
        if (opened.st_dev, opened.st_ino) != expected or (
            named.st_dev,
            named.st_ino,
        ) != expected:
            raise SafeFileError(f"transaction lock identity changed: {path}")


@dataclass(frozen=True)
class PinnedFileTransaction:
    """One file name and its parent directory pinned for a whole transaction.

    Callers may acquire a sibling lock and then read, validate, and mutate the
    target through this object without ever resolving the lexical parent again.
    """

    parent: PinnedParent
    on_directory_fsync: DirectoryFsyncCallback | None = None
    lock: PinnedLock | None = None
    authorize_mutation: MutationAuthorizationCallback | None = None

    def revalidate_lock(self) -> None:
        if self.lock is None:
            raise SafeFileError("pinned file transaction has no bound lock identity")
        self.lock.revalidate()

    def revalidate_mutation_authority(self) -> None:
        """Re-run authority against this still-pinned parent and target name."""

        _invoke_mutation_authorization(self.parent, self.authorize_mutation)

    @property
    def path(self) -> Path:
        return self.parent.path / self.parent.name

    def sibling(self, name: str) -> PinnedFileTransaction:
        """Return a non-owning target view over this exact pinned transaction.

        The returned view shares the parent dirfd, parent-directory flock, and
        original lock identity. It is valid only while the owning transaction's
        context is active.
        """

        self.revalidate_lock()
        sibling_parent = _sibling(self.parent, name)
        return PinnedFileTransaction(
            sibling_parent,
            self.on_directory_fsync,
            self.lock,
            self.authorize_mutation,
        )

    def read_bytes(self, *, missing_ok: bool = False) -> bytes:
        self.revalidate_lock()
        return _read_regular_bytes_pinned(self.parent, missing_ok=missing_ok)

    def append_bytes(self, data: bytes, *, mode: int = 0o600) -> None:
        self.revalidate_lock()
        self.revalidate_mutation_authority()
        _append_bytes_pinned(
            self.parent,
            data,
            mode=mode,
            on_directory_fsync=self.on_directory_fsync,
        )

    def atomic_append_bytes(
        self,
        suffix: bytes,
        *,
        require_trailing_newline: bool = False,
        mode: int = 0o600,
    ) -> Path | None:
        self.revalidate_lock()
        self.revalidate_mutation_authority()
        return _atomic_append_bytes_pinned(
            self.parent,
            suffix,
            require_trailing_newline=require_trailing_newline,
            mode=mode,
            on_directory_fsync=self.on_directory_fsync,
        )

    def atomic_replace_bytes(
        self,
        data: bytes,
        *,
        expected_sha256: str | None = None,
        require_existing: bool = True,
        mode: int = 0o600,
    ) -> Path | None:
        self.revalidate_lock()
        self.revalidate_mutation_authority()
        return _atomic_replace_locked(
            self.parent,
            data,
            expected_sha256=expected_sha256,
            require_existing=require_existing,
            mode=mode,
            on_directory_fsync=self.on_directory_fsync,
        )

    @contextmanager
    def exclusive_sibling_lock(
        self,
        name: str,
        *,
        timeout_seconds: float,
    ) -> Iterator[PinnedLock]:
        sibling = _sibling(self.parent, name)
        with _exclusive_lock_pinned(
            sibling,
            timeout_seconds=timeout_seconds,
            on_directory_fsync=self.on_directory_fsync,
        ) as lock:
            yield lock


_NOFOLLOW = getattr(os, "O_NOFOLLOW", None)
_DIRECTORY = getattr(os, "O_DIRECTORY", None)
if _NOFOLLOW is None or _DIRECTORY is None:  # pragma: no cover - Linux contract
    raise RuntimeError("safe council evidence writes require O_NOFOLLOW and O_DIRECTORY")

_DIR_FLAGS = os.O_RDONLY | _DIRECTORY | os.O_CLOEXEC | _NOFOLLOW
_DIRECTORY_LOCK_STATE = threading.local()

_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
try:
    _RENAMEAT2 = ctypes.CDLL(None, use_errno=True).renameat2
except AttributeError:  # pragma: no cover - fail-closed Linux contract
    _RENAMEAT2 = None
else:
    _RENAMEAT2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _RENAMEAT2.restype = ctypes.c_int


def _absolute(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _fsync_fd(descriptor: int) -> None:
    os.fsync(descriptor)


def _notify_fsync(
    callback: DirectoryFsyncCallback | None, *paths: Path
) -> None:
    if callback is None:
        return
    for path in paths:
        callback(path)


@contextmanager
def pinned_parent(
    path: str | os.PathLike[str],
    *,
    create_parents: bool,
    on_directory_fsync: DirectoryFsyncCallback | None = None,
) -> Iterator[PinnedParent]:
    """Pin a target's parent by walking every component without following links."""

    target = _absolute(path)
    if not target.name or target.name in (".", ".."):
        raise SafeFileError(f"unsafe target name: {target}")

    current_fd = os.open("/", _DIR_FLAGS)
    current_path = Path("/")
    try:
        for component in target.parent.parts[1:]:
            try:
                next_fd = os.open(component, _DIR_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                if not create_parents:
                    raise SafeFileError(
                        f"safe parent does not exist: {current_path / component}"
                    ) from None
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise SafeFileError(
                        f"cannot create safe parent: {current_path / component}"
                    ) from exc
                _fsync_fd(current_fd)
                try:
                    next_fd = os.open(component, _DIR_FLAGS, dir_fd=current_fd)
                except OSError as exc:
                    raise SafeFileError(
                        f"unsafe parent component: {current_path / component}"
                    ) from exc
                _fsync_fd(next_fd)
                _notify_fsync(
                    on_directory_fsync,
                    current_path / component,
                    current_path,
                )
            except OSError as exc:
                raise SafeFileError(
                    f"unsafe parent component: {current_path / component}"
                ) from exc

            info = os.fstat(next_fd)
            if not stat.S_ISDIR(info.st_mode):
                os.close(next_fd)
                raise SafeFileError(
                    f"unsafe parent component: {current_path / component}"
                )
            os.close(current_fd)
            current_fd = next_fd
            current_path /= component

        pinned = PinnedParent(target.parent, current_fd, target.name)
        current_fd = -1
        try:
            yield pinned
        finally:
            pinned.close()
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def _identity(info: os.stat_result) -> FileIdentity:
    return FileIdentity(info.st_dev, info.st_ino, info.st_mode, info.st_size)


def _directory_identity(info: os.stat_result) -> DirectoryIdentity:
    if not stat.S_ISDIR(info.st_mode):
        raise SafeFileError("pinned mutation parent is not a directory")
    return DirectoryIdentity(info.st_dev, info.st_ino)


def capture_directory_identity(path: str | os.PathLike[str]) -> DirectoryIdentity:
    """Capture a directory inode through a no-follow component walk.

    This is intended for pre-capturing protected live-root identities.  A
    later mutation callback compares those identities with ancestry walked
    from its actually pinned parent descriptor.
    """

    target = _absolute(path)
    probe = target / ".council-directory-identity"
    with pinned_parent(probe, create_parents=False) as parent:
        return _directory_identity(os.fstat(parent.descriptor))


def pinned_parent_ancestry_identities(
    parent: PinnedParent,
) -> frozenset[DirectoryIdentity]:
    """Walk current inode ancestry from an already-open pinned parent."""

    descriptor = os.dup(parent.descriptor)
    identities: set[DirectoryIdentity] = set()
    try:
        for _depth in range(4096):
            current = _directory_identity(os.fstat(descriptor))
            identities.add(current)
            try:
                parent_descriptor = os.open("..", _DIR_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                raise SafeFileError(
                    "cannot inspect pinned mutation parent ancestry"
                ) from exc
            parent_identity = _directory_identity(os.fstat(parent_descriptor))
            os.close(descriptor)
            descriptor = parent_descriptor
            if parent_identity == current:
                return frozenset(identities)
        raise SafeFileError("pinned mutation parent ancestry is too deep")
    finally:
        os.close(descriptor)


def revalidate_pinned_parent(parent: PinnedParent) -> None:
    """Require the pinned directory to remain at its original lexical name.

    Mutations must continue to use ``parent.descriptor`` regardless of this
    check.  Reopening the path here is only a fail-closed namespace check: it
    catches a renamed parent, a replacement directory, or a symlink
    substitution without ever granting the reopened path mutation authority.
    """

    probe = parent.path / parent.name
    with pinned_parent(probe, create_parents=False) as current:
        expected = _directory_identity(os.fstat(parent.descriptor))
        actual = _directory_identity(os.fstat(current.descriptor))
        if actual != expected:
            raise SafeFileError(
                f"pinned parent namespace identity changed: {parent.path}"
            )


def pinned_mutation_target(parent: PinnedParent) -> PinnedMutationTarget:
    """Describe the exact parent/name that subsequent dirfd mutation will use."""

    target = _stat_name(parent)
    target_identity: FileIdentity | None = None
    if target is not None:
        _validate_regular(target, parent.path / parent.name)
        target_identity = _identity(target)
    parent_identity = _directory_identity(os.fstat(parent.descriptor))
    return PinnedMutationTarget(
        lexical_path=parent.path / parent.name,
        name=parent.name,
        parent_identity=parent_identity,
        ancestor_identities=pinned_parent_ancestry_identities(parent),
        target_identity=target_identity,
    )


def _invoke_mutation_authorization(
    parent: PinnedParent,
    callback: MutationAuthorizationCallback | None,
) -> None:
    if callback is not None:
        callback(pinned_mutation_target(parent))


def _validate_regular(info: os.stat_result, path: Path) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
    ):
        raise SafeFileError(
            f"unsafe file identity (symlink, special file, or hardlink alias): {path}"
        )


def _stat_name(parent: PinnedParent) -> os.stat_result | None:
    try:
        return os.stat(
            parent.name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None


def _sibling(parent: PinnedParent, name: str) -> PinnedParent:
    if (
        not isinstance(name, str)
        or not name
        or name in (".", "..")
        or Path(name).name != name
    ):
        raise SafeFileError("safe sibling name must be one plain path component")
    return PinnedParent(parent.path, parent.descriptor, name)


def _open_existing(
    parent: PinnedParent, flags: int
) -> tuple[int, FileIdentity]:
    before = _stat_name(parent)
    if before is None:
        raise FileNotFoundError(parent.path / parent.name)
    _validate_regular(before, parent.path / parent.name)
    try:
        descriptor = os.open(
            parent.name,
            flags | os.O_CLOEXEC | _NOFOLLOW,
            dir_fd=parent.descriptor,
        )
    except OSError as exc:
        raise SafeFileError(f"cannot safely open file: {parent.path / parent.name}") from exc
    try:
        opened = os.fstat(descriptor)
        _validate_regular(opened, parent.path / parent.name)
        after = _stat_name(parent)
        if after is None or (after.st_dev, after.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise SafeFileError(f"file identity changed during open: {parent.path / parent.name}")
        return descriptor, _identity(opened)
    except BaseException:
        os.close(descriptor)
        raise


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise SafeFileError("filesystem write made no progress")
        view = view[written:]


def fsync_directory(path: str | os.PathLike[str]) -> None:
    """Fsync a directory reached through a no-follow component walk."""

    target = _absolute(path)
    probe = target / ".council-fsync-anchor"
    with pinned_parent(probe, create_parents=False) as parent:
        _fsync_fd(parent.descriptor)


def _append_bytes_pinned(
    parent: PinnedParent,
    data: bytes,
    *,
    mode: int,
    on_directory_fsync: DirectoryFsyncCallback | None,
) -> None:
    existing = _stat_name(parent)
    if existing is None:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL
    else:
        _validate_regular(existing, parent.path / parent.name)
        flags = os.O_WRONLY | os.O_APPEND
    try:
        descriptor = os.open(
            parent.name,
            flags | os.O_CLOEXEC | _NOFOLLOW,
            mode,
            dir_fd=parent.descriptor,
        )
    except OSError as exc:
        raise SafeFileError(
            f"cannot safely append file: {parent.path / parent.name}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        _validate_regular(opened, parent.path / parent.name)
        named = _stat_name(parent)
        if named is None or (named.st_dev, named.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise SafeFileError(
                f"file identity changed before append: {parent.path / parent.name}"
            )
        _write_all(descriptor, data)
        _fsync_fd(descriptor)
    finally:
        os.close(descriptor)
    if existing is None:
        _fsync_fd(parent.descriptor)
        _notify_fsync(on_directory_fsync, parent.path)


def append_bytes(
    path: str | os.PathLike[str],
    data: bytes,
    *,
    mode: int = 0o600,
    on_directory_fsync: DirectoryFsyncCallback | None = None,
) -> None:
    """Append bytes to a pinned single-link regular file and fsync the result."""

    with pinned_parent(
        path,
        create_parents=True,
        on_directory_fsync=on_directory_fsync,
    ) as parent:
        _append_bytes_pinned(
            parent,
            data,
            mode=mode,
            on_directory_fsync=on_directory_fsync,
        )


def _read_regular_bytes_pinned(
    parent: PinnedParent, *, missing_ok: bool
) -> bytes:
    if _stat_name(parent) is None and missing_ok:
        return b""
    try:
        descriptor, _identity_value = _open_existing(parent, os.O_RDONLY)
    except FileNotFoundError as exc:
        raise SafeFileError(f"file does not exist: {parent.path / parent.name}") from exc
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def read_regular_bytes(path: str | os.PathLike[str]) -> bytes:
    """Read a pinned existing single-link regular file."""

    with pinned_parent(path, create_parents=False) as parent:
        return _read_regular_bytes_pinned(parent, missing_ok=False)


def _temporary_name(name: str) -> str:
    return f".{name}.{uuid.uuid4().hex}.tmp"


def inventory_transaction_escrows(
    path: str | os.PathLike[str],
) -> tuple[TransactionEscrow, ...]:
    """Inventory retained replacements for ``path`` without reading their bytes.

    Escrows are operational custody artifacts, never alternative ledger input.
    Matching and stat calls are descriptor-relative and never follow links.
    """

    target = _absolute(path)
    if not target.parent.exists():
        return ()
    pattern = re.compile(
        rf"^\.{re.escape(target.name)}\.[0-9a-f]{{32}}\.tmp\.escrow\."
        rf"[0-9a-f]{{32}}$"
    )
    with pinned_parent(target, create_parents=False) as parent:
        entries: list[TransactionEscrow] = []
        try:
            names = os.listdir(parent.descriptor)
        except OSError as exc:
            raise SafeFileError(
                f"cannot inventory transaction escrows: {target.parent}"
            ) from exc
        for name in sorted(item for item in names if pattern.fullmatch(item)):
            try:
                info = os.stat(
                    name,
                    dir_fd=parent.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                # A concurrent operator reconciliation may move an entry only
                # while writers/reporters are not quiescent. Do not invent it.
                continue
            if stat.S_ISREG(info.st_mode):
                entry_type = "regular"
            elif stat.S_ISLNK(info.st_mode):
                entry_type = "symlink"
            elif stat.S_ISDIR(info.st_mode):
                entry_type = "directory"
            else:
                entry_type = "special"
            entries.append(
                TransactionEscrow(
                    path=target.parent / name,
                    size=info.st_size,
                    entry_type=entry_type,
                )
            )
        return tuple(entries)


def _require_renameat2() -> None:
    if _RENAMEAT2 is None:
        raise SafeFileError(
            "safe atomic publication requires Linux renameat2 support"
        )


def _renameat2_pinned(
    parent: PinnedParent,
    source_name: str,
    target_name: str,
    flags: int,
) -> None:
    """Rename two names in the retained parent without re-resolving its path."""

    _require_renameat2()
    assert _RENAMEAT2 is not None
    ctypes.set_errno(0)
    result = _RENAMEAT2(
        parent.descriptor,
        os.fsencode(source_name),
        parent.descriptor,
        os.fsencode(target_name),
        flags,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
        raise SafeFileError(
            "safe atomic publication requires renameat2 exchange/no-replace support"
        )
    raise OSError(
        error_number,
        os.strerror(error_number),
        f"{source_name} -> {target_name}",
    )


def _exchange_names_pinned(
    parent: PinnedParent, source_name: str, target_name: str
) -> None:
    _renameat2_pinned(parent, source_name, target_name, _RENAME_EXCHANGE)


def _rename_noreplace_pinned(
    parent: PinnedParent, source_name: str, target_name: str
) -> None:
    _renameat2_pinned(parent, source_name, target_name, _RENAME_NOREPLACE)


def _same_inode(info: os.stat_result | None, identity: FileIdentity) -> bool:
    return info is not None and (info.st_dev, info.st_ino) == (
        identity.device,
        identity.inode,
    )


def _stat_sibling(parent: PinnedParent, name: str) -> os.stat_result | None:
    return _stat_name(_sibling(parent, name))


def _reverse_exchange_if_unchanged(
    parent: PinnedParent,
    temporary_name: str,
    *,
    expected_target: FileIdentity,
    expected_temporary: FileIdentity,
) -> bool:
    """Reverse a publication only while both exchange entries retain custody.

    The exchange itself is non-destructive: if an uncooperative writer races
    the final precheck, both objects remain named even though their names may
    be swapped.  A failed postcheck therefore returns ``False`` and leaves all
    entries recoverable instead of guessing which pathname is ours.
    """

    target_before = _stat_name(parent)
    temporary_before = _stat_sibling(parent, temporary_name)
    if not _same_inode(target_before, expected_target) or not _same_inode(
        temporary_before, expected_temporary
    ):
        return False
    try:
        _exchange_names_pinned(parent, temporary_name, parent.name)
    except OSError:
        return False
    target_after = _stat_name(parent)
    temporary_after = _stat_sibling(parent, temporary_name)
    return _same_inode(target_after, expected_temporary) and _same_inode(
        temporary_after, expected_target
    )


def _retain_escrow_name(
    parent: PinnedParent,
    name: str,
    expected_identity: FileIdentity,
) -> tuple[Path, bool]:
    """Move a mutable temporary name to a unique, recoverable escrow.

    There is deliberately no unlink here.  Linux has no unlink-by-expected-
    inode operation, so a stat/open check followed by ``unlinkat`` always has
    one final substitution cut.  A no-replace rename is non-destructive even
    if the source changes at that cut: whichever inode occupies ``name`` is
    retained under the exact returned pathname, and the identity result tells
    the caller whether it was the inode the transaction expected.
    """

    for _attempt in range(8):
        escrow_name = f"{name}.escrow.{uuid.uuid4().hex}"
        try:
            _rename_noreplace_pinned(parent, name, escrow_name)
        except FileExistsError:  # UUID collision; source remains untouched.
            continue
        except FileNotFoundError:
            return parent.path / name, False
        observed = _stat_sibling(parent, escrow_name)
        escrow_path = parent.path / escrow_name
        retained_expected = _same_inode(observed, expected_identity)
        try:
            _fsync_fd(parent.descriptor)
        except OSError as exc:
            raise SafeFileError(
                f"cannot synchronize recoverable transaction escrow: {escrow_path}"
            ) from exc
        return escrow_path, retained_expected
    return parent.path / name, False


def _digest_descriptor(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _authenticate_descriptor_bytes(
    descriptor: int,
    identity: FileIdentity,
    expected_data: bytes,
    path: Path,
) -> None:
    before = os.fstat(descriptor)
    _validate_regular(before, path)
    if not _same_inode(before, identity):
        raise SafeFileError(f"file descriptor identity changed: {path}")
    if before.st_size != len(expected_data) or _digest_descriptor(
        descriptor
    ) != hashlib.sha256(expected_data).hexdigest():
        raise SafeFileError(f"file content changed under descriptor custody: {path}")
    after = os.fstat(descriptor)
    _validate_regular(after, path)
    if not _same_inode(after, identity) or after.st_size != len(expected_data):
        raise SafeFileError(f"file identity changed during authentication: {path}")


def _atomic_replace_locked(
    parent: PinnedParent,
    data: bytes,
    *,
    expected_sha256: str | None,
    require_existing: bool,
    mode: int,
    on_directory_fsync: DirectoryFsyncCallback | None,
) -> Path | None:
    _require_renameat2()
    existing = _stat_name(parent)
    existing_identity: FileIdentity | None = None
    existing_mode = mode
    if existing is not None:
        _validate_regular(existing, parent.path / parent.name)
        descriptor, existing_identity = _open_existing(parent, os.O_RDONLY)
        try:
            if expected_sha256 is not None:
                digest = hashlib.sha256()
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                if digest.hexdigest() != expected_sha256:
                    raise SafeFileError(
                        f"file content changed before replace: {parent.path / parent.name}"
                    )
            existing_mode = stat.S_IMODE(existing_identity.mode)
        finally:
            os.close(descriptor)
    elif require_existing:
        raise SafeFileError(f"file does not exist: {parent.path / parent.name}")

    temporary_name = _temporary_name(parent.name)
    temporary_fd = -1
    temporary_identity: FileIdentity | None = None
    cleanup_identity: FileIdentity | None = None
    publication_succeeded = False
    retained_escrow: Path | None = None
    operation_error: Exception | None = None
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | _NOFOLLOW,
            existing_mode,
            dir_fd=parent.descriptor,
        )
        temporary_info = os.fstat(temporary_fd)
        _validate_regular(temporary_info, parent.path / temporary_name)
        temporary_identity = _identity(temporary_info)
        cleanup_identity = temporary_identity
        os.fchmod(temporary_fd, existing_mode)
        _write_all(temporary_fd, data)
        _fsync_fd(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = -1

        current = _stat_name(parent)
        if existing_identity is None:
            if current is not None:
                raise SafeFileError(
                    f"file appeared before atomic replace: {parent.path / parent.name}"
                )
        elif current is None or (current.st_dev, current.st_ino) != (
            existing_identity.device,
            existing_identity.inode,
        ):
            raise SafeFileError(
                f"file identity changed before atomic replace: {parent.path / parent.name}"
            )

        assert temporary_identity is not None
        if existing_identity is None:
            try:
                _rename_noreplace_pinned(parent, temporary_name, parent.name)
            except FileExistsError as exc:
                raise SafeFileError(
                    f"file appeared before atomic publication: "
                    f"{parent.path / parent.name}"
                ) from exc
            published = _stat_name(parent)
            retained_temporary = _stat_sibling(parent, temporary_name)
            if not _same_inode(published, temporary_identity) or (
                retained_temporary is not None
            ):
                cleanup_identity = None
                raise SafeFileError(
                    f"atomic no-replace publication identity changed: "
                    f"{parent.path / parent.name}"
                )
            cleanup_identity = None
        else:
            _exchange_names_pinned(parent, temporary_name, parent.name)
            published = _stat_name(parent)
            displaced = _stat_sibling(parent, temporary_name)
            if not _same_inode(published, temporary_identity) or not _same_inode(
                displaced, existing_identity
            ):
                displaced_identity = (
                    _identity(displaced) if displaced is not None else None
                )
                reversed_cleanly = False
                if (
                    _same_inode(published, temporary_identity)
                    and displaced_identity is not None
                ):
                    reversed_cleanly = _reverse_exchange_if_unchanged(
                        parent,
                        temporary_name,
                        expected_target=temporary_identity,
                        expected_temporary=displaced_identity,
                    )
                cleanup_identity = temporary_identity if reversed_cleanly else None
                if reversed_cleanly:
                    raise SafeFileError(
                        f"concurrent file substitution detected and reversed: "
                        f"{parent.path / parent.name}"
                    )
                raise SafeFileError(
                    f"concurrent file substitution detected; entries retained: "
                    f"{parent.path / parent.name}"
                )
            cleanup_identity = existing_identity

        try:
            _fsync_fd(parent.descriptor)
        except BaseException as exc:
            reversed_cleanly = False
            if existing_identity is not None:
                reversed_cleanly = _reverse_exchange_if_unchanged(
                    parent,
                    temporary_name,
                    expected_target=temporary_identity,
                    expected_temporary=existing_identity,
                )
                cleanup_identity = temporary_identity if reversed_cleanly else None
            else:
                target_now = _stat_name(parent)
                temporary_now = _stat_sibling(parent, temporary_name)
                if _same_inode(target_now, temporary_identity) and temporary_now is None:
                    try:
                        _rename_noreplace_pinned(
                            parent, parent.name, temporary_name
                        )
                    except OSError:
                        pass
                    else:
                        reversed_cleanly = _same_inode(
                            _stat_sibling(parent, temporary_name), temporary_identity
                        ) and _stat_name(parent) is None
                cleanup_identity = temporary_identity if reversed_cleanly else None
            if reversed_cleanly:
                try:
                    _fsync_fd(parent.descriptor)
                except OSError as rollback_sync_error:
                    cleanup_identity = None
                    raise SafeFileError(
                        f"atomic publication durability failed and rollback was not durable: "
                        f"{parent.path / parent.name}"
                    ) from rollback_sync_error
                raise SafeFileError(
                    f"atomic publication durability failed and was reversed: "
                    f"{parent.path / parent.name}"
                ) from exc
            cleanup_identity = None
            raise SafeFileError(
                f"atomic publication durability failed; entries retained: "
                f"{parent.path / parent.name}"
            ) from exc

        _notify_fsync(on_directory_fsync, parent.path)
        publication_succeeded = True
    except Exception as exc:
        operation_error = exc
        raise
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if cleanup_identity is not None:
            retained_escrow, retained_expected = _retain_escrow_name(
                parent, temporary_name, cleanup_identity
            )
            if operation_error is not None:
                raise SafeFileError(
                    f"{operation_error}; recoverable transaction escrow retained: "
                    f"{retained_escrow}"
                ) from operation_error
            if not retained_expected:
                message = (
                    "temporary escrow identity changed; recoverable entry retained: "
                    f"{retained_escrow}"
                )
                if publication_succeeded:
                    raise SafeFileError(message)
                raise SafeFileError(message)
    return retained_escrow


def atomic_replace_bytes(
    path: str | os.PathLike[str],
    data: bytes,
    *,
    expected_sha256: str | None = None,
    require_existing: bool = True,
    mode: int = 0o600,
    on_directory_fsync: DirectoryFsyncCallback | None = None,
) -> Path | None:
    """Atomically replace a pinned file after rechecking identity and content."""

    with pinned_parent(
        path,
        create_parents=not require_existing,
        on_directory_fsync=on_directory_fsync,
    ) as parent:
        return _atomic_replace_locked(
            parent,
            data,
            expected_sha256=expected_sha256,
            require_existing=require_existing,
            mode=mode,
            on_directory_fsync=on_directory_fsync,
        )


def _atomic_append_bytes_pinned(
    parent: PinnedParent,
    suffix: bytes,
    *,
    require_trailing_newline: bool,
    mode: int,
    on_directory_fsync: DirectoryFsyncCallback | None,
) -> Path | None:
    existing = _read_regular_bytes_pinned(parent, missing_ok=True)
    info = _stat_name(parent)
    expected_sha256 = hashlib.sha256(existing).hexdigest() if info is not None else None
    if require_trailing_newline and existing and not existing.endswith(b"\n"):
        raise SafeFileError(
            f"file has a torn trailing record: {parent.path / parent.name}"
        )
    return _atomic_replace_locked(
        parent,
        existing + suffix,
        expected_sha256=expected_sha256,
        require_existing=info is not None,
        mode=mode,
        on_directory_fsync=on_directory_fsync,
    )


def atomic_append_bytes(
    path: str | os.PathLike[str],
    suffix: bytes,
    *,
    require_trailing_newline: bool = False,
    mode: int = 0o600,
    on_directory_fsync: DirectoryFsyncCallback | None = None,
) -> Path | None:
    """Atomically replace a pinned file with its prior bytes plus ``suffix``."""

    with pinned_parent(
        path,
        create_parents=True,
        on_directory_fsync=on_directory_fsync,
    ) as parent:
        return _atomic_append_bytes_pinned(
            parent,
            suffix,
            require_trailing_newline=require_trailing_newline,
            mode=mode,
            on_directory_fsync=on_directory_fsync,
        )


def create_bytes_exclusive(
    path: str | os.PathLike[str],
    data: bytes,
    *,
    mode: int = 0o600,
    on_directory_fsync: DirectoryFsyncCallback | None = None,
) -> None:
    """Publish complete authenticated bytes without replacing an existing name."""

    _require_renameat2()
    with pinned_parent(
        path,
        create_parents=True,
        on_directory_fsync=on_directory_fsync,
    ) as parent:
        if _stat_name(parent) is not None:
            raise SafeFileError(f"file already exists: {parent.path / parent.name}")
        temporary_name = _temporary_name(parent.name)
        descriptor = -1
        staged_identity: FileIdentity | None = None
        published = False
        retained_escrow: Path | None = None
        operation_error: Exception | None = None
        try:
            descriptor = os.open(
                temporary_name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | _NOFOLLOW,
                mode,
                dir_fd=parent.descriptor,
            )
        except OSError as exc:
            raise SafeFileError(
                f"cannot safely stage exclusive file: {parent.path / parent.name}"
            ) from exc
        try:
            info = os.fstat(descriptor)
            _validate_regular(info, parent.path / temporary_name)
            staged_identity = _identity(info)
            os.fchmod(descriptor, mode)
            _write_all(descriptor, data)
            _fsync_fd(descriptor)
            _authenticate_descriptor_bytes(
                descriptor,
                staged_identity,
                data,
                parent.path / temporary_name,
            )

            staged_name = _stat_sibling(parent, temporary_name)
            if not _same_inode(staged_name, staged_identity):
                raise SafeFileError(
                    f"exclusive staging identity changed before publication: "
                    f"{parent.path / temporary_name}"
                )
            try:
                _rename_noreplace_pinned(parent, temporary_name, parent.name)
            except FileExistsError as exc:
                raise SafeFileError(
                    f"file appeared before exclusive publication: "
                    f"{parent.path / parent.name}"
                ) from exc

            leaf = _stat_name(parent)
            if not _same_inode(leaf, staged_identity):
                raise SafeFileError(
                    f"exclusive published identity changed: {parent.path / parent.name}"
                )
            try:
                _fsync_fd(parent.descriptor)
            except OSError as exc:
                target_now = _stat_name(parent)
                temporary_now = _stat_sibling(parent, temporary_name)
                reversed_cleanly = False
                if _same_inode(target_now, staged_identity) and temporary_now is None:
                    try:
                        _rename_noreplace_pinned(
                            parent, parent.name, temporary_name
                        )
                    except OSError:
                        pass
                    else:
                        reversed_cleanly = _same_inode(
                            _stat_sibling(parent, temporary_name), staged_identity
                        ) and _stat_name(parent) is None
                if reversed_cleanly:
                    try:
                        _fsync_fd(parent.descriptor)
                    except OSError as rollback_error:
                        raise SafeFileError(
                            "exclusive publication durability failed and rollback "
                            f"was not durable: {parent.path / parent.name}"
                        ) from rollback_error
                    raise SafeFileError(
                        "exclusive publication durability failed and was reversed: "
                        f"{parent.path / parent.name}"
                    ) from exc
                raise SafeFileError(
                    "exclusive publication durability failed; entries retained: "
                    f"{parent.path / parent.name}"
                ) from exc

            _notify_fsync(on_directory_fsync, parent.path)
            _authenticate_descriptor_bytes(
                descriptor,
                staged_identity,
                data,
                parent.path / parent.name,
            )
            final_leaf = _stat_name(parent)
            if not _same_inode(final_leaf, staged_identity):
                raise SafeFileError(
                    f"exclusive published identity changed before success: "
                    f"{parent.path / parent.name}"
                )
            published = True
        except Exception as exc:
            operation_error = exc
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if not published and staged_identity is not None:
                staged_name = _stat_sibling(parent, temporary_name)
                if staged_name is not None:
                    retained_escrow, retained_expected = _retain_escrow_name(
                        parent,
                        temporary_name,
                        staged_identity,
                    )
                    if operation_error is not None:
                        raise SafeFileError(
                            f"{operation_error}; recoverable exclusive escrow retained: "
                            f"{retained_escrow}"
                        ) from operation_error
                    if not retained_expected:
                        raise SafeFileError(
                            "exclusive escrow identity changed; recoverable entry retained: "
                            f"{retained_escrow}"
                        )


@contextmanager
def _exclusive_lock_pinned(
    parent: PinnedParent,
    *,
    timeout_seconds: float,
    on_directory_fsync: DirectoryFsyncCallback | None,
) -> Iterator[PinnedLock]:
    existing = _stat_name(parent)
    created = False
    if existing is None:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    else:
        _validate_regular(existing, parent.path / parent.name)
        flags = os.O_RDWR
    try:
        descriptor = os.open(
            parent.name,
            flags | os.O_CLOEXEC | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            0o600,
            dir_fd=parent.descriptor,
        )
        created = existing is None
    except FileExistsError:
        # Two first writers may both observe a missing lock.  The losing
        # O_EXCL creator must reopen and validate the winner's inode rather
        # than turn a safe creation race into an application failure.
        try:
            descriptor, _identity_value = _open_existing(
                parent,
                os.O_RDWR | getattr(os, "O_NONBLOCK", 0),
            )
        except (OSError, SafeFileError) as exc:
            raise SafeFileError(
                f"cannot safely open lock: {parent.path / parent.name}"
            ) from exc
    except OSError as exc:
        raise SafeFileError(
            f"cannot safely open lock: {parent.path / parent.name}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        _validate_regular(info, parent.path / parent.name)
        named = _stat_name(parent)
        if named is None or (named.st_dev, named.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            raise SafeFileError(
                f"lock identity changed during open: {parent.path / parent.name}"
            )
        if created:
            os.fchmod(descriptor, 0o600)
            _fsync_fd(descriptor)
            _fsync_fd(parent.descriptor)
            _notify_fsync(on_directory_fsync, parent.path)
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise SafeFileError(
                        f"timed out after {timeout_seconds:g}s waiting for lock: "
                        f"{parent.path / parent.name}"
                    ) from exc
                time.sleep(0.05)
        lock = PinnedLock(parent, parent.name, descriptor, _identity(info))
        try:
            yield lock
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def _shared_lock_pinned(
    parent: PinnedParent,
    *,
    timeout_seconds: float,
    on_directory_fsync: DirectoryFsyncCallback | None,
) -> Iterator[PinnedLock]:
    """Take a shared flock on one pinned, identity-checked lock-file inode."""

    existing = _stat_name(parent)
    created = False
    if existing is None:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    else:
        _validate_regular(existing, parent.path / parent.name)
        flags = os.O_RDWR
    try:
        descriptor = os.open(
            parent.name,
            flags | os.O_CLOEXEC | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            0o600,
            dir_fd=parent.descriptor,
        )
        created = existing is None
    except FileExistsError:
        try:
            descriptor, _identity_value = _open_existing(
                parent,
                os.O_RDWR | getattr(os, "O_NONBLOCK", 0),
            )
        except (OSError, SafeFileError) as exc:
            raise SafeFileError(
                f"cannot safely open lock: {parent.path / parent.name}"
            ) from exc
    except OSError as exc:
        raise SafeFileError(
            f"cannot safely open lock: {parent.path / parent.name}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        _validate_regular(info, parent.path / parent.name)
        named = _stat_name(parent)
        if named is None or (named.st_dev, named.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            raise SafeFileError(
                f"lock identity changed during open: {parent.path / parent.name}"
            )
        if created:
            os.fchmod(descriptor, 0o600)
            _fsync_fd(descriptor)
            _fsync_fd(parent.descriptor)
            _notify_fsync(on_directory_fsync, parent.path)
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise SafeFileError(
                        f"timed out after {timeout_seconds:g}s waiting for shared lock: "
                        f"{parent.path / parent.name}"
                    ) from exc
                time.sleep(0.05)
        lock = PinnedLock(parent, parent.name, descriptor, _identity(info))
        try:
            yield lock
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def exclusive_lock(
    path: str | os.PathLike[str],
    *,
    timeout_seconds: float,
    on_directory_fsync: DirectoryFsyncCallback | None = None,
    authorize_mutation: MutationAuthorizationCallback | None = None,
) -> Iterator[None]:
    """Take an exclusive flock on a pinned, safe, single-link lock file."""

    with pinned_parent(
        path,
        create_parents=True,
        on_directory_fsync=on_directory_fsync,
    ) as parent:
        _invoke_mutation_authorization(parent, authorize_mutation)
        with _exclusive_directory_transaction_lock(
            parent,
            timeout_seconds=timeout_seconds,
        ):
            with _exclusive_lock_pinned(
                parent,
                timeout_seconds=timeout_seconds,
                on_directory_fsync=on_directory_fsync,
            ) as lock:
                lock.revalidate()
                try:
                    yield
                finally:
                    lock.revalidate()


@contextmanager
def shared_lock(
    path: str | os.PathLike[str],
    *,
    timeout_seconds: float,
    on_directory_fsync: DirectoryFsyncCallback | None = None,
    authorize_mutation: MutationAuthorizationCallback | None = None,
) -> Iterator[PinnedLock]:
    """Take the reader side of the exact transaction boundary used by writers.

    The parent-directory shared flock coordinates with
    :func:`exclusive_lock` even if an uncooperative process replaces the lock
    file's directory entry.  The yielded lock retains the opened inode identity
    so a caller can revalidate it at the precise cut it protects.
    """

    with pinned_parent(
        path,
        create_parents=True,
        on_directory_fsync=on_directory_fsync,
    ) as parent:
        _invoke_mutation_authorization(parent, authorize_mutation)
        with _shared_directory_transaction_lock(
            parent,
            timeout_seconds=timeout_seconds,
        ):
            with _shared_lock_pinned(
                parent,
                timeout_seconds=timeout_seconds,
                on_directory_fsync=on_directory_fsync,
            ) as lock:
                lock.revalidate()
                try:
                    yield lock
                finally:
                    lock.revalidate()


@contextmanager
def _exclusive_directory_transaction_lock(
    parent: PinnedParent, *, timeout_seconds: float
) -> Iterator[None]:
    """Serialize cooperating file transactions sharing this directory inode."""

    info = os.fstat(parent.descriptor)
    identity = (info.st_dev, info.st_ino)
    held = getattr(_DIRECTORY_LOCK_STATE, "held", None)
    modes = getattr(_DIRECTORY_LOCK_STATE, "modes", None)
    if held is None:
        held = {}
        _DIRECTORY_LOCK_STATE.held = held
    if modes is None:
        modes = {}
        _DIRECTORY_LOCK_STATE.modes = modes
    depth = held.get(identity, 0)
    if depth:
        if modes.get(identity) != "exclusive":
            raise SafeFileError(
                f"cannot upgrade shared pinned parent transaction lock: {parent.path}"
            )
        held[identity] = depth + 1
        try:
            yield
        finally:
            held[identity] -= 1
        return

    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(parent.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError as exc:
            if time.monotonic() >= deadline:
                raise SafeFileError(
                    f"timed out after {timeout_seconds:g}s waiting for pinned parent "
                    f"transaction lock: {parent.path}"
                ) from exc
            time.sleep(0.05)
        except OSError as exc:
            raise SafeFileError(
                f"cannot lock pinned parent directory: {parent.path}"
            ) from exc
    held[identity] = 1
    modes[identity] = "exclusive"
    try:
        yield
    finally:
        try:
            del held[identity]
            del modes[identity]
        finally:
            fcntl.flock(parent.descriptor, fcntl.LOCK_UN)


@contextmanager
def _shared_directory_transaction_lock(
    parent: PinnedParent, *, timeout_seconds: float
) -> Iterator[None]:
    """Coordinate snapshot readers with writer transactions on this dir inode."""

    info = os.fstat(parent.descriptor)
    identity = (info.st_dev, info.st_ino)
    held = getattr(_DIRECTORY_LOCK_STATE, "held", None)
    modes = getattr(_DIRECTORY_LOCK_STATE, "modes", None)
    if held is None:
        held = {}
        _DIRECTORY_LOCK_STATE.held = held
    if modes is None:
        modes = {}
        _DIRECTORY_LOCK_STATE.modes = modes
    depth = held.get(identity, 0)
    if depth:
        # A nested shared section inside this thread is already covered by the
        # stronger or equal directory lock held by the outer transaction.
        held[identity] = depth + 1
        try:
            yield
        finally:
            held[identity] -= 1
        return

    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(parent.descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            break
        except BlockingIOError as exc:
            if time.monotonic() >= deadline:
                raise SafeFileError(
                    f"timed out after {timeout_seconds:g}s waiting for shared pinned "
                    f"parent transaction lock: {parent.path}"
                ) from exc
            time.sleep(0.05)
        except OSError as exc:
            raise SafeFileError(
                f"cannot lock pinned parent directory: {parent.path}"
            ) from exc
    held[identity] = 1
    modes[identity] = "shared"
    try:
        yield
    finally:
        try:
            del held[identity]
            del modes[identity]
        finally:
            fcntl.flock(parent.descriptor, fcntl.LOCK_UN)


@contextmanager
def locked_file_transaction(
    path: str | os.PathLike[str],
    *,
    lock_name: str | None = None,
    timeout_seconds: float,
    on_directory_fsync: DirectoryFsyncCallback | None = None,
    authorize_mutation: MutationAuthorizationCallback | None = None,
) -> Iterator[PinnedFileTransaction]:
    """Pin one file's parent and hold its sibling lock for the entire operation."""

    with pinned_parent(
        path,
        create_parents=True,
        on_directory_fsync=on_directory_fsync,
    ) as parent:
        # Bind authority to the actually opened parent/name before deriving or
        # creating any lock entry.  Transaction mutators repeat this check so a
        # later plain-directory rename cannot reuse stale lexical authority.
        _invoke_mutation_authorization(parent, authorize_mutation)
        sibling_name = lock_name or f"{parent.name}.lock"
        sibling = _sibling(parent, sibling_name)
        with _exclusive_directory_transaction_lock(
            parent,
            timeout_seconds=timeout_seconds,
        ):
            with _exclusive_lock_pinned(
                sibling,
                timeout_seconds=timeout_seconds,
                on_directory_fsync=on_directory_fsync,
            ) as lock:
                transaction = PinnedFileTransaction(
                    parent,
                    on_directory_fsync,
                    lock,
                    authorize_mutation,
                )
                yield transaction
