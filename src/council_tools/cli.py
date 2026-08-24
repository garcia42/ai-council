"""Command-line interface for council forecast records and reports."""

from __future__ import annotations

import argparse
import json
import math
import os
import pwd
import socket
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .artifacts import ArtifactStore, SecretDetectedError, secret_detectors
from .activation_evidence import evaluate_activation_evidence
from .capture_runtime import (
    append_capture_activation,
    append_evidence_bound_capture_activation,
    append_capture_initiation,
    append_capture_invalidation,
    append_capture_resolution,
    append_council_attempt_v2,
    append_council_seats_finished,
    append_council_v2,
    capture_report,
    validate_capture_ledger,
)
from .capture_schema import strict_json_loads
from .recording_coverage import (
    RecordingCoverageError,
    format_recording_coverage,
    parse_timestamp,
    recording_exit_code,
    report_recording_coverage,
)
from .evidence_backup import (
    create_evidence_snapshot,
    restore_evidence_snapshot,
    verify_evidence_snapshot,
)

from .brief_recovery import (
    plan_blind_brief_recovery,
    prepare_blind_brief,
    recover_blind_brief,
)
from .forecasts import (
    LedgerError,
    append_ledger_row,
    append_override,
    append_resolution,
    audit,
    derived_ledger_lock_path,
    evidence_write_lock,
    load_jsonl,
    make_attempt,
    make_completion,
    repair_trailing_jsonl,
    transaction_escrow_inventory,
    validate_ledger_row,
)


ACCOUNT_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
DEFAULT_LOG = str(ACCOUNT_HOME / ".claude/knowledge/futures-panel-log.jsonl")
DEFAULT_EVENTS = str(
    ACCOUNT_HOME / ".claude/knowledge/council-eval/predictions_resolved.jsonl"
)
DEFAULT_CAPTURE_EVENTS = str(
    ACCOUNT_HOME / ".claude/knowledge/council-eval/capture_resolved.jsonl"
)
DEFAULT_ARTIFACT_ROOT = str(
    ACCOUNT_HOME / ".local/state/council-tools/capture-artifacts"
)
DEFAULT_COORDINATION_LOCK = str(
    ACCOUNT_HOME / ".local/state/council-tools/evidence.lock"
)
DEFAULT_CONTROL_STORE = str(
    ACCOUNT_HOME / ".local/state/council-tools/capture-control"
)
LIVE_KNOWLEDGE_ROOT = (ACCOUNT_HOME / ".claude/knowledge").resolve()
LIVE_RUNTIME_STATE_ROOT = (ACCOUNT_HOME / ".local/state/council-tools").resolve()
LIVE_RUNTIME_SOURCE_ROOT = (ACCOUNT_HOME / "council-tools").resolve()
LIVE_WRITE_ROOTS = (
    LIVE_KNOWLEDGE_ROOT,
    LIVE_RUNTIME_STATE_ROOT,
    LIVE_RUNTIME_SOURCE_ROOT,
)
DEFAULT_AUTHORITY_HOST = "manny"

def _path_candidates(raw_path: str | Path) -> tuple[Path, Path]:
    """Return lexical and symlink-resolved identities for an authorization check."""

    lexical = Path(os.path.abspath(os.fspath(raw_path)))
    try:
        resolved = lexical.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise LedgerError(f"cannot authorize unresolved write path: {lexical}") from exc
    return lexical, resolved


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_live_knowledge_path(raw_path: str | Path) -> bool:
    return any(
        _is_within(candidate, LIVE_KNOWLEDGE_ROOT)
        for candidate in _path_candidates(raw_path)
    )


def _is_live_write_path(raw_path: str | Path) -> bool:
    """Recognize both lexical and resolved containment in every live tree.

    Checking both identities prevents an outside symlink into a live tree from
    bypassing the host gate, while also preventing a symlink placed lexically
    inside a live tree from redirecting a write elsewhere without authority.
    """

    return any(
        _is_within(candidate, root)
        for candidate in _path_candidates(raw_path)
        for root in LIVE_WRITE_ROOTS
    )


def _require_write_authority(*paths: str | Path) -> None:
    live_targets = []
    for raw_path in paths:
        if raw_path is None or not _is_live_write_path(raw_path):
            continue
        live_targets.append(Path(raw_path))
    if not live_targets:
        return
    expected = DEFAULT_AUTHORITY_HOST.split(".")[0]
    actual = socket.gethostname().split(".")[0]
    if actual != expected:
        raise LedgerError(
            f"live council evidence writes are authorized only on {expected}; "
            f"current host is {actual}"
        )


def _require_ledger_write_authority(
    path: str | Path, *additional_paths: str | Path | None
) -> None:
    """Authorize both a JSONL target and its implicit sibling lock."""

    _require_write_authority(
        path,
        derived_ledger_lock_path(path),
        *additional_paths,
    )


def _local_coordination_lock(path: str | Path) -> str:
    lexical, _resolved = _path_candidates(path)
    return str(lexical.with_name(f"{lexical.name}.evidence.lock"))


def _resolve_coordination_lock(args: argparse.Namespace) -> None:
    """Bind omitted coordination locks to the effective store context.

    Default/live stores retain the one live evidence lock. Explicit standalone
    local stores get a stable sibling lock, so off-host rehearsal cannot touch
    live runtime state merely because the option was omitted.
    """

    if not hasattr(args, "coordination_lock") or args.coordination_lock is not None:
        return
    if getattr(args, "check_only", False):
        return

    fields = getattr(args, "_coordination_context_fields", ())
    values = [
        getattr(args, field, None)
        for field in fields
        if getattr(args, field, None) is not None
    ]
    if not values:
        raise LedgerError("cannot derive evidence coordination lock without a store path")
    if any(_is_live_write_path(value) for value in values):
        args.coordination_lock = DEFAULT_COORDINATION_LOCK
        return
    anchor_field = getattr(args, "_coordination_anchor_field", fields[0])
    anchor = getattr(args, anchor_field, None) or values[0]
    args.coordination_lock = _local_coordination_lock(anchor)


