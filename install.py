#!/usr/bin/env python3
"""Install or verify the version-controlled council forecast runtime files."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import uuid
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


class InstallError(RuntimeError):
    pass


ReplaceFunction = Callable[[str | bytes | os.PathLike, str | bytes | os.PathLike], None]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_digest(source_repo: Path) -> str:
    digest = hashlib.sha256()
    source = source_repo / "src/council_tools"
    files = sorted(source.rglob("*.py"))
    if not files:
        raise InstallError(f"council-tools source files are missing: {source}")
    for path in files:
        digest.update(str(path.relative_to(source_repo)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _repository_identity(source_repo: Path, *, require_clean: bool) -> tuple[str, str]:
    try:
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
    return head, _source_digest(source_repo)


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
    old_allowlist = (
        'NON_COUNCIL_RECORD_KINDS = {"pre-mortem-calibration", "council-calibration"}'
    )
    new_allowlist = (
        'NON_COUNCIL_RECORD_KINDS = {\n'
        '    "pre-mortem-calibration",\n'
        '    "council-calibration",\n'
        '    "council-attempt",\n'
        '}'
    )
    if '"council-attempt"' in criterion_text:
        return criterion_text
    if old_allowlist not in criterion_text:
        raise InstallError("blind-seat allowlist preimage not found")
    return criterion_text.replace(old_allowlist, new_allowlist, 1)


def _render(
    root: Path,
    *,
    source_repo: Path = REPO,
    require_clean_source: bool = False,
) -> dict[Path, bytes]:
    source_repo = source_repo.resolve()
    commit, source_sha256 = _repository_identity(
        source_repo, require_clean=require_clean_source
    )
    reporter, skill, criterion, claude_md = _runtime_targets(root)
    for target in _runtime_targets(root):
        if not target.exists():
            raise InstallError(f"runtime target does not exist: {target}")

    skill_text = skill.read_text(encoding="utf-8")
    skill_text = _upsert_block(
        skill_text,
        begin=FORECAST_BEGIN,
        end=FORECAST_END,
        body=(source_repo / "runtime/council-forecast-contract.md").read_text(encoding="utf-8"),
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
        body=(source_repo / "runtime/CLAUDE_FORECAST_CONTRACT.md").read_text(encoding="utf-8"),
        marker="## SLO/SLI changes require a full council review\n",
    )

    reporter_text = (source_repo / "runtime/predictions_report.py").read_text(
        encoding="utf-8"
    )
    replacements = {
        "@@COUNCIL_TOOLS_SOURCE_ROOT@@": str(source_repo),
        "@@COUNCIL_TOOLS_COMMIT@@": commit,
        "@@COUNCIL_TOOLS_SOURCE_SHA256@@": source_sha256,
    }
    for token, value in replacements.items():
        if reporter_text.count(token) != 1:
            raise InstallError(f"runtime reporter template token count is not one: {token}")
        reporter_text = reporter_text.replace(token, value)

    return {
        reporter: reporter_text.encode("utf-8"),
        skill: skill_text.encode("utf-8"),
        criterion: criterion_text.encode("utf-8"),
        claude_md: claude_text.encode("utf-8"),
    }


def check(
    root: Path,
    *,
    source_repo: Path = REPO,
    require_clean_source: bool | None = None,
) -> tuple[bool, list[str]]:
    if require_clean_source is None:
        require_clean_source = root.resolve() == Path("/home/trader")
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


def _backup_targets(root: Path, targets: Iterable[Path], backup_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_root / f"{stamp}-{uuid.uuid4().hex[:8]}"
    backup.mkdir(parents=True, exist_ok=False)
    manifest = []
    for target in targets:
        relative = target.relative_to(root)
        destination = backup / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, destination)
        manifest.append(f"{relative}\t{_digest(destination)}")
    manifest_path = backup / "MANIFEST.tsv"
    with manifest_path.open("x", encoding="utf-8") as handle:
        handle.write("\n".join(manifest) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    latest_temporary = backup_root / f".LATEST-{uuid.uuid4().hex}.tmp"
    try:
        with latest_temporary.open("x", encoding="utf-8") as handle:
            handle.write(str(backup) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(latest_temporary, backup_root / "LATEST")
    finally:
        if latest_temporary.exists():
            latest_temporary.unlink()
    return backup


def _atomic_restore(source: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _replace_all(
    payloads: dict[Path, bytes],
    *,
    rollback_sources: dict[Path, Path],
    replace_func: ReplaceFunction = os.replace,
) -> None:
    temporaries: dict[Path, Path] = {}
    replaced: list[Path] = []
    try:
        for target, content in payloads.items():
            temporary = target.with_name(
                f".{target.name}.council-tools-{uuid.uuid4().hex}.tmp"
            )
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, target.stat().st_mode)
            temporaries[target] = temporary
        for target in payloads:
            replace_func(temporaries[target], target)
            replaced.append(target)
    except Exception as exc:
        rollback_errors = []
        for target in reversed(replaced):
            try:
                _atomic_restore(rollback_sources[target], target)
            except OSError as rollback_exc:
                rollback_errors.append(f"{target}: {rollback_exc}")
        if rollback_errors:
            raise InstallError(
                "replacement failed and automatic rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        raise InstallError("replacement failed; runtime restored from backup") from exc
    finally:
        for temporary in temporaries.values():
            if temporary.exists():
                temporary.unlink()


def _verified_backup_payloads(root: Path, backup: Path) -> dict[Path, bytes]:
    manifest = backup / "MANIFEST.tsv"
    if not manifest.is_file():
        raise InstallError(f"backup manifest does not exist: {manifest}")
    payloads: dict[Path, bytes] = {}
    for line_number, raw in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        try:
            relative_text, expected_digest = raw.split("\t")
        except ValueError as exc:
            raise InstallError(f"malformed manifest line {line_number}") from exc
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise InstallError(f"unsafe manifest path: {relative}")
        source = backup / relative
        if not source.is_file():
            raise InstallError(f"backup file does not exist: {source}")
        if _digest(source) != expected_digest:
            raise InstallError(f"backup digest mismatch: {source}")
        payloads[root / relative] = source.read_bytes()
    expected_targets = set(_runtime_targets(root))
    if set(payloads) != expected_targets:
        raise InstallError("backup manifest does not name the exact runtime target set")
    return payloads


def install(
    root: Path,
    backup_root: Path,
    *,
    replace_func: ReplaceFunction = os.replace,
    source_repo: Path = REPO,
    require_clean_source: bool | None = None,
) -> Path:
    source_repo = source_repo.resolve()
    if require_clean_source is None:
        require_clean_source = root.resolve() == Path("/home/trader")
    try:
        backup_root.resolve().relative_to(source_repo)
    except ValueError:
        pass
    else:
        raise InstallError("backup root must be outside the council-tools source tree")
    rendered = _render(
        root,
        source_repo=source_repo,
        require_clean_source=require_clean_source,
    )
    backup = _backup_targets(root, rendered, backup_root)
    rollback_sources = {
        target: backup / target.relative_to(root) for target in rendered
    }
    try:
        _replace_all(
            rendered,
            rollback_sources=rollback_sources,
            replace_func=replace_func,
        )
    except InstallError as exc:
        raise InstallError(f"{exc}; backup={backup}") from exc
    return backup


def restore(root: Path, backup: Path, backup_root: Path) -> Path:
    payloads = _verified_backup_payloads(root, backup)
    criterion = root / ".claude/knowledge/council-eval/blind_seat_kill_criterion.py"
    payloads[criterion] = _with_attempt_allowlist(
        payloads[criterion].decode("utf-8")
    ).encode("utf-8")
    for target in payloads:
        if not target.exists():
            raise InstallError(f"runtime target does not exist: {target}")
    pre_restore = _backup_targets(root, payloads, backup_root)
    rollback_sources = {
        target: pre_restore / target.relative_to(root) for target in payloads
    }
    try:
        _replace_all(payloads, rollback_sources=rollback_sources)
    except InstallError as exc:
        raise InstallError(f"{exc}; pre-restore backup={pre_restore}") from exc
    return pre_restore


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
            print(f"installed; backup={backup}")
            return 0
        if not args.backup:
            raise InstallError("restore requires --backup")
        pre_restore = restore(
            root,
            Path(args.backup).resolve(),
            Path(args.backup_root).resolve(),
        )
        print(f"restored; pre_restore_backup={pre_restore}")
        return 0
    except (InstallError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
