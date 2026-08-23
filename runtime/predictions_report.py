#!/usr/bin/env python3
"""Compatibility entry point for the version-controlled council forecast tools."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path


SOURCE_ROOT = Path("@@COUNCIL_TOOLS_SOURCE_ROOT@@")
EXPECTED_COMMIT = "@@COUNCIL_TOOLS_COMMIT@@"
EXPECTED_SOURCE_SHA256 = "@@COUNCIL_TOOLS_SOURCE_SHA256@@"


def _source_digest(source_root: Path) -> str:
    digest = hashlib.sha256()
    source = source_root / "src/council_tools"
    files = sorted(source.glob("*.py"))
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


_assert_source_integrity()
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from council_tools.cli import main  # noqa: E402


if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("report")
    elif sys.argv[1] == "--all":
        sys.argv[1] = "report"
    elif sys.argv[1] == "--resolve":
        raise SystemExit(
            "timestamp/index resolution is retired; use resolve <outcomeId> "
            "true|false|void with evidence"
        )
    if sys.argv[1] == "report":
        if "--log" not in sys.argv and os.environ.get("PANEL_LOG"):
            sys.argv.extend(("--log", os.environ["PANEL_LOG"]))
        if "--events" not in sys.argv and os.environ.get("PANEL_RESOLVED"):
            sys.argv.extend(("--events", os.environ["PANEL_RESOLVED"]))
    raise SystemExit(main())
