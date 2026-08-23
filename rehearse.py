#!/usr/bin/env python3
"""Rehearse the council-tools install against copied runtime files only."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import install


REPO = Path(__file__).resolve().parent
LEDGER_RELATIVE = Path(".claude/knowledge/futures-panel-log.jsonl")
EVENTS_RELATIVE = Path(
    ".claude/knowledge/council-eval/predictions_resolved.jsonl"
)
BLIND_RELATIVE = Path(
    ".claude/knowledge/council-eval/blind_seat_kill_criterion.py"
)


class RehearsalError(RuntimeError):
    pass


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RehearsalError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _copy(source_root: Path, stage_root: Path, relative: Path) -> None:
    source = source_root / relative
    if not source.is_file():
        raise RehearsalError(f"rehearsal source does not exist: {source}")
    destination = stage_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def rehearse(source_root: Path, *, today: str) -> dict:
    source_root = source_root.resolve()
    target_relatives = [
        target.relative_to(source_root) for target in install._runtime_targets(source_root)
    ]
    source_files = [
        *(source_root / relative for relative in target_relatives),
        source_root / LEDGER_RELATIVE,
    ]
    before_hashes = {str(path): _digest(path) for path in source_files}

    with tempfile.TemporaryDirectory(prefix="council-tools-rehearsal-") as temporary:
        stage_root = Path(temporary)
        for relative in [*target_relatives, LEDGER_RELATIVE]:
            _copy(source_root, stage_root, relative)
        live_events = source_root / EVENTS_RELATIVE
        if live_events.is_file():
            _copy(source_root, stage_root, EVENTS_RELATIVE)

        baseline_blind = _load_module(
            source_root / BLIND_RELATIVE, "council_blind_before_rehearsal"
        )
        staged_blind_before = baseline_blind.load_rows(
            str(stage_root / LEDGER_RELATIVE)
        )
        baseline_tally = baseline_blind.tally(staged_blind_before)

        backup = install.install(stage_root, stage_root / "installer-backups")
        clean, differences = install.check(stage_root)
        if not clean:
            raise RehearsalError(f"staged install has drift: {differences}")

        runtime_env = dict(os.environ)
        runtime_env["COUNCIL_RUNTIME_ROOT"] = str(stage_root)
        runtime_tests = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(REPO / "tests"),
                "-p",
                "test_runtime_contract.py",
                "-v",
            ],
            text=True,
            capture_output=True,
            env=runtime_env,
            check=False,
        )
        if runtime_tests.returncode != 0:
            raise RehearsalError(
                "staged runtime contract tests failed:\n"
                + runtime_tests.stdout
                + runtime_tests.stderr
            )

        reporter = stage_root / ".claude/knowledge/council-eval/predictions_report.py"
        failure_log = stage_root / ".claude/knowledge/council-eval/failure-path.jsonl"
        failure_events = stage_root / ".claude/knowledge/council-eval/failure-events.jsonl"
        attempt_spec = stage_root / "failure-attempt.json"
        attempt_spec.write_text(
            json.dumps(
                {
                    "question": "Does the failure-path rehearsal preserve a sealed set?",
                    "expectedSeats": ["code", "theory", "ops", "blind"],
                    "sharedOutcome": {
                        "claim": "The failure-path rehearsal completes without a partial set",
                        "resolutionDate": "2026-09-30",
                        "resolvedBy": "Inspect the retained rehearsal output",
                        "decisionLink": "Council scorer activation",
                        "materiality": "A partial set would invalidate activation",
                        "actionIfTrue": "Continue activation review",
                        "actionIfFalse": "Hold activation and repair sealing",
                        "evidenceCutoffAt": "2026-08-22T12:00:00Z",
                    },
                }
            ),
            encoding="utf-8",
        )
        started = subprocess.run(
            [
                sys.executable,
                str(reporter),
                "attempt",
                "--log",
                str(failure_log),
                "--spec",
                str(attempt_spec),
                "--ts",
                "2026-08-22T12:00:00Z",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if started.returncode != 0:
            raise RehearsalError(f"failure-path attempt failed: {started.stderr}")
        run_id = json.loads(started.stdout)["runId"]
        common_completion = {
            "runId": run_id,
            "councilFields": {
                "verdicts": {
                    "code": "APPROVE",
                    "theory": "APPROVE",
                    "ops": "APPROVE",
                },
                "blindSeat": {
                    "role": "generic",
                    "required": True,
                    "ran": True,
                    "changedDecision": False,
                },
            },
        }
        malformed_spec = stage_root / "failure-malformed-completion.json"
        malformed_spec.write_text(
            json.dumps(
                {
                    **common_completion,
                    "seatStates": {
                        seat: "submitted" for seat in ("code", "theory", "ops", "blind")
                    },
                    "probabilities": {
                        "code": "not-an-integer",
                        "theory": 50,
                        "ops": 50,
                        "blind": 50,
                    },
                }
            ),
            encoding="utf-8",
        )
        rejected = subprocess.run(
            [
                sys.executable,
                str(reporter),
                "complete",
                "--log",
                str(failure_log),
                "--spec",
                str(malformed_spec),
                "--ts",
                "2026-08-22T12:05:00Z",
                "--check-only",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if rejected.returncode != 1 or len(failure_log.read_text(encoding="utf-8").splitlines()) != 1:
            raise RehearsalError("malformed submitted probability did not fail without append")

        unavailable_spec = stage_root / "failure-unavailable-completion.json"
        unavailable_spec.write_text(
            json.dumps(
                {
                    **common_completion,
                    "seatStates": {
                        "code": "unavailable",
                        "theory": "submitted",
                        "ops": "submitted",
                        "blind": "submitted",
                    },
                    "probabilities": {"theory": 50, "ops": 50, "blind": 50},
                }
            ),
            encoding="utf-8",
        )
        checked = subprocess.run(
            [
                sys.executable,
                str(reporter),
                "complete",
                "--log",
                str(failure_log),
                "--spec",
                str(unavailable_spec),
                "--ts",
                "2026-08-22T12:06:00Z",
                "--check-only",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(reporter),
                "complete",
                "--log",
                str(failure_log),
                "--spec",
                str(unavailable_spec),
                "--ts",
                "2026-08-22T12:06:00Z",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if checked.returncode != 0 or completed.returncode != 0:
            raise RehearsalError(
                "explicit unavailable-seat completion failed: "
                + checked.stderr
                + completed.stderr
            )
        failure_report = subprocess.run(
            [
                sys.executable,
                str(reporter),
                "report",
                "--log",
                str(failure_log),
                "--events",
                str(failure_events),
                "--today",
                today,
                "--json",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if failure_report.returncode != 0:
            raise RehearsalError(f"failure-path report failed: {failure_report.stderr}")
        failure_payload = json.loads(failure_report.stdout)
        if (
            failure_payload["completeForecastRows"] != 1
            or failure_payload["forecastIssuances"] != 3
            or failure_payload["missingForecastSeats"]
            or failure_payload["seatEmissionStates"]["code"]["unavailable"] != 1
        ):
            raise RehearsalError(f"failure-path accounting mismatch: {failure_payload}")

        report_command = [
            sys.executable,
            str(reporter),
            "report",
            "--log",
            str(stage_root / LEDGER_RELATIVE),
            "--events",
            str(stage_root / EVENTS_RELATIVE),
            "--today",
            today,
            "--json",
        ]
        report = subprocess.run(
            report_command,
            text=True,
            capture_output=True,
            check=False,
        )
        if report.returncode not in (0, 3):
            raise RehearsalError(
                f"staged report failed with {report.returncode}: {report.stderr}"
            )
        report_payload = json.loads(report.stdout)

        staged_blind = _load_module(
            stage_root / BLIND_RELATIVE, "council_blind_after_rehearsal"
        )
        staged_tally = staged_blind.tally(
            staged_blind.load_rows(str(stage_root / LEDGER_RELATIVE))
        )
        stable_tally_fields = (
            "completedRuns",
            "changedDecisionRuns",
            "unchangedDecisionRuns",
            "blockedNonRuns",
            "notRequiredSkips",
            "consecutiveRequiredBlockedNonRuns",
            "operationalState",
            "decisionChangingRate",
            "criterion",
            "errors",
        )
        baseline_stable = {key: baseline_tally[key] for key in stable_tally_fields}
        staged_stable = {key: staged_tally[key] for key in stable_tally_fields}
        if baseline_stable != staged_stable:
            raise RehearsalError(
                f"blind-seat decision tally changed: {baseline_stable} != {staged_stable}"
            )

        after_hashes = {str(path): _digest(path) for path in source_files}
        if before_hashes != after_hashes:
            raise RehearsalError("source runtime files changed during rehearsal")

        return {
            "status": "PASS",
            "sourceRoot": str(source_root),
            "liveFilesUnchanged": True,
            "stagedInstallClean": True,
            "stagedBackupManifest": str(backup.relative_to(stage_root) / "MANIFEST.tsv"),
            "runtimeContractTests": 3,
            "failurePath": {
                "malformedProbabilityRejectedWithoutAppend": True,
                "unavailableSeatSealedWithoutProbability": True,
                "completedRows": failure_payload["completeForecastRows"],
                "forecastIssuances": failure_payload["forecastIssuances"],
            },
            "blindTallyStable": baseline_stable,
            "blindClassificationBefore": {
                "legacyBlindRows": baseline_tally["legacyBlindRows"],
                "nonCouncilRecords": baseline_tally["nonCouncilRecords"],
            },
            "blindClassificationAfter": {
                "legacyBlindRows": staged_tally["legacyBlindRows"],
                "nonCouncilRecords": staged_tally["nonCouncilRecords"],
            },
            "reportExitCode": report.returncode,
            "report": report_payload,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/home/trader")
    parser.add_argument(
        "--today",
        default=datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
    )
    args = parser.parse_args()
    try:
        print(json.dumps(rehearse(Path(args.root), today=args.today), sort_keys=True))
        return 0
    except (RehearsalError, install.InstallError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
