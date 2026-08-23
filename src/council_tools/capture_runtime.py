"""Atomic integration for the prospective council V2 capture lifecycle.

The schema, artifact store, finding normalizer, and data-health analyzer are kept
independent so each can be tested adversarially.  This module is the deliberately
small orchestration boundary: it constructs system timestamps while holding the
ledger lock, verifies every referenced artifact before committing a reference,
and keeps V2 outcome resolutions out of the V1 sidecar.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import forecasts as forecasts_module
from .artifacts import (
    ArtifactError,
    ArtifactStore,
    secret_detectors,
    validate_artifact_ref,
    verify_git_blob_oid,
)
from .capture_schema import (
    CaptureSchemaError,
    V2_KINDS,
    blind_brief_identity,
    forecast_request_block_v2,
    finalize_council_v2,
    forecast_request_binding_v2,
    forecast_request_identity_v2,
    make_capture_activation,
    make_capture_initiation,
    make_capture_invalidation,
    make_council_attempt_v2,
    make_council_seats_finished,
    prepare_council_v2,
    raw_payload_secret_detectors,
    parse_forecast_request_binding_v2,
    seat_input_manifest_sha256,
    strict_json_loads,
    validate_v2_record,
    validate_v2_ledger,
)
from .data_health import (
    RAW_RECORD_SHA256_ANNOTATION,
    analyze_capture_data,
    finding_summary_record_key,
)
from .findings import (
    FindingError,
    summarize_findings,
    validate_visible_output_findings,
)
from .forecasts import (
    LOCK_TIMEOUT_SECONDS,
    LedgerError,
    atomic_append_transaction_jsonl,
    append_resolution,
    evidence_write_lock,
    ledger_write_transaction,
    load_jsonl,
    load_transaction_jsonl,
    transaction_escrow_inventory,
)
from .safe_files import (
    DirectoryIdentity,
    PinnedFileTransaction,
    PinnedMutationTarget,
    SafeFileError,
    capture_directory_identity,
    exclusive_lock,
    locked_file_transaction,
)
from .resolution_integrity import (
    ResolutionIntegrityError,
    validate_resolution_event_integrity,
)


Clock = Callable[[], datetime | str]


class CaptureRuntimeError(ValueError):
    """The integrated capture workflow cannot preserve its frozen contract."""


class _RetainedArtifactParseError(CaptureRuntimeError):
    """A retained artifact could not be parsed at a public trust boundary.

    Parser exceptions can include duplicate keys or snippets copied from bytes
    supplied by the caller.  Keep those diagnostics in the exception chain for
    local debugging, but expose only one fixed category to writers, reports,
    and the CLI.
    """


_PROMPT_PARSE_FAILURE = "retained prompt parse failure"
_OUTPUT_PARSE_FAILURE = "retained output parse failure"
_BASELINE_PARSE_FAILURE = "retained baseline parse failure"
_V2_TYPE_FAILURE = "invalid V2 record field type"


class CaptureSecretDetectedError(CaptureRuntimeError):
    """Canonical capture bytes contained a recognized secret pattern.

    Only detector labels are retained. The matching bytes are never included in
    the exception or in the incident record.
    """

    def __init__(self, detectors: Iterable[str]):
        self.detectors = tuple(dict.fromkeys(detectors))
        super().__init__(
            "capture rejected by secret preflight: " + ",".join(self.detectors)
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _os_account_live_write_roots() -> tuple[Path, ...]:
    """Return every live runtime root without trusting caller-controlled HOME."""

    account_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    return (
        (account_home / ".claude/knowledge").resolve(),
        (account_home / ".local/state/council-tools").resolve(),
        (account_home / "council-tools").resolve(),
    )


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_live_activation_target(
    path: str | Path, roots: Iterable[Path]
) -> bool:
    lexical = Path(os.path.abspath(os.fspath(path)))
    try:
        resolved = lexical.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise CaptureRuntimeError(
            f"cannot authorize unresolved activation target: {lexical}"
        ) from exc
    return any(
        _path_is_within(candidate, root)
        for candidate in (lexical, resolved)
        for root in roots
    )


def _capture_live_root_identities(
    roots: Iterable[Path],
) -> tuple[DirectoryIdentity, ...]:
    """Capture existing protected roots before caller-controlled mutation."""

    identities: list[DirectoryIdentity] = []
    for root in roots:
        try:
            identity = capture_directory_identity(root)
        except SafeFileError:
            # A missing optional runtime root cannot be renamed into the target;
            # lexical/resolved containment still protects its future pathname.
            continue
        if identity not in identities:
            identities.append(identity)
    return tuple(identities)


def _activation_mutation_authorizer(
    protected_roots: tuple[DirectoryIdentity, ...],
    *,
    expected_parent: DirectoryIdentity | None,
    expected_target: tuple[int, int] | None,
    expected_target_present: bool,
) -> Callable[[PinnedMutationTarget], None]:
    """Reject activation into a protected inode at the actual mutation cut."""

    def authorize(target: PinnedMutationTarget) -> None:
        if any(target.parent_is_within(root) for root in protected_roots):
            raise CaptureRuntimeError(
                "direct live capture activation is blocked in this release"
            )
        if expected_parent is not None and target.parent_identity != expected_parent:
            raise CaptureRuntimeError(
                "capture activation mutation parent changed after authorization"
            )
        actual_target = (
            None
            if target.target_identity is None
            else (target.target_identity.device, target.target_identity.inode)
        )
        if expected_target_present != (actual_target is not None):
            raise CaptureRuntimeError(
                "capture activation target changed after authorization"
            )
        if expected_target_present and actual_target != expected_target:
            raise CaptureRuntimeError(
                "capture activation target changed after authorization"
            )

    return authorize


def _activation_namespace_authorizer(
    path: str | Path,
    protected_roots: tuple[DirectoryIdentity, ...],
) -> Callable[[PinnedMutationTarget], None]:
    """Freeze preflight namespace identity for one activation mutation path."""

    target = Path(os.path.abspath(os.fspath(path)))
    try:
        expected_parent = capture_directory_identity(target.parent)
    except SafeFileError:
        expected_parent = None
    try:
        target_info = os.lstat(target)
    except FileNotFoundError:
        expected_target_present = False
        expected_target = None
    except OSError as exc:
        raise CaptureRuntimeError(
            "cannot authorize capture activation target identity"
        ) from exc
    else:
        expected_target_present = True
        expected_target = (target_info.st_dev, target_info.st_ino)
    return _activation_mutation_authorizer(
        protected_roots,
        expected_parent=expected_parent,
        expected_target=expected_target,
        expected_target_present=expected_target_present,
    )


@contextmanager
def _activation_coordination_lock(
    path: str | Path | None,
    *,
    authorize_mutation: Callable[[PinnedMutationTarget], None] | None,
):
    if path is None:
        yield
        return
    try:
        with exclusive_lock(
            path,
            timeout_seconds=LOCK_TIMEOUT_SECONDS,
            on_directory_fsync=forecasts_module._fsync_directory,
            authorize_mutation=authorize_mutation,
        ):
            yield
    except SafeFileError as exc:
        raise CaptureRuntimeError(
            f"capture coordination lock failed safely: {exc}"
        ) from exc


def _canonical_row_bytes(row: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            row,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _reject_secret_bytes(data: bytes) -> None:
    detectors = secret_detectors(data)
    if detectors:
        raise CaptureSecretDetectedError(detectors)


def _preflight_raw_payload(payload: Any) -> None:
    """Reject secrets before any schema constructor can reflect a value."""

    detectors = raw_payload_secret_detectors(payload)
    if detectors:
        raise CaptureSecretDetectedError(detectors)


def _initiated_run_id_for_payload(
    prior: Iterable[Mapping[str, Any]],
    payload: Mapping[str, Any],
    *,
    through_initiation_id: bool = False,
) -> str | None:
    """Return only a run identity already authenticated by the durable ledger.

    A rejected payload is untrusted and may itself put a secret in ``runId`` or
    ``initiationId``.  It is safe to append an incident only when that value
    selects exactly one prior initiation; the returned bytes then come from the
    validated ledger rather than from the rejected request.
    """

    field = "initiationId" if through_initiation_id else "runId"
    candidate = payload.get(field)
    if not isinstance(candidate, str):
        return None
    matches = [
        row
        for row in prior
        if row.get("kind") == "capture-initiation" and row.get(field) == candidate
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("runId"), str):
        return None
    return str(matches[0]["runId"])


def _append_locked(
    transaction: PinnedFileTransaction, row: Mapping[str, Any]
) -> Path | None:
    """Atomically append one canonical row while the caller holds both locks.

    Replacing a fully fsynced sibling temp file avoids a torn final record. The
    parent-directory fsync makes first creation and replacement durable across a
    host crash, which is essential for the initiation denominator boundary.
    """

    encoded = _canonical_row_bytes(row)
    _reject_secret_bytes(encoded)
    try:
        return atomic_append_transaction_jsonl(transaction, encoded)
    except LedgerError as exc:
        raise CaptureRuntimeError(f"capture ledger append failed safely: {exc}") from exc


def _validated_rows(
    transaction: PinnedFileTransaction,
) -> tuple[list[dict[str, Any]], list[Mapping[str, Any]]]:
    raw = [row for _, row in load_transaction_jsonl(transaction)]
    v2 = validate_v2_ledger(raw)
    return raw, v2


@contextmanager
def _capture_transaction(
    target: Path,
    *,
    authorize_mutation: Callable[[PinnedMutationTarget], None] | None = None,
):
    """Normalize pinned-ledger failures at the integrated public boundary."""

    try:
        if authorize_mutation is None:
            with ledger_write_transaction(target) as transaction:
                yield transaction
        else:
            with locked_file_transaction(
                target,
                timeout_seconds=LOCK_TIMEOUT_SECONDS,
                on_directory_fsync=forecasts_module._fsync_directory,
                authorize_mutation=authorize_mutation,
            ) as transaction:
                yield transaction
    except (LedgerError, SafeFileError) as exc:
        raise CaptureRuntimeError(f"capture ledger transaction failed safely: {exc}") from exc


def _report_rows(
    path: Path, *, now: datetime | str
) -> tuple[list[dict[str, Any]], list[Mapping[str, Any]], list[dict[str, Any]]]:
    """Classify identifiable bad V2 lifecycle rows without erasing them.

    Writers use :func:`validate_v2_ledger` and remain fail-closed. Reports also
    fail closed for malformed JSON, unknown versions/kinds, an invalid activation,
    or a lifecycle row with no identifiable run. A known V2 lifecycle row whose
    schema/binding is invalid is annotated in a report-only copy so it remains an
    incomplete denominator member.
    """

    loaded = _load_report_jsonl_snapshot(path)
    report_rows: list[dict[str, Any]] = []
    prior: list[Mapping[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for line_number, original, raw_sha256 in loaded:
        row = dict(original)
        # Keep physical-record identity out of strict schema validation and the
        # durable-prior chain. It exists only in the tolerant report copy so the
        # denominator can distinguish equal decoded JSON from a byte-exact retry.
        row[RAW_RECORD_SHA256_ANNOTATION] = raw_sha256
        version = row.get("schemaVersion")
        kind = row.get("kind")
        kind_is_text = isinstance(kind, str)
        is_known_v2 = kind_is_text and kind in V2_KINDS
        version_is_v2 = (
            isinstance(version, int)
            and not isinstance(version, bool)
            and version == 2
        )
        version_is_legacy = version is None or (
            isinstance(version, int)
            and not isinstance(version, bool)
            and version == 1
        )
        identifiable_v2 = is_known_v2 or version_is_v2
        if not identifiable_v2 and not version_is_legacy:
            raise CaptureSchemaError(
                f"ledger row {line_number}: uninterpretable capture record"
            )
        if not identifiable_v2:
            if kind is not None and not kind_is_text:
                raise CaptureSchemaError(
                    f"ledger row {line_number}: uninterpretable capture record"
                )
            report_rows.append(row)
            continue

        structural_error: CaptureSchemaError | None = None
        # Guard the dispatch fields before membership or validator lookup. JSON
        # arrays/objects are valid decoded values but unhashable in sets/maps.
        # A schemaVersion=2 row or a known V2 kind is still an identifiable V2
        # observation when its counterpart has the wrong type.
        if not kind_is_text or not (
            version is None
            or (isinstance(version, int) and not isinstance(version, bool))
        ):
            structural_error = CaptureSchemaError(_V2_TYPE_FAILURE)
        elif not is_known_v2 or not version_is_v2:
            structural_error = CaptureSchemaError("invalid V2 record dispatch")
        else:
            try:
                validate_v2_record(original, prior)
            except CaptureSchemaError as exc:
                structural_error = exc
            except TypeError as exc:
                # A missed nested type guard must not escape the tolerant report
                # boundary as a traceback or leak its caller-controlled repr.
                structural_error = CaptureSchemaError(_V2_TYPE_FAILURE)
                structural_error.__cause__ = exc

        timing_error: CaptureSchemaError | None = None
        if structural_error is None:
            try:
                validate_v2_record(original, prior, now=now)
            except CaptureSchemaError as exc:
                timing_error = exc
            except TypeError as exc:
                timing_error = CaptureSchemaError(_V2_TYPE_FAILURE)
                timing_error.__cause__ = exc

        error = structural_error or timing_error
        if error is None:
            prior.append(original)
            report_rows.append(row)
            continue
        if kind == "capture-activation":
            raise CaptureSchemaError(f"ledger row {line_number}: {error}") from error
        run_id = row.get("runId")
        if not isinstance(run_id, str) or not run_id.strip():
            raise CaptureSchemaError(
                f"ledger row {line_number}: invalid V2 row has no identifiable runId"
            ) from error
        safe_error = (
            _V2_TYPE_FAILURE
            if isinstance(error.__cause__, TypeError)
            else str(error)
        )
        # Do not pass a non-text kind into downstream grouping maps. Preserve a
        # stable report classification while the physical-line hash still
        # distinguishes the underlying record.
        if not kind_is_text:
            row["kind"] = "invalid-v2-record"
        row["_captureSchemaError"] = safe_error
        invalid.append(
            {
                "lineNumber": line_number,
                "kind": kind if kind_is_text else "invalid-v2-record",
                "runId": run_id,
                "error": safe_error,
            }
        )
        report_rows.append(row)
        # A future boundary is otherwise structurally valid and must remain
        # available to validate later records without cascading false orphans.
        if structural_error is None:
            prior.append(original)
    return report_rows, prior, invalid


def _load_report_jsonl_snapshot(
    path: Path,
) -> list[tuple[int, dict[str, Any], str]]:
    """Secret-scan and decode one exact report input byte snapshot."""

    if not path.exists():
        return []
    durable_bytes = path.read_bytes()
    _reject_secret_bytes(durable_bytes)
    loaded = forecasts_module._load_jsonl_bytes_with_raw_identity(
        durable_bytes, label=path.name
    )
    # JSON escapes can hide a detector pattern from the exact raw-byte scan.
    # Scan the complete decoded row set before returning any value to dispatch,
    # lookup, integrity, or error-reporting logic.
    _preflight_raw_payload([row for _line, row, _identity in loaded])
    return loaded


def validate_capture_ledger(
    path: str | Path, *, now: datetime | str | None = None
) -> tuple[list[dict[str, Any]], list[Mapping[str, Any]]]:
    """Load the mixed ledger strictly and return both raw and validated V2 rows.

    The raw stream is intentionally retained because a post-activation V1 run is
    denominator-eligible and capture-incomplete.  Filtering to V2 would erase it.
    """

    raw = [row for _, row in load_jsonl(path)]
    return raw, validate_v2_ledger(raw, now=now)


def _append_constructed(
    path: str | Path,
    constructor: Callable[..., dict[str, Any]],
    payload: Mapping[str, Any],
    *,
    clock: Clock,
    coordination_lock: str | Path | None,
    authorize_mutation: Callable[[PinnedMutationTarget], None] | None = None,
    authorize_coordination_lock: Callable[[PinnedMutationTarget], None] | None = None,
) -> dict[str, Any]:
    target = Path(path)
    coordination_context = (
        evidence_write_lock(coordination_lock)
        if authorize_coordination_lock is None
        else _activation_coordination_lock(
            coordination_lock,
            authorize_mutation=authorize_coordination_lock,
        )
    )
    with coordination_context, _capture_transaction(
        target, authorize_mutation=authorize_mutation
    ) as transaction:
        _raw, prior = _validated_rows(transaction)
        try:
            _preflight_raw_payload(payload)
            row = constructor(payload, prior_rows=prior, clock=clock)
            _append_locked(transaction, row)
        except CaptureSecretDetectedError as exc:
            run_id = _initiated_run_id_for_payload(prior, payload)
            if run_id is not None:
                _record_secret_invalidation_locked(
                    transaction,
                    prior,
                    run_id=run_id,
                    error=exc,
                    clock=clock,
                )
            raise
    return row


def append_capture_activation(
    path: str | Path,
    payload: Mapping[str, Any],
    *,
    clock: Clock = utc_now,
    coordination_lock: str | Path | None = None,
) -> dict[str, Any]:
    live_roots = _os_account_live_write_roots()
    protected_root_identities = _capture_live_root_identities(live_roots)
    ledger_authorizer = _activation_namespace_authorizer(
        path, protected_root_identities
    )
    coordination_authorizer = (
        None
        if coordination_lock is None
        else _activation_namespace_authorizer(
            coordination_lock, protected_root_identities
        )
    )
    if _is_live_activation_target(path, live_roots):
        raise CaptureRuntimeError(
            "direct live capture activation is blocked in this release"
        )
    return _append_constructed(
        path,
        make_capture_activation,
        payload,
        clock=clock,
        coordination_lock=coordination_lock,
        authorize_mutation=ledger_authorizer,
        authorize_coordination_lock=coordination_authorizer,
    )


def append_capture_initiation(
    path: str | Path,
    payload: Mapping[str, Any],
    *,
    clock: Clock = utc_now,
    coordination_lock: str | Path | None = None,
) -> tuple[dict[str, Any], bool]:
    """Append the first durable run boundary, or return its idempotent replay."""

    target = Path(path)
    with evidence_write_lock(coordination_lock), _capture_transaction(target) as transaction:
        _raw, prior = _validated_rows(transaction)
        _preflight_raw_payload(payload)
        row = make_capture_initiation(payload, prior_rows=prior, clock=clock)
        existing = any(
            item.get("kind") == "capture-initiation"
            and item.get("initiationId") == row["initiationId"]
            for item in prior
        )
        if not existing:
            _append_locked(transaction, row)
    return row, not existing


def _bytes_match_ref(data: bytes, ref: Mapping[str, Any], field: str) -> None:
    _reject_secret_bytes(data)
    normalized = validate_artifact_ref(
        {key: ref[key] for key in ("path", "sha256", "bytes") if key in ref}
    )
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != normalized["bytes"] or digest != normalized["sha256"]:
        raise CaptureRuntimeError(f"{field} bytes do not match their artifact reference")


def _record_secret_invalidation_locked(
    transaction: PinnedFileTransaction,
    prior: list[Mapping[str, Any]],
    *,
    run_id: str,
    error: CaptureSecretDetectedError,
    clock: Clock,
) -> None:
    evidence = "incident:secret-detected:" + ",".join(error.detectors)
    invalidation = make_capture_invalidation(
        {
            "runId": run_id,
            "reason": "secret-detected",
            "operator": "capture-runtime",
            "evidenceRef": evidence,
        },
        prior_rows=prior,
        clock=clock,
    )
    _append_locked(transaction, invalidation)


def _activation_for_attempt(
    prior: Iterable[Mapping[str, Any]], initiation_id: str
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    initiation = next(
        (
            row
            for row in prior
            if row.get("kind") == "capture-initiation"
            and row.get("initiationId") == initiation_id
        ),
        None,
    )
    if initiation is None:
        raise CaptureRuntimeError(f"unknown capture initiation: {initiation_id}")
    activation = next(
        (
            row
            for row in prior
            if row.get("kind") == "capture-activation"
            and row.get("activationId") == initiation.get("activationId")
        ),
        None,
    )
    if activation is None:
        raise CaptureRuntimeError("capture initiation has no activation")
    return initiation, activation


def _forecast_identity(row: Mapping[str, Any]) -> dict[str, str]:
    outcome = row["sharedOutcome"]
    return forecast_request_identity_v2(
        str(row["runId"]),
        str(outcome["outcomeId"]),
        str(outcome["fingerprint"]),
        str(row["evidenceCutoffAt"]),
        str(outcome["claim"]),
        str(outcome["resolutionDate"]),
        str(outcome["resolvedBy"]),
        str(outcome["materiality"]),
        str(outcome["actionIfTrue"]),
        str(outcome["actionIfFalse"]),
    )


def _forecast_block(row: Mapping[str, Any]) -> dict[str, Any]:
    outcome = row["sharedOutcome"]
    return forecast_request_block_v2(
        str(row["runId"]),
        str(outcome["outcomeId"]),
        str(outcome["fingerprint"]),
        str(row["evidenceCutoffAt"]),
        str(outcome["claim"]),
        str(outcome["resolutionDate"]),
        str(outcome["resolvedBy"]),
        str(outcome["materiality"]),
        str(outcome["actionIfTrue"]),
        str(outcome["actionIfFalse"]),
    )


def _prompt_bindings(
    activation: Mapping[str, Any],
    decision_ref: Mapping[str, Any],
) -> tuple[bytes, ...]:
    return (
        f"commit={activation['runtimeSourceCommit']}".encode("utf-8"),
        f"blob={decision_ref['gitBlob']}".encode("utf-8"),
        f"sha256={decision_ref['sha256']}".encode("utf-8"),
    )


def _verify_visible_inputs(
    *,
    store: ArtifactStore,
    references: Mapping[str, Mapping[str, Any]],
    visible_inputs: Mapping[str, bytes],
    expected_seats: set[str],
    bindings: tuple[bytes, ...],
    request_row: Mapping[str, Any],
) -> None:
    if set(references) != expected_seats:
        raise CaptureRuntimeError("seat input artifact references must exactly match seatPlan")
    if set(visible_inputs) != expected_seats:
        raise CaptureRuntimeError("visible input bytes must exactly match seatPlan")
    expected_request = _forecast_block(request_row)
    for seat in sorted(expected_seats):
        ref = references[seat]
        data = visible_inputs[seat]
        store.verify(ref)
        _bytes_match_ref(data, ref, f"visible input for {seat}")
        missing = [
            value.decode("utf-8") for value in bindings if data.count(value) != 1
        ]
        if missing:
            raise CaptureRuntimeError(
                f"visible input for {seat} must contain each uniform binding exactly once: "
                f"{missing}"
            )
        try:
            visible_request = parse_forecast_request_binding_v2(data)
        except (CaptureSchemaError, TypeError, ValueError, RecursionError) as exc:
            raise _RetainedArtifactParseError(_PROMPT_PARSE_FAILURE) from exc
        if visible_request != expected_request:
            raise CaptureRuntimeError(
                f"visible input for {seat} forecast request block differs from "
                "the sealed shared target"
            )


def append_council_attempt_v2(
    path: str | Path,
    payload: Mapping[str, Any],
    *,
    artifact_store: ArtifactStore,
    decision_before_bytes: bytes,
    seat_input_artifacts: Mapping[str, Mapping[str, Any]],
    visible_inputs: Mapping[str, bytes],
    clock: Clock = utc_now,
    coordination_lock: str | Path | None = None,
) -> dict[str, Any]:
    """Preflight exact prompt bindings and durably append the V2 attempt."""

    target = Path(path)
    with evidence_write_lock(coordination_lock), _capture_transaction(target) as transaction:
        _raw, prior = _validated_rows(transaction)
        try:
            _preflight_raw_payload(
                {"payload": payload, "seatInputArtifacts": seat_input_artifacts}
            )
            initiation, activation = _activation_for_attempt(
                prior, str(payload.get("initiationId", ""))
            )
            decision_ref = payload.get("decisionBeforeArtifact")
            if not isinstance(decision_ref, Mapping):
                raise CaptureRuntimeError("decisionBeforeArtifact must be an object")
            artifact_store.verify(
                {key: decision_ref[key] for key in ("path", "sha256", "bytes")}
            )
            _bytes_match_ref(decision_before_bytes, decision_ref, "decision-before")
            verify_git_blob_oid(decision_before_bytes, str(decision_ref["gitBlob"]))
            expected = {
                str(item.get("seatId"))
                for item in payload.get("seatPlan", [])
                if isinstance(item, Mapping)
            }
            manifest = seat_input_manifest_sha256(seat_input_artifacts)
            decision_link = payload.get("sharedOutcome", {}).get("decisionLink")
            if not isinstance(decision_link, str) or (
                f"inputManifestSha256={manifest}" not in decision_link
            ):
                raise CaptureRuntimeError(
                    "sharedOutcome.decisionLink must bind inputManifestSha256"
                )
            row = make_council_attempt_v2(payload, prior_rows=prior, clock=clock)
            if row["runId"] != initiation["runId"]:
                raise CaptureRuntimeError(
                    "constructed attempt changed the initiated run identity"
                )
            _verify_visible_inputs(
                store=artifact_store,
                references=seat_input_artifacts,
                visible_inputs=visible_inputs,
                expected_seats=expected,
                bindings=_prompt_bindings(activation, decision_ref),
                request_row=row,
            )
            _append_locked(transaction, row)
        except CaptureSecretDetectedError as exc:
            run_id = _initiated_run_id_for_payload(
                prior, payload, through_initiation_id=True
            )
            if run_id is not None:
                _record_secret_invalidation_locked(
                    transaction,
                    prior,
                    run_id=run_id,
                    error=exc,
                    clock=clock,
                )
            raise
    return row


def append_council_seats_finished(
    path: str | Path,
    payload: Mapping[str, Any],
    *,
    clock: Clock = utc_now,
    coordination_lock: str | Path | None = None,
) -> dict[str, Any]:
    return _append_constructed(
        path,
        make_council_seats_finished,
        payload,
        clock=clock,
        coordination_lock=coordination_lock,
    )


def _parse_baseline(data: bytes) -> Mapping[str, Any]:
    try:
        value = strict_json_loads(data)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        CaptureSchemaError,
        TypeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise _RetainedArtifactParseError(_BASELINE_PARSE_FAILURE) from exc
    if not isinstance(value, Mapping):
        raise CaptureRuntimeError("decision-before artifact must contain a JSON object")
    return value


def _verify_visible_outputs(
    row: Mapping[str, Any], visible_outputs: Mapping[str, bytes]
) -> None:
    submitted = {
        result["seatId"]: result
        for result in row["seatResults"]
        if result["state"] == "submitted"
    }
    if set(visible_outputs) != set(submitted):
        raise CaptureRuntimeError("visible output bytes must exactly match submitted seats")
    declarations = {item["seatId"] for item in row["noFindings"]}
    probabilities = {
        prediction["seat"]: prediction["probability"]
        for prediction in row["predictions"]
    }
    request = _forecast_identity(row)
    visible_findings_by_seat: dict[str, Any] = {}
    for seat, result in submitted.items():
        data = visible_outputs[seat]
        _bytes_match_ref(data, result["outputArtifact"], f"visible output for {seat}")
        try:
            visible = strict_json_loads(data)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            CaptureSchemaError,
            TypeError,
            ValueError,
            RecursionError,
        ) as exc:
            raise _RetainedArtifactParseError(_OUTPUT_PARSE_FAILURE) from exc
        capture = visible.get("capture") if isinstance(visible, Mapping) else None
        if not isinstance(capture, Mapping):
            raise CaptureRuntimeError(
                f"visible output for {seat} must contain a structured capture envelope"
            )
        required_capture_keys = {
            "seatId",
            "sharedProbability",
            "runId",
            "outcomeId",
            "outcomeFingerprint",
            "evidenceCutoffAt",
            "forecastRequestSha256",
            "inputArtifactSha256",
            "findings",
        }
        allowed_capture_keys = required_capture_keys | {"kind"}
        if set(capture) - allowed_capture_keys or not required_capture_keys.issubset(
            capture
        ):
            raise CaptureRuntimeError(
                f"visible output capture envelope for {seat} has missing or unknown keys"
            )
        expected_identity = {
            **request,
            "seatId": seat,
            "inputArtifactSha256": result["inputArtifact"]["sha256"],
        }
        for field, expected in expected_identity.items():
            if capture.get(field) != expected:
                raise CaptureRuntimeError(
                    f"visible output capture.{field} for {seat} differs from "
                    "the frozen request"
                )
        if capture.get("seatId") != seat:
            raise CaptureRuntimeError(
                f"visible output capture.seatId for {seat} does not match its seat"
            )
        probability = capture.get("sharedProbability")
        if (
            isinstance(probability, bool)
            or not isinstance(probability, int)
            or not 0 <= probability <= 100
        ):
            raise CaptureRuntimeError(
                f"visible output capture.sharedProbability for {seat} "
                "must be an integer from 0 to 100"
            )
        if probability != probabilities.get(seat):
            raise CaptureRuntimeError(
                f"visible output capture.sharedProbability for {seat} "
                "differs from the sealed prediction"
            )
        if seat in declarations and capture.get("kind") != "no-findings":
            raise CaptureRuntimeError(
                f"visible output for {seat} lacks its structured no-findings declaration"
            )
        if seat not in declarations and capture.get("kind") == "no-findings":
            raise CaptureRuntimeError(
                f"visible output for {seat} claims no-findings despite retained findings"
            )
        visible_findings_by_seat[seat] = capture["findings"]
    try:
        validate_visible_output_findings(
            run_id=str(row["runId"]),
            submitted_seats=submitted,
            visible_findings_by_seat=visible_findings_by_seat,
            completion_findings=row["findings"],
            no_findings_seats=declarations,
        )
    except FindingError as exc:
        raise CaptureRuntimeError(
            "visible output findings differ from the sealed completion findings"
        ) from exc


def append_council_v2(
    path: str | Path,
    payload: Mapping[str, Any],
    *,
    artifact_store: ArtifactStore,
    decision_before_bytes: bytes,
    seat_input_artifacts: Mapping[str, Mapping[str, Any]],
    visible_inputs: Mapping[str, bytes],
    visible_outputs: Mapping[str, bytes],
    clock: Clock = utc_now,
    coordination_lock: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify custody, normalize findings, and atomically seal one V2 council."""

    target = Path(path)
    with evidence_write_lock(coordination_lock), _capture_transaction(target) as transaction:
        _raw, prior = _validated_rows(transaction)
        try:
            _preflight_raw_payload(
                {"payload": payload, "seatInputArtifacts": seat_input_artifacts}
            )
            prepared = prepare_council_v2(payload, prior_rows=prior)
            attempt = next(
                item
                for item in prior
                if item.get("kind") == "council-attempt-v2"
                and item.get("runId") == prepared["runId"]
            )
            activation = next(
                item
                for item in prior
                if item.get("kind") == "capture-activation"
                and item.get("activationId") == prepared["activationId"]
            )
            decision_ref = prepared["decisionBeforeArtifact"]
            artifact_store.verify(
                {key: decision_ref[key] for key in ("path", "sha256", "bytes")}
            )
            _bytes_match_ref(decision_before_bytes, decision_ref, "decision-before")
            verify_git_blob_oid(decision_before_bytes, str(decision_ref["gitBlob"]))
            baseline = _parse_baseline(decision_before_bytes)

            submitted_results = {
                item["seatId"]: item
                for item in prepared["seatResults"]
                if item["state"] == "submitted"
            }
            all_results = {
                item["seatId"]: item for item in prepared["seatResults"]
            }
            planned_seats = {item["seatId"] for item in prepared["seatPlan"]}
            expected_manifest = seat_input_manifest_sha256(seat_input_artifacts)
            if f"inputManifestSha256={expected_manifest}" not in attempt[
                "sharedOutcome"
            ]["decisionLink"]:
                raise CaptureRuntimeError(
                    "completion input artifacts differ from attempt preflight"
                )
            for seat, result in all_results.items():
                if result["inputArtifact"] != seat_input_artifacts.get(seat):
                    raise CaptureRuntimeError(
                        f"planned input artifact for {seat} differs from attempt preflight"
                    )
            if "blind" in planned_seats:
                blind_input = seat_input_artifacts.get("blind")
                if not isinstance(blind_input, Mapping):
                    raise CaptureRuntimeError(
                        "planned blind seat has no captured visible input artifact"
                    )
                expected_blind_brief = blind_brief_identity(
                    prepared["runId"], str(blind_input.get("path", ""))
                )
                if prepared["blindSeat"]["brief"] != expected_blind_brief:
                    raise CaptureRuntimeError(
                        "blindSeat.brief must bind the blind seat visible input artifact"
                    )
            _verify_visible_inputs(
                store=artifact_store,
                references=seat_input_artifacts,
                visible_inputs=visible_inputs,
                expected_seats=planned_seats,
                bindings=_prompt_bindings(activation, decision_ref),
                request_row=attempt,
            )
            for result in submitted_results.values():
                artifact_store.verify(result["outputArtifact"])
            _verify_visible_outputs(prepared, visible_outputs)

            prior_findings = [
                finding
                for item in prior
                if item.get("kind") == "council-v2"
                for finding in item.get("findings", [])
            ]
            summary = summarize_findings(
                run_id=prepared["runId"],
                submitted_seats=submitted_results,
                findings=prepared["findings"],
                no_findings=prepared["noFindings"],
                baseline=baseline,
                output_artifacts={
                    seat: result["outputArtifact"]
                    for seat, result in submitted_results.items()
                },
                prior_findings=prior_findings,
            )
            # The completion timestamp is the append boundary, not command
            # start: every fallible custody, normalization, and prepared-schema
            # check above completes before the system clock is sampled here.
            row = finalize_council_v2(prepared, prior_rows=prior, clock=clock)
            _append_locked(transaction, row)
        except CaptureSecretDetectedError as exc:
            run_id = _initiated_run_id_for_payload(prior, payload)
            if run_id is not None:
                _record_secret_invalidation_locked(
                    transaction,
                    prior,
                    run_id=run_id,
                    error=exc,
                    clock=clock,
                )
            raise
        except CaptureSchemaError as exc:
            raise CaptureRuntimeError(str(exc)) from exc
    return row, summary


