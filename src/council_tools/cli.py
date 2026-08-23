"""Command-line interface for council forecast records and reports."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .forecasts import (
    LedgerError,
    append_ledger_row,
    append_override,
    append_resolution,
    audit,
    load_jsonl,
    make_attempt,
    make_completion,
    repair_trailing_jsonl,
    validate_ledger_row,
)


DEFAULT_LOG = os.path.expanduser("~/.claude/knowledge/futures-panel-log.jsonl")
DEFAULT_EVENTS = os.path.expanduser(
    "~/.claude/knowledge/council-eval/predictions_resolved.jsonl"
)
LIVE_KNOWLEDGE_ROOT = Path(os.path.expanduser("~/.claude/knowledge")).resolve()
DEFAULT_AUTHORITY_HOST = "manny"


def _is_live_knowledge_path(raw_path: str | Path) -> bool:
    path = Path(raw_path).resolve()
    try:
        path.relative_to(LIVE_KNOWLEDGE_ROOT)
    except ValueError:
        return False
    return True


def _require_write_authority(*paths: str | Path) -> None:
    live_targets = []
    for raw_path in paths:
        path = Path(raw_path).resolve()
        if not _is_live_knowledge_path(path):
            continue
        live_targets.append(path)
    if not live_targets:
        return
    expected = os.environ.get(
        "COUNCIL_LEDGER_AUTHORITY_HOST", DEFAULT_AUTHORITY_HOST
    ).split(".")[0]
    actual = socket.gethostname().split(".")[0]
    if actual != expected:
        raise LedgerError(
            f"live council ledger writes are authorized only on {expected}; "
            f"current host is {actual}"
        )


def _today(value: str | None) -> date:
    if value is None:
        return datetime.now(ZoneInfo("America/New_York")).date()
    return date.fromisoformat(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _print_human(result: dict) -> None:
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
        "unresolved={unresolvedDueOutcomes} old_overdue={oldOverdueOutcomes} "
        "debt={gradingDebtState} score={scoreStatus}".format(**result)
    )
    for item in result["seatScores"]:
        brier = "n/a" if item["brier"] is None else f"{item['brier']:.4f}"
        print(
            f"seat={item['seat']} n={item['n']} brier={brier} "
            f"constant_50={item['constantFiftyBrier']} "
            f"climatology={item['climatologyBrier']} "
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
        _is_live_knowledge_path(args.log) or _is_live_knowledge_path(args.events)
    ):
        raise LedgerError("--today is test-only and cannot be used with live council paths")
    result = audit(args.log, args.events, today=_today(args.today))
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
    _require_write_authority(args.log)
    with Path(args.row).open(encoding="utf-8") as handle:
        row = json.load(handle)
    prior = [item for _, item in load_jsonl(args.log)]
    validate_ledger_row(row, prior)
    if not args.check_only:
        append_ledger_row(args.log, row)
    print("valid" if args.check_only else "recorded")
    return 0


def command_attempt(args: argparse.Namespace) -> int:
    _require_write_authority(args.log)
    with Path(args.spec).open(encoding="utf-8") as handle:
        spec = json.load(handle)
    ts = args.ts or _now()
    outcome = spec.get("sharedOutcome")
    if not isinstance(outcome, dict):
        raise LedgerError("attempt spec requires sharedOutcome object")
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
    )
    append_ledger_row(args.log, row)
    print(json.dumps({"runId": row["runId"], "outcomeId": row["sharedOutcome"]["outcomeId"]}))
    return 0


def command_complete(args: argparse.Namespace) -> int:
    _require_write_authority(args.log)
    with Path(args.spec).open(encoding="utf-8") as handle:
        spec = json.load(handle)
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
        council_fields=spec.get("councilFields") or {},
        seat_states=spec.get("seatStates") or {},
        probabilities=spec.get("probabilities") or {},
        ts=args.ts or _now(),
    )
    if args.check_only:
        validate_ledger_row(row, rows)
    else:
        append_ledger_row(args.log, row)
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
    _require_write_authority(args.events)
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
    event = append_resolution(
        args.events,
        outcome_id=args.outcome_id,
        resolution_date=current["outcomeResolutionDates"][args.outcome_id],
        came_true=came_true,
        evidence=args.evidence,
        resolver=args.resolver,
        resolved_at=args.resolved_at or _now(),
        method=args.method,
        reviewer=args.reviewer,
        void_reason=void_reason,
        supersedes_resolution_id=args.supersedes,
    )
    print(event["resolutionId"])
    return 0


def command_override(args: argparse.Namespace) -> int:
    _require_write_authority(args.events)
    event = append_override(
        args.events,
        reason=args.reason,
        operator=args.operator,
        created_at=args.created_at or _now(),
        expires_date=args.expires,
    )
    print(event["overrideId"])
    return 0


def command_repair_tail(args: argparse.Namespace) -> int:
    _require_write_authority(args.path)
    result = repair_trailing_jsonl(
        args.path,
        expected_line=args.confirm_final_line,
        backup_dir=args.backup_dir,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser("report")
    report.add_argument("--log", default=DEFAULT_LOG)
    report.add_argument("--events", default=DEFAULT_EVENTS)
    report.add_argument("--today")
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=command_report)

    record = sub.add_parser("record")
    record.add_argument("--log", default=DEFAULT_LOG)
    record.add_argument("--row", required=True)
    record.add_argument("--check-only", action="store_true")
    record.set_defaults(func=command_record)

    attempt = sub.add_parser("attempt")
    attempt.add_argument("--log", default=DEFAULT_LOG)
    attempt.add_argument("--spec", required=True)
    attempt.add_argument("--ts")
    attempt.set_defaults(func=command_attempt)

    complete = sub.add_parser("complete")
    complete.add_argument("--log", default=DEFAULT_LOG)
    complete.add_argument("--spec", required=True)
    complete.add_argument("--ts")
    complete.add_argument("--check-only", action="store_true")
    complete.set_defaults(func=command_complete)

    resolve = sub.add_parser("resolve")
    resolve.add_argument("outcome_id")
    resolve.add_argument("outcome", choices=("true", "false", "void"))
    resolve.add_argument("--log", default=DEFAULT_LOG)
    resolve.add_argument("--events", default=DEFAULT_EVENTS)
    resolve.add_argument("--evidence", required=True)
    resolve.add_argument("--resolver", required=True)
    resolve.add_argument("--resolved-at")
    resolve.add_argument(
        "--method", choices=("deterministic", "manual-reviewed"), required=True
    )
    resolve.add_argument("--reviewer")
    resolve.add_argument("--void-reason")
    resolve.add_argument("--supersedes")
    resolve.set_defaults(func=command_resolve)

    override = sub.add_parser("override-debt")
    override.add_argument("--events", default=DEFAULT_EVENTS)
    override.add_argument("--reason", required=True)
    override.add_argument("--operator", required=True)
    override.add_argument("--created-at")
    override.add_argument("--expires", required=True)
    override.set_defaults(func=command_override)

    repair = sub.add_parser(
        "repair-tail",
        help="quarantine and remove one explicitly confirmed corrupt final JSONL line",
    )
    repair.add_argument("--path", required=True)
    repair.add_argument("--confirm-final-line", required=True, type=int)
    repair.add_argument("--backup-dir", required=True)
    repair.set_defaults(func=command_repair_tail)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (LedgerError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
