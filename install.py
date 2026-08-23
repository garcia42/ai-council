#!/usr/bin/env python3
"""Install or verify the version-controlled council forecast runtime files."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import os
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable


REPO = Path(__file__).resolve().parent
FORECAST_BEGIN = "<!-- council-tools forecast contract BEGIN -->"
FORECAST_END = "<!-- council-tools forecast contract END -->"
CLAUDE_BEGIN = "<!-- council-tools durable forecast contract BEGIN -->"
CLAUDE_END = "<!-- council-tools durable forecast contract END -->"
DEFAULT_BACKUP_ROOT = Path(
    "/home/trader/.local/state/council-tools/runtime-backups"
)
LIVE_ROOT = Path("/home/trader").resolve()


class InstallError(RuntimeError):
    pass


BeforeReplaceHook = Callable[[Path, Path], None]
BeforeRetainHook = Callable[[Path, Path], None]
BackupCopyHook = Callable[[str, Path], None]
TransactionCheck = Callable[[], None]
BeforeSuccessHook = Callable[[tuple[Path, ...]], None]
FileIdentity = tuple[int, int]
RollbackPayload = tuple[bytes, int]


@dataclass(frozen=True)
class _ExpectedTargetBinding:
    parent: FileIdentity
    target: FileIdentity
    target_metadata: tuple[object, ...]


@dataclass(frozen=True)
class _MutationSnapshot:
    root: FileIdentity
    targets: dict[Path, _ExpectedTargetBinding]


@dataclass
class _PinnedTarget:
    parent_fd: int
    parent_identity: FileIdentity
    target_identity: FileIdentity
    target_metadata: tuple[object, ...]
    mode: int
    link_count: int
    temporary_name: str | None = None
    temporary_identity: FileIdentity | None = None
    temporary_descriptor: int | None = None
    staged_identity: FileIdentity | None = None
    staged_digest: str | None = None
    preserve_temporary: bool = False


@dataclass
class _PinnedBackupFile:
    relative: Path
    destination: Path
    parent_fd: int
    descriptor: int
    source_descriptor: int
    source_metadata: tuple[object, ...]
    identity: FileIdentity
    mode: int
    digest: str


@dataclass
class _PinnedReport:
    relative: Path
    descriptor: int
    identity: FileIdentity
    mode: int
    digest: str
    content: bytes


@dataclass
class _AuthenticatedBackup:
    path: Path
    descriptor: int
    files: tuple[_PinnedBackupFile, ...]
    manifest_descriptor: int
    manifest_identity: FileIdentity
    manifest_digest: str
    seal_descriptor: int
    seal_identity: FileIdentity
    seal_digest: str
    reports: list[_PinnedReport]


@dataclass
class _PinnedSourceFile:
    relative: Path
    parent_fd: int
    descriptor: int
    parent_identity: FileIdentity
    identity: FileIdentity
    metadata: tuple[object, ...]
    digest: str
    content: bytes


@dataclass
class _PinnedSourceRepository:
    path: Path
    descriptor: int
    identity: FileIdentity
    files: tuple[_PinnedSourceFile, ...]


class _CommittedCustodyFailure(RuntimeError):
    def __init__(self, retained: tuple[Path, ...], cause: Exception):
        super().__init__(str(cause))
        self.retained = retained
        self.cause = cause


_RENAME_EXCHANGE = 2
_RENAME_NOREPLACE = 1
try:
    _RENAMEAT2 = ctypes.CDLL(None, use_errno=True).renameat2
except AttributeError:
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


def _identity(metadata: os.stat_result) -> FileIdentity:
    return metadata.st_dev, metadata.st_ino


def _directory_open_flags() -> int:
    missing = [
        name for name in ("O_DIRECTORY", "O_NOFOLLOW") if not hasattr(os, name)
    ]
    required_dir_fd = (os.open, os.stat, os.rename)
    if missing or any(function not in os.supports_dir_fd for function in required_dir_fd):
        detail = ", ".join(missing) if missing else "directory-relative syscalls"
        raise InstallError(
            f"secure installer mutation is unsupported on this platform: {detail}"
        )
    if os.stat not in os.supports_follow_symlinks:
        raise InstallError(
            "secure installer mutation requires no-follow directory-relative stat"
        )
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_absolute_directory_nofollow(path: Path) -> int:
    if not path.is_absolute():
        raise InstallError(f"trusted directory path is not absolute: {path}")
    descriptor = os.open(path.anchor, _directory_open_flags())
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(
                component,
                _directory_open_flags(),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_directory_beneath(root_fd: int, relative: Path) -> int:
    descriptor = os.dup(root_fd)
    try:
        for component in relative.parts:
            if component in ("", "."):
                continue
            if component == "..":
                raise InstallError("trusted target parent escapes installation root")
            next_descriptor = os.open(
                component,
                _directory_open_flags(),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _target_metadata(parent_fd: int, target: Path) -> os.stat_result:
    metadata = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise InstallError(f"runtime target is no longer a regular file: {target}")
    return metadata


def _open_pinned_targets(
    trusted_root: Path,
    targets: Iterable[Path],
    *,
    expected: _MutationSnapshot | None,
) -> tuple[int, FileIdentity, dict[Path, _PinnedTarget]]:
    root_fd = _open_absolute_directory_nofollow(trusted_root)
    root_identity = _identity(os.fstat(root_fd))
    if expected is not None and root_identity != expected.root:
        os.close(root_fd)
        raise InstallError("installation root identity changed before mutation")

    pinned: dict[Path, _PinnedTarget] = {}
    try:
        for target in targets:
            try:
                relative_parent = target.parent.relative_to(trusted_root)
            except ValueError as exc:
                raise InstallError(
                    f"runtime target escapes installation root: {target}"
                ) from exc
            parent_fd = _open_directory_beneath(root_fd, relative_parent)
            try:
                parent_identity = _identity(os.fstat(parent_fd))
                metadata = _target_metadata(parent_fd, target)
                target_identity = _identity(metadata)
                target_metadata = _stable_regular_metadata(metadata)
                if expected is not None:
                    binding = expected.targets.get(target)
                    if binding is None:
                        raise InstallError(
                            f"runtime target was not in mutation snapshot: {target}"
                        )
                    if (
                        parent_identity != binding.parent
                        or target_identity != binding.target
                        or target_metadata != binding.target_metadata
                    ):
                        raise InstallError(
                            f"runtime target identity changed before mutation: {target}"
                        )
                pinned[target] = _PinnedTarget(
                    parent_fd=parent_fd,
                    parent_identity=parent_identity,
                    target_identity=target_identity,
                    target_metadata=target_metadata,
                    mode=stat.S_IMODE(metadata.st_mode),
                    link_count=metadata.st_nlink,
                )
            except Exception:
                os.close(parent_fd)
                raise
        return root_fd, root_identity, pinned
    except Exception:
        for item in pinned.values():
            os.close(item.parent_fd)
        os.close(root_fd)
        raise


def _snapshot_mutation_bindings(
    trusted_root: Path, targets: Iterable[Path]
) -> _MutationSnapshot:
    root_fd, root_identity, pinned = _open_pinned_targets(
        trusted_root, targets, expected=None
    )
    try:
        return _MutationSnapshot(
            root=root_identity,
            targets={
                target: _ExpectedTargetBinding(
                    parent=item.parent_identity,
                    target=item.target_identity,
                    target_metadata=item.target_metadata,
                )
                for target, item in pinned.items()
            },
        )
    finally:
        for item in pinned.values():
            os.close(item.parent_fd)
        os.close(root_fd)


def _assert_current_binding(
    trusted_root: Path,
    root_identity: FileIdentity,
    root_fd: int,
    target: Path,
    pinned: _PinnedTarget,
) -> None:
    current_root_fd = _open_absolute_directory_nofollow(trusted_root)
    try:
        if _identity(os.fstat(current_root_fd)) != root_identity:
            raise InstallError("installation root identity changed during mutation")
        relative_parent = target.parent.relative_to(trusted_root)
        current_parent_fd = _open_directory_beneath(
            current_root_fd, relative_parent
        )
        try:
            if _identity(os.fstat(current_parent_fd)) != pinned.parent_identity:
                raise InstallError(
                    f"runtime target ancestor changed during mutation: {target}"
                )
            current_metadata = _target_metadata(current_parent_fd, target)
            if (
                _identity(current_metadata) != pinned.target_identity
                or _stable_regular_metadata(current_metadata)
                != pinned.target_metadata
            ):
                raise InstallError(
                    f"runtime target identity changed during mutation: {target}"
                )
        finally:
            os.close(current_parent_fd)
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, InstallError):
            raise
        raise InstallError(
            f"cannot verify runtime target binding during mutation: {target}"
        ) from exc
    finally:
        os.close(current_root_fd)

    # The original root descriptor must still name the captured root as well.
    if _identity(os.fstat(root_fd)) != root_identity:
        raise InstallError("pinned installation root identity changed")


def _canonical_directory(path: Path, *, label: str, must_exist: bool) -> Path:
    try:
        resolved = path.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise InstallError(f"cannot resolve {label}: {path}: {exc}") from exc
    if resolved.exists() and not resolved.is_dir():
        raise InstallError(f"{label} is not a directory: {resolved}")
    return resolved


def _contained_regular_file(path: Path, root: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallError(f"{label} escapes its trusted root: {path}") from exc
    # The root itself is canonicalized first. Rejecting any remaining alias
    # keeps replacement semantics tied to the exact path that was validated.
    if resolved != path:
        raise InstallError(f"{label} traverses a filesystem alias: {path}")
    if not resolved.is_file():
        raise InstallError(f"{label} is not a regular file: {resolved}")
    return resolved


def _validated_runtime_targets(root: Path) -> tuple[Path, ...]:
    targets = tuple(
        _contained_regular_file(target, root, label="runtime target")
        for target in _runtime_targets(root)
    )
    if len(set(targets)) != len(targets):
        raise InstallError("runtime targets do not have unique resolved identities")
    return targets


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mkdir_durable(path: Path, *, exist_ok: bool) -> None:
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    if cursor.exists() and not cursor.is_dir():
        raise InstallError(f"directory ancestor is not a directory: {cursor}")
    path.mkdir(parents=True, exist_ok=exist_ok)
    # A directory's contents are durable only after the directory itself is
    # synced; its name is durable only after its parent is synced.
    for created in missing:
        _fsync_directory(created)
        _fsync_directory(created.parent)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _installed_source_relatives(source_repo: Path) -> tuple[Path, ...]:
    fixed = (
        Path("runtime/council-forecast-contract.md"),
        Path("runtime/CLAUDE_FORECAST_CONTRACT.md"),
        Path("runtime/predictions_report.py"),
    )
    package_root = source_repo / "src/council_tools"
    package = tuple(
        sorted(candidate.relative_to(source_repo) for candidate in package_root.rglob("*.py"))
    )
    if not package:
        raise InstallError(f"council-tools source files are missing: {package_root}")
    return fixed + package


def _pin_install_sources(source_repo: Path) -> _PinnedSourceRepository:
    source_repo = _canonical_directory(
        source_repo, label="council-tools source repository", must_exist=True
    )
    repository_fd = _open_absolute_directory_nofollow(source_repo)
    repository_identity = _identity(os.fstat(repository_fd))
    opened: list[_PinnedSourceFile] = []
    try:
        relatives = _installed_source_relatives(source_repo)
        if len(set(relatives)) != len(relatives):
            raise InstallError("installed source files are not unique")
        for relative in relatives:
            if relative.is_absolute() or ".." in relative.parts:
                raise InstallError(f"unsafe installed source path: {relative}")
            parent_fd = _open_directory_beneath(repository_fd, relative.parent)
            descriptor = -1
            try:
                parent_identity = _identity(os.fstat(parent_fd))
                descriptor = os.open(
                    relative.name,
                    os.O_RDONLY
                    | getattr(os, "O_NONBLOCK", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent_fd,
                )
                metadata_before = os.fstat(descriptor)
                stability = _stable_regular_metadata(metadata_before)
                if metadata_before.st_nlink != 1:
                    raise InstallError(
                        f"installed source metadata is invalid: {relative}"
                    )
                identity = _identity(metadata_before)
                if _entry_identity(parent_fd, relative.name) != identity:
                    raise InstallError(f"installed source binding changed: {relative}")
                content = _read_descriptor(descriptor)
                if _stable_regular_metadata(os.fstat(descriptor)) != stability:
                    raise InstallError(
                        f"installed source changed while being pinned: {relative}"
                    )
                digest = hashlib.sha256(content).hexdigest()
                if _entry_identity(parent_fd, relative.name) != identity:
                    raise InstallError(
                        f"installed source binding changed while being pinned: {relative}"
                    )
                opened.append(
                    _PinnedSourceFile(
                        relative=relative,
                        parent_fd=parent_fd,
                        descriptor=descriptor,
                        parent_identity=parent_identity,
                        identity=identity,
                        metadata=stability,
                        digest=digest,
                        content=content,
                    )
                )
                parent_fd = -1
                descriptor = -1
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if parent_fd >= 0:
                    os.close(parent_fd)
        return _PinnedSourceRepository(
            path=source_repo,
            descriptor=repository_fd,
            identity=repository_identity,
            files=tuple(opened),
        )
    except Exception:
        for item in opened:
            os.close(item.descriptor)
            os.close(item.parent_fd)
        os.close(repository_fd)
        raise


def _authenticate_pinned_sources(sources: _PinnedSourceRepository) -> None:
    try:
        current_repository_fd = _open_absolute_directory_nofollow(sources.path)
    except OSError as exc:
        raise InstallError("source repository identity changed during publication") from exc
    try:
        if (
            _identity(os.fstat(current_repository_fd)) != sources.identity
            or _identity(os.fstat(sources.descriptor)) != sources.identity
        ):
            raise InstallError("source repository identity changed during publication")
    finally:
        os.close(current_repository_fd)
    for item in sources.files:
        current_parent_fd = _open_directory_beneath(
            sources.descriptor, item.relative.parent
        )
        try:
            if _identity(os.fstat(current_parent_fd)) != item.parent_identity:
                raise InstallError(
                    f"installed source ancestor changed: {item.relative}"
                )
            if _entry_identity(current_parent_fd, item.relative.name) != item.identity:
                raise InstallError(f"installed source binding changed: {item.relative}")
        finally:
            os.close(current_parent_fd)
        if (
            _stable_regular_metadata(os.fstat(item.descriptor)) != item.metadata
            or _digest_descriptor(item.descriptor) != item.digest
            or _stable_regular_metadata(os.fstat(item.descriptor)) != item.metadata
        ):
            raise InstallError(f"installed source authentication failed: {item.relative}")


def _close_pinned_sources(sources: _PinnedSourceRepository) -> None:
    for item in sources.files:
        os.close(item.descriptor)
        os.close(item.parent_fd)
    os.close(sources.descriptor)


def _source_digest_from_custody(sources: _PinnedSourceRepository) -> str:
    digest = hashlib.sha256()
    package = tuple(
        item
        for item in sources.files
        if item.relative.parts[:2] == ("src", "council_tools")
    )
    for item in sorted(package, key=lambda candidate: str(candidate.relative)):
        digest.update(str(item.relative).encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.content)
        digest.update(b"\0")
    return digest.hexdigest()


def _source_digest(source_repo: Path) -> str:
    digest = hashlib.sha256()
    source = source_repo / "src/council_tools"
    files = sorted(source.rglob("*.py"))
    if not files:
        raise InstallError(f"council-tools source files are missing: {source}")
    for candidate in files:
        path = _contained_regular_file(
            candidate, source_repo, label="council-tools source file"
        )
        digest.update(str(path.relative_to(source_repo)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _repository_identity(
    source_repo: Path,
    *,
    require_clean: bool,
    source_sha256: str | None = None,
) -> tuple[str, str]:
    source_repo = _canonical_directory(
        source_repo, label="council-tools source repository", must_exist=True
    )
    try:
        top_level = subprocess.run(
            ["git", "-C", str(source_repo), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        if Path(top_level).resolve(strict=True) != source_repo:
            raise InstallError(
                "council-tools source must be the resolved repository root"
            )
        head = subprocess.run(
            ["git", "-C", str(source_repo), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        if require_clean:
            status = subprocess.run(
                [
                    "git",
                    "-C",
                    str(source_repo),
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ],
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            if status:
                raise InstallError("live install requires a clean council-tools source commit")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise InstallError(f"cannot identify council-tools source commit: {exc}") from exc
    if source_sha256 is None:
        source_sha256 = _source_digest(source_repo)
    return head, source_sha256


def _block(begin: str, body: str, end: str) -> str:
    return f"{begin}\n\n{body.rstrip()}\n\n{end}\n"


def _upsert_block(text: str, *, begin: str, end: str, body: str, marker: str) -> str:
    replacement = _block(begin, body, end)
    if begin in text:
        start = text.index(begin)
        try:
            finish = text.index(end, start) + len(end)
        except ValueError as exc:
            raise InstallError(f"found {begin} without {end}") from exc
        suffix = text[finish:]
        if suffix.startswith("\n"):
            suffix = suffix[1:]
        return text[:start] + replacement + suffix
    position = text.find(marker)
    if position < 0:
        raise InstallError(f"install marker not found: {marker}")
    return text[:position] + replacement + "\n" + text[position:]


def _runtime_targets(root: Path) -> tuple[Path, ...]:
    claude_dir = root / ".claude"
    reporter = claude_dir / "knowledge/council-eval/predictions_report.py"
    skill = claude_dir / "skills/council/SKILL.md"
    criterion = claude_dir / "knowledge/council-eval/blind_seat_kill_criterion.py"
    claude_md = root / "CLAUDE.md"
    return reporter, skill, criterion, claude_md


def _with_attempt_allowlist(criterion_text: str) -> str:
    required_kinds = (
        "council-attempt",
        "capture-activation",
        "capture-initiation",
        "council-attempt-v2",
        "council-seats-finished",
        "capture-invalidation",
    )
    old_allowlist = (
        'NON_COUNCIL_RECORD_KINDS = {"pre-mortem-calibration", "council-calibration"}'
    )
    new_allowlist = (
        'NON_COUNCIL_RECORD_KINDS = {\n'
        '    "pre-mortem-calibration",\n'
        '    "council-calibration",\n'
        + "".join(f'    "{kind}",\n' for kind in required_kinds)
        + '}'
    )
    if all(f'"{kind}"' in criterion_text for kind in required_kinds):
        return criterion_text
    if old_allowlist in criterion_text:
        return criterion_text.replace(old_allowlist, new_allowlist, 1)

    marker = "NON_COUNCIL_RECORD_KINDS = {\n"
    start = criterion_text.find(marker)
    if start < 0:
        raise InstallError("blind-seat allowlist preimage not found")
    finish = criterion_text.find("\n}", start)
    if finish < 0:
        raise InstallError("blind-seat allowlist closing brace not found")
    missing = "".join(
        f'    "{kind}",\n'
        for kind in required_kinds
        if f'"{kind}"' not in criterion_text[start:finish]
    )
    return criterion_text[: finish + 1] + missing + criterion_text[finish + 1 :]


def _render_with_source_custody(
    root: Path,
    *,
    source_repo: Path = REPO,
    require_clean_source: bool = False,
) -> tuple[dict[Path, bytes], _PinnedSourceRepository]:
    root = _canonical_directory(root, label="installation root", must_exist=True)
    source_repo = _canonical_directory(
        source_repo, label="council-tools source repository", must_exist=True
    )
    sources = _pin_install_sources(source_repo)
    returned = False
    try:
        source_sha256 = _source_digest_from_custody(sources)
        commit, _ = _repository_identity(
            source_repo,
            require_clean=require_clean_source,
            source_sha256=source_sha256,
        )
        _authenticate_pinned_sources(sources)
        source_bytes = {item.relative: item.content for item in sources.files}
        reporter, skill, criterion, claude_md = _validated_runtime_targets(root)

        skill_text = skill.read_text(encoding="utf-8")
        skill_text = _upsert_block(
            skill_text,
            begin=FORECAST_BEGIN,
            end=FORECAST_END,
            body=source_bytes[
                Path("runtime/council-forecast-contract.md")
            ].decode("utf-8"),
            marker="## Steps\n",
        )

        criterion_text = _with_attempt_allowlist(
            criterion.read_text(encoding="utf-8")
        )

        claude_text = claude_md.read_text(encoding="utf-8")
        claude_text = _upsert_block(
            claude_text,
            begin=CLAUDE_BEGIN,
            end=CLAUDE_END,
            body=source_bytes[
                Path("runtime/CLAUDE_FORECAST_CONTRACT.md")
            ].decode("utf-8"),
            marker="## SLO/SLI changes require a full council review\n",
        )

        reporter_text = source_bytes[Path("runtime/predictions_report.py")].decode(
            "utf-8"
        )
        replacements = {
            "@@COUNCIL_TOOLS_SOURCE_ROOT@@": str(source_repo),
            "@@COUNCIL_TOOLS_COMMIT@@": commit,
            "@@COUNCIL_TOOLS_SOURCE_SHA256@@": source_sha256,
        }
        for token, value in replacements.items():
            if reporter_text.count(token) != 1:
                raise InstallError(
                    f"runtime reporter template token count is not one: {token}"
                )
            reporter_text = reporter_text.replace(token, value)

        rendered = {
            reporter: reporter_text.encode("utf-8"),
            skill: skill_text.encode("utf-8"),
            criterion: criterion_text.encode("utf-8"),
            claude_md: claude_text.encode("utf-8"),
        }
        _authenticate_pinned_sources(sources)
        returned = True
        return rendered, sources
    finally:
        if not returned:
            _close_pinned_sources(sources)


def _render(
    root: Path,
    *,
    source_repo: Path = REPO,
    require_clean_source: bool = False,
) -> dict[Path, bytes]:
    rendered, sources = _render_with_source_custody(
        root,
        source_repo=source_repo,
        require_clean_source=require_clean_source,
    )
    try:
        _authenticate_pinned_sources(sources)
        return rendered
    finally:
        _close_pinned_sources(sources)


def check(
    root: Path,
    *,
    source_repo: Path = REPO,
    require_clean_source: bool | None = None,
) -> tuple[bool, list[str]]:
    root = _canonical_directory(root, label="installation root", must_exist=True)
    if root == LIVE_ROOT:
        require_clean_source = True
    elif require_clean_source is None:
        require_clean_source = False
    rendered = _render(
        root,
        source_repo=source_repo,
        require_clean_source=require_clean_source,
    )
    differences = []
    for target, expected in rendered.items():
        if target.read_bytes() != expected:
            differences.append(str(target))
    return not differences, differences


def _stable_regular_metadata(metadata: os.stat_result) -> tuple[object, ...]:
    if not stat.S_ISREG(metadata.st_mode):
        raise InstallError("backup source is not a regular file")
    return (
        _identity(metadata),
        stat.S_IFMT(metadata.st_mode),
        stat.S_IMODE(metadata.st_mode),
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _digest_descriptor(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _read_descriptor(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return b"".join(chunks)


def _mkdirs_beneath(root_fd: int, relative: Path) -> int:
    descriptor = os.dup(root_fd)
    try:
        for component in relative.parts:
            if component in ("", "."):
                continue
            if component == "..":
                raise InstallError("backup descendant escapes retained backup")
            try:
                os.mkdir(component, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            else:
                _fsync_pinned_directory(descriptor)
            next_descriptor = os.open(
                component,
                _directory_open_flags(),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _stream_pinned_backup_file(
    target: Path,
    pinned: _PinnedTarget,
    destination: Path,
    relative: Path,
    destination_parent_fd: int,
    *,
    backup_copy_hook: BackupCopyHook | None,
) -> _PinnedBackupFile:
    parent_fd = destination_parent_fd
    source_descriptor = -1
    destination_descriptor = -1
    source_retained = False
    try:
        source_descriptor = os.open(
            target.name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=pinned.parent_fd,
        )
        source_before = os.fstat(source_descriptor)
        source_stability = _stable_regular_metadata(source_before)
        if (
            _identity(source_before) != pinned.target_identity
            or stat.S_IMODE(source_before.st_mode) != pinned.mode
            or source_before.st_nlink != pinned.link_count
        ):
            raise InstallError(f"runtime backup source identity changed: {target}")
        if backup_copy_hook is not None:
            backup_copy_hook("before-copy", target)

        destination_descriptor = os.open(
            destination.name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            pinned.mode,
            dir_fd=parent_fd,
        )
        copied_digest = hashlib.sha256()
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            copied_digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written == 0:
                    raise OSError("short write while streaming runtime backup")
                view = view[written:]
        os.fchmod(destination_descriptor, pinned.mode)
        os.fsync(destination_descriptor)
        _fsync_pinned_directory(parent_fd)
        if backup_copy_hook is not None:
            backup_copy_hook("after-copy", target)

        source_after = os.fstat(source_descriptor)
        if _stable_regular_metadata(source_after) != source_stability:
            raise InstallError(f"runtime backup source changed while copying: {target}")
        source_digest = _digest_descriptor(source_descriptor)
        if _stable_regular_metadata(os.fstat(source_descriptor)) != source_stability:
            raise InstallError(f"runtime backup source changed during verification: {target}")
        if source_digest != copied_digest.hexdigest():
            raise InstallError(f"runtime backup source bytes changed while copying: {target}")

        destination_metadata = os.fstat(destination_descriptor)
        if (
            not stat.S_ISREG(destination_metadata.st_mode)
            or destination_metadata.st_nlink != 1
            or stat.S_IMODE(destination_metadata.st_mode) != pinned.mode
            or destination_metadata.st_size != source_after.st_size
        ):
            raise InstallError(f"runtime backup destination metadata is invalid: {destination}")
        destination_digest = _digest_descriptor(destination_descriptor)
        if destination_digest != source_digest:
            raise InstallError(f"runtime backup destination digest mismatch: {destination}")
        destination_identity = _identity(destination_metadata)
        if _entry_identity(parent_fd, destination.name) != destination_identity:
            raise InstallError(f"runtime backup destination binding changed: {destination}")
        result = _PinnedBackupFile(
            relative=relative,
            destination=destination,
            parent_fd=parent_fd,
            descriptor=destination_descriptor,
            source_descriptor=source_descriptor,
            source_metadata=source_stability,
            identity=destination_identity,
            mode=pinned.mode,
            digest=destination_digest,
        )
        source_retained = True
        return result
    except Exception:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        if parent_fd >= 0:
            os.close(parent_fd)
        raise
    finally:
        if not source_retained and source_descriptor >= 0:
            os.close(source_descriptor)


def _write_pinned_manifest(backup_fd: int, content: bytes) -> tuple[int, FileIdentity, str]:
    descriptor = os.open(
        "MANIFEST.tsv",
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=backup_fd,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError("short write while creating backup manifest")
            view = view[written:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise InstallError("backup manifest metadata is invalid")
        identity = _identity(metadata)
        digest = _digest_descriptor(descriptor)
        if _entry_identity(backup_fd, "MANIFEST.tsv") != identity:
            raise InstallError("backup manifest binding changed")
        _fsync_pinned_directory(backup_fd)
        return descriptor, identity, digest
    except Exception:
        os.close(descriptor)
        raise


def _write_pinned_manifest_seal(
    backup_fd: int, manifest_digest: str
) -> tuple[int, FileIdentity, str]:
    content = (manifest_digest + "\n").encode("ascii")
    descriptor = os.open(
        "MANIFEST.sha256",
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=backup_fd,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError("short write while creating backup manifest seal")
            view = view[written:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise InstallError("backup manifest seal metadata is invalid")
        identity = _identity(metadata)
        digest = _digest_descriptor(descriptor)
        if _entry_identity(backup_fd, "MANIFEST.sha256") != identity:
            raise InstallError("backup manifest seal binding changed")
        _fsync_pinned_directory(backup_fd)
        return descriptor, identity, digest
    except Exception:
        os.close(descriptor)
        raise


def _authenticate_pinned_backup(
    backup: _AuthenticatedBackup,
    *,
    verify_sources: bool,
) -> None:
    try:
        current_backup_fd = _open_absolute_directory_nofollow(backup.path)
    except OSError as exc:
        raise InstallError("backup directory identity changed before publication") from exc
    try:
        if _identity(os.fstat(current_backup_fd)) != _identity(
            os.fstat(backup.descriptor)
        ):
            raise InstallError("backup directory identity changed before publication")
    finally:
        os.close(current_backup_fd)
    for item in backup.files:
        if verify_sources:
            if item.source_descriptor < 0:
                raise InstallError(
                    f"backup source descriptor is unavailable: {item.destination}"
                )
            if (
                _stable_regular_metadata(os.fstat(item.source_descriptor))
                != item.source_metadata
                or _digest_descriptor(item.source_descriptor) != item.digest
                or _stable_regular_metadata(os.fstat(item.source_descriptor))
                != item.source_metadata
            ):
                raise InstallError(
                    f"backup source authentication failed: {item.destination}"
                )
        current_parent_fd = _open_directory_beneath(
            backup.descriptor, item.relative.parent
        )
        try:
            if _identity(os.fstat(current_parent_fd)) != _identity(
                os.fstat(item.parent_fd)
            ):
                raise InstallError(
                    f"backup destination ancestor changed: {item.destination}"
                )
            if _entry_identity(current_parent_fd, item.destination.name) != item.identity:
                raise InstallError(
                    f"backup destination binding changed: {item.destination}"
                )
        finally:
            os.close(current_parent_fd)
        metadata = os.fstat(item.descriptor)
        if (
            _identity(metadata) != item.identity
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != item.mode
            or _digest_descriptor(item.descriptor) != item.digest
        ):
            raise InstallError(f"backup destination authentication failed: {item.destination}")
    manifest_metadata = os.fstat(backup.manifest_descriptor)
    if (
        _entry_identity(backup.descriptor, "MANIFEST.tsv")
        != backup.manifest_identity
        or _identity(manifest_metadata) != backup.manifest_identity
        or not stat.S_ISREG(manifest_metadata.st_mode)
        or manifest_metadata.st_nlink != 1
        or stat.S_IMODE(manifest_metadata.st_mode) != 0o600
        or _digest_descriptor(backup.manifest_descriptor) != backup.manifest_digest
    ):
        raise InstallError("backup manifest authentication failed")
    seal_metadata = os.fstat(backup.seal_descriptor)
    expected_seal = (backup.manifest_digest + "\n").encode("ascii")
    if (
        _entry_identity(backup.descriptor, "MANIFEST.sha256")
        != backup.seal_identity
        or _identity(seal_metadata) != backup.seal_identity
        or not stat.S_ISREG(seal_metadata.st_mode)
        or seal_metadata.st_nlink != 1
        or stat.S_IMODE(seal_metadata.st_mode) != 0o600
        or _digest_descriptor(backup.seal_descriptor) != backup.seal_digest
        or _read_descriptor(backup.seal_descriptor) != expected_seal
    ):
        raise InstallError("backup manifest seal authentication failed")
    for report in backup.reports:
        metadata = os.fstat(report.descriptor)
        if (
            _entry_identity(backup.descriptor, report.relative.name)
            != report.identity
            or _identity(metadata) != report.identity
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != report.mode
            or _digest_descriptor(report.descriptor) != report.digest
            or _read_descriptor(report.descriptor) != report.content
        ):
            raise InstallError(
                f"backup custody report authentication failed: {report.relative}"
            )


def _close_authenticated_backup(backup: _AuthenticatedBackup) -> None:
    for item in backup.files:
        if item.source_descriptor >= 0:
            os.close(item.source_descriptor)
        os.close(item.descriptor)
        os.close(item.parent_fd)
    for report in backup.reports:
        os.close(report.descriptor)
    os.close(backup.seal_descriptor)
    os.close(backup.manifest_descriptor)
    os.close(backup.descriptor)


def _authenticated_backup_payloads(
    backup: _AuthenticatedBackup,
    root: Path,
    *,
    verify_sources: bool,
) -> dict[Path, RollbackPayload]:
    _authenticate_pinned_backup(backup, verify_sources=verify_sources)
    payloads: dict[Path, RollbackPayload] = {}
    for item in backup.files:
        metadata_before = os.fstat(item.descriptor)
        content = _read_descriptor(item.descriptor)
        metadata_after = os.fstat(item.descriptor)
        if (
            _identity(metadata_before) != item.identity
            or _identity(metadata_after) != item.identity
            or stat.S_IMODE(metadata_after.st_mode) != item.mode
            or hashlib.sha256(content).hexdigest() != item.digest
        ):
            raise InstallError(
                f"backup payload changed while entering transaction custody: "
                f"{item.destination}"
            )
        payloads[root / item.relative] = (content, item.mode)
    _authenticate_pinned_backup(backup, verify_sources=verify_sources)
    return payloads


def _backup_targets(
    root: Path,
    targets: Iterable[Path],
    backup_root: Path,
    *,
    expected_bindings: _MutationSnapshot | None = None,
    backup_copy_hook: BackupCopyHook | None = None,
) -> _AuthenticatedBackup:
    root = _canonical_directory(root, label="installation root", must_exist=True)
    backup_root = _canonical_directory(
        backup_root, label="backup root", must_exist=False
    )
    validated_targets = tuple(
        _contained_regular_file(target, root, label="runtime backup target")
        for target in targets
    )
    root_fd, root_identity, pinned_targets = _open_pinned_targets(
        root,
        validated_targets,
        expected=expected_bindings,
    )
    copied_files: list[_PinnedBackupFile] = []
    backup_root_fd = -1
    backup_fd = -1
    manifest_fd = -1
    seal_fd = -1
    transaction: _AuthenticatedBackup | None = None
    transaction_returned = False
    try:
        _mkdir_durable(backup_root, exist_ok=True)
        backup_root_fd = _open_absolute_directory_nofollow(backup_root)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_name = f"{stamp}-{uuid.uuid4().hex[:8]}"
        backup = backup_root / backup_name
        os.mkdir(backup_name, 0o700, dir_fd=backup_root_fd)
        _fsync_pinned_directory(backup_root_fd)
        expected_backup_identity = _entry_identity(backup_root_fd, backup_name)
        backup_fd = os.open(
            backup_name,
            _directory_open_flags(),
            dir_fd=backup_root_fd,
        )
        if _identity(os.fstat(backup_fd)) != expected_backup_identity:
            raise InstallError("backup directory binding changed while being retained")
        manifest_rows = []
        for target in validated_targets:
            relative = target.relative_to(root)
            destination = backup / relative
            destination_parent_fd = _mkdirs_beneath(backup_fd, relative.parent)
            copied = _stream_pinned_backup_file(
                target,
                pinned_targets[target],
                destination,
                relative,
                destination_parent_fd,
                backup_copy_hook=backup_copy_hook,
            )
            copied_files.append(copied)
            manifest_rows.append(f"{relative}\t{copied.digest}")
        manifest_content = ("\n".join(manifest_rows) + "\n").encode("utf-8")
        manifest_fd, manifest_identity, manifest_digest = _write_pinned_manifest(
            backup_fd,
            manifest_content,
        )
        seal_fd, seal_identity, seal_digest = _write_pinned_manifest_seal(
            backup_fd,
            manifest_digest,
        )
        transaction = _AuthenticatedBackup(
            path=backup,
            descriptor=backup_fd,
            files=tuple(copied_files),
            manifest_descriptor=manifest_fd,
            manifest_identity=manifest_identity,
            manifest_digest=manifest_digest,
            seal_descriptor=seal_fd,
            seal_identity=seal_identity,
            seal_digest=seal_digest,
            reports=[],
        )
        for target in validated_targets:
            _assert_current_binding(
                root,
                root_identity,
                root_fd,
                target,
                pinned_targets[target],
            )
        _authenticate_pinned_backup(transaction, verify_sources=True)

        latest_temporary = backup_root / f".LATEST-{uuid.uuid4().hex}.escrow"
        try:
            _write_new_report_payload(latest_temporary, str(backup) + "\n")
            _fsync_report_file(latest_temporary)
            retained_latest = _publish_latest_pointer(backup_root, latest_temporary)
        except (InstallError, OSError) as exc:
            raise _retained_custody_error(
                action="backup pointer publication failed closed",
                state="unchanged",
                backup=backup,
                retained=(latest_temporary,),
                report=None,
                cause=exc,
            ) from exc
        if retained_latest is not None:
            retained_report = backup / "RETAINED_BACKUP_POINTERS.tsv"
            try:
                _write_pinned_report(
                    transaction,
                    Path("RETAINED_BACKUP_POINTERS.tsv"),
                    f"{retained_latest}\toperator-review-required\n",
                )
            except (InstallError, OSError) as exc:
                raise _retained_custody_error(
                    action="backup pointer publication committed but custody report failed",
                    state="unchanged",
                    backup=backup,
                    retained=(retained_latest,),
                    report=retained_report,
                    cause=exc,
                ) from exc
        # The callback that publishes LATEST runs outside the backup
        # directory and is an adversarial handoff boundary. Reauthenticate the
        # exact still-open payload, manifest, and seal descriptors before the
        # runtime mutation caller can proceed.
        _authenticate_pinned_backup(transaction, verify_sources=True)
        transaction_returned = True
        return transaction
    finally:
        if not transaction_returned:
            if transaction is not None:
                _close_authenticated_backup(transaction)
            else:
                if seal_fd >= 0:
                    os.close(seal_fd)
                if manifest_fd >= 0:
                    os.close(manifest_fd)
                for item in copied_files:
                    os.close(item.source_descriptor)
                    os.close(item.descriptor)
                    os.close(item.parent_fd)
                if backup_fd >= 0:
                    os.close(backup_fd)
        if backup_root_fd >= 0:
            os.close(backup_root_fd)
        for pinned in pinned_targets.values():
            os.close(pinned.parent_fd)
        os.close(root_fd)


def _write_pinned_temporary(
    parent_fd: int,
    name: str,
    content: bytes,
    *,
    mode: int,
) -> tuple[int, FileIdentity, str]:
    descriptor = os.open(
        name,
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        mode,
        dir_fd=parent_fd,
    )
    retained = False
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError("short write while creating runtime temporary")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        identity = _identity(metadata)
        expected_digest = hashlib.sha256(content).hexdigest()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_size != len(content)
            or _entry_identity(parent_fd, name) != identity
            or _digest_descriptor(descriptor) != expected_digest
            or _read_descriptor(descriptor) != content
        ):
            raise InstallError("runtime temporary authentication failed after staging")
        retained = True
        return descriptor, identity, expected_digest
    finally:
        if not retained:
            os.close(descriptor)


def _authenticate_staged_runtime(
    pinned: _PinnedTarget,
    target: Path,
    expected: bytes,
    *,
    namespace_name: str,
) -> None:
    descriptor = pinned.temporary_descriptor
    staged_identity = pinned.staged_identity
    expected_digest = pinned.staged_digest
    if descriptor is None or staged_identity is None or expected_digest is None:
        raise InstallError(f"runtime staged descriptor is unavailable: {target}")
    metadata_before = os.fstat(descriptor)
    observed = _read_descriptor(descriptor)
    metadata_after = os.fstat(descriptor)
    if (
        _identity(metadata_before) != staged_identity
        or _identity(metadata_after) != staged_identity
        or not stat.S_ISREG(metadata_after.st_mode)
        or metadata_after.st_nlink != 1
        or stat.S_IMODE(metadata_after.st_mode) != pinned.mode
        or metadata_after.st_size != len(expected)
        or _entry_identity(pinned.parent_fd, namespace_name) != staged_identity
        or hashlib.sha256(observed).hexdigest() != expected_digest
        or observed != expected
    ):
        raise InstallError(f"staged runtime authentication failed: {target}")


def _entry_identity(parent_fd: int, name: str) -> FileIdentity:
    return _identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))


def _entry_metadata(parent_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)


def _require_exchange_support() -> None:
    if _RENAMEAT2 is None:
        raise InstallError(
            "secure installer publication requires renameat2(RENAME_EXCHANGE)"
        )


def _exchange_pinned(parent_fd: int, source_name: str, target_name: str) -> None:
    _require_exchange_support()
    assert _RENAMEAT2 is not None
    ctypes.set_errno(0)
    result = _RENAMEAT2(
        parent_fd,
        os.fsencode(source_name),
        parent_fd,
        os.fsencode(target_name),
        _RENAME_EXCHANGE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
        raise InstallError(
            "secure installer publication does not support atomic name exchange"
        )
    raise OSError(
        error_number,
        os.strerror(error_number),
        f"{source_name} <-> {target_name}",
    )


def _rename_noreplace_pinned(
    source_parent_fd: int,
    source_name: str,
    target_parent_fd: int,
    target_name: str,
) -> None:
    _require_exchange_support()
    assert _RENAMEAT2 is not None
    ctypes.set_errno(0)
    result = _RENAMEAT2(
        source_parent_fd,
        os.fsencode(source_name),
        target_parent_fd,
        os.fsencode(target_name),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
        raise InstallError(
            "secure installer publication does not support no-replace rename"
        )
    raise OSError(
        error_number,
        os.strerror(error_number),
        f"{source_name} -> {target_name}",
    )


def _publish_latest_pointer(
    backup_root: Path,
    staged: Path,
) -> Path | None:
    parent_fd = _open_absolute_directory_nofollow(backup_root)
    try:
        staged_identity = _entry_identity(parent_fd, staged.name)
        for _ in range(8):
            try:
                _entry_identity(parent_fd, "LATEST")
            except FileNotFoundError:
                try:
                    _rename_noreplace_pinned(
                        parent_fd,
                        staged.name,
                        parent_fd,
                        "LATEST",
                    )
                except FileExistsError:
                    continue
                _fsync_pinned_directory(parent_fd)
                if _entry_identity(parent_fd, "LATEST") != staged_identity:
                    raise InstallError(
                        "backup LATEST pointer changed after no-replace publication; "
                        f"retained_entry={staged}"
                    )
                return None
            try:
                _exchange_pinned(parent_fd, staged.name, "LATEST")
            except FileNotFoundError:
                continue
            _fsync_pinned_directory(parent_fd)
            if _entry_identity(parent_fd, "LATEST") != staged_identity:
                raise InstallError(
                    "backup LATEST pointer changed after atomic exchange; "
                    f"retained_entry={staged}"
                )
            return staged
        raise InstallError(
            "backup LATEST pointer namespace did not stabilize; "
            f"retained_entry={staged}"
        )
    except (InstallError, OSError) as exc:
        if "retained_entry=" in str(exc):
            raise
        raise InstallError(f"{exc}; retained_entry={staged}") from exc
    finally:
        os.close(parent_fd)


def _fsync_pinned_directory(parent_fd: int) -> None:
    os.fsync(parent_fd)


def _read_rollback_payload(source: Path) -> tuple[bytes, int]:
    source_descriptor = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        source_metadata = os.fstat(source_descriptor)
        chunks = []
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), stat.S_IMODE(source_metadata.st_mode)
    finally:
        os.close(source_descriptor)


def _retained_escrows(
    pinned_targets: dict[Path, _PinnedTarget],
    *,
    before_retain: BeforeRetainHook | None,
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    retained: list[Path] = []
    issues: list[str] = []
    for target, pinned in pinned_targets.items():
        temporary_name = pinned.temporary_name
        expected_identity = pinned.temporary_identity
        if temporary_name is None:
            continue
        escrow = target.with_name(temporary_name)
        retained.append(escrow)
        try:
            observed_before = _entry_identity(pinned.parent_fd, temporary_name)
        except OSError as exc:
            issues.append(f"{escrow}: missing before retention ({exc})")
            continue
        if before_retain is not None:
            before_retain(escrow, target)
        try:
            observed_after = _entry_identity(pinned.parent_fd, temporary_name)
        except OSError as exc:
            issues.append(f"{escrow}: missing after retention boundary ({exc})")
            continue
        if (
            pinned.preserve_temporary
            or expected_identity is None
            or observed_before != expected_identity
            or observed_after != observed_before
        ):
            issues.append(f"{escrow}: retained escrow identity changed")
    return tuple(retained), tuple(issues)


def _retained_summary(retained: Iterable[Path]) -> str:
    rendered = tuple(str(path) for path in retained)
    return ", ".join(rendered) if rendered else "<none>"


def _retained_custody_error(
    *,
    action: str,
    state: str,
    backup: Path,
    retained: Iterable[Path],
    report: Path | None,
    cause: Exception,
) -> InstallError:
    report_text = str(report) if report is not None else "<not-created>"
    return InstallError(
        f"{action}; installer publication state={state}; backup={backup}; "
        f"retained_entries={_retained_summary(retained)}; report={report_text}; "
        f"cause={cause}"
    )


def _open_new_report(path: Path):
    return path.open("x", encoding="utf-8")


def _write_report_payload(handle, content: str) -> None:
    handle.write(content)
    handle.flush()


def _write_new_report_payload(path: Path, content: str) -> None:
    with _open_new_report(path) as handle:
        _write_report_payload(handle, content)


def _fsync_report_file(path: Path) -> None:
    _fsync_file(path)


def _fsync_report_directory(path: Path) -> None:
    _fsync_directory(path)


def _open_new_report_at(parent_fd: int, name: str, mode: int) -> int:
    return os.open(
        name,
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        mode,
        dir_fd=parent_fd,
    )


def _write_report_descriptor(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise OSError("short write while creating custody report")
        view = view[written:]


def _fsync_report_descriptor(descriptor: int) -> None:
    os.fsync(descriptor)


def _sync_pinned_report_directory(descriptor: int) -> None:
    _fsync_pinned_directory(descriptor)


def _write_pinned_report(
    backup: _AuthenticatedBackup,
    relative: Path,
    content: str,
) -> Path:
    if relative.parent != Path(".") or relative.name in {
        "MANIFEST.tsv",
        "MANIFEST.sha256",
    }:
        raise InstallError(f"unsafe backup custody report path: {relative}")
    encoded = content.encode("utf-8")
    descriptor = _open_new_report_at(backup.descriptor, relative.name, 0o600)
    retained = False
    try:
        _write_report_descriptor(descriptor, encoded)
        os.fchmod(descriptor, 0o600)
        _fsync_report_descriptor(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise InstallError(f"backup custody report metadata is invalid: {relative}")
        identity = _identity(metadata)
        digest = _digest_descriptor(descriptor)
        if (
            _entry_identity(backup.descriptor, relative.name) != identity
            or _read_descriptor(descriptor) != encoded
        ):
            raise InstallError(f"backup custody report binding changed: {relative}")
        _sync_pinned_report_directory(backup.descriptor)
        backup.reports.append(
            _PinnedReport(
                relative=relative,
                descriptor=descriptor,
                identity=identity,
                mode=0o600,
                digest=digest,
                content=encoded,
            )
        )
        retained = True
        return backup.path / relative
    finally:
        if not retained:
            os.close(descriptor)


def _write_retained_escrow_report(
    backup: _AuthenticatedBackup,
    root: Path,
    retained: Iterable[Path],
) -> Path:
    rows = []
    for escrow in retained:
        try:
            relative = escrow.relative_to(root)
        except ValueError as exc:
            raise InstallError(
                f"retained escrow escapes installation root: {escrow}"
            ) from exc
        rows.append(f"{relative}\toperator-review-required")
    return _write_pinned_report(
        backup,
        Path("RETAINED_ESCROWS.tsv"),
        "\n".join(rows) + "\n",
    )


def _reverse_exchange(
    pinned: _PinnedTarget,
    target: Path,
    *,
    expected_target: FileIdentity,
    expected_temporary: FileIdentity,
) -> bool:
    temporary_name = pinned.temporary_name
    if temporary_name is None:
        return False
    try:
        if (
            _entry_identity(pinned.parent_fd, target.name) != expected_target
            or _entry_identity(pinned.parent_fd, temporary_name)
            != expected_temporary
        ):
            pinned.preserve_temporary = True
            return False
        _exchange_pinned(pinned.parent_fd, temporary_name, target.name)
        _fsync_pinned_directory(pinned.parent_fd)
        if (
            _entry_identity(pinned.parent_fd, target.name)
            != expected_temporary
            or _entry_identity(pinned.parent_fd, temporary_name)
            != expected_target
        ):
            pinned.preserve_temporary = True
            return False
    except (InstallError, OSError):
        pinned.preserve_temporary = True
        return False
    pinned.target_identity = expected_temporary
    pinned.target_metadata = _stable_regular_metadata(
        _entry_metadata(pinned.parent_fd, target.name)
    )
    pinned.temporary_identity = expected_target
    return True


def _publish_pinned(
    pinned: _PinnedTarget,
    target: Path,
    expected: bytes,
) -> None:
    temporary_name = pinned.temporary_name
    installer_identity = pinned.temporary_identity
    original_identity = pinned.target_identity
    original_metadata = pinned.target_metadata
    if temporary_name is None or installer_identity is None:
        raise InstallError(f"runtime temporary was not created: {target}")

    _exchange_pinned(pinned.parent_fd, temporary_name, target.name)
    staged_authentication_error: Exception | None = None
    try:
        _authenticate_staged_runtime(
            pinned,
            target,
            expected,
            namespace_name=target.name,
        )
    except (InstallError, OSError) as exc:
        staged_authentication_error = exc
    sync_error: Exception | None = None
    try:
        _fsync_pinned_directory(pinned.parent_fd)
    except Exception as exc:
        sync_error = exc

    try:
        published_stat = _entry_metadata(pinned.parent_fd, target.name)
        displaced_stat = _entry_metadata(pinned.parent_fd, temporary_name)
        published_identity = _identity(published_stat)
        displaced_identity = _identity(displaced_stat)
        published_metadata = _stable_regular_metadata(published_stat)
        displaced_metadata = (
            _stable_regular_metadata(displaced_stat)
            if stat.S_ISREG(displaced_stat.st_mode)
            else None
        )
    except (OSError, RuntimeError) as exc:
        pinned.preserve_temporary = True
        raise InstallError(
            f"cannot verify atomic runtime publication: {target}"
        ) from exc

    if (
        staged_authentication_error is None
        and sync_error is None
        and published_identity == installer_identity
        and displaced_identity == original_identity
        # renameat2(RENAME_EXCHANGE) updates the displaced inode's ctime even
        # when its bytes and all other stable metadata are unchanged.  Keep
        # ctime in the pre-publication/source checks, but exclude only that
        # kernel-authored field when authenticating the post-exchange escrow.
        and displaced_metadata[:-1] == original_metadata[:-1]
    ):
        pinned.target_identity = installer_identity
        pinned.target_metadata = published_metadata
        pinned.temporary_identity = original_identity
        return

    reversed_cleanly = False
    if published_identity == installer_identity:
        reversed_cleanly = _reverse_exchange(
            pinned,
            target,
            expected_target=installer_identity,
            expected_temporary=displaced_identity,
        )
    else:
        pinned.preserve_temporary = True

    if sync_error is not None:
        if not reversed_cleanly:
            raise InstallError(
                f"runtime publication durability failed and recovery is indeterminate: {target}"
            ) from sync_error
        raise InstallError(
            f"runtime publication durability failed and was reversed: {target}"
        ) from sync_error
    if staged_authentication_error is not None:
        if not reversed_cleanly:
            raise InstallError(
                f"staged runtime authentication failed and recovery is indeterminate: {target}"
            ) from staged_authentication_error
        raise InstallError(
            f"staged runtime authentication failed and was reversed: {target}"
        ) from staged_authentication_error
    if not reversed_cleanly:
        raise InstallError(
            f"concurrent target substitution detected; entries preserved: {target}"
        )
    raise InstallError(
        f"concurrent target substitution detected and reversed: {target}"
    )


def _rollback_published(pinned: _PinnedTarget, target: Path) -> bool:
    temporary_name = pinned.temporary_name
    original_identity = pinned.temporary_identity
    installer_identity = pinned.target_identity
    if temporary_name is None or original_identity is None:
        raise InstallError(f"runtime rollback escrow is missing: {target}")
    try:
        current_target = _entry_identity(pinned.parent_fd, target.name)
        current_temporary = _entry_identity(pinned.parent_fd, temporary_name)
    except OSError as exc:
        pinned.preserve_temporary = True
        raise InstallError(f"cannot inspect runtime rollback escrow: {target}") from exc
    if current_target != installer_identity:
        # A concurrent writer superseded the installer. Never overwrite that
        # writer while attempting rollback; the installer-owned inode is no
        # longer bound at this target name.
        return False
    if current_temporary != original_identity:
        pinned.preserve_temporary = True
        raise InstallError(f"runtime rollback escrow identity changed: {target}")
    if not _reverse_exchange(
        pinned,
        target,
        expected_target=installer_identity,
        expected_temporary=original_identity,
    ):
        raise InstallError(f"runtime rollback exchange was not reversible: {target}")
    return True


def _replace_all(
    payloads: dict[Path, bytes],
    *,
    rollback_sources: dict[Path, Path] | None = None,
    rollback_payloads: dict[Path, RollbackPayload] | None = None,
    trusted_root: Path | None = None,
    expected_bindings: _MutationSnapshot | None = None,
    before_replace: BeforeReplaceHook | None = None,
    before_retain: BeforeRetainHook | None = None,
    transaction_check: TransactionCheck | None = None,
    before_success: BeforeSuccessHook | None = None,
) -> tuple[Path, ...]:
    if not payloads:
        return ()
    if trusted_root is None:
        trusted_root = Path(
            os.path.commonpath([str(target.parent) for target in payloads])
        )
    trusted_root = _canonical_directory(
        trusted_root, label="trusted mutation root", must_exist=True
    )
    _require_exchange_support()
    root_fd, root_identity, pinned_targets = _open_pinned_targets(
        trusted_root,
        payloads,
        expected=expected_bindings,
    )
    replaced: list[Path] = []
    try:
        if rollback_payloads is None:
            if rollback_sources is None:
                raise InstallError("rollback custody is missing")
            rollback_payloads = {
                target: _read_rollback_payload(source)
                for target, source in rollback_sources.items()
            }
        elif rollback_sources is not None:
            raise InstallError("rollback custody is ambiguous")
        if set(rollback_payloads) != set(payloads):
            raise InstallError("rollback sources do not match runtime targets")
        for target, snapshot in rollback_payloads.items():
            content, mode = snapshot
            if not isinstance(content, bytes) or not 0 <= mode <= 0o7777:
                raise InstallError(f"rollback snapshot is invalid: {target}")
        for target, content in payloads.items():
            pinned = pinned_targets[target]
            pinned.temporary_name = (
                f".{target.name}.council-tools-{uuid.uuid4().hex}.escrow"
            )
            (
                pinned.temporary_descriptor,
                pinned.temporary_identity,
                pinned.staged_digest,
            ) = _write_pinned_temporary(
                pinned.parent_fd,
                pinned.temporary_name,
                content,
                mode=pinned.mode,
            )
            pinned.staged_identity = pinned.temporary_identity
        if transaction_check is not None:
            transaction_check()
        for target in payloads:
            pinned = pinned_targets[target]
            temporary_name = pinned.temporary_name
            _assert_current_binding(
                trusted_root,
                root_identity,
                root_fd,
                target,
                pinned,
            )
            # This is deliberately the last hook before the atomic exchange.
            # A swap here is captured as the displaced exchange entry, then
            # verified and reversed without destroying either inode.
            if before_replace is not None:
                before_replace(target.with_name(temporary_name), target)
            if transaction_check is not None:
                transaction_check()
            _authenticate_staged_runtime(
                pinned,
                target,
                payloads[target],
                namespace_name=temporary_name,
            )
            _publish_pinned(pinned, target, payloads[target])
            replaced.append(target)
            _authenticate_staged_runtime(
                pinned,
                target,
                payloads[target],
                namespace_name=target.name,
            )
            _assert_current_binding(
                trusted_root,
                root_identity,
                root_fd,
                target,
                pinned,
            )
        # A target published early in the transaction may be superseded while
        # later targets are being processed. Revalidate the whole set before
        # releasing any displaced-original escrow.
        for target in payloads:
            _authenticate_staged_runtime(
                pinned_targets[target],
                target,
                payloads[target],
                namespace_name=target.name,
            )
            _assert_current_binding(
                trusted_root,
                root_identity,
                root_fd,
                target,
                pinned_targets[target],
            )
        if transaction_check is not None:
            transaction_check()
        retained, retention_issues = _retained_escrows(
            pinned_targets,
            before_retain=before_retain,
        )
        if retention_issues:
            raise InstallError(
                "runtime publication committed but escrow retention failed "
                "closed: "
                + "; ".join(retention_issues)
                + "; retained_entries="
                + _retained_summary(retained)
            )
        if before_success is not None:
            before_success(retained)
        if transaction_check is not None:
            transaction_check()
        for target in payloads:
            _authenticate_staged_runtime(
                pinned_targets[target],
                target,
                payloads[target],
                namespace_name=target.name,
            )
        return retained
    except _CommittedCustodyFailure:
        # Runtime publication is complete and its exact original escrows are
        # retained. Preserve amendment-021 semantics: a custody-report failure
        # is explicitly reported as committed rather than silently rolled back.
        raise
    except Exception as exc:
        rollback_errors = []
        concurrent_targets = []
        for target in reversed(replaced):
            try:
                if not _rollback_published(pinned_targets[target], target):
                    concurrent_targets.append(str(target))
            except (InstallError, OSError) as rollback_exc:
                rollback_errors.append(f"{target}: {rollback_exc}")
        if rollback_errors:
            retained, retention_issues = _retained_escrows(
                pinned_targets,
                before_retain=None,
            )
            raise InstallError(
                f"replacement failed: {exc}; installer publication state="
                "partially committed; automatic rollback was incomplete: "
                + "; ".join(rollback_errors)
                + "; retained_entries="
                + _retained_summary(retained)
                + (
                    "; retention_issues=" + "; ".join(retention_issues)
                    if retention_issues
                    else ""
                )
            ) from exc
        retained, retention_issues = _retained_escrows(
            pinned_targets,
            before_retain=None,
        )
        retention_suffix = "; retained_entries=" + _retained_summary(retained)
        if retention_issues:
            retention_suffix += "; retention_issues=" + "; ".join(
                retention_issues
            )
        if concurrent_targets:
            raise InstallError(
                f"replacement failed: {exc}; installer publication state="
                "rolled back where still installer-owned; concurrent targets "
                "were preserved: "
                + ", ".join(concurrent_targets)
                + retention_suffix
            ) from exc
        state = "rolled back" if replaced else "unchanged"
        raise InstallError(
            f"replacement failed: {exc}; installer publication state={state}; "
            "runtime restored from transaction escrow and durable backup retained"
            + retention_suffix
        ) from exc
    finally:
        # Names in the mutable runtime namespace are never unlinked here. A
        # pre-unlink inode check cannot make unlink conditional; another writer
        # could replace the name in the gap. Unique escrows are deliberately
        # retained and reported for an operator-controlled cleanup context.
        for pinned in pinned_targets.values():
            if pinned.temporary_descriptor is not None:
                os.close(pinned.temporary_descriptor)
            os.close(pinned.parent_fd)
        os.close(root_fd)


def _open_existing_pinned_regular_file(
    parent_fd: int,
    name: str,
    *,
    label: str,
    required_mode: int | None = None,
) -> tuple[int, os.stat_result, bytes, str]:
    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    try:
        metadata_before = os.fstat(descriptor)
        stability = _stable_regular_metadata(metadata_before)
        if (
            metadata_before.st_nlink != 1
            or (
                required_mode is not None
                and stat.S_IMODE(metadata_before.st_mode) != required_mode
            )
        ):
            raise InstallError(f"{label} metadata is invalid")
        identity = _identity(metadata_before)
        if _entry_identity(parent_fd, name) != identity:
            raise InstallError(f"{label} binding changed")
        content = _read_descriptor(descriptor)
        metadata_after = os.fstat(descriptor)
        if _stable_regular_metadata(metadata_after) != stability:
            raise InstallError(f"{label} changed while being verified")
        digest = hashlib.sha256(content).hexdigest()
        if _entry_identity(parent_fd, name) != identity:
            raise InstallError(f"{label} binding changed while being verified")
        return descriptor, metadata_after, content, digest
    except Exception:
        os.close(descriptor)
        raise


def _open_verified_backup_payloads(
    root: Path,
    backup: Path,
) -> tuple[_AuthenticatedBackup, dict[Path, bytes]]:
    root = _canonical_directory(root, label="installation root", must_exist=True)
    backup = _canonical_directory(backup, label="backup", must_exist=True)
    backup_fd = _open_absolute_directory_nofollow(backup)
    manifest_fd = -1
    seal_fd = -1
    opened_files: list[_PinnedBackupFile] = []
    transaction: _AuthenticatedBackup | None = None
    returned = False
    try:
        (
            manifest_fd,
            manifest_metadata,
            manifest_content,
            manifest_digest,
        ) = _open_existing_pinned_regular_file(
            backup_fd,
            "MANIFEST.tsv",
            label="backup manifest",
            required_mode=0o600,
        )
        seal_fd, seal_metadata, seal_content, seal_digest = (
            _open_existing_pinned_regular_file(
                backup_fd,
                "MANIFEST.sha256",
                label="backup manifest seal",
                required_mode=0o600,
            )
        )
        if seal_content != (manifest_digest + "\n").encode("ascii"):
            raise InstallError("backup manifest seal digest mismatch")

        expected_targets = set(_validated_runtime_targets(root))
        payloads: dict[Path, bytes] = {}
        seen_relatives: set[Path] = set()
        try:
            manifest_lines = manifest_content.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise InstallError("backup manifest is not UTF-8") from exc
        for line_number, raw in enumerate(manifest_lines, 1):
            try:
                relative_text, expected_digest = raw.split("\t")
            except ValueError as exc:
                raise InstallError(f"malformed manifest line {line_number}") from exc
            relative = Path(relative_text)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative in seen_relatives
            ):
                raise InstallError(f"unsafe or duplicate manifest path: {relative}")
            seen_relatives.add(relative)
            target = root / relative
            if target not in expected_targets:
                raise InstallError(f"unexpected backup target: {target}")
            parent_fd = _open_directory_beneath(backup_fd, relative.parent)
            descriptor = -1
            try:
                descriptor, metadata, content, digest = (
                    _open_existing_pinned_regular_file(
                        parent_fd,
                        relative.name,
                        label=f"backup payload: {backup / relative}",
                    )
                )
                if digest != expected_digest:
                    raise InstallError(f"backup digest mismatch: {backup / relative}")
                opened_files.append(
                    _PinnedBackupFile(
                        relative=relative,
                        destination=backup / relative,
                        parent_fd=parent_fd,
                        descriptor=descriptor,
                        source_descriptor=-1,
                        source_metadata=(),
                        identity=_identity(metadata),
                        mode=stat.S_IMODE(metadata.st_mode),
                        digest=digest,
                    )
                )
                payloads[target] = content
                descriptor = -1
                parent_fd = -1
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if parent_fd >= 0:
                    os.close(parent_fd)
        if set(payloads) != expected_targets:
            raise InstallError(
                "backup manifest does not name the exact runtime target set"
            )
        transaction = _AuthenticatedBackup(
            path=backup,
            descriptor=backup_fd,
            files=tuple(opened_files),
            manifest_descriptor=manifest_fd,
            manifest_identity=_identity(manifest_metadata),
            manifest_digest=manifest_digest,
            seal_descriptor=seal_fd,
            seal_identity=_identity(seal_metadata),
            seal_digest=seal_digest,
            reports=[],
        )
        _authenticate_pinned_backup(transaction, verify_sources=False)
        returned = True
        return transaction, payloads
    finally:
        if not returned:
            if transaction is not None:
                _close_authenticated_backup(transaction)
            else:
                for item in opened_files:
                    os.close(item.descriptor)
                    os.close(item.parent_fd)
                if seal_fd >= 0:
                    os.close(seal_fd)
                if manifest_fd >= 0:
                    os.close(manifest_fd)
                os.close(backup_fd)


def _verified_backup_payloads(root: Path, backup: Path) -> dict[Path, bytes]:
    transaction, payloads = _open_verified_backup_payloads(root, backup)
    try:
        return payloads
    finally:
        _close_authenticated_backup(transaction)


def install(
    root: Path,
    backup_root: Path,
    *,
    before_replace: BeforeReplaceHook | None = None,
    backup_copy_hook: BackupCopyHook | None = None,
    source_repo: Path = REPO,
    require_clean_source: bool | None = None,
) -> Path:
    root = _canonical_directory(root, label="installation root", must_exist=True)
    source_repo = _canonical_directory(
        source_repo, label="council-tools source repository", must_exist=True
    )
    backup_root = _canonical_directory(
        backup_root, label="backup root", must_exist=False
    )
    if root == LIVE_ROOT:
        require_clean_source = True
    elif require_clean_source is None:
        require_clean_source = False
    try:
        backup_root.resolve().relative_to(source_repo)
    except ValueError:
        pass
    else:
        raise InstallError("backup root must be outside the council-tools source tree")
    rendered, source_custody = _render_with_source_custody(
        root,
        source_repo=source_repo,
        require_clean_source=require_clean_source,
    )
    backup_transaction: _AuthenticatedBackup | None = None
    try:
        _authenticate_pinned_sources(source_custody)
        mutation_snapshot = _snapshot_mutation_bindings(root, rendered)
        backup_transaction = _backup_targets(
            root,
            rendered,
            backup_root,
            expected_bindings=mutation_snapshot,
            backup_copy_hook=backup_copy_hook,
        )
        backup = backup_transaction.path
        rollback_payloads = _authenticated_backup_payloads(
            backup_transaction,
            root,
            verify_sources=True,
        )

        def check_backup_custody() -> None:
            _authenticate_pinned_sources(source_custody)
            _authenticate_pinned_backup(
                backup_transaction,
                verify_sources=False,
            )

        def finish_install(retained: tuple[Path, ...]) -> None:
            check_backup_custody()
            try:
                _write_retained_escrow_report(
                    backup_transaction, root, retained
                )
            except (InstallError, OSError) as exc:
                raise _CommittedCustodyFailure(retained, exc) from exc
            check_backup_custody()

        try:
            _replace_all(
                rendered,
                rollback_payloads=rollback_payloads,
                trusted_root=root,
                expected_bindings=mutation_snapshot,
                before_replace=before_replace,
                transaction_check=check_backup_custody,
                before_success=finish_install,
            )
        except _CommittedCustodyFailure as failure:
            raise _retained_custody_error(
                action="runtime publication committed but custody report failed",
                state="committed",
                backup=backup,
                retained=failure.retained,
                report=backup / "RETAINED_ESCROWS.tsv",
                cause=failure.cause,
            ) from failure.cause
        except InstallError as exc:
            raise InstallError(f"{exc}; backup={backup}") from exc
        return backup
    finally:
        if backup_transaction is not None:
            _close_authenticated_backup(backup_transaction)
        _close_pinned_sources(source_custody)


def restore(
    root: Path,
    backup: Path,
    backup_root: Path,
    *,
    backup_copy_hook: BackupCopyHook | None = None,
    before_replace: BeforeReplaceHook | None = None,
) -> Path:
    root = _canonical_directory(root, label="installation root", must_exist=True)
    backup = _canonical_directory(backup, label="backup", must_exist=True)
    backup_root = _canonical_directory(
        backup_root, label="backup root", must_exist=False
    )
    restore_source, payloads = _open_verified_backup_payloads(root, backup)
    pre_restore_transaction: _AuthenticatedBackup | None = None
    try:
        criterion = (
            root / ".claude/knowledge/council-eval/blind_seat_kill_criterion.py"
        )
        payloads[criterion] = _with_attempt_allowlist(
            payloads[criterion].decode("utf-8")
        ).encode("utf-8")
        for target in payloads:
            if not target.exists():
                raise InstallError(f"runtime target does not exist: {target}")
        mutation_snapshot = _snapshot_mutation_bindings(root, payloads)
        pre_restore_transaction = _backup_targets(
            root,
            payloads,
            backup_root,
            expected_bindings=mutation_snapshot,
            backup_copy_hook=backup_copy_hook,
        )
        pre_restore = pre_restore_transaction.path
        rollback_payloads = _authenticated_backup_payloads(
            pre_restore_transaction,
            root,
            verify_sources=True,
        )

        def check_restore_custody() -> None:
            _authenticate_pinned_backup(restore_source, verify_sources=False)
            _authenticate_pinned_backup(
                pre_restore_transaction,
                verify_sources=False,
            )

        def finish_restore(retained: tuple[Path, ...]) -> None:
            check_restore_custody()
            try:
                _write_retained_escrow_report(
                    pre_restore_transaction, root, retained
                )
            except (InstallError, OSError) as exc:
                raise _CommittedCustodyFailure(retained, exc) from exc
            check_restore_custody()

        try:
            _replace_all(
                payloads,
                rollback_payloads=rollback_payloads,
                trusted_root=root,
                expected_bindings=mutation_snapshot,
                before_replace=before_replace,
                transaction_check=check_restore_custody,
                before_success=finish_restore,
            )
        except _CommittedCustodyFailure as failure:
            raise _retained_custody_error(
                action="runtime restore publication committed but custody report failed",
                state="committed",
                backup=pre_restore,
                retained=failure.retained,
                report=pre_restore / "RETAINED_ESCROWS.tsv",
                cause=failure.cause,
            ) from failure.cause
        except InstallError as exc:
            raise InstallError(f"{exc}; pre-restore backup={pre_restore}") from exc
        return pre_restore
    finally:
        if pre_restore_transaction is not None:
            _close_authenticated_backup(pre_restore_transaction)
        _close_authenticated_backup(restore_source)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "install", "restore"))
    parser.add_argument("--root", default="/home/trader")
    parser.add_argument(
        "--backup-root", default=str(DEFAULT_BACKUP_ROOT)
    )
    parser.add_argument("--backup", help="installer backup directory to restore")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        if args.action == "check":
            clean, differences = check(root)
            if clean:
                print("runtime matches canonical sources")
                return 0
            for item in differences:
                print(f"DRIFT: {item}")
            return 1
        if args.action == "install":
            backup = install(root, Path(args.backup_root).resolve())
            print(
                f"installed; backup={backup}; retained_escrows_report="
                f"{backup / 'RETAINED_ESCROWS.tsv'}"
            )
            return 0
        if not args.backup:
            raise InstallError("restore requires --backup")
        pre_restore = restore(
            root,
            Path(args.backup).resolve(),
            Path(args.backup_root).resolve(),
        )
        print(
            f"restored; pre_restore_backup={pre_restore}; "
            f"retained_escrows_report={pre_restore / 'RETAINED_ESCROWS.tsv'}"
        )
        return 0
    except (InstallError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
