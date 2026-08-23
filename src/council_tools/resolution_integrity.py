"""Cross-record integrity checks for append-only outcome resolutions.

This module deliberately does not read ledgers or choose correction/supersession
winners.  A caller first identifies the canonical issued outcome and the active
resolution event, then calls :func:`validate_resolution_event_integrity`.

Integration contract:

* V1 audit supplies the canonical attempt outcome and the timestamp of the
  completion that actually issued the scored predictions.  Existing legacy
  events may set ``require_outcome_fingerprint=False``; newly written events
  should carry and require ``outcomeFingerprint``.
* V2 ``capture_report`` supplies the attempt's ``sharedOutcome`` and the matched
  completion's ``finalizedAt`` issuance boundary, with
  ``require_outcome_fingerprint=True``.

The report's ``as_of`` is always supplied explicitly.  This keeps future-dated
resolution events from entering a historical report merely because they are
already present in an append-only sidecar.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ResolutionIntegrityError(ValueError):
    """A resolution is not bound to its issued outcome and report horizon."""


@dataclass(frozen=True)
class ValidatedResolutionEvent:
    outcome_id: str
    resolution_date: date
    outcome_fingerprint: str | None
    resolved_at: datetime
    issuance_at: datetime
    report_as_of: datetime


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResolutionIntegrityError(f"{field} must be an object")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResolutionIntegrityError(f"{field} must be non-empty text")
    return value


def _date(value: Any, field: str) -> date:
    raw = _text(value, field)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ResolutionIntegrityError(f"{field} must be YYYY-MM-DD") from exc
    if str(parsed) != raw:
        raise ResolutionIntegrityError(f"{field} must be canonical YYYY-MM-DD")
    return parsed


def _utc(value: datetime | str, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ResolutionIntegrityError(
                f"{field} must be an ISO-8601 timestamp"
            ) from exc
    else:
        raise ResolutionIntegrityError(
            f"{field} must be an aware datetime or ISO-8601 text"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResolutionIntegrityError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _fingerprint(value: Any, field: str) -> str:
    raw = _text(value, field)
    if _SHA256.fullmatch(raw) is None:
        raise ResolutionIntegrityError(f"{field} must be a lowercase SHA-256 digest")
    return raw


def validate_resolution_event_integrity(
    event: Mapping[str, Any],
    canonical_outcome: Mapping[str, Any],
    *,
    issuance_at: datetime | str,
    as_of: datetime | str,
    require_outcome_fingerprint: bool,
) -> ValidatedResolutionEvent:
    """Strictly bind one resolution event to one issued outcome.

    Equality is allowed at both temporal boundaries.  A resolution fails when
    it is earlier than issuance/completion or later than the report cutoff.
    """

    event = _mapping(event, "resolution event")
    outcome = _mapping(canonical_outcome, "canonical outcome")
    if not isinstance(require_outcome_fingerprint, bool):
        raise ResolutionIntegrityError(
            "require_outcome_fingerprint must be boolean"
        )

    canonical_id = _text(outcome.get("outcomeId"), "canonical outcome.outcomeId")
    event_id = _text(event.get("outcomeId"), "resolution event.outcomeId")
    if event_id != canonical_id:
        raise ResolutionIntegrityError(
            "resolution event.outcomeId differs from canonical outcome"
        )

    canonical_date_text = _text(
        outcome.get("resolutionDate"), "canonical outcome.resolutionDate"
    )
    resolution_date = _date(
        canonical_date_text, "canonical outcome.resolutionDate"
    )
    event_date_text = _text(
        event.get("resolutionDate"), "resolution event.resolutionDate"
    )
    _date(event_date_text, "resolution event.resolutionDate")
    if event_date_text != canonical_date_text:
        raise ResolutionIntegrityError(
            "resolution event.resolutionDate differs from canonical outcome"
        )

    canonical_fingerprint_value = outcome.get("fingerprint")
    event_fingerprint_value = event.get("outcomeFingerprint")
    outcome_fingerprint: str | None = None
    if require_outcome_fingerprint:
        canonical_fingerprint = _fingerprint(
            canonical_fingerprint_value, "canonical outcome.fingerprint"
        )
        event_fingerprint = _fingerprint(
            event_fingerprint_value, "resolution event.outcomeFingerprint"
        )
        if event_fingerprint != canonical_fingerprint:
            raise ResolutionIntegrityError(
                "resolution event.outcomeFingerprint differs from canonical outcome"
            )
        outcome_fingerprint = event_fingerprint
    elif event_fingerprint_value is not None:
        canonical_fingerprint = _fingerprint(
            canonical_fingerprint_value, "canonical outcome.fingerprint"
        )
        event_fingerprint = _fingerprint(
            event_fingerprint_value, "resolution event.outcomeFingerprint"
        )
        if event_fingerprint != canonical_fingerprint:
            raise ResolutionIntegrityError(
                "resolution event.outcomeFingerprint differs from canonical outcome"
            )
        outcome_fingerprint = event_fingerprint

    resolved_at = _utc(event.get("resolvedAt"), "resolution event.resolvedAt")
    issuance_boundary = _utc(issuance_at, "issuance_at")
    report_cutoff = _utc(as_of, "as_of")
    if resolved_at < issuance_boundary:
        raise ResolutionIntegrityError(
            "resolution event.resolvedAt precedes issuance/completion"
        )
    if resolved_at > report_cutoff:
        raise ResolutionIntegrityError(
            "resolution event.resolvedAt follows report as_of"
        )

    return ValidatedResolutionEvent(
        outcome_id=event_id,
        resolution_date=resolution_date,
        outcome_fingerprint=outcome_fingerprint,
        resolved_at=resolved_at,
        issuance_at=issuance_boundary,
        report_as_of=report_cutoff,
    )


__all__ = [
    "ResolutionIntegrityError",
    "ValidatedResolutionEvent",
    "validate_resolution_event_integrity",
]
