"""Acquire a private bare-repository directory and hold it by descriptor.

Binding Git execution to a *pathname* is not custody: a pathname can be
replaced between the moment it is checked and the moment it is used.
``design/git-fd-binding-spike.md`` measured the alternative on the pinned Git
binary and it holds -- a child bound through an inherited directory descriptor
still reads its own objects after the directory is renamed, and after a hostile
bare repository is planted at the original pathname it **cannot see any object
the decoy contains** (Row 5).

That property starts here, with acquiring the descriptor.  Acquisition has to be
careful about what it opens, because an existing directory may be a symlink
elsewhere, may be owned by someone else, may be group or world writable, or may
already carry repository state of unknown provenance.  Any of those turns a
lease into a handle on something the caller did not create, and the descriptor
would then faithfully bind execution to the wrong repository.

So the target must not already exist, the final component is opened with symlink
following disabled, and the created directory's device, inode, owner and mode
are recorded so a later check has something exact to compare against.

**A borrowed descriptor does not by itself protect an in-flight operation.**
That was measured, not assumed (Row 6).  Git re-resolves the procfs selector as
a *path*, so once the directory is unlinked the selector resolves to a deleted
path and the entries beneath it are gone: deleting the tree while a child was
live made that child fail, and every later bound command failed the same way.
The direction of that failure is the only consolation -- an error, never a wrong
answer read from a substituted repository.

So a borrow exists to prevent the **removal**, not to keep a descriptor open.
An operation spanning validation through the reaping of its child holds one, and
a removal path observes the count.  Without such a token there is nothing for
cleanup to wait on, and the ordering that keeps an in-flight operation intact
would exist only as a convention nothing enforces.

What this module does **not** promise, stated rather than implied:

* **A lease crossing a process boundary is unmeasured.**  Every spike result was
  obtained within one process.  An early probe that opened a descriptor in one
  process and used it in another produced a spurious failure; that was a broken
  probe rather than a finding, and the question remains open.
* **A same-effective-user process is outside the threat model.**  Such a process
  can close or replace an inherited descriptor, and nothing here defends that.
  The model covers other users, symlink and name substitution, and inherited
  configuration -- not an attacker already running as this user.
* Freshness means the created directory held no repository, object, reference or
  configuration state before use.  It does not mean it is now safe.
* **A borrow is process-local.**  It coordinates operations inside this process
  and nothing else: no lock file, no advisory lock, no shared state.  A second
  process removing the tree is not prevented by any borrow held here.

**Removal is the operation that can break something already in flight.**  It is
therefore ordered against the borrow count, and it confirms identity before it
unlinks anything.  The leased path is retained for diagnostics, but by the time
removal runs that path may name a different directory than the lease was opened
on -- because the whole reason custody is held by descriptor is that a name can
be replaced.  Removing whatever now sits at the retained path would be the exact
substitution attack the lease exists to prevent, performed by the cleanup path
itself.

Hygiene inspection is a separate outcome.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

#: Owner-only, because a group- or world-writable repository can be modified by
#: someone the lease was never meant to include.
PRIVATE_DIRECTORY_MODE = 0o700

#: The selector a child is given.  Resolution follows the descriptor rather than
#: re-walking the name, which is the whole point.
PROC_FD_TEMPLATE = "/proc/self/fd/{descriptor}"


class GitRepositoryLeaseError(ValueError):
    """A stable, field-addressed lease-acquisition failure."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"git repository lease {code} at {field}")


@dataclass(frozen=True)
class LeaseIdentity:
    """Exact identity evidence for the directory this lease was opened on."""

    device: int
    inode: int
    owner_uid: int
    mode: int

    @classmethod
    def of_descriptor(cls, descriptor: int) -> "LeaseIdentity":
        info = os.fstat(descriptor)
        return cls(
            device=info.st_dev,
            inode=info.st_ino,
            owner_uid=info.st_uid,
            mode=stat.S_IMODE(info.st_mode),
        )