def _today(value: str | None) -> date:
    if value is None:
        return datetime.now(ZoneInfo("America/New_York")).date()
    return date.fromisoformat(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_spec(path: str, label: str) -> dict[str, Any]:
    try:
        value = strict_json_loads(Path(path).read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        # Parser diagnostics can contain attacker-controlled duplicate key text.
        # Specs may themselves be the secret-bearing input, so the CLI exposes
        # only a stable parse category and keeps the original exception chained.
        raise LedgerError(f"{label} is not valid strict JSON") from exc
    if not isinstance(value, dict):
        raise LedgerError(f"{label} must be an object")
    return value


def _named_file_bytes(values: list[str], label: str) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for value in values:
        if "=" not in value:
            raise LedgerError(f"{label} must use seat=/absolute/or/relative/path")
        seat, raw_path = value.split("=", 1)
        if not seat or not raw_path:
            raise LedgerError(f"{label} must use seat=path")
        if seat in result:
            raise LedgerError(f"duplicate {label} seat")
        result[seat] = Path(raw_path).read_bytes()
    return result


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _transaction_escrow_paths(*paths: str | Path) -> set[str]:
    inventory = transaction_escrow_inventory(*paths)
    return {str(entry["path"]) for entry in inventory["entries"]}


def _new_transaction_escrows(
    before: set[str], *paths: str | Path
) -> list[str]:
    return sorted(_transaction_escrow_paths(*paths) - before)


def _same_path(left: str | Path, right: str | Path) -> bool:
    left_path = Path(left).resolve()
    right_path = Path(right).resolve()
    if left_path == right_path:
        return True
    try:
        return left_path.samefile(right_path)
    except (FileNotFoundError, OSError):
        return False


def _require_separate_capture_sidecar(log: str | Path, events: str | Path) -> None:
    if _same_path(log, events):
        raise LedgerError("capture ledger and resolution sidecar must be separate files")
    if _same_path(events, DEFAULT_EVENTS):
        raise LedgerError("V2 capture resolutions must not use the V1 resolution sidecar")


def _print_human(result: dict) -> None:
    escrows = result["transactionEscrows"]
    print(
        f"transaction_escrows={escrows['count']} "
        f"transaction_escrow_bytes={escrows['aggregateBytes']}"
    )
    for entry in escrows["entries"]:
        print(
            f"transaction_escrow={entry['path']} bytes={entry['bytes']} "
            f"type={entry['entryType']} store={entry['storePath']}"
        )
    print(
        "councils={councilRows} with_forecasts={councilRowsWithPredictions} "
        "complete={completeForecastRows} attempts={attempts} orphan_attempts={orphans}".format(
            orphans=len(result["orphanAttempts"]), **result
        )
    )
    print(
        "issuances={forecastIssuances} representative={representativeForecasts} "
        "outcomes={uniqueOutcomes} shared={sharedOutcomes} repeated={repeatedIssuances}".format(
            **result
        )
    )
    print(
        "due={eligibleDueOutcomes} resolved={resolvedOutcomes} void={voidOutcomes} "
        "void_rate={voidRateOfEligibleOutcomes} "
        "unresolved={unresolvedDueOutcomes} old_overdue={oldOverdueOutcomes} "
        "debt={gradingDebtState} score={scoreStatus}".format(**result)
    )
    for item in result["seatScores"]:
        brier = "n/a" if item["brier"] is None else f"{item['brier']:.4f}"
        print(
            f"seat={item['seat']} n={item['n']} brier={brier} "
            f"constant_50={item['constantFiftyBrier']} "
            f"in_sample_base_rate={item['inSampleBaseRateBrier']} "
            f"due={item['dueOutcomes']} unresolved={item['unresolvedDueOutcomes']} "
            f"status={item['scoreStatus']}"
        )
    for seat, states in result["seatEmissionStates"].items():
        print(
            f"emission_seat={seat} submitted={states['submitted']} "
            f"abstained={states['abstained']} unavailable={states['unavailable']}"
        )
    if result["legacyIneligiblePredictions"]:
        print(
            f"legacy_ineligible={len(result['legacyIneligiblePredictions'])}",
            file=sys.stderr,
        )
    states = result["allPredictionStates"]
    print(
        "all_prediction_states="
        f"future:{states['future']},due:{states['due']},resolved:{states['resolved']},"
        f"void:{states['void']},legacy_ineligible:{states['legacyIneligible']}"
    )
    for item in result["invalidRecords"]:
        print(f"ERROR: {item}", file=sys.stderr)
    print(result["label"])


def command_report(args: argparse.Namespace) -> int:
    if args.today and (
        _is_live_write_path(args.log) or _is_live_write_path(args.events)
    ):
        raise LedgerError("--today is test-only and cannot be used with live council paths")
    result = (
        audit(args.log, args.events, today=_today(args.today))
        if args.today
        else audit(args.log, args.events, as_of=_now())
    )
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        _print_human(result)
    if result["invalidRecords"]:
        return 1
    if result["gradingDebtState"] == "BLOCK_FINALIZATION":
        return 3
    return 0


def command_record(args: argparse.Namespace) -> int:
    if not args.check_only:
        _require_ledger_write_authority(args.log, args.coordination_lock)
    row = _load_spec(args.row, "record")
    prior = [item for _, item in load_jsonl(args.log)]
    validate_ledger_row(row, prior)
    if not args.check_only:
        append_ledger_row(
            args.log, row, coordination_lock=args.coordination_lock
        )
    print("valid" if args.check_only else "recorded")
    return 0


def command_attempt(args: argparse.Namespace) -> int:
    _require_ledger_write_authority(args.log, args.coordination_lock)
    spec = _load_spec(args.spec, "attempt spec")
    if not isinstance(spec.get("expectedSeats"), list):
        raise LedgerError("attempt spec expectedSeats must be a list")
    ts = args.ts or _now()
    outcome = spec.get("sharedOutcome")
    if not isinstance(outcome, dict):
        raise LedgerError("attempt spec requires sharedOutcome object")
    related_outcome_ids = outcome.get("relatedOutcomeIds") or []
    if not isinstance(related_outcome_ids, list):
        raise LedgerError("sharedOutcome.relatedOutcomeIds must be a list")
    row = make_attempt(
        question=spec.get("question"),
        expected_seats=spec.get("expectedSeats"),
        claim=outcome.get("claim"),
        resolution_date=outcome.get("resolutionDate"),
        resolved_by=outcome.get("resolvedBy"),
        decision_link=outcome.get("decisionLink"),
        materiality=outcome.get("materiality"),
        action_if_true=outcome.get("actionIfTrue"),
        action_if_false=outcome.get("actionIfFalse"),
        evidence_cutoff_at=outcome.get("evidenceCutoffAt") or ts,
        ts=ts,
        related_outcome_ids=related_outcome_ids,
    )
    append_ledger_row(
        args.log, row, coordination_lock=args.coordination_lock
    )
    print(json.dumps({"runId": row["runId"], "outcomeId": row["sharedOutcome"]["outcomeId"]}))
    return 0


def command_complete(args: argparse.Namespace) -> int:
    if not args.check_only:
        _require_ledger_write_authority(args.log, args.coordination_lock)
    spec = _load_spec(args.spec, "completion spec")
    for field in ("councilFields", "seatStates", "probabilities"):
        value = spec.get(field)
        if not isinstance(value, dict):
            raise LedgerError(f"completion spec {field} must be an object")
    rows = [item for _, item in load_jsonl(args.log)]
    run_id = spec.get("runId")
    matches = [
        item
        for item in rows
        if item.get("kind") == "council-attempt" and item.get("runId") == run_id
    ]
    if len(matches) != 1:
        raise LedgerError(f"completion spec must match exactly one attempt: {run_id}")
    row = make_completion(
        attempt=matches[0],
        council_fields=spec["councilFields"],
        seat_states=spec["seatStates"],
        probabilities=spec["probabilities"],
        ts=args.ts or _now(),
    )
    if args.check_only:
        validate_ledger_row(row, rows)
    else:
        append_ledger_row(
            args.log, row, coordination_lock=args.coordination_lock
        )
    print(
        json.dumps(
            {
                "runId": row["runId"],
                "predictions": len(row["predictions"]),
                "status": "valid" if args.check_only else "recorded",
            }
        )
    )
    return 0


def command_resolve(args: argparse.Namespace) -> int:
    _require_ledger_write_authority(args.events, args.coordination_lock)
    came_true = None
    void_reason = args.void_reason
    if args.outcome == "true":
        came_true = True
    elif args.outcome == "false":
        came_true = False
    elif args.outcome != "void":
        raise LedgerError("outcome must be true, false, or void")
    if args.outcome == "void" and not void_reason:
        raise LedgerError("void outcome requires --void-reason")
    if args.outcome != "void" and void_reason:
        raise LedgerError("--void-reason is valid only with outcome=void")
    current = audit(args.log, args.events)
    if current["invalidRecords"]:
        raise LedgerError("cannot resolve while the forecast ledger is invalid")
    if args.outcome_id not in current["knownOutcomeIds"]:
        raise LedgerError(f"unknown council outcomeId: {args.outcome_id}")
    fingerprint = current["outcomeFingerprints"].get(args.outcome_id)
    if fingerprint is None:
        raise LedgerError(
            "cannot create a new resolution for an outcome without an issued fingerprint"
        )
    event = append_resolution(
        args.events,
        outcome_id=args.outcome_id,
        resolution_date=current["outcomeResolutionDates"][args.outcome_id],
        outcome_fingerprint=fingerprint,
        came_true=came_true,
        evidence=args.evidence,
        resolver=args.resolver,
        resolved_at=_now(),
        method=args.method,
        reviewer=args.reviewer,
        void_reason=void_reason,
        supersedes_resolution_id=args.supersedes,
        coordination_lock=args.coordination_lock,
    )
    print(event["resolutionId"])
    return 0


def command_override(args: argparse.Namespace) -> int:
    _require_ledger_write_authority(args.events, args.coordination_lock)
    event = append_override(
        args.events,
        reason=args.reason,
        operator=args.operator,
        created_at=args.created_at or _now(),
        expires_date=args.expires,
        coordination_lock=args.coordination_lock,
    )
    print(event["overrideId"])
    return 0


def command_repair_tail(args: argparse.Namespace) -> int:
    _require_ledger_write_authority(
        args.path, args.backup_dir, args.coordination_lock
    )
    result = repair_trailing_jsonl(
        args.path,
        expected_line=args.confirm_final_line,
        backup_dir=args.backup_dir,
        coordination_lock=args.coordination_lock,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def command_capture_artifact(args: argparse.Namespace) -> int:
    run_fields = {
        "run-id": args.run_id,
        "log": args.log,
        "operator": args.operator,
        "evidence-ref": args.evidence_ref,
    }
    supplied_run_fields = {name for name, value in run_fields.items() if value}
    if args.control_artifact:
        if supplied_run_fields:
            raise LedgerError(
                "--control-artifact cannot be combined with run incident fields"
            )
    elif supplied_run_fields != set(run_fields):
        raise LedgerError(
            "capture-artifact requires --run-id, --log, --operator, and "
            "--evidence-ref, or explicit --control-artifact"
        )
    write_paths = [args.artifact_root, args.coordination_lock]
    if not args.control_artifact:
        write_paths.extend((args.log, derived_ledger_lock_path(args.log)))
    _require_write_authority(*write_paths)
    data = Path(args.file).read_bytes()
    secret_tokens = [Path(path).read_bytes() for path in args.secret_token_file]
    store = ArtifactStore(args.artifact_root)
    with evidence_write_lock(args.coordination_lock):
        try:
            if not args.control_artifact:
                _raw, validated_rows = validate_capture_ledger(args.log, now=_now())
                initiations = [
                    row
                    for row in validated_rows
                    if row.get("kind") == "capture-initiation"
                    and row.get("runId") == args.run_id
                ]
                if len(initiations) != 1:
                    raise LedgerError(
                        "run-bound artifact capture requires exactly one prior "
                        f"capture-initiation for runId: {args.run_id}"
                    )
            ref = store.capture(data, secret_tokens=secret_tokens)
        except SecretDetectedError:
            if not args.control_artifact:
                # The outer coordination lock is still held.  Passing None
                # avoids reacquiring the same flock while the append retains
                # its ordinary per-ledger serialization.
                append_capture_invalidation(
                    args.log,
                    {
                        "runId": args.run_id,
                        "reason": "secret-detected",
                        "operator": args.operator,
                        "evidenceRef": args.evidence_ref,
                    },
                    coordination_lock=None,
                )
            raise
    print(json.dumps(ref, sort_keys=True))
    return 0


def command_capture_activate(args: argparse.Namespace) -> int:
    _require_ledger_write_authority(args.log, args.coordination_lock)
    spec = _load_spec(args.spec, "capture activation spec")
    live = _is_live_write_path(args.log)
    if live:
        installed_commit = getattr(args, "_runtime_source_commit", None)
        installed_source_sha256 = getattr(args, "_runtime_source_sha256", None)
        installed_root = getattr(args, "_runtime_source_root", None)
        if not installed_commit or not installed_source_sha256 or not installed_root:
            raise LedgerError(
                "live capture activation must run through the installed source-pinned wrapper"
            )
        source_root = Path(installed_root)
        expected_cli = source_root / "src/council_tools/cli.py"
        if (
            not source_root.is_absolute()
            or expected_cli.resolve() != Path(__file__).resolve()
        ):
            raise LedgerError(
                "live capture activation runtime source root does not match loaded code"
            )
        if spec.get("runtimeSourceCommit") != installed_commit:
            raise LedgerError(
                "capture activation runtimeSourceCommit does not match installed runtime pin"
            )
        if spec.get("runtimeSourceSha256") != installed_source_sha256:
            raise LedgerError(
                "capture activation runtimeSourceSha256 does not match installed runtime pin"
            )
        if not args.approval_manifest_file or not args.artifact_root:
            raise LedgerError(
                "live capture activation requires --approval-manifest-file and --artifact-root"
            )
        expected_commit = installed_commit
        expected_source_sha256 = installed_source_sha256
    elif args.approval_manifest_file or args.artifact_root:
        if not args.approval_manifest_file or not args.artifact_root:
            raise LedgerError(
                "manifest verification requires both --approval-manifest-file and --artifact-root"
            )
        expected_commit = spec.get("runtimeSourceCommit")
        expected_source_sha256 = spec.get("runtimeSourceSha256")
    escrows_before = _transaction_escrow_paths(args.log)
    if args.approval_manifest_file and args.artifact_root:
        manifest_data = Path(args.approval_manifest_file).read_bytes()
        row, evidence = append_evidence_bound_capture_activation(
            args.log,
            spec,
            manifest_data=manifest_data,
            artifact_store=ArtifactStore(args.artifact_root),
            expected_runtime_commit=expected_commit,
            expected_source_sha256=expected_source_sha256,
            coordination_lock=args.coordination_lock,
        )
    else:
        row = append_capture_activation(
            args.log,
            spec,
            coordination_lock=args.coordination_lock,
        )
        evidence = None
    print(
        json.dumps(
            {
                "activationId": row["activationId"],
                "activationEvidence": evidence,
                "transactionEscrows": _new_transaction_escrows(
                    escrows_before, args.log
                ),
            },
            sort_keys=True,
        )
    )
    return 0


def command_activation_readiness(args: argparse.Namespace) -> int:
    expected_commit = (
        args.runtime_source_commit
        or getattr(args, "_runtime_source_commit", None)
    )
    expected_source_sha256 = (
        args.runtime_source_sha256
        or getattr(args, "_runtime_source_sha256", None)
    )
    if not expected_commit or not expected_source_sha256:
        raise LedgerError(
            "activation readiness requires authenticated or explicit runtime source bindings"
        )
    evaluated_at = args.at or _now()
    result = evaluate_activation_evidence(
        Path(args.manifest_file).read_bytes(),
        reader=ArtifactStore(args.artifact_root),
        expected_runtime_commit=expected_commit,
        expected_source_sha256=expected_source_sha256,
        activation_time=evaluated_at,
        as_of=evaluated_at,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("appendReady") is True else 1


def command_capture_initiate(args: argparse.Namespace) -> int:
    _require_ledger_write_authority(args.log, args.coordination_lock)
    escrows_before = _transaction_escrow_paths(args.log)
    row, recorded = append_capture_initiation(
        args.log,
        {"activationId": args.activation_id, "idempotencyKey": args.idempotency_key},
        coordination_lock=args.coordination_lock,
    )
    print(
        json.dumps(
            {
                "initiationId": row["initiationId"],
                "runId": row["runId"],
                "status": "recorded" if recorded else "idempotent-replay",
                "transactionEscrows": _new_transaction_escrows(
                    escrows_before, args.log
                ),
            },
            sort_keys=True,
        )
    )
    return 0


def command_capture_attempt(args: argparse.Namespace) -> int:
    _require_ledger_write_authority(args.log, args.coordination_lock)
    spec = _load_spec(args.spec, "capture attempt spec")
    seat_inputs = spec.pop("seatInputArtifacts", None)
    if not isinstance(seat_inputs, dict):
        raise LedgerError("capture attempt spec requires seatInputArtifacts object")
    escrows_before = _transaction_escrow_paths(args.log)
    row = append_council_attempt_v2(
        args.log,
        spec,
        artifact_store=ArtifactStore(args.artifact_root),
        decision_before_bytes=Path(args.decision_before_file).read_bytes(),
        seat_input_artifacts=seat_inputs,
        visible_inputs=_named_file_bytes(args.visible_input, "visible input"),
        coordination_lock=args.coordination_lock,
    )
    print(
        json.dumps(
            {
                "runId": row["runId"],
                "outcomeId": row["sharedOutcome"]["outcomeId"],
                "transactionEscrows": _new_transaction_escrows(
                    escrows_before, args.log
                ),
            },
            sort_keys=True,
        )
    )
    return 0


def command_capture_seats_finished(args: argparse.Namespace) -> int:
    _require_ledger_write_authority(args.log, args.coordination_lock)
    escrows_before = _transaction_escrow_paths(args.log)
    row = append_council_seats_finished(
        args.log,
        _load_spec(args.spec, "capture seats-finished spec"),
        coordination_lock=args.coordination_lock,
    )
    print(
        json.dumps(
            {
                "runId": row["runId"],
                "status": "recorded",
                "transactionEscrows": _new_transaction_escrows(
                    escrows_before, args.log
                ),
            },
            sort_keys=True,
        )
    )
    return 0


def command_capture_complete(args: argparse.Namespace) -> int:
    _require_ledger_write_authority(args.log, args.coordination_lock)
    spec = _load_spec(args.spec, "capture completion spec")
    seat_inputs = spec.pop("seatInputArtifacts", None)
    if not isinstance(seat_inputs, dict):
        raise LedgerError("capture completion spec requires seatInputArtifacts object")
    escrows_before = _transaction_escrow_paths(args.log)
    row, summary = append_council_v2(
        args.log,
        spec,
        artifact_store=ArtifactStore(args.artifact_root),
        decision_before_bytes=Path(args.decision_before_file).read_bytes(),
        seat_input_artifacts=seat_inputs,
        visible_inputs=_named_file_bytes(args.visible_input, "visible input"),
        visible_outputs=_named_file_bytes(args.visible_output, "visible output"),
        coordination_lock=args.coordination_lock,
    )
    print(
        json.dumps(
            {
                "runId": row["runId"],
                "predictions": len(row["predictions"]),
                "findingSummary": summary,
                "status": "recorded",
                "transactionEscrows": _new_transaction_escrows(
                    escrows_before, args.log
                ),
            },
            sort_keys=True,
        )
    )
    return 0


def command_capture_invalidate(args: argparse.Namespace) -> int:
    _require_ledger_write_authority(args.log, args.coordination_lock)
    escrows_before = _transaction_escrow_paths(args.log)
    row = append_capture_invalidation(
        args.log,
        _load_spec(args.spec, "capture invalidation spec"),
        coordination_lock=args.coordination_lock,
    )
    print(
        json.dumps(
            {
                "invalidationId": row["invalidationId"],
                "transactionEscrows": _new_transaction_escrows(
                    escrows_before, args.log
                ),
            },
            sort_keys=True,
        )
    )
    return 0


def command_capture_resolve(args: argparse.Namespace) -> int:
    _require_separate_capture_sidecar(args.log, args.events)
    _require_ledger_write_authority(
        args.events,
        args.coordination_lock,
        args.log,
        derived_ledger_lock_path(args.log),
    )
    came_true = None if args.outcome == "void" else args.outcome == "true"
    if args.outcome == "void" and not args.void_reason:
        raise LedgerError("void outcome requires --void-reason")
    if args.outcome != "void" and args.void_reason:
        raise LedgerError("--void-reason is valid only with outcome=void")
    escrows_before = _transaction_escrow_paths(args.events)
    event = append_capture_resolution(
        args.log,
        args.events,
        outcome_id=args.outcome_id,
        came_true=came_true,
        evidence=args.evidence,
        resolver=args.resolver,
        resolved_at=_now(),
        method=args.method,
        reviewer=args.reviewer,
        void_reason=args.void_reason,
        supersedes_resolution_id=args.supersedes,
        coordination_lock=args.coordination_lock,
    )
    print(
        json.dumps(
            {
                "resolutionId": event["resolutionId"],
                "transactionEscrows": _new_transaction_escrows(
                    escrows_before, args.events
                ),
            },
            sort_keys=True,
        )
    )
    return 0


def command_capture_report(args: argparse.Namespace) -> int:
    _require_separate_capture_sidecar(args.log, args.events)
    if args.as_of and (
        _is_live_write_path(args.log) or _is_live_write_path(args.events)
    ):
        raise LedgerError("--as-of is test-only and cannot be used with live council paths")
    report = capture_report(
        args.log,
        args.events,
        artifact_store=ArtifactStore(args.artifact_root),
        as_of=args.as_of or _now(),
    )
    safe = _json_safe(report)
    if args.json:
        print(json.dumps(safe, sort_keys=True, allow_nan=False))
    else:
        cohort = safe["cohort"]
        timing = safe["timing"]
        accuracy = safe["descriptiveForecastAccuracy"]
        print(
            f"capture={cohort['completeInitiationCount']}/{cohort['eligibleInitiationCount']} "
            f"fraction={cohort['captureFraction']} filled={cohort['cohortFilled']} "
            f"outcome={cohort['sharedOperationalOutcome']}"
        )
        print(
            f"median_active_seconds={timing['medianActiveHandlingSeconds']} "
            f"artifact_failures={safe['artifacts']['artifactIntegrityFailureCount']} "
            f"resolved_exogenous_predictions={accuracy['predictionCount']} "
            f"mean_brier={accuracy['meanBrier']}"
        )
        readiness = safe["activationReadiness"]
        blockers = ",".join(readiness["blockingReasons"]) or "none"
        print(
            f"activation={readiness['status']} blockers={blockers} "
            f"prospective_audit={safe['prospectiveAudit']['status']} "
            f"durability={safe['durability']['status']}"
        )
        escrows = safe["transactionEscrows"]
        print(
            f"transaction_escrows={escrows['count']} "
            f"transaction_escrow_bytes={escrows['aggregateBytes']}"
        )
        for entry in escrows["entries"]:
            print(
                f"transaction_escrow={entry['path']} bytes={entry['bytes']} "
                f"type={entry['entryType']} store={entry['storePath']}"
            )
        print("DESCRIPTIVE CAPTURE ONLY - NO SEAT COMPARISON VERDICT")
    return 0


def command_evidence_snapshot(args: argparse.Namespace) -> int:
    _require_write_authority(args.target, args.coordination_lock)
    result = create_evidence_snapshot(
        ledger_path=args.log,
        resolution_store_path=args.events,
        control_store_path=args.control_store,
        artifact_root=args.artifact_root,
        lock_path=args.coordination_lock,
        snapshot_target=args.target,
        repository_root=args.repository_root,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def command_evidence_verify(args: argparse.Namespace) -> int:
    print(json.dumps(verify_evidence_snapshot(args.snapshot), sort_keys=True))
    return 0


def command_evidence_restore(args: argparse.Namespace) -> int:
    _require_write_authority(args.target)
    print(
        json.dumps(
            restore_evidence_snapshot(
                args.snapshot,
                args.target,
                repository_root=args.repository_root,
            ),
            sort_keys=True,
        )
    )
    return 0


def command_prepare_brief(args: argparse.Namespace) -> int:
    result = prepare_blind_brief(
        run_id=args.run_id,
        source_path=args.source,
        destination_path=args.destination,
        expected_sha256=args.expected_sha256,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def command_recover_brief(args: argparse.Namespace) -> int:
    with Path(args.spec).open(encoding="utf-8") as handle:
        spec = json.load(handle)
    result = recover_blind_brief(
        spec,
        operator_confirmed=args.confirm_operator_approved_rewrite,
        rehearsal_root=args.rehearsal_root,
        resume=args.resume,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def command_plan_brief_recovery(args: argparse.Namespace) -> int:
    spec = plan_blind_brief_recovery(
        ledger_path=args.ledger,
        target_line=args.target_line,
        replacement_source=args.replacement_source,
        destination_path=args.destination,
        artifact_dir=args.artifact_dir,
        operator=args.operator,
        approval_reference=args.approval_reference,
        approval_reason=args.approval_reason,
        approved_at=args.approved_at,
        rehearsal_root=args.rehearsal_root,
    )
    print(json.dumps(spec, indent=2, sort_keys=False))
    return 0


def _recording_timestamp(value: str) -> datetime:
    """argparse converter, so a malformed window is a usage error (exit 2)."""

    try:
        return parse_timestamp(value, field="window bound")
    except RecordingCoverageError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def command_recording_coverage(args: argparse.Namespace) -> int:
    try:
        result = report_recording_coverage(
            log_path=args.log, since=args.since, until=args.until
        )
    except RecordingCoverageError as exc:
        # Makes "there is no exit 1" hold by construction rather than by the
        # duplicated precondition above happening to agree with the module's.
        print(str(exc), file=sys.stderr)
        return 2
    rendered = json.dumps(result, sort_keys=True) if args.json else (
        format_recording_coverage(result)
    )
    print(rendered, file=sys.stdout if result["determined"] else sys.stderr)
    return recording_exit_code(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def coordinated(
        command: argparse.ArgumentParser,
        *,
        anchor_field: str,
        context_fields: tuple[str, ...],
    ) -> None:
        command.add_argument("--coordination-lock")
        command.set_defaults(
            _coordination_anchor_field=anchor_field,
            _coordination_context_fields=context_fields,
        )

    report = sub.add_parser("report")
    report.add_argument("--log", default=DEFAULT_LOG)
    report.add_argument("--events", default=DEFAULT_EVENTS)
    report.add_argument("--today")
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=command_report)

    recording = sub.add_parser(
        "recording-coverage",
        help="report how councils record what they reviewed, from the ledger alone",
        epilog=(
            "Reads the ledger only; it never invokes git, never classifies a commit, "
            "and never reports that a change went unreviewed. Exit 0 the report ran "
            "and can be trusted; 3 it cannot. There is deliberately no exit 1: low "
            "adoption is the finding, not an error state."
        ),
    )
    recording.add_argument(
        "--log", default=DEFAULT_LOG, help="council ledger to read (default: the live ledger)"
    )
    recording.add_argument(
        "--since",
        type=_recording_timestamp,
        help="only council rows at or after this instant; ISO-8601 or a bare date read as UTC midnight",
    )
    recording.add_argument(
        "--until", type=_recording_timestamp, help="only council rows strictly before this instant"
    )
    recording.add_argument(
        "--json", action="store_true", help="emit the full result as JSON"
    )
    recording.set_defaults(func=command_recording_coverage)

    record = sub.add_parser("record")
    record.add_argument("--log", default=DEFAULT_LOG)
    record.add_argument("--row", required=True)
    record.add_argument("--check-only", action="store_true")
    coordinated(record, anchor_field="log", context_fields=("log",))
    record.set_defaults(func=command_record)

    attempt = sub.add_parser("attempt")
    attempt.add_argument("--log", default=DEFAULT_LOG)
    attempt.add_argument("--spec", required=True)
    attempt.add_argument("--ts")
    coordinated(attempt, anchor_field="log", context_fields=("log",))
    attempt.set_defaults(func=command_attempt)

    complete = sub.add_parser("complete")
    complete.add_argument("--log", default=DEFAULT_LOG)
    complete.add_argument("--spec", required=True)
    complete.add_argument("--ts")
    complete.add_argument("--check-only", action="store_true")
    coordinated(complete, anchor_field="log", context_fields=("log",))
    complete.set_defaults(func=command_complete)

    resolve = sub.add_parser("resolve")
    resolve.add_argument("outcome_id")
    resolve.add_argument("outcome", choices=("true", "false", "void"))
    resolve.add_argument("--log", default=DEFAULT_LOG)
    resolve.add_argument("--events", default=DEFAULT_EVENTS)
    resolve.add_argument("--evidence", required=True)
    resolve.add_argument("--resolver", required=True)
    resolve.add_argument(
        "--method", choices=("deterministic", "manual-reviewed"), required=True
    )
    resolve.add_argument("--reviewer")
    resolve.add_argument("--void-reason")
    resolve.add_argument("--supersedes")
    coordinated(
        resolve,
        anchor_field="log",
        context_fields=("log", "events"),
    )
    resolve.set_defaults(func=command_resolve)

    override = sub.add_parser("override-debt")
    override.add_argument("--events", default=DEFAULT_EVENTS)
    override.add_argument("--reason", required=True)
    override.add_argument("--operator", required=True)
    override.add_argument("--created-at")
    override.add_argument("--expires", required=True)
    coordinated(override, anchor_field="events", context_fields=("events",))
    override.set_defaults(func=command_override)

    repair = sub.add_parser(
        "repair-tail",
        help="quarantine and remove one explicitly confirmed corrupt final JSONL line",
    )
    repair.add_argument("--path", required=True)
    repair.add_argument("--confirm-final-line", required=True, type=int)
    repair.add_argument("--backup-dir", required=True)
    coordinated(repair, anchor_field="path", context_fields=("path",))
    repair.set_defaults(func=command_repair_tail)

    artifact = sub.add_parser("capture-artifact")
    artifact.add_argument("--file", required=True)
    artifact.add_argument("--artifact-root", default=DEFAULT_ARTIFACT_ROOT)
    artifact.add_argument("--secret-token-file", action="append", default=[])
    artifact.add_argument("--run-id")
    artifact.add_argument("--log")
    artifact.add_argument("--operator")
    artifact.add_argument("--evidence-ref")
    artifact.add_argument("--control-artifact", action="store_true")
    coordinated(
        artifact,
        anchor_field="log",
        context_fields=("log", "artifact_root"),
    )
    artifact.set_defaults(func=command_capture_artifact)

    activate = sub.add_parser("capture-activate")
    activate.add_argument("--log", default=DEFAULT_LOG)
    activate.add_argument("--spec", required=True)
    activate.add_argument("--approval-manifest-file")
    activate.add_argument("--artifact-root")
    coordinated(activate, anchor_field="log", context_fields=("log",))
    activate.set_defaults(func=command_capture_activate)

    readiness = sub.add_parser("activation-readiness")
    readiness.add_argument("--manifest-file", required=True)
    readiness.add_argument("--artifact-root", required=True)
    readiness.add_argument("--runtime-source-commit")
    readiness.add_argument("--runtime-source-sha256")
    readiness.add_argument("--at")
    readiness.set_defaults(func=command_activation_readiness)

    initiate = sub.add_parser("capture-initiate")
    initiate.add_argument("--log", default=DEFAULT_LOG)
    initiate.add_argument("--activation-id", required=True)
    initiate.add_argument("--idempotency-key", required=True)
    coordinated(initiate, anchor_field="log", context_fields=("log",))
    initiate.set_defaults(func=command_capture_initiate)

    capture_attempt = sub.add_parser("capture-attempt")
    capture_attempt.add_argument("--log", default=DEFAULT_LOG)
    capture_attempt.add_argument("--artifact-root", default=DEFAULT_ARTIFACT_ROOT)
    capture_attempt.add_argument("--spec", required=True)
    capture_attempt.add_argument("--decision-before-file", required=True)
    capture_attempt.add_argument("--visible-input", action="append", default=[])
    coordinated(capture_attempt, anchor_field="log", context_fields=("log",))
    capture_attempt.set_defaults(func=command_capture_attempt)

    seats_finished = sub.add_parser("capture-seats-finished")
    seats_finished.add_argument("--log", default=DEFAULT_LOG)
    seats_finished.add_argument("--spec", required=True)
    coordinated(seats_finished, anchor_field="log", context_fields=("log",))
    seats_finished.set_defaults(func=command_capture_seats_finished)

    capture_complete = sub.add_parser("capture-complete")
    capture_complete.add_argument("--log", default=DEFAULT_LOG)
    capture_complete.add_argument("--artifact-root", default=DEFAULT_ARTIFACT_ROOT)
    capture_complete.add_argument("--spec", required=True)
    capture_complete.add_argument("--decision-before-file", required=True)
    capture_complete.add_argument("--visible-input", action="append", default=[])
    capture_complete.add_argument("--visible-output", action="append", default=[])
    coordinated(capture_complete, anchor_field="log", context_fields=("log",))
    capture_complete.set_defaults(func=command_capture_complete)

    invalidate = sub.add_parser("capture-invalidate")
    invalidate.add_argument("--log", default=DEFAULT_LOG)
    invalidate.add_argument("--spec", required=True)
    coordinated(invalidate, anchor_field="log", context_fields=("log",))
    invalidate.set_defaults(func=command_capture_invalidate)

    capture_resolve = sub.add_parser("capture-resolve")
    capture_resolve.add_argument("outcome_id")
    capture_resolve.add_argument("outcome", choices=("true", "false", "void"))
    capture_resolve.add_argument("--log", default=DEFAULT_LOG)
    capture_resolve.add_argument("--events", default=DEFAULT_CAPTURE_EVENTS)
    capture_resolve.add_argument("--evidence", required=True)
    capture_resolve.add_argument("--resolver", required=True)
    capture_resolve.add_argument(
        "--method", choices=("deterministic", "manual-reviewed"), required=True
    )
    capture_resolve.add_argument("--reviewer")
    capture_resolve.add_argument("--void-reason")
    capture_resolve.add_argument("--supersedes")
    coordinated(
        capture_resolve,
        anchor_field="log",
        context_fields=("log", "events"),
    )
    capture_resolve.set_defaults(func=command_capture_resolve)

    capture_health = sub.add_parser("capture-report")
    capture_health.add_argument("--log", default=DEFAULT_LOG)
    capture_health.add_argument("--events", default=DEFAULT_CAPTURE_EVENTS)
    capture_health.add_argument("--artifact-root", default=DEFAULT_ARTIFACT_ROOT)
    capture_health.add_argument("--as-of")
    capture_health.add_argument("--json", action="store_true")
    capture_health.set_defaults(func=command_capture_report)

    snapshot = sub.add_parser("evidence-snapshot")
    snapshot.add_argument("--log", default=DEFAULT_LOG)
    snapshot.add_argument("--events", default=DEFAULT_CAPTURE_EVENTS)
    snapshot.add_argument("--control-store", default=DEFAULT_CONTROL_STORE)
    snapshot.add_argument("--artifact-root", default=DEFAULT_ARTIFACT_ROOT)
    snapshot.add_argument("--target", required=True)
    snapshot.add_argument("--repository-root", required=True)
    coordinated(
        snapshot,
        anchor_field="log",
        context_fields=("log", "events", "control_store", "artifact_root"),
    )
    snapshot.set_defaults(func=command_evidence_snapshot)

    verify = sub.add_parser("evidence-verify")
    verify.add_argument("snapshot")
    verify.set_defaults(func=command_evidence_verify)

    restore = sub.add_parser("evidence-restore")
    restore.add_argument("snapshot")
    restore.add_argument("target")
    restore.add_argument("--repository-root", required=True)
    restore.set_defaults(func=command_evidence_restore)
    prepare_brief = sub.add_parser(
        "prepare-brief",
        help="exclusively create one immutable run-scoped blind brief",
    )
    prepare_brief.add_argument("--run-id", required=True)
    prepare_brief.add_argument("--source", required=True)
    prepare_brief.add_argument("--destination", required=True)
    prepare_brief.add_argument("--expected-sha256", required=True)
    prepare_brief.set_defaults(func=command_prepare_brief)

    recover_brief = sub.add_parser(
        "recover-brief",
        help="operator-approved hash-pinned recovery of one valid council brief field",
    )
    recover_brief.add_argument("--spec", required=True)
    recover_brief.add_argument(
        "--confirm-operator-approved-rewrite", action="store_true", required=True
    )
    recover_brief.add_argument("--resume", action="store_true")
    recover_brief.add_argument(
        "--rehearsal-root",
        help="test-only mirror root; all absolute spec paths are mapped beneath it",
    )
    recover_brief.set_defaults(func=command_recover_brief)

    plan_brief = sub.add_parser(
        "plan-brief-recovery",
        help="derive a recovery spec from the live ledger bytes",
    )
    plan_brief.add_argument("--ledger", required=True)
    plan_brief.add_argument("--target-line", required=True, type=int)
    plan_brief.add_argument("--replacement-source", required=True)
    plan_brief.add_argument("--destination")
    plan_brief.add_argument("--artifact-dir")
    plan_brief.add_argument("--operator", required=True)
    plan_brief.add_argument("--approval-reference", required=True)
    plan_brief.add_argument("--approval-reason", required=True)
    plan_brief.add_argument("--approved-at")
    plan_brief.add_argument(
        "--rehearsal-root",
        help="test-only mirror root; all absolute paths are mapped beneath it",
    )
    plan_brief.set_defaults(func=command_plan_brief_recovery)
    return parser


def main(
    *,
    runtime_source_commit: str | None = None,
    runtime_source_sha256: str | None = None,
    runtime_source_root: str | Path | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args()
    args._runtime_source_commit = runtime_source_commit
    args._runtime_source_sha256 = runtime_source_sha256
    args._runtime_source_root = runtime_source_root
    try:
        _resolve_coordination_lock(args)
        return args.func(args)
    except RecursionError:
        # Last-resort public boundary for any future JSON-bearing command that
        # has not yet routed through strict_json_loads. Never emit interpreter
        # depth diagnostics or a traceback for caller-controlled nesting.
        print("command rejected invalid JSON", file=sys.stderr)
        return 1
    except TypeError:
        # JSON can decode arrays/objects where closed scalar fields are
        # expected. A missed inner guard must still terminate at a stable,
        # non-reflective public category rather than emit a traceback or the
        # caller-controlled value's repr.
        print("command rejected invalid value type", file=sys.stderr)
        return 1
    except (LedgerError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
