#!/usr/bin/env python3
"""Rehearse the council-tools install against copied runtime files only."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import pwd
import re
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
LEGACY_LOG_RELATIVE = Path("truth-and-reconciliation/data/forecasts.jsonl")
LEGACY_EVENTS_RELATIVE = Path("truth-and-reconciliation/data/resolved.jsonl")
LEGACY_ENV_KEYS = (
    "PANEL_LOG",
    "PANEL_RESOLVED",
    "COUNCIL_TOOLS_DENY_OPEN_PATHS",
    "COUNCIL_RUNTIME_CONTRACT_DENY_PATHS",
)
ACCOUNT_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
LIVE_COUNCIL_LOG = ACCOUNT_HOME / ".claude/knowledge/futures-panel-log.jsonl"
LIVE_COUNCIL_EVENTS = (
    ACCOUNT_HOME / ".claude/knowledge/council-eval/predictions_resolved.jsonl"
)
LIVE_CAPTURE_EVENTS = (
    ACCOUNT_HOME / ".claude/knowledge/council-eval/capture_resolved.jsonl"
)
LIVE_COORDINATION_LOCK = ACCOUNT_HOME / ".local/state/council-tools/evidence.lock"


class RehearsalError(RuntimeError):
    pass


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _path_state(path: Path, *, hash_content: bool = True) -> dict:
    """Capture enough state to detect creation, replacement, chmod, or mutation."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    state = {
        "exists": True,
        "mode": info.st_mode,
        "inode": info.st_ino,
        "device": info.st_dev,
        "size": info.st_size,
        "nlink": info.st_nlink,
        "ctimeNs": info.st_ctime_ns,
        "mtimeNs": info.st_mtime_ns,
    }
    if hash_content and path.is_file():
        state["sha256"] = _digest(path)
    return state


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RehearsalError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clean_council_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in LEGACY_ENV_KEYS:
        env.pop(key, None)
    return env


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RehearsalError(f"{path} line {line_number}: {exc}") from exc
            if not isinstance(row, dict):
                raise RehearsalError(f"{path} line {line_number}: expected object")
            rows.append(row)
    return rows


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
    live_events = source_root / EVENTS_RELATIVE
    if live_events.is_file():
        source_files.append(live_events)
    legacy_log = source_root / LEGACY_LOG_RELATIVE
    legacy_events = source_root / LEGACY_EVENTS_RELATIVE
    if not legacy_log.is_file() or not legacy_events.is_file():
        raise RehearsalError("real T&R legacy log and resolution sidecar are required")
    source_files.extend((legacy_log, legacy_events))
    before_hashes = {str(path): _digest(path) for path in source_files}
    live_guard_paths = (
        LIVE_COUNCIL_LOG,
        LIVE_COUNCIL_EVENTS,
        LIVE_CAPTURE_EVENTS,
        LIVE_COORDINATION_LOCK,
    )
    before_live_state = {
        str(path): _path_state(
            path, hash_content=path != LIVE_COORDINATION_LOCK
        )
        for path in live_guard_paths
    }

    core_env = _clean_council_env()
    core_env["PYTHONPATH"] = f"{REPO / 'src'}:{REPO}"
    core_tests = subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "tests.test_forecasts",
            "tests.test_cli",
            "tests.test_artifacts",
            "tests.test_capture_schema",
            "tests.test_findings",
            "tests.test_data_health",
            "tests.test_evidence_backup",
            "tests.test_finding_audit",
            "tests.test_offhost_durability",
            "tests.test_activation_evidence",
            "tests.test_capture_integration",
            "tests.test_install",
            "tests.test_legacy_report",
            "-v",
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        env=core_env,
        check=False,
    )
    if core_tests.returncode != 0:
        raise RehearsalError(
            "isolated core suite failed:\n" + core_tests.stdout + core_tests.stderr
        )
    core_count_match = re.search(r"Ran (\d+) tests", core_tests.stdout + core_tests.stderr)
    if core_count_match is None:
        raise RehearsalError("could not derive isolated core test count")
    core_test_count = int(core_count_match.group(1))

    with tempfile.TemporaryDirectory(
        prefix="council-tools-rehearsal-", dir="/var/tmp"
    ) as temporary:
        stage_root = Path(temporary)
        staged_source = stage_root / "council-tools-source"
        shutil.copytree(
            REPO,
            staged_source,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        for relative in [*target_relatives, LEDGER_RELATIVE]:
            _copy(source_root, stage_root, relative)
        if live_events.is_file():
            _copy(source_root, stage_root, EVENTS_RELATIVE)

        baseline_blind = _load_module(
            source_root / BLIND_RELATIVE, "council_blind_before_rehearsal"
        )
        staged_blind_before = baseline_blind.load_rows(
            str(stage_root / LEDGER_RELATIVE)
        )
        baseline_tally = baseline_blind.tally(staged_blind_before)

        backup = install.install(
            stage_root,
            stage_root / "installer-backups",
            source_repo=staged_source,
        )
        clean, differences = install.check(stage_root, source_repo=staged_source)
        if not clean:
            raise RehearsalError(f"staged install has drift: {differences}")

        runtime_env = _clean_council_env()
        runtime_env["COUNCIL_RUNTIME_ROOT"] = str(stage_root)
        # The contract suite must use only its staged/temp stores. Its CPython
        # audit hook rejects opens and the enumerated path-mutator events before
        # their filesystem call; the post-run metadata comparison remains a
        # separate defense for effects outside that explicitly tested scope.
        denied_runtime_paths = os.pathsep.join(str(path) for path in live_guard_paths)
        runtime_env["COUNCIL_RUNTIME_CONTRACT_DENY_PATHS"] = denied_runtime_paths
        runtime_env["COUNCIL_TOOLS_DENY_OPEN_PATHS"] = denied_runtime_paths
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
        runtime_count_match = re.search(
            r"Ran (\d+) tests", runtime_tests.stdout + runtime_tests.stderr
        )
        if runtime_count_match is None:
            raise RehearsalError("could not derive staged runtime test count")
        runtime_test_count = int(runtime_count_match.group(1))
        runtime_test_output = runtime_tests.stdout + runtime_tests.stderr
        isolation_proof = re.search(
            r"test_rehearsal_audit_guard_denies_live_access_and_path_mutators.*"
            r"\.\.\. ok",
            runtime_test_output,
        )
        if isolation_proof is None:
            raise RehearsalError(
                "runtime contract live access/path-mutator denial proof did not execute"
            )

        reporter = stage_root / ".claude/knowledge/council-eval/predictions_report.py"
        council_env = _clean_council_env()
        council_env["COUNCIL_TOOLS_DENY_OPEN_PATHS"] = os.pathsep.join(
            str(path) for path in live_guard_paths
        )
        rehearsal_lock = stage_root / "evidence.lock"
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
                "--coordination-lock",
                str(rehearsal_lock),
            ],
            text=True,
            capture_output=True,
            env=council_env,
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
                "--coordination-lock",
                str(rehearsal_lock),
            ],
            text=True,
            capture_output=True,
            env=council_env,
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
                "--coordination-lock",
                str(rehearsal_lock),
            ],
            text=True,
            capture_output=True,
            env=council_env,
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
                "--coordination-lock",
                str(rehearsal_lock),
            ],
            text=True,
            capture_output=True,
            env=council_env,
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
            env=council_env,
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

        # Exercise the additive V2 lifecycle through the installed, source-pinned
        # reporter.  Every path is staged; this never activates or appends live V2.
        v2_log = stage_root / ".claude/knowledge/council-eval/v2-rehearsal.jsonl"
        v2_events = stage_root / ".claude/knowledge/council-eval/v2-resolved.jsonl"
        v2_artifacts = stage_root / "private-v2-artifacts"
        v2_lock = rehearsal_lock
        runtime_commit = subprocess.run(
            ["git", "-C", str(staged_source), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        runtime_source_sha256 = install._source_digest(staged_source)

        activation_spec = stage_root / "v2-activation.json"
        activation_spec.write_text(
            json.dumps(
                {
                    "cohortName": "copied-runtime-v2-rehearsal",
                    "captureVersion": "capture-v2.0.0",
                    "runtimeSourceCommit": runtime_commit,
                    "runtimeSourceSha256": runtime_source_sha256,
                    "artifactRootPolicy": "private-content-addressed-v1",
                }
            ),
            encoding="utf-8",
        )

        def v2_command(arguments):
            result = subprocess.run(
                [sys.executable, str(reporter), *arguments],
                text=True,
                capture_output=True,
                env=council_env,
                check=False,
            )
            if result.returncode != 0:
                raise RehearsalError(
                    f"V2 copied-runtime command failed: {arguments[0]}: "
                    + result.stdout
                    + result.stderr
                )
            return result

        activated = v2_command(
            [
                "capture-activate",
                "--log",
                str(v2_log),
                "--spec",
                str(activation_spec),
                "--coordination-lock",
                str(v2_lock),
            ]
        )
        activation_id = json.loads(activated.stdout)["activationId"]
        initiated = v2_command(
            [
                "capture-initiate",
                "--log",
                str(v2_log),
                "--activation-id",
                activation_id,
                "--idempotency-key",
                "copied-runtime-one",
                "--coordination-lock",
                str(v2_lock),
            ]
        )
        initiation = json.loads(initiated.stdout)

        baseline_file = stage_root / "v2-baseline.json"
        baseline_file.write_text(
            json.dumps(
                {
                    "schemaVersion": 3,
                    "knownConsiderations": [
                        {
                            "considerationId": "KC-01",
                            "claim": "A copied-runtime rehearsal is not live activation.",
                        }
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

        def capture_v2_artifact(path):
            result = v2_command(
                [
                    "capture-artifact",
                    "--file",
                    str(path),
                    "--artifact-root",
                    str(v2_artifacts),
                    "--run-id",
                    initiation["runId"],
                    "--log",
                    str(v2_log),
                    "--operator",
                    "copied-runtime-rehearsal",
                    "--evidence-ref",
                    "incident:copied-runtime-rehearsal",
                    "--coordination-lock",
                    str(v2_lock),
                ]
            )
            return json.loads(result.stdout)

        baseline_ref = capture_v2_artifact(baseline_file)
        baseline_bytes = baseline_file.read_bytes()
        baseline_blob = hashlib.sha1(
            b"blob " + str(len(baseline_bytes)).encode("ascii") + b"\0" + baseline_bytes
        ).hexdigest()
        staged_capture_schema = _load_module(
            staged_source / "src/council_tools/capture_schema.py",
            "council_capture_schema_for_rehearsal",
        )
        v2_question = "Does copied V2 preserve its evidence lifecycle?"
        v2_claim = "The copied V2 rehearsal remains internally valid."
        v2_resolution_date = "2099-12-31"
        v2_resolved_by = "Inspect the retained copied rehearsal."
        v2_evidence_cutoff = datetime.now(ZoneInfo("UTC")).isoformat()
        v2_materiality = "Failure blocks live activation."
        v2_action_if_true = "Retain the implementation evidence."
        v2_action_if_false = "Repair before activation."
        provisional_decision_link = (
            f"commit={runtime_commit};blob={baseline_blob};"
            f"sha256={baseline_ref['sha256']};"
            f"inputManifestSha256={'0' * 64}"
        )
        v2_outcome_id = staged_capture_schema.outcome_id_v2(
            initiation["runId"], v2_claim
        )
        v2_outcome_fingerprint = staged_capture_schema.outcome_fingerprint_v2(
            v2_claim,
            v2_resolution_date,
            v2_resolved_by,
            provisional_decision_link,
        )
        forecast_request_args = (
            initiation["runId"],
            v2_outcome_id,
            v2_outcome_fingerprint,
            v2_evidence_cutoff,
            v2_claim,
            v2_resolution_date,
            v2_resolved_by,
            v2_materiality,
            v2_action_if_true,
            v2_action_if_false,
        )
        forecast_request_block = staged_capture_schema.forecast_request_block_v2(
            *forecast_request_args
        )
        canonical_forecast_request = (
            staged_capture_schema.canonical_forecast_request_json_v2(
                forecast_request_block
            )
        )
        forecast_request = staged_capture_schema.forecast_request_identity_v2(
            *forecast_request_args
        )
        if forecast_request["forecastRequestSha256"] != hashlib.sha256(
            canonical_forecast_request
        ).hexdigest():
            raise RehearsalError(
                "copied V2 forecast request identity does not bind canonical bytes"
            )
        forecast_request_binding = (
            staged_capture_schema.forecast_request_binding_v2(
                *forecast_request_args
            )
        )
        prompt_file = stage_root / "v2-blind-prompt.txt"
        prompt_file.write_text(
            "\n".join(
                (
                    "seatId=blind",
                    f"commit={runtime_commit}",
                    f"source-sha256={runtime_source_sha256}",
                    f"blob={baseline_blob}",
                    f"sha256={baseline_ref['sha256']}",
                    forecast_request_binding,
                    f"question={v2_question}",
                    "",
                )
            ),
            encoding="utf-8",
        )
        if staged_capture_schema.parse_forecast_request_binding_v2(
            prompt_file.read_bytes()
        ) != forecast_request_block:
            raise RehearsalError(
                "copied V2 visible prompt does not contain its exact canonical request"
            )
        prompt_ref = capture_v2_artifact(prompt_file)
        output_file = stage_root / "v2-blind-output.json"
        output_file.write_text(
            json.dumps(
                {
                    "answer": "The copied lifecycle is internally consistent.",
                    "capture": {
                        "kind": "no-findings",
                        "findings": [],
                        "seatId": "blind",
                        "sharedProbability": 50,
                        **forecast_request,
                        "inputArtifactSha256": prompt_ref["sha256"],
                    },
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        output_ref = capture_v2_artifact(output_file)
        input_refs = {"blind": prompt_ref}
        input_manifest = hashlib.sha256(
            json.dumps(input_refs, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        v2_decision_link = (
            f"commit={runtime_commit};blob={baseline_blob};"
            f"sha256={baseline_ref['sha256']};"
            f"inputManifestSha256={input_manifest}"
        )
        if (
            staged_capture_schema.outcome_fingerprint_v2(
                v2_claim,
                v2_resolution_date,
                v2_resolved_by,
                v2_decision_link,
            )
            != v2_outcome_fingerprint
        ):
            raise RehearsalError(
                "copied V2 outcome fingerprint changed after prompt-manifest binding"
            )

        attempt_v2_spec = stage_root / "v2-attempt.json"
        attempt_v2_spec.write_text(
            json.dumps(
                {
                    "initiationId": initiation["initiationId"],
                    "decisionFamilyId": "family-copied-runtime-v2",
                    "question": v2_question,
                    "decisionBeforeArtifact": {**baseline_ref, "gitBlob": baseline_blob},
                    "outcomeClass": "intervention-sensitive",
                    "outcomeClassRationale": "The implementation under review controls this result.",
                    "evidenceCutoffAt": v2_evidence_cutoff,
                    "seatPlan": [
                        {
                            "seatId": "blind",
                            "role": "control",
                            "agentVersion": "copied-runtime-v1",
                            "agentDefinitionDigest": hashlib.sha256(
                                b"copied-runtime-blind"
                            ).hexdigest(),
                        }
                    ],
                    "sharedOutcome": {
                        "claim": v2_claim,
                        "resolutionDate": v2_resolution_date,
                        "resolvedBy": v2_resolved_by,
                        "decisionLink": v2_decision_link,
                        "materiality": v2_materiality,
                        "actionIfTrue": v2_action_if_true,
                        "actionIfFalse": v2_action_if_false,
                        "relatedOutcomeIds": [],
                    },
                    "seatInputArtifacts": input_refs,
                }
            ),
            encoding="utf-8",
        )
        v2_command(
            [
                "capture-attempt",
                "--log",
                str(v2_log),
                "--artifact-root",
                str(v2_artifacts),
                "--spec",
                str(attempt_v2_spec),
                "--decision-before-file",
                str(baseline_file),
                "--visible-input",
                f"blind={prompt_file}",
                "--coordination-lock",
                str(v2_lock),
            ]
        )
        seats_finished_spec = stage_root / "v2-seats-finished.json"
        seats_finished_spec.write_text(
            json.dumps(
                {"runId": initiation["runId"], "seatStates": {"blind": "submitted"}}
            ),
            encoding="utf-8",
        )
        v2_command(
            [
                "capture-seats-finished",
                "--log",
                str(v2_log),
                "--spec",
                str(seats_finished_spec),
                "--coordination-lock",
                str(v2_lock),
            ]
        )
        completion_v2_spec = stage_root / "v2-completion.json"
        completion_v2_spec.write_text(
            json.dumps(
                {
                    "runId": initiation["runId"],
                    "seatResults": [
                        {
                            "seatId": "blind",
                            "role": "control",
                            "agentVersion": "copied-runtime-v1",
                            "agentDefinitionDigest": hashlib.sha256(
                                b"copied-runtime-blind"
                            ).hexdigest(),
                            "state": "submitted",
                            "launcherAttempts": 1,
                            "inputArtifact": prompt_ref,
                            "outputArtifact": output_ref,
                            "modelId": "copied-runtime-model",
                            "toolPolicy": "no-tools-v1",
                            "repositoryCommit": runtime_commit,
                        }
                    ],
                    "findings": [],
                    "noFindings": [
                        {
                            "kind": "no-findings",
                            "seatId": "blind",
                            "outputArtifact": output_ref,
                        }
                    ],
                    "probabilities": {"blind": 50},
                    "blindSeat": {
                        "role": "independent-control",
                        "required": True,
                        "ran": True,
                        "changedDecision": False,
                        "brief": f"{prompt_ref['path']}#{initiation['runId']}",
                    },
                    "seatInputArtifacts": input_refs,
                }
            ),
            encoding="utf-8",
        )
        v2_command(
            [
                "capture-complete",
                "--log",
                str(v2_log),
                "--artifact-root",
                str(v2_artifacts),
                "--spec",
                str(completion_v2_spec),
                "--decision-before-file",
                str(baseline_file),
                "--visible-input",
                f"blind={prompt_file}",
                "--visible-output",
                f"blind={output_file}",
                "--coordination-lock",
                str(v2_lock),
            ]
        )
        v2_report_result = v2_command(
            [
                "capture-report",
                "--log",
                str(v2_log),
                "--events",
                str(v2_events),
                "--artifact-root",
                str(v2_artifacts),
                "--as-of",
                "2099-01-01T00:00:00Z",
                "--json",
            ]
        )
        v2_report = json.loads(v2_report_result.stdout)
        if (
            v2_report["cohort"]["eligibleInitiationCount"] != 1
            or v2_report["cohort"]["completeInitiationCount"] != 1
            or v2_report["artifacts"]["artifactIntegrityFailureCount"] != 0
            or v2_report["ledger"]["invalidV2RecordCount"] != 0
        ):
            raise RehearsalError(f"V2 copied-runtime report mismatch: {v2_report}")

        # Prove that reporting re-parses the retained visible request rather than
        # trusting append-time checks.  The probe changes a valid, fingerprint-
        # neutral outcome field in a copied ledger while leaving the retained
        # prompt untouched; report-time provenance must invalidate completion.
        report_binding_probe_log = stage_root / "v2-report-binding-probe.jsonl"
        report_binding_probe_rows = []
        for line in v2_log.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("kind") in {"council-attempt-v2", "council-v2"}:
                row["sharedOutcome"]["materiality"] = (
                    "A changed target must be rejected at report time."
                )
            report_binding_probe_rows.append(
                json.dumps(row, sort_keys=True, separators=(",", ":"))
            )
        report_binding_probe_log.write_text(
            "\n".join(report_binding_probe_rows) + "\n",
            encoding="utf-8",
        )
        report_binding_probe_result = v2_command(
            [
                "capture-report",
                "--log",
                str(report_binding_probe_log),
                "--events",
                str(v2_events),
                "--artifact-root",
                str(v2_artifacts),
                "--as-of",
                "2099-01-01T00:00:00Z",
                "--json",
            ]
        )
        report_binding_probe = json.loads(report_binding_probe_result.stdout)
        report_binding_errors = [
            str(item.get("error", ""))
            for item in report_binding_probe["ledger"]["invalidV2Records"]
        ]
        if (
            report_binding_probe["ledger"]["invalidV2RecordCount"] != 1
            or report_binding_probe["cohort"]["completeInitiationCount"] != 0
            or not any(
                "report-time forecast provenance failure" in error
                and "forecast request block differs from the sealed shared target"
                in error
                for error in report_binding_errors
            )
        ):
            raise RehearsalError(
                "V2 copied-runtime report did not reject a retained prompt/target "
                f"mismatch: {report_binding_probe}"
            )

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
            env=council_env,
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
        v2_tally = staged_blind.tally(staged_blind.load_rows(str(v2_log)))
        if (
            v2_tally["completedRuns"] != 1
            or v2_tally["nonCouncilRecords"] != 4
            or v2_tally["errors"]
        ):
            raise RehearsalError(f"V2 blind-seat classification mismatch: {v2_tally}")
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

        drift_source = staged_source / "src/council_tools/forecasts.py"
        drift_original = drift_source.read_bytes()
        drift_source.write_bytes(drift_original + b"\n# injected rehearsal drift\n")
        drift_log = stage_root / ".claude/knowledge/council-eval/drift-attempt.jsonl"
        drift_attempt = subprocess.run(
            [
                sys.executable,
                str(reporter),
                "attempt",
                "--log",
                str(drift_log),
                "--spec",
                str(attempt_spec),
                "--ts",
                "2026-08-22T12:00:00Z",
                "--coordination-lock",
                str(rehearsal_lock),
            ],
            text=True,
            capture_output=True,
            env=council_env,
            check=False,
        )
        drift_source.write_bytes(drift_original)
        if (
            drift_attempt.returncode == 0
            or "runtime integrity check failed" not in drift_attempt.stderr
            or drift_log.exists()
        ):
            raise RehearsalError(
                "write-capable runtime did not fail closed on imported-source drift"
            )

        staged_legacy_log = stage_root / "legacy-copy/forecasts.jsonl"
        staged_legacy_events = stage_root / "legacy-copy/resolved.jsonl"
        staged_legacy_log.parent.mkdir(parents=True)
        shutil.copy2(legacy_log, staged_legacy_log)
        shutil.copy2(legacy_events, staged_legacy_events)
        legacy_rows = _load_jsonl(staged_legacy_log)
        legacy_resolutions = _load_jsonl(staged_legacy_events)
        resolved_keys = {
            event.get("key")
            or f"{event.get('ts')}#{event.get('index')}"
            for event in legacy_resolutions
        }
        unresolved = None
        for row in legacy_rows:
            predictions = row.get("predictions") or []
            if not isinstance(predictions, list):
                raise RehearsalError("real T&R predictions field is not a list")
            for index, prediction in enumerate(predictions):
                key = f"{row.get('ts')}#{index}"
                if key not in resolved_keys and isinstance(prediction, dict):
                    unresolved = (str(row.get("ts")), index)
                    break
            if unresolved is not None:
                break
        if unresolved is None:
            raise RehearsalError("real T&R log has no unresolved item for copied resolve gate")
        legacy_env = {
            **council_env,
            "PANEL_LOG": str(staged_legacy_log),
            "PANEL_RESOLVED": str(staged_legacy_events),
            "COUNCIL_TOOLS_DENY_OPEN_PATHS": os.pathsep.join(
                (
                    str(source_root / LEDGER_RELATIVE),
                    str(source_root / EVENTS_RELATIVE),
                    *(str(path) for path in live_guard_paths),
                )
            ),
        }
        legacy_before = subprocess.run(
            [sys.executable, str(reporter), "--all"],
            text=True,
            capture_output=True,
            env=legacy_env,
            check=False,
        )
        expected_before = f"CALIBRATION ({len(legacy_resolutions)} resolved)"
        if legacy_before.returncode != 0 or expected_before not in legacy_before.stdout:
            raise RehearsalError(
                "real-data-copy T&R report gate failed: "
                + legacy_before.stdout
                + legacy_before.stderr
            )
        legacy_resolve = subprocess.run(
            [
                sys.executable,
                str(reporter),
                "--resolve",
                unresolved[0],
                str(unresolved[1]),
                "false",
                "copied-data rehearsal only",
            ],
            text=True,
            capture_output=True,
            env=legacy_env,
            check=False,
        )
        copied_resolutions = _load_jsonl(staged_legacy_events)
        if legacy_resolve.returncode != 0 or len(copied_resolutions) != len(legacy_resolutions) + 1:
            raise RehearsalError(
                "real-data-copy T&R resolve gate failed: "
                + legacy_resolve.stdout
                + legacy_resolve.stderr
            )
        legacy_after = subprocess.run(
            [sys.executable, str(reporter), "--all"],
            text=True,
            capture_output=True,
            env=legacy_env,
            check=False,
        )
        expected_after = f"CALIBRATION ({len(copied_resolutions)} resolved)"
        if legacy_after.returncode != 0 or expected_after not in legacy_after.stdout:
            raise RehearsalError(
                "post-resolve T&R copied report gate failed: "
                + legacy_after.stdout
                + legacy_after.stderr
            )

        after_hashes = {str(path): _digest(path) for path in source_files}
        if before_hashes != after_hashes:
            raise RehearsalError("source runtime files changed during rehearsal")
        after_live_state = {
            str(path): _path_state(
                path, hash_content=path != LIVE_COORDINATION_LOCK
            )
            for path in live_guard_paths
        }
        if before_live_state != after_live_state:
            raise RehearsalError("live council paths changed during rehearsal")

        return {
            "status": "PASS",
            "sourceRoot": str(source_root),
            "liveFilesUnchanged": True,
            "liveCoordinationLockUnopened": True,
            "stagedInstallClean": True,
            "stagedBackupManifest": str(backup.relative_to(stage_root) / "MANIFEST.tsv"),
            "isolatedCoreTests": core_test_count,
            "runtimeContractTests": runtime_test_count,
            "runtimeContractIsolation": {
                "stores": "staged-or-temp-only",
                "liveOpenDeniedBeforeSyscall": True,
                "liveLinkDeniedBeforeSyscall": True,
                "executedProofTest": (
                    "test_rehearsal_audit_guard_denies_live_access_and_path_mutators"
                ),
                "representativeMutationProofs": {
                    "chmod": True,
                    "rename": True,
                    "replace": True,
                    "remove": True,
                    "unlink": True,
                    "symlink": True,
                },
                "deniedCpythonAuditEvents": [
                    "open",
                    "os.chmod",
                    "os.chown",
                    "os.utime",
                    "os.truncate",
                    "os.link",
                    "os.symlink",
                    "os.rename",
                    "os.remove",
                    "os.mkdir",
                    "os.rmdir",
                ],
                "eventAliases": {
                    "os.replace": "os.rename",
                    "os.unlink": "os.remove",
                },
                "metadataComparisonDefenseInDepth": True,
                "enforcementScope": (
                    "CPython audit events in the runtime-contract subprocess; "
                    "not arbitrary native syscalls outside CPython auditing or "
                    "access through a directory descriptor inherited before the hook"
                ),
            },
            "legacyCompatibility": {
                "realRowsParsed": len(legacy_rows),
                "realResolutionsParsed": len(legacy_resolutions),
                "copiedResolutionAppended": True,
                "councilPathsDeniedDuringProcess": True,
            },
            "failurePath": {
                "malformedProbabilityRejectedWithoutAppend": True,
                "unavailableSeatSealedWithoutProbability": True,
                "completedRows": failure_payload["completeForecastRows"],
                "forecastIssuances": failure_payload["forecastIssuances"],
            },
            "captureV2": {
                "copiedRuntimeLifecycleComplete": True,
                "forecastRequestAndPromptBound": True,
                "canonicalForecastRequestParsed": True,
                "reportTimeForecastRequestVerified": True,
                "structuredEmptyFindingsVerified": True,
                "completeInitiations": v2_report["cohort"]["completeInitiationCount"],
                "artifactIntegrityFailures": v2_report["artifacts"][
                    "artifactIntegrityFailureCount"
                ],
                "blindCompletedRuns": v2_tally["completedRuns"],
                "blindNonCouncilRecords": v2_tally["nonCouncilRecords"],
                "liveActivated": False,
            },
            "runtimeSourcePin": {
                "commit": subprocess.run(
                    ["git", "-C", str(staged_source), "rev-parse", "HEAD"],
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip(),
                "sourceDriftRejectedBeforeAppend": True,
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