#: Terminal states a lease can end in, so a caller can tell a refusal from a
#: success afterwards.  Without this they look identical from the outside.
TERMINAL_ACTIVE = "active"
TERMINAL_REMOVED = "removed"
TERMINAL_REFUSED_IDENTITY = "refused-identity-mismatch"

TERMINAL_STATES = (TERMINAL_ACTIVE, TERMINAL_REMOVED, TERMINAL_REFUSED_IDENTITY)


@dataclass
class _BorrowState:
    """Mutable borrow count, kept off the frozen lease value itself."""

    outstanding: int = 0
    terminal: str = TERMINAL_ACTIVE


@dataclass(frozen=True)
class BareRepositoryLease:
    """An opaque handle on one private directory, held by descriptor.

    Constructed only by :func:`acquire_lease`.  The path is retained for
    diagnostics and for the cleanup outcome; **custody is never regained by
    re-resolving it**, which is what :meth:`revalidate` enforces.
    """

    descriptor: int
    path: str
    identity: LeaseIdentity
    _borrows: _BorrowState = field(default_factory=_BorrowState, compare=False, repr=False)

    @property
    def terminal_state(self) -> str:
        """What became of this lease: still active, removed, or refused."""

        return self._borrows.terminal

    @property
    def borrow_count(self) -> int:
        """How many borrows are outstanding, for a removal path to observe."""

        return self._borrows.outstanding

    @property
    def is_borrowed(self) -> bool:
        return self._borrows.outstanding > 0

    @contextmanager
    def borrow(self) -> Iterator["BareRepositoryLease"]:
        """Hold the lease for one operation, from validation through completion.

        Identity is revalidated as the borrow is taken, so an operation never
        begins against a descriptor whose identity has already drifted.  The
        borrow is released on the raising path too: a failed operation must not
        leave a lease pinned forever, because cleanup waits on this count.
        """

        self.revalidate()
        self._borrows.outstanding += 1
        try:
            yield self
        finally:
            self._borrows.outstanding -= 1

    @property
    def selector(self) -> str:
        """The ``--git-dir`` value a bound child is given."""

        return PROC_FD_TEMPLATE.format(descriptor=self.descriptor)

    def revalidate(self) -> None:
        """Confirm the live descriptor still names the directory we opened.

        Compares against the recorded evidence rather than re-walking the path,
        because re-walking is exactly the operation an attacker who replaced the
        name is waiting for.
        """

        try:
            live = LeaseIdentity.of_descriptor(self.descriptor)
        except OSError as error:
            raise GitRepositoryLeaseError("descriptor-unusable", "lease.descriptor") from error
        if live != self.identity:
            raise GitRepositoryLeaseError("identity-mismatch", "lease.identity")


def acquire_lease(parent_directory: str | os.PathLike[str], name: str) -> BareRepositoryLease:
    """Create ``parent_directory/name`` privately and return a lease on it."""

    if isinstance(parent_directory, (bytes, bytearray)) or isinstance(name, (bytes, bytearray)):
        raise GitRepositoryLeaseError("invalid-path", "parent_directory")
    if type(name) is not str or not name or "/" in name or name in (".", ".."):
        raise GitRepositoryLeaseError("invalid-name", "name")

    parent = Path(parent_directory)
    if not parent.is_dir():
        raise GitRepositoryLeaseError("parent-not-a-directory", "parent_directory")

    target = parent / name
    try:
        # ``mkdir`` refuses an existing target, so a lease is never a handle on
        # a directory the caller did not create.
        os.mkdir(target, PRIVATE_DIRECTORY_MODE)
    except FileExistsError as error:
        raise GitRepositoryLeaseError("target-exists", "name") from error
    except OSError as error:
        raise GitRepositoryLeaseError("cannot-create", "name") from error

    try:
        # O_NOFOLLOW refuses a symlink at the final component; O_DIRECTORY
        # refuses anything that is not a directory.
        descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        _remove_quietly(target)
        code = "symlinked-target" if error.errno in (errno.ELOOP, errno.EMLINK) else "cannot-open"
        raise GitRepositoryLeaseError(code, "name") from error

    try:
        identity = LeaseIdentity.of_descriptor(descriptor)
        if identity.owner_uid != os.geteuid():
            raise GitRepositoryLeaseError("foreign-owner", "name")
        if identity.mode != PRIVATE_DIRECTORY_MODE:
            raise GitRepositoryLeaseError("permissive-mode", "name")
        _require_fresh(descriptor)
    except Exception:
        os.close(descriptor)
        _remove_quietly(target)
        raise

    return BareRepositoryLease(
        descriptor=descriptor, path=str(target), identity=identity
    )


