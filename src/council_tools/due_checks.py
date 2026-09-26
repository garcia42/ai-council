"""Run the machine-checkable resolution of due council outcomes.

An attempt may carry ``sharedOutcome.check``: an absolute command the seats saw
and the fingerprint binds. Once the outcome's resolution date has ended in
America/New_York, ``resolve-due`` runs it. Exit 0 records TRUE and exit 1 FALSE,
both as ``deterministic`` resolutions whose evidence is a retained JSON record
of exactly what ran. Any other exit, a timeout, or a command that cannot start
records nothing: that outcome stays with a human, like every outcome without a
check.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .forecasts import LedgerError, validate_check
from .safe_files import SafeFileError, create_bytes_exclusive

RESOLVER = "resolve-due"
OUTPUT_LIMIT_BYTES = 65536
#: The environment every check runs in. A check must not depend on whoever
#: happens to run ``resolve-due``: same command, same inputs, same grade.
CHECK_PATH = "/usr/local/bin:/usr/bin:/bin"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _truncate(data: bytes) -> tuple[str, bool]:
    return (
        data[:OUTPUT_LIMIT_BYTES].decode("utf-8", errors="replace"),
        len(data) > OUTPUT_LIMIT_BYTES,
    )


def run_check(check: Mapping[str, Any]) -> dict[str, Any]:
    """Run one check and describe the result; never raises for the command's own faults."""

    validate_check(check)
    started = _now()
    env = {
        "PATH": CHECK_PATH,
        "HOME": os.environ.get("HOME", "/"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    try:
        process = subprocess.Popen(
            check["argv"],
            cwd=check["cwd"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return {
            "startedAt": started,
            "finishedAt": _now(),
            "exitCode": None,
            "verdict": "undetermined",
            "reason": f"could not start: {exc.__class__.__name__}: {exc.strerror or exc}",
            "stdout": "",
            "stderr": "",
        }
    try:
        stdout, stderr = process.communicate(timeout=check["timeoutSeconds"])
        timed_out = False
    except subprocess.TimeoutExpired:
        # The whole session, not just the child: a shell check can leave
        # grandchildren that would otherwise outlive the grade.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        timed_out = True
    out_text, out_truncated = _truncate(stdout)
    err_text, err_truncated = _truncate(stderr)
    code = None if timed_out else process.returncode
    if timed_out:
        verdict, reason = "undetermined", f"timed out after {check['timeoutSeconds']}s"
    elif code == 0:
        verdict, reason = "true", "exit 0"
    elif code == 1:
        verdict, reason = "false", "exit 1"
    else:
        verdict, reason = "undetermined", f"exit {code} is neither 0 nor 1"
    return {
        "startedAt": started,
        "finishedAt": _now(),
        "exitCode": code,
        "verdict": verdict,
        "reason": reason,
        "stdout": out_text,
        "stdoutTruncated": out_truncated,
        "stderr": err_text,
        "stderrTruncated": err_truncated,
    }


def due_candidates(current: Mapping[str, Any], today) -> list[str]:
    """Outcomes with a check, a resolution date that has ended, and no grade yet."""

    graded = set(current["gradedOutcomeIds"])
    dates = current["outcomeResolutionDates"]
    return [
        outcome_id
        for outcome_id in sorted(current["outcomeChecks"])
        if outcome_id not in graded
        and outcome_id in current["outcomeFingerprints"]
        and date.fromisoformat(dates[outcome_id]) < today
    ]


def write_evidence(
    evidence_dir: Path,
    outcome_id: str,
    fingerprint: str,
    check: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    on_directory_fsync: Callable[[Path], None] | None = None,
) -> str:
    """Retain the run as an exclusive file and return the evidence reference."""

    record = {
        "schemaVersion": 1,
        "kind": "resolve-due-evidence",
        "outcomeId": outcome_id,
        "outcomeFingerprint": fingerprint,
        "check": dict(check),
        **result,
    }
    data = (json.dumps(record, sort_keys=True, indent=1) + "\n").encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    stamp = result["startedAt"].replace(":", "").replace("-", "")
    path = evidence_dir / f"{outcome_id}-{stamp}.json"
    try:
        create_bytes_exclusive(path, data, on_directory_fsync=on_directory_fsync)
    except SafeFileError as exc:
        raise LedgerError(str(exc)) from exc
    return f"{path}#sha256={digest}"
