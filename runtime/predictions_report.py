#!/usr/bin/env python3
"""Compatibility entry point for the version-controlled council forecast tools."""

import hashlib
import os
import pwd
import subprocess
import sys
from pathlib import Path


SOURCE_ROOT = Path("@@COUNCIL_TOOLS_SOURCE_ROOT@@")
EXPECTED_COMMIT = "@@COUNCIL_TOOLS_COMMIT@@"
EXPECTED_SOURCE_SHA256 = "@@COUNCIL_TOOLS_SOURCE_SHA256@@"
ACCOUNT_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
COUNCIL_LOG = ACCOUNT_HOME / ".claude/knowledge/futures-panel-log.jsonl"
COUNCIL_RESOLVED = (
    ACCOUNT_HOME / ".claude/knowledge/council-eval/predictions_resolved.jsonl"
)
LIVE_KNOWLEDGE_ROOT = (ACCOUNT_HOME / ".claude/knowledge").resolve()


def _install_deny_path_audit_hook() -> None:
    """Test-only negative proof: fail if this process opens any named path."""

    raw_paths = os.environ.get("COUNCIL_TOOLS_DENY_OPEN_PATHS")
    if not raw_paths:
        return
    denied = {Path(item).expanduser().resolve() for item in raw_paths.split(os.pathsep)}

    def deny_open(event, args):
        if event != "open" or not args:
            return
        raw_path = args[0]
        if not isinstance(raw_path, (str, bytes, os.PathLike)):
            return
        if Path(raw_path).expanduser().resolve() in denied:
            raise RuntimeError(f"denied test open: {raw_path}")

    sys.addaudithook(deny_open)


def _assert_legacy_store_is_separate(log_path: str, resolved_path: str) -> None:
    council_paths = (COUNCIL_LOG.resolve(), COUNCIL_RESOLVED.resolve())
    selected = (
        Path(log_path).expanduser().resolve(),
        Path(resolved_path).expanduser().resolve(),
    )

    def aliases(left: Path, right: Path) -> bool:
        if left == right:
            return True
        try:
            return left.samefile(right)
        except (FileNotFoundError, OSError):
            return False

    overlap = {
        candidate
        for candidate in selected
        if candidate.is_relative_to(LIVE_KNOWLEDGE_ROOT)
        or any(aliases(candidate, council_path) for council_path in council_paths)
    }
    if overlap:
        raise SystemExit(
            "legacy compatibility mode refuses live-knowledge paths or council-store aliases: "
            + ", ".join(str(path) for path in sorted(overlap, key=str))
        )
    if aliases(*selected):
        raise SystemExit("legacy log and resolution paths must be separate files")


def _source_digest(source_root: Path) -> str:
    digest = hashlib.sha256()
    source = source_root / "src/council_tools"
    files = sorted(source.rglob("*.py"))
    if not files:
        raise RuntimeError(f"council-tools source files are missing: {source}")
    for path in files:
        digest.update(str(path.relative_to(source_root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _assert_source_integrity() -> None:
    try:
        head = subprocess.run(
            ["git", "-C", str(SOURCE_ROOT), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        source_digest = _source_digest(SOURCE_ROOT)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"council-tools runtime integrity check failed: {exc}") from exc
    if head != EXPECTED_COMMIT or source_digest != EXPECTED_SOURCE_SHA256:
        raise SystemExit(
            "council-tools runtime integrity check failed: installed pin does not match "
            f"source (expected commit {EXPECTED_COMMIT}, found {head})"
        )


_install_deny_path_audit_hook()
_assert_source_integrity()
sys.path.insert(0, str(SOURCE_ROOT / "src"))


if __name__ == "__main__":
    legacy_log = os.environ.get("PANEL_LOG")
    legacy_resolved = os.environ.get("PANEL_RESOLVED")
    if legacy_log or legacy_resolved:
        if not legacy_log or not legacy_resolved:
            raise SystemExit("legacy mode requires both PANEL_LOG and PANEL_RESOLVED")
        _assert_legacy_store_is_separate(legacy_log, legacy_resolved)
        from council_tools.legacy_report import main as legacy_main

        raise SystemExit(
            legacy_main(
                sys.argv[1:],
                log_path=legacy_log,
                resolved_path=legacy_resolved,
            )
        )
    from council_tools.cli import main

    if len(sys.argv) == 1:
        sys.argv.append("report")
    elif sys.argv[1] == "--all":
        sys.argv[1] = "report"
    elif sys.argv[1] == "--resolve":
        raise SystemExit(
            "timestamp/index resolution is retired; use resolve <outcomeId> "
            "true|false|void with evidence"
        )
    raise SystemExit(
        main(
            runtime_source_commit=EXPECTED_COMMIT,
            runtime_source_sha256=EXPECTED_SOURCE_SHA256,
            runtime_source_root=SOURCE_ROOT,
        )
    )