def _require_fresh(descriptor: int) -> None:
    """Refuse a directory that already holds anything.

    Freshness means it held no repository, object, reference or configuration
    state before use.  It does not mean it is now safe.
    """

    if os.listdir(descriptor):
        raise GitRepositoryLeaseError("target-not-empty", "name")


def _remove_quietly(target: Path) -> None:
    try:
        os.rmdir(target)
    except OSError:  # pragma: no cover - cleanup is best effort on the error path
        pass


def release_descriptor(lease: BareRepositoryLease) -> None:
    """Close the descriptor only.

    Removing the directory is a separate outcome, and the spike measured why it
    has to be: unlinking a tree while a bound child is live makes that child
    fail, so removal must be ordered against borrows rather than done here.
    """

    if lease.is_borrowed:
        # Closing the descriptor mid-operation is the same failure as removing
        # the tree: the bound child loses its repository.
        raise GitRepositoryLeaseError("lease-is-borrowed", "lease.descriptor")
    try:
        os.close(lease.descriptor)
    except OSError as error:
        raise GitRepositoryLeaseError("descriptor-unusable", "lease.descriptor") from error


def remove_lease(lease: BareRepositoryLease) -> str:
    """Remove the leased directory, or refuse and say why.

    Order of checks is the point.  Borrows first, because removing during one
    breaks the operation in flight rather than merely being untidy.  Identity
    second, because the retained path may no longer name the directory this
    lease was opened on, and deleting whatever now sits there would be the
    substitution attack this design exists to prevent.
    """

    if type(lease) is not BareRepositoryLease:
        raise GitRepositoryLeaseError("invalid-lease", "lease")

    if lease.terminal_state != TERMINAL_ACTIVE:
        # Saying so is better than a second unlink of a path that may since
        # have been reused by something else entirely.
        raise GitRepositoryLeaseError("already-terminal", "lease")

    if lease.is_borrowed:
        raise GitRepositoryLeaseError("lease-is-borrowed", "lease")

    try:
        lease.revalidate()
    except GitRepositoryLeaseError:
        # Retain the evidence rather than delete an unknown replacement.
        lease._borrows.terminal = TERMINAL_REFUSED_IDENTITY
        raise

    # Compare what the retained path names *now* against what was leased.  The
    # descriptor is still the authority; this check is about the path we are
    # about to hand to a recursive delete.
    try:
        at_path = os.stat(lease.path)
    except OSError as error:
        lease._borrows.terminal = TERMINAL_REFUSED_IDENTITY
        raise GitRepositoryLeaseError("path-unreadable", "lease.path") from error

    if (at_path.st_dev, at_path.st_ino) != (lease.identity.device, lease.identity.inode):
        lease._borrows.terminal = TERMINAL_REFUSED_IDENTITY
        raise GitRepositoryLeaseError("identity-mismatch", "lease.path")

    os.close(lease.descriptor)
    shutil.rmtree(lease.path)
    lease._borrows.terminal = TERMINAL_REMOVED
    return TERMINAL_REMOVED


__all__ = [
    "PRIVATE_DIRECTORY_MODE",
    "TERMINAL_ACTIVE",
    "TERMINAL_REFUSED_IDENTITY",
    "TERMINAL_REMOVED",
    "TERMINAL_STATES",
    "remove_lease",
    "PROC_FD_TEMPLATE",
    "BareRepositoryLease",
    "GitRepositoryLeaseError",
    "LeaseIdentity",
    "acquire_lease",
    "release_descriptor",
]