def append_capture_invalidation(
    path: str | Path,
    payload: Mapping[str, Any],
    *,
    clock: Clock = utc_now,
    coordination_lock: str | Path | None = None,
) -> dict[str, Any]:
    target = Path(path)
    with evidence_write_lock(coordination_lock), _capture_transaction(target) as transaction:
        _raw, prior = _validated_rows(transaction)
        try:
            _preflight_raw_payload(payload)
            row = make_capture_invalidation(payload, prior_rows=prior, clock=clock)
            _append_locked(transaction, row)
        except CaptureSecretDetectedError as exc:
            # The requested metadata is never persisted. Seal a fixed, non-secret
            # fallback before releasing either lock so a normally handled secret
            # cannot leave an otherwise eligible run without its invalidation.
            run_id = _initiated_run_id_for_payload(prior, payload)
            if run_id is not None:
                _record_secret_invalidation_locked(
                    transaction,
                    prior,
                    run_id=run_id,
                    error=exc,
                    clock=clock,
                )
            raise
    return row


def append_capture_resolution(
    log_path: str | Path,
    events_path: str | Path,
    *,
    outcome_id: str,
    came_true: bool | None,
    evidence: str,
    resolver: str,
    resolved_at: str,
    method: str,
    reviewer: str | None = None,
    void_reason: str | None = None,
    supersedes_resolution_id: str | None = None,
    coordination_lock: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve exactly one sealed V2 issuance before touching its sidecar."""

    _preflight_raw_payload(
        {
            "logPath": os.fspath(log_path),
            "eventsPath": os.fspath(events_path),
            "outcomeId": outcome_id,
            "cameTrue": came_true,
            "evidence": evidence,
            "resolver": resolver,
            "resolvedAt": resolved_at,
            "method": method,
            "reviewer": reviewer,
            "voidReason": void_reason,
            "supersedesResolutionId": supersedes_resolution_id,
            "coordinationLock": (
                None
                if coordination_lock is None
                else os.fspath(coordination_lock)
            ),
        }
    )
    with evidence_write_lock(coordination_lock), _capture_transaction(
        Path(log_path)
    ) as log_transaction:
        _raw, rows = _validated_rows(log_transaction)
        attempts = [
            row
            for row in rows
            if row.get("kind") == "council-attempt-v2"
            and row.get("sharedOutcome", {}).get("outcomeId") == outcome_id
        ]
        if len(attempts) != 1:
            raise CaptureRuntimeError(
                f"V2 outcomeId must have exactly one valid attempt issuance: {outcome_id}"
            )
        attempt = attempts[0]
        completions = [
            row
            for row in rows
            if row.get("kind") == "council-v2"
            and row.get("runId") == attempt["runId"]
        ]
        if len(completions) != 1:
            raise CaptureRuntimeError(
                f"V2 outcomeId must have exactly one sealed council-v2 issuance: {outcome_id}"
            )
        completion = completions[0]
        outcome = attempt["sharedOutcome"]
        prospective_event = {
            "outcomeId": outcome_id,
            "resolutionDate": outcome["resolutionDate"],
            "outcomeFingerprint": outcome["fingerprint"],
            "resolvedAt": resolved_at,
        }
        try:
            validate_resolution_event_integrity(
                prospective_event,
                outcome,
                issuance_at=completion["finalizedAt"],
                as_of=resolved_at,
                require_outcome_fingerprint=True,
            )
        except ResolutionIntegrityError as exc:
            raise CaptureRuntimeError(
                f"capture resolution integrity failure: {exc}"
            ) from exc
        # The shared coordination lock is already held. Passing None prevents
        # non-reentrant acquisition. A same-parent sidecar reuses the already
        # pinned parent flock and original log-lock identity; a genuinely
        # different parent takes its own independent pinned transaction.
        events_target = Path(os.path.abspath(os.fspath(events_path)))
        if events_target == log_transaction.path:
            raise CaptureRuntimeError(
                "capture resolution sidecar must be distinct from the capture ledger"
            )
        sidecar_transaction: PinnedFileTransaction | None = None
        if events_target.parent == log_transaction.path.parent:
            try:
                sidecar_transaction = log_transaction.sibling(events_target.name)
            except SafeFileError as exc:
                raise CaptureRuntimeError(
                    f"capture resolution sidecar is unsafe: {exc}"
                ) from exc
        try:
            return append_resolution(
                events_target,
                outcome_id=outcome_id,
                resolution_date=outcome["resolutionDate"],
                outcome_fingerprint=outcome["fingerprint"],
                came_true=came_true,
                evidence=evidence,
                resolver=resolver,
                resolved_at=resolved_at,
                method=method,
                reviewer=reviewer,
                void_reason=void_reason,
                supersedes_resolution_id=supersedes_resolution_id,
                coordination_lock=None,
                _row_writer=_append_locked,
                _transaction=sidecar_transaction,
            )
        except LedgerError as exc:
            raise CaptureRuntimeError(
                f"capture resolution append failed safely: {exc}"
            ) from exc


def _annotate_report_provenance(
    rows: list[dict[str, Any]],
    invalid: list[dict[str, Any]],
    *,
    artifact_store: ArtifactStore,
) -> tuple[int, dict[str, Mapping[str, Any]]]:
    """Fail closed when retained prompt/response evidence disagrees with ledger."""

    attempts = {
        row.get("runId"): row
        for row in rows
        if row.get("kind") == "council-attempt-v2"
        and "_captureSchemaError" not in row
    }
    activations = {
        row.get("activationId"): row
        for row in rows
        if row.get("kind") == "capture-activation"
        and "_captureSchemaError" not in row
    }
    added = 0
    finding_summaries: dict[str, Mapping[str, Any]] = {}
    prior_findings: list[Mapping[str, Any]] = []
    for line_number, row in enumerate(rows, start=1):
        if row.get("kind") != "council-v2" or "_captureSchemaError" in row:
            continue
        run_id = str(row.get("runId", ""))
        try:
            attempt = attempts[run_id]
            activation = activations[attempt["activationId"]]
            results = {
                result["seatId"]: result for result in row["seatResults"]
            }
            references = {
                seat: result["inputArtifact"] for seat, result in results.items()
            }
            visible_inputs = {
                seat: artifact_store.read_verified(ref)
                for seat, ref in references.items()
            }
            _verify_visible_inputs(
                store=artifact_store,
                references=references,
                visible_inputs=visible_inputs,
                expected_seats={item["seatId"] for item in row["seatPlan"]},
                bindings=_prompt_bindings(
                    activation, row["decisionBeforeArtifact"]
                ),
                request_row=attempt,
            )
            visible_outputs = {
                seat: artifact_store.read_verified(result["outputArtifact"])
                for seat, result in results.items()
                if result["state"] == "submitted"
            }
            _verify_visible_outputs(row, visible_outputs)

            decision_ref = row["decisionBeforeArtifact"]
            baseline_bytes = artifact_store.read_verified(
                {
                    key: decision_ref[key]
                    for key in ("path", "sha256", "bytes")
                }
            )
            verify_git_blob_oid(
                baseline_bytes, str(decision_ref["gitBlob"])
            )
            baseline = _parse_baseline(baseline_bytes)
            submitted_results = {
                seat: result
                for seat, result in results.items()
                if result["state"] == "submitted"
            }
            summary_identity = finding_summary_record_key(line_number, row)
            if summary_identity is None:
                raise CaptureRuntimeError(
                    "report completion lacks physical record identity"
                )
            finding_summaries[summary_identity] = summarize_findings(
                run_id=run_id,
                submitted_seats=submitted_results,
                findings=row["findings"],
                no_findings=row["noFindings"],
                baseline=baseline,
                output_artifacts={
                    seat: result["outputArtifact"]
                    for seat, result in submitted_results.items()
                },
                prior_findings=prior_findings,
            )
        except _RetainedArtifactParseError as exc:
            error = f"report-time {exc}"
            row["_captureSchemaError"] = error
            invalid.append(
                {
                    "lineNumber": line_number,
                    "kind": "council-v2",
                    "runId": run_id,
                    "error": error,
                }
            )
            added += 1
        except (
            ArtifactError,
            CaptureRuntimeError,
            CaptureSchemaError,
            FindingError,
            KeyError,
        ) as exc:
            error = f"report-time forecast provenance failure: {exc}"
            row["_captureSchemaError"] = error
            invalid.append(
                {
                    "lineNumber": line_number,
                    "kind": "council-v2",
                    "runId": run_id,
                    "error": error,
                }
            )
            added += 1
        finally:
            # A structurally valid sealed row retains its globally reserved
            # finding/group identities even when later custody or baseline
            # revalidation makes that run ineligible for headline analysis.
            row_findings = row.get("findings")
            if isinstance(row_findings, list):
                prior_findings.extend(
                    item for item in row_findings if isinstance(item, Mapping)
                )
    return added, finding_summaries


def capture_report(
    log_path: str | Path,
    events_path: str | Path,
    *,
    artifact_store: ArtifactStore,
    as_of: datetime | str,
) -> dict[str, Any]:
    """Produce the capture-only report from strict mixed-ledger inputs."""

    raw, validation_prior, invalid = _report_rows(Path(log_path), now=as_of)
    _provenance_invalid_count, finding_summaries = _annotate_report_provenance(
        raw, invalid, artifact_store=artifact_store
    )
    valid_v2 = [
        row
        for row in raw
        if row.get("schemaVersion") == 2
        and row.get("kind") in V2_KINDS
        and "_captureSchemaError" not in row
    ]
    # Reuse the mature append-only resolution validator, but keep these events in
    # their own sidecar so the V1 audit never sees V2 outcome IDs.
    event_rows = _load_report_jsonl_snapshot(Path(events_path))
    events = [row for _, row, _raw_sha256 in event_rows]
    unrelated = [row.get("kind") for row in events if row.get("kind") != "outcome-resolution"]
    if unrelated:
        raise CaptureRuntimeError(
            "capture resolution sidecar contains unrelated event kinds: "
            + ", ".join(str(kind) for kind in unrelated)
        )
    prior_events: list[dict[str, Any]] = []
    for event in events:
        forecasts_module._validate_resolution_event(event, prior_events)
        prior_events.append(event)
    issued_outcome_ids = {
        outcome.get("outcomeId")
        for row in validation_prior
        if row.get("kind") == "council-attempt-v2"
        for outcome in [row.get("sharedOutcome")]
        if isinstance(outcome, Mapping) and isinstance(outcome.get("outcomeId"), str)
    }
    unknown_outcome_ids = sorted(
        {
            row["outcomeId"]
            for row in events
            if row.get("outcomeId") not in issued_outcome_ids
        }
    )
    if unknown_outcome_ids:
        raise CaptureRuntimeError(
            "capture resolution sidecar references outcomes with no V2 attempt issuance: "
            + ", ".join(unknown_outcome_ids)
        )
    attempts_by_outcome_id = {
        row["sharedOutcome"]["outcomeId"]: row
        for row in validation_prior
        if row.get("kind") == "council-attempt-v2"
        and isinstance(row.get("sharedOutcome"), Mapping)
    }
    completions_by_run_id = {
        row.get("runId"): row
        for row in validation_prior
        if row.get("kind") == "council-v2"
    }
    for event in events:
        attempt = attempts_by_outcome_id[event["outcomeId"]]
        completion = completions_by_run_id.get(attempt.get("runId"))
        if completion is None or not isinstance(completion.get("finalizedAt"), str):
            raise CaptureRuntimeError(
                "capture resolution sidecar references an outcome with no sealed issuance: "
                + str(event["outcomeId"])
            )
        try:
            validate_resolution_event_integrity(
                event,
                attempt["sharedOutcome"],
                issuance_at=completion["finalizedAt"],
                as_of=as_of,
                require_outcome_fingerprint=True,
            )
        except ResolutionIntegrityError as exc:
            raise CaptureRuntimeError(
                f"capture resolution integrity failure: {exc}"
            ) from exc
    report = analyze_capture_data(
        raw,
        as_of=as_of,
        resolution_events=events,
        artifact_integrity=artifact_store.verify,
        finding_summaries=finding_summaries,
    )
    report["transactionEscrows"] = transaction_escrow_inventory(
        log_path, events_path
    )
    validated_v2_count = len(valid_v2)
    invalid_v2_count = len(invalid)
    non_v2_count = len(raw) - validated_v2_count - invalid_v2_count
    if non_v2_count < 0:  # defensive invariant for future report classifiers
        raise CaptureRuntimeError(
            "capture ledger record classifications exceed physical record count"
        )
    resolution_provenance = report["resolutionProvenanceDiagnostics"]
    report["ledger"] = {
        "rawRecordCount": len(raw),
        "validatedV2RecordCount": validated_v2_count,
        "invalidV2RecordCount": invalid_v2_count,
        "nonV2RecordCount": non_v2_count,
        "recordCountReconciles": (
            validated_v2_count + invalid_v2_count + non_v2_count == len(raw)
        ),
        "invalidV2Records": invalid,
        "invalidResolutionRecordCount": resolution_provenance[
            "invalidLedgerResolutionEventCount"
        ],
        "invalidResolutionRecords": resolution_provenance[
            "invalidLedgerResolutionEvents"
        ],
        "captureResolutionEventCount": sum(
            item.get("kind") == "outcome-resolution" for item in events
        ),
    }
    return report


__all__ = [
    "CaptureRuntimeError",
    "CaptureSecretDetectedError",
    "append_capture_activation",
    "append_capture_initiation",
    "append_capture_invalidation",
    "append_capture_resolution",
    "append_council_attempt_v2",
    "append_council_seats_finished",
    "append_council_v2",
    "capture_report",
    "forecast_request_binding_v2",
    "forecast_request_identity_v2",
    "seat_input_manifest_sha256",
    "validate_capture_ledger",
]
