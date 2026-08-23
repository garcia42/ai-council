"""Strict, append-only council forecast validation and descriptive scoring."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time as datetime_time, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from .capture_schema import strict_json_loads
from .safe_files import (
    PinnedFileTransaction,
    SafeFileError,
    create_bytes_exclusive as safe_create_bytes_exclusive,
    exclusive_lock as safe_exclusive_lock,
    fsync_directory as safe_fsync_directory,
    inventory_transaction_escrows as safe_inventory_transaction_escrows,
    locked_file_transaction as safe_locked_file_transaction,
)
from .resolution_integrity import (
    ResolutionIntegrityError,
    validate_resolution_event_integrity,
)


SCHEMA_VERSION = 1
CANONICAL_SEATS = ("code", "theory", "ops", "blind")
SEAT_ALIASES = {
    "code": "code",
    "pysystemtrade-expert": "code",
    "theory": "theory",
    "qoppac-blog-expert": "theory",
    "ops": "ops",
    "et-futures-journal-expert": "ops",
    "blind": "blind",
}
SEAT_STATES = {"submitted", "abstained", "unavailable"}
VOID_REASONS = {
    "cancelled-decision",
    "claim-defective",
    "data-lost",
    "outcome-ambiguous",
    "resolution-impossible",
}
RESOLUTION_METHODS = {"deterministic", "manual-reviewed"}
ID_PREFIXES = {"run", "outcome", "prediction", "resolution", "override"}
ID_RE = re.compile(
    r"^(run|outcome|prediction|resolution|override)-[0-9a-f]{32}$"
)
LOCK_TIMEOUT_SECONDS = 10.0
OUTCOME_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class LedgerError(ValueError):
    """The forecast ledger or a proposed record violates the contract."""


def new_id(prefix: str) -> str:
    if prefix not in ID_PREFIXES:
        raise LedgerError(f"unknown id prefix: {prefix}")
    return f"{prefix}-{uuid.uuid4().hex}"


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{field} must be non-empty text")
    return value.strip()


def _require_id(value: Any, prefix: str, field: str | None = None) -> str:
    label = field or prefix
    value = _require_text(value, label)
    if not ID_RE.fullmatch(value) or not value.startswith(f"{prefix}-"):
        raise LedgerError(f"{label} must be a {prefix} UUID id")
    return value


def _parse_timestamp(value: Any, field: str) -> datetime:
    value = _require_text(value, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise LedgerError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _parse_date(value: Any, field: str) -> date:
    value = _require_text(value, field)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise LedgerError(f"{field} must be YYYY-MM-DD") from exc


def normalize_seat(value: Any) -> str:
    raw = _require_text(value, "seat")
    try:
        return SEAT_ALIASES[raw]
    except KeyError as exc:
        raise LedgerError(f"unknown seat: {raw}") from exc


def outcome_fingerprint(
    claim: str, resolution_date: str, resolved_by: str, decision_link: str
) -> str:
    pieces = (
        _require_text(claim, "claim"),
        _require_text(resolution_date, "resolutionDate"),
        _require_text(resolved_by, "resolvedBy"),
        _require_text(decision_link, "decisionLink"),
    )
    canonical = "\x1f".join(" ".join(item.split()).casefold() for item in pieces)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _legacy_id(prefix: str, *pieces: Any) -> str:
    # Compatibility identity for pre-contract rows only. These IDs deliberately depend on
    # the normalized legacy content: changing a claim, timestamp, resolution rule, or list
    # position changes the ID and therefore its resolution linkage. New writes must use UUIDs.
    canonical = json.dumps(pieces, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def make_attempt(
    *,
    question: str,
    expected_seats: Iterable[str],
    claim: str,
    resolution_date: str,
    resolved_by: str,
    decision_link: str,
    materiality: str,
    action_if_true: str,
    action_if_false: str,
    evidence_cutoff_at: str,
    ts: str,
    run_id: str | None = None,
    outcome_id: str | None = None,
    related_outcome_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(expected_seats, (list, tuple)):
        raise LedgerError("expectedSeats must be a list")
    seats = [normalize_seat(seat) for seat in expected_seats]
    outcome = {
        "outcomeId": outcome_id or new_id("outcome"),
        "claim": claim,
        "resolutionDate": resolution_date,
        "resolvedBy": resolved_by,
        "decisionLink": decision_link,
        "materiality": materiality,
        "actionIfTrue": action_if_true,
        "actionIfFalse": action_if_false,
        "evidenceCutoffAt": evidence_cutoff_at,
        "relatedOutcomeIds": list(related_outcome_ids or []),
    }
    outcome["fingerprint"] = outcome_fingerprint(
        claim, resolution_date, resolved_by, decision_link
    )
    row = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "council-attempt",
        "runId": run_id or new_id("run"),
        "ts": ts,
        "question": question,
        "expectedSeats": seats,
        "sharedOutcome": outcome,
    }
    validate_attempt(row)
    return row


def validate_attempt(row: dict[str, Any]) -> None:
    if row.get("schemaVersion") != SCHEMA_VERSION:
        raise LedgerError("council-attempt schemaVersion must be 1")
    if row.get("kind") != "council-attempt":
        raise LedgerError("attempt kind must be council-attempt")
    _require_id(row.get("runId"), "run", "runId")
    issued = _parse_timestamp(row.get("ts"), "ts")
    _require_text(row.get("question"), "question")
    seats = row.get("expectedSeats")
    if not isinstance(seats, list) or not seats:
        raise LedgerError("expectedSeats must be a non-empty list")
    normalized = [normalize_seat(seat) for seat in seats]
    if normalized != seats:
        raise LedgerError("expectedSeats must use canonical seat names")
    if len(set(seats)) != len(seats):
        raise LedgerError("expectedSeats contains duplicates")

    outcome = row.get("sharedOutcome")
    if not isinstance(outcome, dict):
        raise LedgerError("sharedOutcome must be an object")
    _require_id(outcome.get("outcomeId"), "outcome", "outcomeId")
    claim = _require_text(outcome.get("claim"), "claim")
    deadline = _parse_date(outcome.get("resolutionDate"), "resolutionDate")
    resolved_by = _require_text(outcome.get("resolvedBy"), "resolvedBy")
    decision_link = _require_text(outcome.get("decisionLink"), "decisionLink")
    _require_text(outcome.get("materiality"), "materiality")
    _require_text(outcome.get("actionIfTrue"), "actionIfTrue")
    _require_text(outcome.get("actionIfFalse"), "actionIfFalse")
    cutoff = _parse_timestamp(outcome.get("evidenceCutoffAt"), "evidenceCutoffAt")
    if cutoff > issued:
        raise LedgerError("evidenceCutoffAt cannot be after the attempt timestamp")
    if issued.date() >= deadline:
        raise LedgerError("attempt timestamp must precede resolutionDate")
    expected = outcome_fingerprint(claim, str(deadline), resolved_by, decision_link)
    if outcome.get("fingerprint") != expected:
        raise LedgerError("sharedOutcome fingerprint does not match its content")
    related = outcome.get("relatedOutcomeIds", [])
    if not isinstance(related, list):
        raise LedgerError("relatedOutcomeIds must be a list")
    for item in related:
        _require_id(item, "outcome", "relatedOutcomeIds item")


def make_completion(
    *,
    attempt: dict[str, Any],
    council_fields: dict[str, Any],
    seat_states: dict[str, str],
    probabilities: dict[str, int],
    ts: str,
) -> dict[str, Any]:
    """Seal one complete set of seat submissions for an existing attempt."""
    validate_attempt(attempt)
    if not isinstance(council_fields, dict):
        raise LedgerError("councilFields must be an object")
    if not isinstance(seat_states, dict):
        raise LedgerError("seatStates must be an object")
    if not isinstance(probabilities, dict):
        raise LedgerError("probabilities must be an object")
    expected = attempt["expectedSeats"]
    normalized_states = {normalize_seat(seat): state for seat, state in seat_states.items()}
    if set(normalized_states) != set(expected):
        raise LedgerError("seatStates must exactly match attempt expectedSeats")
    normalized_probabilities = {
        normalize_seat(seat): probability for seat, probability in probabilities.items()
    }
    submitted = {
        seat for seat, state in normalized_states.items() if state == "submitted"
    }
    if set(normalized_probabilities) != submitted:
        raise LedgerError("probabilities must exactly match submitted seats")
    outcome = attempt["sharedOutcome"]
    predictions = [
        {
            "predictionId": new_id("prediction"),
            "outcomeId": outcome["outcomeId"],
            "seat": seat,
            "type": "shared",
            "claim": outcome["claim"],
            "probability": normalized_probabilities[seat],
            "issuedAt": ts,
            "resolutionDate": outcome["resolutionDate"],
            "resolvedBy": outcome["resolvedBy"],
        }
        for seat in expected
        if seat in submitted
    ]
    protected = {
        "schemaVersion",
        "kind",
        "runId",
        "ts",
        "question",
        "forecastState",
        "predictions",
    }
    overlap = protected.intersection(council_fields)
    if overlap:
        raise LedgerError(f"councilFields contains protected keys: {sorted(overlap)}")
    row = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "council",
        "runId": attempt["runId"],
        "ts": ts,
        "question": attempt["question"],
        **council_fields,
        "forecastState": {"sealed": True, "seats": normalized_states},
        "predictions": predictions,
    }
    return row


def _attempts(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for row in rows:
        if row.get("kind") != "council-attempt":
            continue
        run_id = row.get("runId")
        if run_id in result:
            raise LedgerError(f"duplicate council-attempt runId: {run_id}")
        validate_attempt(row)
        result[run_id] = row
    return result


def validate_completion(row: dict[str, Any], prior_rows: Iterable[dict[str, Any]]) -> None:
    if row.get("schemaVersion") != SCHEMA_VERSION:
        raise LedgerError("new council completion schemaVersion must be 1")
    if row.get("kind") != "council":
        raise LedgerError("completion kind must be council")
    run_id = _require_id(row.get("runId"), "run", "runId")
    prior_rows = list(prior_rows)
    attempts = _attempts(prior_rows)
    if run_id not in attempts:
        raise LedgerError(f"council completion has no matching attempt: {run_id}")
    if any(r.get("kind") == "council" and r.get("runId") == run_id for r in prior_rows):
        raise LedgerError(f"duplicate council completion for runId: {run_id}")
    attempt = attempts[run_id]
    completed_at = _parse_timestamp(row.get("ts"), "ts")
    attempted_at = _parse_timestamp(attempt.get("ts"), "attempt ts")
    if completed_at < attempted_at:
        raise LedgerError("completion timestamp precedes attempt")
    if row.get("question") != attempt.get("question"):
        raise LedgerError("completion question differs from attempt question")

    state = row.get("forecastState")
    if not isinstance(state, dict) or state.get("sealed") is not True:
        raise LedgerError("forecastState.sealed must be true")
    seat_states = state.get("seats")
    if not isinstance(seat_states, dict):
        raise LedgerError("forecastState.seats must be an object")
    expected_seats = attempt["expectedSeats"]
    if set(seat_states) != set(expected_seats):
        raise LedgerError("forecastState seats must exactly match expectedSeats")
    for seat, value in seat_states.items():
        normalize_seat(seat)
        if value not in SEAT_STATES:
            raise LedgerError(f"invalid forecast seat state for {seat}: {value}")

    predictions = row.get("predictions")
    if not isinstance(predictions, list):
        raise LedgerError("predictions must be a list")
    ids: set[str] = set()
    seats_seen: set[str] = set()
    outcome = attempt["sharedOutcome"]
    for prediction in predictions:
        if not isinstance(prediction, dict):
            raise LedgerError("prediction must be an object")
        prediction_id = _require_id(
            prediction.get("predictionId"), "prediction", "predictionId"
        )
        if prediction_id in ids:
            raise LedgerError(f"duplicate predictionId: {prediction_id}")
        ids.add(prediction_id)
        seat = normalize_seat(prediction.get("seat"))
        if seat in seats_seen:
            raise LedgerError(f"multiple shared predictions for seat: {seat}")
        seats_seen.add(seat)
        if seat_states.get(seat) != "submitted":
            raise LedgerError(f"prediction exists for non-submitted seat: {seat}")
        if prediction.get("type") != "shared":
            raise LedgerError("MVP council predictions must have type=shared")
        if prediction.get("outcomeId") != outcome["outcomeId"]:
            raise LedgerError("prediction outcomeId differs from attempt")
        if prediction.get("claim") != outcome["claim"]:
            raise LedgerError("shared prediction claim differs from attempt")
        if prediction.get("resolutionDate") != outcome["resolutionDate"]:
            raise LedgerError("shared prediction resolutionDate differs from attempt")
        if prediction.get("resolvedBy") != outcome["resolvedBy"]:
            raise LedgerError("shared prediction resolvedBy differs from attempt")
        probability = prediction.get("probability")
        if isinstance(probability, bool) or not isinstance(probability, int):
            raise LedgerError("probability must be an integer")
        if not 0 <= probability <= 100:
            raise LedgerError("probability must be between 0 and 100")
        issued_at = _parse_timestamp(prediction.get("issuedAt"), "issuedAt")
        cutoff = _parse_timestamp(outcome.get("evidenceCutoffAt"), "evidenceCutoffAt")
        deadline = _parse_date(outcome.get("resolutionDate"), "resolutionDate")
        if issued_at < cutoff:
            raise LedgerError("prediction issuedAt precedes evidence cutoff")
        if issued_at > completed_at:
            raise LedgerError("prediction issuedAt follows completion timestamp")
        if issued_at.date() >= deadline:
            raise LedgerError("prediction issuedAt must precede resolutionDate")

    submitted = {seat for seat, value in seat_states.items() if value == "submitted"}
    if submitted != seats_seen:
        missing = sorted(submitted - seats_seen)
        extra = sorted(seats_seen - submitted)
        raise LedgerError(
            f"submitted seat/prediction mismatch; missing={missing} extra={extra}"
        )


def _load_jsonl_bytes_with_raw_identity(
    data: bytes, *, label: str
) -> list[tuple[int, dict[str, Any], str]]:
    rows = []
    for line_number, raw in enumerate(data.splitlines(keepends=True), 1):
        if not raw.strip():
            continue
        try:
            text = raw.decode("utf-8")
            value = strict_json_loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise LedgerError(
                f"{label} line {line_number}: invalid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise LedgerError(f"{label} line {line_number}: record must be an object")
        rows.append((line_number, value, hashlib.sha256(raw).hexdigest()))
    return rows


def load_jsonl_with_raw_identity(
    path: str | Path,
) -> list[tuple[int, dict[str, Any], str]]:
    """Load JSONL records with an identity for their exact durable bytes.

    The digest covers the complete physical line, including its line ending.  It is
    intentionally returned out of band rather than inserted into the decoded value:
    callers can validate the durable record without report-only metadata changing its
    schema.  Capture reporting uses this identity to recognize a byte-exact retry; two
    JSON objects that merely decode to equal mappings remain distinct ledger events.
    """

    path = Path(path)
    if not path.exists():
        return []
    with path.open("rb") as handle:
        return _load_jsonl_bytes_with_raw_identity(handle.read(), label=path.name)


def load_jsonl(path: str | Path) -> list[tuple[int, dict[str, Any]]]:
    """Load JSONL records while preserving the historical two-tuple API."""

    return [
        (line_number, value)
        for line_number, value, _raw_sha256 in load_jsonl_with_raw_identity(path)
    ]


def load_transaction_jsonl_with_raw_identity(
    transaction: PinnedFileTransaction,
) -> list[tuple[int, dict[str, Any], str]]:
    """Read JSONL through the parent dirfd pinned by a ledger transaction."""

    return _load_jsonl_bytes_with_raw_identity(
        transaction.read_bytes(missing_ok=True),
        label=transaction.path.name,
    )


def load_transaction_jsonl(
    transaction: PinnedFileTransaction,
) -> list[tuple[int, dict[str, Any]]]:
    return [
        (line_number, value)
        for line_number, value, _raw_sha256 in load_transaction_jsonl_with_raw_identity(
            transaction
        )
    ]


def _canonical_json_line(value: Mapping[str, Any]) -> str:
    """Serialize one ledger record using strict JSON number semantics."""

    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        )
    except (TypeError, ValueError) as exc:
        raise LedgerError("record must be serializable as strict JSON") from exc


def _fsync_directory(path: Path) -> None:
    try:
        safe_fsync_directory(path)
    except SafeFileError as exc:
        raise LedgerError(str(exc)) from exc


def _append_jsonl_line(
    transaction: PinnedFileTransaction, row: Mapping[str, Any]
) -> None:
    encoded = _canonical_json_line(row).encode("utf-8")
    transaction.append_bytes(encoded)


def atomic_append_transaction_jsonl(
    transaction: PinnedFileTransaction, encoded_line: bytes
) -> Path | None:
    """Crash-atomically append one preflighted JSONL row in a pinned transaction.

    V2 capture owns canonical serialization and secret detection, then calls
    this function while holding :func:`ledger_write_transaction`.
    """

    if not isinstance(encoded_line, bytes) or not encoded_line.endswith(b"\n"):
        raise LedgerError("atomic JSONL append requires one newline-terminated byte row")
    try:
        return transaction.atomic_append_bytes(
            encoded_line,
            require_trailing_newline=True,
        )
    except SafeFileError as exc:
        raise LedgerError(str(exc)) from exc


def transaction_escrow_inventory(
    *paths: str | Path,
) -> dict[str, Any]:
    """Return read-only JSONL escrow custody metadata, never escrow contents."""

    entries: dict[str, dict[str, Any]] = {}
    for raw_path in paths:
        store_path = Path(raw_path).absolute()
        try:
            discovered = safe_inventory_transaction_escrows(store_path)
        except SafeFileError as exc:
            raise LedgerError(str(exc)) from exc
        for escrow in discovered:
            rendered = str(escrow.path)
            entries[rendered] = {
                "storePath": str(store_path),
                "path": rendered,
                "bytes": escrow.size,
                "entryType": escrow.entry_type,
            }
    ordered = [entries[path] for path in sorted(entries)]
    return {
        "count": len(ordered),
        "aggregateBytes": sum(item["bytes"] for item in ordered),
        "entries": ordered,
    }


def derived_ledger_lock_path(path: str | Path) -> Path:
    """Return the exact sibling lock used to serialize one JSONL store."""

    target = Path(path)
    return target.with_name(f"{target.name}.lock")


@contextmanager
def ledger_write_transaction(path: str | Path):
    """Pin ledger parent identity across its sibling lock, read, and mutation."""

    path = Path(path)
    try:
        with safe_locked_file_transaction(
            path,
            timeout_seconds=LOCK_TIMEOUT_SECONDS,
            on_directory_fsync=_fsync_directory,
        ) as transaction:
            yield transaction
    except SafeFileError as exc:
        raise LedgerError(str(exc)) from exc


@contextmanager
def _ledger_lock(path: Path):
    """Compatibility name for V2 callers; yields the pinned transaction."""

    with ledger_write_transaction(path) as transaction:
        yield transaction


@contextmanager
def evidence_write_lock(path: str | Path | None):
    """Take the exact exclusive lock shared by all evidence-store writers.

    Unlike ``_ledger_lock``, this locks the path supplied by the caller rather
    than deriving a per-file lock name.  Evidence snapshots take a shared flock
    on this same inode, producing one coherent cut across the ledger, resolution
    sidecar, control store, and artifact root.
    """

    if path is None:
        yield
        return
    target = Path(path)
    if not target.is_absolute():
        raise LedgerError("evidence coordination lock path must be absolute")
    try:
        with safe_exclusive_lock(
            target,
            timeout_seconds=LOCK_TIMEOUT_SECONDS,
            on_directory_fsync=_fsync_directory,
        ):
            yield
    except SafeFileError as exc:
        raise LedgerError(str(exc)) from exc



RUN_ID_RE = re.compile(r"run-[0-9a-f]{32}")

def validate_blind_brief_identity(
    row: dict[str, Any], prior_rows: Iterable[dict[str, Any]]
) -> None:
    """One immutable run-scoped brief path per council row.

    Any row carrying an explicit ``ran`` state must name the brief that state
    refers to, whether the seat ran or was skipped — a skipped seat was still
    given a question, and the deployed kill criterion has required a brief on
    every explicit-run-state row since before this rule existed. Accepting a
    briefless row here would append a line that the criterion then rejects,
    halting every later council: the same outage through a different door.

    Rows with no ``ran`` key at all are pre-contract legacy and are left alone,
    exactly as the kill criterion leaves them alone.
    """

    blind_seat = row.get("blindSeat")
    if not isinstance(blind_seat, dict):
        raise LedgerError("new council completion requires blindSeat object")
    if "ran" not in blind_seat:
        return
    run_id = row.get("runId")
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise LedgerError("new council completion requires a well-formed runId")
    brief = blind_seat.get("brief")
    if not isinstance(brief, str) or not brief.strip():
        raise LedgerError("new council completion requires a blind brief path")
    brief = brief.strip()
    brief_path = Path(brief)
    if not brief_path.is_absolute() or brief_path != Path(os.path.abspath(brief_path)):
        raise LedgerError("new council blind brief path must be absolute and normalized")
    if run_id not in brief_path.name:
        raise LedgerError("new council blind brief path must contain its exact runId")
    resolved_brief = brief_path.resolve(strict=False)
    for prior in prior_rows:
        prior_seat = prior.get("blindSeat")
        if not isinstance(prior_seat, dict):
            continue
        prior_brief = prior_seat.get("brief")
        if not isinstance(prior_brief, str) or not prior_brief.strip():
            continue
        prior_path = Path(prior_brief.strip())
        aliases = prior_path.resolve(strict=False) == resolved_brief
        if not aliases and prior_path.exists() and brief_path.exists():
            try:
                aliases = prior_path.samefile(brief_path)
            except OSError:
                aliases = False
        if aliases:
            raise LedgerError(
                f"blind brief path already belongs to another council: {brief}"
            )


def validate_ledger_row(row: dict[str, Any], prior_rows: Iterable[dict[str, Any]]) -> None:
    # This is also the CLI's check-only boundary. Serializing here prevents
    # otherwise unvalidated nested council metadata from poisoning the ledger
    # with Python's non-standard NaN/Infinity tokens.
    _canonical_json_line(row)
    prior_rows = list(prior_rows)
    kind = row.get("kind")
    if kind == "council-attempt":
        validate_attempt(row)
        run_id = row["runId"]
        if any(item.get("runId") == run_id for item in prior_rows):
            raise LedgerError(f"duplicate runId: {run_id}")
        outcome = row["sharedOutcome"]
        outcome_id = outcome["outcomeId"]
        if any(
            item.get("kind") == "council-attempt"
            and item.get("sharedOutcome", {}).get("outcomeId") == outcome_id
            for item in prior_rows
        ):
            raise LedgerError(f"duplicate outcomeId: {outcome_id}")
        for item in prior_rows:
            if item.get("kind") != "council-attempt":
                continue
            other = item.get("sharedOutcome", {})
            if other.get("fingerprint") != outcome.get("fingerprint"):
                continue
            if other.get("outcomeId") not in outcome.get("relatedOutcomeIds", []):
                raise LedgerError(
                    "outcome fingerprint already exists; link it in relatedOutcomeIds"
                )
    elif kind == "council":
        validate_completion(row, prior_rows)
        validate_blind_brief_identity(row, prior_rows)
        existing_prediction_ids = {
            prediction.get("predictionId")
            for item in prior_rows
            for prediction in (item.get("predictions") or [])
            if isinstance(prediction, dict)
        }
        for prediction in row["predictions"]:
            if prediction["predictionId"] in existing_prediction_ids:
                raise LedgerError(
                    f"duplicate predictionId: {prediction['predictionId']}"
                )
    else:
        raise LedgerError(f"record command does not accept kind: {kind}")


def append_ledger_row(
    path: str | Path,
    row: dict[str, Any],
    *,
    coordination_lock: str | Path | None = None,
) -> None:
    path = Path(path)
    with evidence_write_lock(coordination_lock):
        with ledger_write_transaction(path) as transaction:
            prior = [
                row_value for _, row_value in load_transaction_jsonl(transaction)
            ]
            validate_ledger_row(row, prior)
            _append_jsonl_line(transaction, row)


def _validate_resolution_event(
    event: dict[str, Any], prior_events: Iterable[dict[str, Any]]
) -> None:
    required_keys = {
        "schemaVersion",
        "kind",
        "resolutionId",
        "outcomeId",
        "resolutionDate",
        "status",
        "cameTrue",
        "voidReason",
        "evidence",
        "resolver",
        "reviewer",
        "method",
        "resolvedAt",
        "supersedesResolutionId",
    }
    optional_keys = {"outcomeFingerprint"}
    if not isinstance(event, dict) or not required_keys.issubset(event) or (
        set(event) - required_keys - optional_keys
    ):
        raise LedgerError(
            "resolution event must contain the exact resolution schema with optional "
            "outcomeFingerprint"
        )
    if event.get("schemaVersion") != SCHEMA_VERSION:
        raise LedgerError("resolution schemaVersion must be 1")
    if event.get("kind") != "outcome-resolution":
        raise LedgerError("resolution kind must be outcome-resolution")
    _require_id(event.get("resolutionId"), "resolution", "resolutionId")
    outcome_id = _require_id(event.get("outcomeId"), "outcome", "outcomeId")
    fingerprint = event.get("outcomeFingerprint")
    if fingerprint is not None and (
        not isinstance(fingerprint, str)
        or OUTCOME_FINGERPRINT_RE.fullmatch(fingerprint) is None
    ):
        raise LedgerError("outcomeFingerprint must be a lowercase SHA-256 digest")
    resolved_at = _parse_timestamp(event.get("resolvedAt"), "resolvedAt")
    resolution_date = _parse_date(event.get("resolutionDate"), "resolutionDate")
    _require_text(event.get("evidence"), "evidence")
    resolver = _require_text(event.get("resolver"), "resolver")
    method = _require_text(event.get("method"), "method")
    if method not in RESOLUTION_METHODS:
        raise LedgerError(f"method must be one of {sorted(RESOLUTION_METHODS)}")
    reviewer = event.get("reviewer")
    if method == "manual-reviewed":
        reviewer = _require_text(reviewer, "reviewer")
        if reviewer == resolver:
            raise LedgerError("manual resolution reviewer must differ from resolver")
    elif reviewer not in (None, ""):
        raise LedgerError("deterministic resolution must not name a reviewer")

    status = _require_text(event.get("status"), "status")
    void_reason = event.get("voidReason")
    if void_reason is not None:
        void_reason = _require_text(void_reason, "voidReason")
    if status == "resolved":
        if not isinstance(event.get("cameTrue"), bool):
            raise LedgerError("resolved outcome requires Boolean cameTrue")
        if void_reason not in (None, ""):
            raise LedgerError("resolved outcome cannot have voidReason")
        if resolved_at.astimezone(ZoneInfo("America/New_York")).date() <= resolution_date:
            raise LedgerError(
                "non-void resolution must be recorded after resolutionDate in America/New_York"
            )
    elif status == "void":
        if event.get("cameTrue") is not None:
            raise LedgerError("void outcome requires cameTrue=null")
        if void_reason not in VOID_REASONS:
            raise LedgerError(f"voidReason must be one of {sorted(VOID_REASONS)}")
        if method != "manual-reviewed":
            raise LedgerError("void outcome requires manual-reviewed method")
    else:
        raise LedgerError("resolution status must be resolved or void")

    prior_events = [item for item in prior_events if item.get("kind") == "outcome-resolution"]
    if any(item.get("resolutionId") == event["resolutionId"] for item in prior_events):
        raise LedgerError(f"duplicate resolutionId: {event['resolutionId']}")
    previous = [item for item in prior_events if item.get("outcomeId") == outcome_id]
    supersedes = event.get("supersedesResolutionId")
    if previous:
        latest = previous[-1]
        if supersedes != latest.get("resolutionId"):
            raise LedgerError("resolution correction must supersede the latest resolution")
    elif supersedes not in (None, ""):
        raise LedgerError("first resolution cannot supersede another resolution")


def append_resolution(
    path: str | Path,
    *,
    outcome_id: str,
    resolution_date: str,
    outcome_fingerprint: str,
    came_true: bool | None,
    evidence: str,
    resolver: str,
    resolved_at: str,
    method: str,
    reviewer: str | None = None,
    void_reason: str | None = None,
    supersedes_resolution_id: str | None = None,
    resolution_id: str | None = None,
    coordination_lock: str | Path | None = None,
    _row_writer: Callable[[PinnedFileTransaction, Mapping[str, Any]], None]
    | None = None,
    _transaction: PinnedFileTransaction | None = None,
) -> dict[str, Any]:
    status = "void" if void_reason is not None else "resolved"
    event = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "outcome-resolution",
        "resolutionId": resolution_id or new_id("resolution"),
        "outcomeId": outcome_id,
        "resolutionDate": resolution_date,
        "status": status,
        "cameTrue": came_true,
        "voidReason": void_reason,
        "evidence": evidence,
        "resolver": resolver,
        "reviewer": reviewer,
        "method": method,
        "resolvedAt": resolved_at,
        "supersedesResolutionId": supersedes_resolution_id,
    }
    event["outcomeFingerprint"] = outcome_fingerprint
    path = Path(path)

    def validate_and_append(transaction: PinnedFileTransaction) -> None:
        prior = [item for _, item in load_transaction_jsonl(transaction)]
        _validate_resolution_event(event, prior)
        if _row_writer is None:
            _append_jsonl_line(transaction, event)
        else:
            _row_writer(transaction, event)

    if _transaction is not None:
        if coordination_lock is not None:
            raise LedgerError(
                "supplied resolution transaction requires coordination_lock=None"
            )
        if path.absolute() != _transaction.path.absolute():
            raise LedgerError("supplied resolution transaction does not match event path")
        validate_and_append(_transaction)
    else:
        with evidence_write_lock(coordination_lock):
            with ledger_write_transaction(path) as transaction:
                validate_and_append(transaction)
    return event


def append_override(
    path: str | Path,
    *,
    reason: str,
    operator: str,
    created_at: str,
    expires_date: str,
    override_id: str | None = None,
    coordination_lock: str | Path | None = None,
) -> dict[str, Any]:
    created = _parse_timestamp(created_at, "createdAt")
    expires = _parse_date(expires_date, "expiresDate")
    if expires < created.astimezone(ZoneInfo("America/New_York")).date():
        raise LedgerError("override expiresDate precedes createdAt")
    event = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "grading-debt-override",
        "overrideId": override_id or new_id("override"),
        "reason": _require_text(reason, "reason"),
        "operator": _require_text(operator, "operator"),
        "createdAt": created_at,
        "expiresDate": expires_date,
    }
    _require_id(event["overrideId"], "override", "overrideId")
    path = Path(path)
    with evidence_write_lock(coordination_lock):
        with ledger_write_transaction(path) as transaction:
            prior = [item for _, item in load_transaction_jsonl(transaction)]
            if any(item.get("overrideId") == event["overrideId"] for item in prior):
                raise LedgerError(f"duplicate overrideId: {event['overrideId']}")
            _append_jsonl_line(transaction, event)
    return event


def repair_trailing_jsonl(
    path: str | Path,
    *,
    expected_line: int,
    backup_dir: str | Path,
    coordination_lock: str | Path | None = None,
) -> dict[str, Any]:
    """Quarantine and remove exactly one confirmed invalid final nonblank line.

    The function refuses missing files, valid files, earlier corruption, line-number drift,
    and multiple corrupt lines. It uses the ledger's append lock so an operator cannot repair
    a snapshot while another process is appending to it.
    """

    path = Path(path)
    backup_dir = Path(backup_dir)
    if isinstance(expected_line, bool) or not isinstance(expected_line, int):
        raise LedgerError("expected_line must be a positive integer")
    if expected_line < 1:
        raise LedgerError("expected_line must be a positive integer")
    with evidence_write_lock(coordination_lock), ledger_write_transaction(
        path
    ) as transaction:
        try:
            original = transaction.read_bytes()
        except SafeFileError as exc:
            raise LedgerError(str(exc)) from exc
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LedgerError("ledger is not valid UTF-8; automatic repair refused") from exc

        lines = text.splitlines(keepends=True)
        invalid: list[tuple[int, str]] = []
        nonblank_lines = [
            line_number
            for line_number, raw in enumerate(lines, 1)
            if raw.strip()
        ]
        for line_number, raw in enumerate(lines, 1):
            if not raw.strip():
                continue
            try:
                value = strict_json_loads(raw)
            except (json.JSONDecodeError, ValueError):
                invalid.append((line_number, "invalid JSON"))
                continue
            if not isinstance(value, dict):
                invalid.append((line_number, "record must be an object"))

        if not invalid:
            raise LedgerError("ledger has no invalid line to repair")
        if len(invalid) != 1:
            raise LedgerError("ledger has multiple invalid lines; automatic repair refused")
        invalid_line, reason = invalid[0]
        if not nonblank_lines or invalid_line != nonblank_lines[-1]:
            raise LedgerError(
                f"invalid line {invalid_line} is not the final nonblank line; repair refused"
            )
        if invalid_line != expected_line:
            raise LedgerError(
                f"confirmed line {expected_line} does not match invalid final line "
                f"{invalid_line}"
            )

        digest = hashlib.sha256(original).hexdigest()
        backup = backup_dir / (
            f"{path.name}.quarantine.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}."
            f"{digest[:12]}"
        )
        try:
            safe_create_bytes_exclusive(
                backup,
                original,
                on_directory_fsync=_fsync_directory,
            )
        except SafeFileError as exc:
            raise LedgerError(str(exc)) from exc

        repaired = "".join(
            raw for line_number, raw in enumerate(lines, 1) if line_number != invalid_line
        ).encode("utf-8")
        try:
            transaction_escrow = transaction.atomic_replace_bytes(
                repaired,
                expected_sha256=digest,
                require_existing=True,
            )
        except SafeFileError as exc:
            raise LedgerError(str(exc)) from exc
    return {
        "path": str(path),
        "removedLine": invalid_line,
        "reason": reason,
        "backup": str(backup),
        "originalSha256": digest,
        "transactionEscrow": (
            None if transaction_escrow is None else str(transaction_escrow)
        ),
    }


def brier_score(probability: int, came_true: bool) -> float:
    if isinstance(probability, bool) or not isinstance(probability, int):
        raise LedgerError("probability must be an integer")
    if not 0 <= probability <= 100:
        raise LedgerError("probability must be between 0 and 100")
    if not isinstance(came_true, bool):
        raise LedgerError("came_true must be Boolean")
    p = probability / 100.0
    outcome = 1.0 if came_true else 0.0
    return (p - outcome) ** 2


def _legacy_prediction(
    row: dict[str, Any], prediction: dict[str, Any], index: int
) -> dict[str, Any]:
    seat = normalize_seat(prediction.get("seat"))
    claim = _require_text(prediction.get("claim"), "claim")
    resolution_date = str(_parse_date(prediction.get("resolutionDate"), "resolutionDate"))
    resolved_by = _require_text(prediction.get("resolvedBy"), "resolvedBy")
    probability = prediction.get("probability")
    if isinstance(probability, bool) or not isinstance(probability, int):
        raise LedgerError("probability must be an integer")
    if not 0 <= probability <= 100:
        raise LedgerError("probability must be between 0 and 100")
    outcome_id = _legacy_id("outcome", claim, resolution_date, resolved_by)
    prediction_id = _legacy_id(
        "prediction", row.get("ts"), row.get("question"), seat, claim, index
    )
    issued_at = row.get("ts") or "1970-01-01T00:00:00Z"
    _parse_timestamp(issued_at, "legacy prediction row ts")
    return {
        "predictionId": prediction_id,
        "outcomeId": outcome_id,
        "runId": row.get("runId") or _legacy_id(
            "run", row.get("ts"), row.get("question"), row.get("kind")
        ),
        "seat": seat,
        "type": prediction.get("type") or "legacy",
        "claim": claim,
        "probability": probability,
        "issuedAt": issued_at,
        "resolutionDate": resolution_date,
        "resolvedBy": resolved_by,
        "sourceKind": row.get("kind"),
        "legacy": True,
    }


def _validate_excluded_prediction(prediction: dict[str, Any]) -> None:
    """Validate metadata without imposing the council's seat vocabulary."""
    _require_text(prediction.get("seat"), "seat")
    _require_text(prediction.get("claim"), "claim")
    _parse_date(prediction.get("resolutionDate"), "resolutionDate")
    _require_text(prediction.get("resolvedBy"), "resolvedBy")
    probability = prediction.get("probability")
    if isinstance(probability, bool) or not isinstance(probability, int):
        raise LedgerError("probability must be an integer")
    if not 0 <= probability <= 100:
        raise LedgerError("probability must be between 0 and 100")


