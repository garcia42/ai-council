"""Operator-approved recovery for one valid council row's blind-brief path."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import socket
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .forecasts import LedgerError, _ledger_lock


LIVE_KNOWLEDGE_ROOT = Path("/home/trader/.claude/knowledge")
DEFAULT_AUTHORITY_HOST = "manny"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
Checkpoint = Callable[[str], None]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LedgerError(f"{label} must be an object")
    observed = set(value)
    if observed != expected:
        raise LedgerError(
            f"{label} has unexpected shape; missing={sorted(expected - observed)} "
            f"extra={sorted(observed - expected)}"
        )
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{label} must be non-empty text")
    return value.strip()


def _require_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LedgerError(f"{label} must be a positive integer")
    return value


def _require_sha256(value: Any, label: str) -> str:
    value = _require_text(value, label)
    if not SHA256_RE.fullmatch(value):
        raise LedgerError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_timestamp(value: Any, label: str) -> str:
    value = _require_text(value, label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise LedgerError(f"{label} must include a timezone")
    return value


def _logical_path(value: Any, label: str) -> Path:
    raw = _require_text(value, label)
    path = Path(raw)
    if not path.is_absolute() or path != Path(os.path.abspath(path)):
        raise LedgerError(f"{label} must be an absolute normalized path")
    return path


def _physical_path(logical: Path, rehearsal_root: Path | None) -> Path:
    if rehearsal_root is None:
        return logical
    return rehearsal_root / logical.relative_to(logical.anchor)


def _logical_of(physical: Path, rehearsal_root: Path | None) -> Path:
    if rehearsal_root is None:
        return physical
    return Path(physical.anchor) / physical.relative_to(rehearsal_root)


def _real_existing_file(path: Path, label: str) -> os.stat_result:
    try:
        observed = path.lstat()
    except FileNotFoundError as exc:
        raise LedgerError(f"{label} does not exist: {path}") from exc
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise LedgerError(f"{label} must be a real single-link regular file: {path}")
    if path.resolve(strict=True) != path:
        raise LedgerError(f"{label} path contains a filesystem alias: {path}")
    if observed.st_uid != os.geteuid():
        raise LedgerError(f"{label} must be owned by the current operator: {path}")
    return observed


def _real_existing_directory(path: Path, label: str) -> os.stat_result:
    try:
        observed = path.lstat()
    except FileNotFoundError as exc:
        raise LedgerError(f"{label} does not exist: {path}") from exc
    if not stat.S_ISDIR(observed.st_mode):
        raise LedgerError(f"{label} must be a real directory: {path}")
    if path.resolve(strict=True) != path:
        raise LedgerError(f"{label} path contains a filesystem alias: {path}")
    if observed.st_uid != os.geteuid():
        raise LedgerError(f"{label} must be owned by the current operator: {path}")
    return observed


def _refuse_existing(path: Path, label: str) -> None:
    if os.path.lexists(path):
        raise LedgerError(f"{label} already exists: {path}")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _exclusive_write(path: Path, data: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _fsync_directory(path.parent)


def _audit_line(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _append_audit(path: Path, value: dict[str, Any], *, create: bool) -> None:
    flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_CREAT | os.O_EXCL if create else os.O_APPEND
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "ab" if not create else "wb") as handle:
            descriptor = -1
            handle.write(_audit_line(value))
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _fsync_directory(path.parent)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_rows(lines: list[bytes]) -> list[dict[str, Any]]:
    rows = []
    for line_number, raw in enumerate(lines, 1):
        if not raw.endswith(b"\n"):
            raise LedgerError(f"ledger line {line_number} lacks its final newline")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LedgerError(f"ledger line {line_number} is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise LedgerError(f"ledger line {line_number} is not an object")
        rows.append(value)
    return rows


def _brief_owners(rows: list[dict[str, Any]], brief: str) -> list[int]:
    result = []
    for line_number, row in enumerate(rows, 1):
        seat = row.get("blindSeat")
        if isinstance(seat, dict) and str(seat.get("brief") or "").strip() == brief:
            result.append(line_number)
    return result


def _prediction_digest(rows: list[dict[str, Any]]) -> str:
    predictions = [
        [line_number, row.get("predictions")]
        for line_number, row in enumerate(rows, 1)
        if "predictions" in row
    ]
    return _digest(
        json.dumps(predictions, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _assert_live_authority(*paths: Path) -> None:
    live = []
    live_root = LIVE_KNOWLEDGE_ROOT.resolve()
    for path in paths:
        try:
            path.resolve(strict=False).relative_to(live_root)
        except ValueError:
            continue
        live.append(path)
    if not live:
        return
    expected = os.environ.get(
        "COUNCIL_LEDGER_AUTHORITY_HOST", DEFAULT_AUTHORITY_HOST
    ).split(".")[0]
    observed = socket.gethostname().split(".")[0]
    if observed != expected:
        raise LedgerError(
            f"brief preparation and valid-row recovery under live knowledge are "
            f"authorized only on {expected}; current host is {observed}. This is the "
            f"same authority rule the ledger writers use; override it for a host "
            f"migration with COUNCIL_LEDGER_AUTHORITY_HOST, not by editing source."
        )


def prepare_blind_brief(
    *, run_id: str, source_path: str | Path, destination_path: str | Path,
    expected_sha256: str,
) -> dict[str, Any]:
    """Create one immutable run-scoped brief without an overwrite path."""

    run_id = _require_text(run_id, "runId")
    if not re.fullmatch(r"run-[0-9a-f]{32}", run_id):
        raise LedgerError("runId must be a run UUID id")
    expected_sha256 = _require_sha256(expected_sha256, "expectedSha256")
    source = _logical_path(str(source_path), "source")
    destination = _logical_path(str(destination_path), "destination")
    if run_id not in destination.name:
        raise LedgerError("brief destination must contain the exact runId")
    _assert_live_authority(destination)
    _real_existing_file(source, "brief source")
    _real_existing_directory(destination.parent, "brief destination parent")
    _refuse_existing(destination, "brief destination")
    if source.resolve() == destination.resolve(strict=False):
        raise LedgerError("brief source and destination must not alias")
    content = source.read_bytes()
    observed_sha256 = _digest(content)
    if observed_sha256 != expected_sha256:
        raise LedgerError(
            f"brief source SHA-256 drift: expected {expected_sha256}, "
            f"found {observed_sha256}"
        )
    _exclusive_write(destination, content, 0o444)
    return {
        "status": "created",
        "runId": run_id,
        "destination": str(destination),
        "sha256": observed_sha256,
        "bytes": len(content),
        "mode": "0444",
    }


def _load_spec(spec: Any) -> dict[str, Any]:
    spec = _require_keys(
        spec,
        {
            "schemaVersion",
            "kind",
            "approval",
            "ledger",
            "target",
            "replacementBrief",
            "expectedAfter",
            "artifactDir",
        },
        "recovery spec",
    )
    if spec["schemaVersion"] != 1:
        raise LedgerError("recovery spec schemaVersion must be 1")
    if spec["kind"] != "council-blind-brief-valid-row-recovery":
        raise LedgerError("recovery spec kind is not supported")
    approval = _require_keys(
        spec["approval"], {"operator", "approvedAt", "reference", "reason"}, "approval"
    )
    _require_text(approval["operator"], "approval.operator")
    _require_timestamp(approval["approvedAt"], "approval.approvedAt")
    _require_text(approval["reference"], "approval.reference")
    _require_text(approval["reason"], "approval.reason")
    _require_keys(
        spec["ledger"],
        {"path", "expectedSha256", "expectedLineCount", "expectedByteCount", "expectedMode"},
        "ledger",
    )
    _logical_path(spec["ledger"]["path"], "ledger.path")
    _require_sha256(spec["ledger"]["expectedSha256"], "ledger.expectedSha256")
    _require_integer(spec["ledger"]["expectedLineCount"], "ledger.expectedLineCount")
    _require_integer(spec["ledger"]["expectedByteCount"], "ledger.expectedByteCount")
    if not re.fullmatch(r"0[0-7]{3}", str(spec["ledger"]["expectedMode"])):
        raise LedgerError("ledger.expectedMode must be a four-digit octal mode")
    target = _require_keys(
        spec["target"],
        {
            "line",
            "runId",
            "rawLineSha256",
            "oldBriefPath",
            "conflictingLine",
            "conflictingRunId",
            "conflictingRawLineSha256",
        },
        "target",
    )
    _require_integer(target["line"], "target.line")
    _require_text(target["runId"], "target.runId")
    _require_sha256(target["rawLineSha256"], "target.rawLineSha256")
    _logical_path(target["oldBriefPath"], "target.oldBriefPath")
    _require_integer(target["conflictingLine"], "target.conflictingLine")
    _require_text(target["conflictingRunId"], "target.conflictingRunId")
    _require_sha256(
        target["conflictingRawLineSha256"], "target.conflictingRawLineSha256"
    )
    replacement = _require_keys(
        spec["replacementBrief"], {"sourcePath", "sha256", "destinationPath"}, "replacementBrief"
    )
    _logical_path(replacement["sourcePath"], "replacementBrief.sourcePath")
    _require_sha256(replacement["sha256"], "replacementBrief.sha256")
    destination = _logical_path(
        replacement["destinationPath"], "replacementBrief.destinationPath"
    )
    if target["runId"] not in destination.name:
        raise LedgerError("replacement brief destination must contain the exact target runId")
    expected_after = _require_keys(
        spec["expectedAfter"],
        {
            "ledgerSha256",
            "ledgerByteCount",
            "targetRawLineSha256",
            "unaffectedPrefixSha256",
            "unaffectedSuffixSha256",
        },
        "expectedAfter",
    )
    _require_sha256(expected_after["ledgerSha256"], "expectedAfter.ledgerSha256")
    _require_integer(expected_after["ledgerByteCount"], "expectedAfter.ledgerByteCount")
    _require_sha256(
        expected_after["targetRawLineSha256"], "expectedAfter.targetRawLineSha256"
    )
    _require_sha256(
        expected_after["unaffectedPrefixSha256"], "expectedAfter.unaffectedPrefixSha256"
    )
    _require_sha256(
        expected_after["unaffectedSuffixSha256"], "expectedAfter.unaffectedSuffixSha256"
    )
    artifact = _logical_path(spec["artifactDir"], "artifactDir")
    if target["runId"] not in artifact.name:
        raise LedgerError("artifactDir must contain the exact target runId")
    return spec





def _repaired_prefix_matches(observed: bytes, after: bytes, line_count: int) -> bool:
    """True when the first ``line_count`` lines of ``observed`` are exactly ``after``.

    A recovery that completes its ledger swap and then dies before its audit is
    resumable only if a legitimate append landing in the meantime does not hide
    the evidence. The ledger is append-only, so the repaired image survives as a
    prefix; anything after it is somebody else's row and is none of our business.
    """

    lines = observed.splitlines(keepends=True)
    if len(lines) < line_count:
        return False
    return b"".join(lines[:line_count]) == after


def _is_repaired_image(observed: bytes, spec: dict[str, Any]) -> bool:
    """Cheap pre-check that the target row is already repaired in ``observed``."""

    lines = observed.splitlines(keepends=True)
    target_index = spec["target"]["line"] - 1
    if len(lines) < spec["ledger"]["expectedLineCount"] or target_index >= len(lines):
        return False
    if _digest(lines[target_index]) != spec["expectedAfter"]["targetRawLineSha256"]:
        return False
    return (
        _digest(b"".join(lines[:target_index]))
        == spec["expectedAfter"]["unaffectedPrefixSha256"]
    )


def _read_audit_records(path: Path) -> list[dict[str, Any]]:
    records = []
    for line_number, raw in enumerate(path.read_bytes().splitlines(keepends=True), 1):
        if not raw.endswith(b"\n"):
            raise LedgerError(f"recovery audit line {line_number} lacks its final newline")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LedgerError(
                f"recovery audit line {line_number} is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(value, dict):
            raise LedgerError(f"recovery audit line {line_number} is not an object")
        records.append(value)
    return records


def _resume_audit_state(
    audit: Path, pinned: dict[str, Any]
) -> tuple[str, bool]:
    """Return the prior recoveryId and whether the recovery already completed."""

    records = _read_audit_records(audit)
    if not 1 <= len(records) <= 2:
        raise LedgerError(
            f"recovery audit must hold one prepared and at most one completed "
            f"record; found {len(records)}"
        )
    prepared = records[0]
    if prepared.get("status") != "prepared":
        raise LedgerError("first recovery audit record is not a prepared intent")
    recovery_id = prepared.get("recoveryId")
    if not isinstance(recovery_id, str) or not recovery_id.strip():
        raise LedgerError("prepared recovery audit record has no recoveryId")
    for key, expected in pinned.items():
        if prepared.get(key) != expected:
            raise LedgerError(
                f"prepared recovery audit disagrees with this spec on {key}: "
                f"recorded {prepared.get(key)!r}, spec {expected!r}"
            )
    if len(records) == 1:
        return recovery_id, False
    completed = records[1]
    if completed.get("status") != "completed":
        raise LedgerError("second recovery audit record is not a completion")
    if completed.get("recoveryId") != recovery_id:
        raise LedgerError("completed recovery audit record has a different recoveryId")
    return recovery_id, True


def _verify_existing_bytes(path: Path, expected: bytes, label: str) -> None:
    _real_existing_file(path, label)
    observed = path.read_bytes()
    if observed != expected:
        raise LedgerError(
            f"{label} exists with unexpected content: expected SHA-256 "
            f"{_digest(expected)}, found {_digest(observed)}"
        )


def _leaked_temporaries(ledger: Path) -> list[Path]:
    pattern = f".{ledger.name}.brief-recovery-*.tmp"
    return sorted(path for path in ledger.parent.glob(pattern) if path.is_file())


def recover_blind_brief(
    spec: Any,
    *,
    operator_confirmed: bool,
    rehearsal_root: str | Path | None = None,
    resume: bool = False,
    checkpoint: Checkpoint | None = None,
) -> dict[str, Any]:
    """Perform one hash-pinned, byte-audited valid-row repair under the append lock.

    With ``resume`` the command re-enters an interrupted recovery. It refuses to
    start a new one: the artifact directory must already exist, every artifact it
    finds must match this spec byte for byte, and the ledger must be at either the
    recorded before image or the planned after image. Anything else fails closed.
    """

    if operator_confirmed is not True:
        raise LedgerError("operator approval confirmation is required")
    spec = _load_spec(spec)
    logical_ledger = Path(spec["ledger"]["path"])
    logical_source = Path(spec["replacementBrief"]["sourcePath"])
    logical_destination = Path(spec["replacementBrief"]["destinationPath"])
    logical_artifact = Path(spec["artifactDir"])

    rehearsal = None
    if rehearsal_root is not None:
        rehearsal = Path(rehearsal_root)
        _real_existing_directory(rehearsal, "rehearsal root")
        if rehearsal == Path("/") or rehearsal == Path("/home/trader"):
            raise LedgerError("rehearsal root must not be a live or broad filesystem root")
        try:
            rehearsal.relative_to(LIVE_KNOWLEDGE_ROOT.resolve())
        except ValueError:
            pass
        else:
            raise LedgerError("rehearsal root must be outside live knowledge")

    ledger = _physical_path(logical_ledger, rehearsal)
    source = _physical_path(logical_source, rehearsal)
    destination = _physical_path(logical_destination, rehearsal)
    artifact_dir = _physical_path(logical_artifact, rehearsal)
    _assert_live_authority(ledger, destination)
    ledger_stat = _real_existing_file(ledger, "ledger")
    _real_existing_file(source, "replacement brief source")
    _real_existing_directory(destination.parent, "replacement brief parent")
    _real_existing_directory(artifact_dir.parent, "artifact parent")
    if resume:
        _real_existing_directory(artifact_dir, "recovery artifact directory")
    else:
        _refuse_existing(destination, "replacement brief destination")
        _refuse_existing(artifact_dir, "recovery artifact directory")
    if len({ledger.resolve(), source.resolve(), destination.resolve(strict=False), artifact_dir.resolve(strict=False)}) != 4:
        raise LedgerError("ledger, source, destination, and artifact paths must not alias")

    source_bytes = source.read_bytes()
    source_sha = _digest(source_bytes)
    if source_sha != spec["replacementBrief"]["sha256"]:
        raise LedgerError(
            f"replacement brief SHA-256 drift: expected "
            f"{spec['replacementBrief']['sha256']}, found {source_sha}"
        )

    expected_mode = int(spec["ledger"]["expectedMode"], 8)
    if stat.S_IMODE(ledger_stat.st_mode) != expected_mode:
        raise LedgerError(
            f"ledger mode drift: expected {expected_mode:04o}, "
            f"found {stat.S_IMODE(ledger_stat.st_mode):04o}"
        )

    checkpoint = checkpoint or (lambda _name: None)
    reused: list[str] = []
    cleaned: list[str] = []
    with _ledger_lock(ledger):
        observed_stat = _real_existing_file(ledger, "ledger")
        observed = ledger.read_bytes()
        observed_sha = _digest(observed)
        backup_name = f"{ledger.name}.{spec['ledger']['expectedSha256']}.backup"
        backup = artifact_dir / backup_name
        audit = artifact_dir / f"council-brief-recovery-{spec['target']['runId']}.audit.jsonl"

        if observed_sha == spec["ledger"]["expectedSha256"]:
            ledger_phase = "before"
            before = observed
            before_stat = observed_stat
        elif resume and _is_repaired_image(observed, spec):
            ledger_phase = "after"
            if not backup.is_file():
                raise LedgerError(
                    "ledger is already at the repaired image but its recovery backup "
                    f"is missing: {backup}"
                )
            _real_existing_file(backup, "recovery backup")
            before = backup.read_bytes()
            if _digest(before) != spec["ledger"]["expectedSha256"]:
                raise LedgerError("recovery backup does not hold the recorded before image")
            before_stat = observed_stat
        else:
            raise LedgerError(
                f"ledger SHA-256 drift: expected {spec['ledger']['expectedSha256']}, "
                f"found {observed_sha}"
            )

        if len(before) != spec["ledger"]["expectedByteCount"]:
            raise LedgerError("ledger byte-count drift")
        lines = before.splitlines(keepends=True)
        if len(lines) != spec["ledger"]["expectedLineCount"]:
            raise LedgerError("ledger line-count drift")
        before_sha = _digest(before)
        rows = _parse_rows(lines)
        target_index = spec["target"]["line"] - 1
        conflict_index = spec["target"]["conflictingLine"] - 1
        if target_index >= len(rows) or conflict_index >= len(rows):
            raise LedgerError("target or conflicting line is outside the ledger")
        target_row = rows[target_index]
        conflicting_row = rows[conflict_index]
        if _digest(lines[target_index]) != spec["target"]["rawLineSha256"]:
            raise LedgerError("target raw-line SHA-256 drift")
        if _digest(lines[conflict_index]) != spec["target"]["conflictingRawLineSha256"]:
            raise LedgerError("conflicting raw-line SHA-256 drift")
        if target_row.get("kind") != "council" or target_row.get("schemaVersion") != 1:
            raise LedgerError("target has unexpected council completion shape")
        if target_row.get("runId") != spec["target"]["runId"]:
            raise LedgerError("target runId drift")
        if conflicting_row.get("kind") != "council" or conflicting_row.get("runId") != spec["target"]["conflictingRunId"]:
            raise LedgerError("conflicting council row drift")
        seat = target_row.get("blindSeat")
        if not isinstance(seat, dict) or seat.get("ran") is not True:
            raise LedgerError("target blindSeat has unexpected shape")
        old_brief = spec["target"]["oldBriefPath"]
        if seat.get("brief") != old_brief:
            raise LedgerError("target old blind brief path drift")
        if _brief_owners(rows, old_brief) != [
            spec["target"]["conflictingLine"],
            spec["target"]["line"],
        ]:
            raise LedgerError("old brief ownership set differs from the exact incident")
        new_brief = spec["replacementBrief"]["destinationPath"]
        if _brief_owners(rows, new_brief):
            raise LedgerError("replacement brief path is already owned by a council row")
        canonical_before_target = (
            json.dumps(target_row, sort_keys=True, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        if canonical_before_target != lines[target_index]:
            raise LedgerError("target row is not in the expected canonical encoding")
        if _digest(b"".join(lines[:target_index])) != spec["expectedAfter"]["unaffectedPrefixSha256"]:
            raise LedgerError("unaffected prefix SHA-256 drift")
        if _digest(b"".join(lines[target_index + 1 :])) != spec["expectedAfter"]["unaffectedSuffixSha256"]:
            raise LedgerError("unaffected suffix SHA-256 drift")

        before_predictions_sha = _prediction_digest(rows)
        before_unrelated_sha = _digest(
            b"".join(raw for index, raw in enumerate(lines) if index != target_index)
        )
        after_rows = copy.deepcopy(rows)
        after_rows[target_index]["blindSeat"]["brief"] = new_brief
        after_target = (
            json.dumps(after_rows[target_index], sort_keys=True, separators=(",", ":"))
            .encode("utf-8")
            + b"\n"
        )
        if _digest(after_target) != spec["expectedAfter"]["targetRawLineSha256"]:
            raise LedgerError("expected repaired target raw-line SHA-256 does not match")
        after_lines = list(lines)
        after_lines[target_index] = after_target
        after = b"".join(after_lines)
        after_sha = _digest(after)
        if after_sha != spec["expectedAfter"]["ledgerSha256"]:
            raise LedgerError("expected repaired ledger SHA-256 does not match")
        if len(after) != spec["expectedAfter"]["ledgerByteCount"]:
            raise LedgerError("expected repaired ledger byte count does not match")
        if _prediction_digest(after_rows) != before_predictions_sha:
            raise LedgerError("recovery would change a prediction")
        if any(
            before_row != after_row
            for index, (before_row, after_row) in enumerate(zip(rows, after_rows))
            if index != target_index
        ):
            raise LedgerError("recovery would change an unrelated row")
        restored_target = copy.deepcopy(after_rows[target_index])
        restored_target["blindSeat"]["brief"] = old_brief
        if restored_target != target_row:
            raise LedgerError("recovery would change more than target blindSeat.brief")
        new_owners = [
            _brief_owners(after_rows, old_brief),
            _brief_owners(after_rows, new_brief),
        ]
        if new_owners != [[spec["target"]["conflictingLine"]], [spec["target"]["line"]]]:
            raise LedgerError("recovery does not yield one owner per affected brief")
        if ledger_phase == "after" and not _repaired_prefix_matches(observed, after, len(lines)):
            raise LedgerError("ledger is at neither the recorded before nor the planned after image")
        checkpoint("validated")

        if resume:
            for leaked in _leaked_temporaries(ledger):
                leaked.unlink()
                cleaned.append(str(_logical_of(leaked, rehearsal)))
            _fsync_directory(ledger.parent)
        else:
            artifact_dir.mkdir(mode=0o700)
            os.chmod(artifact_dir, 0o700)
            _fsync_directory(artifact_dir.parent)

        if backup.exists():
            if not resume:
                raise LedgerError(f"recovery backup already exists: {backup}")
            _verify_existing_bytes(backup, before, "recovery backup")
            reused.append("backup")
        else:
            _exclusive_write(backup, before, 0o400)
        checkpoint("backup-durable")

        pinned_audit = {
            "kind": "council-blind-brief-valid-row-recovery-audit",
            "ledger": str(logical_ledger),
            "targetLine": spec["target"]["line"],
            "targetRunId": spec["target"]["runId"],
            "conflictingLine": spec["target"]["conflictingLine"],
            "conflictingRunId": spec["target"]["conflictingRunId"],
            "oldBriefPath": old_brief,
            "newBriefPath": new_brief,
            "replacementBriefSha256": source_sha,
            "beforeLedgerSha256": before_sha,
            "afterLedgerSha256": after_sha,
            "predictionsSha256": before_predictions_sha,
            "unaffectedRowsSha256": before_unrelated_sha,
        }
        already_completed = False
        if audit.exists():
            if not resume:
                raise LedgerError(f"recovery audit already exists: {audit}")
            _real_existing_file(audit, "recovery audit")
            recovery_id, already_completed = _resume_audit_state(audit, pinned_audit)
            reused.append("audit-intent")
        else:
            recovery_id = f"brief-recovery-{uuid.uuid4().hex}"
        if already_completed and ledger_phase != "after":
            raise LedgerError(
                "recovery audit records a completion but the ledger is at the before image"
            )

        common_audit = {
            "schemaVersion": 1,
            **pinned_audit,
            "recoveryId": recovery_id,
            "operator": spec["approval"]["operator"],
            "approvalReference": spec["approval"]["reference"],
            "approvalReason": spec["approval"]["reason"],
            "approvedAt": spec["approval"]["approvedAt"],
            "host": socket.gethostname(),
            "beforeLedgerBytes": len(before),
            "afterLedgerBytes": len(after),
            "lineCount": len(lines),
            "beforeTargetRawLineSha256": _digest(lines[target_index]),
            "afterTargetRawLineSha256": _digest(after_target),
            "backup": str(logical_artifact / backup.name),
            "ledgerMode": f"{stat.S_IMODE(before_stat.st_mode):04o}",
        }
        if "audit-intent" not in reused:
            _append_audit(
                audit,
                {**common_audit, "status": "prepared", "recordedAt": _utc_now()},
                create=True,
            )
        checkpoint("intent-durable")

        if destination.exists():
            if not resume:
                raise LedgerError(f"replacement brief destination already exists: {destination}")
            _verify_existing_bytes(destination, source_bytes, "replacement brief destination")
            reused.append("brief")
        else:
            _exclusive_write(destination, source_bytes, 0o444)
        checkpoint("brief-durable")

        if ledger_phase == "after":
            reused.append("ledger")
            checkpoint("before-ledger-replace")
        else:
            temporary = ledger.with_name(
                f".{ledger.name}.brief-recovery-{uuid.uuid4().hex}.tmp"
            )
            try:
                _exclusive_write(temporary, after, stat.S_IMODE(before_stat.st_mode))
                checkpoint("before-ledger-replace")
                current_stat = _real_existing_file(ledger, "ledger")
                if (current_stat.st_dev, current_stat.st_ino) != (
                    observed_stat.st_dev,
                    observed_stat.st_ino,
                ) or ledger.read_bytes() != before:
                    raise LedgerError("ledger changed despite the held append lock")
                os.replace(temporary, ledger)
                _fsync_directory(ledger.parent)
            finally:
                if os.path.lexists(temporary):
                    temporary.unlink()
        checkpoint("ledger-durable")

        after_stat = _real_existing_file(ledger, "ledger")
        if stat.S_IMODE(after_stat.st_mode) != expected_mode:
            raise LedgerError("ledger mode was not preserved")
        if not _repaired_prefix_matches(ledger.read_bytes(), after, len(lines)):
            raise LedgerError("post-recovery ledger bytes differ from the planned image")
        if already_completed:
            reused.append("completion-audit")
        else:
            _append_audit(
                audit,
                {**common_audit, "status": "completed", "recordedAt": _utc_now()},
                create=False,
            )
        checkpoint("complete-audit-durable")

    performed = {"backup", "audit-intent", "brief", "ledger", "completion-audit"} - set(reused)
    return {
        "status": "already-complete" if not performed else "completed",
        "resumed": bool(resume),
        "reusedSteps": reused,
        "cleanedTemporaries": cleaned,
        "recoveryId": recovery_id,
        "ledger": str(logical_ledger),
        "beforeLedgerSha256": before_sha,
        "afterLedgerSha256": after_sha,
        "lineCount": len(lines),
        "targetLine": spec["target"]["line"],
        "targetRunId": spec["target"]["runId"],
        "conflictingRunId": spec["target"]["conflictingRunId"],
        "replacementBrief": str(logical_destination),
        "replacementBriefSha256": source_sha,
        "backup": str(logical_artifact / backup.name),
        "audit": str(logical_artifact / audit.name),
        "predictionsSha256": before_predictions_sha,
        "unaffectedRowsSha256": before_unrelated_sha,
        "ledgerMode": f"{stat.S_IMODE(after_stat.st_mode):04o}",
    }


def plan_blind_brief_recovery(
    *,
    ledger_path: str | Path,
    target_line: int,
    replacement_source: str | Path,
    destination_path: str | Path | None = None,
    artifact_dir: str | Path | None = None,
    operator: str,
    approval_reference: str,
    approval_reason: str,
    approved_at: str | None = None,
    rehearsal_root: str | Path | None = None,
) -> dict[str, Any]:
    """Derive a recovery spec from the live ledger bytes.

    This only proposes. ``recover_blind_brief`` re-derives every value here from
    the ledger it actually reads and refuses to act on any disagreement, so a
    wrong plan fails closed rather than repairing the wrong row.
    """

    logical_ledger = _logical_path(str(ledger_path), "ledger path")
    target_line = _require_integer(target_line, "target line")
    rehearsal = Path(rehearsal_root) if rehearsal_root is not None else None
    if rehearsal is not None:
        _real_existing_directory(rehearsal, "rehearsal root")
    ledger = _physical_path(logical_ledger, rehearsal)
    source = _physical_path(_logical_path(str(replacement_source), "replacement source"), rehearsal)
    ledger_stat = _real_existing_file(ledger, "ledger")
    _real_existing_file(source, "replacement brief source")
    source_bytes = source.read_bytes()

    with _ledger_lock(ledger):
        before = ledger.read_bytes()
        lines = before.splitlines(keepends=True)
        rows = _parse_rows(lines)
        if target_line > len(rows):
            raise LedgerError(f"target line {target_line} is outside the ledger")
        target_index = target_line - 1
        target_row = rows[target_index]
        if target_row.get("kind") != "council":
            raise LedgerError(f"line {target_line} is not a council completion")
        run_id = _require_text(target_row.get("runId"), "target runId")
        seat = target_row.get("blindSeat")
        if not isinstance(seat, dict) or seat.get("ran") is not True:
            raise LedgerError("target blindSeat has unexpected shape")
        old_brief = _require_text(seat.get("brief"), "target blindSeat.brief")
        owners = _brief_owners(rows, old_brief)
        if len(owners) != 2 or target_line not in owners:
            raise LedgerError(
                f"line {target_line} does not share its brief with exactly one other "
                f"council row; owners={owners}"
            )
        conflicting_line = next(line for line in owners if line != target_line)
        conflicting_run_id = _require_text(
            rows[conflicting_line - 1].get("runId"), "conflicting runId"
        )

        if destination_path is None:
            old_path = Path(old_brief)
            logical_destination = old_path.with_name(
                f"{old_path.stem}-{run_id}{old_path.suffix}"
            )
        else:
            logical_destination = _logical_path(str(destination_path), "destination path")
        if run_id not in logical_destination.name:
            raise LedgerError("destination must contain the exact target runId")
        if _brief_owners(rows, str(logical_destination)):
            raise LedgerError("destination is already owned by a council row")

        canonical_target = (
            json.dumps(target_row, sort_keys=True, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        if canonical_target != lines[target_index]:
            raise LedgerError(
                f"line {target_line} is not in canonical encoding; it cannot be "
                "replanned without changing unrelated bytes"
            )
        after_rows = copy.deepcopy(rows)
        after_rows[target_index]["blindSeat"]["brief"] = str(logical_destination)
        after_target = (
            json.dumps(after_rows[target_index], sort_keys=True, separators=(",", ":"))
            .encode("utf-8")
            + b"\n"
        )
        after_lines = list(lines)
        after_lines[target_index] = after_target
        after = b"".join(after_lines)

        if artifact_dir is None:
            logical_artifact = (
                Path.home() / ".local/state/council-tools" / f"ledger-recovery-{run_id}"
            )
        else:
            logical_artifact = _logical_path(str(artifact_dir), "artifact dir")
        if run_id not in logical_artifact.name:
            raise LedgerError("artifact dir must contain the exact target runId")

        return {
            "schemaVersion": 1,
            "kind": "council-blind-brief-valid-row-recovery",
            "approval": {
                "operator": _require_text(operator, "operator"),
                "approvedAt": _require_timestamp(approved_at or _utc_now(), "approvedAt"),
                "reference": _require_text(approval_reference, "approval reference"),
                "reason": _require_text(approval_reason, "approval reason"),
            },
            "ledger": {
                "path": str(logical_ledger),
                "expectedSha256": _digest(before),
                "expectedLineCount": len(lines),
                "expectedByteCount": len(before),
                "expectedMode": f"{stat.S_IMODE(ledger_stat.st_mode):04o}",
            },
            "target": {
                "line": target_line,
                "runId": run_id,
                "rawLineSha256": _digest(lines[target_index]),
                "oldBriefPath": old_brief,
                "conflictingLine": conflicting_line,
                "conflictingRunId": conflicting_run_id,
                "conflictingRawLineSha256": _digest(lines[conflicting_line - 1]),
            },
            "replacementBrief": {
                "sourcePath": str(_logical_of(source, rehearsal)),
                "sha256": _digest(source_bytes),
                "destinationPath": str(logical_destination),
            },
            "expectedAfter": {
                "ledgerSha256": _digest(after),
                "ledgerByteCount": len(after),
                "targetRawLineSha256": _digest(after_target),
                "unaffectedPrefixSha256": _digest(b"".join(lines[:target_index])),
                "unaffectedSuffixSha256": _digest(b"".join(lines[target_index + 1 :])),
            },
            "artifactDir": str(logical_artifact),
        }
