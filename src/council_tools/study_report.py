"""Separate study results with cumulative operational obligations and counters."""

from __future__ import annotations

import fcntl
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import ArtifactStore
from .capture_runtime import capture_report
from .capture_schema import strict_json_loads
from .forecasts import audit
from .study_routes import resolve_study_route


class StudyReportError(ValueError):
    """The cumulative report cannot verify its retained inputs."""


def _rows(data: bytes):
    rows = []
    for line, raw in enumerate(data.splitlines(keepends=True), 1):
        if not raw.strip():
            continue
        value = strict_json_loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise StudyReportError("study ledger contains a non-object row")
        rows.append((line, value, hashlib.sha256(raw).hexdigest()))
    return rows


def _load_criterion(path: Path, expected_sha256: str):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise StudyReportError("installed blind criterion digest differs")
    # Execute the exact verified bytes, not a pathname re-opened after the hash.
    # This is the existing installed criterion, never user-provided ledger code.
    namespace = {"__name__": "_verified_council_blind_criterion", "__file__": str(path)}
    exec(compile(data, str(path), "exec"), namespace)
    if not all(callable(namespace.get(key)) for key in ("tally", "_apply_supersedes")):
        raise StudyReportError("installed blind criterion interface differs")
    return namespace


def _combine_blind(criterion, old_rows, fresh_rows):
    old = criterion["tally"](old_rows)
    fresh = criterion["tally"](fresh_rows)
    if old["errors"] or fresh["errors"]:
        raise StudyReportError("a study has invalid blind-seat accounting")
    old_kept, _, old_errors = criterion["_apply_supersedes"](old_rows)
    fresh_kept, _, fresh_errors = criterion["_apply_supersedes"](fresh_rows)
    if old_errors or fresh_errors:
        raise StudyReportError("a study has invalid supersession accounting")
    if any(row.get("kind") == "council-superseded" for _, row, _ in fresh_rows):
        raise StudyReportError("fresh study contains forbidden retrospective supersession")

    # Detect cross-store identity reuse without renumbering any physical row.
    def identities(rows):
        runs, briefs = set(), set()
        for _, row in rows:
            if isinstance(row.get("runId"), str):
                runs.add(row["runId"])
            seat = row.get("blindSeat")
            if isinstance(seat, dict) and "ran" in seat:
                briefs.add(str(seat.get("brief") or "").strip())
        return runs, briefs

    old_runs, old_briefs = identities([(line, row) for line, row, _ in old_rows])
    fresh_runs, fresh_briefs = identities([(line, row) for line, row, _ in fresh_rows])
    if old_runs & fresh_runs or old_briefs & fresh_briefs:
        raise StudyReportError("run or blind brief identity is reused across studies")
    fields = (
        "completedRuns", "changedDecisionRuns", "unchangedDecisionRuns",
        "blockedNonRuns", "notRequiredSkips", "legacyBlindRows",
        "supersededRows", "nonCouncilRecords",
    )
    combined = {field: old[field] + fresh[field] for field in fields}
    fresh_required_success = any(
        isinstance(row.get("blindSeat"), dict)
        and row["blindSeat"].get("required") is True
        and row["blindSeat"].get("ran") is True
        for _, row in fresh_kept
    )
    trailing = fresh["consecutiveRequiredBlockedNonRuns"]
    if not fresh_required_success:
        trailing += old["consecutiveRequiredBlockedNonRuns"]
    combined["consecutiveRequiredBlockedNonRuns"] = trailing
    combined["operationalState"] = "BLOCKED_DEGRADED" if trailing >= 2 else "OK"
    n, changed = combined["completedRuns"], combined["changedDecisionRuns"]
    combined["decisionChangingRate"] = changed / n if n else None
    combined["criterion"] = "NOT_YET_EVALUABLE" if n < 10 else "RETIRE" if changed == 0 else "KEEP"
    return old, fresh, combined


def study_operations_report(
    *, criterion_path: str | Path, criterion_sha256: str,
    legacy_ledger_sha256: str, as_of: datetime | None = None,
):
    """Read both stores under their existing shared lock; never pool study scores.

    The legacy digest must come from the actual closure receipt. Both ledgers
    and all sidecars must exist, so loss of a fresh store cannot read as zero.
    ``as_of`` is for local tests; the public CLI always uses the real clock.
    """

    old = resolve_study_route("council-legacy")
    fresh = resolve_study_route("council-fresh-20260910")
    if old.coordination_lock != fresh.coordination_lock:
        raise StudyReportError("studies do not share the evidence lock")
    criterion = _load_criterion(Path(criterion_path), criterion_sha256)
    clock = as_of or datetime.now(timezone.utc)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise StudyReportError("report clock requires a timezone")
    with Path(old.coordination_lock).open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        snapshots = {}
        for route in (old, fresh):
            snapshots[route.study_id] = {
                field: Path(getattr(route, field)).read_bytes()
                for field in ("log", "v1_events", "v2_events")
            }
        if hashlib.sha256(snapshots[old.study_id]["log"]).hexdigest() != legacy_ledger_sha256:
            raise StudyReportError("closed issuance ledger differs from closure receipt")
        old_tally, fresh_tally, combined = _combine_blind(
            criterion, _rows(snapshots[old.study_id]["log"]),
            _rows(snapshots[fresh.study_id]["log"]),
        )
        studies = {}
        for route, tally in ((old, old_tally), (fresh, fresh_tally)):
            forecast = audit(route.log, route.v1_events, as_of=clock)
            capture = capture_report(
                route.log, route.v2_events, artifact_store=ArtifactStore(route.artifact_root), as_of=clock,
            )
            if forecast["invalidRecords"] or capture["ledger"]["invalidV2RecordCount"]:
                raise StudyReportError("a study contains invalid forecast evidence")
            studies[route.study_id] = {
                "collectionState": route.collection_state,
                "forecast": forecast, "capture": capture, "blind": tally,
                "inputSha256": {
                    field: hashlib.sha256(data).hexdigest()
                    for field, data in snapshots[route.study_id].items()
                },
            }
        # The two independent captures are not an aggregate research sample.
        # Existing V1 debt policy is retained for each store, including overrides.
        debt_blocked = any(s["forecast"]["gradingDebtState"] == "BLOCK_FINALIZATION" for s in studies.values())
        fresh_health = studies[fresh.study_id]["capture"]["activationReadiness"]
        blocked = debt_blocked or combined["operationalState"] == "BLOCKED_DEGRADED" or fresh_health.get("currentlyHealthy") is not True
        return {
            "asOf": clock.isoformat(), "studies": studies,
            "cumulativeBlindOperations": combined,
            "oldOverdueOutcomes": sum(s["forecast"]["oldOverdueOutcomes"] for s in studies.values()),
            "gradingDebtBlocksFinalization": debt_blocked,
            "freshStudyCurrentlyHealthy": fresh_health.get("currentlyHealthy") is True,
            "finalizationBlocked": blocked,
            "exitCode": 3 if blocked else 0,
            "criterionSha256": criterion_sha256,
            "legacyIssuanceClosureSha256": legacy_ledger_sha256,
        }
