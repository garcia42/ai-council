#!/usr/bin/env python3
"""Install or verify the version-controlled council forecast runtime files."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
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


def _render(root: Path) -> dict[Path, bytes]:
    reporter, skill, criterion, claude_md = _runtime_targets(root)
    for target in _runtime_targets(root):
        if not target.exists():
            raise InstallError(f"runtime target does not exist: {target}")

    skill_text = skill.read_text(encoding="utf-8")
    skill_text = _upsert_block(
        skill_text,
        begin=FORECAST_BEGIN,
        end=FORECAST_END,
        body=(REPO / "runtime/council-forecast-contract.md").read_text(encoding="utf-8"),
        marker="## Steps\n",
    )

    criterion_text = criterion.read_text(encoding="utf-8")
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
    if '"council-attempt"' not in criterion_text:
        if old_allowlist not in criterion_text:
            raise InstallError("blind-seat allowlist preimage not found")
        criterion_text = criterion_text.replace(old_allowlist, new_allowlist, 1)

    claude_text = claude_md.read_text(encoding="utf-8")
    claude_text = _upsert_block(
        claude_text,
        begin=CLAUDE_BEGIN,
        end=CLAUDE_END,
        body=(REPO / "runtime/CLAUDE_FORECAST_CONTRACT.md").read_text(encoding="utf-8"),
        marker="## SLO/SLI changes require a full council review\n",
    )

    return {
        reporter: (REPO / "runtime/predictions_report.py").read_bytes(),
        skill: skill_text.encode("utf-8"),
        criterion: criterion_text.encode("utf-8"),
        claude_md: claude_text.encode("utf-8"),
    }


def check(root: Path) -> tuple[bool, list[str]]:
    rendered = _render(root)
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
) -> Path:
    try:
        backup_root.resolve().relative_to(REPO)
    except ValueError:
        pass
    else:
        raise InstallError("backup root must be outside the council-tools source tree")
    rendered = _render(root)
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