def _excluded_prediction(
    row: dict[str, Any], prediction: dict[str, Any], index: int
) -> dict[str, Any]:
    _validate_excluded_prediction(prediction)
    seat = _require_text(prediction.get("seat"), "seat")
    claim = prediction["claim"].strip()
    resolution_date = str(_parse_date(prediction["resolutionDate"], "resolutionDate"))
    resolved_by = prediction["resolvedBy"].strip()
    return {
        "predictionId": prediction.get("predictionId")
        or _legacy_id(
            "prediction", row.get("ts"), row.get("kind"), seat, claim, index
        ),
        "outcomeId": prediction.get("outcomeId")
        or _legacy_id("outcome", claim, resolution_date, resolved_by),
        "seat": seat,
        "claim": claim,
        "probability": prediction["probability"],
        "resolutionDate": resolution_date,
        "resolvedBy": resolved_by,
        "sourceKind": row.get("kind"),
    }


def _strict_prediction(row: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]:
    return {
        **prediction,
        "runId": row["runId"],
        "seat": normalize_seat(prediction.get("seat")),
        "sourceKind": "council",
        "legacy": False,
    }


def _load_events(
    path: str | Path,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    resolutions: dict[str, dict[str, Any]] = {}
    overrides = []
    prior = []
    for _, event in load_jsonl(path):
        kind = event.get("kind")
        if kind == "outcome-resolution":
            _validate_resolution_event(event, prior)
            resolutions[event["outcomeId"]] = event
        elif kind == "grading-debt-override":
            _require_id(event.get("overrideId"), "override", "overrideId")
            _require_text(event.get("reason"), "reason")
            _require_text(event.get("operator"), "operator")
            _parse_timestamp(event.get("createdAt"), "createdAt")
            _parse_date(event.get("expiresDate"), "expiresDate")
            overrides.append(event)
        else:
            raise LedgerError(f"unknown forecast event kind: {kind}")
        prior.append(event)
    return resolutions, overrides


def _legacy_expected_seats(row: dict[str, Any]) -> set[str]:
    expected = {"code", "theory", "ops"}
    blind = row.get("blindSeat")
    if isinstance(blind, dict) and blind.get("ran") is True:
        expected.add("blind")
    return expected


def audit(
    log_path: str | Path,
    events_path: str | Path,
    *,
    today: date | None = None,
    as_of: datetime | str | None = None,
) -> dict[str, Any]:
    if as_of is not None:
        if isinstance(as_of, datetime):
            if as_of.tzinfo is None or as_of.utcoffset() is None:
                raise LedgerError("as_of must include a timezone")
            report_as_of = as_of.astimezone(timezone.utc)
        else:
            report_as_of = _parse_timestamp(as_of, "as_of")
        observed_today = report_as_of.astimezone(
            ZoneInfo("America/New_York")
        ).date()
        if today is not None and today != observed_today:
            raise LedgerError("today must match as_of in America/New_York")
        today = observed_today
    elif today is not None:
        # Explicit dates remain a deterministic local-test API and represent
        # the complete New York reporting day.
        report_as_of = datetime.combine(
            today,
            datetime_time.max,
            tzinfo=ZoneInfo("America/New_York"),
        ).astimezone(timezone.utc)
    else:
        report_as_of = datetime.now(timezone.utc)
        today = report_as_of.astimezone(ZoneInfo("America/New_York")).date()
    numbered_rows = load_jsonl(log_path)
    rows = [row for _, row in numbered_rows]
    resolutions, overrides = _load_events(events_path)

    attempts_by_run: dict[str, dict[str, Any]] = {}
    completions_by_run: dict[str, dict[str, Any]] = {}
    predictions: list[dict[str, Any]] = []
    excluded_valid: list[dict[str, Any]] = []
    invalid_records: list[str] = []
    legacy_ineligible: list[dict[str, Any]] = []
    excluded_predictions = 0
    missing_forecast_seats = []
    council_rows = 0
    council_rows_with_predictions = 0
    complete_forecast_rows = 0
    seat_emission_states = {
        seat: {state: 0 for state in sorted(SEAT_STATES)}
        for seat in CANONICAL_SEATS
    }

    prior_rows: list[dict[str, Any]] = []
    for line_number, row in numbered_rows:
        kind = row.get("kind")
        if kind == "council-attempt":
            validate_ledger_row(row, prior_rows)
            attempts_by_run[row["runId"]] = row
        elif kind == "council":
            council_rows += 1
            raw_predictions = row.get("predictions") or []
            if raw_predictions:
                council_rows_with_predictions += 1
            contract_row = (
                row.get("schemaVersion") == SCHEMA_VERSION and row.get("runId")
            )
            if contract_row:
                validate_completion(row, prior_rows)
                completions_by_run[row["runId"]] = row
                expected = set(attempts_by_run[row["runId"]]["expectedSeats"])
                states = row["forecastState"]["seats"]
                prediction_required = {
                    seat for seat, state in states.items() if state == "submitted"
                }
                for seat, state in states.items():
                    seat_emission_states[seat][state] += 1
            else:
                expected = _legacy_expected_seats(row)
                prediction_required = expected
            seen = set()
            for index, raw_prediction in enumerate(raw_predictions):
                try:
                    prediction = (
                        _strict_prediction(row, raw_prediction)
                        if row.get("schemaVersion") == SCHEMA_VERSION and row.get("runId")
                        else _legacy_prediction(row, raw_prediction, index)
                    )
                    seen.add(prediction["seat"])
                    predictions.append(prediction)
                except LedgerError as exc:
                    legacy_ineligible.append(
                        {
                            "line": line_number,
                            "index": index,
                            "kind": kind,
                            "reason": str(exc),
                        }
                    )
            missing = sorted(prediction_required - seen)
            if missing:
                missing_forecast_seats.append(
                    {
                        "line": line_number,
                        "runId": row.get("runId"),
                        "missingSeats": missing,
                    }
                )
            if contract_row and not missing:
                complete_forecast_rows += 1
            elif expected and seen == expected:
                complete_forecast_rows += 1
        else:
            raw_predictions = row.get("predictions") or []
            excluded_predictions += len(raw_predictions)
            for index, raw_prediction in enumerate(raw_predictions):
                try:
                    excluded_valid.append(
                        _excluded_prediction(row, raw_prediction, index)
                    )
                except LedgerError as exc:
                    legacy_ineligible.append(
                        {
                            "line": line_number,
                            "index": index,
                            "kind": kind,
                            "reason": str(exc),
                        }
                    )
        prior_rows.append(row)

    prediction_ids = set()
    for prediction in predictions:
        prediction_id = prediction["predictionId"]
        if prediction_id in prediction_ids:
            invalid_records.append(f"duplicate predictionId: {prediction_id}")
        prediction_ids.add(prediction_id)

    # The earliest issuance is the headline forecast for a seat/outcome. Later entries remain
    # in issuance counts and are visible as repeats, but cannot replace the pre-evidence price.
    representatives: dict[tuple[str, str], dict[str, Any]] = {}
    repeated_issuances = 0
    for prediction in sorted(
        predictions,
        key=lambda item: (
            _parse_timestamp(item["issuedAt"], "issuedAt"),
            item["predictionId"],
        ),
    ):
        key = (prediction["seat"], prediction["outcomeId"])
        if key in representatives:
            repeated_issuances += 1
            continue
        representatives[key] = prediction

    outcome_predictions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for prediction in representatives.values():
        outcome_predictions[prediction["outcomeId"]].append(prediction)

    issued_outcomes: dict[str, tuple[dict[str, Any], str]] = {}
    for run_id, attempt_row in attempts_by_run.items():
        completion_row = completions_by_run.get(run_id)
        if completion_row is None:
            continue
        canonical_outcome = attempt_row["sharedOutcome"]
        outcome_id = canonical_outcome["outcomeId"]
        if outcome_id in outcome_predictions:
            issued_outcomes[outcome_id] = (
                canonical_outcome,
                completion_row["ts"],
            )

    unknown_resolution_ids = sorted(set(resolutions) - set(outcome_predictions))
    if unknown_resolution_ids:
        invalid_records.extend(
            f"resolution references unknown outcomeId: {item}"
            for item in unknown_resolution_ids
        )

    valid_resolutions: dict[str, dict[str, Any]] = {}
    for outcome_id, event in resolutions.items():
        issued = issued_outcomes.get(outcome_id)
        if issued is None:
            continue
        canonical_outcome, issuance_at = issued
        try:
            validate_resolution_event_integrity(
                event,
                canonical_outcome,
                issuance_at=issuance_at,
                as_of=report_as_of,
                # Events created before the binding field was introduced remain
                # readable. Every current writer supplies the field.
                require_outcome_fingerprint="outcomeFingerprint" in event,
            )
        except ResolutionIntegrityError as exc:
            invalid_records.append(
                f"resolution integrity failed for {outcome_id}: {exc}"
            )
            continue
        valid_resolutions[outcome_id] = event
    resolutions = valid_resolutions

    due_outcomes = set()
    old_overdue = set()
    for outcome_id, items in outcome_predictions.items():
        deadline = _parse_date(items[0]["resolutionDate"], "resolutionDate")
        if deadline <= today:
            due_outcomes.add(outcome_id)
            if outcome_id not in resolutions and (today - deadline).days > 14:
                old_overdue.add(outcome_id)

    resolved_outcomes = {
        outcome_id
        for outcome_id, event in resolutions.items()
        if outcome_id in outcome_predictions and event.get("status") == "resolved"
    }
    void_outcomes = {
        outcome_id
        for outcome_id, event in resolutions.items()
        if outcome_id in outcome_predictions and event.get("status") == "void"
    }
    unresolved_due = due_outcomes - resolved_outcomes - void_outcomes

    active_override = any(
        _parse_timestamp(item["createdAt"], "createdAt")
        .astimezone(ZoneInfo("America/New_York"))
        .date()
        <= today
        <= _parse_date(item["expiresDate"], "expiresDate")
        for item in overrides
    )
    if len(old_overdue) >= 3:
        debt_state = "OVERRIDDEN" if active_override else "BLOCK_FINALIZATION"
    elif unresolved_due:
        debt_state = "WARN"
    else:
        debt_state = "CLEAN"

    seat_scores = []
    by_seat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for prediction in representatives.values():
        by_seat[prediction["seat"]].append(prediction)
    for seat in CANONICAL_SEATS:
        items = by_seat.get(seat, [])
        resolved_items = [
            item for item in items if item["outcomeId"] in resolved_outcomes
        ]
        scores = [
            brier_score(
                item["probability"], resolutions[item["outcomeId"]]["cameTrue"]
            )
            for item in resolved_items
        ]
        due_for_seat = {
            item["outcomeId"]
            for item in items
            if _parse_date(item["resolutionDate"], "resolutionDate") <= today
        }
        unresolved_for_seat = due_for_seat - resolved_outcomes - void_outcomes
        if scores:
            mean_probability = sum(item["probability"] for item in resolved_items) / len(
                resolved_items
            )
            event_rate = sum(
                1 for item in resolved_items if resolutions[item["outcomeId"]]["cameTrue"]
            ) / len(resolved_items)
            mean_brier = sum(scores) / len(scores)
        else:
            mean_probability = None
            event_rate = None
            mean_brier = None
        seat_scores.append(
            {
                "seat": seat,
                "n": len(scores),
                "brier": mean_brier,
                "constantFiftyBrier": 0.25 if scores else None,
                "inSampleBaseRateBrier": (
                    event_rate * (1.0 - event_rate)
                    if event_rate is not None
                    else None
                ),
                "meanProbability": mean_probability,
                "eventRate": event_rate,
                "dueOutcomes": len(due_for_seat),
                "unresolvedDueOutcomes": len(unresolved_for_seat),
                "scoreStatus": "INCOMPLETE" if unresolved_for_seat else "DESCRIPTIVE",
            }
        )

    score_status = (
        "INCOMPLETE"
        if unresolved_due
        else "DESCRIPTIVE"
        if resolved_outcomes
        else "NO_RESOLVED_OUTCOMES"
    )
    shared_outcomes = sum(
        1
        for items in outcome_predictions.values()
        if len({item["seat"] for item in items if item.get("type") == "shared"}) >= 2
    )
    orphan_attempts = sorted(set(attempts_by_run) - set(completions_by_run))
    all_states = {
        "future": 0,
        "due": 0,
        "resolved": 0,
        "void": 0,
        "legacyIneligible": len(legacy_ineligible),
    }
    for prediction in [*predictions, *excluded_valid]:
        event = resolutions.get(prediction["outcomeId"])
        if event and event.get("status") == "resolved":
            all_states["resolved"] += 1
        elif event and event.get("status") == "void":
            all_states["void"] += 1
        elif _parse_date(prediction["resolutionDate"], "resolutionDate") <= today:
            all_states["due"] += 1
        else:
            all_states["future"] += 1

    return {
        "scope": "council",
        "transactionEscrows": transaction_escrow_inventory(
            log_path, events_path
        ),
        "rawPredictions": sum(len(row.get("predictions") or []) for row in rows),
        "forecastIssuances": len(predictions),
        "representativeForecasts": len(representatives),
        "repeatedIssuances": repeated_issuances,
        "uniqueOutcomes": len(outcome_predictions),
        "knownOutcomeIds": sorted(outcome_predictions),
        "outcomeResolutionDates": {
            outcome_id: items[0]["resolutionDate"]
            for outcome_id, items in sorted(outcome_predictions.items())
        },
        "outcomeFingerprints": {
            outcome_id: issued_outcomes[outcome_id][0]["fingerprint"]
            for outcome_id in sorted(outcome_predictions)
            if outcome_id in issued_outcomes
        },
        "sharedOutcomes": shared_outcomes,
        "excludedPredictions": excluded_predictions,
        "excludedValidPredictions": len(excluded_valid),
        "allPredictionStates": all_states,
        "legacyIneligiblePredictions": legacy_ineligible,
        "attempts": len(attempts_by_run),
        "orphanAttempts": orphan_attempts,
        "councilRows": council_rows,
        "councilRowsWithPredictions": council_rows_with_predictions,
        "completeForecastRows": complete_forecast_rows,
        "missingForecastSeats": missing_forecast_seats,
        "seatEmissionStates": seat_emission_states,
        "eligibleDueOutcomes": len(due_outcomes),
        "resolvedOutcomes": len(resolved_outcomes),
        "voidOutcomes": len(void_outcomes),
        "voidRateOfEligibleOutcomes": (
            len(void_outcomes) / len(due_outcomes | void_outcomes)
            if due_outcomes or void_outcomes
            else None
        ),
        "unresolvedDueOutcomes": len(unresolved_due),
        "oldOverdueOutcomes": len(old_overdue),
        "gradingDebtState": debt_state,
        "scoreStatus": score_status,
        "seatScores": seat_scores,
        "invalidRecords": invalid_records,
        "label": (
            "DESCRIPTIVE ONLY - normal seat operation has unequal information access; "
            "outcomes may be seat-controlled and are non-independent"
        ),
    }
